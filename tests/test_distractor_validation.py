from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from arctic_qa import validation
from arctic_qa.util import sha256_bytes, stable_id


def _depth_tuple_answer() -> dict[str, object]:
    return {
        "text": "15 m, 75 m, and 155 m",
        "evidence_quote": (
            "at 15 m (left), 75 m (middle), and 155 m (right) depths"
        ),
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
        validation._text_display_issue(
            text, answer=answer, distractor=distractor
        )
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
