# Benchmark cost guard and live benchmarking section

Task `arctic-benchmark-guard-site-r1`, branch `fm/arctic-benchmark-guard-site-r1`, 2026-09-16.

The captain ordered two things on 2026-09-16:
keep the live benchmark evaluation inside his budget, and give him a page to watch it.
Both are live.

- The guard runs as the systemd user unit `arctic-benchmark-guard-r1`.
- The page is at **http://nixos.tail183fb5.ts.net:8787/**, section "Live benchmarking".

The reference document is `docs/BENCHMARK_GUARD.md`.
It holds the rules, the file formats and the operating instructions.
This report holds the decisions, the present readings and the open coordination item.

## 1. What the guard does

The guard polls five read-only sources every 5 minutes:

1. the cost journal of the streaming evaluator, `cost-journal.jsonl`;
2. the watch state of the evaluator, `watch-state.json`;
3. the `benchmark_evaluation` phase of the shared paid-call ledger;
4. the chapter 3 construction budget policy;
5. `quota-axi --json --full`.

It writes `guard-state.json` and appends to `guard-log.jsonl`.
It makes no paid model call.
It never stops a process.
When a bound becomes urgent it pauses one model through the evaluator's documented switch, the file with the schema `benchmark-evaluation-model-pause-v1`.
A paused model keeps its trials pending, so no other work is repeated.

## 2. The extrapolation

The guard first projects how many questions the chapter 3 run will still produce.

```
construction_remaining_usd = dataset_construction_allocation_usd - construction_spent_usd
usd_per_accepted_item      = construction_spent_usd / accepted_question_count
further_accepted_items     = construction_remaining_usd / usd_per_accepted_item
expected_total_questions   = accepted_question_count + further_accepted_items
questions_still_expected   = expected_total_questions - questions_evaluated
```

The construction spend is the ledger total minus the evaluation-phase spend.
The construction policy also stops the run at `accepted_question_target`, and the smaller of the two bounds wins.

Then each meter projects forward at its own observed cost per question.

```
usd_per_question       = spend_so_far / questions_evaluated
extrapolated_total_usd = spend_so_far + usd_per_question * questions_still_expected
```

Gemini uses the whole `benchmark_evaluation` phase, because the whole phase draws on the same USD 200 allocation.
A subscription vendor has no USD.
It projects tokens and the list-price equivalent, both of which are information only.
A subscription pause therefore never comes from an extrapolation.
It comes from a quota threshold, because `quota-axi` gives no per-caller attribution.

## 3. The rules and the thresholds

| Rule | Vendor | Threshold |
| --- | --- | --- |
| `gemini_extrapolated_over_budget` | Gemini | extrapolated total more than USD 200 |
| `gemini_evaluation_ceiling_margin` | Gemini | less than USD 10 left under the ledger evaluation ceiling |
| `claude_session_window_floor` | Claude Code | 5-hour session window below 15 percent remaining |
| `fable_weekly_window_floor` | Claude Code | Fable weekly window below 10 percent, after the captain's own pause expires |
| `codex_weekly_window_floor` | Codex | weekly window below 10 percent remaining |
| `codex_projected_exhaustion` | Codex | exhaustion projected before the weekly reset, and the benchmark drives the burn |

A fired rule pauses at most one model of its vendor per cycle: the model with the highest cost per question that nothing pauses yet.
Two rules of one vendor say the same thing, so only the first fired rule of that vendor acts.
The captain set that tie-break for Gemini and the guard applies it to all three vendors.
A Gemini model ranks by real USD.
A subscription model ranks by the list-price equivalent of its tokens.
When the condition clears, the guard removes the pauses it owns.
It never touches an entry that an operator or the captain wrote.

Three decisions needed judgment.

**The ceiling-margin rule is inert under a small ceiling.**
The rule assumes a ceiling that is large against its USD 10 band.
The active evaluation policy still carries the USD 5.00 ceiling of the canary.
Under that ceiling the band covers the whole budget, and the rule would fire on the first call, which is not urgent.
The guard therefore applies the rule only when the ceiling is USD 20 or more, and records `rule_applies: false` otherwise.
The evaluator's own ceiling precheck already pauses the Gemini vendor before a small ceiling is passed.

**"The benchmark is the main consumer" needed a measurable definition.**
`quota-axi` reports account windows, not callers, and every agent session on this machine shares them.
The guard proves the only thing it can prove: the evaluator still polls, and the benchmark booked more Codex calls than in the previous cycle.
When the evaluator is idle, the burn belongs to the other sessions and a pause would save nothing.
This keeps the Codex projection rule rare, as the captain asked.

**The Fable rule waits for the captain's own pause.**
The captain paused Fable until 23:00 UTC on 2026-09-16.
The guard reads that entry's `resume_at_utc` and starts its own Fable rule at that moment.
Until then the captain's pause governs, and the guard does not touch it.

## 4. The present readings

From `guard-state.json` at 2026-09-16T11:01:50Z.

Evaluation and budget:

| Reading | Value |
| --- | --- |
| Questions evaluated | 5 |
| Trials recorded | 166 |
| Gemini spend so far | USD 0.153364 |
| Gemini cost per question | USD 0.030673 |
| Questions still expected | 272 |
| **Extrapolated Gemini total** | **USD 8.496366** against the USD 200 allocation |
| Evaluation ledger ceiling | USD 5.00, USD 4.846636 left, policy `arctic-abstention-evaluation-policy-v1-canary` |

Expectation of the chapter 3 run:

| Reading | Value |
| --- | --- |
| Accepted questions now | 32 |
| Construction spend | USD 57.690742 of the USD 500.00 allocation |
| Cost per accepted item | USD 1.802836 |
| Further accepted items projected | 245 |
| Expected total questions | 277 (the allocation binds, not the target of 500) |

Subscription vendors, at the present rate over the 272 questions still expected:

| Vendor | Calls | Tokens (in / out / thinking) | List price so far | Extrapolated list price |
| --- | --- | --- | --- | --- |
| Claude Code | 75 | 61,437 / 225 / 23,768 | USD 1.430523 | USD 79.250974 |
| Codex | 90 | 221,034 / 1,724 / 43,130 | USD 1.935837 | USD 107.245370 |

Both bill USD 0.
The list-price equivalent shows what the same calls would cost on the API.

Quota windows:

| Window | Remaining | Resets | Projected exhausted |
| --- | --- | --- | --- |
| Claude 5-hour session | 81 percent | 2026-09-16T14:10Z | 2026-09-16T18:58Z |
| Claude 7-day | 34 percent | 2026-09-16T23:00Z | 2026-09-19T19:24Z |
| Claude Fable week | 14 percent | 2026-09-16T23:00Z | 2026-09-17T12:25Z |
| Codex week | 22 percent | 2026-09-20T10:08Z | 2026-09-17T07:35Z |

No rule fires.
The extrapolated Gemini total of USD 8.50 is far below USD 200.
The Codex projection is before the weekly reset, but the streaming evaluator is idle, so the benchmark is not the main consumer and the rule stays clear.
One model is paused: `claude-fable-5-1`, by the captain's own order, until 23:00 UTC.

Two readings deserve a note.
The Fable weekly window is at 14 percent and its reset is the same 23:00 UTC moment the captain named, so the captain's plan and the meter agree.
The evaluation phase of the ledger also holds the canary spend on `gemini-3.1-pro-preview`.
That model is in the table, marked "not in the current plan", because its spend still draws on the same USD 200 allocation.

## 5. The website section

The section "Live benchmarking" is on the corpus viewer page and uses the route `/api/live-benchmark`.
Both rebuild the whole payload from the files on each request.
Neither makes a paid call, and neither writes.

The section shows:

- the per-model table: N1 to N5 counts, ACC, Precision_abs, Recall_abs, F1_abs, Abstention Rate, R-Acc, SSR, questions evaluated, invalid rate, and paused or active with the reason;
- the Gemini spend against the USD 200 allocation with the cost per question and the extrapolated total;
- the per-question cost rows: generation USD, Gemini evaluation USD, subscription tokens, list-price equivalent, trials and wall time;
- the four guarded quota windows with their resets and projections;
- every guard rule with its verdict;
- the evaluator watch state.

The metrics come from `abstention_score.metrics_from_counts`, the same function the scorer uses, so the page and the paper tables cannot drift apart.

## 6. Deployment

The branch is rebased onto `main` at `183779b`, so the merge stays a fast-forward.
The viewer service and the guard service both run a read-only runtime snapshot of commit `2942738`.
That is the last commit of the branch that changes code.
The commits after it change only this report and `docs/BENCHMARK_GUARD.md`.
The snapshot is `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-benchmark-guard-site-r1/runtime/app-2942738-arctic-benchmark-guard-site-r1`.
The exact unit command is below.
It is the previous command of `arctic-corpus-stage-r1-formatting.service` with two changes: the new snapshot path, and the two new benchmark options.

```sh
APP=/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-benchmark-guard-site-r1/runtime/app-2942738-arctic-benchmark-guard-site-r1
G=/mnt/crdata/research-abstention/arctic-qa/abstention-eval/guard-r1
systemctl --user stop arctic-corpus-stage-r1-formatting.service
systemd-run --user --unit=arctic-corpus-stage-r1-formatting \
  /run/current-system/sw/bin/nix develop path:$APP -c env PYTHONPATH=$APP/src \
  python -m arctic_qa corpus-view \
  --corpus-root /mnt/crdata/research-abstention/arctic-qa/corpus-search-r1 \
  --run-id 20260911T232247Z \
  --runtime-dir /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-corpus-stage-r1/runtime \
  --progress-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-corpus-stage-r1/runtime/corpus-progress-v1.json \
  --zotero-receipts-dir /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/research-workbench/zotero/receipts \
  --metadata-run-dir /mnt/crdata/research-abstention/arctic-qa/metadata-prefilter-r1/run-20260912T024210Z \
  --source-run-dir /mnt/crdata/research-abstention/arctic-qa/source-screening-r1/run-20260912T033630Z \
  --access-run-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/production-campaign-r1/live-jsonl-rerun-r2/materialized-rerun-first-800 \
  --gemini-run-dir /mnt/crdata/research-abstention/arctic-qa/gemini-eligibility-r1/first-production-73e92b5-live-rerun-r13 \
  --gemini-connection-file "/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-corpus-stage-r1/gemini-readiness-r1/Read-only Gemini connection check.json" \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --streaming-budget-policy-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-qa-build-r1/proposed-streaming-dataset-budget-policy-v7.json \
  --streaming-progress-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/progress.json \
  --publication-package-dir /mnt/crdata/research-abstention/arctic-qa/publication-packages/trial-r1/v7 \
  --live-dataset-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/live-publication \
  --project-overview-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/streaming-dataset-r1/project-progress-overview-v1.json \
  --research-timeline-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-corpus-stage-r1/runtime/research-fleet-timeline-v1.json \
  --benchmark-journal-dir /mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r2 \
  --benchmark-guard-state-file $G/guard-state.json \
  --pipeline-namespace /mnt/crdata/research-abstention/arctic-qa \
  --pipeline-db-file /mnt/crdata/research-abstention/arctic-qa/state.sqlite3 \
  --pipeline-receipts-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts \
  --pipeline-eligibility-root /mnt/crdata/research-abstention/arctic-qa/gemini-eligibility-r1/first-production-73e92b5-live-rerun-r13 \
  --host 100.66.176.85 --port 8787 --stale-after-seconds 86400 --process-stale-after-seconds 300
```

The guard unit:

```sh
systemd-run --user --unit=arctic-benchmark-guard-r1 --working-directory=$APP \
  /run/current-system/sw/bin/nix develop path:$APP -c env PYTHONPATH=$APP/src \
  python -m arctic_qa.benchmark_guard \
    --journal-dir /mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r2 \
    --guard-dir $G \
    --pause-file $G/model-pause.json \
    --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
    --construction-policy-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-production-run-r1/streaming-dataset-budget-policy-v9-chapter3.json \
    --evaluation-policy-file $APP/config/benchmark-evaluation-policy-v1.json \
    --quota-binary /home/ben/.npm-global/bin/quota-axi \
    --status-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/state/arctic-benchmark-guard-site-r1.status \
    --interval-seconds 300
```

Watch it with `journalctl --user -u arctic-benchmark-guard-r1 -f`.
Stop it with `systemctl --user stop arctic-benchmark-guard-r1`.

Verification of the deployment:

- `curl http://nixos.tail183fb5.ts.net:8787/healthz` returns `{"status":"available","run_id":"20260911T232247Z"}`.
- The served page holds `id="live-benchmark"` and the heading "Live benchmarking".
- A comparison of the element ids of the page before and after the restart shows no id lost and only the 15 new benchmark ids gained.
- `/api/state`, `/api/candidates`, `/api/live-dataset` and `/api/pipeline-trace` all answer 200. `/downloads/dataset-metadata.json` answers 503 as before, because the unit selects no dataset metadata file.
- `/api/live-benchmark` returns the 9 model rows, the 5 question rows, the budget block, the four quota windows and the six rules, with no error.

## 7. The pause interface, proved end to end

The guard writes `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/guard-r1/model-pause.json`.
That file already carries the captain's Fable pause, copied from the entry that task `arctic-abstention-streaming-eval-r1` wrote.
The guard left that entry untouched through every cycle so far.

Task `arctic-abstention-streaming-eval-r1` has now landed the whole pause interface on its branch, at `8863129`.
The evaluator takes `--pause-file`, re-reads that file before every item, and reports the state with `--action pause-status`.
Its expiry rule is the same as the guard's: an entry with no `resume_at_utc` holds, and an entry whose `resume_at_utc` has passed does not.

The interface was proved with the evaluator's own reader, not with a copy of it.

```
$ PYTHONPATH=src python -m arctic_qa --json abstention-eval --action pause-status \
    --pause-file .../guard-r1/model-pause.json
{"paused_now":["claude-fable-5-1"], ...}
```

A guard-written entry reads back the same way.
A forced `claude_session_window_floor` pause, run into a scratch directory against the recorded low-session report, produced an entry with `owner`, `rule`, `vendor`, `numbers` and `cost_per_question_usd`.
The evaluator's `pause-status` accepted it and listed the model in `paused_now`.
The extra fields are additional keys of the entry, which the evaluator ignores.

One step remains, and it belongs to the evaluator's launcher, not to this task:

- the streaming launcher must pass `--pause-file /mnt/crdata/research-abstention/arctic-qa/abstention-eval/guard-r1/model-pause.json`.

Without that option the evaluator reads its own default file and a guard pause is inert.
The guard still measures, still extrapolates, still logs and still shows everything on the page.
This is a coordination item for firstmate. This task does not change the evaluator.

A second item is smaller.
The guard reads `config/benchmark-evaluation-policy-v1.json`, which carries the USD 5.00 canary ceiling.
The evaluation crew has a reviewed ceiling transition on its branch.
When the raised USD 200 ceiling lands, repoint `--evaluation-policy-file` at the new policy file and restart the guard unit.
The ceiling-margin rule becomes active at that moment, because the new ceiling is more than USD 20.

A third item is a cleanup.
The guard carries its own reader of the pause file, because the evaluator's reader is not on `main` yet.
After both branches merge, `benchmark_guard.read_pause_file` and `benchmark_guard.active_pauses` can call `abstention_plan.load_pause` and `abstention_plan.paused_models` instead.
The two readers apply the same rule today, and the interoperation test above proves it.

## 8. Tests

`tests/test_benchmark_guard.py`, 34 tests:

- the extrapolation arithmetic, with the worked numbers in the comments;
- the two bounds of the expected question count, the allocation and the accepted question target;
- the evaluation-phase sum, including reservations and ambiguous charges;
- each of the six rules against recorded `quota-axi` output;
- the tie-break that picks the costliest model per question;
- one whole guard cycle: a quiet cycle, a pause, a second pause while the condition holds, a resume when it clears, and an operator entry that the guard must not remove;
- one pause only, when two rules of one vendor fire together;
- the Codex projection rule measuring on the first cycle before it can act;
- the command line against a recorded report.

`tests/test_live_benchmark_viewer.py`, 9 tests:

- the section payload built from fixture journal and guard-state files;
- the metrics of one model checked against hand arithmetic;
- the paused model, its reason and the expiry of its resume time;
- every per-question cost column;
- the evaluator watch state;
- the payload with no guard state and with no journal;
- the HTTP route through a live `CorpusServer`;
- the page: every existing section id is still present, and the new section is bound to its loader.

Fixtures:

- `fixtures/quota-axi-2026-09-16T10-41Z.json` is the recorded live report.
- Four derived files lower one window each, so that one rule fires. Each names its derivation in the `_derivation` field.

The whole suite passes on the branch tip: `PYTHONPATH=src pytest tests/` reports `1305 passed in 1023.72s`, with the branch rebased onto `main` at `183779b`.

`ruff check .` and `ruff format --check .` are clean.
`data/` now leaves the linter through `extend-exclude` in `pyproject.toml`, because it holds the recorded evidence scripts of finished tasks, which must stay exactly as they ran.
Those 16 pre-existing lint errors were not caused by this task.
