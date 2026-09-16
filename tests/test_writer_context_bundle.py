"""Phase 1 regression tests: the two-part evidence bundle and its gates.

Every defect the r15 holistic audit names for the generation slice has a test
here. Each one shows both halves: the writer now receives the study-setting
text, and the added gate still rejects an item that rests on it wrongly.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from arctic_qa import generation, streaming, validation
from arctic_qa.errors import CandidateRejectedError
from arctic_qa.util import sha256_bytes


ROOT = Path(__file__).resolve().parents[1]
SCOPE_KEYS = (
    "geography",
    "population",
    "period",
    "method",
    "comparison",
    "uncertainty",
)
SITE_SENTENCE = (
    "The study site was the Villum Research Station at Station Nord in the "
    "northeastern region of Greenland."
)
FINDING_SENTENCE = "Sulfate aerosol mass declined by 15 percent during the 2016 season."


def _scope(**values: str) -> dict[str, str | None]:
    return {key: values.get(key) for key in SCOPE_KEYS}


def _scope_evidence(span_id: str, **values: str) -> list[dict[str, str]]:
    """Cite the given span for every scope value, quoting the value itself."""
    return [
        {"dimension": key, "span_id": span_id, "quote": value}
        for key, value in values.items()
    ]


def _span_record(quote: str, span_id: str) -> dict[str, object]:
    return {
        "span_id": span_id,
        "quote": quote,
        "source_bytes_sha256": sha256_bytes(quote.encode("utf-8")),
    }


def _source(
    component: str,
    finding_quotes: list[str],
    activity_quotes: list[str],
    phrases: list[str] | None = None,
) -> dict[str, object]:
    return {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "eligibility_job_key": "job-phase-1",
                "resolved_eligible_arctic_scope": {
                    "component": component,
                    "finding_spans": [
                        _span_record(quote, f"f{index}")
                        for index, quote in enumerate(finding_quotes)
                    ],
                    "activity_spans": [
                        _span_record(quote, f"a{index}")
                        for index, quote in enumerate(activity_quotes)
                    ],
                    "question_scope_phrases": list(phrases or []),
                },
            }
        ),
    }


def _chunks(text: str) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": "chunk-1",
            "section_id": "results",
            "heading": "Results",
            "chunk_index": 0,
            "text": text,
        }
    ]


def _records(
    evidence: str, answer_text: str, scope: dict[str, str | None]
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    locator = {"chunk_id": "chunk-1", "start_offset": 0, "end_offset": len(evidence)}
    answer = {
        "text": answer_text,
        "variants": [],
        "evidence_quote": evidence,
        "locator": locator,
        "scope": scope,
        "required_question_phrases": [
            value for value in scope.values() if value is not None
        ]
        or ["declined"],
        "claim_type": "observation",
    }
    reconstruction = {
        "answer": answer_text,
        "evidence_quote": evidence,
        "locator": locator,
        "scope": deepcopy(scope),
        "ambiguity_label": "one_answer",
        "question_claim_type": "observation",
    }
    verification = {
        **reconstruction,
        "scope": deepcopy(scope),
        "source_entailment_model_verified": True,
        "relation_scope_match": True,
        "ambiguity_resolved": True,
        "alternative_answer_search_passed": True,
        "question_context_required": False,
        "question_context_source_supported": True,
        "question_context_answer_leakage_absent": True,
        "interpretation_scope_applies_to_finding": True,
        "question_verification_contract_version": (
            generation.QUESTION_VERIFICATION_CONTRACT_VERSION
        ),
        "question_context_referent_resolved": True,
        "question_context_missing_detail": "",
        "question_answer_leakage_absent": True,
    }
    return answer, reconstruction, verification


def _standalone() -> dict[str, object]:
    return {
        "contract_version": generation.STANDALONE_VERIFICATION_CONTRACT_VERSION,
        "pass": True,
        "answer_leakage_absent": True,
        "unresolved_phrases": [],
        "missing_detail_types": [],
        "reasons": [],
        "review_rationale": "The scientific referent is complete.",
    }


# E1: the span restriction belonged to the separable component only.


def test_whole_study_keeps_no_span_restriction_and_still_records_custody() -> None:
    text = f"{SITE_SENTENCE}\n{FINDING_SENTENCE}\nA second reported result followed."
    scope, restriction, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [SITE_SENTENCE]), _chunks(text)
    )

    assert scope is not None
    assert scope["component"] == "whole_study"
    assert restriction is None
    context, spans = generation._finding_context(_chunks(text), restriction)
    assert "A second reported result followed." in context
    assert spans


def test_separable_component_still_restricts_the_finding_spans() -> None:
    arctic = "At the Arctic station nitrate declined by 15 percent."
    southern = "At the southern station nitrate increased by 20 percent."
    scope, restriction, _ = generation._eligible_generation_scope(
        _source(
            "separable_arctic_component",
            [arctic],
            [SITE_SENTENCE],
            ["Arctic station"],
        ),
        _chunks(f"{arctic}\n{southern}"),
    )

    assert scope is not None
    assert restriction is not None
    context, _ = generation._finding_context(
        _chunks(f"{arctic}\n{southern}"), restriction
    )
    assert arctic in context
    assert southern not in context


def test_whole_study_guard_reports_an_unrestricted_writer_bundle() -> None:
    # The old guard tested arctic_scope, so a whole-study paper (which now
    # carries no restriction) still declared scope_restricted to every role.
    chunk = _chunks(f"{SITE_SENTENCE}\n{FINDING_SENTENCE}")[0]

    assert '"scope_restricted":false' in generation._context(chunk, None)
    assert '"scope_restricted":true' in generation._context(chunk, [])


# E2 and W1: the activity spans reach the writer as context-only evidence.


def test_activity_spans_travel_through_the_same_hashed_custody_path() -> None:
    text = f"{SITE_SENTENCE}\n{FINDING_SENTENCE}"
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [SITE_SENTENCE]), _chunks(text)
    )

    assert [span["text"] for span in interpretation] == [SITE_SENTENCE]
    span = interpretation[0]
    assert span["text_sha256"] == sha256_bytes(SITE_SENTENCE.encode("utf-8"))
    assert span["eligibility_quote_sha256"] == span["text_sha256"]
    assert text[span["start_offset"] : span["end_offset"]] == SITE_SENTENCE


def test_unlocatable_activity_span_costs_context_not_custody() -> None:
    absent = "The study site was a station that the chunks never contain."
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [absent]),
        _chunks(FINDING_SENTENCE),
    )

    assert interpretation == []


def test_unlocatable_finding_span_still_fails_closed() -> None:
    with pytest.raises(CandidateRejectedError) as error:
        generation._eligible_generation_scope(
            _source("whole_study", ["A finding the chunks never contain."], []),
            _chunks(FINDING_SENTENCE),
        )

    assert error.value.reason_code == "eligible_arctic_scope_finding_unbound"


def test_context_only_block_carries_the_audit_instruction_and_is_unselectable() -> None:
    text = f"{SITE_SENTENCE}\n{FINDING_SENTENCE}"
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [SITE_SENTENCE]), _chunks(text)
    )
    block = generation._context_only_source(interpretation)

    assert "CONTEXT_ONLY_SOURCE_BEGIN" in block
    assert "CONTEXT_ONLY_SOURCE_END" in block
    assert "Villum Research Station" in block
    assert '"selectable_for_answer_evidence":false' in block
    assert '"span_role":"interpretation"' in block
    assert (
        "CONTEXT_ONLY_SOURCE supports question_context statements only."
    ) in generation.CONTEXT_ONLY_SOURCE_INSTRUCTIONS
    assert (
        "Never select a CONTEXT_ONLY_SOURCE span as answer evidence, as a scope "
        "value, or as a required question phrase."
    ) in generation.CONTEXT_ONLY_SOURCE_INSTRUCTIONS
    assert generation.CONTEXT_ONLY_SOURCE_INSTRUCTIONS in block


def test_context_only_block_is_absent_when_no_setting_span_survives() -> None:
    assert generation._context_only_source([]) == ""
    assert generation._context_only_source(None) == ""


@pytest.mark.parametrize(
    "quote",
    [
        # A residual figure or table pointer still kills the sentence
        # (chapter 2 yield audit 4.1 a keeps _RESIDUAL_LOCATOR_PATTERN).
        "Sample locations are shown in Figure 3 of the same study.",
        "Sampling used the method of Table 2 across the whole campaign.",
        "The site was Ny-Alesund          a second column line intruded here.",
        "Short.",
        # A fragment without a finite verb or terminal punctuation.
        "Villum Research Station at Station Nord in northeastern Greenland",
    ],
)
def test_unusable_setting_spans_never_reach_a_model(quote: str) -> None:
    text = f"{quote}\n{FINDING_SENTENCE}"
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [quote]), _chunks(text)
    )

    assert interpretation == []


def test_separable_component_forwards_only_its_own_setting_spans() -> None:
    arctic = "At the Arctic station nitrate declined by 15 percent."
    arctic_setting = "Sampling at the Arctic station ran through the 2016 season."
    # An unlabelled span that names another place keeps the phrase test.
    southern_setting = "Sampling at the Bothnian Bay site ran through the 2016 season."
    text = "\n".join([arctic, arctic_setting, southern_setting])
    _, _, interpretation = generation._eligible_generation_scope(
        _source(
            "separable_arctic_component",
            [arctic],
            [arctic_setting, southern_setting],
            ["Arctic station"],
        ),
        _chunks(text),
    )

    assert [span["text"] for span in interpretation] == [arctic_setting]


# Rigor: no answer may rest on a context-only span.


def test_answer_bearing_setting_span_is_dropped_before_any_model_sees_it() -> None:
    leaking = "The study site reported a 15 percent decline across the season."
    spans = [
        {"text": leaking, "span_id": "s1"},
        {"text": SITE_SENTENCE, "span_id": "s2"},
    ]
    answer = {"text": "15 percent", "variants": []}

    forwarded = generation._forwarded_context_only_spans(spans, answer)

    assert [span["span_id"] for span in forwarded] == ["s2"]


def test_forwarded_setting_span_with_the_answer_is_a_gate_rejection() -> None:
    answer, reconstruction, verification = _records(
        FINDING_SENTENCE, "15 percent", _scope(method="sulfate aerosol mass")
    )

    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": FINDING_SENTENCE},
        "By how much did sulfate aerosol mass change in the 2016 season?",
        answer,
        reconstruction,
        verification,
        "",
        standalone_verification=_standalone(),
        interpretation_spans=[
            {"text": "The site reported a 15 percent decline.", "span_id": "s1"}
        ],
    )

    assert "interpretation_span_contains_answer" in reasons


def test_setting_scope_that_does_not_apply_to_the_finding_is_rejected() -> None:
    answer, reconstruction, verification = _records(
        FINDING_SENTENCE, "15 percent", _scope(method="sulfate aerosol mass")
    )
    verification["interpretation_scope_applies_to_finding"] = False

    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": FINDING_SENTENCE},
        "By how much did sulfate aerosol mass change in the 2016 season?",
        answer,
        reconstruction,
        verification,
        "",
        standalone_verification=_standalone(),
        interpretation_spans=[{"text": SITE_SENTENCE, "span_id": "s1"}],
    )

    assert "interpretation_scope_not_applicable_to_finding" in reasons


def test_missing_applicability_verdict_is_a_rejection_not_a_default_pass() -> None:
    answer, reconstruction, verification = _records(
        FINDING_SENTENCE, "15 percent", _scope(method="sulfate aerosol mass")
    )
    verification.pop("interpretation_scope_applies_to_finding")

    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": FINDING_SENTENCE},
        "By how much did sulfate aerosol mass change in the 2016 season?",
        answer,
        reconstruction,
        verification,
        "",
        standalone_verification=_standalone(),
        interpretation_spans=[{"text": SITE_SENTENCE, "span_id": "s1"}],
    )

    assert "interpretation_scope_not_applicable_to_finding" in reasons


# FS-1 and 4.1 fix 3: scope binds to the finding span plus interpretation spans.


def test_scope_binds_to_an_interpretation_span_for_an_unstated_dimension() -> None:
    answer = {"evidence_quote": FINDING_SENTENCE}
    scope = _scope(geography="Station Nord", method="sulfate aerosol mass")

    assert not validation.scope_is_evidence_bound(scope, answer)
    assert validation.scope_is_evidence_bound(scope, answer, [SITE_SENTENCE])


def test_scope_absent_from_both_span_kinds_stays_unbound() -> None:
    answer = {"evidence_quote": FINDING_SENTENCE}
    scope = _scope(geography="Svalbard", method="sulfate aerosol mass")

    assert not validation.scope_is_evidence_bound(scope, answer, [SITE_SENTENCE])


# 4.3: coverage instead of verbatim splicing, and one comparison projection.


def test_required_phrase_is_covered_by_question_context() -> None:
    scope = _scope(method="sulfate aerosol mass")
    answer, reconstruction, verification = _records(
        FINDING_SENTENCE, "15 percent", scope
    )
    question = "By how much did the measured value decline in the 2016 season?"

    assert "scope_qualifier_missing" in generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": FINDING_SENTENCE},
        question,
        answer,
        reconstruction,
        verification,
        "",
        standalone_verification=_standalone(),
    )
    assert "scope_qualifier_missing" not in generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": FINDING_SENTENCE},
        question,
        answer,
        reconstruction,
        verification,
        "The measured value is sulfate aerosol mass.",
        standalone_verification=_standalone(),
    )


def test_line_wrapped_required_phrase_binds_through_the_shared_projection() -> None:
    evidence = "Sulfate aerosol mass declined by 15 per-\ncent during the 2016 season."
    scope = _scope(method="15 per-\ncent")
    answer, reconstruction, verification = _records(evidence, "a decline", scope)

    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": evidence},
        "What declined by 15 percent during the 2016 season?",
        answer,
        reconstruction,
        verification,
        "",
        standalone_verification=_standalone(),
    )

    assert "scope_qualifier_not_source_bound" not in reasons
    assert "scope_qualifier_missing" not in reasons


def test_displayed_scope_dimension_must_reach_the_reader() -> None:
    answer = {"scope": _scope(geography="Station Nord", method="sulfate aerosol mass")}

    assert validation.scope_qualifier_not_displayed(
        answer, "By how much did sulfate aerosol mass decline?", ""
    )
    assert not validation.scope_qualifier_not_displayed(
        answer,
        "By how much did sulfate aerosol mass decline?",
        "The measurements were made at Station Nord.",
    )


@pytest.mark.parametrize(
    "question",
    [
        "What value was reported\nat the station?",
        "What value was reported at the    station?",
        'What value follows "the sulfate aerosol mass declined by a large and '
        'sustained margin across the record"?',
    ],
)
def test_raw_pdf_bytes_in_benchmark_text_are_rejected(question: str) -> None:
    assert validation.benchmark_text_raw_source_artifact(question, "")


def test_clean_benchmark_text_is_not_a_raw_source_artifact() -> None:
    assert not validation.benchmark_text_raw_source_artifact(
        "By how much did sulfate aerosol mass decline in the 2016 season?",
        "The measurements were made at Station Nord in northeastern Greenland.",
    )


# FS-3 and FS-5: admission runs before the finding is frozen.


def test_bare_table_row_without_column_labels_is_not_admitted() -> None:
    row = {"evidence_quote": "East-Siberian Sea   48 ± 14   27 ± 8   19 ± 6"}

    assert (
        generation._finding_admission_reason(row, [])
        == "finding_span_is_table_or_caption"
    )
    assert generation._finding_admission_reason(row, ["span-caption-1"]) is None


def test_figure_defined_referent_without_a_prose_definition_is_not_admitted() -> None:
    span = {
        "evidence_quote": (
            "The Central Arctic region shown in Fig. 6e carried the highest flux."
        )
    }

    assert (
        generation._finding_admission_reason(span, [])
        == "finding_span_figure_defined_referent"
    )


def test_prose_finding_passes_admission() -> None:
    assert (
        generation._finding_admission_reason({"evidence_quote": FINDING_SENTENCE}, [])
        is None
    )


def test_two_column_coherence_runs_in_shadow_mode_only() -> None:
    garbled = {
        "evidence_quote": (
            "ducted along a transect beside the 25 km          Shrubs were selected "
            "from a broader          sample pool"
        )
    }

    assert generation._finding_coherence_shadow(garbled) == (
        "finding_span_not_coherent_prose"
    )
    # Shadow mode: the same span is still admitted.
    assert generation._finding_admission_reason(garbled, []) is None


# FS-6: ranked candidate findings.


def test_ranking_freezes_the_best_admissible_candidate_without_a_new_call() -> None:
    table_row = "East-Siberian Sea   48 ± 14   27 ± 8   19 ± 6"
    text = f"{table_row}\n{FINDING_SENTENCE}"
    chunks = _chunks(text)
    spans = {span["span_id"]: span for span in generation._finding_spans(chunks[0])}
    by_text = {span["text"]: span_id for span_id, span in spans.items()}
    chunk_span_id = next(iter(spans))

    candidates = [
        {
            "rank": 1,
            "answer": {
                "text": "48",
                "source_span_id": chunk_span_id,
                "scope": _scope(method="East-Siberian Sea"),
                "scope_evidence": _scope_evidence(
                    chunk_span_id, method="East-Siberian Sea"
                ),
                "required_question_phrases": ["East-Siberian Sea"],
                "claim_type": "observation",
                "selection_rationale": "table row",
            },
            "ranking_rationale": "first",
        },
        {
            "rank": 2,
            "answer": {
                "text": "15 percent",
                "source_span_id": chunk_span_id,
                "scope": _scope(method="sulfate aerosol mass"),
                "scope_evidence": _scope_evidence(
                    chunk_span_id, method="sulfate aerosol mass"
                ),
                "required_question_phrases": ["sulfate aerosol mass"],
                "claim_type": "observation",
                "selection_rationale": "prose sentence",
            },
            "ranking_rationale": "second",
        },
    ]
    assert by_text  # the chunk produced at least one selectable span

    answer, chunk, admission = generation._admit_ranked_finding(
        candidates, spans, chunks, None, [], excluded_span_ids=[]
    )

    assert answer["text"] == "48"
    assert chunk["chunk_id"] == "chunk-1"
    assert admission["admitted_rank"] == 1
    assert admission["candidate_count"] == 2


def test_admission_advances_to_the_next_rank_and_records_the_rejection() -> None:
    chunks = _chunks(FINDING_SENTENCE)
    spans = {span["span_id"]: span for span in generation._finding_spans(chunks[0])}
    span_id = next(iter(spans))
    candidates = [
        {
            "rank": 1,
            "answer": {
                "text": "15 percent",
                "source_span_id": "span-that-does-not-exist",
                "scope": _scope(method="sulfate aerosol mass"),
                "required_question_phrases": ["sulfate aerosol mass"],
                "claim_type": "observation",
                "selection_rationale": "unlocatable",
            },
            "ranking_rationale": "first",
        },
        {
            "rank": 2,
            "answer": {
                "text": "15 percent",
                "source_span_id": span_id,
                "scope": _scope(method="sulfate aerosol mass"),
                "scope_evidence": _scope_evidence(
                    span_id, method="sulfate aerosol mass"
                ),
                "required_question_phrases": ["sulfate aerosol mass"],
                "claim_type": "observation",
                "selection_rationale": "prose sentence",
            },
            "ranking_rationale": "second",
        },
    ]

    answer, _, admission = generation._admit_ranked_finding(
        candidates, spans, chunks, None, [], excluded_span_ids=[]
    )

    assert answer["source_span_id"] == span_id
    assert admission["admitted_rank"] == 2
    assert admission["rejected_candidates"] == [
        {"rank": 1, "reason_code": "finding_evidence_span_not_found"}
    ]


def test_the_extractor_cannot_cite_an_interpretation_span_it_was_not_given() -> None:
    chunks = _chunks(f"{SITE_SENTENCE}\n{FINDING_SENTENCE}")
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [SITE_SENTENCE]), chunks
    )
    spans = {span["span_id"]: span for span in generation._finding_spans(chunks[0])}
    real_id = interpretation[0]["span_id"]
    candidates = [
        {
            "rank": 1,
            "answer": {
                "text": "15 percent",
                "source_span_id": next(iter(spans)),
                "scope": _scope(method="sulfate aerosol mass"),
                "scope_evidence": _scope_evidence(
                    next(iter(spans)), method="sulfate aerosol mass"
                ),
                "required_question_phrases": ["sulfate aerosol mass"],
                "claim_type": "observation",
                "selection_rationale": "prose sentence",
                "interpretation_span_ids": [real_id, "span-the-model-invented"],
            },
            "ranking_rationale": "first",
        }
    ]

    answer, _, _ = generation._admit_ranked_finding(
        candidates, spans, chunks, None, interpretation, excluded_span_ids=[]
    )

    assert answer["interpretation_span_ids"] == [real_id]


def test_every_rejected_candidate_raises_the_last_admission_reason() -> None:
    chunks = _chunks(FINDING_SENTENCE)
    spans = {span["span_id"]: span for span in generation._finding_spans(chunks[0])}
    candidates = [
        {
            "rank": 1,
            "answer": {
                "text": "15 percent",
                "source_span_id": "absent",
                "scope": _scope(method="sulfate aerosol mass"),
                "required_question_phrases": ["sulfate aerosol mass"],
                "claim_type": "observation",
                "selection_rationale": "unlocatable",
            },
            "ranking_rationale": "first",
        }
    ]
    assert spans

    with pytest.raises(CandidateRejectedError) as error:
        generation._admit_ranked_finding(
            candidates, spans, chunks, None, [], excluded_span_ids=[]
        )

    assert error.value.reason_code == "finding_evidence_span_not_found"


# W2, W5 and 4.1 fix 4: one slot checklist, shared by the writer and the judge.


def test_writer_schema_records_one_entry_per_referent_slot() -> None:
    schema = generation.ROLE_SCHEMAS["question_writer"]
    slots = schema["properties"]["referent_slots"]

    assert "referent_slots" in schema["required"]
    assert slots["minItems"] == 10
    assert slots["maxItems"] == 10
    assert slots["items"]["properties"]["slot"]["enum"] == [
        "subject_or_system",
        "measured_variable",
        "unit_meaning",
        "percentage_basis",
        "acronym",
        "location",
        "period_or_event",
        "population_or_sample",
        "treatment_or_condition",
        "comparison_basis",
    ]
    assert "unavailable_in_source" in slots["items"]["properties"]["state"]["enum"]


def test_question_context_instructions_replace_the_default_to_empty_rule() -> None:
    instructions = generation.QUESTION_CONTEXT_INSTRUCTIONS

    assert (
        "Set question_context to an empty string when the question is self-contained."
        not in instructions
    )
    assert (
        "For each referent slot, decide whether the question alone fixes it"
        in instructions
    )
    # Chapter 3 (chapter 2 yield audit 4.1 g): the opening asks for one
    # supported setting sentence instead of granting a permission to omit.
    assert "Write question_context for every question." in instructions
    assert (
        "Set question_context to an empty string only when every applicable slot "
        "is fixed by the question alone." not in instructions
    )
    assert "taken from SOURCE_DATA or from CONTEXT_ONLY_SOURCE" in instructions
    assert "cite, for each context statement, the span id" in instructions.lower()
    # Kept: the ban on invented definitions and every anti-leakage rule.
    assert "Do not infer or invent a definition." in instructions
    assert "Do not include answer-bearing numbers" in instructions
    assert (
        "If an acronym expansion answers the question, do not supply that"
        in instructions
    )


def test_the_benchmark_contract_sentence_survives_unchanged() -> None:
    assert (
        "A reader can need SOURCE_DATA to determine or verify the answer. A reader "
        "must not need it to identify a referent or interpret scope."
    ) in generation.BENCHMARK_STANDALONE_INSTRUCTIONS


def test_writer_coverage_rule_replaces_verbatim_splicing() -> None:
    coverage = generation.REQUIRED_PHRASE_COVERAGE_INSTRUCTIONS

    assert "Include every required_question_phrases entry verbatim" not in coverage
    assert "Cover the meaning of every required_question_phrases entry" in coverage
    assert "Do not quote SOURCE_DATA inside the question." in coverage
    assert "Do not drop a required phrase." in coverage
    # The raw-text reject is paired with the instruction that prevents it.
    assert "Do not use a line break, a tab, or a run of two or more spaces." in coverage


def test_writer_bans_a_publication_relative_period() -> None:
    period = generation.DISPLAYED_PERIOD_INSTRUCTIONS

    assert "the past N years" in period
    assert "recent years" in period
    assert "at this time" in period


# 4.1 fix 3: the extractor no longer minimizes scope.


def test_extractor_schema_carries_ranked_candidates_and_interpretation_ids() -> None:
    extractor = generation.ROLE_SCHEMAS["extractor"]
    findings = extractor["properties"]["candidate_findings"]

    assert extractor["required"] == ["candidate_findings"]
    assert findings["minItems"] == 1
    assert findings["maxItems"] == 3
    answer = findings["items"]["properties"]["answer"]
    assert "interpretation_span_ids" in answer["properties"]


# E4: results on both sides of the boundary are a separable component.


def test_eligibility_prompt_v7_states_the_component_rule() -> None:
    prompt = (ROOT / "config" / "gemini-eligibility-prompt-v7.txt").read_text(
        encoding="utf-8"
    )

    assert (
        "A paper that reports one or more results inside the Arctic boundary and "
        "one or\nmore results outside it is always separable_arctic_component."
    ) in prompt
    assert "Never use\nwhole_study for such a paper." in prompt
    # The v6 rules the audit told us to keep are still present.
    assert (
        "Do not use a title, affiliation, background statement, or citation" in prompt
    )


# Contract versions this slice owns.


def test_phase_one_contract_versions_are_recorded() -> None:
    """The chapter 2 contract row keeps its historical literals under chapter 3."""
    assert (
        generation.SCOPE_ROLE_FINDING_POLICY_VERSION
        == "one-finding-per-paper-ranked-context-v8"
    )
    # The scope contract keeps selected-evidence-literal-scope-v4 semantics.
    assert validation.SCOPE_CONTRACT_VERSION == "selected-evidence-literal-scope-v4"
    contract = validation.CANDIDATE_CONTRACTS["2.7.0"]
    assert contract["prompt_version"] == "arctic-qa-generation-v22"
    assert contract["context_only_evidence_contract_version"] == (
        "question-context-evidence-v1"
    )
    assert contract["referent_slot_contract_version"] == "referent-slot-checklist-v1"
    assert contract["finding_admission_contract_version"] == (
        "freeze-time-finding-admission-v1"
    )
    assert contract["scope_contract_version"] == "selected-evidence-literal-scope-v4"
    assert (
        contract["standalone_verification_contract_version"]
        == validation.STANDALONE_VERIFICATION_CONTRACT_VERSION
    )


def test_new_post_writer_codes_route_through_the_bounded_paths() -> None:
    assert "scope_qualifier_not_displayed" in streaming.REPAIRABLE_QUESTION_REASONS
    assert "benchmark_text_raw_source_artifact" in streaming.REPAIRABLE_QUESTION_REASONS
    assert (
        "interpretation_scope_not_applicable_to_finding"
        in streaming.REPAIRABLE_QUESTION_REASONS
    )
    assert (
        "interpretation_span_contains_answer" in streaming.ALTERNATIVE_FINDING_REASONS
    )
    assert "finding_span_is_table_or_caption" in streaming.ALTERNATIVE_FINDING_REASONS
    assert (
        "finding_span_figure_defined_referent"
        in streaming.IMMEDIATE_ALTERNATIVE_FINDING_REASONS
    )
