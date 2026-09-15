from __future__ import annotations

from copy import deepcopy

import pytest

from arctic_qa import generation, streaming


SCOPE_KEYS = (
    "geography",
    "population",
    "period",
    "method",
    "comparison",
    "uncertainty",
)


def _scope(**values: str) -> dict[str, str | None]:
    return {key: values.get(key) for key in SCOPE_KEYS}


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
        ],
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
        "question_verification_contract_version": generation.QUESTION_VERIFICATION_CONTRACT_VERSION,
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


@pytest.mark.parametrize(
    ("evidence", "question", "answer_text", "scope"),
    [
        (
            "At Station A, acoustic detections of bowhead whales were greatest in fall and spring during the 2020 survey.",
            "At Station A, during the 2020 survey, which seasons had the greatest acoustic detections of bowhead whales?",
            "fall and spring",
            _scope(
                geography="Station A",
                population="bowhead whales",
                period="2020 survey",
                method="acoustic detections",
            ),
        ),
        (
            "At Station NWP01, acoustic detections of narwhals were nearly continuous throughout the year and occurred near the fjord mouth.",
            "At Station NWP01, for acoustic detections of narwhals throughout the year, where did they occur?",
            "near the fjord mouth",
            _scope(
                geography="Station NWP01",
                population="narwhals",
                period="throughout the year",
                method="acoustic detections",
            ),
        ),
        (
            "The DCM Arctic Ocean samples contained seven groups of unique Cu(II)-IMAC-SPE components.",
            "For the DCM Arctic Ocean samples, how many groups of unique Cu(II)-IMAC-SPE components were reported?",
            "seven groups",
            _scope(
                population="DCM Arctic Ocean samples",
                method="Cu(II)-IMAC-SPE",
            ),
        ),
        (
            "At Station A during summer, the relationship between salinity and methane concentration was negative compared with open water.",
            "At Station A during summer, what relationship between salinity and methane concentration was reported compared with open water?",
            "negative",
            _scope(
                geography="Station A",
                period="summer",
                comparison="compared with open water",
            ),
        ),
    ],
)
def test_scope_roles_keep_answer_targets_out_of_independent_qualifiers(
    evidence: str,
    question: str,
    answer_text: str,
    scope: dict[str, str | None],
) -> None:
    answer, reconstruction, verification = _records(evidence, answer_text, scope)
    question_context = ""
    if "NWP01" in question:
        question_context = "NWP01 identifies the named Arctic fjord-mouth station."
    elif "DCM" in question:
        question_context = (
            "DCM means deep chlorophyll maximum. IMAC-SPE is the stated "
            "metal-affinity extraction method."
        )
    if question_context:
        verification["question_context_required"] = True

    assert (
        generation._qa_gate_reasons(
            {"chunk_id": "chunk-1", "text": evidence},
            question,
            answer,
            reconstruction,
            verification,
            question_context,
            standalone_verification=_standalone(),
        )
        == []
    )
    assert answer_text not in [value for value in scope.values() if value]


@pytest.mark.parametrize(
    ("scope", "question"),
    [
        (_scope(geography="Station A"), "What was reported during summer?"),
        (_scope(period="summer"), "What was reported at Station A during winter?"),
        (
            _scope(population="DCM Arctic Ocean samples"),
            "How many groups were reported?",
        ),
        (
            _scope(comparison="compared with open water"),
            "What was reported at Station A?",
        ),
        (
            _scope(comparison="under ice-covered conditions"),
            "What was reported in open water?",
        ),
    ],
)
def test_scope_binding_rejects_missing_or_changed_independent_qualifiers(
    scope: dict[str, str | None], question: str
) -> None:
    evidence = "At Station A during summer, DCM Arctic Ocean samples differed compared with open water under ice-covered conditions."
    answer, reconstruction, verification = _records(evidence, "a result", scope)

    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": evidence},
        question,
        answer,
        reconstruction,
        verification,
        standalone_verification=_standalone(),
    )

    assert reasons == ["scope_qualifier_missing"]


def test_scope_contract_keeps_sample_descriptors_as_population_and_requires_all_results() -> (
    None
):
    descriptions = generation.SCOPE_SCHEMA["properties"]

    assert generation.SCOPE_ROLE_SEMANTICS_VERSION == "scope-role-semantics-v2"
    assert (
        "Scope fields are not answer slots"
        in generation.SCOPE_ROLE_SEMANTICS_INSTRUCTIONS
    )
    assert (
        "sample descriptor remains population"
        in generation.SCOPE_ROLE_SEMANTICS_INSTRUCTIONS
    )
    assert "sample descriptor" in descriptions["population"]["description"]
    assert "Do not use geography" in descriptions["geography"]["description"]
    assert "ask for every value" in generation.QUESTION_ALIGNMENT_INSTRUCTIONS


def test_semantic_referent_failure_routes_as_one_revision_root() -> None:
    evidence = "Arctic samples in the sampled group showed higher abundance."
    scope = _scope(population="Arctic samples")
    answer, reconstruction, verification = _records(
        evidence,
        "higher abundance",
        scope,
    )
    verification.update(
        {
            "relation_scope_match": False,
            "question_context_required": True,
            "question_context_referent_resolved": False,
            "question_context_missing_detail": "the sampled group's identity",
        }
    )
    question = "What was observed in the sampled group of Arctic samples?"
    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": evidence},
        question,
        answer,
        reconstruction,
        verification,
        "Arctic samples were collected during the spring survey.",
        standalone_verification=_standalone(),
    )

    assert reasons == [
        "relation_scope_mismatch",
        "question_context_referent_unresolved",
    ]
    primary = streaming._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )
    revision = streaming._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths={(1, 0): {"attempt": primary, "candidate": None}},
        failed_path={"attempt": primary, "candidate": None},
        reason_codes=reasons,
    )

    assert revision is not None
    assert revision["attempt_kind"] == "surgical_correction"
    assert revision["trigger_reason_code"] == "question_context_referent_unresolved"
    assert revision["question_revision_index"] == 1


def test_independent_semantic_and_entailment_failures_repair_the_evidence_first() -> (
    None
):
    primary = streaming._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )

    # The paper-support signal is never collapsed and evidence outranks
    # context, so the repair answers the entailment defect, not the wording.
    repair = streaming._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths={(1, 0): {"attempt": primary, "candidate": None}},
        failed_path={"attempt": primary, "candidate": None},
        reason_codes=[
            "question_context_referent_unresolved",
            "source_entailment_not_verified",
        ],
    )

    assert repair is not None
    assert repair["trigger_reason_code"] == "source_entailment_not_verified"
    assert repair["attempt_kind"] == "question_revision"


def test_frozen_answer_phrase_routes_alternative_after_raw_leak_reason() -> None:
    evidence = "Nunavut was recorded."
    answer, reconstruction, verification = _records(
        evidence,
        "Nunavut",
        _scope(geography="Nunavut"),
    )
    question = "Which territory was recorded as Nunavut?"
    reasons = generation._qa_gate_reasons(
        {"chunk_id": "chunk-1", "text": evidence},
        question,
        answer,
        reconstruction,
        verification,
        standalone_verification=_standalone(),
    )

    assert "question_answer_leakage" in reasons
    assert "finding_answer_phrase_in_required_question_phrases" in reasons
    primary = streaming._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )
    alternative = streaming._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths={
            (1, 0): {
                "attempt": primary,
                "candidate": {
                    "item_id": "item-primary",
                    "candidate_json": '{"answer":{"source_span_id":"span-primary"}}',
                },
            }
        },
        failed_path={
            "attempt": primary,
            "candidate": {
                "item_id": "item-primary",
                "candidate_json": '{"answer":{"source_span_id":"span-primary"}}',
            },
        },
        reason_codes=reasons,
    )

    assert alternative is not None
    assert alternative["attempt_kind"] == "alternative_finding"
    assert alternative["trigger_reason_code"] == (
        "finding_answer_phrase_in_required_question_phrases"
    )
