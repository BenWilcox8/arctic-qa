# The full slot that closed a question, and the wave that filled it

Task `arctic-eval-concurrency-stop-r1`, 2026-09-17.
Base `main` cdb302e.
The code change is `c1fe938`, which is the snapshot the live unit runs and the commit the successor authorization binds.
Live service: unit `arctic-abstention-stream-r3`, work directory `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r11`.

## The order

Captain, 2026-09-17 15:48 and 16:00 UTC: every question short of 48 responses is reopened and the evaluations run to completion as fast as possible; the final dataset snapshot is taken when all the evaluations finish.

## What was stranded

From 16:30 UTC the unit journal held one event over and over:

```json
{"event":"vendor_stopped_on_this_item",
 "reason":"not_submitted: the paid-call concurrency limit is complete",
 "vendor":"google_gemini"}
```

Ten questions met it between 16:30:12 and 17:04:32 UTC.

| Time (UTC) | Question |
| --- | --- |
| 16:30:12 | `aqa-edd8ca8e4f36f3b21842` |
| 16:41:13 | `aqa-b090be4d6c22d807cc4c` |
| 16:41:17 | `aqa-27d0a5c52655da6b2283` |
| 16:46:34 | `aqa-26ee2a318740fde88911` |
| 16:46:36 | `aqa-f3e9cd93d6cdcdd457f4` |
| 16:57:31 | `aqa-4251fb46dcaa2ded907f` |
| 16:59:14 | `aqa-ed9e0dbcb70898d6a315` |
| 17:00:28 | `aqa-738949c5c347a199267f` |
| 17:04:03 | `aqa-5903b596b017f0d4310e` |
| 17:04:32 | `aqa-2cbbea930ac101b2586d` |

Two more events of the same shape in that window carry the per-item repeat limit instead, which is a real cap and not this defect.

## Why the limit filled

The concurrency limit of the evaluation phase belongs to the shared paid-call ledger, so it counts the calls of the whole process.
`config/benchmark-evaluation-policy-v3.json` sets it to 4.

`abstention_plan.effective_concurrency` reads the same limit per question, which is right for one question alone.
A wave multiplies it.
The unit runs `--item-workers 8`, and `config/benchmark-evaluation-plan-high-v1.json` gives the Gemini vendor 4 calls in flight, so eight questions sent up to 32 calls at a limit of 4.

With the producer stopped, nothing else held an evaluation slot.
The wave filled the limit against itself.

## Why a refusal was fatal

`execute` waits `TRANSIENT_RESERVATION_RETRY_SECONDS`, 90 seconds, for room.
Past that it wrote an immutable `not_submitted` receipt and marked the ledger row.
That refusal is resumable only under a later reviewed transition: `_resume_not_submitted` requires the active transition to name the refused request's transition as its predecessor.

For the producer that is right, and unchanged: it records the family, skips the paper, and the next reviewed transition brings the request back.

For the streaming evaluator it is fatal.
The evaluation policy forbids a re-ask of a recorded trial, and `run_vendor` reads the recorded rows as its `done` set, so the refused trial is spent.
The refusal also stops the vendor inside the question, so every later Gemini trial of that question is left undispatched.
The question can never reach its 48 planned responses.

This is the "remaining strand path" that `research/arctic-eval-busy-strand-r1/report.md` named and `research/arctic-eval-reopen-r1/report.md` counted: 16 recorded responses of this work directory held that refusal instead of a letter at 16:00 UTC, and 90 held some non-answer at 17:09 UTC.

## What moves

**The refusal is told, not recorded.**
`SharedGeminiBroker(defer_transient_reservations=True)` raises `errors.TransientReservationError` at the bound and writes nothing at all: no receipt, no ledger mutation.
The ledger row stays `counting`, which is a free token count that never reached a reservation, and `_reusable_counting_row` already reopens such a row for the next attempt of the same request key.
`abstention_cli._broker` sets the flag for the streaming evaluator and the concurrent plan, and for no other caller.

**The trial waits it out, then is owed.**
`abstention_plan.PRE_PROVIDER_REFUSALS` gains the new error, so `run_vendor` treats it exactly as it treats the busy exclusive lock and the missing harness binary: `PRE_PROVIDER_RETRY_ROUNDS` attempts with a doubling backoff, then the trial is left pending.
Nothing is recorded, the arm keeps running, and the question is owed the trial through `evaluation.pending_deferred_trials`.

**The wave is paced under the limit.**
`abstention_plan.evaluation_admission` is one gate per invocation, sized at the evaluation policy's `maximum_concurrent_requests`.
`watch` builds it once and hands it to every question; `GeminiBrokerEvaluationProvider` takes it around the paid call alone and never around a replayed receipt.
A call past the limit now waits in this process, where waiting is free and nothing is recorded, instead of waiting out the broker's bound and being refused.
The per-question slots of the subscription arms are unchanged, because a slot there belongs to the vendor and nothing outside this process enforces it.

The limit itself does not move.
The gate is set from the policy and never above it.
No money rule, gate, receipt, price config or ceiling changes.

## The cut-over

| Step | Time (UTC) |
| --- | --- |
| Snapshot `c1fe938` and successor authorization written | 17:05 |
| `systemctl --user stop arctic-abstention-stream-r3` | 17:05:46 |
| Unit inactive | 17:08:34 |
| Shared ledger settled: `inflight` 0, no `submitted` evaluation row, no halt | 17:09 |
| Re-open applied: 12 questions, 60 trials | 17:10:15 |
| Relaunch on `c1fe938` with 8 questions in flight | 17:10:20 |

The re-open follows the four bounds of `research/arctic-eval-reopen-r1/report.md` and uses its script unchanged.
Only a trial with no recorded response row is re-opened, so the trials the refusal already recorded are not re-asked.
32 further questions already carried a re-open row from the 16:01 UTC batches and were left alone, exactly as the script's idempotence requires.

| Measure at 17:09 UTC | Value |
| --- | --- |
| Accepted questions with a journal row | 195 |
| Questions owing a trial the provider never saw | 44 |
| Trials owed | 466 |

| Model | Trials owed |
| --- | --- |
| gemini-3.7-flash | 111 |
| gemini-3.8-flash | 98 |
| claude-sonnet-5 | 91 |
| claude-opus-5 | 86 |
| claude-fable-5-1 | 80 |

The projected Gemini cost of the 12 re-opened questions is USD 0.68 at this run's own measured rate of USD 0.015904 a trial.

## Tests

- `tests/test_phase_scoped_slots.py`: the deferring caller hears the refusal, nothing is written, the row stays `counting`, and the next attempt of the same request key runs under the same authorization; both scheduling reasons defer and every other refusal is still recorded; the default broker is unchanged.
- `tests/test_abstention_plan.py`: the full slots are a pre-provider refusal, the trial is left pending and the next pass runs exactly it; one gate holds four questions under a limit of three, and without the gate the wave reaches the broker whole.
- `tests/test_ledger_proof_cost.py`: the evaluator's own factory sets both flags.
- Also green: `tests/test_abstention_watch.py`, `tests/test_abstention_run.py`, `tests/test_abstention_broker.py`, `tests/test_abstention_subscription.py`, `tests/test_broker_provider.py`, `tests/test_broker_operation_lock_wait.py`.

## Artifacts

The reviewed successor authorization, its review record and the launcher are `streaming-eval-r11-{authorization,review,launcher}-c1fe938.*` under `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-authorization-r8/`.
The re-open receipt is `reopen-batch3-concurrency-stop.json` under `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-reopen-r1/`.
