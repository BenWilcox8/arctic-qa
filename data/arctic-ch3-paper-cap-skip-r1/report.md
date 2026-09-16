# Chapter 3 paper-cost-cap skip report (arctic-ch3-paper-cap-skip-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-paper-cap-skip-r1`, from local `main` at `3f7de6e`.
Captain standing order (2026-09-16): the chapter 3 run must run unattended overnight with constant supervision; "if there is some issue that arises such that over $10 is spent in a row without a machine accepted question, stop and analyze what is happening. If it is simply a dead streak, then continue the run."
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-paper-cap-skip-r1/`.
Predecessor activation: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-expansion-200-r1/` (report `data/arctic-ch3-expansion-200-r1/report.md`).

## 1. Result in nine lines

1. The per-paper cost cap now ends one paper family, not the run. Commit `9c87fc4` on branch `fm/arctic-ch3-paper-cap-skip-r1`. Sections 2 and 3.
2. The producer runs again as `chapter3-7dc6485-r3` on campaign `arctic-qa-production-campaign-003`, PID 4020626, pane `%37`, tmux session `arctic-ch3-production-r1`, over the whole frozen corpus of 4420 papers. Section 6.
3. The relaunch met the exact family that stopped the run at 12:37 UTC, `family-e3d2c9790e5499a69728` (`10.7557/7.8033`), recorded it at 13:27:20 UTC and continued. Section 7.
4. The first ten minutes were healthy: 17 paid requests, USD 0.261740, no halt, `integrity_valid` true, four more papers screened. USD 194.990993 of the allocation remains. Section 6.
5. No new money and no new ledger transition. The applied USD 200 expansion transition stands, and the successor gate names the gate that authorized it. Section 5.
6. The cap stays at USD 1.00, and nothing is charged past it. Only a whole-run stop still ends the producer. Section 3.
7. A relaunch replays the stored refusal for free and never tries the capped family again. Two tests prove this. Section 4.
8. The suite is green on the deployed commit: 1326 passed in four parts, ruff clean. `suite-result.json` in the activation directory.
9. Two steps of the inherited activation procedure needed a correction: the launch precondition and the gate succession block. Section 5.

## 2. The fault

At 12:37 UTC on 2026-09-16 the producer of run `chapter3-7dc6485-r3` stopped with this line:

```json
{"code":"BUDGET_EXHAUSTED","message":"the paid request exceeds the paper cost limit","status":"error"}
```

The launcher log `launcher-9862787-ch3x.log` of the expansion activation holds that line two times.
The first line is the exit.
The second line is the relaunch, which stopped the same way.

The chain was this:

1. `maximum_paper_cost_usd` in the policy `streaming-dataset-budget-policy-v10-chapter3-expansion.json` is USD 1.00. It bounds one paper family.
2. `SharedGeminiBroker._reserve` refuses a construction request that takes the family past that cap. It writes an immutable `not_submitted` receipt. It charges nothing and it does not halt.
3. `broker_provider._provider_result` made a plain `BudgetError` from every `not_submitted` receipt.
4. `streaming._progress_generation` accepted one `BudgetError` message as a skip, `PER_REQUEST_CAP_REASON`. It raised every other message again, so the producer stopped.
5. The refused receipt is immutable, and the paper-cap reason is not in `RESUMABLE_NOT_SUBMITTED_REASONS`. Each relaunch read the stored refusal, raised the same `BudgetError` at the same point, and stopped on the same paper. A relaunch made no paid call, and no progress was possible.

The family that stopped the run is `family-e3d2c9790e5499a69728` (`10.7557/7.8033`).
It stopped at stage `finding_answer_extraction` with USD 0.965092 of the USD 1.00 cap spent.

The budget of a paper family is cumulative over the whole project, not over one run.
That family made 37 paid calls across five earlier runs before chapter 3 reached it.
The frozen corpus of 4420 papers holds every paper that the earlier chapters worked on.
The run must therefore expect families that are already near their cap.
Without a repair, the fault stops the run again and again.

## 3. The fix

One rule: **the per-paper cost cap bounds one family, never the run.**

| File | Change |
|---|---|
| `src/arctic_qa/model_broker.py` | `PAPER_COST_CAP_REASON` names the refusal, and `_reserve` raises it. `family_cost_state(family_id)` returns the reserved, spent, ambiguous and committed money of one family against the cap. |
| `src/arctic_qa/errors.py` | `PaperCostCapError(BudgetError)`, code `PAPER_COST_CAP_REACHED`. It carries the stage that stopped. |
| `src/arctic_qa/broker_provider.py` | A `not_submitted` receipt with the paper-cap reason raises `PaperCostCapError` with the stage of the receipt. Every other refusal keeps its `BudgetError`. |
| `src/arctic_qa/streaming.py` | The producer catches `PaperCostCapError` at the generation stage and at the eligibility stage. It settles the in-flight call record as `generation_incomplete` with reason code `paper_cost_cap_reached`. It writes a `rejection_ledger` row at stage `paper_cost_cap`, counts the family under `paper_cost_cap_reached`, and continues with the next paper. |
| `src/arctic_qa/gemini_batch.py` | The batch path maps the same disposition, so the cap ends one family there too. |

The recorded row holds the reason code, the family ID, the committed spend of the family against the cap, the broker stage that stopped, the selection row and the generation attempt.

These four properties did not change:

- The cap stays at USD 1.00. It is one of the five values that `_validate_policy` pins exactly. It cannot move without a registered budget transition.
- Nothing is charged past the cap. The refusal happens before the reservation.
- A whole-run stop still ends the producer: the allocation ceiling, the away session ceiling and a broker halt.
- The refused receipt stays immutable and stays non-resumable. A relaunch replays it for free and never tries the capped family again.

## 4. Tests

Five new tests. One test that already existed keeps its place.

| Test | What it proves |
|---|---|
| `test_broker_provider.py::test_paper_cost_cap_refusal_names_the_family_and_charges_nothing_past_it` | A real broker, a real family and real money. Repeated costly calls take the family to the cap. The next call raises `PaperCostCapError` with the stage. The broker is not halted. The committed money of the family stays at or under USD 1.00. The refusal is on disk as a `not_submitted` receipt with `live_call_made: false`. |
| `test_broker_provider.py::test_a_capped_family_is_not_retried_on_relaunch` | A second provider on the same ledger reaches the same refusal at the same call. It makes no new provider call, and the spend of the family does not move. This is the resume path of the fault. |
| `test_streaming.py::test_streaming_completes_when_the_broker_refuses_a_capped_family` | The whole path with a real broker. The run ends `completed`, not with an exception. The rejection row holds the reason code, the broker stage and the committed spend of the family from the ledger. |
| `test_streaming.py::test_streaming_records_a_capped_family_and_keeps_going` | Two papers. The cap ends the first family, and the record names it. The second paper is generated and accepted. The call record of the capped family is settled as `generation_incomplete` with reason `paper_cost_cap_reached`. The progress file names the skip. |
| `test_streaming.py::test_streaming_still_exits_on_a_whole_run_stop` | Three whole-run messages (allocation ceiling, session ceiling, halt) still stop the producer. Each writes the error state to the progress file and writes no `paper_cost_cap` row. |
| `test_streaming.py::test_streaming_skips_only_a_request_above_the_per_request_bound` | Unchanged. The per-request bound still skips, and the live-test cap still raises. |

## 5. The release

No new money and no new ledger transition.
The applied USD 200 expansion transition of `09fd733` stands.
The successor gate names the gate that authorized it.

| Artifact | Path |
|---|---|
| Source archive (immutable, mode 444) | `source-9c87fc4-arctic-ch3-paper-cap-skip-r1.tar`, SHA-256 `6ce8649409789fa2e6d8bc3aeb520cd97cfa3cd958eb003e8942a140541f6bc5` |
| Runtime snapshot | `runtime/app-9c87fc4-arctic-ch3-paper-cap-skip-r1`, tree SHA-256 `bdba1657c7e4f0fb0773b475618ff7e1a275fbe774126e734208be74bfab293f` |
| Implementation review | `implementation-review-9c87fc4-ch3cap.md` |
| Execution gate (immutable, mode 444) | `live-execution-gate-9c87fc4-ch3cap.json`, SHA-256 `bede18b5faaf2443b07109aeb1c71d30202d6e5917736782e5dae742f08feb3f` |
| Launcher (mode 555) | `launcher-9c87fc4-ch3cap.sh` |
| Activation receipt | `activation-receipt-9c87fc4-ch3cap.json` |
| Activation state | `activation-state-9c87fc4.json` |
| Build script | `build-paper-cap-skip.py`, with the modes `resnapshot`, `launch`, `observe` and `phase-e` |
| Read-only monitors | `status.sh` and `skipped-families.py` |

The gate is the predecessor gate `live-execution-gate-9862787-ch3x.json` with these fields moved: the commit, the review record and its hash, the source archive hash, the runtime snapshot hash and path, and the prior gate binding.
`supersedes_config_transition_review` is inherited unchanged and still names the `09fd733` gate that authorized the expansion event, so that immutable event stays valid under the active gate.

The broker of the new runtime validated the shared ledger under policy v10, the new gate and the applied transition.
The result: `integrity_valid` true, `halted` false, `config_transition_sha256` equal to the applied expansion event, and `away_session_total_ceiling_usd` USD 253.990121.
This validation makes no paid call.

The producer of the previous launch already stopped at 12:37 UTC at a zero-in-flight boundary.
An interrupt was therefore not necessary.
The tmux session `arctic-ch3-production-r1` was absent, no process held the run id, and the ledger showed no unsettled construction request of `chapter3-7dc6485-r3`.

I changed one step of the inherited activation procedure. The predecessor `launch` asserted that the ledger counter `inflight` is zero. The evaluator crew runs `benchmark_evaluation` requests on the same ledger, and that counter covers every phase, although the broker counts concurrency per phase since `3cd57a8`. An evaluation request in flight takes no construction slot. The precondition now reads the unsettled construction requests of this run only. `construction_requests_in_flight` in `build-paper-cap-skip.py` does that test.

I corrected one more step. The successor gate keeps the `supersedes_config_transition_review` block of its predecessor, which names the `09fd733` gate that authorized the expansion event. My first attempt wrote the predecessor gate into that block, and the runtime refused the ledger with "the configuration transition review changed". `_gate_succeeds_transition_review` compares those four fields with the authorization itself, so a second successor must still name the gate that authorized the event. The script now asserts this before it writes the gate.

## 6. The relaunch and the first ten minutes

The producer started at 13:24:12 UTC on the `9c87fc4` runtime, in tmux session `arctic-ch3-production-r1`, pane `%37`, PID 4020626. It covers the whole frozen corpus of 4420 papers.

The ten-minute observation, taken every 30 seconds from the ledger, the ledger status file and the progress file:

| Measure | Value |
|---|---|
| Window | 13:24:47 to 13:34:19 UTC, 601 seconds, 20 samples |
| Producer alive at the end | yes |
| Halted at the end | no |
| `integrity_valid` at the end | true |
| Paid requests completed in the window | 17 |
| Spend in the window | USD 0.261740 |
| Families the cap skipped | 1 |
| Papers screened, start to end | 41 to 45 |
| Eligible, start to end | 26 to 29 |
| Allocation left at the end | USD 194.990993 |
| Verdict | healthy |

The first minutes replayed the work of the 41 families that the earlier launches reached. Those replays read stored receipts and cost nothing: `spent_usd` held at USD 59.208402 until 13:27. The producer then met the capped family, recorded it, and moved on to new papers. From 13:28 the spend and the request count moved again.

The captain's rule for a dead streak is USD 10 of spend with no machine accepted question. The last acceptance of this campaign was at 12:29:27 UTC. Construction spend since then is USD 0.653346 over 39 requests. That is far below USD 10, so the rule does not apply and the run continues.

## 7. Skipped families

The relaunch met the exact family that stopped the run, recorded it, and continued.

| Field | Value |
|---|---|
| Time | 2026-09-16T13:27:20Z |
| Candidate key | `10.7557/7.8033` |
| Family | `family-e3d2c9790e5499a69728` |
| Source | `src-e3d2c9790e5499a69728` |
| Broker stage | `finding_answer_extraction` |
| Committed spend | USD 0.965092 of USD 1.00 |
| Money left in the family | USD 0.034908 |
| Generation attempt | `alternative_finding` |

The `rejection_ledger` row at stage `paper_cost_cap` holds the reason code `paper_cost_cap_reached`, the broker error text, the family, the candidate key, the committed spend and the selection row.
The in-flight call record of the family is settled as `generation_incomplete` with the same reason code.
The producer stayed alive and moved to the next paper.
The progress file shows `paper_cost_cap_reached: 1`.

This is the same family, the same stage and the same money that stopped the producer at 12:37 UTC.
`skipped-families.py` in the activation directory prints this list at any time, with `--json` for a machine readable form.

## 8. Open points

1. **The budget of a paper family is cumulative over the project, not over one run.** The frozen chapter 3 corpus of 4420 papers holds every paper that the earlier chapters worked on. At 13:00 UTC the ledger held 325 families with spend: two above USD 0.95, four above USD 0.75 and 21 above USD 0.50. Each of these can reach the cap in chapter 3. The run now records and skips such a family instead of stopping. A family with no earlier spend has the whole USD 1.00.
2. **A skipped family gives no question.** The skip is not a scientific decision. It says that the family has no money left. If the run skips many families, the corpus yield decreases for a reason that is not scientific. Section 7 records the families that this run skipped. `skipped-families.py` in the activation directory lists them at any time.
3. **The cap does not move here.** A higher `maximum_paper_cost_usd` needs a registered budget transition and a captain decision. `_validate_policy` pins the value at exactly USD 1.00, so the policy file alone cannot move it.
4. **The evaluator crew shares this ledger.** Its `benchmark_evaluation` requests count in the `inflight` counter of the ledger although they take no construction slot. The launch precondition of this activation therefore reads the construction phase of this run only.
