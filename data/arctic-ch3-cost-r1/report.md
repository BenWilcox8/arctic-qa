# Chapter 3 cost slice report (arctic-ch3-cost-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-cost-r1`, from local `main` at `bd2fb22`.
Scope: chapter 2 yield audit sections 4.4, 4.5, 4.9, section 8 cost plan steps 1 to 4 and 6, and the phase D rows `generate_candidate`, `Payloads and roles` and `Finding bank`.
No paid provider call was made.
No file under `/mnt/crdata` was changed.
The chapter 2 receipts and database were read in read-only mode for the measurements.

## 1. Audit findings addressed

| Audit id | Where | Status |
| --- | --- | --- |
| 4.4 (DG-1, R1, C1, standalone F5, RT-5) | judge call plan in `generate_candidate` | Done, with the standalone call kept |
| 4.9 C2 | evidence once, system instructions, duplicated verifier line, context budget | Done and measured from the receipts |
| 4.9 C9 | `cost_aware` never selected for a production run | Done |
| 4.5 (a) (FS-3, C3, RT-4) | ranked finding bank, served to the alternative-finding rung, second pass guard | Done |
| 4.5 (b) (FS-4) | `answer_basis_class` ranking, `no_admissible_finding` | Done |
| 4.5 (c) (FS-5 as amended) | freeze-time checks re-derived from the frozen text, `unavailable_in_source` short circuit | Done and measured |
| 4.5 (e) (C4 as amended) | admission-failure tabulation, structural pre-screen in shadow mode | Done and measured; the screen stays in shadow |
| 4.8 rank-order stop (C6 part 1) | option verification in rank order to the export need | Done, with a reserve for the option-repair rung |
| Cost plan steps 1, 2, 3, 4, 6 | projection from the recorded receipts | Done, section 6 |

Section 4.10 items were not implemented.
Rejection before the standalone call was not implemented.

## 2. Changes and the exact rule conditions

### 2.1 Judge call plan (`judge-call-plan-v1`)

Order in `generate_candidate` after the writer returns:

1. `_pre_judge_gate_reasons` runs the free checks.
   A free check is one whose verdict cannot differ between the pre-judge position and the judged position.
   The set is: `answer_evidence_not_located`, `answer_scope_not_source_bound`, `interpretation_span_contains_answer`, `benchmark_text_raw_source_artifact`, `scope_qualifier_not_displayed`, `question_context_invalid`, `scope_qualifier_missing`, `finding_answer_phrase_in_required_question_phrases`, `scope_qualifier_not_source_bound`, plus `question_answer_leakage`, the `benchmark_context_verification_reason` codes and the inherited-finding admission code.
   The codes are the same names, from the same functions and the same inputs as the QA gate.
   `source_bound_numeric_rule_missing` is not in the set, because the direct-literal branch reads the answer verifier's request id.
   `question_qualifier_binding_reason` is not in the set, because a verifier span can satisfy it.
2. When any writer `referent_slots` entry has state `unavailable_in_source`, no judge is called.
   The persisted reason list starts with `writer_slot_unavailable_<slot>` for each such slot, followed by the free codes.
3. Otherwise the standalone call is made for every candidate.
4. When the free checks or `_standalone_gate_reasons` report a code, the reconstructor and the answer verifier are not called.
   The candidate is persisted as `qa_gate_failed` with `reconstruction` and `answer_verification` null.
   The persisted list is the standalone codes followed by the free codes, in the QA gate order.
5. Otherwise the full suite runs and `_qa_gate_reasons` re-runs the free subset in its original position, so the persisted list keeps its shape.
6. A candidate is in the shadow cohort when `sha256([policy, run_id, entity_id])` maps below 0.05.
   A cohort member runs every call.
   Its persisted reason list is still the short-circuit list, so routing reads the same input for every candidate.
   The full-suite list is recorded in `provenance.judge_call_plan.shadow_gate_reasons` with the judge records.

`validate_candidate` returns a `qa_gate_failed` candidate's own reason list to routing, so the routing input is exactly the persisted list.
The routing registrations (streaming.py, one comment each): the ten `writer_slot_unavailable_*` codes are repairable and in the context layer with the referent-slot family; the four slot kinds are in `_SLOT_REASON_TYPES`; `no_admissible_finding` and `finding_required_phrase_artifact` are finding-layer alternative-finding codes; `finding_context_over_budget` is terminal in the contract layer.

Rigor safeguard.
No code, verdict or threshold changes.
The skipped calls are the calls whose result could not change a rejection.
Zero of the 6 accepted and zero of the 13 distractor-stage chapter 2 items fall in a skip class (section 6, `WOULD_HAVE_SKIPPED_A_GOOD_ITEM` is absent from the matrix).
The shadow cohort keeps the judge's recall measurable.

### 2.2 Payloads and roles

- `_context` and `_finding_context` emit the evidence spans once and drop the chunk `text` field.
  Every span keeps its id, offsets, `text_sha256` and text.
  The tiles cover every byte of the chunk, so the model sees the same bytes, the same ids and the same hashes.
- The static instruction blocks of the extractor, the reconstructor, the answer verifier and the option verifier are `EXTRACTOR_SYSTEM`, `RECONSTRUCTOR_SYSTEM`, `ANSWER_VERIFIER_SYSTEM` and `OPTION_VERIFIER_SYSTEM`.
  Each is `SYSTEM` plus the block that used to ride in the user prompt.
  The user prompt carries the evidence and the per-call records only.
  `_call_provenance` binds the role system text into the prompt hash, so a receipt still matches its exact request.
- The duplicated verifier line appears once.
- `MAX_FINDING_CONTEXT_CHARS` is 400,000, measured from the 252 recorded extractor payloads (largest 393,094 characters with the evidence twice, 257,607 once).
  A paper above it is rejected with `finding_context_over_budget` before any paid call.
- `assert_profile_allowed_for_phase` refuses `cost_aware` for the `away_production` phase at startup, beside the existing writer-judge separation assertion.

Rigor safeguard: same bytes, same span ids, same hashes, same instruction sentences; no gate changes.

### 2.3 Finding bank (`ranked-finding-bank-v1`)

- Table `finding_bank` holds every ranked candidate the extractor returns, with its 1-based position, span ids, the raw candidate, an admission status (`admissible`, `rejected`, `frozen`), the reason code and the frozen finding id.
- The bank key is `stable_id(contract, PROMPT_VERSION, sha256(EXTRACTOR_INSTRUCTIONS), admission contract, span contract, scope contract, arctic_scope)`.
  A row under another key is never served.
- An attempt without a frozen finding is served from the bank before any extractor call.
  A row is skipped when its spans intersect the attempt's exclusions.
  A served candidate is re-evaluated through `_evaluate_ranked_findings` with the current scope, the current interpretation spans and the full admission path.
  A row that fails is marked `rejected` with its code.
  The extractor is called only when no servable row remains.
- The extractor schema requires `admissible`, `answer_basis_class` and `source_blind_answer_basis` per candidate, outside `answer`.
  The order is: `study_internal_index` last, then a self-declared inadmissible candidate behind the admissible ones, then the model's rank.
  When every admissible candidate is `study_internal_index`, the attempt raises `no_admissible_finding`.
  The code is in `FINDING_ADMISSION_REASK_REASONS`, so the one free re-ask can still look for another finding; when a bank holds only such rows, the family records the code with no call.
- The second admission pass is spent only when the first pass rejected on a re-askable code and `_unexcluded_finding_spans` finds an eligible span outside the routed and admission exclusions.
- The prescreen (`structural-finding-prescreen-shadow-v1`) records one verdict per paper in `finding_prescreen_shadow` before the first extractor call and never blocks.

Rigor safeguard: ranking only reorders; a banked candidate passes the same admission contract as a fresh one; every new check only rejects; the basis fields never enter a judge payload or the frozen answer.

### 2.4 Freeze-time checks re-derived from the frozen text

Measured on the 81 frozen chapter 2 quotes (`freeze-time-signatures.json`):

| Signature | Quotes | On a good item |
| --- | ---: | ---: |
| No English finite verb, no interpretation span | 12 | 1 (a Russian sentence, family eb3fdf61) |
| No finite verb, no interpretation span, at most 8 words | 3 | 0 |
| Two column-interleave runs | 0 | 0 |
| Line-wrap hyphen inside the quote | 5 | 0 |
| Lost-space artifact in a required phrase (`[A-Z]{2,}[a-z]{3,}`) | 1 of 179 phrases (`SMLcoupled clouds`) | 0 |

Promoted: the short verb-less fragment rule as `finding_span_is_table_or_caption`, and the artifact rule as `finding_required_phrase_artifact` (re-askable).
The three fragment hits are the two `fdb38096` table rows the audit names and the `Herschel Island (104° ...)` entry.
Not promoted: the bare "no finite verb" rule, because the verb list is English only and it fires on an accepted Russian item; the column-interleave rule (no hit); the hyphen rule (five hits, none decisive, and the audit's version targeted phrases, where it has no hit); the x-mark table rule (no hit).

### 2.5 Option verifier call plan (`rank-order-option-verification-v1`)

- The validator's model-free option checks run through `validate_distractor` with no verdict before any paid call; a failing option is recorded in the prefilter with its code.
- Options are verified in the writer's order.
  A verdict counts when `contradiction_established` and `alternative_answer_search_passed` are true, `question_admits_option_as_correct` is false, and a compound or negated option has an independent verdict.
  Verification stops once `OPTION_VERIFIED_TARGET` verdicts count.
- The target is four, not three.
  `exporting._absent_mcq` builds the answer-absent MCQ from a fourth accepted distractor.
  A stop at three would remove that export form from every item.
  The target is one constant; the captain can set it to three and drop the absent form.
- The rest of the proposals are recorded as a reserve in `provenance.option_verification_call_plan`.
- The judge-options slice's per-option code composes through `validate_distractor` and `_option_verdict_verified`.
  With the chapter 2 writer proposing four options the plan saves nothing; it saves with their six proposals.

Rigor safeguard: floor of three, per-option hash binding, one call and one receipt per verified option, and the validator's own preconditions decide what counts.

## 3. Measurements

### 3.1 Token delta from the recorded request traces (`token-delta.json`)

Every chapter 2 request trace holds the exact prompt.
The new payload was rendered from the recorded one and the delta converted at the per-stage median characters per token from the receipts.

| Stage | Calls | Prompt tokens | Share removed | Tokens removed | Input saving USD |
| --- | ---: | ---: | ---: | ---: | ---: |
| finding_answer_extraction | 252 | 11,114,643 | 31.6 percent | 3,197,547 | 2.398 |
| question_generation | 160 | 922,741 | 14.9 percent | 133,548 | 0.100 |
| blinded_reconstruction | 136 | 540,524 | 22.6 percent | 115,749 | 0.232 |
| answer_verification | 134 | 735,385 | 16.4 percent | 117,792 | 0.236 |
| option_verification | 43 | 256,066 | 16.9 percent | 43,531 | 0.087 |
| distractor_generation, repair | 14 | 69,130 | 17.6 percent | 12,535 | 0.009 |
| Total | | | | | 3.062 |

The audit's inference was USD 2.5 to 3.8.
The measured figure is USD 3.06 unscaled, USD 2.70 after the judge calls the plan skips.
Moving the static instructions to the system instruction removes no tokens; it is enabling work for a caching path.

### 3.2 Admission-failure codes of the 65 finding-less papers (audit 4.5 e)

From the chapter 2 rejection ledger: `finding_span_figure_defined_referent` 27, `finding_evidence_span_not_found` 14, `alternative_finding_not_distinct` 9, `eligible_arctic_scope_missing_from_finding` 6, `extractor_response_invalid` 4, `finding_evidence_quote_excludes_finding` 2, `eligible_arctic_scope_finding_unbound` 2, operational 3 on one paper.
The extractor chose figure-referent sentences or cited a span id that does not exist.
A structural screen on the spans cannot catch either.

### 3.3 Pre-screen shadow replay (`prescreen-shadow.json`)

| Group | Papers | Would reject | Fire rate |
| --- | ---: | ---: | ---: |
| Froze a finding (56 families) | 56 | 12 | 21.4 percent |
| Froze nothing | 62 measured of 64 | 11 | 17.7 percent |

The screen fires more often on the papers that produced findings, and one of its hits (family eb3fdf61) produced a distractor-stage item.
Six of the twelve hits are Cyrillic papers, where the English verb list cannot see a sentence.
The screen stays in shadow, booked at USD 0, and must not be promoted in this form.

## 4. Expected effect

Acceptance: none of the changes admits anything, so the accepted set at the chapter 2 gates is unchanged (6 items).
The bank makes the alternative-finding rung cheap, so the family c0f8d9a4 pattern (an item from a second finding) costs no extraction.
`no_admissible_finding` and the two promoted checks only reject; on the chapter 2 data they reject 0 good items.

Cost, per step, on the same 200 papers at fixed yield (`cost-projection.json`):

| Step | Change | Spend after USD | USD per accepted item | Audit expected |
| --- | --- | ---: | ---: | ---: |
| 0 | Chapter 2 as run | 19.99 | 3.33 | 19.99 |
| 1 | Judge call plan: 115 reconstruction and 115 verification calls skipped, 17 standalone calls skipped, shadow cohort USD 0.17 | 16.68 | 2.78 | 16.49 |
| 2 | Evidence once, measured | 13.98 | 2.33 | 13.99 |
| 3 | Finding bank: 65 extraction calls after a family's first candidate | 11.34 | 1.89 | 12.89 |
| 4 | Second pass guard: 3 re-asks in separable-scope families (upper bound 14 calls, USD 0.50) | 11.32 | 1.89 | 12.69 |
| 6 | Option call plan: 0 calls at four proposals | 11.32 | 1.89 | 12.89 |

Step 1 skip classes on the 139 candidates: 17 slot, 71 free check, 27 standalone, 24 full suite.
Step 3 assumes the first extraction returns the ranked candidates the new prompt asks for; the audit booked 33 calls (USD 1.1) for the same rung, the receipts show 65 calls after the first candidate (audit RT-4 counts the same 65).
The projection is USD 11.3, under the audit's USD 12 to 13, because step 3 uses the receipts rather than the audit's halved booking.
With the sibling slices' yield fixes (12 to 18 items) the same spend gives USD 0.63 to 0.94 per accepted item.

## 5. Deviations from the audit text

- The verified-distractor target is four, not three (section 2.5).
- In the unrestricted evidence branch the spans stay and the chunk text goes, not the reverse: a role selects a span id and the pipeline resolves the quote against the span text, so the spans must carry the text.
- `source_bound_numeric_rule_missing` is not a free check (section 2.1).
- On a writer-declared unavailable slot the standalone codes are absent from the persisted list, also for a cohort member, so routing sees one input per class.
- `no_admissible_finding` is re-askable once through the existing free re-ask, and terminal when the bank holds only study-internal rows.

## 6. Contract versions

Owned and added: `judge-call-plan-v1`, `judge-short-circuit-shadow-cohort-v1`, `ranked-finding-bank-v1`, `structural-finding-prescreen-shadow-v1`, `rank-order-option-verification-v1`.
Price config: unchanged (no price or timeout changed), so no v8 revision and no ledger transition.
Not bumped: `GENERATION_PROMPT_VERSION` (writer-context slice), the standalone and option contracts (judge-options slice), the routing contract (routing slice), `CANDIDATE_SCHEMA_VERSION`.
Integration should bump the prompt version once for every slice, because the extractor and judge prompts changed here.

## 7. Files touched outside the owned set

- `src/arctic_qa/streaming.py`: `assert_profile_allowed_for_phase` in `_resolve_model_roles`; routing set registrations with one comment each (`REPAIRABLE_QUESTION_REASONS`, `TERMINAL_GENERATION_REASONS`, `ALTERNATIVE_FINDING_REASONS`, `IMMEDIATE_ALTERNATIVE_FINDING_REASONS`, `_SLOT_REASON_TYPES`, `_reason_family`, `_failure_layer`).
- `src/arctic_qa/db.py`: tables `finding_bank` and `finding_prescreen_shadow` (created by the schema on open; no migration).
- `src/arctic_qa/providers.py`: the fake provider reads the system instruction and the prompt as one text.
- `src/arctic_qa/model_roles.py`: `NON_PRODUCTION_PROFILES`, `assert_profile_allowed_for_phase`.
- `fixtures/fake-author.jsonl`: the three new extractor fields.
- `tests/test_streaming.py`, `tests/test_gemini_batch.py`: the scripted transports read both request parts; one call-count expectation follows the plan.
- `docs/STREAMING_DATASET.md`: section "Chapter 3 call plan".
- `validation.py`: untouched.
- Shared-file overlap for integration: the extractor prompt text (writer-context slice edits the scope sentence), `_generate_distractors` (judge-options slice), the routing sets (routing slice).

## 8. Tests

New: `tests/test_cost_call_plan.py` (16 tests), `tests/test_finding_bank.py` (15 tests), harness `tests/cost_plan_harness.py`.
Whole suite: see the final commit message for the result of the bounded parts.

## 9. Deferred items

| Item | Owner |
| --- | --- |
| 4.5 (d) bare study-local label as a required phrase | writer-context slice or integration |
| Gemini context caching for the system instructions | a later cost slice, after a caching path exists in `model_broker.py` |
| `maximum_output_tokens` cap per schema | after a calibration run (audit C9) |
| Six proposals and the false-flag fix that make the option call plan save calls | judge-options slice |
| Reading the bank from `_prior_finding_span_ids` so a family with no frozen finding can still take an alternative attempt | routing slice |
| A language-aware prose test before any promotion of the pre-screen | measurement phase E |
