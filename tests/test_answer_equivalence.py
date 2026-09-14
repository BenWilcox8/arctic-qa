from __future__ import annotations

from copy import deepcopy

import pytest

from arctic_qa import validation


def _retained_directional_record() -> tuple[dict[str, object], dict[str, object]]:
    evidence = (
        "Due to the partly differing analytical methods for environmental\n"
        "parameters and light conditions between the two cruises, separate statistical\n"
        "analyses were performed for CAO (including the CAO-influenced Station\n"
        "50 in the Wandel Sea) and MIZ (Table 1). Nitrogen fixation rates in the\n"
        "CAO correlated positively with primary production (generalised linear\n"
        "model (GLM), p = 0.004; Figure S6). It was also positively correlated with\n"
        "photosynthetic picoeukaryotes (PPE) and negatively correlated with\n"
        "ammonium (NH4+) (GLM, pPPE = 0.015, pNH4 = 0.021)."
    )
    return (
        {
            "text": (
                "Nitrogen fixation rates in the CAO correlated positively with "
                "primary production (generalised linear model (GLM), p = 0.004; "
                "Figure S6)."
            ),
            "evidence_quote": evidence,
            "deterministic_rule": {
                "kind": "directional_relation",
                "source_value": "positively correlated",
            },
            "required_question_phrases": [
                "Nitrogen fixation rates",
                "CAO",
                "primary production",
            ],
        },
        {
            "answer": "positively",
            "alternatives": [],
            "scope": {
                "comparison": None,
                "geography": "CAO",
                "method": "generalised linear model",
                "period": None,
                "population": "Nitrogen fixation rates",
                "uncertainty": None,
            },
        },
    )


def _retained_percentage_record() -> tuple[dict[str, object], dict[str, object]]:
    evidence = (
        "Nielsen et al. (2019) determined the organic mass component in PM1 to "
        "be on average 24 % over the period February–May at Villum. However, "
        "the organic component of the aerosol mass in the Arctic is highly "
        "size-dependent in the particle size range probed by both the CCN "
        "counter and HTDMA in this study."
    )
    return (
        {
            "text": (
                "Nielsen et al. (2019) determined the organic mass component in "
                "PM1 to be on average 24 % over the period February–May at Villum."
            ),
            "variants": [
                "Over the period February–May at Villum, Nielsen et al. (2019) "
                "determined the organic mass component in PM1 to be on average 24 %."
            ],
            "evidence_quote": evidence,
            "required_question_phrases": [
                "organic mass component in PM1",
                "February–May",
                "Villum",
            ],
            "scope": {
                "comparison": None,
                "geography": "Villum",
                "method": None,
                "period": "February–May",
                "population": None,
                "uncertainty": None,
            },
        },
        {
            "answer": "24 %",
            "alternatives": ["24 %", "24 percent"],
            "numeric": {"canonical_value": "24", "unit": "%"},
            "scope": {
                "comparison": None,
                "geography": "Villum",
                "method": None,
                "period": "February–May",
                "population": "organic mass component in PM1",
                "uncertainty": None,
            },
        },
    )


def _retained_approximate_range_record() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "text": "approximately 0.5 to 0.8 m beneath the ice bottom",
            "variants": [
                "0.5 to 0.8 m beneath the ice bottom",
                "approximately 0.5 to 0.8 m beneath the\nice bottom",
            ],
            "evidence_quote": (
                "through a hydrohole in the ice, and the Vector transducer was "
                "positioned approximately 0.5 to 0.8 m beneath the"
            ),
            "required_question_phrases": ["Vector transducer", "beneath the"],
            "scope": {
                "comparison": None,
                "geography": None,
                "method": "deployed through a hydrohole in the ice",
                "period": None,
                "population": "Vector transducer",
                "uncertainty": None,
            },
        },
        {
            "answer": "approximately 0.5 to 0.8 m",
            "alternatives": ["approximately 0.5 to 0.8 m"],
            "numeric": {"canonical_value": "0.5 to 0.8", "unit": "m"},
            "scope": {
                "comparison": None,
                "geography": None,
                "method": None,
                "period": None,
                "population": None,
                "uncertainty": "approximately",
            },
        },
    )


def _retained_multi_value_record() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "text": (
                "Their respective shares of optimal forecasts are above 90%, 80%, "
                "and 70% respectively."
            ),
            "variants": [
                "Their respective shares of optimal forecasts are above 90%, 80%, "
                "and 70%."
            ],
            "evidence_quote": (
                "Their respective shares of optimal forecasts are above 90%, 80%, "
                "and 70% respectively. In"
            ),
            "deterministic_rule": {
                "kind": "closed_set",
                "source_values": ["above 90%", "80%", "70%"],
            },
            "numeric_rule": {
                "canonical_value": "90",
                "conversion_rule": "Direct percentage report",
                "reported_precision": "0",
                "rounding_rule": "none",
                "tolerance": "0",
                "tolerance_basis": "reported",
                "unit": "%",
            },
            "required_question_phrases": [
                "October",
                "shares of optimal forecasts",
                "March",
                "January",
            ],
        },
        {
            "answer": (
                "Their respective shares of optimal forecasts are above 90%, 80%, "
                "and 70% respectively."
            ),
            "alternatives": ["above 90%, 80%, and 70% respectively"],
            "numeric": {"canonical_value": "90%, 80%, and 70%", "unit": "%"},
            "scope": {
                "comparison": "shares of optimal forecasts across October, March, and January",
                "geography": None,
                "method": "Figure 4 analysis reporting fraction of days for which any FEML offers the lowest RMSFEs",
                "period": "120 days",
                "population": "forecasts for October, March, and January",
                "uncertainty": None,
            },
        },
    )


def _retained_direct_value_record() -> tuple[dict[str, object], dict[str, object]]:
    answer = {
        "text": "84.8%",
        "variants": ["84.8 percent", "84.8"],
        "evidence_quote": (
            "maximum biohydrogen fraction of 84.8% from the Ice 4 sample and "
            "85.0% from the Water 5"
        ),
        "numeric_rule": {
            "canonical_value": "84.8",
            "conversion_rule": "direct source reporting when no conversion occurs.",
            "reported_precision": "0.1",
            "rounding_rule": (
                "Direct reporting from the source span without additional rounding "
                "applied."
            ),
            "tolerance": "0",
            "tolerance_basis": "84.8%",
            "unit": "%",
        },
        "required_question_phrases": [
            "maximum biohydrogen fraction",
            "Ice 4 sample",
        ],
    }
    provenance = {
        "verification_calls": {
            "answer_verifier": {
                "role": "answer_verifier",
                "request_id": "6lemavaDKp7bz7IP_--l8AY",
            }
        }
    }
    return answer, provenance


def _audit_text_record(
    answer_text: str,
    reconstruction_text: str,
    numeric: dict[str, str],
) -> tuple[dict[str, object], dict[str, object]]:
    evidence = f"The source reports {answer_text}."
    return (
        {
            "text": answer_text,
            "evidence_quote": evidence,
            "scope": {"population": "the reported result"},
            "required_question_phrases": ["reported result"],
        },
        {
            "answer": reconstruction_text,
            "numeric": numeric,
            "scope": {"population": "the reported result"},
        },
    )


def test_retained_directional_phrase_accepts_its_single_direction() -> None:
    answer, reconstruction = _retained_directional_record()

    assert validation.reconstruction_matches(answer, reconstruction)


@pytest.mark.parametrize("rebuilt", ["negatively", "not positively"])
def test_retained_directional_phrase_rejects_polarity_and_negation(
    rebuilt: str,
) -> None:
    answer, reconstruction = _retained_directional_record()
    reconstruction["answer"] = rebuilt

    assert not validation.reconstruction_matches(answer, reconstruction)


def test_retained_percentage_uses_typed_value_and_scope() -> None:
    answer, reconstruction = _retained_percentage_record()

    assert validation.reconstruction_matches(answer, reconstruction)
    assert not validation.reconstruction_has_competing_alternatives(
        answer, reconstruction
    )


@pytest.mark.parametrize("alternative", ["25 percent", "24.0 percent"])
def test_typed_percentage_rejects_changed_alternative_value_or_precision(
    alternative: str,
) -> None:
    answer, reconstruction = _retained_percentage_record()
    reconstruction["alternatives"] = [alternative]

    assert validation.reconstruction_has_competing_alternatives(answer, reconstruction)


@pytest.mark.parametrize(
    "numeric",
    [
        {"canonical_value": "-24", "unit": "%"},
        {"canonical_value": "24.0", "unit": "%"},
        {"canonical_value": "24", "unit": "m"},
    ],
)
def test_typed_percentage_rejects_changed_sign_precision_or_unit(
    numeric: dict[str, str],
) -> None:
    answer, reconstruction = _retained_percentage_record()
    reconstruction["numeric"] = numeric

    assert not validation.reconstruction_matches(answer, reconstruction)


@pytest.mark.parametrize("missing_scope", ["period", "population", "geography"])
def test_typed_percentage_rejects_a_short_answer_without_a_required_condition(
    missing_scope: str,
) -> None:
    answer, reconstruction = _retained_percentage_record()
    reconstruction = deepcopy(reconstruction)
    reconstruction["scope"][missing_scope] = None  # type: ignore[index]

    assert not validation.reconstruction_matches(answer, reconstruction)


def test_retained_approximate_range_stays_rejected_without_typed_condition() -> None:
    answer, reconstruction = _retained_approximate_range_record()

    assert not validation.reconstruction_matches(answer, reconstruction)


@pytest.mark.parametrize(
    "rebuilt",
    [
        "0.5 to 0.8 m beneath the ice bottom",
        "approximately 0.5 to 0.9 m beneath the ice bottom",
        "approximately 0.5 to 0.8 cm beneath the ice bottom",
        "approximately 0.5 to 0.8 m beneath the snow bottom",
    ],
)
def test_complete_range_rejects_changed_qualifier_endpoint_unit_or_condition(
    rebuilt: str,
) -> None:
    answer = {
        "text": "approximately 0.5 to 0.8 m beneath the ice bottom",
        "evidence_quote": (
            "The transducer was approximately 0.5 to 0.8 m beneath the ice bottom."
        ),
    }

    assert not validation.reconstruction_matches(answer, {"answer": rebuilt})


def test_complete_range_accepts_safe_spelling_changes() -> None:
    answer = {
        "text": "approximately 0.5 to 0.8 m beneath the ice bottom",
        "evidence_quote": (
            "The transducer was approximately 0.5 to 0.8 m beneath the ice bottom."
        ),
    }

    assert validation.reconstruction_matches(
        answer, {"answer": "about 0.5-0.8 metres beneath the ice bottom"}
    )


def test_partial_text_reconstruction_cannot_satisfy_a_multi_part_answer() -> None:
    answer = {"text": "higher in winter and lower in summer", "variants": []}

    assert not validation.reconstruction_matches(answer, {"answer": "higher in winter"})


def test_retained_three_threshold_record_stays_rejected_without_structure() -> None:
    answer, reconstruction = _retained_multi_value_record()

    assert not validation.reconstruction_matches(answer, reconstruction)


def test_partial_multi_value_structure_cannot_represent_three_thresholds() -> None:
    answer, reconstruction = _retained_multi_value_record()
    numeric_rule = answer["numeric_rule"]  # type: ignore[index]
    numeric_rule["structure_contract_version"] = (  # type: ignore[index]
        validation.MULTI_VALUE_NUMERIC_CONTRACT_VERSION
    )
    numeric_rule["values"] = [  # type: ignore[index]
        {"canonical_value": "90", "unit": "%"}
    ]

    assert not validation.reconstruction_matches(answer, reconstruction)


def test_complete_multi_value_structure_represents_each_threshold() -> None:
    answer, reconstruction = _retained_multi_value_record()
    numeric_rule = answer["numeric_rule"]  # type: ignore[index]
    numeric_rule["structure_contract_version"] = (  # type: ignore[index]
        validation.MULTI_VALUE_NUMERIC_CONTRACT_VERSION
    )
    numeric_rule["values"] = [  # type: ignore[index]
        {"canonical_value": "90", "unit": "%", "operator": ">"},
        {"canonical_value": "80", "unit": "%", "operator": "="},
        {"canonical_value": "70", "unit": "%", "operator": "="},
    ]

    assert validation.reconstruction_matches(answer, reconstruction)


def test_retained_direct_value_requires_a_new_bound_contract() -> None:
    answer, provenance = _retained_direct_value_record()

    assert not validation.numeric_rule_is_source_bound(answer, provenance)


@pytest.mark.parametrize(
    "answer_text,reconstruction_text,numeric",
    [
        (
            "5.5 to 6.9 nM",
            "5.5 to 6.9 nM",
            {"canonical_value": "5.5 to 6.9", "unit": "nM"},
        ),
        (
            "sustained and statistically significant decline",
            "A sustained and statistically significant decline",
            {"canonical_value": "null", "unit": "null"},
        ),
    ],
)
def test_grounded_text_match_survives_incomplete_optional_numeric_metadata(
    answer_text: str,
    reconstruction_text: str,
    numeric: dict[str, str],
) -> None:
    answer, reconstruction = _audit_text_record(
        answer_text, reconstruction_text, numeric
    )

    assert validation.reconstruction_matches(answer, reconstruction)


@pytest.mark.parametrize(
    "answer_text,reconstruction_text,numeric",
    [
        (
            "5.5 to 6.9 nM",
            "5.5 to 6.8 nM",
            {"canonical_value": "5.5 to 6.9", "unit": "nM"},
        ),
        (
            "5.5 to 6.9 nM",
            "5.5 to 6.9 pM",
            {"canonical_value": "5.5 to 6.9", "unit": "nM"},
        ),
        (
            "not a sustained decline",
            "a sustained decline",
            {"canonical_value": "null", "unit": "null"},
        ),
        (
            "5.5 to 6.9 nM in selected surface seawater",
            "5.5 to 6.9 nM in deep seawater",
            {"canonical_value": "5.5 to 6.9", "unit": "nM"},
        ),
        (
            "5.5 nM",
            "6.9 nM",
            {"canonical_value": "5.5", "unit": "nM"},
        ),
    ],
)
def test_incomplete_numeric_metadata_cannot_hide_changed_reconstruction(
    answer_text: str,
    reconstruction_text: str,
    numeric: dict[str, str],
) -> None:
    answer, reconstruction = _audit_text_record(
        answer_text, reconstruction_text, numeric
    )

    assert not validation.reconstruction_matches(answer, reconstruction)


def test_exact_text_does_not_hide_a_conflicting_supported_unit() -> None:
    answer, reconstruction = _audit_text_record(
        "5.5 m",
        "5.5 m",
        {"canonical_value": "5.5", "unit": "cm"},
    )

    assert not validation.reconstruction_matches(answer, reconstruction)


def test_bound_direct_value_contract_accepts_the_retained_source_value() -> None:
    answer, provenance = _retained_direct_value_record()
    numeric_rule = answer["numeric_rule"]  # type: ignore[index]
    provenance["direct_value_contract_version"] = (
        validation.DIRECT_SOURCE_VALUE_CONTRACT_VERSION
    )
    provenance["direct_value_request_id"] = "6lemavaDKp7bz7IP_--l8AY"

    assert validation.numeric_rule_is_source_bound(answer, provenance)
    assert "direct_value_contract_version" not in numeric_rule
    assert "direct_value_request_id" not in numeric_rule


@pytest.mark.parametrize(
    "target,field,value",
    [
        (
            "provenance",
            "direct_value_contract_version",
            "numeric-rule-source-support-v2",
        ),
        ("provenance", "direct_value_request_id", "other-request"),
        ("numeric_rule", "tolerance", "0.1"),
        ("numeric_rule", "reported_precision", "0.01"),
        ("numeric_rule", "rounding_rule", "rounded to one decimal place"),
        ("numeric_rule", "conversion_rule", "directly converted from a fraction"),
    ],
)
def test_bound_direct_value_contract_rejects_changed_guards(
    target: str, field: str, value: str
) -> None:
    answer, provenance = _retained_direct_value_record()
    numeric_rule = answer["numeric_rule"]  # type: ignore[index]
    provenance["direct_value_contract_version"] = (
        validation.DIRECT_SOURCE_VALUE_CONTRACT_VERSION
    )
    provenance["direct_value_request_id"] = "6lemavaDKp7bz7IP_--l8AY"
    record = provenance if target == "provenance" else numeric_rule
    record[field] = value  # type: ignore[index]

    assert not validation.numeric_rule_is_source_bound(answer, provenance)
