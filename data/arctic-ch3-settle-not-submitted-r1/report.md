# Chapter 3 settlement-skip report (arctic-ch3-settle-not-submitted-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-settle-not-submitted-r1`, from local `main` at `92ee7e0`.
Captain standing order (2026-09-16): the chapter 3 production run must run unattended overnight with constant supervision.
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-settle-not-submitted-r1/`.
Predecessor activation: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-paper-cap-skip-r1/` (report `data/arctic-ch3-paper-cap-skip-r1/report.md`).

## 1. Result in eight lines

1. A settlement of a paid request never ends the run again. Commit `14783d7` on branch `fm/arctic-ch3-settle-not-submitted-r1`. Sections 2 and 3.
2. The fault was not the per-paper cap. It was a double settlement between the producer's orphan recovery and a concurrent evaluation worker of the same shared ledger. Section 2.
3. The capped family keeps its own final path. The broker replays the stored refusal, never resumes it and never settles it. Section 3.
4. The producer runs again as `chapter3-7dc6485-r3` on campaign `arctic-qa-production-campaign-003`, PID 1124212, pane `%38`, tmux session `arctic-ch3-production-r1`, over the whole frozen corpus of 4420 papers. Section 6.
5. The fifteen-minute observation is healthy: 45 paid requests, USD 0.682694, 33 accepted, no halt, `integrity_valid` true. The cap-refused receipt `107a46f1` is unchanged at both ends of the window. Section 6.
6. No new money and no new ledger transition. The applied USD 200 expansion transition stands, and the successor gate names the gate that authorized it. Section 5.
7. Three tests hold the repair. One of them reproduces the exact 13:53 UTC exit from the recorded run. Section 4.
8. The suite is green on the deployed commit: 1365 passed in four parts, ruff clean. `suite-result.json` in the activation directory.

## 2. The fault

At 13:53:59 UTC on 2026-09-16 the producer of run `chapter3-7dc6485-r3` stopped with this line:

```json
{"code":"VALUEERROR","message":"the paid request is not submitted","status":"error"}
```

The launcher log is `data/arctic-ch3-paper-cap-skip-r1/launcher-9c87fc4-ch3cap.log` in the activation directory.
The deployed snapshot was `app-9c87fc4-arctic-ch3-paper-cap-skip-r1`, commit `9c87fc4`.

The run record names the call that stopped the producer.
The `calls` table of `/mnt/crdata/research-abstention/arctic-qa/state.sqlite3` holds this row:

| Field | Value |
|---|---|
| `call_id` | `call-03a79b021185f1610938` |
| `entity_id` | `eligibility-e1917f5b035d6c6b9f5c` |
| `role` | `eligibility` |
| `started_at` | 2026-09-16T13:53:50Z |
| `completed_at` | 2026-09-16T13:53:59Z |
| `status` | `failed` |
| `error_code` | `BROKER_RESPONSE_INVALID` |
| `error_text` | `the paid request is not submitted` |

The failed call is a new eligibility call of a new paper.
It is not the request `107a46f1`, which the per-paper cost cap refused at 12:37 UTC.
That capped family was already recorded and skipped at 13:27:20 UTC, in the `rejection_ledger` row `rejection-dd2a934e6a5b9c05f46e` at stage `paper_cost_cap`.
The cap skip worked.

The failed call never reached the provider.
It wrote no receipt, and it left no request row of its own in the shared ledger.
It stopped inside `SharedGeminiBroker._recover_orphans`, which every paid call runs before it starts.

The chain was this:

1. `execute` calls `_recover_orphans` first. The recovery reads the whole ledger one time under the ledger lock, then releases the lock and settles each interrupted request it found.
2. A construction run recovers requests of every run (`own_run_only` is false), because a stopped producer of another run can leave a paid liability.
3. The abstention evaluation run `abstention-stream-r6-aqa-7f09e4bdf6bac5c50d4c` was live on the same shared ledger. An evaluation request takes no exclusive operation lock: it holds only its own in-flight lock during its provider call.
4. The evaluation request `230291e74a` was submitted at 13:53:39 UTC. The producer took its ledger snapshot at 13:53:50 UTC, and the snapshot held that request as `submitted`.
5. The evaluation worker wrote its final receipt at 13:53:52 UTC, settled the request in the ledger at 13:53:57 UTC, and released its in-flight lock.
6. The producer then reached that request in its loop. The in-flight lock was gone and the final receipt was on disk, so the recovery read the receipt and called `_settle`.
7. `_settle` found the row in state `completed`, not `submitted`, and raised `ValueError("the paid request is not submitted")`. The producer exited.

The window is the time between the snapshot and the settlement.
The recovery of a ledger with 4250 requests takes seconds, because it reads a receipt path for every terminal request.
The evaluation run makes a paid call every few seconds.
Therefore the window is hit often, and the producer stops again at any time while both runs share the ledger.

Nothing was lost and nothing was charged twice.
The evaluation worker did the whole settlement of its own request.
The producer only tried to do it a second time.

## 3. The fix

One rule: **a settlement that finds nothing to settle records the skip and returns.**

| File | Change |
|---|---|
| `src/arctic_qa/model_broker.py` | `_settle` returns a boolean instead of raising. A row that is not `submitted` holds no reservation to release, so the settlement writes `<request_key>.settle-skipped.json` and returns `False`. A request key the ledger never held takes the same path. |
| `src/arctic_qa/model_broker.py` | `_recover_orphans` reads the request row again under the ledger lock before it writes any receipt, and skips a row that left `submitted`. A final receipt another worker wrote first is reported the same way, through `_write_recovered_receipt`. |
| `src/arctic_qa/model_broker.py` | `_apply_settlement` holds the money arithmetic of a submitted request. `_settle` is now the state decision plus that call. |
| `src/arctic_qa/model_broker.py` | `execute` replays a stored per-paper cap refusal before it paces, counts or resumes. `_resume_not_submitted` refuses that receipt under every transition. |

The skip note has the schema `shared-paid-call-settle-skipped-v1`.
It holds the request key, the observed state, the reason and the time.
The note is an observation beside the receipts, never an accounting event.
The money of the request stays in the ledger row that the other worker wrote.

The per-paper cap keeps its own final path, which section 4 pins with a test.
The broker replays the stored refusal, so the producer records the capped family and continues with the next paper.
The refusal is never resumed and never settled.

What the fix does not change:

- The cap stays at USD 1.00 (`maximum_paper_cost_usd`), and nothing is charged past it.
- No reservation is released twice. A settlement moves money only for a row that is `submitted` at that moment, under the ledger lock.
- The integrity of the ledger still stops the run. `_validated_ledger` runs on every read, and a real accounting fault still halts through it.
- Only a whole-run stop ends the producer: the allocation ceiling, the session ceiling or a halt.

## 4. Tests

| Test | What it holds |
|---|---|
| `tests/test_model_broker.py::test_a_request_another_worker_settles_during_recovery_does_not_end_the_run` | The exact 13:53 UTC exit. One request is left submitted with a durable response, as a crash leaves it. A second broker of the same ledger settles it exactly when the recovery probes its in-flight lock. The producer's own call completes, the skip note is on disk, and the money of the request is settled one time. |
| `tests/test_model_broker.py::test_settlement_of_a_never_submitted_request_records_and_continues` | A settlement of a completed row returns `False`, moves no money and writes the note. A request key the ledger never held does the same. |
| `tests/test_broker_provider.py::test_a_capped_family_refusal_is_never_resumed_and_never_settled` | `execute` replays the stored cap refusal with no provider call and no ledger movement. `_resume_not_submitted` refuses it. A settlement of it returns `False`. |

The first test fails on the unfixed code with the production error:

```
ValueError: the paid request is not submitted
src/arctic_qa/model_broker.py:6173
```

The two cap-skip tests of the predecessor stay green:
`test_paper_cost_cap_refusal_names_the_family_and_charges_nothing_past_it` and `test_a_capped_family_is_not_retried_on_relaunch`.
The transient-resume tests of `tests/test_phase_scoped_slots.py` stay green.

The whole suite on the deployed commit `14783d7`, in four bounded parts run in parallel:

| Part | Result |
|---|---|
| `tests/` without the three slow files | 1118 passed in 372s |
| `tests/test_cli_integration.py` | 98 passed in 136s |
| `tests/test_streaming.py` | 61 passed in 511s |
| `tests/test_model_broker.py` | 88 passed in 156s |
| `ruff check` and `ruff format --check` on `src` and `tests` | clean |

Total: 1365 passed.
The record is `suite-result.json` and `suite-14783d7.log` in the activation directory.

## 5. The release

No new money and no new ledger transition.
The applied USD 200 expansion transition of `09fd733` stands.
The successor gate names the gate that authorized it.

| Artifact | Path |
|---|---|
| Source archive (immutable, mode 444) | `source-14783d7-arctic-ch3-settle-not-submitted-r1.tar`, SHA-256 `cd67de40b7e40e43ba93fa05cef964c0084a6b47756eb3ba1972313ae6ed959d` |
| Runtime snapshot | `runtime/app-14783d7-arctic-ch3-settle-not-submitted-r1`, tree SHA-256 `44258bfc3414cc167395b50bcad0f22fe9253fe410aad24ed4799072d5e6ea0b` |
| Implementation review | `implementation-review-14783d7-ch3settle.md`, SHA-256 `d72eb54b0941a2521cf1f9aa9405fa9e92caa6ec9ede4424dbba9d622421dad9` |
| Execution gate (immutable, mode 444) | `live-execution-gate-14783d7-ch3settle.json`, SHA-256 `d22ae01700d5924c9156f6425b0a7eb6748a5005b740322bdb7765f60e50f50d` |
| Launcher (mode 555) | `launcher-14783d7-ch3settle.sh`, SHA-256 `657cc30897020a70497e72223f4fb753a7b10256c26fd68b406d906ebcf8e88e` |
| Activation receipt | `activation-receipt-14783d7-ch3settle.json` |
| Activation state | `activation-state-14783d7.json` |
| Build script | `build-settle-not-submitted.py`, with the modes `resnapshot`, `launch`, `observe` and `phase-e` |
| Read-only monitors | `status.sh`, `skipped-families.py` and `settle-skipped.py` |

The predecessor of this activation is the cap-skip activation `9c87fc4` (`arctic-ch3-paper-cap-skip-r1`), gate SHA-256 `bede18b5faaf2443b07109aeb1c71d30202d6e5917736782e5dae742f08feb3f`.
The gate is that predecessor gate with these fields moved: the commit, the review record and its hash, the source archive hash, the runtime snapshot hash and path, and the prior gate binding.
`supersedes_config_transition_review` is inherited unchanged and still names the `09fd733` gate that authorized the expansion event, so that immutable event stays valid under the active gate.

The broker of the new runtime validated the shared ledger under policy v10, the new gate and the applied transition.
The result: `integrity_valid` true, `halted` false, `status_state` `valid`, `config_transition_sha256` `b489a0d3...` equal to the applied expansion event, and `away_session_total_ceiling_usd` USD 253.990121.
This validation makes no paid call.

The producer of the previous launch already stopped at 13:53:59 UTC at a zero-in-flight boundary for this run.
An interrupt was therefore not necessary.
The tmux session `arctic-ch3-production-r1` was absent and no process held the run id.
The ledger held no unsettled construction request of `chapter3-7dc6485-r3`.

The abstention evaluation run was live on the same shared ledger through the whole release.
Its `benchmark_evaluation` requests take no construction slot, so the launch precondition of the predecessor, which reads the construction requests of this run only, held without a change.
I changed two steps of the inherited procedure:

1. The observation window is fifteen minutes, not ten.
2. The observation reads the state of the cap-refused request `107a46f1` at its start and at its end, and lists every settlement note. `stale_receipt_state` in `build-settle-not-submitted.py` does that.

## 6. The relaunch and the health observation

The producer started at 14:32:05 UTC on the `14783d7` runtime, in tmux session `arctic-ch3-production-r1`, pane `%38`, PID 1124212. It covers the whole frozen corpus of 4420 papers.

The abstention evaluation run was live on the same shared ledger through the whole window. That is the condition the fault needs.

The fifteen-minute observation, taken every 30 seconds from the ledger, the ledger status file and the progress file:

| Measure | Value |
|---|---|
| Window | 14:32:53 to 14:47:25 UTC, 902 seconds, 30 samples |
| Producer alive at the end | yes |
| Halted at the end | no |
| `integrity_valid` at the end | true |
| Paid requests completed in the window | 45 |
| Spend in the window | USD 0.682694 |
| Accepted questions, start to end | 33 to 33 |
| Papers screened, start to end | 66 to 72 |
| Eligible, start to end | 38 to 41 |
| Families the cap skipped in the window | 1 (the replay of the stored refusal, free) |
| Allocation left at the end | USD 193.827487 |
| Verdict | healthy |

The state of the cap-refused request `107a46f1` at the start and at the end of the window:

| Field | At the start | At the end |
|---|---|---|
| Ledger state | `not_submitted` | `not_submitted` |
| Ledger reason | the paid request exceeds the paper cost limit | the same |
| Receipt state | `not_submitted` | `not_submitted` |
| Receipt SHA-256 | `cfe1cf8517e4494ece564ba618fdbbdcdb9b2253f8af6e66abf3ddbd746a9265` | the same |
| `live_call_made` | false | false |
| Resumed | no | no |
| Settlement note beside it | none | none |

The receipt did not move. It was not resumed and it was not settled.
The producer replayed the stored refusal free, recorded the family under `paper_cost_cap_reached` and continued, which the progress counts show.

No settlement of the window skipped anything: `settle_skipped_notes` is empty.
The race did not fire inside these fifteen minutes.
The repair is proven by the unit test of section 4, which reproduces the exact interleaving; the window shows that the run is healthy while both workers share the ledger.

The captain's rule for a dead streak is USD 10 of construction spend with no machine accepted question.
The last acceptance of this campaign was `aqa-199da6fd46bc6e3d7cd2` at 12:29:27 UTC.
Construction spend since then is USD 1.856820 over 91 requests.
That is far below USD 10, so the rule does not apply and the run continues.

## 7. Open points

1. **The evaluator crew shares this ledger and still runs the old settlement.** Its snapshot is `app-c545cf8-arctic-ch3-production-run-r1`, commit `c545cf8`, which raises on a settlement that finds its row already moved. Its own recovery reads its own run only (`own_run_only` true), so it is far less exposed than the producer was. The exposure that is left is narrow: two processes recovering the same orphan at the same time, each with a stale snapshot. The evaluator crew should re-snapshot onto this commit or later at its next release.
2. **The window is a property of the shared ledger, not of one run.** Any second worker that settles its own request on this ledger opens it. The repair is therefore in the settlement itself, not in one caller.
3. **A skip note is an observation, never money.** `settle-skipped.py` in the activation directory lists the notes at any time. A note beside a request whose ledger row is not terminal would be a real fault; nothing in this run shows one.
4. **The per-paper cap and its skip do not move here.** The cap stays at USD 1.00. `_validate_policy` pins that value, so the policy file alone cannot move it.
