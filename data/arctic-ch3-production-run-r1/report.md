# Chapter 3 production run report (arctic-ch3-production-run-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-production-run-r1`, from local `main` at `7dc6485`.
Captain order (2026-09-16, verbatim): "Run the chapter 3 with a budget of $20".
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-production-run-r1/`.
Chapter 3 data root: `/mnt/crdata/research-abstention/arctic-qa/chapter3/`.
Nothing under `chapter2/` or `streaming-dataset-r1/` changed except the shared ledger, the receipts and the progress file that the run writes by design.

## 1. Result in six lines

1. The producer runs as `chapter3-7dc6485-r2` on campaign `arctic-qa-production-campaign-003`, in tmux session `arctic-ch3-production-r1`, PID 2229516, since 08:09:09 UTC, on the `c545cf8` runtime. Sections 6, 6b and 6c give every launch, stop and relaunch.
2. The shared ledger carries two chained transitions: price config v8, then the construction ceiling of USD 73.990121 (USD 53.990121 of construction spend at the chapter 2 pause plus USD 20.00). Section 3.
3. Three calibration cassettes were recorded through the gate: v4 and v5 failed the release rule, v6 met it (21 of 21 controls failed, 19 of 20 must-pass rows passed). USD 0.744912 in total. Section 5.
4. The first launch died on HTTP 400 (an `enum` inside array items in eligibility schema v4, never sent live before); the rejection was diagnosed, fixed, settled at zero cost and the halt lifted. The second launch died on a missing `finding_bank` table (the migration skipped a table added under an unchanged schema version); fixed and migrated. Sections 6, 6b, 6c.
5. Five code changes were necessary on top of `7dc6485` (section 2). The branch tip is `ea3568a` merged with local `main` at `76eba30`; the deployed runtime is `c545cf8`, which differs from the tip only by the migration fix that the migrated database no longer needs.
6. The eligibility watch of the first 20 papers and the first phase E readout are in sections 7 and 8.

## 2. Deviations from the fixed decisions, and why

The brief fixed the deployed commit as local `main` at `7dc6485`.
The deployed runtime is `c545cf8` instead.
That commit is `7dc6485` plus nine commits of this task, and no other change:

| Commit | Change | Reason |
|---|---|---|
| `525d86a` | `ruff format` over the nine abstention modules. Layout only. | The format check was red on `7dc6485`. The abstention harness landed without a format pass. |
| `4994432` | `CHAPTER3_BUDGET_CHANGE` and `CHAPTER3_CUMULATIVE_CEILING_USD` in `model_broker.py`. `calibration_paper_identity` in `standalone_calibration.py`. The stream-input options of `calibrate-standalone` in `cli.py`. Five tests in `tests/test_chapter3_production_run.py`. | See the two paragraphs below. |
| `9f4cb18` | `data/arctic-ch3-production-run-r1/phase_e_measures.py`. | The phase E script travels with the deployed runtime. |
| `dfb87f6` | The report and the v4 cassette. | Evidence for the first decision. |
| `61d2ea6` | `STANDALONE_SYSTEM` revised to contract v5 (three fail clauses, no pass rule changed); the three v4 misses moved to a `seen` slice; every test that pinned the live literal moved to v5. | Firstmate decision of 2026-09-16 05:52 UTC, option (a). |
| `089a016` | The report and the v5 cassette. | Evidence for the second decision. |
| `e2a8cba` | `STANDALONE_SYSTEM` revised to contract v6: the unit clause excludes a unit implied by a named metric, the sample clause covers only an absent sample type, a comparison-basis clause is added; the two held-out rows v6 was written on moved to `seen`. | Captain decision of 2026-09-16 06:56 UTC. |
| `0badc1f` | The report, the v6 cassette, the halt. | Evidence for the third decision. |
| `ea3568a` | `Database.migrate` runs the `CREATE TABLE IF NOT EXISTS` script on a database whose schema version already matches; `tests/test_db_migrate_idempotent.py`. | The second r2 stop (section 6c). Applied to the production database once; the deployed `c545cf8` runtime does not need it any more. |
| `5f41f2d` (merge) | Local `main` at `76eba30` (the abstention subscription providers) merged into the branch with a merge commit; the one conflict, `abstention_cli.py`, took `main`'s content and a `ruff format` pass. | Inbox message 002. The abstention tests pass on the merge: 36 passed. |
| `c545cf8` | Eligibility schema v4: the reason-code vocabulary moves from an `enum` inside the array items to the item description; prompt v8 names it there; `tests/test_eligibility_request_constraints.py`. Broker: the provider error body and status of every non-2xx answer go into the ambiguous receipt; `settle-http-rejection` settles a rejection before generation at zero cost; `tests/test_http_rejection_settlement.py`. | Firstmate decision of 2026-09-16 07:30 UTC, steps 2 and 3. |

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
The first gate binds `9f4cb18` and names `7dc6485` as `deploy_base_commit`.
The successor gates `live-execution-gate-61d2ea6-ch3.json` (sha256 `6e718fc9...`) and `live-execution-gate-e2a8cba-ch3.json` (sha256 `8e6a2123...`) bind their commits and name the `9f4cb18` gate in `supersedes_config_transition_review`, so the two applied transition events stay valid under them. The broker accepted 41 requests under each successor gate, and the producer's first request under the last one.

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

Three recordings, all with `calibrate-standalone --mode record --provider broker --phase away_production --run-id chapter3-7dc6485-r1 --campaign-id arctic-qa-production-campaign-003`, the gate of the commit named, the policy v9, the ledger transition and the five stream-input options, from that commit's runtime snapshot.

| Quantity | v4 (`9f4cb18`) | v5 (`61d2ea6`) | v6 (`e2a8cba`) |
|---|---|---|---|
| Recorded (UTC) | 05:39:48 to 05:48:49 | 06:16:20 to 06:25:46 | 07:14:43 to 07:24:15 |
| Calls, all `completed`, stage `standalone_verification`, `gemini-3.1-pro-preview` | 41 | 41 | 41 |
| Ledger paper family suffix (`standalone-calibration:standalone-calibration-v2:`) | `2a7b6da0146b` | `144b05368c26` | the v6 prompt hash prefix, in the ledger |
| Spend | USD 0.238626 | USD 0.244198 | USD 0.262088 |
| Cassette (copied into this directory) | `...cassette.v4.jsonl`, sha256 `0d4a0eff...` | `...cassette.v5.jsonl`, sha256 `ebd1cfe2...` | `...cassette.v6.jsonl`, sha256 `68ff6bc2...` |
| Report (copied into this directory) | `calibration-report-9f4cb18-ch3.v4.json` | `calibration-report-61d2ea6-ch3.v5.json` | `calibration-report-e2a8cba-ch3.v6.json` |
| Must-pass | 20 of 20 | 17 of 20 | 19 of 20 (14 of 14 core, 5 of 5 held-out, 0 of 1 seen) |
| Must-fail | 18 of 21; 3 held-out controls passed | 20 of 21; 1 held-out control passed | 21 of 21 |
| Release rule | not met | not met | met |

Ledger after the third recording: spent 54.795709, inflight 0, not halted at that time, `integrity_valid` true, construction headroom USD 19.141424.
The one v6 must-pass miss is `aqa-a355163ada6a2e646d0e` ("RMS error" of the ITP 103 reconstruction, unit not named), a seen row; the v6 judge still reads `undefined_measured_variable` on it although the clause names "RMS error" as a metric that implies its unit.
The must-pass rate of 95 percent is above the 80 percent floor.
The v5 and v6 results stand as evidence for a later prompt slice.

**The v4 misses** (both labelers, judge):

| Row | Question | Both labelers | v4 judge |
|---|---|---|---|
| `aqa-69212ceec983481ba3b4` | "During the aeromycological analyses across the entire sample collection period in May 2019, what airborne fungal concentration value was obtained at sampling location number four, located in the 'Huset' region in the middle of the Longyeardalen valley?" | `undefined_measured_variable`: the unit or metric (CFU per cubic meter) is never named | pass |
| `aqa-75eec503337a30ec9192` | "What was the median relative abundance of the bacterial genus Corynebacterium in samples from Resolute Bay?" (empty context) | `undefined_population_or_sample`: the sample type is not stated | pass |
| `aqa-ada85a8246f0e1d0a319` | "Which ports are listed in association with the taxon Prochromadora sp. 3?" (empty context) | `undefined_location`, `source_dependent_locator`: "listed" points to an unseen table | pass |

The v5 prompt adds one fail clause per class (an unnamed unit or metric, an unstated sample type, a locator word that points at an unseen table) and changes no pass rule.
The three rows moved to a `seen` slice with their labels unchanged, as the decision required.
Under v5 all three fail as required.

**The v5 misses:**

| Row | Slice, label | Question | Both labelers | v5 judge |
|---|---|---|---|---|
| `aqa-950e9ae4b70336903e74` | core, must_pass | "According to the Operational taxonomic units cluster analysis, into how many groups were the 12 samples clustered?" Context: "Samples were collected across Hill, Up, Down, and Sedi sampling sites." | pass | fail, `undefined_population_or_sample` on "12 samples" |
| `aqa-4879bbe8febc48c0b1aa` | core, must_pass | "Across all years, measurement campaigns, and sites in the nutrient-poor Stordalen permafrost peatland (n = 1383), what was the reported mean net N2O flux?" | pass, a quantity stated with its own sample size | fail, `undefined_measured_variable` on "mean net N2O flux" (no unit) |
| `aqa-a355163ada6a2e646d0e` | held-out, must_pass | "In the analysis of ice drift displacement data reconstructed using only the first two principal components, what was the RMS error in reconstruction of ITP 103?" with the ITP context | pass | fail, `undefined_measured_variable` on "RMS error" (no unit) |
| `aqa-590325c3fba229c0e14f` | held-out, must_fail | "What percentage higher was krill production at Iceland supported by baleen whale migration from the Arctic Ocean to seamounts forming Iceland shelf waters?" (empty context) | `undefined_period_or_event`, `undefined_comparison_basis`: the baseline of "higher" is never stated | pass |

The two new fail clauses cut both ways.
The unit clause now kills two named quantities ("mean net N2O flux", "RMS error") whose unit a scientist reads from the name, and the sample clause kills "the 12 samples" in a clustering count where the sample type does not change the count.
The v4 judge already passed the krill row, so that miss is not new to v5; it is a comparison-basis defect that neither prompt names.
The must-pass rate (17 of 20, 85 percent) is above the 80 percent floor, but one must-fail violation fails the rule on its own.

## 6. The launch and the halt

Launched at 2026-09-16 07:24:36 UTC by `build-activation.py e2a8cba launch`: tmux session `arctic-ch3-production-r1`, pane `%29`, producer PID 3595066, launcher `launcher-e2a8cba-ch3.sh`, runtime `app-e2a8cba-arctic-ch3-production-run-r1`, gate sha256 `8e6a2123...`.
The receipt `activation-receipt-e2a8cba-ch3.json` recorded a fresh progress observation at 07:24:41 UTC: state `running`, stage `eligibility`, 4420 papers ready, 0 completed.

At 07:24:51 UTC the first paid call of the run, the eligibility screening of paper 1 (`10.37482/issn2221-2698.2025.59.44`, family `family-4a182d987f95fb00f5ed`, `gemini-3.8-flash`, prompt v8, schema v4), returned HTTP 400 from `generateContent` after `countTokens` had accepted the same request at 23,298 input tokens.
The broker booked the reservation of USD 0.048194 as an ambiguous charge (`known_http_response_unknown_charge`), halted the ledger (`ambiguous_generation_charge`), and the producer stopped itself with `AMBIGUOUS_CHARGE` at 07:24:53 UTC.
The tmux session ended with it.
No paper was screened, no eligibility row was written, and no other request of the run exists.

| Quantity | Value |
|---|---|
| Request key | `585436686ba860d2a789530888a8a8d15d11045d7489df09d62f53344190d680` |
| Receipt, request trace | `model-receipts/<key>.json`, `model-receipts/<key>.request-trace.json` |
| Ledger after | halted, reason `ambiguous_generation_charge`, inflight 0, ambiguous reserved USD 0.140842 (0.092648 chapter 2 plus this 0.048194), spent 54.795709, `integrity_valid` true |
| Provider error body | not on record: the broker keeps the status code and `Retry-After` only |

**Diagnosis** (firstmate decision of 2026-09-16 07:30 UTC, step 1; every call is recorded in `diagnostic-400-call.json` and `diagnostic-hypothesis-calls.json` in this directory):

| Call | Payload | Result |
|---|---|---|
| Diagnostic | the exact traced payload, request sha256 `f8e078e8...` | 400 `INVALID_ARGUMENT`, "Request contains an invalid argument." |
| A | the same, every `description` keyword removed from the schema | 400 |
| B | A plus `reason_codes.items` as a plain string | 200 |
| C | the traced payload, `reason_codes.items` given `type: string` beside its enum | 400 |
| D | the traced payload, `reason_codes.items` as a plain string, descriptions kept | 200 |

The provider rejects an `enum` inside the `items` of an array (18 values here).
The `description` keywords are accepted.
Schema v3, which chapter 2 sent 202 times, has no enum inside items.
The two 200 calls (B, D) generated one screening each outside the ledger, about 24,700 tokens each on `gemini-3.8-flash`, about USD 0.02 each by the registered rates; the three 400 calls are not billed.

**The fix** (`c545cf8`, step 2): the vocabulary moves into the item description of `reason_codes` in schema v4, prompt v8 says "the reason codes that the schema lists", and `tests/test_eligibility_request_constraints.py` keeps every live eligibility schema inside the keyword set the provider accepted and refuses an enum inside array items.
The validator already ignored the enum (`_measurement_relaxed_schema`), so the contract semantics are unchanged: the codes are a measurement vocabulary and take no part in the eligibility decision.

**The broker change** (step 3): every non-2xx answer now records `error_body` (at most 4000 characters) and `provider_error_status` in the ambiguous receipt.
`settle-http-rejection` settles one ambiguous charge whose recorded body, or a reproduction of the exact same request, proves a rejection before generation (HTTP 400, `INVALID_ARGUMENT`): the reservation leaves the ambiguous funds, the request becomes a zero-cost settled record with `http_rejection_settlement_sha256`, the halt lifts when every other ambiguous request has its continuation, and the key is never replayed.
`docs/SHARED_MODEL_BROKER.md` documents it.

**The settlement** of `585436686ba8...` at 08:02:33 UTC, under the `e2a8cba` gate the request ran under, with the fix commit's code:

| Quantity | Value |
|---|---|
| Evidence, review | `http-rejection-evidence-58543668-ch3.json`, `http-rejection-review-58543668-ch3.md` (copied into this directory) |
| Evidence source | reproduction: the diagnostic call, same request sha256, same 400 `INVALID_ARGUMENT` |
| Settlement event | `model-receipts/<key>.http-rejection-settlement.json`, sha256 `3d806f22...` |
| Released | USD 0.048194; ambiguous reserved USD 0.140842 to 0.092648 (the chapter 2 level) |
| Ledger after | not halted, inflight 0, `integrity_valid` true, spent unchanged at 54.795709 |

**The relaunch.** The stream's immutable invocation manifest for run id `chapter3-7dc6485-r1` binds the uncorrected prompt and schema hashes, so a relaunch under that id fails before any call ("the immutable streaming run inputs changed"), and the manifest is never deleted.
The corrected run is `chapter3-7dc6485-r2`: the same campaign `arctic-qa-production-campaign-003`, the same streaming input, the same frozen order from paper 1, the same gate chain (the `c545cf8` gate names the `9f4cb18` gate in `supersedes_config_transition_review`), the carried v6 calibration (the judge prompt hash is unchanged), and its own eligibility run directory `chapter3/gemini-eligibility/chapter3-7dc6485-r2`.
Paper 1 is screened again by the corrected request, which has a new request key; nothing is replayed.
The decision named "the same run id"; the immutable manifest makes that impossible without deleting a run artifact, so the run id moved to r2. Section 6b gives the launch evidence.

### 6b. The r2 launch

Launched at 2026-09-16 08:05:18 UTC by `build-activation.py c545cf8 launch` (run id r2): tmux session `arctic-ch3-production-r1`, pane `%30`, producer PID 1999695, launcher `launcher-c545cf8-ch3.sh`, runtime `app-c545cf8-arctic-ch3-production-run-r1` (sha256 `53d94841...`), gate `live-execution-gate-c545cf8-ch3.json` (sha256 `a25e62b6...`).
The receipt `activation-receipt-c545cf8-ch3.json` recorded a fresh progress observation at 08:05:22 UTC: state `running`, stage `eligibility`, 4420 papers ready.
At 08:06:02 UTC paper 1 had two completed eligibility calls under the corrected schema and no halt.
The receipt lists every calibration recording (v4, v5, v6) with its misses, and the settlement of the r1 rejection.

### 6c. The second stop and the migration fix

At 08:06:25 UTC, after paper 1 (unresolved after its re-ask) and paper 2 (eligible), the producer stopped in the generation stage on `sqlite3.OperationalError: no such table: finding_bank`.
No paid call failed: three eligibility calls completed for USD 0.0627 and the ledger stayed clean, not halted, with the request states and receipts consistent.
Cause: `Database.migrate` returned before the idempotent table script whenever `schema_info.version` already matched.
The chapter 3 integration added the `finding_bank` and `finding_prescreen_shadow` tables to the schema under the same version, so a fresh database (the dry run, the tests) had them and the production database, already at version 5, never got them.
Fix (`ea3568a`): `migrate` runs the `CREATE TABLE IF NOT EXISTS` script on every open; `tests/test_db_migrate_idempotent.py` drops the two tables from a current database and checks that a re-open restores them.
The production database was migrated once with the fixed code at 08:19 UTC (the two tables and their index were created; schema version 5, no other change, no backup because the version did not change).
The `c545cf8` runtime then runs correctly on it, so the producer was relaunched on the same runtime, the same gate and the same run id r2; the relaunch resumes the three completed eligibility receipts by request key and replays nothing.
The first r2 receipt is kept as `activation-receipt-c545cf8-ch3.first-launch.json`.
The relaunch at 08:09:09 UTC runs as PID 2229516 in tmux session `arctic-ch3-production-r1`; the receipt `activation-receipt-c545cf8-ch3.json` records it with a fresh progress observation at 08:09:20 UTC.

## 7. The eligibility watch

Not reached: no paper was screened.
`build-activation.py e2a8cba watch-eligibility` implements rule 5 (after the producer passes paper 20, read the last row of each of the first 20 papers; more than 5 `screening_error` or `unresolved_rescreenable` rows interrupt at a zero-in-flight boundary).
The watch ran once after the halt and recorded 0 papers with rows in `eligibility-watch-e2a8cba-ch3.json`.

## 8. The first phase E readout

`phase_e_measures.py` is in this directory and in the deployed runtime.
On the chapter 2 artifacts it reproduces the yield audit's values: 38 of 200 screening errors, USD 19.995149 over 6 items, USD 3.3325 per item, and the stage costs of the audit cost summary.
On the chapter 3 artifacts it has nothing to read: the run halted before its first eligibility row.
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

Command: `nix develop -c bash -c 'PYTHONPATH=src pytest <files> -p no:cacheprovider -o addopts=""'`, four bounded foreground parts, run on every deployed commit.

| Part | `9f4cb18`, `61d2ea6`, `e2a8cba` | `c545cf8` | `ea3568a` |
|---|---|---|---|
| `tests/` without the three slow files | 984 passed | 994 passed | see below |
| `tests/test_cli_integration.py` | 98 passed | 98 passed | see below |
| `tests/test_streaming.py` | 56 passed | 56 passed | see below |
| `tests/test_model_broker.py` | 86 passed | 86 passed | see below |
| `ruff check`, `ruff format --check` | clean | clean | clean |

On the merge commit `5f41f2d` the four abstention test files pass: 36 passed.

The five new tests in `tests/test_chapter3_production_run.py` are in the first part.
They pin the ceiling constant, replay the chain (chapter 2 price, chapter 2 ceiling, chapter 3 price, chapter 3 ceiling) on a fixture ledger, refuse a wrong tranche, record a cassette through a bound broker with a fake transport, and refuse a recording without the gate's streaming input.
The offline calibration tests pin the v5 clauses and the `seen` slice.

## 10. Deferred and open

| Item | Owner |
|---|---|
| The 400 halt. Options this report sees: (a) a broker change that keeps the HTTP error body in the ambiguous receipt, plus a reviewed release path for a 4xx `known_http_response_unknown_charge` (a documented request rejection), then a reviewed continuation of `585436686ba8...` and a relaunch that captures the message; (b) one authorized diagnostic `generateContent` call with the exact traced payload, outside the producer, to read the 400 message before any code change; (c) revert the eligibility request to prompt v7 and schema v3 for chapter 3, which chapter 2 sent live 202 times, and record that the v8 re-screen is untested. Every option is a rule or scope decision above this task. | firstmate and captain |
| Merge local `main` (`76eba30`) into the branch with a merge commit and rerun the abstention tests, before "ready in branch" (inbox message 002). Not done: the task is blocked before that point. | this task |
| The exporter rebind to the chapter 3 runtime (step 7 of the pattern) waits for a running producer, so the dataset page keeps showing the chapter 2 snapshot. | this task, after the halt clears |
| Measures 4 and 5 (reader labels) are outside this task. | phase E |
| The v6 judge's one must-pass miss ("RMS error") and the v5 evidence are input for a later prompt slice. | a later judge slice |
| Three `standalone-calibration` paper families exist in the shared ledger, one per recorded prompt hash. | whoever revises the prompt |
