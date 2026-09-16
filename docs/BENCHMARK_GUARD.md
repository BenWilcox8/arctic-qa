# Benchmark cost and quota guard

This document describes the guard of the live benchmark evaluation and the "Live benchmarking" section of the corpus viewer.
The guard watches what the streaming abstention evaluation costs.
It pauses one model when a budget or a quota becomes urgent.
It makes no paid model call and it never stops a process.

Read `docs/ABSTENTION_EVALUATION.md` first.
The guard reads the records of the streaming evaluator that this document describes.

## The three meters

The evaluation runs on three vendors and each vendor bills in a different currency.

| Vendor | Bills | Bound |
| --- | --- | --- |
| `google_gemini` | Google API credits, through the shared paid-call ledger, phase `benchmark_evaluation` | USD 200, and the evaluation ceiling of the ledger |
| `anthropic_claude_code` | the claude.ai subscription at USD 0 | the 5-hour session window, and the Fable weekly window |
| `openai_codex` | the ChatGPT subscription at USD 0 | the weekly window |

The captain gave the Gemini allocation on 2026-09-16:
"I will allocate $200 for benchmarking the gemini models".
The same order gave the other two bounds:
"claude session/weekly usage (dont worry about the weekly usage except for fable), codex weekly usage".

A subscription call costs USD 0.
Its meter is the token count and the list-price equivalent of those tokens.
The list price is information only.
The rates live in `config/benchmark-evaluation-list-prices-v1.json`, which no gate binds.

## Inputs

The guard reads five sources and writes to none of them.

1. The cost journal of the streaming evaluator, `cost-journal.jsonl` in the evaluator work directory.
   One row holds one evaluated question with its generation cost, its Gemini cost, its subscription tokens and its N0 to N5 outcomes per model.
2. The watch state of the evaluator, `watch-state.json` in the same directory.
   It says whether the evaluator still polls and which vendors it paused by itself.
3. The shared paid-call ledger.
   The guard sums the `benchmark_evaluation` phase from the ledger requests and splits it per model by the stage name `evaluation_answer:<model>`.
   A reservation and an ambiguous charge count toward the ceiling, so the guard counts them too.
4. The construction budget policy of chapter 3.
   The guard reads `dataset_construction_allocation_usd` and `accepted_question_target` from it.
5. The command `quota-axi --json --full`.
   The guard reads four windows from that report: `claude/five_hour`, `claude/seven_day`, `claude/model:fable` and `codex/weekly`.

## Outputs

The guard writes two files in its own directory:

- `guard-state.json`, schema `benchmark-guard-state-v1`.
  One complete reading: the per-model table, the per-vendor rollup, the budget, the extrapolation, the quota windows, every rule with its numbers, the paused models and the actions of this cycle.
  The corpus viewer reads this file.
- `guard-log.jsonl`, schema `benchmark-guard-log-v1`.
  An append-only log.
  Every cycle appends one `poll` row with the headline numbers.
  Every pause and every resume appends its own row with the numbers that triggered it.

The guard also appends one `working:` line to the task status file for each pause and each resume, so that the supervisor sees it.

## The extrapolation

The guard answers one question: what will the whole chapter 3 run cost at the present rate?

First it projects how many questions the run will still produce:

```
construction_remaining_usd   = dataset_construction_allocation_usd - construction_spent_usd
usd_per_accepted_item        = construction_spent_usd / accepted_question_count
further_accepted_items       = construction_remaining_usd / usd_per_accepted_item
expected_total_questions     = accepted_question_count + further_accepted_items
questions_still_expected     = expected_total_questions - questions_evaluated
```

The construction spend is the ledger total minus the evaluation-phase spend, as `AGENTS.md` states.
The construction policy also stops the run at `accepted_question_target`.
The smaller of the two bounds wins, and `binding_bound` names it.

Then it projects each meter forward at its own observed cost per question:

```
usd_per_question         = spend_so_far / questions_evaluated
extrapolated_total_usd   = spend_so_far + usd_per_question * questions_still_expected
```

The Gemini meter uses the whole `benchmark_evaluation` phase of the ledger, because the whole phase draws on the same USD 200 allocation.
A subscription meter has no USD.
It projects tokens and the list-price equivalent instead.
A subscription pause therefore never comes from an extrapolation.
It comes from a quota threshold, because `quota-axi` reports no per-caller attribution.

## The rules

A rule fires only on an urgent condition.
The captain asked for a pause to be rare:
"This should be rare and only happen if there are very urget issues with the benchmarking cost."

| Rule | Vendor | Fires when |
| --- | --- | --- |
| `gemini_extrapolated_over_budget` | `google_gemini` | the extrapolated Gemini total is more than USD 200 |
| `gemini_evaluation_ceiling_margin` | `google_gemini` | the `benchmark_evaluation` phase has less than USD 10 left under its ledger ceiling |
| `claude_session_window_floor` | `anthropic_claude_code` | the Claude 5-hour session window is below 15 percent remaining |
| `fable_weekly_window_floor` | `anthropic_claude_code` | the Fable weekly window is below 10 percent remaining, after the captain's own Fable pause has expired |
| `codex_weekly_window_floor` | `openai_codex` | the Codex weekly window is below 10 percent remaining |
| `codex_projected_exhaustion` | `openai_codex` | `quota-axi` projects the Codex weekly window exhausted before its reset, and the benchmark is the main consumer |

Three rules need a note.

The ceiling-margin rule is inert when the ceiling is smaller than twice the warning band, that is USD 20.
Under a small ceiling the band covers the whole budget and the rule would fire on the first call.
The evaluator's own ceiling precheck already pauses the Gemini vendor before a small ceiling is passed.
The field `rule_applies` records this.

The Fable rule waits for the captain's own pause to expire.
The captain wrote on 2026-09-16:
"pause the fable evaluation because I only have ~80% fable usage left today; I will run the fable benchmarking after the reset at 6:00pm today".
That pause carries `resume_at_utc` 2026-09-16T23:00:00Z.
The guard takes that time as the moment its Fable rule starts.

The Codex projection rule needs proof that the benchmark drives the burn.
`quota-axi` gives no per-caller attribution, and the Claude and Codex quotas are shared with every agent session on this machine.
The guard therefore proves the only thing it can prove: the evaluator still polls, and the benchmark booked more Codex calls than in the previous cycle.
When the evaluator is idle, the burn belongs to the other sessions and a pause would save nothing.

## Which model the guard pauses

A fired rule names the models of its vendor from the evaluation plan.
The guard pauses one model per fired rule per cycle: the model with the highest cost per question that nothing pauses yet.
The captain set that tie-break for Gemini and the guard applies it to every vendor.
A Gemini model ranks by real USD.
A subscription model ranks by the list-price equivalent of its tokens, which is the only per-model number that tracks how hard it leans on the shared quota.
A model with no measured cost sorts last, because a pause would save nothing that is proven.

If the condition still holds at the next cycle, the guard pauses the next model of that vendor.
When the condition clears, the guard removes every pause that it owns for that rule.

## The pause switch

The guard pauses through the evaluator's documented switch and never by stopping a process.
The switch is one JSON file with the schema `benchmark-evaluation-model-pause-v1`:

```json
{
  "schema": "benchmark-evaluation-model-pause-v1",
  "config_id": "arctic-abstention-model-pause-v1",
  "paused_models": {
    "claude-fable-5-1": {
      "reason": "the captain's daily Claude Fable quota is about 80 percent used",
      "paused_at_utc": "2026-09-16T10:20:00Z",
      "resume_at_utc": "2026-09-16T23:00:00Z"
    }
  }
}
```

A paused model keeps its trials pending.
They are not recorded, not counted as invalid, and they run after the pause is lifted.
An entry with no `resume_at_utc` holds until an operator or the guard removes it.
An entry whose `resume_at_utc` has passed no longer pauses.

The guard marks its own entries with `"owner": "benchmark-cost-guard"` and adds the rule and the numbers.
The guard removes only its own entries.
An entry that an operator or the captain wrote stays exactly as written.

The evaluator must read the same file.
Give the evaluator launcher the same path that the guard writes.

## Operate the guard

Run the guard as a systemd user unit, like the live publication snapshot service.
Operate it from a read-only runtime snapshot of a landed commit, not from a worktree.

```sh
systemd-run --user --unit=arctic-benchmark-guard-r1 \
  --working-directory=<runtime-snapshot> \
  <runtime-snapshot>/... nix develop -c env PYTHONPATH=<runtime-snapshot>/src \
  python -m arctic_qa.benchmark_guard \
    --journal-dir <evaluator work dir> \
    --guard-dir <guard dir> \
    --pause-file <the pause file the evaluator reads> \
    --shared-ledger-file <shared paid-call ledger> \
    --construction-policy-file <active chapter 3 budget policy> \
    --evaluation-policy-file <active benchmark evaluation policy> \
    --quota-binary /home/ben/.npm-global/bin/quota-axi \
    --status-file <task status file> \
    --interval-seconds 300
```

Watch it with `journalctl --user -u arctic-benchmark-guard-r1 -f`.
Stop it with `systemctl --user stop arctic-benchmark-guard-r1`.

Options:

- `--once` runs one cycle and exits. Use it for a test.
- `--gemini-budget-usd` changes the allocation. The default is 200.00.
- `--plan-file` reads the models of each vendor from `config/benchmark-evaluation-plan-high-v1.json`. Without it the guard uses the captain's eight models.
- `--quota-binary` gives the absolute path of `quota-axi`. The nix devshell has no npm global bin on its PATH, so a service must pass `/home/ben/.npm-global/bin/quota-axi`.
- `--recorded-quota-file` reads a saved `quota-axi --json --full` report instead of the live command. Use it for a test.
- `--evaluator-stale-seconds` sets how long the watch state can be old before the evaluator counts as idle. The default is 900.

## The website section

The corpus viewer shows the section "Live benchmarking" at `/`.
The route `/api/live-benchmark` returns the section payload.
Both rebuild from the files on each request and make no paid call.

Give the viewer two options:

- `--benchmark-journal-dir <evaluator work dir>` for the cost journal and the watch state.
- `--benchmark-guard-state-file <guard dir>/guard-state.json` for the budget, the extrapolation, the quota readings and the pause state.

The section shows:

- The per-model table: N1 to N5 counts, ACC, Precision_abs, Recall_abs, F1_abs, Abstention Rate, R-Acc, SSR, questions evaluated, invalid rate, and paused or active with the reason.
- The Gemini spend against the USD 200 allocation, with the cost per question and the extrapolated total.
- The per-question cost rows: generation USD, Gemini evaluation USD, subscription tokens, the list-price equivalent, the trials and the wall time.
- The quota readings of the four guarded windows.
- Every guard rule with its verdict.
- The watch state of the evaluator.

The metrics come from `abstention_score.metrics_from_counts`, the same function that the scorer uses, so the section and the paper tables cannot drift apart.
The section shows no metric for a model that has answered no scored trial yet.
A model that only the ledger knows, such as an earlier canary model, is marked "not in the current plan".

## Limits

- The guard has no attribution for the Claude and Codex quotas. It sees the whole account window, which every agent session on this machine shares. A pause helps only when the benchmark is the main consumer of that window.
- The guard reads the ledger without a write lock, under the shared lock of the ledger file. A number can be one cycle old.
- The guard pauses one model per fired rule per cycle. A vendor whose whole quota collapses needs as many cycles as it has models.
- A pause is inert until the evaluator reads the same pause file. Confirm the path of the evaluator launcher before you rely on the guard.
- When `quota-axi` cannot run, no quota rule fires. The guard records the error in `guard-state.json`, and the viewer shows it. A missing quota reading never causes a pause.
