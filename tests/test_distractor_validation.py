from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from arctic_qa import validation
from arctic_qa.util import sha256_bytes, stable_id


R14_FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "r14-audit-priorities-r1.json").read_text(
        encoding="utf-8"
    )
)


def _depth_tuple_answer() -> dict[str, object]:
    return {
        "text": "15 m, 75 m, and 155 m",
        "evidence_quote": ("at 15 m (left), 75 m (middle), and 155 m (right) depths"),
        "deterministic_rule": {
            "kind": "closed_set",
            "source_values": ["15 m (left)", "75 m (middle)", "155 m (right)"],
        },
    }


def _depth_tuple_distractor(text: str) -> dict[str, object]:
    return {
        "text": text,
        "deterministic": {
            "kind": "closed_set",
            "candidate_relation": "contradicts",
            "candidate_value": text,
        },
    }


def _bind_span(record: dict[str, object], quote: str) -> None:
    locator = {"chunk_id": "chunk-1", "start_offset": 0, "end_offset": len(quote)}
    evidence_hash = sha256_bytes(quote.encode("utf-8"))
    record.update(
        {
            "evidence_quote": quote,
            "locator": locator,
            "evidence_text_sha256": evidence_hash,
            "span_contract_version": validation.LEGACY_SOURCE_SPAN_CONTRACT_VERSION,
            "source_span_id": stable_id(
                validation.LEGACY_SOURCE_SPAN_CONTRACT_VERSION,
                locator["chunk_id"],
                locator["start_offset"],
                locator["end_offset"],
                evidence_hash,
            ),
        }
    )


def test_closed_set_depth_tuple_allows_conjunction_and_typed_contradiction() -> None:
    answer = _depth_tuple_answer()
    distractor = _depth_tuple_distractor("10 m, 50 m, and 100 m")

    assert (
        validation._text_display_issue(
            distractor["text"], answer=answer, distractor=distractor
        )
        is None
    )
    assert validation._source_bound_typed_incompatibility(answer, distractor)


def test_validate_distractor_accepts_the_closed_set_depth_tuple() -> None:
    quote = "at 15 m (left), 75 m (middle), and 155 m (right) depths"
    answer = _depth_tuple_answer()
    distractor = _depth_tuple_distractor("10 m, 50 m, and 100 m")
    _bind_span(distractor, quote)
    candidate = {
        "answer": answer,
        "source": {"content_hash": "source-hash"},
        "finding_id": "finding-1",
        "provenance": {"run_id": "run-1", "generation_arm": "answer_first"},
    }
    verdict_payload = {
        "contradiction_established": True,
        "alternative_answer_search_passed": True,
        "true_in_different_context": False,
        "question_admits_option_as_correct": False,
        "evidence_quote": quote,
        "locator": distractor["locator"],
        "rationale": "The tuple contradicts the reported depths.",
    }
    verdict = {
        "source_hash": "source-hash",
        "qa_hash": "qa-hash",
        "option_hash": "option-hash",
        "option_text": distractor["text"],
        **verdict_payload,
        "provenance": {
            "role": "option_verifier",
            "provider": "fake",
            "requested_model": "fake-verifier",
            "returned_model": "fake-verifier",
            "request_id": "request-1",
            "prompt_version": "test-only",
            "prompt_hash": "prompt-1",
        },
    }
    db = Mock()
    db.one.return_value = {
        "response_json": json.dumps(verdict_payload),
        "returned_model": "fake-verifier",
        "request_id": "request-1",
    }

    result = validation.validate_distractor(
        db,
        candidate,
        distractor,
        {"chunk-1": {"text": quote}},
        verdict,
        "qa-hash",
        "option-hash",
        False,
    )

    assert result["accepted"] is True
    assert result["deterministic"] is True
    assert result["reasons"] == []


@pytest.mark.parametrize(
    "text",
    [
        "10 m, 50 m, and the trend increased",
        "10 m, 50 m, or 100 m",
        "10 m and 50 m",
        "alpha and beta",
        "10 m, 50 m, and 100 m; the trend increased",
    ],
)
def test_closed_set_conjunction_guard_rejects_non_tuple_compounds(text: str) -> None:
    answer = _depth_tuple_answer()
    distractor = _depth_tuple_distractor(text)

    assert (
        validation._text_display_issue(text, answer=answer, distractor=distractor)
        == "displayed_assertion_compound"
    )


def test_conjunction_guard_requires_the_declared_closed_set_contract() -> None:
    answer = _depth_tuple_answer()
    answer["deterministic_rule"] = {
        "kind": "directional_relation",
        "source_value": "increased",
    }
    distractor = _depth_tuple_distractor("10 m, 50 m, and 100 m")

    assert (
        validation._text_display_issue(
            distractor["text"], answer=answer, distractor=distractor
        )
        == "displayed_assertion_compound"
    )


def test_r14_bumblebee_attempts_satisfy_categorical_closed_set_contract() -> None:
    answer = {
        "text": R14_FIXTURE["closed_set_answer"],
        "evidence_quote": R14_FIXTURE["closed_set_evidence"],
        "deterministic_rule": {
            "kind": "closed_set",
            "source_values": R14_FIXTURE["closed_set_source_values"],
            "member_type": "categorical_entity",
            "ordering": "unordered",
        },
    }

    for attempt in R14_FIXTURE["bumblebee_attempts"]:
        assert len(attempt) == 3
        for text in attempt:
            distractor = {
                "text": text,
                "deterministic": {
                    "kind": "closed_set",
                    "candidate_value": text,
                    "candidate_values": [
                        part.strip()
                        for part in text.replace(", and ", ", ").split(", ")
                    ],
                    "member_type": "categorical_entity",
                    "ordering": "unordered",
                },
            }
            assert (
                validation._text_display_issue(
                    text, answer=answer, distractor=distractor
                )
                is None
            )
            assert validation._source_bound_typed_incompatibility(answer, distractor)


def test_categorical_closed_set_rejects_non_exhaustive_and_mixed_claims() -> None:
    answer = {
        "text": "alpha, beta, and gamma",
        "evidence_quote": "Observed taxa included alpha, beta, and gamma.",
        "deterministic_rule": {
            "kind": "closed_set",
            "source_values": ["alpha", "beta", "gamma"],
            "member_type": "categorical_value",
            "ordering": "unordered",
        },
    }
    mixed = _depth_tuple_distractor("alpha, beta, and abundance increased")

    assert validation._categorical_closed_set_contract(answer, mixed) is None
    assert (
        validation._text_display_issue(mixed["text"], answer=answer, distractor=mixed)
        == "displayed_assertion_compound"
    )

    for non_exhaustive in (
        "Samples were only measured in summer. Examples include alpha, beta, and gamma.",
        "Observed taxa included alpha, beta, and gamma.",
    ):
        answer["evidence_quote"] = non_exhaustive
        assert validation._categorical_closed_set_contract(answer, mixed) is None

    answer["evidence_quote"] = "Exactly three taxa were alpha, beta, and gamma."
    for invalid_text in (
        "alpha and beta",
        "alpha, alpha, and delta",
    ):
        invalid = {
            "text": invalid_text,
            "deterministic": {
                "kind": "closed_set",
                "candidate_value": invalid_text,
            },
        }
        assert validation._categorical_closed_set_contract(answer, invalid) is None


def test_categorical_closed_set_honors_order_and_equivalent_membership() -> None:
    answer = {
        "text": "alpha, beta, and gamma",
        "evidence_quote": "The ordered sequence consists of alpha, beta, and gamma.",
        "deterministic_rule": {
            "kind": "closed_set",
            "source_values": ["alpha", "beta", "gamma"],
            "member_type": "categorical_value",
            "ordering": "ordered",
        },
    }
    swapped = {
        "text": "beta, alpha, and gamma",
        "deterministic": {
            "kind": "closed_set",
            "candidate_value": "beta, alpha, and gamma",
            "candidate_values": ["beta", "alpha", "gamma"],
            "member_type": "categorical_value",
            "ordering": "ordered",
        },
    }

    assert validation._source_bound_typed_incompatibility(answer, swapped)
    answer["deterministic_rule"]["ordering"] = "unordered"
    swapped["deterministic"]["ordering"] = "unordered"
    assert not validation._source_bound_typed_incompatibility(answer, swapped)
    assert validation._option_equivalence_key(answer, swapped) == (
        "closed_set",
        "unordered",
        "alpha",
        "beta",
        "gamma",
    )


R15_OPTION_PAYLOADS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "r15-audit-priorities-r1.json").read_text(
        encoding="utf-8"
    )
)["option_payloads"]


def _payload(item_id: str) -> dict[str, object]:
    return R15_OPTION_PAYLOADS[item_id]


def test_r15_option_payloads_are_frozen_for_every_named_defect() -> None:
    assert set(R15_OPTION_PAYLOADS) == {
        "aqa-2af2b346d30478f68400",
        "aqa-247e683b6c742bae46fa",
        "aqa-a3843bad70b6db3f6bf0",
        "aqa-3e74c2c169f63d6686e4",
        "aqa-aa01612494c63919f347",
        "aqa-a4a455748ac2480b2650",
        "aqa-a8ac70980059cdfd7001",
    }
    for payload in R15_OPTION_PAYLOADS.values():
        assert payload["answer"]["evidence_quote"]
        assert len(payload["distractors"]) >= 4


def test_structural_parallelism_keeps_every_two_part_directional_option() -> None:
    """DW-1: a two-part answer lost all four two-part options."""
    payload = _payload("aqa-2af2b346d30478f68400")
    answer = payload["answer"]

    for distractor in payload["distractors"]:
        assert (
            validation._text_display_issue(
                distractor["text"], answer=answer, distractor=distractor
            )
            is None
        )


def test_structural_parallelism_never_exempts_a_compound_against_an_atomic_answer() -> (
    None
):
    answer = {
        "text": "increased",
        "evidence_quote": "Sea ice extent increased over the record.",
        "deterministic_rule": {
            "kind": "directional_relation",
            "source_value": "increased",
        },
    }
    compound = {
        "text": "increased and then decreased",
        "deterministic": {"kind": "directional_relation"},
    }

    assert (
        validation._text_display_issue(
            compound["text"], answer=answer, distractor=compound
        )
        == "displayed_assertion_compound"
    )


def test_structural_parallelism_requires_the_answer_shape_to_be_source_bound() -> None:
    answer = {
        "text": "stronger and more persistent",
        "evidence_quote": "The pattern was stronger in winter.",
        "deterministic_rule": {
            "kind": "directional_relation",
            "source_value": "stronger and more persistent",
        },
    }
    option = {
        "text": "weaker and less persistent",
        "deterministic": {"kind": "directional_relation"},
    }

    assert (
        validation._text_display_issue(option["text"], answer=answer, distractor=option)
        == "displayed_assertion_compound"
    )


def test_member_parser_reads_followed_by_then_and_rank_prefixes() -> None:
    """DW-2: 'followed by' made a three-member answer parse as two members."""
    payload = _payload("aqa-247e683b6c742bae46fa")

    assert validation._displayed_categorical_members(payload["answer"]["text"]) == [
        "proteobacteria",
        "bacteroidetes",
        "epsilonbacteraeota",
    ]
    assert validation._displayed_categorical_members("alpha then beta then gamma") == [
        "alpha",
        "beta",
        "gamma",
    ]
    assert validation._displayed_categorical_members(
        "class Alpha > class Beta > class Gamma"
    ) == ["alpha", "beta", "gamma"]


def test_member_parser_reads_an_enumeration_lead_and_a_typed_member_array() -> None:
    assert validation._displayed_categorical_members(
        "anaerobic taxa including denitrifiers, sulfate reducers, and methanogens"
    ) == ["denitrifiers", "sulfate reducers", "methanogens"]
    assert validation._displayed_categorical_members(
        "groups such as alpha, beta, and gamma"
    ) == ["alpha", "beta", "gamma"]
    assert validation._displayed_categorical_members(
        "ignored display", ["phylum Alpha", "Beta", "Gamma"]
    ) == ["alpha", "beta", "gamma"]


def test_including_enumeration_still_fails_the_completeness_contract() -> None:
    """D5: the reported-enumeration contract was refuted and is not built."""
    payload = _payload("aqa-aa01612494c63919f347")
    answer = payload["answer"]

    for distractor in payload["distractors"]:
        assert validation._categorical_closed_set_contract(answer, distractor) is None


def test_single_member_closed_set_takes_the_unique_categorical_path() -> None:
    """DW-2: a one-member closed set failed every option on contract_invalid."""
    payload = _payload("aqa-3e74c2c169f63d6686e4")
    answer_rule = payload["answer"]["deterministic_rule"]

    assert validation._is_single_member_closed_set(answer_rule) is True
    assert (
        validation._is_single_member_closed_set(
            {"kind": "closed_set", "source_values": ["a", "b"]}
        )
        is False
    )


def test_numeric_display_reads_word_integers_and_a_unicode_minus() -> None:
    """Root cause B: spelling removed 4 of 4 and 3 of 4 correct options."""
    counts = _payload("aqa-3e74c2c169f63d6686e4")
    minus = _payload("aqa-a8ac70980059cdfd7001")

    for distractor in counts["distractors"]:
        assert (
            validation._numeric_display_issue(distractor["text"], distractor["numeric"])
            is None
        )
    for distractor in minus["distractors"]:
        assert (
            validation._numeric_display_issue(distractor["text"], distractor["numeric"])
            is None
        )


def test_numeric_display_matches_a_compound_unit_as_a_substring() -> None:
    assert (
        validation._numeric_display_issue(
            "0.35 ppm yr-1", {"canonical_value": "0.35", "unit": "ppm yr-1"}
        )
        is None
    )
    assert (
        validation._numeric_display_issue(
            "-0.38 ug N2O-N m-2 h-1",
            {"canonical_value": "-0.38", "unit": "ug N2O-N m-2 h-1"},
        )
        is None
    )
    assert (
        validation._numeric_display_issue(
            "1.8 cm", {"canonical_value": "2.0", "unit": "cm"}
        )
        == "numeric_metadata_display_mismatch"
    )


def test_numeric_display_still_refuses_a_range_and_a_non_scalar() -> None:
    payload = _payload("aqa-a4a455748ac2480b2650")

    for distractor in payload["distractors"]:
        assert (
            validation._numeric_display_issue(distractor["text"], distractor["numeric"])
            == "numeric_metadata_not_scalar"
        )
    assert (
        validation._numeric_display_issue(
            "5.5 to 6.9 nM", {"canonical_value": "6.9", "unit": "nM"}
        )
        == "numeric_display_ambiguous"
    )


def test_categorical_option_kinds_must_mirror_a_closed_set_answer() -> None:
    """An option that declares a different kind gets no exemption."""
    payload = _payload("aqa-a3843bad70b6db3f6bf0")
    answer = payload["answer"]
    compound = [
        distractor
        for distractor in payload["distractors"]
        if "," in str(distractor["text"])
    ]

    assert compound
    for distractor in compound:
        assert (
            validation._text_display_issue(
                distractor["text"], answer=answer, distractor=distractor
            )
            == "displayed_assertion_compound"
        )


def test_compound_option_without_a_deterministic_contradiction_is_refused() -> None:
    """r15 audit section 4.8 item 3."""
    candidate = {"provenance": {"author_model": "gemini-3.8-flash"}}
    compound = {"text": "stronger and more persistent"}
    atomic = {"text": "stronger"}
    same_family = {"provenance": {"requested_model": "gemini-3.8-flash"}}
    other_family = {"provenance": {"requested_model": "claude-opus-5"}}

    assert validation._option_needs_independent_support(compound) is True
    assert validation._option_needs_independent_support(atomic) is False
    assert validation._option_verdict_is_independent(candidate, same_family) is False
    assert validation._option_verdict_is_independent(candidate, other_family) is True
