from __future__ import annotations

import json

import pytest

from arctic_qa import generation, validation
from arctic_qa.errors import CandidateRejectedError
from arctic_qa.util import sha256_bytes, stable_id


def _span(
    chunk_id: str,
    text: str,
    start: int,
    end: int,
    *,
    eligibility_span_id: str | None = None,
) -> dict[str, object]:
    quote = text[start:end]
    text_sha256 = sha256_bytes(quote.encode())
    record: dict[str, object] = {
        "span_id": stable_id(
            generation.FINDING_SPAN_CONTRACT_VERSION,
            chunk_id,
            start,
            end,
            text_sha256,
        ),
        "chunk_id": chunk_id,
        "start_offset": start,
        "end_offset": end,
        "text_sha256": text_sha256,
        "text": quote,
    }
    if eligibility_span_id:
        record.update(
            {
                "eligibility_span_id": eligibility_span_id,
                "eligibility_quote_sha256": sha256_bytes(quote.encode()),
                "eligibility_locator": {
                    "source_block_id": "text-block-1",
                    "section_id": "extracted-text",
                    "page_id": None,
                },
                "eligibility_match_kind": "exact",
            }
        )
    return record


def _role_record(chunk_id: str, text: str, start: int, end: int) -> dict[str, object]:
    quote = text[start:end]
    text_sha256 = sha256_bytes(quote.encode())
    contract = generation.FINDING_SPAN_CONTRACT_VERSION
    return {
        "evidence_quote": quote,
        "locator": {
            "chunk_id": chunk_id,
            "start_offset": start,
            "end_offset": end,
        },
        "source_span_id": stable_id(contract, chunk_id, start, end, text_sha256),
        "evidence_text_sha256": text_sha256,
        "span_contract_version": contract,
    }


def test_adjacent_eligible_fragments_become_one_exact_source_interval() -> None:
    text = (
        "The extremely cold thermal stress\n"
        "frequencies have substantially decreased over 1979-2020."
    )
    boundary = text.index("frequencies")
    spans = [
        _span("chunk-1", text, 0, boundary, eligibility_span_id="s000028"),
        _span(
            "chunk-1",
            text,
            boundary,
            len(text),
            eligibility_span_id="s000029",
        ),
    ]

    combined = generation._coalesce_source_spans(
        spans, {"chunk-1": {"chunk_id": "chunk-1", "text": text}}
    )

    assert len(combined) == 1
    assert combined[0]["text"] == text
    assert combined[0]["start_offset"] == 0
    assert combined[0]["end_offset"] == len(text)
    assert combined[0]["eligibility_span_ids"] == ["s000028", "s000029"]
    assert [row["source_span_id"] for row in combined[0]["evidence_components"]] == [
        spans[0]["span_id"],
        spans[1]["span_id"],
    ]
    assert combined[0]["text_sha256"] == sha256_bytes(text.encode())
    resolved = generation._resolve_source_span(
        {"source_span_id": combined[0]["span_id"]},
        {combined[0]["span_id"]: combined[0]},
        reason_code="test_span_missing",
    )
    assert validation.source_span_evidence_resolves(
        resolved, {"chunk-1": {"chunk_id": "chunk-1", "text": text}}
    )
    resolved["evidence_components"][1]["text_sha256"] = "0" * 64
    assert not validation.source_span_evidence_resolves(
        resolved, {"chunk-1": {"chunk_id": "chunk-1", "text": text}}
    )


def test_nonadjacent_and_cross_chunk_spans_remain_separate() -> None:
    text = "Arctic result one. Unselected sentence. Arctic result two."
    first_end = text.index(" Unselected")
    second_start = text.index("Arctic result two")
    spans = [
        _span("chunk-1", text, 0, first_end),
        _span("chunk-1", text, second_start, len(text)),
        _span("chunk-2", "Other paper text.", 0, len("Other paper text.")),
    ]

    combined = generation._coalesce_source_spans(
        spans,
        {
            "chunk-1": {"chunk_id": "chunk-1", "text": text},
            "chunk-2": {"chunk_id": "chunk-2", "text": "Other paper text."},
        },
    )

    assert [row["text"] for row in combined] == [
        "Arctic result one.",
        "Arctic result two.",
        "Other paper text.",
    ]


def test_role_evidence_unifies_adjacent_intervals_but_keeps_distant_passages() -> None:
    text = (
        "Subject and scope. Result decreased. "
        "Unselected sentence. Distant supporting result."
    )
    first_end = text.index("Result")
    adjacent_end = text.index(" Unselected")
    distant_start = text.index("Distant")
    chunks = {"chunk-1": {"chunk_id": "chunk-1", "text": text}}

    evidence = generation._decision_evidence(
        {
            "answer": _role_record("chunk-1", text, 0, first_end),
            "reconstruction": _role_record("chunk-1", text, first_end, adjacent_end),
            "answer_verification": _role_record(
                "chunk-1", text, distant_start, len(text)
            ),
        },
        chunks,
    )

    assert [row["evidence_quote"] for row in evidence] == [
        text[:adjacent_end],
        text[distant_start:],
    ]
    assert evidence[0]["roles"] == ["answer", "reconstruction"]
    assert evidence[1]["roles"] == ["answer_verification"]
    assert evidence[0]["evidence_text_sha256"] == sha256_bytes(
        text[:adjacent_end].encode()
    )


def test_matching_answer_can_use_a_different_valid_passage() -> None:
    text = (
        "At Station A, ice loss substantially decreased. "
        "A separate analysis found ice loss substantially decreased."
    )
    second_start = text.index("A separate")
    answer = {
        **_role_record("chunk-1", text, 0, second_start - 1),
        "text": "substantially decreased",
        "variants": [],
        "claim_type": "observation",
        "scope": {
            "geography": "Station A",
            "population": "ice loss",
            "period": None,
            "method": None,
            "comparison": None,
            "uncertainty": None,
        },
        "required_question_phrases": ["Station A", "ice loss"],
    }
    reconstruction = {
        **_role_record("chunk-1", text, second_start, len(text)),
        "answer": "substantially decreased",
        "ambiguity_label": "one_answer",
        "question_claim_type": "observation",
        "scope": {
            "geography": None,
            "population": "ice loss",
            "period": None,
            "method": None,
            "comparison": None,
            "uncertainty": None,
        },
    }
    verifier = {
        **reconstruction,
        "source_entailment_model_verified": True,
        "relation_scope_match": True,
        "scope_value_contradicted_by_source": False,
        "contradicted_scope_field": "",
        "scope_representation_note": "",
        "ambiguity_resolved": True,
        "alternative_answer_search_passed": True,
        "question_context_required": False,
        "question_context_source_supported": True,
        "question_context_answer_leakage_absent": True,
        "question_verification_contract_version": generation.QUESTION_VERIFICATION_CONTRACT_VERSION,
        "question_context_referent_resolved": True,
        "question_context_missing_detail": "",
        "question_answer_leakage_absent": True,
    }

    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": text},
        "How did Station A ice loss change?",
        answer,
        reconstruction,
        verifier,
        standalone_verification={
            "contract_version": generation.STANDALONE_VERIFICATION_CONTRACT_VERSION,
            "pass": True,
            "answer_leakage_absent": True,
            "unresolved_phrases": [],
            "missing_detail_types": [],
            "reasons": [],
            "review_rationale": "The scientific referent is complete.",
        },
    )

    assert reasons == []
    assert answer["source_span_id"] != reconstruction["source_span_id"]


def test_same_answer_does_not_override_scope_unit_or_negation_guards() -> None:
    assert not validation.reconstruction_matches(
        {"text": "substantially decreased", "variants": []},
        {"answer": "not substantially decreased"},
    )
    assert not validation.reconstruction_matches(
        {"text": "2 m", "variants": []}, {"answer": "2 cm"}
    )

    text = "Arctic ice loss substantially decreased."
    record = _role_record("chunk-1", text, 0, len(text))
    scope = {
        "geography": "Arctic",
        "population": "ice loss",
        "period": None,
        "method": None,
        "comparison": None,
        "uncertainty": None,
    }
    answer = {
        **record,
        "text": "substantially decreased",
        "claim_type": "observation",
        "scope": scope,
        "required_question_phrases": ["Arctic", "ice loss"],
    }
    reconstruction = {
        **record,
        "answer": "substantially decreased",
        "ambiguity_label": "one_answer",
        "question_claim_type": "observation",
        "scope": scope,
    }
    verifier = {
        **reconstruction,
        "source_entailment_model_verified": True,
        "relation_scope_match": False,
        "scope_value_contradicted_by_source": False,
        "contradicted_scope_field": "",
        "scope_representation_note": "",
        "ambiguity_resolved": True,
        "alternative_answer_search_passed": True,
        "question_context_required": False,
        "question_context_source_supported": True,
        "question_context_answer_leakage_absent": True,
        "question_verification_contract_version": generation.QUESTION_VERIFICATION_CONTRACT_VERSION,
        "question_context_referent_resolved": True,
        "question_context_missing_detail": "",
        "question_answer_leakage_absent": True,
    }

    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": text},
        "How did Arctic ice loss change?",
        answer,
        reconstruction,
        verifier,
    )

    assert "relation_scope_mismatch" in reasons

    changed_period = dict(answer)
    changed_period["scope"] = {**scope, "period": "1980-2020"}
    changed_period["required_question_phrases"] = [
        "Arctic",
        "ice loss",
        "1980-2020",
    ]
    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": text},
        "How did Arctic ice loss change during 1980-2020?",
        changed_period,
        reconstruction,
        {**verifier, "relation_scope_match": True},
    )
    assert "answer_scope_not_source_bound" in reasons
    assert "scope_qualifier_not_source_bound" in reasons


def test_combined_arctic_evidence_cannot_claim_an_unapproved_span() -> None:
    first = "Arctic subject "
    second = "substantially decreased."
    finding_spans = [
        {
            "span_id": "s1",
            "quote": first,
            "start_byte": 0,
            "end_byte": len(first.encode()),
            "locator": {"section_id": "results"},
            "source_bytes_sha256": sha256_bytes(first.encode()),
        },
        {
            "span_id": "s2",
            "quote": second,
            "start_byte": len(first.encode()),
            "end_byte": len((first + second).encode()),
            "locator": {"section_id": "results"},
            "source_bytes_sha256": sha256_bytes(second.encode()),
        },
    ]
    resolved_scope = {
        "component": "separable_arctic_component",
        "finding_spans": finding_spans,
        "question_scope_phrases": ["Arctic subject"],
    }
    provenance_scope = {
        "component": "separable_arctic_component",
        "question_scope_phrases": ["Arctic subject"],
        "eligibility_job_key": "job-1",
        "finding_spans": finding_spans,
    }
    candidate = {
        "schema_version": "2.1.0",
        "question": "How did the Arctic subject change?",
        "answer": {
            "evidence_quote": first + second,
            "eligibility_span_ids": ["s1", "s2"],
            "evidence_components": [
                {
                    "eligibility_span_id": row["span_id"],
                    "eligibility_quote_sha256": row["source_bytes_sha256"],
                    "eligibility_locator": row["locator"],
                }
                for row in finding_spans
            ],
        },
        "provenance": {
            "eligible_arctic_scope": provenance_scope,
            "eligible_arctic_scope_sha256": sha256_bytes(
                json.dumps(
                    provenance_scope,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
        },
    }
    source = {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "eligibility_job_key": "job-1",
                "resolved_eligible_arctic_scope": resolved_scope,
            }
        ),
    }

    assert validation._eligible_arctic_scope_error(candidate, source) is None
    candidate["answer"]["eligibility_span_ids"] = ["s1", "outside-scope"]
    assert validation._eligible_arctic_scope_error(candidate, source) == (
        "eligible_arctic_finding_out_of_scope"
    )


def test_model_contexts_expose_only_selectable_combined_span_ids() -> None:
    text = "The reported value was 9.7 percent across adjacent source fragments."
    boundary = text.index("across")
    spans = [
        _span("chunk-1", text, 0, boundary),
        _span("chunk-1", text, boundary, len(text)),
    ]
    chunk = {
        "chunk_id": "chunk-1",
        "section_id": "results",
        "heading": "Results",
        "page": 4,
        "text": text,
    }
    combined = generation._coalesce_source_spans(spans, {"chunk-1": chunk})
    combined_span = combined[0]

    role_context = generation._context(chunk, combined)
    finding_context, resolver_spans = generation._finding_context([chunk], combined)
    serialized_contexts = [role_context, finding_context]

    for context in serialized_contexts:
        payload = json.loads(
            context.removeprefix("SOURCE_DATA_BEGIN\n").removesuffix(
                "\nSOURCE_DATA_END"
            )
        )
        evidence_spans = (
            payload["evidence_spans"]
            if "evidence_spans" in payload
            else payload["chunks"][0]["evidence_spans"]
        )
        assert evidence_spans == [
            {
                "span_id": combined_span["span_id"],
                "chunk_id": "chunk-1",
                "start_offset": 0,
                "end_offset": len(text),
                "text_sha256": combined_span["text_sha256"],
                "text": text,
            }
        ]
        serialized = json.dumps(payload)
        for component in combined_span["evidence_components"]:
            assert component["source_span_id"] not in serialized

    assert (
        resolver_spans[combined_span["span_id"]]["evidence_components"]
        == (combined_span["evidence_components"])
    )


def test_projected_combined_span_roundtrips_with_component_provenance() -> None:
    text = "The result changed from 10.1 percent to 9.7 percent."
    boundary = text.index("to")
    spans = [
        _span("chunk-1", text, 0, boundary),
        _span("chunk-1", text, boundary, len(text)),
    ]
    combined_span = generation._coalesce_source_spans(
        spans, {"chunk-1": {"chunk_id": "chunk-1", "text": text}}
    )[0]
    projected = generation._model_source_span(combined_span)

    resolved = generation._resolve_source_span(
        {"source_span_id": projected["span_id"]},
        {combined_span["span_id"]: combined_span},
        reason_code="test_span_missing",
    )

    assert resolved["source_span_id"] == combined_span["span_id"]
    assert resolved["evidence_quote"] == text
    assert resolved["locator"] == {
        "chunk_id": "chunk-1",
        "start_offset": 0,
        "end_offset": len(text),
    }
    assert resolved["source_span_ids"] == [row["span_id"] for row in spans]
    assert resolved["evidence_components"] == combined_span["evidence_components"]


def test_resolver_rejects_component_unknown_and_cross_source_ids() -> None:
    text = "The source reports 9.7 percent."
    boundary = text.index("9.7")
    span = generation._coalesce_source_spans(
        [
            _span("chunk-1", text, 0, boundary),
            _span("chunk-1", text, boundary, len(text)),
        ],
        {"chunk-1": {"chunk_id": "chunk-1", "text": text}},
    )[0]
    other_source_span = _span("chunk-from-other-source", text, 0, len(text))
    spans_by_id = {span["span_id"]: span}

    for invalid_id in (
        span["evidence_components"][0]["source_span_id"],
        "unknown-source-span",
        other_source_span["span_id"],
    ):
        with pytest.raises(CandidateRejectedError, match="does not exist"):
            generation._resolve_source_span(
                {"source_span_id": invalid_id},
                spans_by_id,
                reason_code="test_span_missing",
            )
