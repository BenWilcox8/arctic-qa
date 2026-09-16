# Chapter 3 expansion report (arctic-ch3-expansion-200-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-expansion-200-r1`, from local `main` at `183779b` (the broker fix of the evaluator crew).
Captain order (2026-09-16 10:27 UTC, verbatim): "I would like to start a $200 run of the question pipeline expanding on the current generation. I am about to go to sleep, so make sure the supervision of the run is constant."
Earlier order (2026-09-16 05:00 UTC, verbatim): "Run the chapter 3 with a budget of $20".
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-expansion-200-r1/`.
Predecessor activation: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-production-run-r1/` (report `data/arctic-ch3-production-run-r1/report.md`).

## 1. Result in ten lines

PENDING_RESULT

## 2. Deviations from the fixed decisions, and why

The brief fixed one ledger transition: the construction ceiling from USD 73.990121 to USD 253.990121.
Three other exact values that `_validate_policy` pins would have ended the run before the allocation did.

| Limit | Value | Used at 10:27 UTC | Left | Run would end at about |
|---|---|---|---|---|
| `away_maximum_generation_submissions` | 5000 | 4090 (every paid request counts) | 910 | USD 16 (r3 paid USD 0.018 per request) |
| `accepted_question_target` | 500 | 32 | 468 | USD 130 (r3 paid USD 0.27 per accepted item) |
| `construction_review_checkpoint_usd` | 250.00 | 57.69 | 192.31 | USD 250 of construction spend, USD 3.99 under the ceiling |

Firstmate decided (2026-09-16 10:30 UTC, option a) that the captain's order names the allocation as the one stop.
The four fields move together in one registered change set, `CHAPTER3_EXPANSION_CHANGE`: the ceiling to USD 253.990121, the submission count to 20000, the accepted target to 2000 and the checkpoint to the ceiling.
The 500 accepted target and the USD 250 checkpoint were project design values, not captain limits; the captain's USD 200 order is the review.
The broker refuses each of the four fields alone and accepts the expanded counts only under the expansion ceiling.
The evaluation phase keeps its own ceiling (USD 5.00 in its own policy), untouched.

The brief fixed the release commit as the local `main` commit firstmate would name.
Firstmate named `183779b` (inbox message 002, 10:49 UTC).
The deployed commits are `183779b` plus the commits of this task, and no other change:

| Commit | Change | Reason |
|---|---|---|
| `a05c45a` | `CHAPTER3_EXPANSION_*` constants and `CHAPTER3_EXPANSION_CHANGE` in `model_broker.py`; `_validate_policy` accepts the expanded ceiling, checkpoint and counts only together; the expected tranche of the change set; three tests in `tests/test_chapter3_production_run.py`; `docs/SHARED_MODEL_BROKER.md` and `AGENTS.md`. | The broker accepts only registered transitions (section 2 of the predecessor report). |
| `09fd733` | `_is_received_max_tokens_ambiguous_case` accepts the message of the two-field usage rule beside the old one. | `tests/test_ambiguous_continuation.py::test_received_max_tokens_continuation_preserves_response_and_never_replays` failed on `183779b` itself: the two-field rule of that commit reports a response with both token counts absent as "provider usage is inconsistent", and the case pinned the older message. The shape test is unchanged. |
| PENDING_FIX_COMMIT | Per-phase concurrency slots (`_phase_inflight`); `execute` waits up to 90 seconds for a slot or the window before it records a transient refusal; a request that a transient refusal stopped resumes under the next reviewed transition (`RESUMABLE_NOT_SUBMITTED_REASONS`); the resume identity keeps the request fields and lets the price and policy hashes follow the active transition; `tests/test_phase_scoped_slots.py` (6 tests); docs. | Section 6: the first relaunch stopped on a stale refusal. |

## 3. The ledger transition

Ledger: `/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json`.
State before the transition (2026-09-16 11:13 UTC), read by the new snapshot's own broker:

| Quantity | Value |
|---|---|
| `spent_usd` | 57.844106 |
| Evaluation spend inside it (three `evaluation_answer:*` stages) | 0.153364 |
| Construction spend | 57.690742 |
| `reserved_usd` (one orphaned no-replay reservation) | 0.021016 |
| `ambiguous_reserved_usd` (three chapter 2 ambiguous charges, each with a reviewed continuation) | 0.092648 |
| `away_session_usd` used | 57.804406 |
| Ceiling before | 73.990121 |
| `count_requests` | 4115 |
| `integrity_valid` | true (the status file the retired `8445117` runtime wrote at 10:19 UTC said false; the `09fd733` runtime validated the same ledger as valid at 11:13 UTC, `ledger_validation_before_transition` in the state file) |

The chapter 3 construction baseline stays USD 53.990121, the spend settled at the chapter 2 pause.
The allocation becomes USD 200.00 in total; the USD 20.00 already inside the chapter 3 ceiling is not added on top.
The new ceiling is USD 253.990121 (an increase of USD 180.00 over the previous ceiling).
The two held amounts count against the ceiling, so the spendable headroom after the transition is USD 196.185715.

The transition was applied at 2026-09-16 11:13:37 UTC by constructing the broker of the `09fd733` snapshot once.
The ledger validated with `integrity_valid` true before and after; a second construction with the same file reused the event.
The transition changed neither the ledger file (sha256 `4feb6680...` before and after), its spend nor its request count.

| Field | Value |
|---|---|
| Schema | `shared-paid-call-config-transition-v2` |
| From | price v8 `f0ec899d...`, policy v9 `22f11e01...` |
| To | price v8 (unchanged), policy v10 `0246b453...` |
| Changed fields | ceiling 73.990121 to 253.990121; submissions 5000 to 20000; accepted target 500 to 2000; checkpoint 250.00 to 253.990121 |
| Tranche | 253.990121 |
| Predecessor event | `config-transition-836a6dba...json` (the chapter 3 ceiling event), sha256 `390ff277...` |
| Authorization file | `ledger-config-transition-09fd733-ch3x.json`, sha256 `c0e14897...` |
| New event | `config-transition-0233039f...json`, sha256 `b489a0d3...` |
| Limits after | ceiling 253.990121, checkpoint 253.990121, submissions 20000, accepted target 2000 |
| Remaining after | away session USD 196.185715, checkpoint USD 196.185715, submissions 15910, accepted 1968 |

Policy v10 is policy v9 with the four changed fields: `streaming-dataset-budget-policy-v10-chapter3-expansion.json`.

## 4. The gate bindings

Gate: `live-execution-gate-09fd733-ch3x.json`, sha256 `3caf77d2...`.
It is a successor of the `8445117` gate (sha256 `318c7173...`) and names the `9f4cb18` gate in `supersedes_config_transition_review`, so the two earlier events stayed valid while they were active.
The expansion event binds this gate directly.

| Binding | Value |
|---|---|
| Phase | `away_production` |
| Run id, campaign | `chapter3-7dc6485-r3` (unchanged), `arctic-qa-production-campaign-003` |
| Predecessor invocation run id | `chapter3-7dc6485-r3` (the run continues) |
| Authorization | `captain-run-decision-200.txt` (sha256 `97329011...`) and, as `prior_authorization_record`, `captain-run-decision.txt` (sha256 `b694742a...`) |
| Allocation | USD 200.000000 in total; prior allocation USD 20.000000; prior ceiling 73.990121; tranche 253.990121 |
| Papers | `maximum_ranked_papers` 4420, the whole frozen corpus |
| Streaming input | `chapter3/streaming-input/chapter3-7dc6485-r1-input`, 4420 families, frozen manifest `32af3636...`, frozen order `cc89bca5...` (both unchanged) |
| Eligibility | prompt v8 `a3f547ee...`, schema v4 `ad935edb...`, re-screen prompt v2 `b1a51e11...` (all unchanged from the `8445117` runtime, so the immutable invocation manifest of r3 accepts the continuation) |
| Contracts | unchanged from the `8445117` gate; `STANDALONE_SYSTEM` sha256 unchanged, so the v6 cassette carries over and no calibration call was paid |
| Policy | v10 `0246b453...`; prior policy v9 `22f11e01...` |
| Transitions | `ledger-config-transition-09fd733-ch3x.json` (this one), `ledger-config-transition-9f4cb18-ch3.json` and `price-config-transition-9f4cb18-ch3.json` (the chapter 3 chain) |
| Source | archive `source-09fd733-arctic-ch3-expansion-200-r1.tar` sha256 `9353f3bd...`, runtime snapshot sha256 `f861086b...` |
| Review | `implementation-review-09fd733-ch3x.md`, sha256 `7a56f3c8...` |

PENDING_SUCCESSOR_GATE

## 5. The first relaunch and its stop

Launched at 2026-09-16 11:15:26 UTC by `build-expansion.py 09fd733 launch`: tmux session `arctic-ch3-production-r1`, pane `%34`, producer PID 1618803, launcher `launcher-09fd733-ch3x.sh`, run id `chapter3-7dc6485-r3`, 4420 papers.
The receipt `activation-receipt-09fd733-ch3x.json` recorded a fresh progress observation at 11:15:26 UTC and a health observation from 11:16:14 to 11:18:14 UTC: the producer resumed the run, re-read its 33 screened papers, and exited at 11:18:01 UTC in the generation stage with `BUDGET_EXHAUSTED: the paid-call concurrency limit is complete`.
No paid request was submitted (ledger `count_requests` 4115 before and after), the ledger stayed clean and not halted, and the tmux session closed with the producer.

**Cause.** At 2026-09-16 10:00:07 UTC, in the previous run, the producer asked for the finding answer extraction of paper `10.1038/s41467-020-20470-z` (family `family-12a16685549de1048e27`, request key `4e8625ff...`).
At that moment two Gemini requests of the abstention evaluator (run `abstention-concurrent-...`, submitted 09:59:47 and 09:59:58 UTC, completed 10:00:12 and 10:00:09 UTC) were in flight.
The broker's concurrency check read the ledger's single `inflight` counter against the construction limit of 2, refused the request as "the paid-call concurrency limit is complete", wrote an immutable `not_submitted` receipt with that reason, and the producer exited with `BUDGET_EXHAUSTED` (the report of the predecessor task attributed the 10:01 UTC stop to the evaluator's ambiguous charge, which halted the ledger 40 seconds later).
On every resume the provider finds that receipt, and since its reason is not the live-test cap, returns it as final: `BudgetError` again, at the same paper, whatever the ledger's in-flight count is.
Firstmate's inbox message 003 (11:35 UTC) named the shared in-flight counter and asked for a relaunch on `09fd733` at in-flight zero; that relaunch would have stopped at the same receipt, which is why section 2 carries a third commit.

**Repair** (PENDING_FIX_COMMIT, `tests/test_phase_scoped_slots.py`):

1. Each phase counts its own in-flight requests (`_phase_inflight`: the ledger counter minus the evaluation phase's in-flight requests, or the evaluation phase's alone). An evaluation request never takes a construction slot. The per-minute window stays shared, because the tests reopen it through `recent_submission_times_utc` and a collision there is transient.
2. `execute` waits up to 90 seconds, in 3 second steps, when the reservation fails with the concurrency or the minute reason, and records the refusal only after that.
3. A `not_submitted` request with one of those two reasons resumes under the next reviewed transition, like a request the live-test cap stopped: the active transition must name the refused request's transition as its predecessor, the request identity must be stable, and the price and policy hashes follow the active transition. The expansion transition is the direct successor of the chapter 3 ceiling event that `4e8625ff...` ran under, so the relaunch resumes it with a `.resume-<event>` receipt and replays nothing.
4. The provider reissues such a receipt through the broker instead of raising on it.

## 6. The relaunch

PENDING_RELAUNCH

## 7. Health observation

PENDING_HEALTH

## 8. The phase E readout

PENDING_PHASE_E

## 9. Test results

Command: `run-suite.sh <commit>` in the activation directory: the four bounded parts the previous activation ran, in parallel, `nix develop -c bash -c 'PYTHONPATH=src pytest <files> -p no:cacheprovider -o addopts=""'`.

| Part | `a05c45a` | `09fd733` | PENDING_FIX_COMMIT |
|---|---|---|---|
| `tests/` without the three slow files | 1 failed, 1027 passed (`test_received_max_tokens_continuation_preserves_response_and_never_replays`, a failure of `183779b` itself) | 1028 passed | PENDING |
| `tests/test_cli_integration.py` | 98 passed | 98 passed | PENDING |
| `tests/test_streaming.py` | 56 passed | 56 passed | PENDING |
| `tests/test_model_broker.py` | 86 passed | 86 passed | PENDING |
| `ruff check`, `ruff format --check` | clean | clean | PENDING |

The three expansion tests in `tests/test_chapter3_production_run.py` pin the constants and the change set, replay the chain (chapter 2 price, chapter 2 ceiling, chapter 3 price, chapter 3 ceiling, chapter 3 expansion) on a fixture ledger, check that a restart reuses the event, and refuse a wrong tranche, a ceiling-only change set and a policy that moves one more field.
`_validate_policy` refuses the expanded counts under the chapter 3 ceiling and the expanded checkpoint under any other ceiling.

## 10. Monitoring

`status.sh` in the activation directory prints, from the ledger, its status file, the progress file and the state database (opened read-only, no paid call): the halted flag and integrity verdict, construction spend and spend since the expansion baseline, the remaining allocation against the USD 253.990121 ceiling, papers screened, candidates by status, accepted families and USD per accepted item.
`status.sh --json` prints the same as one JSON object.
`build-expansion.py <commit> observe` records a ten minute health window into the receipt; `build-expansion.py <commit> phase-e` runs the phase E script of the predecessor task on the run so far.

## 11. Deferred and open

PENDING_OPEN
