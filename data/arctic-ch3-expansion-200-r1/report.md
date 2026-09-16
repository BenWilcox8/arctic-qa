# Chapter 3 expansion report (arctic-ch3-expansion-200-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-expansion-200-r1`, from local `main` at `183779b` (the broker fix of the evaluator crew).
Captain order (2026-09-16 10:27 UTC, verbatim): "I would like to start a $200 run of the question pipeline expanding on the current generation. I am about to go to sleep, so make sure the supervision of the run is constant."
Earlier order (2026-09-16 05:00 UTC, verbatim): "Run the chapter 3 with a budget of $20".
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-expansion-200-r1/`.
Predecessor activation: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-production-run-r1/` (report `data/arctic-ch3-production-run-r1/report.md`).

## 1. Result in ten lines

1. The producer runs as `chapter3-7dc6485-r3` on campaign `arctic-qa-production-campaign-003` in tmux session `arctic-ch3-production-r1`, PID 2643924, pane `%35`. It started at 12:17 UTC on the `9862787` runtime and covers the whole frozen corpus of 4420 papers. Section 6.
2. The shared ledger carries one new chained transition. The chapter 3 allocation becomes USD 200.00 in total, so the construction ceiling moved from USD 73.990121 to USD 253.990121. The submission count, the accepted target and the review checkpoint moved in the same change set (firstmate decision, option a). `integrity_valid` was true before and after, validated with the new code. Section 3.
3. The gate chain is `09fd733` (binds the transition), then its successor `9862787` (runs the producer). Both bind the two captain orders, the commit, the corpus manifest hashes and the transition. Section 4.
4. Four commits were necessary on top of `183779b`: the ceiling registration, a test repair of `183779b` itself, and two broker repairs. The repairs correct two faults that a concurrent evaluator exposes: a global in-flight counter that became a stale refusal receipt, and a start-time ledger snapshot check that evaluation requests invalidated. Sections 2 and 5.
5. The first relaunch (11:15 UTC, `09fd733`) stopped after three minutes on that stale receipt, without a paid call. The relaunch on `9862787` resumed the refused request at 12:20 UTC under the expansion event and replays nothing. Sections 5 and 6.
6. The first ten minutes were healthy: 24 paid requests completed, USD 0.415 spent, no halt, `integrity_valid` true, two more papers screened. USD 195.73 of the allocation remained at 12:28 UTC. Section 7.
7. Phase E at 35 papers: measure 3 (screening error share) is 0.0857, above its 0.05 target and above the 0.0455 of the first 22 papers. Measure 6 is USD 0.36 per accepted item against a USD 1.00 target. Measures 1 and 2 read 11 families. Section 8.
8. The suite is green on the deployed commit: 1276 passed in four parts, ruff clean. Section 9.
9. Firstmate monitors with `status.sh` in the activation directory (read-only, no paid call). Section 10.
10. Open: the evaluator crew's per-phase in-flight change must rebase onto this branch, and firstmate expects one more re-snapshot when that lands. Section 11.

## 2. Deviations from the fixed decisions, and why

The brief fixed one ledger transition: the construction ceiling from USD 73.990121 to USD 253.990121.
Three other exact values that `_validate_policy` pins would have ended the run before the allocation did.

| Limit | Value | Used at 10:27 UTC | Left | Run would end at about |
|---|---|---|---|---|
| `away_maximum_generation_submissions` | 5000 | 4090 (every paid request counts) | 910 | USD 16 (r3 paid USD 0.018 per request) |
| `accepted_question_target` | 500 | 32 | 468 | USD 130 (r3 paid USD 0.27 per accepted item) |
| `construction_review_checkpoint_usd` | 250.00 | 57.69 | 192.31 | USD 250 of construction spend, USD 3.99 under the ceiling |

Firstmate decided (2026-09-16 10:30 UTC, option a) that the captain's order names the allocation as the one stop.
The four fields move together in one registered change set, `CHAPTER3_EXPANSION_CHANGE`. The ceiling moves to USD 253.990121, the submission count to 20000, the accepted target to 2000 and the checkpoint to the ceiling.
The 500 accepted target and the USD 250 checkpoint were project design values, not captain limits. The captain's USD 200 order is the review.
The broker refuses each of the four fields alone and accepts the expanded counts only under the expansion ceiling.
The evaluation phase keeps its own ceiling (USD 5.00 in its own policy), untouched.

The brief fixed the release commit as the local `main` commit firstmate would name.
Firstmate named `183779b` (inbox message 002, 10:49 UTC).
The deployed commits are `183779b` plus the commits of this task, and no other change:

| Commit | Change | Reason |
|---|---|---|
| `a05c45a` | `CHAPTER3_EXPANSION_*` constants and `CHAPTER3_EXPANSION_CHANGE` in `model_broker.py`. `_validate_policy` accepts the expanded ceiling, checkpoint and counts only together. the expected tranche of the change set. three tests in `tests/test_chapter3_production_run.py`. `docs/SHARED_MODEL_BROKER.md` and `AGENTS.md`. | The broker accepts only registered transitions (section 2 of the predecessor report). |
| `09fd733` | `_is_received_max_tokens_ambiguous_case` accepts the message of the two-field usage rule beside the old one. | `tests/test_ambiguous_continuation.py::test_received_max_tokens_continuation_preserves_response_and_never_replays` failed on `183779b` itself: the two-field rule of that commit reports a response with both token counts absent as "provider usage is inconsistent", and the case pinned the older message. The shape test is unchanged. |
| `3cd57a8` | Per-phase concurrency slots (`_phase_inflight`). `execute` waits up to 90 seconds for a slot or the window before it records a transient refusal. a request that a transient refusal stopped resumes under the next reviewed transition (`RESUMABLE_NOT_SUBMITTED_REASONS`). the resume identity keeps the request fields and lets the price and policy hashes follow the active transition. six tests in `tests/test_phase_scoped_slots.py`. docs. | Section 5: the first relaunch stopped on a stale refusal. |
| `9862787` | Before its first construction request, an applied transition accepts a ledger whose only requests touched since the application belong to the evaluation phase (`_only_evaluation_activity_since`). a construction request under the previous configuration still stops the start. two more tests in `tests/test_phase_scoped_slots.py`. docs. | Section 5: the re-snapshot on `3cd57a8` was refused with "the configuration transition ledger hash changed", because the evaluator made twelve requests (11:26 to 11:29 UTC) after the expansion event and before any construction request bound it. A relaunch on `09fd733` would have been refused the same way. |

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
| `integrity_valid` | true (the status file the retired `8445117` runtime wrote at 10:19 UTC said false. the `09fd733` runtime validated the same ledger as valid at 11:13 UTC, `ledger_validation_before_transition` in the state file) |

The chapter 3 construction baseline stays USD 53.990121, the spend settled at the chapter 2 pause.
The allocation becomes USD 200.00 in total. The USD 20.00 already inside the chapter 3 ceiling is not added on top.
The new ceiling is USD 253.990121 (an increase of USD 180.00 over the previous ceiling).
The two held amounts count against the ceiling, so the spendable headroom after the transition is USD 196.185715.

The transition was applied at 2026-09-16 11:13:37 UTC by constructing the broker of the `09fd733` snapshot once.
The ledger validated with `integrity_valid` true before and after. A second construction with the same file reused the event.
The transition changed neither the ledger file (sha256 `4feb6680...` before and after), its spend nor its request count.

| Field | Value |
|---|---|
| Schema | `shared-paid-call-config-transition-v2` |
| From | price v8 `f0ec899d...`, policy v9 `22f11e01...` |
| To | price v8 (unchanged), policy v10 `0246b453...` |
| Changed fields | ceiling 73.990121 to 253.990121, submissions 5000 to 20000, accepted target 500 to 2000, checkpoint 250.00 to 253.990121 |
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
| Allocation | USD 200.000000 in total, prior allocation USD 20.000000, prior ceiling 73.990121, tranche 253.990121 |
| Papers | `maximum_ranked_papers` 4420, the whole frozen corpus |
| Streaming input | `chapter3/streaming-input/chapter3-7dc6485-r1-input`, 4420 families, frozen manifest `32af3636...`, frozen order `cc89bca5...` (both unchanged) |
| Eligibility | prompt v8 `a3f547ee...`, schema v4 `ad935edb...`, re-screen prompt v2 `b1a51e11...` (all unchanged from the `8445117` runtime, so the immutable invocation manifest of r3 accepts the continuation) |
| Contracts | unchanged from the `8445117` gate. `STANDALONE_SYSTEM` sha256 unchanged, so the v6 cassette carries over and no calibration call was paid |
| Policy | v10 `0246b453...`, prior policy v9 `22f11e01...` |
| Transitions | `ledger-config-transition-09fd733-ch3x.json` (this one), `ledger-config-transition-9f4cb18-ch3.json` and `price-config-transition-9f4cb18-ch3.json` (the chapter 3 chain) |
| Source | archive `source-09fd733-arctic-ch3-expansion-200-r1.tar` sha256 `9353f3bd...`, runtime snapshot sha256 `f861086b...` |
| Review | `implementation-review-09fd733-ch3x.md`, sha256 `7a56f3c8...` |

**Successor gate.** `live-execution-gate-9862787-ch3x.json`, sha256 `ecf65400...`, built by `build-expansion.py 9862787 resnapshot` at 12:16 UTC after the suite of section 9 passed on that commit.
It keeps every binding above except the commit, the review, the source and the runtime. It names the `09fd733` gate in `supersedes_config_transition_review`, because the expansion event binds that gate. The `9f4cb18` chain binding moves to `chapter3_ceiling_transition_review` for the record.
The `9862787` runtime validated the ledger under this gate and the applied event before the launch (`ledger_validation_after_resnapshot` in `activation-state-9862787.json`: `integrity_valid` true, not halted, active event `b489a0d3...`, ceiling 253.990121).

| Binding | Value |
|---|---|
| Commit | `9862787` (predecessor `09fd733`) |
| Source | archive `source-9862787-arctic-ch3-expansion-200-r1.tar` sha256 `ff69a952...`, runtime snapshot `app-9862787-arctic-ch3-expansion-200-r1` sha256 `9b9b98f8...` |
| Review | `implementation-review-9862787-ch3x.md` |
| Launcher | `launcher-9862787-ch3x.sh`: the `09fd733` launcher with the runtime and the gate replaced. transition file, policy v10, run id, eligibility directory and 4420 papers unchanged |
| Run id | `chapter3-7dc6485-r3`, so the immutable invocation manifest and the 33 screened papers are reused. nothing is replayed |

The gate `live-execution-gate-09fd733-ch3x.json` and its runtime stay on disk as the record of the transition and of the first relaunch.

## 5. The first relaunch and its stop

Launched at 2026-09-16 11:15:26 UTC by `build-expansion.py 09fd733 launch`: tmux session `arctic-ch3-production-r1`, pane `%34`, producer PID 1618803, launcher `launcher-09fd733-ch3x.sh`, run id `chapter3-7dc6485-r3`, 4420 papers.
The receipt `activation-receipt-09fd733-ch3x.json` recorded a fresh progress observation at 11:15:26 UTC and a health observation from 11:16:14 to 11:18:14 UTC. The producer resumed the run and re-read its 33 screened papers. At 11:18:01 UTC it exited in the generation stage with `BUDGET_EXHAUSTED: the paid-call concurrency limit is complete`.
No paid request was submitted (ledger `count_requests` 4115 before and after), the ledger stayed clean and not halted, and the tmux session closed with the producer.

**Cause.** At 2026-09-16 10:00:07 UTC, in the previous run, the producer asked for the finding answer extraction of paper `10.1038/s41467-020-20470-z` (family `family-12a16685549de1048e27`, request key `4e8625ff...`).
At that moment two Gemini requests of the abstention evaluator (run `abstention-concurrent-...`, submitted 09:59:47 and 09:59:58 UTC, completed 10:00:12 and 10:00:09 UTC) were in flight.
The broker's concurrency check read the ledger's single `inflight` counter against the construction limit of 2. It refused the request as "the paid-call concurrency limit is complete" and wrote an immutable `not_submitted` receipt with that reason. The producer exited with `BUDGET_EXHAUSTED`. The report of the predecessor task attributed the 10:01 UTC stop to the evaluator's ambiguous charge, which halted the ledger 40 seconds later.
On every resume the provider finds that receipt. Its reason is not the live-test cap, so the provider returns it as final: `BudgetError` again, at the same paper, whatever the ledger's in-flight count is.
Firstmate's inbox message 003 (11:35 UTC) named the shared in-flight counter and asked for a relaunch on `09fd733` at in-flight zero. That relaunch would have stopped at the same receipt. This is why section 2 carries a third commit.

**Repair** (`3cd57a8` and `9862787`, `tests/test_phase_scoped_slots.py`, eight tests):

1. Each phase counts its own in-flight requests (`_phase_inflight`: the ledger counter minus the evaluation phase's in-flight requests, or the evaluation phase's alone). An evaluation request never takes a construction slot. The per-minute window stays shared, because the tests reopen it through `recent_submission_times_utc` and a collision there is transient.
2. `execute` waits up to 90 seconds, in 3 second steps, when the reservation fails with the concurrency or the minute reason, and records the refusal only after that.
3. A `not_submitted` request with one of those two reasons resumes under the next reviewed transition, like a request the live-test cap stopped. The active transition must name the refused request's transition as its predecessor, and the request identity must be stable. The price and policy hashes follow the active transition. The expansion transition is the direct successor of the chapter 3 ceiling event that `4e8625ff...` ran under, so the relaunch resumes it with a `.resume-<event>` receipt and replays nothing.
4. The provider reissues such a receipt through the broker instead of raising on it.
5. An applied transition that still waits for its first construction request accepts evaluation requests made since its application. The snapshot check keeps refusing a construction request made under the previous configuration. Without this, the evaluator's twelve requests of 11:26 to 11:29 UTC blocked every construction start after the expansion event.

## 6. The relaunch

Launched at 2026-09-16 12:17:29 UTC by `build-expansion.py 9862787 launch`: tmux session `arctic-ch3-production-r1`, pane `%35`, producer PID 2643924, launcher `launcher-9862787-ch3x.sh`. The runtime is `app-9862787-arctic-ch3-expansion-200-r1`, the gate sha256 `ecf65400...`, the run id `chapter3-7dc6485-r3`, the paper bound 4420.
The receipt `activation-receipt-9862787-ch3x.json` recorded a fresh progress observation at 12:17:29 UTC: state `running`, stage `eligibility`, 33 papers screened, 5 accepted.
The producer re-read its 33 screened papers and reached paper `10.1038/s41467-020-20470-z` at 12:20 UTC.

**The resume of `4e8625ff...`.** The provider found the `not_submitted` receipt with the concurrency reason and reissued the request through the broker.
The broker accepted the resume because the active transition (the expansion event `b489a0d3...`) names the request's transition (`390ff277...`) as its predecessor. The ledger row now carries `resumed_from_not_submitted_sha256` `e04d6424...`, the sha256 of the refused receipt, which is unchanged on disk. It also carries `resumed_from_config_transition_sha256` `390ff277...`.
The request was submitted at 12:20:10 UTC as `4e8625ff...resume-b489a0d3....submitted.json` and completed. No earlier receipt was rewritten and no request was replayed.
The ledger validated with `integrity_valid` true after it.

The evaluator was paused by its crew during this relaunch (firstmate message 003). The per-phase slots of `3cd57a8` make the producer independent of it from now on.

## 7. Health observation

`build-expansion.py 9862787 observe` sampled the ledger, its status file and the progress file every 30 seconds from 12:18:19 to 12:27:50 UTC (20 samples) and wrote `first_progress_observation` into the receipt.

| Quantity | Start | End |
|---|---|---|
| Producer alive | yes | yes |
| Ledger halted | no | no |
| `integrity_valid` | true | true |
| Requests completed in the window | | 24 |
| Spent in the window | | USD 0.415403 |
| Papers screened (`eligibility_completed`) | 33 | 35 |
| Eligible | 19 | 21 |
| Accepted items (`accepted_qa`) | 5 | 5 |
| Remaining allocation (`away_session_usd`) | USD 196.185715 | USD 195.734364 |
| Verdict | | healthy |

`status.sh` at 12:28:22 UTC: 35 papers screened (43 eligibility rows), 23 benchmark candidates, 5 accepted families, one request in flight. The run had spent USD 1.809777 and the campaign USD 3.394769. Construction spend was USD 58.129802 and USD 195.710525 remained.
The accepted count did not move in ten minutes. The two new papers were in eligibility, and the generation stage was mid-paper at the end of the window.

## 8. The phase E readout

`build-expansion.py 9862787 phase-e` ran `data/arctic-ch3-production-run-r1/phase_e_measures.py` from the deployed runtime on the run so far at 12:28 UTC (35 papers).
The output is `phase-e-r3-expansion-first-window.json` beside this report, and in the activation directory as JSON and text.
The run continues, so the numbers are a snapshot and not a final value.

| Measure | Chapter 2 | r3 at 22 papers (09:50 UTC) | r3 at 35 papers (12:28 UTC) | Target | Verdict now |
|---|---|---|---|---|---|
| 1. Writer families with zero context spans | 23 of 56 (0.41) | 1 of 6 (0.17) | 2 of 11 (0.17) | under 0.10 | not met |
| 2. Writer families with no date | 42 of 56 (0.75) | 1 of 6 (0.17) | 3 of 11 (0.27) | under 0.30 | met |
| 3. Papers whose last eligibility row is an error | 38 of 200 (0.19) | 1 of 22 (0.05) | 3 of 35 (0.09) | under 0.05 | not met |
| 6. USD per accepted item | USD 3.3325 (6 items) | USD 0.1585 (5 items) | USD 0.3620 (5 items) | under USD 1.00 | met |

Measure 3 moved from 1 error in 22 papers to 3 in 35: two of the thirteen new papers ended in `screening_error`.
Measure 6 doubled because the run spent USD 1.01 more on the same 5 accepted items: eligibility (USD 0.84) and finding answer extraction (USD 0.69) are the two largest stages.
Eleven families carry no weight for measures 1 and 2 yet. Measure 3 needs the next readings. The eligibility rows of the three error papers are the input for the next eligibility slice.
The judge call plan skipped 10 judge calls after a free check failure, 4 after a standalone failure and 3 on an unavailable slot. The shadow cohort is empty.

## 9. Test results

Command: `run-suite.sh <commit>` in the activation directory: the four bounded parts the previous activation ran, in parallel, `nix develop -c bash -c 'PYTHONPATH=src pytest <files> -p no:cacheprovider -o addopts=""'`.

| Part | `a05c45a` | `09fd733` | `3cd57a8` | `9862787` |
|---|---|---|---|
| `tests/` without the three slow files | 1 failed, 1027 passed (`test_received_max_tokens_continuation_preserves_response_and_never_replays`, a failure of `183779b` itself) | 1028 passed | 1034 passed | 1036 passed |
| `tests/test_cli_integration.py` | 98 passed | 98 passed | 98 passed | 98 passed |
| `tests/test_streaming.py` | 56 passed | 56 passed | 56 passed | 56 passed |
| `tests/test_model_broker.py` | 86 passed | 86 passed | 86 passed | 86 passed |
| `ruff check`, `ruff format --check` | clean | clean | clean | clean |

The three expansion tests in `tests/test_chapter3_production_run.py` pin the constants and the change set. They replay the chain (chapter 2 price, chapter 2 ceiling, chapter 3 price, chapter 3 ceiling, chapter 3 expansion) on a fixture ledger and check that a restart reuses the event. They refuse a wrong tranche, a ceiling-only change set and a policy that moves one more field.
`_validate_policy` refuses the expanded counts under the chapter 3 ceiling and the expanded checkpoint under any other ceiling.
The eight tests in `tests/test_phase_scoped_slots.py` pin per-phase slots, the bounded wait before a transient refusal, and the refusal that outlasts the wait. They pin the resume under the next reviewed transition (broker and provider paths, restart validation, no replay). They pin the start after evaluation activity and the start that construction activity still stops.
The total on `9862787` is 1276 passed. The logs are `suite-<commit>.log` in the activation directory.

## 10. Monitoring

`status.sh` in the activation directory reads the ledger, its status file, the progress file and the state database (opened read-only, no paid call). It prints the halted flag and the integrity verdict, the construction spend and the spend since the expansion baseline, and the remaining allocation against the USD 253.990121 ceiling. It also prints the papers screened, the candidates by status, the accepted families and the campaign USD per accepted family.
`status.sh --json` prints the same as one JSON object.
`build-expansion.py <commit> observe` records a ten minute health window into the receipt. `build-expansion.py <commit> phase-e` runs the phase E script of the predecessor task on the run so far.

## 11. Deferred and open

| Item | Owner |
|---|---|
| The evaluator crew is changing the broker to count in-flight per phase (firstmate message 003). `3cd57a8` already does this. that work must rebase onto this branch, and firstmate expects one more re-snapshot when it lands (interrupt the producer at a zero-in-flight boundary, re-snapshot, relaunch the same run id). `build-expansion.py <commit> resnapshot` with `PREDECESSOR_COMMIT=9862787` and a fresh `suite-result.json` is the path. | evaluator crew, firstmate |
| A request refused for a transient reason can resume once per reviewed transition. a refusal that outlasts the 90 second wait under the current transition stays a refused record until the next transition. With per-phase slots the producer refuses itself only on its own window. | recorded, no action |
| The ledger status file's `remaining` block reads the policy of the broker that last published it. between the transition and the first request of the new runtime it still showed the USD 73.99 ceiling. `status.sh` computes the remaining allocation from the ceiling instead. | recorded, no action |
| Measure 3 is 0.0857 at 35 papers against its 0.05 target: 3 papers ended `screening_error`. | a later eligibility slice |
| Measure 1 is 2 of 11 families, above its 0.10 target. eleven families carry no weight yet. | phase E, a later reading |
| Measures 4 and 5 (reader labels) are outside this task. | phase E |
| The `09fd733` runtime and gate stay on disk as the record of the transition and the first relaunch. the `a05c45a` and `3cd57a8` snapshots were removed before any gate bound them, and their suite results are kept as `suite-result-<commit>.superseded.json`. | recorded, no action |
