from __future__ import annotations

from arctic_qa import generation
from arctic_qa.exporting import _absent_mcq, _present_mcq, _short_answer
from arctic_qa.validation import (
    benchmark_context_verification_reason,
    option_context_verification_reason,
    question_answer_leaks_answer,
    question_context_leaks_answer,
    question_context_verification_reason,
    required_question_phrases_contain_answer,
)


def _verification(*, required: bool) -> dict[str, bool]:
    return {
        "question_context_required": required,
        "question_context_source_supported": True,
        "question_context_answer_leakage_absent": True,
    }


def test_required_acronym_context_passes_context_gates() -> None:
    context = "SST means sea surface temperature in coastal water samples at Site A."
    answer = {"text": "lower during winter"}

    assert question_context_verification_reason(
        context, answer, _verification(required=True)
    ) is None


def test_self_contained_question_uses_empty_context() -> None:
    assert question_context_verification_reason(
        "", {"text": "gravel"}, _verification(required=False)
    ) is None


def test_study_local_station_and_species_need_grounded_context() -> None:
    question = (
        "What was the sea ice condition at the southern station (74.5 °N) "
        "during the summer solstice sampling of C. finmarchicus?"
    )
    answer = {"text": "sea ice-free"}

    assert (
        question_context_verification_reason(
            "", answer, _verification(required=False), question=question
        )
        == "question_context_missing"
    )


def test_a_coordinate_alone_does_not_resolve_a_study_local_station() -> None:
    question = "What was measured at the southern station (74.5 °N)?"
    verification = _verification(required=True)

    assert (
        question_context_verification_reason(
            "The southern station was at 74.5 °N.",
            {"text": "sea ice-free"},
            verification,
            question=question,
        )
        == "question_context_referent_unresolved"
    )


def test_grounded_context_resolves_study_local_station_and_species() -> None:
    question = (
        "What was the sea ice condition at the southern station (74.5 °N) "
        "during the summer solstice sampling of C. finmarchicus?"
    )
    context = (
        "Calanus finmarchicus samples were collected at two high Arctic stations "
        "during summer solstice sampling."
    )

    assert (
        question_context_verification_reason(
            context,
            {"text": "sea ice-free"},
            _verification(required=True),
            question=question,
        )
        is None
    )


def test_question_verifier_can_report_a_semantic_referent_gap() -> None:
    question = "What was observed in the sampled group?"
    context = "Arctic samples were collected during the spring survey."
    verification = {
        **_verification(required=True),
        "question_context_referent_resolved": False,
        "question_context_missing_detail": "the sampled group's identity",
    }

    assert (
        question_context_verification_reason(
            context,
            {"text": "higher abundance"},
            verification,
            question=question,
        )
        == "question_context_referent_unresolved"
    )


def test_question_creation_and_options_share_referent_gate() -> None:
    question = "What was observed at the southern station?"

    assert benchmark_context_verification_reason(question, "") == (
        "question_context_missing"
    )
    assert option_context_verification_reason("the sampled group", "") == (
        "option_context_missing"
    )
    assert option_context_verification_reason(
        "the sampled group", "Arctic samples from the southern station"
    ) is None


def test_explicit_answer_in_question_fails_creation_and_verification() -> None:
    question = (
        "Which Canadian territory is associated with Grise Fiord, "
        "with the correct answer being Nunavut?"
    )
    answer = {"text": "Nunavut"}

    assert question_answer_leaks_answer(question, answer)
    assert (
        question_context_verification_reason(
            question,
            answer,
            _verification(required=False),
            question=question,
        )
        == "question_answer_leakage"
    )


def test_required_question_phrase_cannot_contain_answer_or_variant() -> None:
    assert required_question_phrases_contain_answer(
        {"text": "Nunavut", "variants": ["NU"], "required_question_phrases": ["Nunavut"]}
    )
    assert not required_question_phrases_contain_answer(
        {
            "text": "Nunavut",
            "variants": ["NU"],
            "required_question_phrases": ["Grise Fiord"],
        }
    )


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
    assert "question_context_referent_resolved" in verifier["properties"]
    assert "question_context_missing_detail" in verifier["properties"]
    assert "question_answer_leakage_absent" in verifier["properties"]
    assert "If an acronym expansion answers the question" in (
        generation.QUESTION_CONTEXT_INSTRUCTIONS
    )


def test_standalone_wording_instructions_cover_scope_and_otu_context() -> None:
    instructions = generation.BENCHMARK_STANDALONE_INSTRUCTIONS
    context_instructions = generation.QUESTION_CONTEXT_INSTRUCTIONS

    assert "reader who cannot see the source paper" in instructions
    assert "actual system, location, samples, period, and conditions" in instructions
    assert "only when SOURCE_DATA supports it" in instructions
    assert "Do not invent a missing detail" in instructions
    assert "paper-specific observation" in instructions
    assert "source-dependent shorthand" in instructions
    assert "according to the study" in instructions
    assert "at this time" in instructions
    assert "determine or verify the answer" in instructions
    assert "identify a referent or interpret scope" in instructions
    assert "exact evidence quotes, source locators, or rationale fields" in instructions
    assert "the southern station" in instructions
    assert "latitude alone does not identify a station or event" in instructions
    assert "Never state the proposed answer" in instructions
    assert "operational taxonomic units (OTUs)" in context_instructions
    assert "source-supported sample and location context" in context_instructions
    assert "taxonomic counts" in context_instructions
    assert "only when its expansion occurs in SOURCE_DATA" in context_instructions
    assert "source-supported subject, place, time, sample, or event" in context_instructions


def test_reconstructor_omits_inapplicable_numeric_metadata() -> None:
    instructions = generation.RECONSTRUCTION_NUMERIC_INSTRUCTIONS

    assert "one scalar value" in instructions
    assert "ranges" in instructions
    assert "directional answers" in instructions
    assert "string 'null'" in instructions
    assert "Do not add p-values" in instructions


def test_generation_attempt_contract_rejects_unbounded_paths() -> None:
    primary = {
        "contract_version": "bounded-paper-progression-v2",
        "attempt_id": "attempt-1",
        "attempt_kind": "primary",
        "finding_attempt_index": 1,
        "question_revision_index": 0,
        "parent_attempt_id": None,
        "parent_item_id": None,
        "trigger_reason_code": None,
        "finding_policy_version": (
            generation.SCOPE_ROLE_FINDING_POLICY_VERSION + ":finding-1"
        ),
        "excluded_finding_span_ids": [],
    }

    assert generation._validated_generation_attempt(primary) == primary
    primary["finding_attempt_index"] = 3
    try:
        generation._validated_generation_attempt(primary)
    except ValueError as error:
        assert "bounded contract" in str(error)
    else:
        raise AssertionError("an unbounded finding path was accepted")


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
