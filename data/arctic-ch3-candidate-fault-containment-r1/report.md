# Chapter 3 candidate-fault containment report (arctic-ch3-candidate-fault-containment-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-candidate-fault-containment-r1`, from local `main` at `37201d6`.
Captain standing order (2026-09-16): the chapter 3 production run must run unattended overnight with constant supervision.
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-candidate-fault-containment-r1/`.
Predecessor activation: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-settle-not-submitted-r1/` (report `data/arctic-ch3-settle-not-submitted-r1/report.md`).

## 1. Result in eight lines

PLACEHOLDER_SUMMARY

## 2. The fault

At 16:28 UTC on 2026-09-16 the producer of run `chapter3-7dc6485-r3` stopped with this line:

```json
{"code":"VALUEERROR","message":"option repair trigger is invalid","status":"error"}
```

The launcher log is `launcher-14783d7-ch3settle.log` in the predecessor activation directory.
The deployed snapshot was `app-14783d7-arctic-ch3-settle-not-submitted-r1`, commit `14783d7`.

The state database `/mnt/crdata/research-abstention/arctic-qa/state.sqlite3` holds the whole chain.
It was read read-only, through a copy.

**The candidate.**
Paper family `family-a5bcbcf9a3aff33d55ed`, source `src-a5bcbcf9a3aff33d55ed`, a lipid-profile comparison between NAO and Arkhangelsk.
Candidate `aqa-c19e58a71497c5bad90c`, written at 16:27:39 UTC on the primary attempt `generation-attempt-b7aaa8b2aec3c5f232c3`.
The answer was "lower"; the option set held "higher", "substantially higher", "equivalent" and "undetermined".

**The verdict.**
Validation event `validation-351efd82bc41a0765734`:

| Field | Value |
|---|---|
| `label` | `machine_accepted_unverified` |
| `reason_codes` | `["option_set_not_mutually_exclusive"]` |
| `labels.mcq_eligible` | false |
| `created_at` | 2026-09-16T16:27:39Z |

The verdict is correct.
"higher" and "substantially higher" can both be true at once, so the whole-set judge refused mutual exclusion.
The question passed every gate, so the candidate became `incomplete_non_mcq` and routing asked for a new option set on the verified question.

**The attempt routing built.**
The three `option_verification` calls of 16:26:57 to 16:27:31 UTC are that judge sequence.
Routing then built the record now stored on candidate `generation-call-5b789d0e60d108a40ada`:

| Field | Value |
|---|---|
| `attempt_kind` | `option_repair` |
| `contract_version` | `bounded-failure-routing-v5` |
| `finding_attempt_index` / `question_revision_index` | 1 / 1 |
| `parent_item_id` | `aqa-c19e58a71497c5bad90c` |
| `trigger_reason_code` | `option_set_not_mutually_exclusive` |

**The refusal.**
`streaming._validate_generation_attempt` accepted that attempt, because `streaming.OPTION_REPAIR_REASONS` held the code.
`_open_generation_call_record` then wrote the call record, which is why the row above exists.
`generation.generate_candidate` called `_validated_generation_attempt`, which read:

```python
if value["trigger_reason_code"] != "insufficient_verified_distractors":
    raise ValueError("option repair trigger is invalid")
```

The producer exited.
No paid call was made for the repair: the ledger stayed at USD 68.136671 with `inflight` 0 and `integrity_valid` true.

**The root cause is a split enum, not a rogue producer.**
The brief asked me to fix the producer of the trigger and not to widen the enum.
The record shows the trigger is a documented option contract code and the producer is right:

1. Commit `6d6ed47` ("Correct the standalone judge and the option stage for chapter 3", ch2 yield audit 4.8) added `option_set_not_mutually_exclusive` and `option_set_answer_not_choosable` to `streaming.OPTION_REPAIR_REASONS`.
2. The same commit added per-code repair text for both codes to `generation.OPTION_REPAIR_GUIDANCE`, which the option repair prompt sends.
3. The same commit left the check in `generation._validated_generation_attempt` naming one code. It is the only place in the codebase that disagrees.

So two validators of the same object named different sets, and the option stage was correct on both sides of the disagreement.
I added no code to the rung.
The set now names exactly the three codes that already route to it, and one owner holds it, so the two layers cannot drift again.
Refusing the two set codes at the producer instead would have deleted the audit 4.8 repair and lost the yield it buys.

## 3. The fixes

### 3.1 One closed trigger set, owned by the contract

| File | Change |
|---|---|
| `src/arctic_qa/generation.py` | New `OPTION_REPAIR_TRIGGER_REASONS`, a frozen set of the three codes that route to the option repair rung. `_validated_generation_attempt` tests membership of it. |
| `src/arctic_qa/streaming.py` | `OPTION_REPAIR_REASONS` is that constant. The routing layer no longer keeps its own copy. |

The contract's rigor does not move:

- Every other rule of the `option_repair` rung stands: the revision index must be 1 or 2, the parent candidate must be a string, and the attempt may exclude no finding.
- A trigger outside the set is refused with the same message, which section 4 pins.
- No prompt, no schema and no contract version changes, so every prompt hash and the run manifest binding stay as they are.

### 3.2 One paper family is one unit of work

| File | Change |
|---|---|
| `src/arctic_qa/streaming.py` | The body of the paper loop in `run_stream` runs inside one `try`. An exception that is not a money stop is contained and the producer continues with the next paper. |
| `src/arctic_qa/streaming.py` | `_ends_the_run` names the stops that still end the producer. |
| `src/arctic_qa/errors.py` | `mark_run_stop` and `is_run_stop`. The marker keeps the exception's class and message and adds one attribute. |
| `src/arctic_qa/broker_provider.py` | `broker_boundary`, the seam. Every refusal the shared broker raises is marked a run stop there, except the three that describe one candidate or one family. |
| `src/arctic_qa/streaming.py` | `_contain_candidate_processing_fault` settles the family, records it and returns the record. |
| `src/arctic_qa/streaming.py` | `_released_family_reservation` proves through the broker's own `family_cost_state` that no reservation is left in flight for the family. |
| `src/arctic_qa/streaming.py` | `_settle_candidate_processing_fault` writes the exception class, message and stage onto every in-flight call record of the family. |
| `src/arctic_qa/streaming.py` | `_record_candidate_processing_fault` writes the `generation_routing` row. |
| `src/arctic_qa/streaming.py` | `_Progress.error` remembers the stage it recorded, so the settled fault names it. |

What a contained fault leaves behind:

1. **The candidate.** Every `incomplete_infra` call record of the family keeps that status and gains `candidate_processing_fault`, a record of `candidate-processing-fault-v1` with `error_class`, `error_message` and `stage`. The row's `reason_code` becomes `candidate_processing_fault`. The status does not move, because the outcome of the opened call is still unknown.
2. **The routing row.** One `rejection_ledger` row at stage `generation_routing` with reason code `candidate_processing_fault`, holding the campaign, the candidate key, the family, the selection row, the exception, the stage, the settled call records and the family's cost state. The insert is idempotent on that identity.
3. **The reservation.** `_released_family_reservation` reads the family's own cost row through the broker. `execute` is the broker's only reservation path and it settles every request it opens, an ambiguous charge included, so a contained fault has nothing left to release. If that row still shows a reservation, the fault is **not** contained: the producer raises, because stranded money is settled through the reviewed settlement path and never by skipping past it.
4. **The run.** `counts["candidate_processing_fault"]` and the progress counts rise, a `paper_results` row records the disposition, the progress file returns to `running`, and the loop continues with the next paper.

The stops that still end the producer:

| Stop | How it arrives |
|---|---|
| Allocation ceiling, session ceiling, construction checkpoint | `BudgetError` from the `not_submitted` receipt reason |
| Away submission cap, accepted-question target | `BudgetError` from the same path |
| Unsettled charge | `AmbiguousChargeError` |
| Execution gate, halted ledger, ledger integrity failure, every other broker refusal | the marker `broker_boundary` puts on it at the seam |
| A direct read of the shared ledger, which crosses no seam | `ValueError` with a message in `RUN_ENDING_LEDGER_STOPS` |
| Stop signal | `KeyboardInterrupt` and `SystemExit` are `BaseException`; the containment catches `Exception` only |

The per-paper cost cap keeps the behaviour the predecessor gave it: `PaperCostCapError` bounds one family and is never a run stop.

### 3.3 Why the seam, and not a list of messages

The first version of the containment named the run-ending messages in one list.
The suite found the hole at once: `streaming live generation is disabled`, which `_validate_gate` raises before every paid call, was contained, and the producer walked the whole corpus with a disabled gate.
A list of messages is a list of the refusals somebody remembered.

The seam is the rule instead.
`broker_provider.broker_boundary` wraps the three places a `BrokerProvider` talks to the shared broker: `execute`, the stored-receipt read and `record_accepted`.
Everything the broker refuses there is marked a run stop, except `CandidateRejectedError`, `ProviderResponseError` and `PaperCostCapError`, which describe one candidate or one family by their own class.
A new refusal message therefore ends the producer with no second registration.
One direct read of the shared ledger, `operational_unresolved_families`, does not cross that seam; `_operational_unresolved_families` marks its refusal in the same way.

## 4. Tests

New file `tests/test_ch3_candidate_fault_containment.py`, 29 tests.

| Test | What it holds |
|---|---|
| `test_the_recorded_option_repair_attempt_passes_the_generation_contract` | The exact 16:28 UTC exit. The attempt is the recorded row of `generation-call-5b789d0e60d108a40ada`, field for field. It raised `option repair trigger is invalid` on the unfixed code. |
| `test_the_recorded_option_repair_attempt_passes_the_routing_contract` | The same attempt through the routing validator. It was already green, which is the disagreement. |
| `test_the_recorded_whole_set_verdict_routes_to_the_option_repair_rung` | The recorded reason codes of validation event `validation-351efd82bc41a0765734` pick the `option_repair` rung. |
| `test_the_contract_and_the_routing_layer_name_one_closed_trigger_set` | The two layers read one object, and it names exactly three codes. |
| `test_a_trigger_outside_the_closed_set_is_still_refused` | Three triggers outside the set, still refused. |
| `test_a_candidate_fault_is_not_a_run_stop` | The recorded `ValueError`, a `KeyError` and the per-paper cap are contained. |
| `test_a_money_stop_ends_the_producer` | Six money stops: the lifetime ceiling, the away cap, the submission limit, the accepted target, a broker halt and a ledger integrity halt. |
| `test_the_recorded_fault_settles_its_paper_and_the_run_continues` | The producer over two papers. The first raises the recorded error, the run completes, `processed` is 2, and the second paper keeps its own disposition. |
| `test_a_contained_fault_leaves_a_generation_routing_row` | One `generation_routing` row with reason code `candidate_processing_fault`, the exception class, the message and the stage. |
| `test_a_contained_fault_settles_its_in_flight_call_record` | The family's call record stays `incomplete_infra` and carries the whole `candidate-processing-fault-v1` record. |
| `test_a_key_error_in_one_candidate_is_contained_too` | A `KeyError` takes the same path. |
| `test_a_ceiling_stop_still_ends_the_producer` | A `BudgetError` in the same place still raises out of `run_stream`. |
| `test_a_ledger_integrity_failure_still_ends_the_producer` | A ledger integrity halt still raises out of `run_stream`. |
| `test_the_broker_seam_marks_a_refusal_as_a_run_stop` | Five broker refusals, the four gate refusals included, keep their class and message and end the producer. |
| `test_the_broker_seam_leaves_a_paper_level_refusal_alone` | The three paper-level refusals cross the seam unmarked. |
| `test_a_disabled_execution_gate_still_ends_the_producer` | The hole the suite found: the message alone is not a run stop, and the same message from the broker is. |

On the unfixed code 13 of the first 20 fail, including every recorded-data test and every containment test.
The seven that pass on the unfixed code are the routing-contract test, which is the half of the disagreement that was already green, and the six money stops, which already ended the producer.

PLACEHOLDER_SUITE

## 5. The release

PLACEHOLDER_RELEASE

## 6. The relaunch and the health observation

PLACEHOLDER_OBSERVATION

## 7. Open points

PLACEHOLDER_OPEN
