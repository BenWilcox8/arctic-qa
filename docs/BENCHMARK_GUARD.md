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

The guard writes three files in its own directory:

- `guard-state.json`, schema `benchmark-guard-state-v1`.
  One complete reading: the per-model table, the per-vendor rollup, the budget, the extrapolation, the quota windows, every rule with its numbers, the paused models and the actions of this cycle.
  The corpus viewer reads this file.
- `guard-log.jsonl`, schema `benchmark-guard-log-v1`.
  An append-only log.
  Every cycle appends one `poll` row with the headline numbers.
  Every pause and every resume appends its own row with the numbers that triggered it.
- `guard-memory.json`, schema `benchmark-guard-memory-v1`.
  What the guard must remember between cycles.
  It holds the Codex attribution samples, the count of clear cycles of each guard-owned pause, the moment the guard last resumed each model, and whether the guard has already reported that the evaluator stopped.
  None of this belongs in the pause file, because that file keeps the captain's own entries exactly as written.
  The guard rebuilds this file from an empty memory when it is absent or broken.
  A guard that starts with an empty memory measures again before it can pause on the Codex projection.

The guard also appends one `working:` line to the task status file for each pause and each resume, so that the supervisor sees it.

## The evaluator itself

An evaluator that stopped scores nothing, so the guard watches it as it watches the meters.
It reads `watch-state.json` of the journal directory, which the evaluator rewrites after every poll.
A watch state that is absent, or that has not moved for `--evaluator-stale-seconds` (900 by default), means the evaluator is not polling.

The guard then puts one line in `errors` of `guard-state.json` and appends one `blocked:` line to its status file.
It appends one `working:` line when the evaluator polls again.
It reports the change of state and not the state, so a long stop adds one line and not one line per cycle, and a guard that has reported nothing yet says nothing about an evaluator that runs.

This is the outside half of the same guard.
The evaluator's own `--status-file` reports a bound that ends it, and this reports a stop of any other kind: a crash, a halt of the shared ledger, or an operator who stopped the unit and forgot it.
The unit met its item bound at 2026-09-16T19:31:44Z, exited 0, and nothing said so until the next morning.

CAUTION: give the guard and the evaluator their new code together, or the evaluator first.
An evaluator older than 2026-09-17 writes `watch-state.json` only at the end of a whole poll cycle, which is about an hour at sixteen pending items.
A guard on the new code against such an evaluator reports a healthy evaluator as stopped, once an hour.

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
| `codex_projected_exhaustion` | `openai_codex` | `quota-axi` projects the Codex weekly window exhausted before its reset, and the measured attribution shows that the benchmark drives that burn |

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
The next section gives that measurement.

## The Codex attribution

`quota-axi` gives no per-caller attribution, and the Codex weekly window is shared with every agent session on this machine.
A rule that pauses a model on that window must therefore measure how much of the burn this benchmark causes.
The guard measures it over one trailing window of at least 30 minutes, from samples that it takes itself every cycle.

Every cycle appends one sample to `guard-memory.json`: the moment, the `percent_remaining` of the Codex weekly window, and the Codex list-price-equivalent USD that the cost journal records for the whole run.
From those samples the guard reads three quantities over the trailing window:

- `window_percent_burn`, what the whole account burned.
  It is the `percent_remaining` delta between the two ends of the window.
  The delta measures the window itself.
  The burn rate that `quota-axi` reports is an average over the whole elapsed weekly cycle, so it still carries the burn of sessions that ran hours before.
  The guard takes that rate only when the delta is not a burn, that is after a window reset inside the trailing period.
- `benchmark_usd_burn`, what this benchmark spent over the same period, in Codex list-price-equivalent USD.
- `other_percent_burn`, what the other sessions burned over the same period.
  The guard separates the two regimes it can see.
  In an interval between two samples, the benchmark either spent Codex USD or spent none.
  The intervals in which it spent none carry only the burn of the other sessions, so their percent points a second is the baseline rate of everything that is not this benchmark.

The rest of the burn belongs to the benchmark.
That gives the share, `benchmark_percent_burn / window_percent_burn`, and the window's percent-per-USD, `benchmark_percent_burn / benchmark_usd_burn`.
The benchmark drives the Codex window when one of two conditions is true:

- Its share is one half or more of the window's burn over that period.
- Its own extrapolated burn to the end of the run would exhaust the window before the reset by itself.
  That extrapolation is `questions_still_expected` multiplied by the benchmark's Codex cost per question, converted to percent points with the percent-per-USD of the same measurement.

The finding of `codex_projected_exhaustion` records the share and both burns, and `guard-state.json` holds the whole measurement under `codex_attribution`.

Three conditions stop the measurement, and the rule cannot fire while any of them holds:

- The trailing window is not full yet.
  A guard that started less than 30 minutes ago has nothing to compare against.
- The guard has not yet watched the window for 30 minutes with this benchmark idle.
  Without that baseline it cannot tell the other sessions' burn from its own.
- The whole account burned one percent point or less over the trailing window.
  `quota-axi` reports whole percent points, so a burn that small is not a measured burn and no share is read from it.

The guard measured this against the live run on 2026-09-16.
Over 2089 seconds the Codex weekly window stayed at 22 percent while the benchmark spent USD 0.49.
The share was 0.000 and the rule did not fire, which is correct: the projection to exhaustion came from other Codex sessions earlier that day.

## Hysteresis

A guard-owned pause holds until the condition is really gone.
Between 18:20 and 18:55 UTC on 2026-09-16 the guard wrote five pauses and four resumes of `gpt-5.6-sol` on alternate cycles.
Two bounds stop that flap, and both apply to every guard-owned pause, not only to the Codex rules:

- The guard removes a pause only after its rule has been clear for three consecutive cycles.
  `guard-state.json` shows the count under `hysteresis` while the pause waits.
- The guard does not pause a model again by the same rule for 30 minutes after it resumed that model.
  When a rule has no other candidate model, the whole rule waits out that hold.

Both counters live in `guard-memory.json`.
The pause file keeps only what the evaluator reads.

## Which model the guard pauses

A fired rule names the models of its vendor from the evaluation plan.
The guard pauses at most one model of each affected vendor per cycle: the model with the highest cost per question that nothing pauses yet.
Two rules of one vendor say the same thing, that the vendor is running out, so the first fired rule of that vendor owns the pause.
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
Give the evaluator launcher the same path that the guard writes, through its `--pause-file` option.
The evaluator re-reads that file before every item, so a pause takes effect without a restart.
Read the state back with the evaluator's own reader:

```bash
PYTHONPATH=src python -m arctic_qa --json abstention-eval --action pause-status \
  --pause-file <the pause file the evaluator reads>
```

## Operate the guard

Run the guard as a systemd user unit, like the live publication snapshot service.
Operate it from a read-only runtime snapshot of a landed commit, not from a worktree.

First build a read-only snapshot of the landed commit:

```sh
APP=<runtime root>/app-<short sha>-<task>
mkdir -p $APP && git archive <short sha> | tar -x -C $APP && chmod -R a-w $APP
```

Then start the unit:

```sh
systemd-run --user --unit=arctic-benchmark-guard-r1 --working-directory=$APP \
  /run/current-system/sw/bin/nix develop path:$APP -c env PYTHONPATH=$APP/src \
  python -m arctic_qa.benchmark_guard \
    --journal-dir <evaluator work dir> \
    --guard-dir <guard dir> \
    --pause-file <the pause file the evaluator reads> \
    --shared-ledger-file <shared paid-call ledger> \
    --construction-policy-file <active chapter 3 budget policy> \
    --evaluation-policy-file <active benchmark evaluation policy> \
    --quota-binary "$HOME/.npm-global/bin/quota-axi" \
    --status-file <task status file> \
    --interval-seconds 300
```

Watch it with `journalctl --user -u arctic-benchmark-guard-r1 -f`.
Stop it with `systemctl --user stop arctic-benchmark-guard-r1`.

Options:

- `--once` runs one cycle and exits. Use it for a test.
- `--gemini-budget-usd` changes the allocation. The default is 200.00.
- `--plan-file` reads the models of each vendor from `config/benchmark-evaluation-plan-high-v1.json`. Without it the guard uses the captain's eight models.
- `--quota-binary` gives the absolute path of `quota-axi`. The nix devshell has no npm global bin on its PATH, so a service must pass the absolute path, `~/.npm-global/bin/quota-axi` for an npm global prefix in the home directory.
- `--recorded-quota-file` reads a saved `quota-axi --json --full` report instead of the live command. Use it for a test.
- `--evaluator-stale-seconds` sets how long the watch state can be old before the evaluator counts as idle. The default is 900.
- `--codex-attribution-window-seconds` sets the trailing window of the Codex attribution. The default is 1800, which is also the smallest value the guard accepts.

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

- The guard has no per-caller attribution from `quota-axi`. It estimates the Codex share from its own samples, as "The Codex attribution" describes. It makes no such estimate for the Claude windows, so the Claude rules read the whole account window that every agent session on this machine shares.
- The Codex attribution needs a benchmark-idle baseline. An evaluator that never stops gives the guard no idle interval, and the Codex projection rule then stays inert. The Codex weekly floor rule still bounds that window.
- The Codex attribution is one measurement of a shared window, not a receipt. It assumes that the other sessions burn at a steady rate over the trailing window.
- The guard reads the ledger without a write lock, under the shared lock of the ledger file. A number can be one cycle old.
- The guard pauses one model of each affected vendor per cycle. A vendor whose whole quota collapses needs as many cycles as it has models, and the hysteresis holds each of those pauses for at least three clear cycles.
- A pause is inert until the evaluator reads the same pause file. Confirm the path of the evaluator launcher before you rely on the guard.
- When the shared ledger cannot be read, the Gemini readings are zero and no Gemini rule fires. The guard records the error, and the evaluator's own ceiling precheck still bounds the run.
- When `quota-axi` cannot run, no quota rule fires. The guard records the error in `guard-state.json`, and the viewer shows it. A missing quota reading never causes a pause.
