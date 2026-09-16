# Broker operation-lock wait report (arctic-broker-operation-lock-wait-r1)

Date: 2026-09-16.
Branch: `fm/arctic-broker-operation-lock-wait-r1`, from local `main` at `cac4949`.
Captain standing order (2026-09-16): the chapter 3 production run and the live benchmark evaluator must run unattended with constant supervision.
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-broker-operation-lock-wait-r1/`.
Predecessor activation: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-candidate-fault-containment-r1/` (report `data/arctic-ch3-candidate-fault-containment-r1/report.md`).

## 1. Result

TBD

## 2. The fault

At 18:26 UTC on 2026-09-16 the producer of run `chapter3-7dc6485-r3` stopped with this line:

```json
{"code":"VALUEERROR","message":"another paid broker operation is active","status":"error"}
```

The launcher log is `launcher-fdf5ea8-ch3fault.log` in the predecessor activation directory.
The deployed snapshot was `app-fdf5ea8-arctic-ch3-candidate-fault-containment-r1`, commit `fdf5ea8`.

The shared ledger has one exclusive operation lock, the file `.<ledger>.operation.lock`.
Before this commit, eight code paths took that lock, and every one of them took it with `LOCK_NB`.
Seven are reviewed operations that an operator starts.
The eighth is `execute`, the broker's one ordinary request path, which the streaming producer and the streaming evaluator reach through `broker_provider.BrokerProvider`.

A concurrent task, `arctic-eval-503-release-r1`, was releasing an ambiguous evaluation charge on the same ledger.
Its status line of 18:27 UTC names the cause exactly: "my earlier retry probing 18:22-18:26Z is what starved the producer at 18:26Z".
Each of those probes held the operation lock for a moment.
One of them held it while the producer asked for its next construction request, the producer's `LOCK_NB` failed, and the broker raised `ValueError("another paid broker operation is active")`.

That refusal then crossed `broker_provider.broker_boundary`.
The seam marks every broker refusal a whole-run stop, on purpose: the execution gate, a ceiling and a halt describe the run, not one candidate, and the candidate-fault containment of `fdf5ea8` must never swallow them.
So the containment could not hold this one, and the producer exited.
It was the fourth chapter 3 run that day that one recoverable event ended.

## 3. The fix

The refusal was correct about the lock and wrong about who has to care.
A reviewed operation is short and exclusive.
Two of them must never overlap, and the loser has an operator to tell, so an immediate refusal is right for them.
An ordinary request is neither: it describes no fault of its own, it has no operator, and the only thing between it and its work is a lock that is about to be free.

`model_broker.hold_operation_lock` now owns that lock and gives the two classes different policies.

```python
def hold_operation_lock(path: Path, *, wait_seconds: float = 0.0) -> Any:
```

A reviewed operation passes no `wait_seconds` and keeps the behaviour it had: one `LOCK_NB`, then `ValueError("another paid broker operation is active")`.
The seven reviewed operations are the ambiguous-continuation authorization, the orphaned-request continuation, the HTTP-rejection settlement, the pretransport settlement, the count-error continuation, the usage reconciliation and the exclusive batch activation.

`execute` passes `OPERATION_LOCK_WAIT_SECONDS`, which is 120 seconds, and polls each `OPERATION_LOCK_WAIT_INTERVAL_SECONDS`, which is one second.
The last sleep of the wait ends at the bound and never past it.
At the bound the request raises `errors.BrokerOperationBusyError`.

`BrokerOperationBusyError` subclasses `ArcticQAError` and `ValueError` and carries the same message, so every caller that matches on the class or on the text is unaffected.
It is registered in `broker_provider._PAPER_LEVEL_BROKER_ERRORS`, so the seam lets it through unmarked, and in `streaming._ends_the_run`, so the producer contains it.
The bound-exceeded request reserved nothing and submitted nothing, so the containment has no money to settle: the family's call records are settled `incomplete_infra`, a `generation_routing` row records `candidate_processing_fault`, and the run continues with the next paper.

Every other broker refusal still ends the run.
The immediate refusal of a reviewed operation still ends the run, because it is still a plain `ValueError` that the seam marks.

## 4. Tests

`tests/test_broker_operation_lock_wait.py` is the slice, 13 tests.
A `Holder` thread takes the real `flock` on a real lock file, because `flock` is held by an open file description and not by a thread, so a second handle of the test process conflicts exactly as another worker's handle does.
No test mocks the lock.

| Test | What it holds |
|---|---|
| `a_lock_released_inside_the_bound_lets_the_request_proceed` | The lock is held and released inside the bound; the caller waits and takes it. |
| `a_lock_held_past_the_bound_raises_the_busy_error` | The bound raises `BrokerOperationBusyError`, with the same message, and it is a `ValueError`. |
| `the_wait_sleeps_to_its_bound_and_no_further` | On a fake clock the sleeps are `[1.0, 1.0, 0.5]` for a bound of 2.5 seconds. |
| `an_exclusive_operation_refuses_a_held_lock_at_once` | The default refuses in under a second and is not the busy error. |
| `a_free_lock_is_taken_and_released` | A free lock is taken by either policy and released on close. |
| `a_wait_the_caller_does_not_ask_for_keeps_the_plain_refusal` | The wait and its error travel together: a tuned bound cannot change the meaning of a refusal. |
| `a_request_waits_for_a_reviewed_operation_and_then_runs` | `execute` waits for a held lock and completes the request; the transport sees `countTokens` then `generateContent`. |
| `a_request_past_the_bound_reserves_nothing_and_submits_nothing` | At the bound: no transport call, `halted` false, `inflight` 0, no request row; the next request of the run completes. |
| `a_reviewed_operation_of_the_broker_keeps_its_immediate_refusal` | `reconcile_omitted_thought_usage` refuses a held lock in under a second. |
| `the_broker_seam_leaves_the_busy_error_alone` | `broker_boundary` does not mark it, and `_ends_the_run` is false. |
| `the_immediate_refusal_of_a_reviewed_operation_still_ends_the_run` | The plain `ValueError` of the same message is still marked and still ends the run. |
| `the_bound_exceeded_case_is_contained_and_the_run_continues` | The producer over two papers: the first family is `candidate_processing_fault`, the second is built, `processed` is 2. |
| `a_contained_bound_exceeded_case_leaves_a_routing_row` | One `generation_routing` row with the error class and the message. |

The first three tests fail on the unfixed code, because `hold_operation_lock` does not exist there and `execute` refuses a held lock at once.

## 5. The release

TBD

## 6. The relaunch and the health observation

TBD

## 7. Open points

TBD
