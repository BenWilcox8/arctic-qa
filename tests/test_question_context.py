from __future__ import annotations

from arctic_qa import generation
from arctic_qa.exporting import _absent_mcq, _present_mcq, _short_answer
from arctic_qa.validation import (
    question_context_leaks_answer,
    question_context_verification_reason,
)


def _verification(*, required: bool) -> dict[str, bool]:
    return {
        "question_context_required": required,
        "question_context_source_supported": True,
        "question_context_answer_leakage_absent": True,
    }


def test_required_acronym_context_passes_context_gates() -> None:
    context = "SST means sea surface temperature in this study."
    answer = {"text": "lower during winter"}

    assert question_context_verification_reason(
        context, answer, _verification(required=True)
    ) is None


def test_self_contained_question_uses_empty_context() -> None:
    assert question_context_verification_reason(
        "", {"text": "gravel"}, _verification(required=False)
    ) is None


def test_acronym_expansion_that_is_the_answer_fails_closed() -> None:
    context = "ALT means active layer thickness."
    answer = {"text": "active layer thickness"}

    assert question_context_leaks_answer(context, answer)
    assert (
        question_context_verification_reason(
            context, answer, _verification(required=True)
        )
        == "question_context_answer_leakage"
    )


def test_answer_bearing_number_in_context_fails_closed() -> None:
    context = "The sampled water depth was 2.0 m."
    answer = {"text": "2.0 m", "variants": ["200 cm"]}

    assert question_context_leaks_answer(context, answer)


def test_question_roles_require_separate_context_and_verifier_gates() -> None:
    for role in ("question_writer", "direct_joint"):
        schema = generation.ROLE_SCHEMAS[role]
        assert "question_context" in schema["required"]
        assert schema["properties"]["question_context"] == {"type": "string"}

    verifier = generation.ROLE_SCHEMAS["answer_verifier"]
    for field in (
        "question_context_required",
        "question_context_source_supported",
        "question_context_answer_leakage_absent",
    ):
        assert field in verifier["required"]
    assert "If an acronym expansion answers the question" in (
        generation.QUESTION_CONTEXT_INSTRUCTIONS
    )


def test_exports_default_legacy_context_and_serialize_new_context() -> None:
    candidate = {
        "item_id": "item-1",
        "question": "What substrate was reported?",
        "answer": {
            "text": "gravel",
            "variants": [],
            "evidence_quote": "The only reported substrate category was gravel.",
            "locator": {"chunk_id": "chunk-1", "start_offset": 0, "end_offset": 48},
            "scope": {},
        },
        "source": {"source_id": "source-1"},
        "release_label": "machine_accepted_unverified",
        "provenance": {},
    }

    assert _short_answer(candidate)["question_context"] == ""
    assert _present_mcq(candidate, [], "seed")["question_context"] == ""
    assert _absent_mcq(candidate, [], "seed")["question_context"] == ""

    candidate["question_context"] = "Substrate means the seafloor material."
    assert _short_answer(candidate)["question_context"] == candidate["question_context"]
    assert _present_mcq(candidate, [], "seed")["question_context"] == (
        candidate["question_context"]
    )
