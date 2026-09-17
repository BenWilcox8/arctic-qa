# The questions nobody asked again, and the harness upgrade that closed the rest

Task `arctic-eval-reopen-r1`, 2026-09-17.
Base `main` 1bf5221.
The code change is `89c1fa8`, which is the snapshot the live unit runs and the commit the successor authorization binds.
Live service: unit `arctic-abstention-stream-r3`, work directory `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r11`.

## The order

Captain, 2026-09-17 15:48 UTC: "Update the snapshot from 13:47 to instead be the final dataset once all the evaluations finish running. Also reopen all the open questions that were not evaluated on."
Captain, 16:00 UTC: "Start running the evaluations right now while the other worker waits so that it can get done quicker."

## What was stranded

`research/arctic-eval-busy-strand-r1/report.md` states the defect.
A question the evaluator closed below its 48 planned responses was closed for good.
`CostJournal.completed_item_ids` reads the last row of the question, and `evaluation.complete` of that row came from the paused models and the paused vendors alone.
A busy ledger lock, a missing harness binary, a vendor pause or a stop of the unit is none of those, so the row called the question complete while `recorded_trials` was far below `planned_trials`.

At 15:59 UTC, with the evaluator idle and every accepted question journalled:

| Measure | Value |
| --- | --- |
| Accepted questions of the campaign | 195 |
| Questions with a journal row | 195 |
| Questions below 48 responses | 92 |
| Trials with no response row | 1,303 |

## The re-open

`reopen-stranded-questions.py` is the operation.
It follows the four bounds that the strand report states.

**Scope.**
Only a trial with no recorded response row is re-opened.
A trial whose row records an answer, a refusal or a failure went out once and is never asked again: `abstention_plan.run_vendor` reads the recorded rows as its `done` set, so those trials stay done whatever the journal says.
The re-opened trials are exactly the ones the provider never saw.

**The record.**
One appended row per question, never an edit.
The row is the superseded row with its costs unchanged, so no total moves, plus a `reopen` block that names the captain order, the reason, the trials owed per model and the trial ids.
`evaluation.pending_deferred_trials` carries the count, which is the field `CostJournal.row_is_complete` already reads for a trial the provider never saw.

**The money.**
The Gemini half is 492 trials.
At this run's own measured rate, USD 0.015767 a trial, that is USD 7.76, against USD 29.01 spent of the USD 200 evaluation ceiling.
No transition is needed.
The 811 subscription trials cost USD 0 and spend quota.

**The rate.**
The batches reached the idle evaluator within a minute, and `abstention_watch.wave_order` cuts the backlog into waves and gives every arm a share of each one.

### The numbers

91 questions re-opened, 1,303 trials.

| Arm | Trials re-opened |
| --- | --- |
| anthropic_claude_code | 636 |
| google_gemini | 492 |
| openai_codex | 175 |

| Model | Trials re-opened |
| --- | --- |
| gemini-3.7-flash | 253 |
| gemini-3.8-flash | 239 |
| claude-sonnet-5 | 231 |
| claude-opus-5 | 210 |
| claude-fable-5-1 | 195 |
| gpt-5.6-terra | 63 |
| gpt-5.6-sol | 60 |
| gpt-6-astra | 52 |

The Fable arm is in the re-open, 195 trials.
The captain's cap stands: the guard pauses `claude-fable-5-1` for good when the Fable weekly usage reaches 80 percent, and a paused model's trials stay pending.

### The batches

| Time (UTC) | Batch | Questions | Trials |
| --- | --- | --- | --- |
| 16:01:44 | the questions the Codex arms owe | 17 | 440 |
| 16:01:49 | the next 21 of the backlog | 21 | 160 |
| 16:02:11 | the rest | 53 | 703 |

The evaluator took up the first wave of eight at 16:01:51 UTC and the first re-opened trial was recorded at 16:02:14 UTC.

No stop of the unit was needed for this.
The unit was idle with nothing in flight, and two rules keep a live append safe: a question the evaluator holds in flight is never re-opened, because the row that pass appends would supersede the re-opened one, and each row is written with one `os.write` to a file opened `O_APPEND`, which the kernel serializes against the evaluator's own append.

## What ended the unit at 16:04:18 UTC

The unit exited status 1 two minutes after it took up the first re-opened wave.
Two faults, both outside the money rules, and neither of them the journal rows.

### The harness version was part of the identity of the run directory

The run manifest carries `decoding.harness_version`, and `prepare_run` compared the whole manifest field by field.
The Claude Code binary on this machine went from 2.1.273 to 2.1.274, so every question whose Claude pass had run on the older binary was refused with "the run directory holds a different run manifest".
Eight questions of `streaming-r11` were in that state, and three of them were in the wave.

A question that is never revisited never compares its manifest again, which is why this defect waited for the re-open to show itself.
Before this snapshot an upgrade was final: a question with trials left could never be finished.

The harness version is a record of the pass, exactly as `code_commit` is, and every response row already carries the version that answered it.
`abstention_run.without_harness_version` takes it out of the comparison, and `pass_manifest_name` records the pass that ran on the new binary beside the first manifest.

### The read of the harness version was an error of the run

`binary_version` raised a `ValueError` when the binary could not be started.
That read runs while a question builds its run, before any trial reserves a row, so the trial's own bounded wait in `run_vendor` does not cover it.
The Claude Code binary was replaced again at 16:03 UTC, and one such error ended the whole watch.

It now asks `transport.probe` first and raises `errors.HarnessUnavailableError`, which `abstention_watch.run_item` contains against the one question: it emits `item_harness_unavailable`, writes no journal row, and takes the next question.
A binary that starts and refuses is still a `ValueError`.

### The proof

The relaunch is 16:15:35 UTC on snapshot `89c1fa8`.
Question `aqa-199da6fd46bc6e3d7cd2`, one of the eight the upgrade had closed, holds all 48 of its responses at 16:22 UTC, and `run-manifest-89c1fa8.json` beside its first manifest records the 2.1.274 binary.

## What cannot complete

### One question of another run id

`aqa-7f09e4bdf6bac5c50d4c` holds 40 of its 48 responses and owes 8 Gemini trials.
Its run directory is in `streaming-r10`, and its journal row names run id `abstention-stream-r10-aqa-7f09e4bdf6bac5c50d4c`.
This evaluator would build a new run directory under its own run id, find no recorded row and ask all 48 trials again, which is the retry the evaluation policy forbids.
So the re-open leaves it alone.
Finishing it needs a pass bound to the `streaming-r10` run id and work directory, which is its own reviewed authorization and the captain's to open.

### Six trials of two questions at the per-item repeat cap

The evaluation policy allows `maximum_calls_per_item_condition_model_arm`, which is 8 calls of one question, condition, model and arm.
The broker counts that across every streaming run of the item, not only this one.
Three questions reached it, and 6 re-opened Gemini trials of two of them sit on a capped combination:

| Question | Model | Condition | Trials owed |
| --- | --- | --- | --- |
| `aqa-52dee70307a8186522bf` | gemini-3.7-flash | gold_absent | 2 |
| `aqa-52dee70307a8186522bf` | gemini-3.8-flash | gold_absent | 2 |
| `aqa-9d5b8cc412f835cc5201` | gemini-3.8-flash | gold_absent | 2 |

Each attempt on such a trial writes a `not_submitted` receipt, which is a recorded response, so the trial is spent.
Raising the cap is a new evaluation policy file, a registered change set and a reviewed transition, which is the captain's to open.

### The responses that hold no provider answer

73 of the recorded responses across the whole work directory record a refusal or a failure instead of a letter.
They count toward the 48 and they are never asked again, because the no-retry contract is about a call the provider answered or refused.
The `not_submitted` half of them is the transient reservation refusal that the strand report names as the remaining strand path.

| State and reason | Rows |
| --- | --- |
| `not_submitted`, the evaluation repeat limit for this item is complete | 20 |
| `not_submitted`, the paid-call concurrency limit is complete | 16 |
| `failed`, the harness exited with None: FileNotFoundError | 14 |
| `failed`, codex turn did not complete: Exceeded skills context budget | 6 |
| `failed`, the harness exited with 1 | 6 |
| `failed`, the harness exited with 143 | 4 |
| `interrupted`, the harness process did not settle this request | 2 |
| `failed`, codex turn did not complete: no turn.completed event | 1 |
| `failed`, codex printed no JSON event | 1 |
| `failed`, the harness exited with -15 | 1 |
| `ambiguous_charge`, interrupted request has no durable provider response | 1 |
| `policy_stop`, duplicate request key | 1 |

## The expected time to finish

Measured over the first eight minutes after the relaunch, with all three arms running and eight questions in flight.

| Arm | Trials an hour | Trials owed | Hours |
| --- | --- | --- | --- |
| anthropic_claude_code | 560 | 546 | 1.0 |
| google_gemini | 343 | 427 | 1.2 |
| openai_codex | 321 | 115 | 0.4 |

The arms run together, so the Gemini arm is the bound: about 1.2 hours from 16:24 UTC.
The earlier hours of this run held the Gemini arm at 172 to 258 trials an hour, which would be 1.7 to 2.5 hours.

## The finished detector

`eval-finished.sh` prints one line when the evaluation has finished, and nothing at all while it still has work it can do.
It is read-only, it opens the state database read-only, and it takes about 1.2 seconds.

Finished means one of two things.

1. Every accepted question of the campaign holds all 48 of its planned responses.
2. What is left is a shortfall the re-open rule cannot clear, and the line names it.
   Two causes are known: a question whose run directory belongs to another run id, and a question whose every owed trial belongs to a model paused with no resume time, which is the captain's Fable cap.

The line always states how many of the recorded responses hold no provider answer.
The exit code is 0 when the detector could tell, with or without a line, and non-zero when it could not.
A non-zero exit is never "finished".

`test-eval-finished.sh` proves its four answers against fixtures: the question that is whole, the question that still owes an askable trial, the question the re-open rule cannot clear, and the accepted question the evaluator has never seen.

## Tests

- `tests/test_abstention_run.py`: an upgraded harness binary still finishes the question, the first manifest is untouched, the new pass has its own record, and every other field is still the identity of the run.
- `tests/test_abstention_subscription.py`: the version read of a binary that will not start is a wait, and a binary that starts and refuses is still an error.
- `tests/test_abstention_watch.py`: a vanished harness binary around the plan never ends the watch, and the message of the version read is read as a harness pause.
- Also green: `tests/test_abstention_plan.py`, `tests/test_abstention_broker.py`.

## Artifacts

| File | What it holds |
| --- | --- |
| `reopen-stranded-questions.py` | The re-open operation, read-only without `--apply`. |
| `eval-finished.sh` | The finished detector. |
| `test-eval-finished.sh`, `make-fixture-db.py` | The detector's fixture test. |
| `reopen-batch1-codex.json`, `reopen-batch1-rest.json`, `reopen-batch2.json` | The receipt of each batch: the question ids, the trials per model and the projected USD. |

The reviewed successor authorization, its review record and the launcher are `streaming-eval-r11-{authorization,review,launcher}-89c1fa8.*` under `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-authorization-r8/`.
The same operation scripts are in `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-reopen-r1/`.
