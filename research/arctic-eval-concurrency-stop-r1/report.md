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

## The second defect: the exit code was read as the answer

The relaunch on `c1fe938` held the Gemini arm, and 27 seconds later the Claude arm went dark:

```json
{"at":"2026-09-17T17:10:47Z","event":"vendor_paused",
 "reason":"failed: the harness exited with 1:","vendor":"anthropic_claude_code"}
```

The same pause had closed the arm at 17:05:31 UTC on the previous snapshot.
Firstmate's first reading of it, and mine, was the stdin race in the receipt of 17:04:51 UTC.
The receipts say otherwise.

### What the 28 failed Claude receipts hold

| Shape | Receipts |
| --- | --- |
| The provider declined: exit 1, empty stderr, a whole result object with `stop_reason: "refusal"` | 8 |
| The binary was not there: `FileNotFoundError` | 14 |
| `SIGTERM` of a unit stop: exit 143 and -15 | 5 |
| The harness started and never received its prompt | 1 |

The refusal text names the cause:

```
API Error: Opus 5's safeguards flagged this message (https://www.anthropic.com/legal/aup).
... Details: `[bio]`
```

The broad safeguard reads the Arctic biology stimulus of one question.

### Why one refusal closed the arm

`SubscriptionEvaluationProvider.answer` read the exit code before it read the output, so a non-zero exit discarded the result the harness had already printed and recorded `the harness exited with 1`.
`abstention_plan.run_vendor` stops a vendor on any response that is not completed, and `abstention_watch.finish_item` pauses a vendor that stopped.
So the arm was off for the rest of the invocation, and every question of that invocation was left short of its Claude trials.

### What moves

The provider saw the request and answered it, so the refusal is a response of this trial.
It carries no letter, so it scores N0, it counts toward the 48 planned responses, and nothing asks it again.
It says nothing about the next question, because the safeguard read the text of this one.

`_parsed_answer` reads the printed output first, whatever the exit code.
A parser that finds the harness's own result record owns the outcome; the exit code becomes the error only when no such record is there.
`parse_claude_output` reads `stop_reason: "refusal"` as a completed call whose output is the refusal text; every other `is_error` result is still a failure.

The one stdin receipt is the opposite case and is a pre-provider refusal.
`HARNESS_PROMPT_FAILURES` names what it leaves in the transport's stderr, the trial raises `errors.HarnessUnavailableError`, which `run_vendor` already waits out and leaves pending, and `SubscriptionLedger.abandon` drops the row and frees the slot with no receipt.
That is the one way a row leaves that ledger: nothing was asked and nothing was charged, so there is no event to receipt.

### The pacing that was not needed

The order asked for the subscription wave to be paced so the harness never sees 24 concurrent spawns.
It never did.
`SubscriptionLedger.submit` waits for one of the policy's per-vendor slots before the harness is started, and that ledger is a file-locked directory shared by every question of the process, so the bound holds however many questions are in flight.

Measured over the 3,257 Claude receipts of `streaming-r11` that carry both a submitted and a completed time, the peak overlap is exactly 3, which is the policy limit.
Only the Gemini arm needed a gate, because its limit lives in the shared paid-call ledger and `effective_concurrency` read it per question.

## The third defect: a binary being replaced under a trial

The Claude arm of the unit on `f160074` scored for six minutes and paused
again at 17:32:03 UTC, with a reason nobody had seen before:

```json
{"event":"vendor_paused","reason":"failed: claude printed no result object",
 "vendor":"anthropic_claude_code"}
```

The receipt's `stderr_tail` names it: `OSError: [Errno 8] Exec format error`.
The binary was being rewritten at that moment; its mtime is 17:31 UTC and it answers `--version` normally now.
The child never ran, the trial was 0.075 seconds long, nothing was asked and nothing was charged.

Two faults meet there.
The probe before the row asks whether the path is executable, which a half-written file still is, so only the run-time result can see this.
And `_parsed_answer` read `int(returncode or 0)`, which made a child that reported no exit status at all look like a clean one and hid the transport's own error behind the parser's.

`is_harness_spawn_failure` already named a result with no exit status, and now names the exec-format error by text as well.
`answer` treats a child that never ran and a child that never received its prompt alike: `errors.HarnessUnavailableError`, waited out inside the trial and left pending, with `SubscriptionLedger.abandon` dropping the row and writing no receipt.
A None exit status is no longer coerced to a zero one.

## The three windows

Each window runs from its own relaunch, with 8 questions in flight.

| Snapshot | Window (UTC) | Gemini trials | Claude trials | `vendor_stopped_on_this_item` | Vendor pauses |
| --- | --- | --- | --- | --- | --- |
| `c1fe938` | 17:10:20 to 17:24:03 | 148 | 22 | 2, both the per-item repeat cap | 1, the provider refusal |
| `f160074` | 17:26:11 to 17:36:37 | 46 | 84 | none | 1, the binary being replaced |
| `45e1769` | 17:37:18 to 17:52:20 | 0, nothing owed | 151 | none | none |

No refusal of the paid-call concurrency limit was recorded after 17:04:32 UTC, which is on the snapshot before the first of these.
The two repeat-cap events are the evaluation policy's `maximum_calls_per_item_condition_model_arm`, which this task does not touch: that refusal is recorded as a response, so the trial leaves the owed set and the cap is self-limiting rather than a loop. Both questions it names now hold all 48 of their rows, 10 and 9 of them the cap.

The Gemini arm ran at 648 trials an hour in the first window, against the 343 an hour that `research/arctic-eval-reopen-r1/report.md` measured on the snapshot before it.

## What is left

`eval-finished.sh` at 17:53 UTC:

```
finished: 194 of 195 accepted questions hold all 48 responses; 1 cannot be
cleared by the reopen rule: aqa-7f09e4bdf6bac5c50d4c holds 40 of 48 and its
run directory belongs to run id abstention-stream-r10-aqa-7f09e4bdf6bac5c50d4c;
100 recorded responses hold no provider answer
```

No question owes a trial any arm can run.
The one shortfall is the question of another run id that `research/arctic-eval-reopen-r1/report.md` already named; finishing it needs a pass bound to the `streaming-r10` run id and work directory, which is its own reviewed authorization and the captain's to open.

Of the 100 recorded responses that hold no provider answer, 16 are the paid-call concurrency refusal of this defect and 7 more were recorded between 16:43 and 17:04 UTC before the fix landed. They are spent: the refusal is a recorded row, and the no-retry contract never asks a recorded trial again. Only the trials the refusal left undispatched could be, and were, re-opened.

## The re-opens

| Time (UTC) | Batch | Questions | Trials |
| --- | --- | --- | --- |
| 17:10:15 | what the concurrency refusal stranded | 12 | 60 |
| 17:26:06 | what the provider refusal stranded | 11 | 115 |
| 17:36:58 | what the replaced binary stranded | 12 | 151 |

Each one uses `reopen-stranded-questions.py` of `research/arctic-eval-reopen-r1/` unchanged, under its four bounds.

## Tests

- `tests/test_phase_scoped_slots.py`: the deferring caller hears the refusal, nothing is written, the row stays `counting`, and the next attempt of the same request key runs under the same authorization; both scheduling reasons defer and every other refusal is still recorded; the default broker is unchanged.
- `tests/test_abstention_subscription.py`: a provider refusal is an answer and never stops the arm; a harness that could not be started and one that never received its prompt record nothing and leave no ledger row or receipt; a harness that printed nothing is still a failure; the parser reads a refusal as a completed call and every other error as a failure.
- `tests/test_abstention_plan.py`: the full slots are a pre-provider refusal, the trial is left pending and the next pass runs exactly it; one gate holds four questions under a limit of three, and without the gate the wave reaches the broker whole.
- `tests/test_ledger_proof_cost.py`: the evaluator's own factory sets both flags.
- Also green: `tests/test_abstention_watch.py`, `tests/test_abstention_run.py`, `tests/test_abstention_broker.py`, `tests/test_abstention_subscription.py`, `tests/test_broker_provider.py`, `tests/test_broker_operation_lock_wait.py`.

## Artifacts

The reviewed successor authorization, its review record and the launcher are `streaming-eval-r11-{authorization,review,launcher}-c1fe938.*` under `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-authorization-r8/`.
The re-open receipt is `reopen-batch3-concurrency-stop.json` under `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-reopen-r1/`.
