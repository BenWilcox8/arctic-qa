# Chapter 2 integration, validation, and execution gate

Task: `arctic-ch2-integration-run-r1`.
Branch: `fm/arctic-ch2-integration-run-r1`, from `fm/arctic-audit-priorities-r1` at commit `a53b106`.
Source of the work: the four slice reports under `data/arctic-ch2-*-r1/report.md`, the r15 audit report section 5, and the chapter 2 plan.
No paid provider call was made.
Nothing under `/mnt/crdata` changed in the integration half.
No legacy corpus, candidate, receipt, export, or runtime snapshot changed.

## 1. Files touched outside the slices

The integration half changed these files on top of the four merged slices.

| File | Change |
| --- | --- |
| `src/arctic_qa/generation.py` | Merge reconciliation only (section 3). |
| `src/arctic_qa/validation.py` | Merge reconciliation only (section 3). |
| `src/arctic_qa/streaming.py` | The end-of-run role disclosure reads the per-role effective models (section 4, blocker 3). |
| `src/arctic_qa/model_roles.py` | A single-provider profile is legal when every role shares the provider (blocker 2). |
| `src/arctic_qa/gemini_eligibility.py` | Price config revision v6 with the judge stage models (blocker 1). |
| `src/arctic_qa/model_broker.py` | The chapter 2 ceiling and its transition change set (section 5). |
| `src/arctic_qa/chapter2_corpus.py` | `materialize_stream_input` writes the gate-bindable streaming input (section 6). |
| `src/arctic_qa/cli.py` | `--role-profile` takes any profile name; `chapter2-corpus --action stream-input`. |
| `config/roles.v1.json` | Profile `gemini_separated`, schema version 1.2.0. |
| `config/gemini-eligibility-v1.json` | Config id `arctic-gemini-eligibility-r1-config-v6`. |
| `config/live-dataset-current-contract-v1.json` | Schema `2.7.0`, prompt `arctic-qa-generation-v22`. |
| `config/gemini-eligibility-prompt-v7.txt` | The corpus crew's v7 plus the writer crew's E4 sentences. |
| `tests/test_chapter2_integration.py` | New, 12 tests (section 8). |
| `tests/test_model_broker.py`, `tests/test_streaming.py`, `tests/test_model_roles.py`, `tests/test_gemini_eligibility.py`, `tests/test_publication_export.py`, `tests/test_project_progress_viewer.py` | Updated for the merged contracts (section 8). |
| `docs/METHODS.md`, `docs/STREAMING_DATASET.md`, `AGENTS.md` | The single-provider rule and the chapter 2 launch contract. |

## 2. Contract versions after integration

One value per contract, as the brief requires.

| Constant | Value | Origin |
| --- | --- | --- |
| `GENERATION_PROMPT_VERSION` | `arctic-qa-generation-v22` | writer slice |
| `CANDIDATE_SCHEMA_VERSION` | `2.7.0` | writer slice |
| `STANDALONE_VERIFICATION_CONTRACT_VERSION` | `source-blind-scientific-referent-v3` | gates slice |
| `QUESTION_VERIFICATION_CONTRACT_VERSION` | `question-verification-v2` | gates slice, one bump for both schema changes |
| `NUMERIC_RULE_CONTRACT_VERSION` | `numeric-rule-source-support-v3` | gates slice |
| `OPTION_DISPLAY_CONTRACT_VERSION` | `displayed-option-structure-v1` | gates slice |
| `STANDALONE_CALIBRATION_SET_VERSION` | `standalone-calibration-v1` | gates slice |
| `ROUTING_CONTRACT_VERSION` (`GENERATION_ATTEMPT_CONTRACT_VERSION`) | `bounded-failure-routing-v4` | core-routing slice |
| `MODEL_ROLES_CONTRACT_VERSION` | `generation-model-roles-v1` | core-routing slice |
| `REJECTION_DIAGNOSTIC_CONTRACT_VERSION` | `rejection-diagnostic-detail-v1` | core-routing slice |
| `SCOPE_ROLE_FINDING_POLICY_VERSION` | `one-finding-per-paper-ranked-context-v8` | writer slice |
| `CONTEXT_ONLY_EVIDENCE_CONTRACT_VERSION` | `question-context-evidence-v1` | writer slice |
| `REFERENT_SLOT_CONTRACT_VERSION` | `referent-slot-checklist-v1` | writer slice |
| `FINDING_ADMISSION_CONTRACT_VERSION` | `freeze-time-finding-admission-v1` | writer slice |
| Eligibility prompt | `gemini-eligibility-prompt-v7` | corpus slice |
| Broker price config id | `arctic-gemini-eligibility-r1-config-v6` | integration |
| `config/roles.v1.json` schema | `1.2.0` | integration |

`CANDIDATE_CONTRACTS["2.7.0"]` names every chapter 2 contract, including `option_display_contract_version`.
`CANDIDATE_CONTRACTS["2.6.0"]` is pinned to the predecessor literals: prompt v21, routing v3, standalone v2, question-verification v1, numeric v2.
A stored chapter 1 candidate therefore keeps its historical contract, and the live export selects chapter 2 only.

## 3. Merge order, conflicts, and resolutions

Merge order: core-routing (`39ca2b7`), writer-context (`e9a4be3`), gates (`7fc381d`), corpus (`5ec1e1e`).
Core-routing merged clean.
Every conflict below was resolved in favour of the audit's section 5 intent, keeping both behaviours.

### Writer-context merge

| File | Conflict | Resolution |
| --- | --- | --- |
| `generation.py` imports and constants | Both slices added imports and version constants. | Kept both. `SCOPE_ROLE_FINDING_POLICY_VERSION` is the writer's v8. `GENERATION_ATTEMPT_CONTRACT_VERSION` reads `ROUTING_CONTRACT_VERSION`. |
| `generation.py` instruction blocks | Core added `REVISION_INSTRUCTIONS`, the widened and surgical texts. Writer added the coverage and period texts. | Kept all. The writer's revision sentences about referent slots were folded into `REVISION_INSTRUCTIONS` (section 7). |
| `generation.py` extractor call | Core: one `answer` per call with a two-pass admission loop. Writer: ranked `candidate_findings` with `_admit_ranked_finding`. | The ranked call runs inside the admission loop. `_admit_ranked_finding` applies both admission checks. When every ranked candidate fails an admission check, the extractor is asked once more with those spans excluded. |
| `generation.py` frozen-finding quality reason | Core: `finding_admission_reason`. Writer: the leak check only. | Core's check first, then the writer's table and figure checks. |
| `generation.py` context bundle | Core: `activity_context_block` on the widened rung. Writer: the two-part bundle on every attempt. | The writer's bundle, as the core report asked. The widened instruction now names `CONTEXT_ONLY_SOURCE`. |
| `generation.py` writer schema | Core: `context_gap`. Writer: `referent_slots`. | Both. |
| `validation.py` contract rows | Core pointed `2.6.0` at the constants. Writer pinned `2.6.0` and added `2.7.0`. | Writer's rows; `2.7.0` reads `ROUTING_CONTRACT_VERSION`. |
| `validation.py` helpers | Core: `phrase_in_source_text`. Writer: `scope_phrase_in_text` and the context-only helpers. | Both wrappers stay, over the one `_scope_phrase_in_text` projection. |
| `validation.py` `scope_is_evidence_bound` | Core: `allow_empty`. Writer: `interpretation_texts`. | One signature with both. The reconstructor call passes both. |
| `streaming.py` routing sets | Each slice added reason codes. | Union. |
| `fixtures/fake-verifier.jsonl` | Core changed prompt markers. Writer changed response fields. | Core markers with the writer's `interpretation_scope_applies_to_finding` field. |
| `tests/test_project_progress_viewer.py` | Three slices fixed the same red test differently. | Core's version, because core also changed the viewer code that the test asserts. |

One follow-up from the merge: a `both` resolution dropped one closing parenthesis in the instruction block. It was found by the syntax check and repaired before any test ran.

### Gates merge

| File | Conflict | Resolution |
| --- | --- | --- |
| `generation.py` extractor numeric block | Core added the D7 sentence. Writer kept the v21 vocabulary. Gates replaced the block with `NUMERIC_METADATA_VOCABULARY`. | The gates block, inside the ranked-candidate call, with the D7 sentence. |
| `generation.py` answer-verifier prompt | Core: "label the claim type alone". Gates: the `relation_scope_match` split. | Both sentences. |
| `generation.py` verifier scope sentence | Writer widened the source to `CONTEXT_ONLY_SOURCE`. Gates moved bookkeeping to `scope_representation_note`. | The writer's source with the gates' note. "Otherwise set relation_scope_match to false" is gone, as section 4.4 fix 5 requires. |
| `generation.py` `_qa_gate_reasons` | Both slices switched to the shared phrase projection. | Writer's copy, which also tests display in `question_context`. |
| `validation.py` constants and helpers | Additive on both sides. | Both. |
| `validation.py` `option_display_issue` | Core's pre-filter next to the gates' numeric display code. | Both. The pre-filter now runs the text rule on every option first, then the numeric rule, in the order the gate uses. |
| `validation.py` `CANDIDATE_CONTRACTS["2.6.0"]` | Gates pointed it at the new gate versions. | Pinned to the `PREDECESSOR_*` literals; `option_display_contract_version` moved to `2.7.0`. |
| `fixtures/fake-author.jsonl`, `fake-verifier.jsonl` | Gates markers and the `question-verification-v2` response. | Gates rows as the base, plus the core markers and the writer field. |
| `docs/BENCHMARK_INPUT_CONTRACT.md`, `AGENTS.md` | Both slices appended. | Both. |

### Corpus merge

| File | Conflict | Resolution |
| --- | --- | --- |
| `gemini_eligibility.py` | Two normalizers for phrase binding. | The corpus `_normalize_for_binding`; `normalize_for_phrase_binding` is its public alias. The core-only constants were removed. |
| `config/gemini-eligibility-prompt-v7.txt` | Add/add. | The corpus v7, plus the writer's three E4 sentences after the corpus sentence that states the same rule. |
| `AGENTS.md` | Both appended. | Both. |

The corpus merge commit also holds one `ruff format` pass over the 14 files that the slices left unformatted, as the gates report asked.

## 4. Run blockers handed to integration

The core-routing report named two run blockers, and the dry run found a third.

**Blocker 1. No per-role stage models with pricing.**
`config/gemini-eligibility-v1.json` is now revision `arctic-gemini-eligibility-r1-config-v6`.
It registers `standalone_verification`, `option_verification`, `blinded_reconstruction`, and `answer_verification` on `gemini-3.1-pro-preview`.
Pricing was read from `https://ai.google.dev/gemini-api/docs/pricing` on 2026-09-15: USD 2.00 input and USD 12.00 output per million tokens, standard tier, prompts up to 200k tokens.
The model page confirms the id `gemini-3.1-pro-preview`, structured output, and preview lifecycle.
The validator pins every value, caps `maximum_input_tokens` at 200,000 to stay inside the price tier, keeps `maximum_output_tokens` at 8192, and requires an active price window.

**Blocker 2. No cross-provider streaming path.**
Decision: run chapter 2 inside one provider.
`run_stream` demands one shared broker and `generate_candidate` refuses mixed budget authorities; a Claude writer with a Gemini judge would need a second metered broker with its own verified price record and ledger binding.
That is not a bounded change, and the captain asked to get things running.
The new profile `gemini_separated` puts `gemini-3.8-flash` on every writer role and `gemini-3.1-pro-preview` on every judge, with `gemini-3.1-flash-lite` as the answer judge.
The audit's point holds: the source-blind judge and the option judge are a different, stronger model than the writer.
The reconstructor and answer verifier are also on the Pro model, because the role contract forbids a judge on the writer model and the only other Gemini model is the weakest one.
`model_roles._validate_profile` accepts a single-provider profile only when every role shares that provider; a mixed profile still keeps every judge outside the writer's family.

**Blocker 3. The end-of-run disclosure compared provider objects.**
`run_stream` computed `same_model_roles` from `author.model == verifier.model`.
One `BrokerProvider` serves both while metering a different model per role, so an enforced run raised at the end after every call.
The disclosure now reads `model_roles["same_model_roles"]`, which `_resolve_model_roles` derives from the per-role effective models.

## 5. Budget and ledger transition

The captain's allocation is USD 75.00 on top of the USD 33.994972 spent before chapter 2.
`model_broker.CHAPTER2_BUDGET_EXTENSION_CHANGE` moves `away_session_total_ceiling_usd` from `61.614496` to `108.994972`, with tranche `108.994972`.
The policy validator accepts that ceiling and no other new value.
The chain is two transitions, in this order:

1. A v3 price transition from the r15 config (sha `fd95a1e0…`) to the v6 config, policy unchanged, predecessor the r13 event `config-transition-1bda6288…`.
2. A v2 policy transition from policy v7 to the chapter 2 policy v8, price unchanged, predecessor the step 1 event.

`tests/test_chapter2_integration.py::test_the_chapter_two_chain_adds_judge_pricing_then_the_ceiling` replays exactly this chain on a fixture ledger and asserts the remaining budget and a refused wrong tranche.
The policy file is `streaming-dataset-budget-policy-v8-chapter2.json` in the task data directory.
No legacy liability is touched: the ledger keeps its reserved and ambiguous holds, and a transition requires the no-replay validation to pass.

## 6. Chapter 2 streaming input

The execution gate binds its input through `continuation_run_manifest_sha256`, `continuation_run_receipt_sha256`, `continuation_frozen_manifest_sha256`, and `continuation_order_sha256`.
The corpus crew's access run records the selection but not those bindings, and its receipt has no `run_manifest_sha256`.
`chapter2_corpus.materialize_stream_input` writes a second access run under the chapter 2 root, `streaming-input/<run-id>/`, that carries them and repeats the same items.
It verifies the manifest hash against the descriptor, the order hash against the selection, and each item against its selection row.
The write is idempotent and touches no existing object.
The test drives `SharedGeminiBroker._validate_stream_input_binding` over the result with a gate built from the returned hashes.

## 7. New and changed prompt text

The slices' prompt text is in their reports.
Integration changed three texts.

**`REVISION_INSTRUCTIONS`**, after "or remove that phrase from the question.":

> When failure_feedback names one or more referent slots, resolve every named slot in question_context before you change any other wording, and take the value from SOURCE_DATA or from CONTEXT_ONLY_SOURCE in plain words.

and, replacing the `context_gap` sentence:

> If neither SOURCE_DATA nor CONTEXT_ONLY_SOURCE states the detail that failure_feedback demands, set context_gap to that exact detail, set that slot to unavailable_in_source in referent_slots, and leave the question unchanged.

**`CONTEXT_WIDENED_REVISION_INSTRUCTIONS`** now starts:

> This attempt repeats an earlier rejection on this finding. CONTEXT_ONLY_SOURCE carries every hash-bound study-setting span this paper supplies for the frozen finding.

**Answer-verifier scope sentence**, merged:

> It must contain the answer and every verified scope value that SOURCE_DATA states. Return the exact proposed scope only when each value occurs verbatim in that span or in a CONTEXT_ONLY_SOURCE span, and the QUESTION or the QUESTION_CONTEXT states it. Record any other case in scope_representation_note.

**`config/gemini-eligibility-prompt-v7.txt`** carries both crews' statements of the E4 rule, one after the other.

## 8. Tests

Command: `nix develop -c bash -c 'PYTHONPATH=src pytest <files> -q'`.
The whole suite ran in three bounded parts on the final commit, because `test_streaming.py`, `test_cli_integration.py`, and `test_model_broker.py` hold real rate-limit sleeps.

| Part | Files | Result |
| --- | --- | --- |
| A | every test except the three below | 568 passed, 0 failed |
| B | `test_streaming.py`, `test_cli_integration.py` | 148 passed, 0 failed |
| C | `test_model_broker.py` | 86 passed, 0 failed |

In total 802 tests passed and none failed or was skipped, on commit `2ef39bd`, which differs from the branch head only by this report file.
`ruff check src tests` and `ruff format --check src tests` pass on every file.

New regression tests, `tests/test_chapter2_integration.py`:

- The launch profile runs inside one provider with separated models and the strongest judge on both source-blind gates.
- A mixed-provider profile still refuses a judge inside the writer family.
- The profile matches the broker stage models role by role.
- Price config v6 pins the judge model, its price, its input cap, and its thinking level; a changed price, cap, level, or expired window is refused; a missing judge stage is refused.
- The chapter 2 ceiling is the only new allowed ceiling and equals the sum stated by the captain.
- The chained transition (price, then ceiling) binds the new ceiling and refuses a wrong tranche.
- The live export selects `2.7.0` and v22, and `2.6.0` keeps v21.
- The chapter 2 streaming input satisfies the gate binding and is idempotent.

Test changes for the merged contracts:

- `test_streaming.py`: the eleven-call test now asserts separated roles, because the v6 config meters every judge stage on a different model from the writer.
- `test_model_broker.py`: the `execute` helper keys a request with the stage's model; the legacy revision fixture registers only the answer judge stage.
- `test_model_roles.py`: a single-provider profile separates by model.
- `test_gemini_eligibility.py`: the checked-in config id is v6, and the legacy revision fixture keeps only the answer judge stage.
- `test_publication_export.py`: the current pair is `2.7.0` and v22.

### Mocked end-to-end dry run

Script: `data/arctic-ch2-integration-run-r1/dry-run/offline-dry-run.py` in the task data directory, result `offline-dry-run-worktree.json`.
It runs three families through `run_stream` in the `away_production` phase with the profile `gemini_separated`, a full execution gate bound to the input, price config v6, eligibility prompt v7 with response contract v3, and the test suite's scripted transport.
No network call is made.
The three families are fixture-sourced, because the scripted provider answers from the checked-in fixtures and cannot answer for a real paper; every gate ran on real code with those inputs.

All 14 checks passed:

- The legacy price config, which serves every judge from the writer model, is refused by the role assertion before any call.
- The roles assertion is enforced, the writer and judges are separated, and both source-blind gates carry the Pro model.
- All three families were accepted, 33 metered calls, with every judge stage metered on `gemini-3.1-pro-preview`.
- The `CONTEXT_ONLY_SOURCE` block, with one forwarded hash-verified activity span per family, is in every extractor, writer, reconstructor, answer-verifier, distractor-writer, and option-verifier prompt, and absent from the source-blind judge prompt.
- The extractor prompt carries `NUMERIC_METADATA_VOCABULARY numeric-rule-source-support-v3`.
- Every accepted candidate carries schema `2.7.0`, prompt v22, standalone contract v3 with `pass` true, question-verification v2, a reconstruction, an answer agreement, admitted rank 1, ten referent slots, and four distractors.

The calibration fixture is green: `tests/test_standalone_calibration.py` is inside part A.

## 9. Rigor safeguard for every integration change

| Change | Why it cannot admit a paper-dependent or unsupported item |
| --- | --- |
| Ranked candidates inside the admission loop | Both slices' admission checks only reject, and they now both run on every ranked candidate. The re-ask spends an extractor call, never a family path. |
| Merged `scope_is_evidence_bound` | `allow_empty` applies only to the blind reconstructor. A populated value must still be verbatim in a hashed span of the same paper. |
| Merged verifier scope sentence | Bookkeeping moved to a note that never gates; the hard rejects on entailment and on a contradicted scope value stay. |
| Single-provider profile | The judge is still a different and stronger model than the writer. A judge on the writer model is still refused. |
| Price config v6 | Pricing only. Every gate and prompt is unchanged by it. |
| Chapter 2 ceiling | Budget only. The ledger halts at exhaustion with no reset. |
| Streaming input | Custody only. Every hash is recomputed from the frozen objects. |
| Role disclosure fix | The assertion is now honest about per-role models; it still raises when an enforced run collapses to one model. |
| Live export pin | Export only selects chapter 2 contracts; legacy snapshots stay on disk. |

## 10. Paid re-screen estimate

Legacy evidence from the shared ledger and the eligibility run directories, read only:

| Measure | Value |
| --- | --- |
| Completed eligibility jobs | 678 |
| Mean cost per job | USD 0.026717 |
| Highest cost per job | USD 0.097757 |
| Decisions | 436 eligible, 196 excluded, 46 uncertain |
| Uncertain with only geography unsatisfied | 45 jobs (6.6 percent) |

Chapter 2 re-screens every paper under prompt v7, one call per paper, with at most one bounded repair call.
The producer screens in frozen order and stops at the ledger ceiling, so the estimate is bounded by `--max-papers`.

| Bound | First screen | Repairs (10 percent) | Geography re-screen (6.6 percent) | Total |
| --- | --- | --- | --- | --- |
| 800 papers (the r15 launcher default) | USD 21.37 | USD 2.14 | USD 1.41 | USD 24.92 |
| 4,420 papers (whole corpus) | USD 118.09 | USD 11.81 | USD 7.79 | USD 137.69 |

The whole-corpus figure exceeds the USD 25 stop rule, and it also exceeds the whole allocation.
The launcher therefore keeps the r15 bound of 800 papers, whose estimate is under USD 25.
The ledger ceiling is expected to stop the run before that bound: at legacy rates, screening plus generation cost about USD 0.13 per screened paper with Pro judges, so USD 75 covers about 560 papers.
The geography re-screen is a separate paid command after the producer halts, inside the same ceiling, and only when budget remains.

## 11. Deferred

| Item | Why | Owner |
| --- | --- | --- |
| Cross-provider streaming (Claude writer) | Needs a second metered broker with a verified price record. Recorded as blocker 2. | open |
| `revision_regressed_context` deterministic guard | Deferred by the core slice; unchanged. | open |
| The validator half of sentence-complete finding spans | Deferred by the corpus slice; unchanged. | open |
| Two-labeler calibration set | The fixture stays `single_labeler_provisional`, as the gates slice recorded. | open |
| The geography re-screen command | Paid, runs after the producer halts if budget remains (section 10). | activation half |
