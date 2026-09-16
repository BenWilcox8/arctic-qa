# Chapter 3 production run report (arctic-ch3-production-run-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-production-run-r1`, from local `main` at `7dc6485`.
Captain order (2026-09-16, verbatim): "Run the chapter 3 with a budget of $20".
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-production-run-r1/`.
Chapter 3 data root: `/mnt/crdata/research-abstention/arctic-qa/chapter3/`.
Nothing under `chapter2/` or `streaming-dataset-r1/` changed except the shared ledger, the receipts and the progress file that the run writes by design.

## 1. Result in six lines

1. The producer is NOT launched. The standalone contract v4 calibration cassette failed the release rule, and rule 4 of the brief blocks the launch. The decision is with firstmate (status `needs-decision`).
2. The shared ledger carries two new chained transitions: price config v8, then the construction ceiling of USD 73.990121. Section 3 gives every number.
3. The cassette was recorded through the same gate and ledger. Every must-pass row passed (20 of 20). Three must-fail controls also passed, all from the held-out chapter 2 slice. Section 5 names them. Spend: USD 0.238626 on 41 Pro calls.
4. Two code changes were necessary before the activation could be built. Section 2 explains them. The prepared commit is `9f4cb18`, which is `7dc6485` plus three commits of this task.
5. The gate, the launcher, the runtime snapshot, the chapter 3 root and the streaming input are built and bound. A launch after the decision needs no rebuild unless the judge prompt changes.
6. The phase E script is beside the run and was proven on the chapter 2 artifacts. It has no chapter 3 run to read yet. Section 8.

## 2. Deviations from the fixed decisions, and why

The brief fixed the deployed commit as local `main` at `7dc6485`.
The run deploys `9f4cb18` instead.
That commit is `7dc6485` plus three commits of this task, and no other change:

| Commit | Change | Reason |
|---|---|---|
| `525d86a` | `ruff format` over the nine abstention modules. Layout only. | The format check was red on `7dc6485`. The abstention harness landed without a format pass. |
| `4994432` | `CHAPTER3_BUDGET_CHANGE` and `CHAPTER3_CUMULATIVE_CEILING_USD` in `model_broker.py`. `calibration_paper_identity` in `standalone_calibration.py`. The stream-input options of `calibrate-standalone` in `cli.py`. Five tests in `tests/test_chapter3_production_run.py`. | See the two paragraphs below. |
| `9f4cb18` | `data/arctic-ch3-production-run-r1/phase_e_measures.py`. | The phase E script travels with the deployed runtime. |

**The ceiling.** The broker accepts only registered policy transitions.
`POLICY_TRANSITION_CHANGES` lists every allowed change set, and `_validate_policy` lists every allowed value of `away_session_total_ceiling_usd`.
At `7dc6485` the allowed ceilings were USD 25, USD 61.614496 and USD 108.994972.
No transition file can move the ceiling to USD 73.990121 without the constant.
Chapter 2 registered its ceiling the same way, in its integration branch.
The new constant is the one new allowed value, its tranche must equal it, and the transition binds a complete stream-input gate like the chapter 2 one.

**The calibration recording.** At `7dc6485`, `calibrate-standalone --provider broker` bound every request to paper `standalone-calibration` and to the row's own `family_id`.
The calibration rows carry the family ids of the chapter 2 candidates they came from.
The ledger binds all 29 of those families to their real papers and source versions, so the broker refuses the first request.
It also refuses the second family, because every row shares one source version.
The recording also never bound the streaming input that the production gate requires before any paid request.
The fix binds one recording to one synthetic paper family, `standalone-calibration:<set version>:<prompt hash prefix>`, with the set version and the full prompt hash as its source version.
A changed prompt or set records under a new paper.
A repeated recording of the same pair resumes its receipts by request key.
The CLI binds the gate's streaming input before the first call, with the same five options the producer uses.
`docs/STANDALONE_CALIBRATION.md` gives the new command.

The run id stays `chapter3-7dc6485-r1` as the brief fixed it.
The gate binds the deployed commit `9f4cb18` and names `7dc6485` as `deploy_base_commit`.

## 3. The ledger transitions

Ledger: `/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json`.
State before the transitions (2026-09-16 05:39 UTC):

| Quantity | Value |
|---|---|
| `spent_usd` | 54.050797 |
| Evaluation spend inside it (`evaluation_answer:gemini-3.1-pro-preview`, the abstention canary) | 0.060676 |
| Construction spend (`spent_usd` minus evaluation spend) | 53.990121 |
| `reserved_usd` (one orphaned no-replay reservation) | 0.021016 |
| `ambiguous_reserved_usd` (three chapter 2 ambiguous charges, each released by a reviewed continuation) | 0.092648 |
| `away_session_usd` used (construction spend plus the two held amounts) | 54.103785 |
| Ceiling before | 108.994972 |
| `count_requests` | 3805 |
| `integrity_valid` | true |

The construction baseline is USD 53.990121.
This is also the `spent_usd` the chapter 2 pause receipt recorded, before the canary.
The chapter 3 allocation is USD 20.00.
The new ceiling is USD 73.990121.
The two held amounts count against the ceiling, so the spendable headroom is USD 19.886336.
The retired chapter 2 headroom of USD 54.891187 is not carried.
The evaluation phase keeps its own USD 5.00 ceiling in its own policy, untouched.

Both transitions were applied at 2026-09-16 05:39 UTC by constructing the broker once each.
The ledger validated with `integrity_valid` true before, between and after.
Neither transition changed the ledger file, its spend or its request count.

| Transition | Schema | From | To | Predecessor event | New event |
|---|---|---|---|---|---|
| Price config v7 to v8 | v3 | price `3f14a663...`, policy v8 `c2b5a29e...` | price `f0ec899d...`, policy unchanged | `config-transition-8d5355a4...` (chapter 2 timeout recovery), file sha256 `4c5523e2...` | `config-transition-da1da612ac1f570328c8b8c52920f25f3db4e7f9d58fcd5f7d725d382977ca9c.json`, file sha256 `65114ee6e569...` |
| Ceiling 108.994972 to 73.990121 | v2 | price v8, policy v8 | price v8, policy v9 `22f11e01...` | the price event, file sha256 `65114ee6e569...` | `config-transition-836a6dbaeee11ee413da7bc690ec1077465ff33d1f4e5896b581240ac5bdc359.json`, file sha256 `390ff2771b3d...` |

The full event paths and hashes are in `activation-state-9f4cb18.json` beside the gate.
Price config v8 (`arctic-gemini-eligibility-r1-config-v8`) registers the `answer_agreement` stage on `gemini-3.1-pro-preview` and a 300 second timeout on `question_generation`.
Policy v9 is policy v8 with the one changed field.

## 4. The gate bindings

Gate: `live-execution-gate-9f4cb18-ch3.json`, sha256 `fd741f81...` (full value in the receipt).

| Binding | Value |
|---|---|
| Phase | `away_production` |
| Run id, campaign | `chapter3-7dc6485-r1`, `arctic-qa-production-campaign-003` |
| Streaming input | `chapter3/streaming-input/chapter3-7dc6485-r1-input`, 4420 families |
| Frozen manifest sha256 | `32af3636...` (equal to chapter 2) |
| Frozen order sha256 | `cc89bca5...` (equal to chapter 2) |
| Eligibility | prompt v8, schema v4, re-screen prompt v2, policy `protocol-v3.json` |
| Roles | `config/roles.v1.json` profile `gemini_separated` |
| Writer, judges | `gemini-3.8-flash`; `gemini-3.1-pro-preview` for every judge and the answer judge |
| Contracts | prompt v23, schema 2.8.0, standalone v4, routing v5, numeric v4, option contract v1, question-verification v2 |
| Authority | `captain-run-decision.txt`, `captain.md`, `arctic-chapter3/plan.md`, the yield audit report |
| Source | archive `source-9f4cb18-...tar` sha256 `7e882565...`, runtime snapshot sha256 `9a05f3b9...` |
| Prior gate | `live-execution-gate-0a636d3-ch2.json` |

The chapter 3 root holds two symlinks, `corpus-freeze` and `article-access`, into the chapter 2 root.
The streaming input was materialized through them with `materialize_stream_input`, so its manifest records the chapter 2 access run by its real path.
The eligibility run directory is `chapter3/gemini-eligibility/chapter3-7dc6485-r1`.

## 5. The calibration

Command: `calibrate-standalone --mode record --provider broker --phase away_production --run-id chapter3-7dc6485-r1 --campaign-id arctic-qa-production-campaign-003`, with the gate, the policy v9, the ledger transition and the five stream-input options, from the runtime snapshot.
Recorded 2026-09-16 05:39:48 to 05:48:49 UTC.

| Quantity | Value |
|---|---|
| Calls | 41 (every gating row), all `completed` |
| Stage, model | `standalone_verification`, `gemini-3.1-pro-preview` |
| Paper family in the ledger | `standalone-calibration:standalone-calibration-v2:2a7b6da0146b` |
| Spend | USD 0.238626 (the integration report estimated USD 0.31) |
| Ledger after | spent 54.289423, inflight 0, not halted, `integrity_valid` true, headroom 19.647710 |
| Cassette | `standalone-calibration-v2.cassette.jsonl` (copied into this directory), sha256 `0d4a0eff...` |
| Report | `calibration-report-9f4cb18-ch3.json` (copied into this directory) |
| Must-pass | 20 of 20 passed (14 core, 6 held-out) |
| Must-fail | 18 of 21 failed as required (12 core, 6 held-out); 3 held-out controls passed |
| Release rule | not met (`passed: false`, exit code 1) |

The three controls that the judge passed, with the labelers' reasons:

| Row | Question | Both labelers | Judge |
|---|---|---|---|
| `aqa-69212ceec983481ba3b4` | "During the aeromycological analyses across the entire sample collection period in May 2019, what airborne fungal concentration value was obtained at sampling location number four, located in the 'Huset' region in the middle of the Longyeardalen valley?" | `undefined_measured_variable`: the unit or metric (CFU per cubic meter) is never named | pass, "all referents are well-defined" |
| `aqa-75eec503337a30ec9192` | "What was the median relative abundance of the bacterial genus Corynebacterium in samples from Resolute Bay?" (empty context) | `undefined_population_or_sample`: the sample type is not stated | pass, "all referents are defined" |
| `aqa-ada85a8246f0e1d0a319` | "Which ports are listed in association with the taxon Prochromadora sp. 3?" (empty context) | `undefined_location`, `source_dependent_locator`: "listed" points to an unseen table | pass, "the study identity is not a necessary detail" |

The deterministic half of the composed decision did not fire on the three rows either.
Every core row (the r15 rows that shaped the prompt) is judged correctly.
Every miss is a held-out chapter 2 row, which is the slice that measures a prompt revision after it is written.
The judge is under-strict on three of nine held-out controls, and under-strict is the direction that admits paper-dependent items.
In chapter 2 these three candidates were killed by other gates (the scope gate, the reconstructor and the deterministic rules), so the run has more than one gate on them.
The release rule of the calibration set does not allow a launch on that argument, and the brief does not either.

## 6. The launch

Not performed.
No tmux session `arctic-ch3-production-r1` exists, no producer process runs, and no request of the run other than the 41 calibration calls is in the ledger.
The launcher `launcher-9f4cb18-ch3.sh` is ready and names the runtime, the gate, the ledger transition, the policy v9 and every input of section 4.
`build-activation.py 9f4cb18 launch` refuses to start while the calibration record says `passed: false`.

## 7. The eligibility watch

Not reached.
`build-activation.py 9f4cb18 watch-eligibility` implements rule 5: after the producer passes paper 20, it reads the last row of each of the first 20 papers in frozen order.
If more than 5 are `screening_error` or `unresolved_rescreenable`, it waits for zero in-flight requests, sends SIGINT and records the interrupt.

## 8. The first phase E readout

`phase_e_measures.py` is in this directory and in the deployed runtime.
On the chapter 2 artifacts it reproduces the yield audit's values: 38 of 200 screening errors, USD 19.995149 over 6 items, USD 3.3325 per item, and the stage costs of the audit cost summary.
On the chapter 3 artifacts it has nothing to read yet, because the run did not start.
The command for the run is:

```
PYTHONPATH=src python data/arctic-ch3-production-run-r1/phase_e_measures.py \
  --ledger /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --state-db /mnt/crdata/research-abstention/arctic-qa/state.sqlite3 \
  --eligibility-run-dir /mnt/crdata/research-abstention/arctic-qa/chapter3/gemini-eligibility/chapter3-7dc6485-r1 \
  --stream-input-dir /mnt/crdata/research-abstention/arctic-qa/chapter3/streaming-input/chapter3-7dc6485-r1-input \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v8.txt \
  --rescreen-prompt-file config/gemini-eligibility-geography-rescreen-v2.txt
```

## 9. Test results

Command: `nix develop -c bash -c 'PYTHONPATH=src pytest <files> -p no:cacheprovider -o addopts=""'` on `9f4cb18`, four bounded foreground parts.

| Part | Result |
|---|---|
| `tests/` without the three slow files | 984 passed |
| `tests/test_cli_integration.py` | 98 passed |
| `tests/test_streaming.py` | 56 passed |
| `tests/test_model_broker.py` | 86 passed |
| `ruff check src tests` | clean |
| `ruff format --check src tests` | clean |

The five new tests in `tests/test_chapter3_production_run.py` are in the first part.
They pin the ceiling constant, replay the chain (chapter 2 price, chapter 2 ceiling, chapter 3 price, chapter 3 ceiling) on a fixture ledger, refuse a wrong tranche, record a cassette through a bound broker with a fake transport, and refuse a recording without the gate's streaming input.

## 10. Deferred and open

| Item | Owner |
|---|---|
| The launch decision after the failed calibration: hold and revise the judge prompt (a new `STANDALONE_SYSTEM`, a new cassette at about USD 0.24, then launch from a re-snapshotted commit), or launch on the recorded judge with the three held-out misses on record. Relabeling the three rows is not an option this report proposes: both labelers agree and the rule would relax a gate. | firstmate and captain |
| The exporter rebind to the chapter 3 runtime (step 7 of the pattern) waits for the launch, so the dataset page keeps showing the chapter 2 snapshot until then. | this task, after the decision |
| Measures 4 and 5 (reader labels) are outside this task. | phase E |
| The `standalone-calibration` paper family in the shared ledger is bound to set v2 and prompt hash `2a7b6da0146b`; a re-recording under a new prompt binds a new family. | whoever revises the prompt |
