# arctic-ch3-gates-r1: deterministic gates, display rules, reconstruction record, calibration harness

Branch: `fm/arctic-ch3-gates-r1`, from local `main` at `bd2fb22`.
Source: `data/arctic-ch2-yield-audit-r1/report.md`, sections 4.3, 4.5, 4.8, and the section 5 rows "Standalone calibration", "Deterministic gates", "Display rules" and "Reconstruction record".
No paid provider call was made.
No file under `/mnt/crdata` and no legacy or chapter 2 data changed.
No production run starts from this work.

## 1. Result in five lines

1. The deterministic replay of the 139 recorded chapter 2 candidates frees 16 candidates in 10 families. The audit expected about 14 in 9. Every freed candidate had passed the Pro standalone judge. No candidate regresses and no new code fires on any candidate.
2. The gate corrections remove 77 code firings across the run: 26 `question_context_referent_unresolved`, 16 `scope_qualifier_not_displayed`, 12 `reconstruction_scope_not_source_bound`, 8 `question_claim_type_disagreement`, 7 `source_bound_numeric_rule_missing`, 6 `scope_qualifier_missing`, 1 `question_context_missing`, 1 `reconstruction_disagreement`.
3. The deterministic "number plus unit against bare number" tier settles 17 of the 40 answer-agreement cases that chapter 2 sent to the flash-lite judge. The remaining judge calls move to the Pro model with three worked examples in the prompt.
4. The standalone calibration set is re-labeled with two labelers as fields, gains a held-out slice of 16 chapter 2 rows, and is wired to the live judge through a record-and-replay cassette. No test makes a live call.
5. The whole suite is green in four bounded foreground parts (section 8).

## 2. Audit findings addressed, by id

| Finding | What landed | Where |
|---|---|---|
| 4.3 c, DG-3, SG-4 | Acronym gloss matcher: hyphen and slash compounds split, glue words skipped, every parenthetical scanned, multi-token gloss "(ARM NSA)", reverse form "TOKEN (gloss)", trailing "version N" skipped, copula definitions, reverse definitions ("abbreviated as FYI"), gene prefixes ("bla TEM"). No chemical-formula shape rule. | `validation._acronym_has_expansion`, `_initials_match`, `_unresolved_acronym_tokens` |
| 4.3 c allowlist | CFU, RMS, RMSD, SE, SEM, CMIP5, CMIP6, PM10, NH4, SO42 added as the audit names. Compass points (NE, NW, SW, NNE, ENE, ESE, SSE, SSW, WSW, WNW, NNW) added under the allowlist rule "one meaning across the natural sciences, never a study label". No site, cruise, run, instrument or strain code. LNG and CO were not added. | `validation._NON_ACRONYM_TOKENS` |
| 4.3 a, DG-2, 4.8 display rules | One display projection: NFKC, dash variants to "-", soft hyphens removed, line-break hyphens repaired, whitespace inside an abbreviation or unit token removed, casefold, whitespace collapsed. A display-only token-window test bounded to one noun phrase. Rejection, never narrowing. | `validation._display_comparison_projection`, `_display_token_window_match`, `scope_phrase_is_displayed` |
| 4.3 d, DG-4 | Numeric uncertainty notation `VALUE ± TOLERANCE UNIT` and `VALUE UNIT (sd = TOLERANCE)`, tolerance inherits the unit only inside one adjacent clause and only when the basis states no unit of its own. `SAFE_UNIT_SPELLINGS` extended, not removed. | `validation._uncertainty_clause_quantities`, `_contains_quantity`, `_contains_quantity_literal`, `_tolerance_basis_carries_unit` |
| 4.3 e, DG-6, R4 | Claim-type definitions in the three schemas and the two judge prompts. Incompatibility test: only `causal` against another label rejects. `causal_overclaim` fires on either judge label. Both labels recorded in `claim_type_note`. | `validation.claim_type_reasons`, `claim_type_note`, `CLAIM_TYPE_DEFINITIONS`; `generation` schemas and prompts |
| 4.3 f, R2 | `allow_empty=True` for the reconstruction record at both call sites, pinned by a two-site spy test. | `validation.reconstruction_scope_reasons`; tests |
| 4.3 f, R3, 4.5 | `reconstruction_scope_contradicts_answer` replaces the wording test for paired values; a content-token superset or an acronym of the answer value passes. | `validation.reconstruction_scope_reasons`, `reconstruction_scope_representation_note` |
| 4.5 R5 part 1 | Deterministic tier for "number plus unit against bare number". | `validation._answer_rule_quantity_matches_rebuilt` |
| 4.5 R5 part 3 | Fallback judge on the Pro model, prompt v2 with three worked examples. | `validation.ANSWER_AGREEMENT_SYSTEM`, `config/roles.v1.json`, `config/gemini-eligibility-v1.json` v8 |
| 4.5 R6 | Directional matcher: content test in place of the one-word dictionary test. Regression and must-fail control shipped. | `validation._directional_content_matches_answer` |
| Standalone F6, section 5 "Standalone calibration" | `calibrate-standalone` command, record and replay modes, cassette bound to the prompt hash, must-fail controls, two labelers as fields, held-out slice. | `standalone_calibration.py`, `cli.py`, `fixtures/standalone-calibration-v2.jsonl`, `docs/STANDALONE_CALIBRATION.md` |
| Section 5 replay | `replay-chapter2-gates` command and a test that pins the exact freed set. | `chapter2_replay.py`, `cli.py`, `tests/test_gate_corrections_ch3.py` |

Nothing from section 4.10 was implemented.
The chemical-formula shape rule, scope narrowing, the frozen-value rewrite and the superset widening on `answer.scope` and `verification.scope` are absent.

## 3. Exact conditions of every new or changed rule

### 3.1 Acronym gloss matcher (contract `deterministic-context-rules-v2`)

A token matches `\b[A-Z][A-Z0-9]{1,7}\b` and is not in the allowlist.
The token is resolved when one of these holds.

1. A parenthetical of acronym-shaped tokens contains the token, and the words before the parenthetical spell the letters of every token in it. The matcher reads words from the end. Each word gives its first letter. A hyphen or slash compound gives one letter for the whole or one letter per part. A glue word (of, the, and, for, in, on, at, a, an, to) can be skipped. A trailing "version N", "ver N" or "v N" is skipped. A trailing bare number equal to the token's digits is skipped. Nothing else is skipped.
2. The reverse form `TOKEN (gloss)` holds, with the gloss words spelling the letters.
3. The token is followed by a definitional phrase and a noun phrase: means, denotes, is short for, stands for, refers to, identifies, is defined as, is the, are the, is a, is an, are, was the, were the, represents, designates, corresponds to.
4. The token follows "abbreviated as", "denoted as", "designated as", "referred to as", "termed", "known as", "labelled", "labeled", "hereafter", "abbreviation" or "acronym".
5. The token follows a lowercase gene prefix: bla, mec, van.

Verified catches that survive: CHINARE, AKMA3, CESM2 unglossed, PIOMAS unglossed, DBO3, KMM, CMP22, GHSZ unglossed, PS80, PS92, ITP unglossed, ARM NSA unglossed.
The replay keeps 24 of the 50 chapter 2 `question_context_referent_unresolved` firings, all on unglossed codes.

### 3.2 Display rules

`scope_phrase_is_displayed(value, question, question_context)` is true when, for the question or for the context, one of these holds.

1. Contiguous containment under the display projection.
2. Contiguous containment under the source-binding projection. This keeps every v1 acceptance.
3. The token-window test: the value has no coordinator or clause punctuation. Its content tokens (function words removed) appear in order in the displayed text. Between two matched tokens there is no coordinator (and, or, nor, but, versus, vs, than) and no clause punctuation (comma, semicolon, colon, parenthesis, bracket, question mark, exclamation mark, slash, sentence-final period). The window is at most `2n + 2` tokens for `n` content tokens. A parenthetical acronym gloss is transparent.

The display projection removes whitespace inside a token only when a short abbreviation (one to three letters, uppercase first) or a single letter precedes digits, or when any letter precedes a signed exponent.
"PM 10" becomes "PM10", "CO 2" becomes "CO2", "kg m −3" becomes "kg m-3".
"station 4" and "of 12 samples" stay two tokens.

Source binding (`selected-evidence-literal-scope-v4`) keeps `_scope_comparison_projection` and the contiguous test unchanged.

### 3.3 Numeric uncertainty notation (contract `numeric-rule-source-support-v4`)

`_contains_quantity` and `_contains_quantity_literal` accept a value or a tolerance when it sits inside one adjacent clause that unites it with the rule's unit: `VALUE ± TOLERANCE UNIT` (also `+/-`, `+-`) or `VALUE UNIT (sd = TOLERANCE)` (also se, sem, s.d., s.e., σ).
`_tolerance_basis_carries_unit(rule, evidence)` is true when the basis carries the unit (v3 rule), or when all of these hold: the basis is a bare uncertainty clause with no unit token of its own, the evidence carries the rule value and the rule tolerance inside one adjacent clause with the rule unit, and the basis text is in the evidence.
"13.0 degC (sd = 5%)" fails: the basis states its own unit.
"1.0 t of methane (0.3 t)" with basis "0.3" fails: no adjacent clause unites the numbers.

### 3.4 Claim type

Labels: observation, association, causal, definition, defined in `CLAIM_TYPE_DEFINITIONS` and placed in the extractor, reconstructor and verifier schemas and in the reconstructor and verifier prompts.
`question_claim_type_disagreement` fires only for the pairs causal-observation, causal-association, causal-definition.
`causal_overclaim` fires when the frozen answer is an observation or an association and either judge label is causal.
Every other pair is recorded in `claim_type_note` on the candidate and in the rejection diagnostic detail.
The 8 recorded chapter 2 pairs (observation-association 4, observation-definition 3, association-observation 1) are all compatible.
In `validate_candidate` an incompatible pair is now `rejected`, not `unresolved`.

### 3.5 Reconstruction record (contract `reconstruction-record-v2`)

For each dimension where the reconstructor states a value:

1. If the answer states a value for the same dimension and one value's content tokens are a subset of the other's, or one is an acronym whose letters the other spells, the value passes.
2. Else, if both values name calendar years (ranges expanded) and share none, `reconstruction_scope_contradicts_answer` fires.
3. Else, the value keeps the verbatim binding of v1 (`reconstruction_scope_not_source_bound`).

An all-null scope passes (`allow_empty=True`).
A reconstructor value the answer does not pair keeps the verbatim binding.
The audit's literal rule, "reject when neither value is a content-token superset of the other", was tried first on the 139 candidates.
It fired on 35 candidates, including the accepted item `aqa-0a79dc67` ("little auks" against "recorded positions") and the pairs "2012" against "spring", "Norwegian Sea" against "NS leg", "humus soils" against "soil P".
Those pairs describe different aspects of one setting, not contradictions.
The year test is the contradiction the audit named, "a different year than the answer claims".
Section 9 lists this as a deviation.

### 3.6 Number plus unit against bare number

`_answer_rule_quantity_matches_rebuilt` is true when all of these hold: the rule has a parsable canonical value and a unit; `answer.text` is exactly that value followed by that unit; the rebuilt text is exactly the same literal, or the literal plus a spelling-equivalent unit; neither text carries a negation.
The literal must match textually, so "24.0" against "24 species" and "25" both go on to the judge or fail.
The tier defers when the reconstructor's own typed quantity names another value or another unit.
It also settles alternatives in `reconstruction_has_competing_alternatives`.

### 3.7 Directional matcher

The whole-string dictionary test is replaced by: every content token of the rebuilt text that is not a directional token must be a token of `answer.text` or of a variant.
"higher krill production" matches; "higher krill mortality" fails; "lower krill production" and "not higher krill production" fail on the unchanged direction and negation guards.
As the audit predicted, this recovers no chapter 2 candidate: `aqa-58c38331` still fails because its frozen answer "higher krill" is truncated.

### 3.8 Fallback judge

`ANSWER_AGREEMENT_PROMPT_VERSION` is `answer-agreement-judge-v2`.
The prompt adds the three worked examples of audit 4.5 R5.
`answer_agreement_resolves` accepts the v1 prompt pair for stored chapter 2 receipts and the v2 pair for new calls.
`config/roles.v1.json` (schema 1.3.0) sets `answer_judge` to `gemini-3.1-pro-preview` in every profile.
`config/gemini-eligibility-v1.json` is revision `arctic-gemini-eligibility-r1-config-v8`: the `answer_agreement` stage carries the Pro model block with `maximum_output_tokens` 128 and the pinned 300 second timeout.
The batch pipeline records the Pro batch price (USD 1.00 in, USD 6.00 out per million tokens, half the standard price).

### 3.9 Calibration harness

`arctic-qa calibrate-standalone --mode record` calls the `standalone_verifier` role once per gating row with the exact `DISPLAYED_TASK` prompt and the exact call parameters of `generate_candidate`, validates every response against the role schema, and writes a cassette whose header binds the SHA-256 of `STANDALONE_SYSTEM`, the set version, the model and the parameters.
`--mode replay` applies `standalone_gate_decision` to every recorded response and fails the release rule when a `must_fail` row passes or fewer than 80 percent of `must_pass` rows pass.
A cassette recorded under another prompt text, another set version, a missing row or a changed prompt is refused.
The fixture header, the labelers and the slices are described in `docs/STANDALONE_CALIBRATION.md`.

Fixture v2 counts: core slice 14 must_pass, 12 must_fail, 1 disputed; held-out slice 6 must_pass, 9 must_fail, 1 disputed.
Labeler 1 is the r15 audit verdict (core) or the chapter 2 family analyst (held-out).
Labeler 2 is this crewmate.
The two disputed rows: `aqa-cb7f5999` (the stem states the confidence interval that fixes the answer) and `aqa-508641f4` ("the best approximating model" has no antecedent).
`aqa-773e25a0` was not added: the analysts judged it accept-as-written, but "CMP22" is an unglossed instrument code and the deterministic half rejects it by design.

## 4. Rigor safeguard per change

- Acronym matcher: a token still needs a literal gloss, a definition or a gene prefix in the displayed text. Every catch on an unglossed code in chapter 2 survives (24 of 50 firings remain, all on codes with no gloss). The allowlist gains no site, cruise, run, instrument or strain code.
- Display rules: they apply only to what the reader sees. Source binding keeps the strict contiguous projection, so no scope value can be stitched from two evidence sentences. The token window fails on a coordinator, so "adult females" is not displayed by "adult males and females". A value absent from the text still fails ("February 2009", "ringed seals", "northern Sweden" against "Abisko, Sweden").
- Numeric notation: the value, the unit and the tolerance must still occur in the frozen span. The unit is inherited only inside one adjacent clause, and never when the basis names its own unit. A tolerance literal absent from the evidence and a changed value still fail.
- Claim type: the only scientific risk the old code guarded, a causal overclaim, is caught on either judge label, which is more than the v22 rule. A taxonomy split between two independent labelers is a note.
- Reconstruction record: the reconstructor asserts nothing about the paper. `answer.scope`, `verification.scope` and `question_qualifier_binding_reason` keep the verbatim test. The year contradiction is a rejection the v1 rule could not make. Every reconstructor value the answer does not pair, and every paired value that is neither entailed nor year-contradicting, keeps the verbatim binding.
- Number plus unit tier: the frozen `numeric_rule` value and unit are already verbatim in the span. The tier needs textual equality of the literal, so a changed value, a changed precision, a changed unit, an added qualifier ("at least 452 kPa") or a dropped uncertainty ("0.81 +/- 0.26 ng/m3" against "0.81") still goes to the judge or fails.
- Directional matcher: stricter than the line it replaces. "higher krill mortality" is excluded on purpose, not by accident.
- Fallback judge: the decision rule is unchanged; the strongest judge replaces the weakest on the cases determinism cannot settle.
- Calibration: no relaxation of the judge prompt can ship while a must-fail control passes, and a cassette recorded under another prompt is refused.

## 5. The deterministic replay

Command: `arctic-qa replay-chapter2-gates --evidence-dir <audit evidence> --report-file <json>`.
The full report is `data/arctic-ch3-gates-r1/replay-chapter2-gates.json`.
The test `test_the_chapter_2_replay_frees_exactly_the_recorded_candidates` pins the list and skips when the evidence directory is absent.

Freed candidates (16, in 10 families; every one had `standalone_verification.pass = true`):

| Family | Candidate | Recorded gate reasons removed |
|---|---|---|
| 0e6af4a9 | aqa-8aaeab6c42e173b2edc6 | scope_qualifier_not_displayed, question_context_referent_unresolved |
| 0e6af4a9 | aqa-c45137c902bda3e1d76f | scope_qualifier_not_displayed, question_context_referent_unresolved |
| 3185c3a7 | aqa-bfdf79ceb8e0c4c0e0be | question_context_referent_unresolved, scope_qualifier_missing |
| 3185c3a7 | aqa-c491bc6dbdc60f2f507f | question_context_referent_unresolved, scope_qualifier_missing |
| 3185c3a7 | aqa-3f6bcd96f09d48019173 | question_context_referent_unresolved, scope_qualifier_missing |
| 46dc1707 | aqa-45b06ab59fd1cb1e831f | question_context_referent_unresolved |
| 46dc1707 | aqa-64c1db140f14e9f2f3fe | question_context_referent_unresolved |
| 55c12122 | aqa-303b02c65012fdcd4a61 | scope_qualifier_not_displayed, question_claim_type_disagreement |
| 7edb49fb | aqa-2c32a5d8907f8343afbe | scope_qualifier_not_displayed |
| b0a9366f | aqa-b2d72a864493c406263f | question_context_referent_unresolved |
| b0a9366f | aqa-09374f7825097bfab2c2 | question_context_referent_unresolved |
| b0a9366f | aqa-615b6a2d2f6ab21e7993 | question_context_missing |
| b1a8778e | aqa-cfd632a0e688f6299c59 | source_bound_numeric_rule_missing |
| b477aebf | aqa-d63cb2893719e9cb344d | scope_qualifier_not_displayed |
| e3d2c979 | aqa-4b25820e40f0c5f1494e | question_claim_type_disagreement |
| fe612777 | aqa-28c6d5253470c8681a8f | scope_qualifier_not_displayed |

Regressions: none. Added code firings: none. Every accepted chapter 2 item still passes.

Candidates the audit named that stay rejected, and why:

- e02e286a `aqa-91e76a3e`, `aqa-e176dcef`: the reconstruction code is gone; `question_qualifier_not_evidence_bound` remains and belongs to the writer-context slice (context-only spans into the qualifier pool).
- efcc5fc4 `aqa-9d37e524`, `aqa-08a52f1a`, `aqa-3a404178`: the BSR code is gone; `scope.geography = "offshore N Svalbard"` is not displayed by "north of Svalbard". The routing slice's `scope_display_repair` rung owns it.
- 46dc1707 `aqa-0d9d6939`: the frozen population "SMLcoupled clouds" is a lost-space artifact; the cost slice's freeze-time artifact check owns it.
- a690fc9d: "API ZYM" and "KMM 9724T" stay unresolved as the audit prescribes; the family also fails on answer scope binding.

## 6. Expected effect on acceptance and cost

- Acceptance: 16 chapter 2 candidates in 10 families end with zero gate reasons after passing all three Pro judges. All 10 families produced zero items in chapter 2. At the run's realized conversion (about one accepted item per family that clears the gate), the same 200 papers yield about 6 to 16 more accepted items on top of the 6 recorded, before the sibling slices' fixes.
- Retry waste: the families that spent attempts on defects no rewrite could reach stop doing so: 3185c3a7, 46dc1707, efcc5fc4 (USD 0.96 on the tokenizer), 7edb49fb (USD 0.34 on a dash), e3d2c979 (USD 0.48 on a claim-type label).
- Fallback judge: 17 of 40 judge calls become deterministic. The remaining calls cost about USD 0.0002 each on Pro instead of USD 0.00003 on flash-lite, about USD 0.005 per run. A spurious "no" from the weak judge cost a whole new attempt (USD 0.044); two of its four "no" verdicts in chapter 2 were errors.
- Cost per accepted item: at the chapter 2 spend of USD 19.995 and 12 to 22 accepted items, USD 0.9 to 1.7 per item from this slice alone. The cost slice's short-circuit removes the judge spend on condemned candidates on top of that.

## 7. Files touched outside the owned set

Owned: `validation.py`, `_qa_gate_reasons` in `generation.py`, the calibration fixture and CLI, their tests.

- `src/arctic_qa/generation.py` outside `_qa_gate_reasons`: `CLAIM_TYPE_DEFINITIONS` in three schema descriptions and two prompts (reconstructor, verifier); `claim_type_note` and `reconstruction_scope_representation_note` on the candidate record; imports.
- `src/arctic_qa/streaming.py`: `reconstruction_scope_contradicts_answer` registered in `_STANDALONE_DEPENDENT_REASONS` and `_reason_family` (scope), with comments. No routing logic changed.
- `src/arctic_qa/cli.py`: the `calibrate-standalone` and `replay-chapter2-gates` commands and handlers.
- `config/roles.v1.json`: `answer_judge` to the Pro model in every profile, schema 1.3.0.
- `config/gemini-eligibility-v1.json`: revision v8, `answer_agreement` stage on the Pro model. A chained ledger price transition is needed before a live run resumes (section 9).
- `src/arctic_qa/gemini_eligibility.py`: v8 accepted; `_validate_pro_answer_agreement_config`.
- `src/arctic_qa/gemini_batch.py`: the agreement batch price is pinned per model (`BATCH_AGREEMENT_PRICE_RECORDS`) instead of to flash-lite.
- `AGENTS.md`: one pointer to `docs/STANDALONE_CALIBRATION.md`.
- Tests updated for the Pro judge and the v8 revision: `test_ambiguous_continuation.py` (the flash-lite max-tokens incident replays under a v7-shaped configuration), `test_broker_provider.py`, `test_chapter2_integration.py`, `test_gemini_batch.py`, `test_gemini_eligibility.py`, `test_streaming.py` (v4 pin, and the 1043 counterfactual no longer rejects the reconstruction record on wording), `test_cli_integration.py` (v4 pin).
- New: `src/arctic_qa/standalone_calibration.py`, `src/arctic_qa/chapter2_replay.py`, `fixtures/standalone-calibration-v2.jsonl`, `docs/STANDALONE_CALIBRATION.md`, `tests/test_gate_corrections_ch3.py`.

Contract versions bumped: `NUMERIC_RULE_CONTRACT_VERSION` v4, `DETERMINISTIC_CONTEXT_RULES_VERSION` v2 (new constant), `RECONSTRUCTION_RECORD_CONTRACT_VERSION` v2 (new constant), `STANDALONE_CALIBRATION_SET_VERSION` v2, `ANSWER_AGREEMENT_PROMPT_VERSION` v2, `REJECTION_DIAGNOSTIC_CONTRACT_VERSION` v2 (no slice owns it; it records the three rule versions).
No sibling version was bumped.

## 8. Test results

Whole suite, four bounded foreground parts, after the last source change:

| Part | Result |
|---|---|
| Every test file except the three below, one process | 186 passed |
| `tests/test_cli_integration.py` | 92 passed |
| `tests/test_streaming.py` | 56 passed |
| `tests/test_model_broker.py` | 86 passed |

`tests/test_gate_corrections_ch3.py` (new) and `tests/test_standalone_calibration.py` (rewritten) are inside the first part.
The replay test ran against the audit evidence on this machine.
The calibration record and replay tests ran with the fake provider.

## 9. Deviations and deferred items, with owners

- Deviation, reconstruction contradiction test (section 3.5): narrowed from "neither value is a superset" to "disjoint calendar years", because the literal rule rejected 35 candidates including an accepted item. Owner of the decision: the captain, through the integration crew. The literal rule is one line to restore.
- Deviation, allowlist: compass points added beyond the audit's enumerated list, under the allowlist's own rule. Owner: integration crew, to keep or drop.
- Deferred, price config v8 ledger transition: `config/gemini-eligibility-v1.json` is revision v8. Before the chapter 2 run resumes or a chapter 3 run starts, the operator must apply a chained ledger price transition (`docs/SHARED_MODEL_BROKER.md`, reviewed configuration transitions). Owner: integration crew and the captain. Until then a live start with the old ledger hash fails closed.
- Deferred, 2.7.0 contract pin: `CANDIDATE_CONTRACTS["2.7.0"]` still binds the live `NUMERIC_RULE_CONTRACT_VERSION` (v4), so re-validation of a stored chapter 2 candidate (v3) reports `generation_contract_version_mismatch`. When the writer-context slice introduces schema 2.8.0, pin 2.7.0 to `CHAPTER2_NUMERIC_RULE_CONTRACT_VERSION` and prompt v22. Owner: integration crew.
- Deferred, live calibration cassette: the harness is built; recording costs about 41 judge calls (about USD 0.31 on Pro). Owner: the captain approves the spend; the judge-options slice records it after its prompt lands.
- Deferred, human labelers: fixture v2 has two model labelers. The r15 requirement for two human labelers is met in form, not in substance. Owner: the captain.
- Deferred, R5 part 2 (reconstructor `numeric.applicable`): not in this slice's brief; the deterministic tier does not need it. Owner: judge-options slice, if wanted.
- Deferred, writer prompt text for the display rule and the uncertainty notation (audit 4.3 b and 4.3 d prompt lines): owner writer-context slice.
