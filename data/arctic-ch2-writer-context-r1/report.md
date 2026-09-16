# Chapter 2 phase 1: generation

Task: `arctic-ch2-writer-context-r1`.
Branch: `fm/arctic-ch2-writer-context-r1`, from `fm/arctic-audit-priorities-r1` at commit `a53b106`.
Source of the work: `data/arctic-r15-holistic-audit-r1/report.md` sections 4.1, 4.3, 4.5, 5 (phase 1) and 6, with `stages/question_writer.md`, `stages/finding_selection.md` and `stages/eligibility.md`.

## 1. Files outside the owned set

The integration crew must reconcile these files with the sibling crews.

| File | Owner | What this task changed |
|---|---|---|
| `src/arctic_qa/validation.py` | gates crew (`arctic-ch2-gates-r1`) | New helper functions, the 2.7.0 contract row, the widened scope binding, and four new reason codes. No gate was made weaker. No standalone or numeric contract text changed. |
| `src/arctic_qa/streaming.py` | core and routing crew (`arctic-ch2-core-routing-r1`) | Six reason-code names added to three existing frozen sets. No routing logic changed. |
| `config/gemini-eligibility-prompt-v7.txt` | corpus crew (`arctic-ch2-corpus-r1`) | New file. It holds v6 plus the E4 component rule only. The corpus crew must merge its ordered geography procedure and bounded re-screen into the same file. |
| `fixtures/fake-author.jsonl`, `fixtures/fake-verifier.jsonl` | shared test fixtures | Updated to the new extractor, writer and answer-verifier response shapes. |
| `tests/test_geography_correction.py` | shared test file | Call arity, and two span-custody tests moved to a separable component. |
| `tests/test_generation_scope_roles.py` | shared test file | One assertion widened for the new `scope_qualifier_not_displayed` code. |
| `tests/test_streaming.py`, `tests/test_cli_integration.py` | shared test files | Prompt and schema version strings, the new extractor payload shape, and the new prompt markers. |
| `tests/test_project_progress_viewer.py` | shared test file | One stale assertion corrected. See section 8. |
| `docs/BENCHMARK_INPUT_CONTRACT.md` | shared doc | Current prompt and schema versions, plus a new section on the two-part evidence bundle. |
| `docs/STREAMING_DATASET.md` | shared doc | One paragraph for generation prompt version 22, in the file's existing version-log style. |

Owned files: `src/arctic_qa/generation.py` and the new `tests/test_writer_context_bundle.py`.

## 2. Contract versions

| Version | Before | Now |
|---|---|---|
| `GENERATION_PROMPT_VERSION` | `arctic-qa-generation-v21` | `arctic-qa-generation-v22` |
| `CANDIDATE_SCHEMA_VERSION` | `2.6.0` | `2.7.0` |
| `SCOPE_ROLE_FINDING_POLICY_VERSION` | `one-finding-per-paper-full-context-v7` | `one-finding-per-paper-ranked-context-v8` |
| `CONTEXT_ONLY_EVIDENCE_CONTRACT_VERSION` | none | `question-context-evidence-v1` |
| `REFERENT_SLOT_CONTRACT_VERSION` | none | `referent-slot-checklist-v1` |
| `FINDING_ADMISSION_CONTRACT_VERSION` | none | `freeze-time-finding-admission-v1` |

`LEGACY_GENERATION_PROMPT_VERSION` holds `arctic-qa-generation-v21` so the 2.6.0 contract row keeps its historical prompt version.
`FINDING_POLICY_VERSION` stays at `one-finding-per-paper-full-context-v6`. It is the legacy base policy and `tests/test_streaming.py` asserts it.

Versions this task did NOT change: `SCOPE_CONTRACT_VERSION` (`selected-evidence-literal-scope-v4`), `STANDALONE_VERIFICATION_CONTRACT_VERSION`, `NUMERIC_RULE_CONTRACT_VERSION`, `QUESTION_VERIFICATION_CONTRACT_VERSION`, `GENERATION_ATTEMPT_CONTRACT_VERSION`.

CAUTION for integration: the `answer_verifier` role schema gained one required field, `interpretation_scope_applies_to_finding`, while `QUESTION_VERIFICATION_CONTRACT_VERSION` stays at `question-verification-v1`. That version belongs to the gates crew. If the gates crew also changes that schema, merge both fields and decide the version bump once.

## 3. Defects addressed, by audit finding id

### E1, section 4.1 fix 2. The span restriction was applied to whole-study papers

`_eligible_generation_scope` returned the coalesced finding spans for every paper.
The frozen policy restricts spans only for a separable Arctic component.
The function now returns `None` as the restriction for a `whole_study` paper, so finding selection reads the whole paper.

The guard at the old `generation.py:1103` tested `arctic_scope is not None`.
With no restriction that guard produced an empty list, which made `_context` declare `scope_restricted` and join spans instead of showing the chunk.
The guard now tests `arctic_scope_spans is not None`.

Rigor: a `whole_study` paper has no non-Arctic component by the classifier's own determination. A wider window cannot admit an out-of-scope claim. The standalone gate, the reconstructor, the answer verifier and every deterministic rule are unchanged.

### E2, W1 half A, FS-1, section 4.1 fix 1. The activity spans never reached any model

`_eligible_generation_scope` read only `finding_spans`.
It now also reads `activity_spans` and re-locates each one through `_locate_eligibility_span`, the same sha256 custody path that finding spans use.
`_context_only_source` renders them as a `CONTEXT_ONLY_SOURCE` block that the writer, the reconstructor, the answer verifier, the distractor writer and the option verifier all receive, because every role builds from the same `context` string.

The block carries the exact instruction text of section 4.1 fix 1:

> CONTEXT_ONLY_SOURCE supports question_context statements only.
> Never select a CONTEXT_ONLY_SOURCE span as answer evidence, as a scope value, or as a required question phrase.

Forwarding rules:

- For a `whole_study` paper, all activity spans go forward.
- For a `separable_arctic_component` paper, only a span that contains one of the certified `question_scope_phrases` goes forward. Those phrases are what define the component, so the forwarded setting belongs to the frozen finding.
- A span is dropped when it holds a figure, table, equation, section, panel or citation locator, when it holds a run of eight or more spaces (the two-column join), or when it is shorter than 16 characters.
- A span is dropped when the frozen answer text or a variant occurs in it.
- An unlocatable activity span is dropped. An unlocatable finding span still raises `eligible_arctic_scope_finding_unbound`.

Rigor: every forwarded span is verbatim, hash-verified text of the same paper. No interpretation span enters `context_spans`, so `_resolve_source_span` refuses it and any role that selects one gets the existing `..._evidence_span_not_found` rejection. The answer, its evidence quote, its numeric rule and its required phrases stay bound to the finding spans.

### W2, W5, section 4.1 fix 4. One slot checklist for the writer and the judge

The first sentence of `QUESTION_CONTEXT_INSTRUCTIONS` was:

> Set question_context to an empty string when the question is self-contained.

It is replaced by the section 4.1 fix 4 text:

> For each referent slot, decide whether the question alone fixes it: subject or system, measured variable, unit meaning, percentage basis, acronym, location, period or event, population or sample, treatment or condition, comparison basis.
> A slot is fixed only when the question states it, or when the slot does not apply to this claim.
> Set question_context to an empty string only when every applicable slot is fixed by the question alone.
> Otherwise state each unfixed slot in question_context, in source-supported words, taken from SOURCE_DATA or from CONTEXT_ONLY_SOURCE.
> Take a location, period, sample, or term definition from CONTEXT_ONLY_SOURCE when SOURCE_DATA does not state it.
> Do not invent a slot value that neither source states.
> Cite, for each context statement, the span id it rests on, in question_rationale.

`REFERENT_SLOT_DEFINITION` holds the same test in one paragraph. The writer prompt and the answer-verifier prompt both receive it, so the two judges stop disagreeing about the same field.

The writer schema gained `referent_slots`: exactly ten entries, one per slot, with the states `stated_in_question`, `stated_in_context`, `not_applicable` and `unavailable_in_source`. The record is stored on the candidate as `referent_slots`.

Rigor: `referent_slots` is a diagnostic record only. No gate reads it, so it can admit nothing. The prompt says so twice. The routing crew can use `unavailable_in_source` for its slot-availability guard.

Kept unchanged: the benchmark contract sentence in `BENCHMARK_STANDALONE_INSTRUCTIONS`, every anti-leakage rule in `QUESTION_CONTEXT_INSTRUCTIONS`, and "Do not infer or invent a definition." The acronym rule now reads "occurs in SOURCE_DATA or in CONTEXT_ONLY_SOURCE", which widens the source and not the permission.

### W3, E6, section 4.3 fix 1. Coverage replaces verbatim splicing

The writer instruction "Include every required_question_phrases entry verbatim." is replaced by `REQUIRED_PHRASE_COVERAGE_INSTRUCTIONS`:

> Cover the meaning of every required_question_phrases entry in the question or in question_context.
> Normalize the wording first: join words broken by a line wrap, collapse runs of spaces to one space, and remove citation marker digits attached to a word.
> Do not copy a source sentence, a figure or table caption, or a clause that states the answer.
> Do not quote SOURCE_DATA inside the question.
> Place an independent scope qualifier in question_context, not in the question stem, when the question reads better without it.
> Do not drop a required phrase.
> If a required phrase cannot be covered without stating the answer, or its text is unreadable, name the phrase in question_rationale.

The gate changed in lockstep. `scope_qualifier_missing` now accepts the phrase in the question or in `question_context`, under the same repair projection, in both `_qa_gate_reasons` and `validate_candidate`.

Rigor: the writer still cannot drop a phrase. The phrase must still be verbatim inside the hashed answer evidence, which `scope_qualifier_not_source_bound` still tests. Only the display path widened.

### section 4.2 fix 2. No publication-relative period

`DISPLAYED_PERIOD_INSTRUCTIONS` goes to the writer and to the direct-joint arm:

> When SOURCE_DATA or CONTEXT_ONLY_SOURCE states a calendar period, a site name, or a sample size for this finding, state it.
> Do not write 'the past N years', 'recent years', 'at this time', or another publication-relative period.
> A period must be a calendar period or an event-anchored period that the displayed task names.

This is a writer rule. No deterministic period reject was added, because the brief's validation list does not name one and the standalone gate already tests the same property.

### section 4.3 fix 3. One comparison projection for the gate and the router

`_qa_gate_reasons` tested required phrases with a bare `normalize_text` substring test while `validate_candidate` used the hyphen-repairing `_scope_phrase_in_text`. The two disagreed.
`_qa_gate_reasons` now calls the same projection, through the new public `scope_phrase_in_text`.
Gapped matching is not introduced anywhere. The `selected-evidence-literal-scope-v4` semantics are unchanged.

### FS-2, section 4.1 fix 3. The extractor no longer minimizes scope

The sentence "Populate only the minimum scope qualifiers needed to make the answer unique." is replaced by:

> Set each non-null scope value to exact SOURCE_DATA text, from a finding span or from an interpretation span.
> Do not use an alias or a paraphrase.
> Keep at least one value non-null.
> Populate every scope qualifier that a reader without the paper needs to interpret the result.
> This always includes geography and period when any supplied span states them.
> Uniqueness inside the paper is not sufficient.
> Put a scope value in required_question_phrases only when the question must repeat it word for word.
> A scope value that comes from an interpretation span belongs in question_context, not in required_question_phrases.

`scope_is_evidence_bound` takes an optional list of interpretation texts. A scope value binds when it occurs in the selected span, or in a forwarded interpretation span. A value the finding span states already matches on the first text, so the interpretation spans only serve a dimension the finding span does not state.

New reason code `scope_qualifier_not_displayed`: a non-null `geography`, `period` or `population` value must occur in the question or in `question_context`.

Rigor: this is a new acceptance obligation. Today a populated scope value can stay invisible to the reader. Scope binding did not get weaker: a value must still be verbatim in a hashed span of the same paper, and it must now also reach the reader.

### FS-3, FS-4, FS-5, section 4.5. Admission runs at freeze time

`_admit_ranked_finding` runs before the INSERT into `findings`.
For each ranked candidate, in rank order, it resolves the span, applies the excluded-span rule, applies `_require_arctic_scope_custody`, confirms the record resolves to one chunk, then applies `_finding_admission_reason`:

- `finding_span_is_table_or_caption`: three or more numeric cells separated by runs of two or more spaces, no finite verb, and no interpretation span cited as the caption or column header.
- `finding_span_figure_defined_referent`: a figure or panel reference in the span, and no interpretation span cited.

The first candidate that passes is frozen. A rejected candidate is recorded in `provenance.finding_admission.rejected_candidates`. When every candidate fails, the last rejection is raised, so the family routes to an alternative finding.

`_finding_coherence_shadow` computes the two-column signature and the line-wrap hyphen signature and writes the result to `provenance.finding_admission.coherence_shadow_reason`. It never rejects. The audit measured this check firing on 81 of 145 findings, so it must not ship fail-closed.

The extractor's `interpretation_span_ids` are filtered to the ids this run actually forwarded, so the model cannot cite a span that does not exist.

The leak check `finding_answer_phrase_in_required_question_phrases` stays where it is. Moving it to freeze time belongs to the core and routing crew.

Rigor: every admission check only rejects. Ranking decides which surviving candidate is frozen; it changes no gate and no acceptance test.

### FS-6, section 4.5 fix 6. Ranked candidate findings

The extractor role schema returns `candidate_findings`: one to three records, each with `rank`, `answer` and `ranking_rationale`, ranked best first.
The prompt states the ranking test from the audit.
The audit says to choose either ranking or the `finding_scope_unidentifiable` admission check, not both. This task implements ranking, so that check is not added.

Because the candidates arrive in one call, an admission failure advances to the next rank without an extra paid call and without spending a family path.

### E4. Results on both sides of the boundary

`config/gemini-eligibility-prompt-v7.txt` is v6 plus:

> A paper that reports one or more results inside the Arctic boundary and one or
> more results outside it is always separable_arctic_component. Never use
> whole_study for such a paper.

This rule is the safety pairing for the E1 change: it keeps `whole_study` meaning "every result is Arctic", which is what makes an unrestricted window safe.

### section 4.1 fix 5. The answer verifier judges the two-part bundle

The verifier rule was "Set question_context_source_supported to true only when every context statement has source support."
It now reads:

> Set question_context_source_supported to true only when every context statement is supported by SOURCE_DATA or by CONTEXT_ONLY_SOURCE, and is applicable to the selected finding.
> Set it to false for any statement supported by neither.
> Set interpretation_scope_applies_to_finding to false when a place or a period taken from CONTEXT_ONLY_SOURCE does not apply to the selected finding.
> Set it to true when no context statement rests on CONTEXT_ONLY_SOURCE.

The slot checklist is pasted into the `question_context_required` rule, so the writer and the verifier apply one definition.
The scope rule now accepts a value that occurs in the selected span or in a CONTEXT_ONLY_SOURCE span, and that the question or the question_context states.

New reason code `interpretation_scope_not_applicable_to_finding`: when this run forwarded any context-only span and the verifier does not return `interpretation_scope_applies_to_finding` as true, the candidate is rejected. A missing field is a rejection, not a default pass.

## 4. Rigor safeguards, per change

| Change | Why it cannot admit a paper-dependent or unsupported item |
|---|---|
| Two-part evidence bundle | Every added span is verbatim, hash-verified text of the same paper, re-located fail-closed. The added text names where, when and on what sample. That is the definition of self-contained, and it is never the answer. |
| CONTEXT_ONLY spans are non-selectable | They never enter `context_spans`, so `_resolve_source_span` rejects any role that selects one. The answer, the evidence quote, the numeric rule and the required phrases stay bound to finding spans. |
| Answer-bearing setting span | Dropped before any model sees it, and `interpretation_span_contains_answer` rejects any candidate whose answer occurs in a forwarded span. The filter and the gate are independent, so a bundle built without the filter still fails. |
| Interpretation span custody at validation | `context_only_spans_resolve` re-checks each recorded span against the chunk bytes and its sha256. A mismatch is `interpretation_span_not_located`. |
| Widened `scope_is_evidence_bound` | A scope value must still be verbatim in a hashed span of the same paper. The widening adds hashed spans; it never accepts an unbound value. |
| `scope_qualifier_not_displayed` | A new rejection. Nothing passed before that fails now, and items that hid their scope from the reader now fail. |
| `benchmark_text_raw_source_artifact` | A new rejection for a newline, three or more consecutive spaces, or a quotation of more than eight words in shown text. |
| Coverage instead of verbatim | The writer still cannot drop a required phrase, and the phrase must still be verbatim inside the hashed answer evidence. |
| `scope_phrase_in_text` in `_qa_gate_reasons` | The gate and the router now use one projection. No gapped matching was added. |
| Freeze-time admission | Table-row and figure-referent checks only reject. They discard findings the pipeline froze before. |
| Coherence check | Shadow mode. It writes a diagnostic and rejects nothing. |
| Ranked candidates | Ranking selects among candidates that all pass the same admission gate. It changes no acceptance test. |
| `referent_slots` | Diagnostics only. No gate reads it. |
| E4 component rule | It makes `whole_study` harder to reach, which is what makes the unrestricted window safe. |
| Whole-study unrestricted window | The classifier already certified every result of such a paper as Arctic. The standalone gate, both leakage checks, `question_context_not_source_supported` and every numeric rule run unchanged on more text. |

The standalone gate still receives only the question and the question_context. This task did not touch `STANDALONE_SYSTEM`, the standalone contract version, the numeric contract or the routing contract.

## 5. New reason codes

| Code | Layer | Routing |
|---|---|---|
| `interpretation_span_contains_answer` | QA gate and `validate_candidate` | alternative finding, immediate |
| `interpretation_scope_not_applicable_to_finding` | QA gate and `validate_candidate` | repairable question |
| `interpretation_span_not_located` | `validate_candidate` | terminal, custody failure |
| `scope_qualifier_not_displayed` | QA gate and `validate_candidate` | repairable question |
| `benchmark_text_raw_source_artifact` | QA gate and `validate_candidate` | repairable question |
| `finding_span_is_table_or_caption` | freeze-time admission | alternative finding, immediate |
| `finding_span_figure_defined_referent` | freeze-time admission | alternative finding, immediate |
| `finding_span_not_coherent_prose` | shadow diagnostic only | none |

## 6. Tests

Command: `nix develop -c bash -c 'PYTHONPATH=src pytest <file> -q'`.

| Suite | Result |
|---|---|
| `tests/test_writer_context_bundle.py` (new) | 44 passed |
| `tests/test_question_context.py` | passed |
| `tests/test_generation_scope_roles.py` | passed |
| `tests/test_eligibility_span_contract.py` | passed |
| `tests/test_gemini_eligibility.py` | passed |
| `tests/test_scope_layout.py` | passed |
| `tests/test_geography_correction.py` | 14 passed |
| `tests/test_current_payload_validation.py` | passed |
| Whole suite, `pytest tests/` | 590 passed, 0 failed |

The focused suites above ran on their own first. The final check ran the whole
suite in one pass, after every edit, and it is green.
`tests/test_streaming.py` and `tests/test_cli_integration.py` are slow: together
they take about 35 minutes on this loaded machine, because their CLI subprocesses
sleep on broker rate limits. Both are inside the 590.

`ruff check src/ tests/` passes.
`ruff format` was applied to `src/arctic_qa/generation.py` and `src/arctic_qa/validation.py`. Seven other source files were already unformatted at the base commit and were left alone.

### New regression tests, by defect

`tests/test_writer_context_bundle.py` covers each defect the audit names for this slice:

- E1: whole study keeps no restriction; a separable component still restricts.
- The guard defect: an unrestricted whole-study bundle no longer declares `scope_restricted`.
- E2 and W1: activity spans travel through the hashed custody path; an unlocatable activity span costs context, never custody; an unlocatable finding span still fails closed.
- The CONTEXT_ONLY block: exact instruction text, `selectable_for_answer_evidence` false, `span_role` interpretation.
- The filters: locator, citation, column garble, short span; separable components keep only their own setting spans.
- Rigor: an answer-bearing setting span is dropped, and a forwarded one is a gate rejection.
- The applicability verdict: false and missing both reject.
- FS-2: scope binds to an interpretation span for a dimension the finding span does not state, and stays unbound when neither span holds it.
- W3: a required phrase covered by question_context passes; a line-wrapped phrase binds through the shared projection.
- `scope_qualifier_not_displayed` and `benchmark_text_raw_source_artifact`.
- FS-4 and FS-5: table row, figure referent, coherence shadow mode.
- FS-6: ranking freezes the best admissible candidate, advances on rejection, records the rejection, and refuses an invented interpretation span id.
- Prompt and schema text: the slot checklist replaced the default-to-empty rule; the benchmark contract sentence, the anti-leakage rules and the ban on invented definitions survive.
- E4: the component rule is in prompt v7.
- Every contract version this slice owns.

## 7. Work the integration crew must do

`config/live-dataset-current-contract-v1.json` still pins `2.6.0` and `arctic-qa-generation-v21`.
The live publication export selects only candidates with those exact contracts, so it selects nothing from chapter 2 until that file moves to `2.7.0` and `arctic-qa-generation-v22`.
This task left the file alone because the integration and run crew owns the execution gate and the launcher, and because `tests/test_publication_export.py` builds its fixtures around the pinned pair.
Move both together.

## 8. One failure that came from the base branch

`tests/test_project_progress_viewer.py::test_project_overview_uses_streaming_counts_as_separate_live_metrics` fails at the base commit `a53b106`.
Commit `e1fd380` ("Scope corpus viewer to latest invocation") changed `accepted_qa_scope` to `current_incremental_invocation` and made `accepted_qa` report the observed count.
It did not update this test.
This task corrected the two stale assertions to match the shipped viewer code.
No viewer code changed.

## 9. Deferred

- The leak-check move to freeze time belongs to the core and routing crew. This task left `finding_answer_phrase_in_required_question_phrases` where it is, and registered the two new admission codes in the routing sets so the crew can fold them in.
- `finding_scope_unidentifiable` is not added. The audit says to choose ranking or that check, not both.
- `source_text_extraction_interleaved` and the span re-extraction route (section 4.3 fix 3, second half) are not added. They depend on the corpus crew's column-aware re-extraction.
- Sentence-boundary span extension (section 4.5 fix 4) is not added. It re-hashes frozen evidence and belongs with the corpus re-freeze.
- The extractor's `interpretation_span_ids` are validated and stored, but no separate verifier call confirms that a caption labels a selected cell. The admission check refuses a bare table row instead.
- The deterministic writer pre-check on `referent_slots` (W2, second half) is not added. The brief calls the record diagnostics only.
