# The evaluation bounds, the silent stop, and the phase of a refused request

Task: `arctic-eval-authorization-r8`.
Date: 2026-09-16.

This record holds four things: why the live benchmark stopped without a word, how the bounds were widened to the captain's allocation, how one refusal row halted the whole shared ledger, and what now says out loud that the benchmark stopped.

## 1. The silent stop

The captain's standing order of 2026-09-16 allocates USD 200 for benchmarking the Gemini models and asks that every machine-accepted question run on the benchmark as it lands.

The streaming evaluator ran as the systemd user unit `arctic-abstention-stream-r3` under the r6 authorization.
That authorization carried canary-scale bounds: 12 items and USD 3.00 of Gemini spend.
They were written for a bounded proof of the service and were never widened when the allocation arrived.

At 2026-09-16T19:31:44Z the evaluator met the item bound, logged `item_bound_reached`, and exited with code 0.
A bound is not an error, so nothing said that the benchmark had stopped.
A restart at 22:57:58Z exited the same way in one poll.
Between those two moments the pipeline accepted four more questions that nobody evaluated, and the 12 evaluated items still waited for their 6 `claude-fable-5-1` trials each.

## 2. The widened bounds

Two authorizations now carry the bounds of the allocation, 2000 items and USD 200.00.

| Authorization | Prefix | Commit | Work directory | Bounds |
|---|---|---|---|---|
| `streaming-eval-r8-authorization.json` | `abstention-stream-r10` | `a0b9a82` | `streaming-r10` | 2000 items, USD 200.00 |
| `streaming-eval-r10-authorization.json` | `abstention-stream-r11` | `a65348d` | `streaming-r11` | 2000 items, USD 200.00 |

2000 is the `accepted_question_target` of the active construction policy, so it is the number of questions this campaign produces.
USD 200.00 is the `evaluation_ceiling_usd` of `config/benchmark-evaluation-policy-v3.json`.
The shared ledger enforces that ceiling on its own, so the authorization bound and the ledger ceiling are now the same number and the ledger stays the control that cannot be bypassed.

At USD 0.168 of Gemini per item, 2000 items cost about USD 337, which is more than the ceiling.
The cost guard reads that extrapolation every five minutes and pauses a model before the ceiling is met.
The bound is no longer the thing that stops the run; the money is.

The r8 authorization restarted the r10 unit at 23:03:25Z and it evaluated again at once.
It did not survive, for the reason of section 3, and the run moved to `streaming-r11` on commit `a65348d` at 23:22:43Z.

## 3. One refusal row halted the shared ledger

At 22:58:25Z the chapter 3 paper-concurrency activation applied the reviewed configuration transition `2d663a9f`.
An applied transition is validated again on every broker start until its first construction request, and that validation compares the ledger hash with `expected_ledger_sha256`.
`_only_evaluation_activity_since` is the one exception: an evaluation request may land in between, a construction request may not.

At 23:03:53Z the evaluator recorded one `not_submitted` refusal, request `52c5da75`, with the reason "the evaluation repeat limit for this item is complete".
The broker wrote `phase` at the reservation, and this refusal stopped before the reservation, so the row carried no phase.
`_only_evaluation_activity_since` reads a row without a phase as a construction request.

At 23:03:56Z the next broker start refused the transition with "the configuration transition ledger hash changed" and wrote the integrity halt record.
Every broker start refuses while that record is on disk, so the chapter 3 producer and the evaluator both stopped.

### The repair

`settle-phaseless-refusal` is the reviewed repair, applied at 23:18:03Z.
Section "The phase of a refused request" of `docs/SHARED_MODEL_BROKER.md` holds the contract.
The review record is `phaseless-refusal-settlement-review.md` of the abstention-eval private directory, and the receipt is `52c5da75....phase-settlement.json` of the model receipts directory.

The row holds no money, so the settlement moves none: it asserts that the row has no `submitted_at_utc`, no `usage`, no reservation and no cost.
It reads the phase from the row's own stage prefix, evaluation trial, evaluation gate hash and evaluation policy hash, and writes that one field.
It supersedes the integrity halt record rather than erasing it, and the receipt binds the renamed record by hash.

After it, `integrity_valid` was true, the applied transition validated again, and exactly one request of this ledger carried a timestamp at or after 22:58:25Z: the repaired one.

### The cause, not the symptom

The repair fixes one row. Two changes stop the fault.

The reader half belongs to task `arctic-ch3-paper-concurrency-r1`, which makes `_only_evaluation_activity_since` read a phase-less evaluation row by its stage (commit `cae0172`).

The writer half is in this record: a request records its `phase` from its first ledger record instead of from its reservation.
Commit `a65348d` did not yet carry it, so the run on `streaming-r11` wrote 12 more phase-less rows in its first 15 minutes.
The fix reaches the unit at the next cutover.

## 4. What says it out loud now

Two halves, inside and outside.

The evaluator takes `--status-file` and appends one `blocked:` line when a bound ends the run: the item bound, every vendor paused, and the Gemini budget bound.
That third case does not end the run, because the subscription vendors go on, but it turns off the arm the allocation pays for, and the 503 of 17:53Z showed what that silence costs.

The cost guard reports an evaluator whose watch state is absent or has not moved for 900 seconds, as an error of `guard-state.json` and as one `blocked:` line of its status file, with one `working:` line when it polls again.
It reports the change of state and not the state, so a long stop adds one line and not one per cycle.
This half catches a stop of any kind: a crash, a halt of the shared ledger, or an operator who stopped the unit and forgot it.

The guard reads `watch-state.json`, and the evaluator wrote that file only at the end of a whole poll cycle.
One cycle covers every pending item, so at sixteen pending items it runs for about an hour, and the guard's bound is 900 seconds.
A guard that read the old behaviour would have called a healthy evaluator stopped, every hour.
The evaluator now publishes after every item as well, so the file moves while the cycle runs and the two halves agree.

## 5. The 12 items of `streaming-r10`

The cutover copies `cost-journal.jsonl` forward, so no question is evaluated or paid for twice, and `remaining_bound` subtracts the 12 carried rows from the bound of 2000.

Those 12 items are not complete.
Eleven of them wait for 6 `claude-fable-5-1` trials each and one has its Fable trials, so 66 trials are missing.

They cannot be completed under `streaming-r11`, and the reason is not the code commit alone.
The per-item plan manifest binds the run id, the evaluation set directory and the gate directory as well, and all three name `streaming-r10`.
Writing the missing trials into the r10 run directory under an r11 run id would put two run ids in one run directory and make the money evidence of that item ambiguous.
And `a0b9a82`, the only code that could continue the r10 run, halted the shared ledger and must not run again.

So the 12 items are carried forward as history, and each one is evaluated again whole under `streaming-r11`.

That backfill needs no decision and no edit, because the evaluator already does it.
`pending_item_ids` excludes the items the journal calls *complete*, and a carried row whose Fable trials are missing is not complete.
So the evaluator takes each of those 12 items as pending, freezes it as a new one-item set under its own r11 run id, and runs the whole 48-trial plan on it.
`CostJournal.latest_item_rows` keeps the last row of each item, so the r11 row replaces the r10 row in every total and nothing is counted twice.
The r10 rows stay in the journal as history, with their own run ids.

The first of them, `aqa-26f384109591547bc96a`, finished at 2026-09-17T00:17:24Z with all 48 trials recorded, `complete` true, no paused trials, and USD 0.156859 of Gemini in 571 seconds.
The 12 together cost about USD 1.9 of Gemini, which is one percent of the allocation, and the result is 12 complete items instead of 12 items at 42 of 48 trials.
Their partial r10 Gemini spend, USD 2.02, is already booked and is not recovered: that is the price of the snapshot that had to be retired.

## 6. The start-order race, still open

At 23:39:44Z a second integrity halt appeared, with another reason: "a configuration transition requires a settled ledger".
A broker starting on the new policy pair refused because the evaluator had one Gemini call in flight.

While an applied transition waits for its first construction request, its validation needs `inflight` to be 0, and a live evaluator breaks that at any moment.
The producer start and the evaluator therefore cannot overlap until the producer has made one paid construction call, which closes that validation for good.

A broker start on the new policy pair needs a settled ledger, and only the producer's first paid construction call ends that condition.
So until that call lands, the two starts are serialized, in this order:

1. Stop the evaluator, so nothing of the evaluation phase is in flight.
2. Supersede the integrity halt record and settle every interrupted request, until `inflight` is 0 and `integrity_valid` is true.
3. Start the producer alone and wait for its first paid construction call.
4. Start the evaluator again.

That is the order this task and the completion worker ran on 2026-09-16 between 23:46Z and 2026-09-17T00:07Z, and it worked: the producer made its first construction call at 23:53Z and the evaluator returned at 00:07:40Z.
After that first construction call the condition is closed for this transition, and the two run together again, as they did all day.

## 7. What the ledger repair actually took

Two integrity halts and two repairs, in this order.

| Moment | Halt reason | Repair |
|---|---|---|
| 23:03:56Z | `the configuration transition ledger hash changed` | `settle-phaseless-refusal` of request `52c5da75`, applied 23:18:03Z |
| 23:39:44Z | `a configuration transition requires a settled ledger` | the broker's own orphan recovery of request `bd118f1f`, applied 23:50Z |

The second halt is the start-order race of section 6.
The evaluator's Gemini call `bd118f1f` was in flight when a broker started on the new policy pair, and that start needs `inflight` to be 0.
The call itself finished: its immutable final receipt records `completed`, `live_call_made` true and an actual cost of USD 0.012987.
Only the ledger row stayed `submitted`, because the settlement read the ledger after the halt record was written and every read refuses then.

The repair is the broker's own `_recover_orphans`, which settles that row from the immutable receipt and makes no paid call.
The script is `recover-orphan-bd118f1f.py` of the activation directory `arctic-eval-authorization-r8`.
It supersedes the halt record first, accepts only the halt of this incident, and prints the settled row and the ledger totals.
Neither repair decided a number: the receipt held the money in one case and the row held no money in the other.

A broker start on a policy pair that the ledger holds no request under runs the whole transition validation.
A broker start on a pair the ledger already holds requests under does not.
That is why both repairs were made with the construction files of the older pair, and why the evaluator, which binds those files, could start while the producer could not.

## 8. The timeline

| UTC | Event |
|---|---|
| 19:31:44 | The evaluator meets the item bound of 12, logs `item_bound_reached`, exits 0. Nothing says so. |
| 22:57:58 | A restart exits the same way in one poll. |
| 22:58:25 | The paper-concurrency transition `2d663a9f` is applied. |
| 23:03:25 | The r8 authorization, 2000 items and USD 200, restarts the r10 unit. It evaluates again. |
| 23:03:53 | A phase-less refusal, request `52c5da75`. |
| 23:03:56 | The first integrity halt. Producer and evaluator both stop. |
| 23:18:03 | The reviewed phase settlement. `integrity_valid` is true again. |
| 23:22:43 | The cutover to snapshot `a65348d`, prefix `abstention-stream-r11`, work directory `streaming-r11`. |
| 23:39:44 | The second integrity halt: a producer start meets the evaluator's call in flight. |
| 23:50 | The orphan recovery. `inflight` 0, `integrity_valid` true, the ledger clear for the producer. |
| 23:53 | The producer makes its first paid construction call, which closes the transition validation for good. |
| 00:07 (17th) | The evaluator restarts on `streaming-r11` with all three vendors. |

## 9. The state at the end of this task

The evaluator runs as `arctic-abstention-stream-r3` from the snapshot `runtime/app-a65348d-arctic-eval-authorization-r8` of the activation directory `arctic-eval-authorization-r8`, on work directory `streaming-r11` and run id prefix `abstention-stream-r11`, with all three vendors and the Fable arm active.

| Fact | Value |
|---|---|
| Restarted | 2026-09-17T00:07:40Z |
| First new evaluation receipt | `c79aad30f84188da9d57756e82b8e5e21d322b14bf2d826eba42fba9b6b707d4` |
| That receipt | `evaluation_answer:gemini-3.8-flash`, submitted 00:08:35Z, completed, USD 0.029674 |
| Shared ledger | `halted` false, `evaluation_halted` false, `integrity_valid` true |
| Carried journal rows | 16, of which 12 are item rows, so `remaining_bound` is 1988 |
| Backfill owed | none as a separate task: the 12 carried items are re-evaluated whole as pending items |

Neither half of section 4 is live yet.
The running snapshot is `a65348d`, which predates both, so `streaming-eval-r10-launcher.sh` passes no `--status-file`: the flag does not exist there and the start would refuse it.
The cost guard runs its own older snapshot too.
Both halves reach their units at their next re-snapshot, after this branch merges.
Until then the bounds are 2000 items and USD 200.00, so a bound stop is far away, and the cost guard's extrapolation is the live control.

The cutover receipt is `cutover-receipt-20260916T232243Z.json` of the activation directory.
It was written by hand from the evidence, because the script timed out waiting for `watch-state.json`, which the watcher writes only at the end of a whole poll cycle.
The script now waits for a new journal row as well, which is the earlier proof.
