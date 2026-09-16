# Chapter 3 integration report (arctic-ch3-integration-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-integration-r1`, from local `main` at `bd2fb22`.
Plan: `data/arctic-chapter3/plan.md`, the row `arctic-ch3-integration-r1`.
Source of the changes: the chapter 2 yield audit, `data/arctic-ch2-yield-audit-r1/report.md`, sections 4 and 5 and 8.
No paid provider call was made.
No file under `/mnt/crdata` changed.
Legacy and chapter 2 data are unchanged.
No activation set, ledger transition or launcher was built.
No production run starts from this work.

## 1. Result in six lines

1. The six slice branches are merged into one branch, in the order gates, writer-context, judge-options, eligibility, routing, cost. Every conflict is listed in section 3 with its resolution.
2. The whole suite is green in four bounded foreground parts: 957 plus 98 plus 56 plus 86 tests, 1197 in total. `ruff check` and `ruff format --check` are clean.
3. The deterministic replay of the 139 recorded chapter 2 candidates through the integrated gates frees 16 candidates in 10 families and regresses none. Every accepted chapter 2 item still passes.
4. The eligibility replay of the 79 non-eligible papers, the routing replay and the calibration harness in cassette replay mode all run on the integrated code. Section 5 gives the numbers.
5. A mocked end-to-end dry run over six candidate families and three papers shows every path the brief names: the pre-judge free checks, the standalone call on every candidate, the Pro call skips, the shadow cohort, the option call plan and the re-screen path.
6. A new test proves that the pre-judge free checks are exactly the corrected gates on all 139 candidates, so phase D sits behind phase C.

## 2. Audit findings addressed

The six slices address the findings in their own reports.
This report addresses the integration rows of the plan and the two cross-slice rules of audit section 5.

| Item | Where |
|---|---|
| Phase D "only after phase C": the free checks read the corrected gates | `tests/test_ch3_integration.py::test_the_pre_judge_free_checks_are_exactly_the_corrected_gates`, section 6 |
| One `GENERATION_PROMPT_VERSION` v23, one `CANDIDATE_SCHEMA_VERSION` 2.8.0 with every `CANDIDATE_CONTRACTS` row pinned | `validation.py`, section 4 |
| One standalone contract v4, one routing contract v5, one price config revision v8 | section 4 |
| Every new reason code registered in the routing sets | `tests/test_ch3_integration.py::test_every_new_candidate_code_has_one_routing_layer_entry`, section 7 |
| The replay of the 139 candidates, the 79 papers and the repeat detector | section 5 |
| The mocked dry run and the cassette replay | section 6 |
| The cost projection and the phase E plan | section 8 and `phase-e-measurement-plan.md` |
| The live export contract moved to 2.8.0 and v23 | section 9 |

## 3. Merge conflicts and their resolution

Every resolution follows audit section 5: keep both behaviours, and when two slices moved one contract, keep one version string.
Nothing from audit section 4.10 was implemented.

### 3.1 writer-context onto gates

| File | Conflict | Resolution |
|---|---|---|
| `tests/test_streaming.py` | prompt version literal v22 (gates) against v23 (writer-context) | v23, with the gates comment kept |

### 3.2 judge-options

| File | Conflict | Resolution |
|---|---|---|
| `validation.py` | gates set the calibration set to v2; judge-options set the standalone contract to v4 and kept the set at v1 | both: `CHAPTER2_STANDALONE_VERIFICATION_CONTRACT_VERSION` v3, `STANDALONE_VERIFICATION_CONTRACT_VERSION` v4, `STANDALONE_CALIBRATION_SET_VERSION` v2 |
| `validation.py` | two `2.8.0` rows of `CANDIDATE_CONTRACTS`, one per slice, and a `2.7.0` row that pinned the live numeric and routing versions | one reconciled table, section 4 |
| `generation.py` | imports and the schema-version comment | both import lists; one `CANDIDATE_SCHEMA_VERSION = "2.8.0"` with a comment that names both slices |
| `fixtures/fake-author.jsonl` | writer-context markers and `scope_evidence`; judge-options six proposals | extractor and writer lines from writer-context, distractor line from judge-options |
| `fixtures/fake-verifier.jsonl` | writer-context verifier markers; judge-options v4 verdict, new option keys, set verdict | standalone, option and set lines from judge-options, answer verifier line from writer-context |
| `tests/test_cli_integration.py` | four option calls and v3 against four calls plus the set call and v4 | judge-options |
| `tests/test_writer_context_bundle.py` | the historical-row test read `2.8.0` with v22 | it reads `2.7.0` and pins v22, v3, numeric v3 and routing v4 |

Reconciliation after the merge, commit `52d21c2`:

- `standalone_verdict_is_unevidenced` re-labelled 30 stored chapter 2 verdicts, because the v4 evidence rule read a v3 verdict that was never asked for phrases. `standalone_verdict_is_evidence_bound` now guards the rule in `validation._standalone_reason_codes` and `generation._standalone_gate_reasons`. A verdict with contract v3 keeps its recorded codes. A raw verdict with no contract version is a live v4 response and is judged.
- `chapter2_replay.py` inserted the creation reason under the bare code while `generate_candidate` now uses the `standalone_det_` namespace, so 23 candidates showed a false "added" code. The replay now mirrors `generate_candidate` and compares the recorded and replayed sets with the namespace collapsed.
- The calibration fixture v2 header named contract v3. It now names v4 and the north star without the guessability clause, and the judge slice fixture names parent set v2.
- The two ITP rows of the judge slice fixture flipped from `reject_pending_gates_fix` to `pass`, because the gates matcher now reads the hyphenated gloss. The CMP22, "CO 2" and CHINARE rows stay rejected by the free screen, as the gates slice decided.

### 3.3 eligibility

| File | Conflict | Resolution |
|---|---|---|
| `AGENTS.md` | two new bullets | both |
| `docs/BENCHMARK_INPUT_CONTRACT.md` | two new sections at one anchor | both, the eligibility section after the writer-context section, with the phrase-test sentence corrected to "geography and sample" |
| `streaming.py` | `OPTION_REPAIR_REASONS` (judge-options) against `ELIGIBILITY_CONTRACT_REASONS` (eligibility) | both sets |

### 3.4 routing

| File | Conflict | Resolution |
|---|---|---|
| `AGENTS.md` | a third bullet | all three |
| `gemini_eligibility.py` | gates made v8 the Pro agreement judge; routing made v8 the writer timeout | one v8 revision does both: `chapter3_revision` selects the Pro agreement validator and the writer timeout stage |
| `config/gemini-eligibility-v1.json` | auto-merged | one v8 file with the Pro `answer_agreement` block and the `question_generation` timeout entry |
| `validation.py` | gates agreement prompt v2 against routing v5 and the scope defect contract | both, with `CHAPTER2_ROUTING_CONTRACT_VERSION` v4 for the `2.7.0` row |
| `streaming.py` | judge-options `forwarded_slot_evidence` against routing `evidence` and `unroutable` | both keywords on `_next_generation_attempt`; the satisfiability guard reads the forwarded text plus a found `slot_lookup` sentence and runs beside routing's `_slot_demand_unmet` |
| `streaming.py` | `_reason_family` literal set against `SCOPE_FAMILY_REASONS` | the set, with `reconstruction_scope_contradicts_answer` added |
| `tests/test_ambiguous_continuation.py`, `tests/test_gemini_eligibility.py`, `tests/test_chapter2_integration.py` | timeouts: agreement 300 (gates) or 120 (routing), writer 120 or 300 | agreement 300, writer 300, extraction 120; the v7-shaped helper drops the writer stage entry |

Reconciliation after the merge, commit `d0a3a10`: `reconstruction_scope_contradicts_answer` joins `REPAIRABLE_QUESTION_REASONS` beside its v1 sibling, so every scope-family code reaches a rung, as routing requires.

### 3.5 cost

The cost slice restructured `generate_candidate` and moved the static prompt text of four roles into system-instruction constants.
The gates, writer-context and judge-options slices edited that same inline text.
Seventeen blocks in `generation.py` conflicted.

| Region | Resolution |
|---|---|
| imports | both, plus `_option_needs_independent_support` for the verified-count rule |
| `FINDING_ADMISSION_REASK_REASONS` | `finding_scope_value_unsourced`, `finding_required_phrase_artifact` and `no_admissible_finding` |
| extractor call | cost structure (`system=EXTRACTOR_SYSTEM`); the writer-context sentences on `scope_evidence` and the uncertainty notation ported into `EXTRACTOR_INSTRUCTIONS` |
| standalone call | cost structure (`skip_reason`, `shadow_cohort`, `run_downstream`) with the judge-options re-ask inside the call block: one re-ask per unevidenced verdict, recorded in `provenance.standalone_verification_reask` |
| reconstructor and verifier prompts | cost structure; `CLAIM_TYPE_DEFINITIONS` (gates) ported into both instruction constants; the widened `interpretation_scope_applies_to_finding` test (writer-context) ported into `ANSWER_VERIFIER_INSTRUCTIONS` |
| `_generate_distractors` | judge-options version (closure precondition, free prefilter, fail-fast, malformed re-ask, rank-order stop, whole-set call). The cost slice's duplicate `validate_distractor` prefilter and `chunks` parameter are gone. The per-option call and its re-ask carry `system=OPTION_VERIFIER_SYSTEM`. `OPTION_VERIFIER_INSTRUCTIONS` carries `OPTION_ADMISSION_RULE` and `option_standalone_interpretable`. |
| option call plan | `_option_verification_call_plan` derives the cost slice's `provenance.option_verification_call_plan` (`rank-order-option-verification-v1`) from the judge-options stage record. `_option_verdict_verified` counts an option when `option_verdict_rejection_reason` is None and, for a compound option, the verdict is independent of the writer. |
| `_qa_gate_reasons` | cost `judged` structure with the gates `reconstruction_scope_reasons` and `claim_type_reasons`, and the writer-context binding pool (`question_context`, `context_only_texts`) |
| `streaming.py` | both registrations: the `standalone_det_` codes and the set codes (judge-options) and `WRITER_SLOT_UNAVAILABLE_REASONS` (cost); the cost layer entries before the scope family |
| `fixtures/fake-author.jsonl` | the three cost extractor fields added to the merged extractor line |
| `tests/test_gemini_batch.py` | batch capture matches markers against system plus prompt (cost) and ignores the option value markers (judge-options) |

Reconciliation after the merge, commits `8144aab` to `d7b91d9`:

- The two judge notes on the candidate record (`claim_type_note`, `reconstruction_scope_representation_note`) are null when the call plan skipped the judges.
- The cost tests script the whole-set verdict where the rank-order stop makes the call, use six proposals, and use the chapter 3 option verdict keys.
- The finding-bank test candidates cite their `scope_evidence`.
- `OPTION_VERIFIED_TARGET` stays 4 (both slices chose 4 for the answer-absent export).
- The batch revision test pins the shadow cohort off, because the entity ids changed with the contract versions and one candidate fell into the 5 percent cohort.
- The option re-ask sends the role system instruction, and the distractor resume path drops the stale `chunks` keyword.

## 4. Contract versions after integration

| Contract | Value | Owner |
|---|---|---|
| `GENERATION_PROMPT_VERSION` | `arctic-qa-generation-v23` | writer-context |
| `CANDIDATE_SCHEMA_VERSION` | `2.8.0` | writer-context and judge-options, one literal |
| `STANDALONE_VERIFICATION_CONTRACT_VERSION` | `source-blind-scientific-referent-v4` | judge-options |
| `OPTION_VERIFICATION_CONTRACT_VERSION` | `option-admitting-interpretation-v1` | judge-options |
| `ROUTING_CONTRACT_VERSION` | `bounded-failure-routing-v5` | routing |
| `NUMERIC_RULE_CONTRACT_VERSION` | `numeric-rule-source-support-v4` | gates |
| `ANSWER_AGREEMENT_PROMPT_VERSION` | `answer-agreement-judge-v2` | gates |
| `STANDALONE_CALIBRATION_SET_VERSION` | `standalone-calibration-v2` | gates |
| `CONTEXT_ONLY_EVIDENCE_CONTRACT_VERSION`, `REFERENT_SLOT_CONTRACT_VERSION`, `FINDING_ADMISSION_CONTRACT_VERSION` | v2 each | writer-context |
| Eligibility prompt v8, schema v4, re-screen prompt v2 | as landed | eligibility |
| Price config `config_id` | `arctic-gemini-eligibility-r1-config-v8` | gates and routing, one revision |
| `config/roles.v1.json` | schema 1.3.0 with `answer_judge` on Pro, `option_set_verifier`, `slot_lookup` | gates, judge-options, routing |

The `CANDIDATE_CONTRACTS` table:

- The `2.7.0` row pins the chapter 2 literals: prompt v22, routing v4, standalone v3, numeric v3, context-only v1, referent-slot v1, finding-admission v1. Every stored chapter 2 candidate validates under its own contract.
- The `2.8.0` row tracks the module constants of every slice and adds `option_verification_contract_version`.
- `tests/test_ch3_integration.py::test_the_reconciled_contract_versions` pins both rows.

## 5. The replays

### 5.1 The 139 chapter 2 candidates through the integrated gates

Command: `arctic-qa replay-chapter2-gates --evidence-dir <audit evidence> --report-file data/arctic-ch3-integration-r1/replay-chapter2-gates.json`.

| Measure | Value |
|---|---|
| Candidates, families | 139, 56 |
| Candidates whose reason set changed | 64 |
| Freed (zero gate reasons, standalone passed) | 16 in 10 families |
| Regressed (accepted before, rejected now) | 0 |
| Codes removed | `question_context_referent_unresolved` 26, `scope_qualifier_not_displayed` 16, `reconstruction_scope_not_source_bound` 12, `question_claim_type_disagreement` 8, `source_bound_numeric_rule_missing` 7, `scope_qualifier_missing` 6, `question_context_missing` 1, `reconstruction_disagreement` 1 |
| Codes added | `question_qualifier_not_evidence_bound` 4 |

The freed candidates are the same 16 the gates slice reported: `aqa-8aaeab6c`, `aqa-c45137c9` (0e6af4a9); `aqa-bfdf79ce`, `aqa-c491bc6d`, `aqa-3f6bcd96` (3185c3a7); `aqa-45b06ab5`, `aqa-64c1db14` (46dc1707); `aqa-303b02c6` (55c12122); `aqa-2c32a5d8` (7edb49fb); `aqa-b2d72a86`, `aqa-09374f78`, `aqa-615b6a2d` (b0a9366f); `aqa-cfd632a0` (b1a8778e); `aqa-d63cb289` (b477aebf); `aqa-4b25820e` (e3d2c979); `aqa-28c6d525` (fe612777).

The four added codes are the writer-context binding pool (audit 4.1 d, DG-5) on candidates `aqa-4a65dfd1` (330081a5) and `aqa-3d34636b`, `aqa-9609859f`, `aqa-74dea66c` (a690fc9d).
Each of the four already carried `answer_scope_not_source_bound`, so no candidate changes outcome.
The replay test pins this exact addition.

### 5.2 The 79 non-eligible papers

`data/arctic-ch3-integration-r1/replay-chapter2-eligibility.json`, from the eligibility slice fixture and the integrated rules:

| Outcome | Chapter 2 | Integrated |
|---|---|---|
| eligible | 0 | 0 |
| uncertain | 8 | 26 |
| excluded | 33 | 37 |
| screening error | 38 | 16, of which 13 are format-repairable |
| geography re-screen pool | 0 | 24 |

Every recorded exclusion stays excluded.
No paper becomes eligible without a new judgment.

### 5.3 The repeat-detector replay

`data/arctic-ch3-integration-r1/replay-chapter2-routing.json`, from the routing slice script on the integrated code:

| Measure | Routing slice | Integrated |
|---|---|---|
| Recorded repairs | 80 | 80 |
| Repeat-detector stops | 1 | 1 |
| Accepted items suppressed | 0 | 0 |
| Candidates with a scope defect | 62 (34 `display_verbatim`, 32 `not_in_evidence`) | 48 (20 `display_verbatim`, 32 `not_in_evidence`) |
| Numeric repair rides along | 12 | 12 |
| Agreement triggers barred | 4 | 4 |
| Guard stops | 0 | 3 `gate_contradiction_unroutable` |

The 14 fewer `display_verbatim` demands are the corrected display rule: the value is displayed, so there is no defect.
The three guard stops are `aqa-2c32a5d8`, `aqa-d63cb289` and `aqa-28c6d525`, whose only recorded code was `scope_qualifier_not_displayed`.
The integrated gates free all three, so the replay hands the router a code the new gate no longer emits, and the guard correctly finds nothing to repair.
In a real run those candidates are accepted at the first attempt and never reach routing.

## 6. The dry run, the cassette replay and the free-check proof

### 6.1 Mocked end-to-end dry run

Script: `data/arctic-ch3-integration-r1/dry_run.py`; output `dry-run.json`.
Every provider is the scripted fake provider on the public fixture source.

| Family | Calls | Skip reason | Shown |
|---|---|---|---|
| clean-full-suite | extractor, writer, standalone, reconstructor, verifier, distractor writer, 4 option verifiers, option set verifier | none | the full plan and the option call plan: 6 proposed, 4 verified, 2 deferred, set verdict mutually exclusive and choosable |
| free-check-failure | extractor, writer, standalone | `skipped_after_free_check_failure` | `pre_judge_gate_reasons` = `question_answer_leakage`; the standalone call kept; reconstructor and verifier skipped |
| standalone-failure | extractor, writer, standalone | `skipped_after_standalone_failure` | the two later Pro calls skipped |
| standalone-unevidenced-reask | extractor, writer, standalone, standalone, then the full suite | none | one re-ask with `CONTRACT_VIOLATION`, recorded in `standalone_verification_reask` |
| unavailable-slot | extractor, writer | `skipped_on_unavailable_slot` | no Pro call; `writer_slot_unavailable_location` first in the reason list |
| shadow-cohort-member | extractor, writer, standalone, reconstructor, verifier | `skipped_after_free_check_failure`, cohort true | the skipped calls made for the shadow; the persisted list is still the short-circuit list; `shadow_gate_reasons` recorded |

| Paper | Attempts | Shown |
|---|---|---|
| geography-rescreen | initial, geography_rescreen | the re-screen applied, four criteria frozen, decision eligible |
| format-reask | initial, format_repair | one bounded re-ask with the format errors named |
| failed-geography-never-rescreened | initial | no re-screen |

### 6.2 The calibration harness in cassette replay mode

`arctic-qa calibrate-standalone --mode record --provider fake` recorded `calibration-cassette-fake-judge.jsonl` from a scripted correct judge over the 41 gating rows of set v2.
`--mode replay` on that cassette passes the release rule: 20 of 20 must_pass rows pass, no must_fail violation, and the cassette binds the SHA-256 of the current `STANDALONE_SYSTEM`.
The judge in the cassette is the fake provider, so this proves the harness, not the prompt.
The live cassette is a paid, captain-approved run (deferred, section 10).

### 6.3 Phase D behind phase C

`tests/test_ch3_integration.py::test_the_pre_judge_free_checks_are_exactly_the_corrected_gates` runs `_pre_judge_gate_reasons` and the judged replay on all 139 candidates.
For every candidate the free list equals the judged list restricted to the nine free codes.
For the 16 freed candidates the free list is empty.
`test_the_free_checks_read_the_corrected_display_rule` shows a dash variant no longer kills a candidate for free.

## 7. Reason codes registered in the routing sets

| Code | Slice | Layer and set |
|---|---|---|
| `reconstruction_scope_contradicts_answer` | gates | context, `SCOPE_FAMILY_REASONS`, `REPAIRABLE_QUESTION_REASONS`, `_STANDALONE_DEPENDENT_REASONS` |
| `finding_scope_value_unsourced` | writer-context | finding, `ALTERNATIVE_FINDING_REASONS`, `IMMEDIATE_ALTERNATIVE_FINDING_REASONS` |
| `standalone_verdict_unevidenced` | judge-options | context, `ALTERNATIVE_FINDING_REASONS`, `IMMEDIATE_ALTERNATIVE_FINDING_REASONS` |
| `standalone_det_*` (5) | judge-options | context, `REPAIRABLE_QUESTION_REASONS`, the rungs of the unprefixed codes |
| `option_pool_empty_after_prefilter`, `closed_set_closure_not_source_established` | judge-options | `IMMEDIATE_ALTERNATIVE_FINDING_REASONS` |
| `option_set_not_mutually_exclusive`, `option_set_answer_not_choosable` | judge-options | options, `OPTION_REPAIR_REASONS`, `REPAIRABLE_QUESTION_REASONS` |
| `eligible_arctic_scope_dimension_unsupported`, `eligible_arctic_scope_phrase_not_specific` | eligibility | `ELIGIBILITY_CONTRACT_REASONS` only, no candidate rung |
| `gate_contradiction_unroutable`, `empty_diagnostic_unroutable` | routing | contract, terminal, `UNROUTABLE_OUTCOME_REASONS` |
| `writer_slot_unavailable_*` (10) | cost | context, `REPAIRABLE_QUESTION_REASONS`, `_SLOT_REASON_TYPES` |
| `no_admissible_finding`, `finding_required_phrase_artifact` | cost | finding, `ALTERNATIVE_FINDING_REASONS`, `IMMEDIATE_ALTERNATIVE_FINDING_REASONS` |
| `finding_context_over_budget` | cost | contract, `TERMINAL_GENERATION_REASONS` |

`test_every_new_candidate_code_has_one_routing_layer_entry` pins the table.

## 8. Cost projection and the expected effect

Script: `data/arctic-ch3-integration-r1/cost_projection.py`; output `cost-projection.json`.
Basis: the chapter 2 receipts (`cost-summary.json` of the audit evidence), the cost slice's receipt-measured steps, the gate replay and the per-call prices of audit 8.1.
Acceptance is held at 6 items in the step table, as the audit does.

| Step | Change | USD change | Spend after | USD per item at 6 |
|---|---|---|---|---|
| 0 | Chapter 2 as run | 0 | 19.99 | 3.33 |
| 1 | Judge call plan | -3.32 | 16.67 | 2.78 |
| 2 | Evidence once, system instructions, context budget | -2.70 | 13.98 | 2.33 |
| 3 | Finding bank | -2.64 | 11.34 | 1.89 |
| 4 | Second pass guard | -0.01 | 11.32 | 1.89 |
| 5 | Eligibility re-ask and re-screen | +0.81 | 12.14 | 2.02 |
| 6 | Option stage | -0.25 | 11.89 | 1.98 |
| 7 | Routing | -0.11 | 11.78 | 1.96 |
| 8 | Standalone contract v4 | -0.42 | 11.35 | 1.89 |
| 9 | Corrected gates, `scope_evidence` at freeze | -1.90 | 9.45 | 1.57 |

Steps 8 and 9 overlap steps 1 and 7 in part, by about USD 1, and the overlaps are named in the JSON rather than netted.
The audit's own step 5 booked the re-screens alone (USD 0.53); this projection books the 13 bounded re-asks too.

Yield scenarios at the projected spend, with one distractor round per extra item:

| Accepted items | USD per accepted item |
|---|---|
| 6 (chapter 2 yield) | 1.57 |
| 12 (audit low) | 0.83 |
| 15 (audit central) | 0.68 |
| 16 (one item per freed family) | 0.64 |
| 18 (audit high) | 0.58 |

The target of under USD 1.00 per accepted item needs the yield fixes, as the audit says.
At the chapter 2 yield the call-efficiency changes alone reach USD 1.57.
The expected yield effect is the sum the slices report: 16 freed candidates in 10 zero-yield families (gates), the context supply and the scope-binding class removed (writer-context), +1 to +2 items from the standalone prompt (judge-options), 24 papers back in the re-screen pool (eligibility), aimed scope repairs in place of blind ones (routing).
The measured answer comes from phase E.

The phase E plan is `data/arctic-ch3-integration-r1/phase-e-measurement-plan.md`: the six measures of audit section 5 phase E, the artifact each reads, the computation, the target and the chapter 2 value, plus the free measures the integrated runtime records.

## 9. Files touched outside the owned set

The owned set is integration: the merges, `data/arctic-ch3-integration-r1/`, `tests/test_ch3_integration.py`.

| File | Change |
|---|---|
| `src/arctic_qa/validation.py` | `standalone_verdict_is_evidence_bound`; the guard in `_standalone_reason_codes`; the reconciled `CANDIDATE_CONTRACTS`; `CHAPTER2_ROUTING_CONTRACT_VERSION` |
| `src/arctic_qa/generation.py` | the conflict resolutions of section 3.5; `_option_verification_call_plan`; the guarded judge notes; `system=OPTION_VERIFIER_SYSTEM` on the option re-ask |
| `src/arctic_qa/chapter2_replay.py` | the namespaced creation reason and the collapsed comparison |
| `src/arctic_qa/streaming.py` | the reconciled routing sets; `forwarded_slot_evidence` beside `evidence` and `unroutable`; `reconstruction_scope_contradicts_answer` in the scope family and the repairable set |
| `src/arctic_qa/gemini_eligibility.py` | one v8 branch for the Pro agreement judge and the writer timeout |
| `config/live-dataset-current-contract-v1.json` | `2.8.0` and `arctic-qa-generation-v23` |
| `docs/BENCHMARK_INPUT_CONTRACT.md` | the current-contract statement, the merged sections |
| `fixtures/standalone-calibration-v2.jsonl`, `fixtures/standalone-calibration-ch3-judge-slice-v1.jsonl` | headers on contract v4 and set v2; two ITP rows flipped to `pass` |
| `fixtures/fake-author.jsonl`, `fixtures/fake-verifier.jsonl` | the merged lines of section 3 |
| `tests/test_publication_export.py`, `tests/test_chapter2_integration.py` | the live export pins on v23 and 2.8.0 |
| `tests/test_cost_call_plan.py`, `tests/test_finding_bank.py`, `tests/test_gemini_batch.py`, `tests/test_standalone_calibration.py`, `tests/test_gate_corrections_ch3.py`, `tests/test_writer_context_chapter3.py`, `tests/test_writer_context_bundle.py`, `tests/test_ambiguous_continuation.py`, `tests/test_gemini_eligibility.py`, `tests/test_streaming.py`, `tests/test_cli_integration.py` | the adaptations named in section 3 |

## 10. Test results

Command: `nix develop -c bash -c 'PYTHONPATH=src pytest <files> -p no:cacheprovider -o addopts=""'`, four bounded foreground parts, after the last source change.

| Part | Result |
|---|---|
| `tests/` without the three slow files | 957 passed |
| `tests/test_cli_integration.py` | 98 passed |
| `tests/test_streaming.py` | 56 passed |
| `tests/test_model_broker.py` | 86 passed |
| `ruff check src tests` | clean |
| `ruff format --check src tests` | clean |

The replay tests ran against the audit evidence on this machine.

## 11. Rigor safeguards of the integration changes

- The evidence-rule guard only decides which verdicts the v4 rule reads. A v3 verdict keeps its recorded codes, and a live verdict is judged under v4. It admits nothing.
- The namespace collapse is a comparison in an offline replay tool. The runtime code is unchanged.
- The reconciled contract table makes stored chapter 2 candidates validate under their historical literals and new candidates under every slice's live version. A candidate under an unknown or mixed row still fails `generation_contract_version_mismatch`.
- The satisfiability guard and routing's unmet-slot test both route to the alternative-finding path. The union only removes revisions.
- The option stage keeps every judge-options rule; the cost slice's plan record is bookkeeping derived from it. `_option_verdict_verified` counts an option only when the chapter 3 verdict admits it and, for a compound option, a model other than the writer judged it.
- The two judge notes are null only when the plan skipped the judges; no gate reads them.
- The live export move selects the chapter 3 contract for new items. Chapter 2 items keep their own contract, and the export's contract test still rejects a superseded or mixed candidate.

## 12. Deviations and deferred items

| Item | Owner |
|---|---|
| The live calibration cassette of contract v4 on the judge model: about 41 calls, about USD 0.31 on Pro, through `calibrate-standalone --provider broker`. No judge prompt ships to a production run without it. | captain approves the spend; the integration crew or the next crew runs it |
| The chained ledger price transition to price config v8 before any live run (`docs/SHARED_MODEL_BROKER.md`). A live start with the old ledger hash fails closed. | captain and the operator of the next run |
| The two-labeler requirement for the calibration set is met by two model labelers, not two humans. | captain |
| `OPTION_VERIFIED_TARGET` is 4, not the audit's 3, to keep the answer-absent MCQ export. One constant. | captain |
| The reconstruction contradiction test is the disjoint-calendar-years rule, not the literal superset rule, because the literal rule rejected an accepted item. | captain, through the gates slice deviation |
| Compass points in the acronym allowlist beyond the audit's enumerated list. Kept. | captain |
| The eligibility re-screen and prompt v8 have no live calibration. The re-screen changes what enters the corpus. | captain, before the next run |
| The batch capture path prepares all six option requests in one round; chapter 3 runs on the streaming path. | a later cost slice, if the batch path is used again |
| The provider-side receipt lookup on a `request_key` retry, and a second `slot_lookup` after a resumed run. | a later broker slice |
| The resolvability shadow, the superlative closure shadow, the structural pre-screen shadow and the two-pass eligibility shadow become gates only after phase E measures them. | phase E, captain |
| Gemini context caching for the system instructions. | a later cost slice |

## 13. Done

Branch `fm/arctic-ch3-integration-r1`, clean, a fast-forward onto local `main` at `bd2fb22`.
No push, no PR, no paid call, no activation set, no ledger transition, no launcher.
The next run is the captain's decision.
