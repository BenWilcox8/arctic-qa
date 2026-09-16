# Broker operation-lock wait report (arctic-broker-operation-lock-wait-r1)

Date: 2026-09-16.
Branch: `fm/arctic-broker-operation-lock-wait-r1`, from local `main` at `cac4949`.
Captain standing order (2026-09-16): the chapter 3 production run and the live benchmark evaluator must run unattended with constant supervision.
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-broker-operation-lock-wait-r1/`.
Predecessor activation: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-candidate-fault-containment-r1/` (report `data/arctic-ch3-candidate-fault-containment-r1/report.md`).

## 1. Result in nine lines

1. The fault is repaired. Commit `f53e3e2`, and `ea00336` after local `main` is merged in. Section 3.
2. The 18:26 UTC exit was one lock with one policy for two kinds of caller. A reviewed operation must refuse a held lock at once; an ordinary request has no operator to tell and should wait. `hold_operation_lock` gives each what it needs. Section 3.
3. The bound raises `BrokerOperationBusyError`, which reserves nothing and submits nothing, so the producer records it against one family and continues. Every other broker refusal still ends the run. Section 3.
4. `tests/test_broker_operation_lock_wait.py` holds the slice, 13 tests on the real `flock`, no mock. Section 4.
5. The producer runs again as `chapter3-7dc6485-r3`, PID 1160587, pane `%41`, tmux session `arctic-ch3-production-r1`, over the whole frozen corpus of 4420 papers. Section 6.
6. The interrupt of the old producer was clean: `SIGINT` at 18:57:51 UTC with zero construction requests in flight, exit at 18:57:53 UTC. Section 6.
7. The fifteen-minute observation is healthy: 6 paid requests, USD 0.036829, no halt, `integrity_valid` true, and the stale cap refusal untouched. Section 6.
8. No new money and no new ledger transition. The applied USD 200 expansion transition stands, and the successor gate names the gate that authorized it. Section 5.
9. The evaluator cutover is prepared and not performed, by firstmate's decision of 19:10 UTC. Section 7, and `cutover.md` beside this report.

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

This is a successor activation of `arctic-ch3-candidate-fault-containment-r1`.
It adds no money and writes no ledger transition.
The applied USD 200 expansion transition stands, and the successor gate names the gate that authorized it in `supersedes_config_transition_review`, so that immutable event stays valid under the new gate.

`build-broker-operation-lock-wait.py` in the activation directory is the release script.
It is the predecessor's script with a new predecessor, a new suffix and one new step, `stop`.

| Artifact | Value |
|---|---|
| Commit | `f53e3e213fe270fc5149b4063e5b8a242091c421` |
| Predecessor commit | `fdf5ea805a1d9a641e4b6e798baf84864418dec2` |
| Source archive | `source-f53e3e2-arctic-broker-operation-lock-wait-r1.tar`, sha256 `0b7553f67298305dbb4a0d54654dcf9a7fc7f68a1d1276f0d27e5c4c6c7d2d36` |
| Runtime snapshot | `runtime/app-f53e3e2-arctic-broker-operation-lock-wait-r1`, sha256 `70266a2b44a69a2bb853bc39b788e0d89cae3a61974f9305df522da3d7ae5d1e` |
| Execution gate | `live-execution-gate-f53e3e2-lockwait.json`, sha256 `ef844c9f64bdd0ed40ea2ac07f056bc16fa0fcddc905a357bcd6dc8e9b669f7d` |
| Review record | `implementation-review-f53e3e2-lockwait.md` |
| Launcher | `launcher-f53e3e2-lockwait.sh` |
| Activation receipt | `activation-receipt-f53e3e2-lockwait.json` |
| Ledger transition | unchanged, `ledger-config-transition-09fd733-ch3x.json` of `arctic-ch3-expansion-200-r1` |

The re-snapshot proved every binding before it wrote the gate.
The price config, the eligibility prompt, the eligibility schema and the geography re-screen prompt are byte-identical to the predecessor runtime, so no price transition and no new eligibility identity are needed.
`STANDALONE_SYSTEM`, the standalone verification contract, the generation prompt version and the candidate schema version are unchanged, so the calibration cassette carries over.
The new runtime's own broker then read the shared ledger: `integrity_valid` true, `halted` false, `status_state` valid, the active `config_transition_sha256` unchanged at `b489a0d3...`, and the session ceiling still USD 253.990121.

The launcher differs from its predecessor in the runtime snapshot path and the gate path, and in nothing else.

The suite on the deployed commit, four bounded parts in parallel:

| Part | Result |
|---|---|
| `tests/` without the three slow files | 1164 passed in 386s |
| `tests/test_cli_integration.py` | 98 passed in 141s |
| `tests/test_streaming.py` | 61 passed in 520s |
| `tests/test_model_broker.py` | 88 passed in 164s |
| `ruff check` and `ruff format --check` | clean |

1411 tests passed.
`suite-result-f53e3e2.json` and `suite-f53e3e2.log` are in the activation directory.

Local `main` then moved to `341a0fd`, which carries `d64e8c4`, the evaluation-phase 503 release with the automatic vendor resume.
Firstmate's message 001 of 19:06 UTC asked for that merge before the branch finishes.
`main` is merged into this branch with a merge commit, and the branch head is `ea00336`.
The merge auto-merged `model_broker.py`, and every one of the eight operation-lock call sites still goes through `hold_operation_lock`.
The suite ran again on the merged head:

| Part | Result |
|---|---|
| `tests/` without the three slow files | 1170 passed in 384s |
| `tests/test_cli_integration.py` | 98 passed in 132s |
| `tests/test_streaming.py` | 61 passed in 513s |
| `tests/test_model_broker.py` | 88 passed in 149s |
| `ruff check` and `ruff format --check` | clean |

1417 tests passed.
`suite-result.json` and `suite-ea00336.log` hold that run.

## 6. The interrupt, the relaunch and the health observation

The `stop` step of the release script is new.
It reads the in-flight count per phase rather than the ledger's shared counter, because an evaluation request of the evaluator crew takes no construction slot and is no reason to wait.
It sent `SIGINT` at 18:57:51 UTC with zero construction requests in flight, the producer exited two seconds later, and the tmux session closed with it.

| Interrupt | Value |
|---|---|
| Signal | `SIGINT` to PID 3665656 |
| Sent at | 2026-09-16T18:57:51Z |
| Construction requests in flight | 0 |
| Exit confirmed | yes, 18:57:53Z |
| Ledger after | `halted` false, `inflight` 0, `spent_usd` 70.854614, `reserved_usd` 0.021016, `ambiguous_reserved_usd` 0.123539 |

The relaunch started at 18:57:56 UTC, on the same run id, the same campaign and the same frozen corpus of 4420 papers.

| Process | Value |
|---|---|
| PID | 1160587 |
| tmux session | `arctic-ch3-production-r1` |
| tmux pane | `%41` |
| Phase | `away_production` |
| Run id | `chapter3-7dc6485-r3` |
| Campaign | `arctic-qa-production-campaign-003` |
| Snapshot | `runtime/app-f53e3e2-arctic-broker-operation-lock-wait-r1` |
| Gate | `live-execution-gate-f53e3e2-lockwait.json`, sha256 `ef844c9f64bdd0ed40ea2ac07f056bc16fa0fcddc905a357bcd6dc8e9b669f7d` |
| Max papers | 4420 |

The fifteen-minute observation, 30 samples at 30-second intervals from 19:06:36 to 19:21:09 UTC:

| Measure | Value |
|---|---|
| Verdict | healthy |
| Producer alive at end | yes |
| Halted at end | no |
| `integrity_valid` at end | true |
| Requests completed in the window | 6 |
| Spend in the window | USD 0.036829 |
| Accepted questions | 38 at both ends |
| Remaining away-session allocation | USD 187.047167 |
| `settle-skipped` notes | none |
| Stale cap refusal | unchanged, still `not_submitted` with the paper-cost-cap reason, no live call |

The cap refusal of 12:37 UTC is the one request the relaunch must not touch.
It is byte-identical at both ends of the window, so the relaunch replayed it free and did not resume it.

One thing about the shape of this window is worth writing down, because it can be read as a stall.
A relaunched producer replays the papers its eligibility run directory already holds before it makes its first paid call.
That replay is free and CPU-bound.
The relaunch of 18:28 UTC made its first paid request at 18:48:23, twenty minutes later; this one made its first at about 19:19, and the window recorded six requests in its last two minutes.
For most of the window the producer was alive, `progress.json` moved every few seconds with a new DOI, and the ledger did not move at all.
That is the replay, not a stall.
`AGENTS.md` now says so.

## 7. The evaluator re-snapshot, and why it is not done

The brief asks for one more thing: re-snapshot the evaluator unit `arctic-abstention-stream-r3` onto the landed commit, with the same launcher and a new snapshot path, restart the unit, and repoint nothing else.
That cannot be done safely with the same work directory, and the reason is a binding in the evaluator, not in this fix.

The evaluator writes one immutable plan manifest per item, at `<work-dir>/runs/<item_id>/plan-manifest.json`, and that manifest holds `code_commit`.
`abstention_plan.py` compares a new manifest with the stored one field by field and raises "the run directory holds a different plan manifest" when they differ.
The launcher passes `--code-commit a0b9a82`, and the streaming authorization binds the same commit, so a snapshot of another commit also needs a new authorization.
Section 8.2 of `data/arctic-abstention-streaming-eval-r1/report.md` states the rule: a restart from a new commit needs a new snapshot, a new run id prefix, a new work directory and a new reviewed authorization, together.

The present state of the work directory `abstention-eval/streaming-r10` makes that expensive right now.

| Fact | Value |
|---|---|
| Items with a run directory | 10 |
| Items complete | 0 |
| Missing trials per item | 6, all of `claude-fable-5-1` |
| Gemini spend already recorded | USD 1.620107 |
| `claude-fable-5-1` resumes at | 2026-09-16T23:00:00Z |

The ten items are incomplete for one reason only: the captain paused `claude-fable-5-1` at 10:20 UTC because about 80 percent of the daily quota was used, and set the resume for the quota reset.
While the model is paused the watcher holds those items and never revisits them.
At 23:00 UTC the pause lifts, all ten leave the held set, and the watcher revisits them to run the last six trials of each.

If the unit runs another commit at that moment, every one of the ten raises the manifest error instead.
The error is journalled per item rather than raised, so the unit would stay up and look healthy while the captain's Fable window produced nothing.
Starting a new work directory instead abandons the ten partial items and re-pays for their Gemini trials.

The evaluator is not exposed to the fault this task repaired.
An evaluation request runs under `EVALUATION_PHASE`, which takes the evaluation admission lock and never the exclusive operation lock, so `execute` on that path never reached the refusal that ended the producer.

The options went to firstmate under the key `evaluator-resnapshot`.

Firstmate answered at 19:10 UTC and chose the deferral.
The running unit finishes every item it has started, the Fable trials that resume after 23:00 UTC included, and firstmate performs the cutover after that.
This task prepares the cutover and executes none of it.
No part of the evaluator was stopped, started or repointed here.

`data/arctic-broker-operation-lock-wait-r1/cutover.md` is the procedure.
It holds the preconditions, the rollback, the bound arithmetic and the answer to the ambiguous-charge question, and it names the script.

| Prepared | Value |
|---|---|
| Runtime snapshot | `runtime/app-ea00336-arctic-abstention-stream-r4`, from the merged head |
| Source archive sha256 | `1eb051f7f5753abc86dab023c2470d1e93cf4c12c9d3a35dde2ebb7b0312843e` |
| Run id prefix | `abstention-stream-r11` |
| Work directory | `abstention-eval/streaming-r11` |
| Review record | `streaming-eval-r7-review.md`, sha256 `1fccde9d1305f02552ef4a31fca89195c5850402c4873a71cf14a98e491b8b33` |
| Authorization, signed | `streaming-eval-r7-authorization.json`, sha256 `264bebec89a3555cf346f5f587c582731e24332516de85a41bedd17a78a7c28b` |
| Launcher | `streaming-eval-r7-launcher.sh` |
| Cutover script | `cutover-evaluator-r11.sh` |

The script defaults to a dry run and acts only with `--apply`.
Its dry run was exercised against the live state at 19:15 UTC and refused, correctly, at the precondition that no model may be held: `claude-fable-5-1` is held until 23:00 UTC and eleven items wait for its trials.
The two repoints, the cost guard's `--journal-dir` and the viewer's `--benchmark-journal-dir`, were tested against both live units without applying them: the argument vector is read from systemd, the argument count is unchanged, exactly one `streaming-r10` becomes `streaming-r11`, and the viewer's one argument that holds spaces survives as one token.

One thing the cutover must do is not obvious, so it is worth stating here too.
A new work directory starts with an empty cost journal, and `pending_item_ids` excludes only what the current journal knows, so the new run would re-evaluate and re-pay for every question the old run finished.
The script therefore copies the old `cost-journal.jsonl` forward.
The carried rows keep their own `run_id`, so no row claims to belong to the new run, and the item bound counts them as spent headroom.

## 8. Open points

| Point | Owner |
|---|---|
| The evaluator re-snapshot of section 7 waits on a decision. | firstmate, captain |
| The deployed producer runs `f53e3e2`, which is this branch before the merge of `main` at `341a0fd`. The merge brings `d64e8c4`, the evaluation-phase 503 release with the automatic vendor resume. Its broker half touches the ambiguous-continuation authorization and the phase halt reader, neither of which the producer uses, so the deployed producer needs no second interrupt for it. A later re-snapshot picks it up. | firstmate |
| The bound is 120 seconds and the poll is one second. No reviewed operation of this ledger has ever held the lock for longer, so the bound has never been reached in production. It is a constant in `model_broker.py` if a future operation needs more. | this task |
| A bound-exceeded fault settles the family's completed calls as `incomplete_infra` and skips the paper, so it discards paid work for that family. With a 120-second bound against operations that take milliseconds, that path should stay unused. | this task |
