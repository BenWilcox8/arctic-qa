from __future__ import annotations

import pytest

from arctic_qa import validation


def _exact_percentage_answer(**rule_overrides: str) -> dict[str, object]:
    return {
        "text": "84.8%",
        "evidence_quote": "Ice 4 had a value of 84.8% at 20 degrees Celsius.",
        "numeric_rule": {
            "canonical_value": "84.8",
            "unit": "%",
            "tolerance": "0",
            "tolerance_basis": "exact percentage reported",
            "reported_precision": "84.8",
            "rounding_rule": "exact match",
            "conversion_rule": "directly reported percentage value",
            **rule_overrides,
        },
    }


def test_source_bound_directional_short_answer_is_equivalent() -> None:
    answer = {
        "text": "correlated positively",
        "evidence_quote": "Nitrogen fixation rates correlated positively with production.",
        "deterministic_rule": {
            "kind": "directional_relation",
            "source_value": "positively",
        },
    }

    assert validation.reconstruction_matches(answer, {"answer": "positively"})


@pytest.mark.parametrize("rebuilt", ["negatively", "not positively"])
def test_directional_short_answer_rejects_changed_sign_or_negation(
    rebuilt: str,
) -> None:
    answer = {
        "text": "correlated positively",
        "evidence_quote": "Nitrogen fixation rates correlated positively with production.",
        "deterministic_rule": {
            "kind": "directional_relation",
            "source_value": "positively",
        },
    }

    assert not validation.reconstruction_matches(answer, {"answer": rebuilt})


def test_percentage_spelling_is_not_a_competing_alternative() -> None:
    answer = _exact_percentage_answer()
    reconstruction = {
        "answer": "24 %",
        "numeric": {"canonical_value": "24", "unit": "%"},
        "alternatives": ["24 percent"],
    }
    answer["text"] = "24 %"
    answer["evidence_quote"] = "The organic mass component averaged 24% at Villum."
    answer["numeric_rule"]["canonical_value"] = "24"  # type: ignore[index]
    answer["numeric_rule"]["reported_precision"] = "24"  # type: ignore[index]

    assert validation.reconstruction_matches(answer, reconstruction)
    assert not validation.reconstruction_has_competing_alternatives(
        answer, reconstruction
    )


@pytest.mark.parametrize(
    "rebuilt",
    [
        "24.0 percent",
        "24 percent at Alert",
        "24 percent for PM2 at Villum",
        "24 percent without Villum",
    ],
)
def test_percentage_equivalence_preserves_precision_cohort_and_conditions(
    rebuilt: str,
) -> None:
    answer = _exact_percentage_answer()
    answer["text"] = "24 % for PM1 at Villum"
    answer["evidence_quote"] = (
        "The organic mass component in PM1 averaged 24% at Villum."
    )
    answer["numeric_rule"]["canonical_value"] = "24"  # type: ignore[index]
    answer["numeric_rule"]["reported_precision"] = "24"  # type: ignore[index]

    assert not validation.reconstruction_matches(answer, {"answer": rebuilt})


def test_source_bound_inequality_spelling_is_equivalent() -> None:
    answer = {
        "text": "above 90% in October",
        "evidence_quote": "Values were above 90% in October.",
    }

    assert validation.reconstruction_matches(
        answer, {"answer": "> 90 percent in October"}
    )


@pytest.mark.parametrize(
    "rebuilt",
    ["below 90% in October", "> 91% in October", "> 90% in November"],
)
def test_inequality_equivalence_preserves_direction_value_and_period(
    rebuilt: str,
) -> None:
    answer = {
        "text": "above 90% in October",
        "evidence_quote": "Values were above 90% in October.",
    }

    assert not validation.reconstruction_matches(answer, {"answer": rebuilt})


def test_approximate_range_spelling_is_equivalent() -> None:
    answer = {
        "text": "approximately 0.5 to 0.8 m beneath the ice bottom",
        "evidence_quote": "The Vector transducer was positioned approximately 0.5 to 0.8 m beneath the ice bottom.",
    }

    assert validation.reconstruction_matches(
        answer, {"answer": "about 0.5-0.8 metres beneath the ice bottom"}
    )


@pytest.mark.parametrize(
    "rebuilt",
    [
        "0.5 to 0.8 m beneath the ice bottom",
        "approximately 0.5 to 0.9 m beneath the ice bottom",
        "approximately 0.5 to 0.8 cm beneath the ice bottom",
        "approximately 0.5 to 0.8 m beneath the snow bottom",
    ],
)
def test_range_equivalence_preserves_qualifier_endpoints_unit_and_condition(
    rebuilt: str,
) -> None:
    answer = {
        "text": "approximately 0.5 to 0.8 m beneath the ice bottom",
        "evidence_quote": "The Vector transducer was positioned approximately 0.5 to 0.8 m beneath the ice bottom.",
    }

    assert not validation.reconstruction_matches(answer, {"answer": rebuilt})


def test_direct_exact_percentage_does_not_require_metadata_in_source() -> None:
    assert validation.numeric_rule_is_source_bound(_exact_percentage_answer())


@pytest.mark.parametrize(
    "overrides",
    [
        {"tolerance": "0.1", "tolerance_basis": "0.1%"},
        {"reported_precision": "84.80"},
        {"rounding_rule": "two decimal places"},
        {"conversion_rule": "converted from a fraction"},
    ],
)
def test_direct_exact_percentage_rejects_changed_numeric_metadata(
    overrides: dict[str, str],
) -> None:
    assert not validation.numeric_rule_is_source_bound(
        _exact_percentage_answer(**overrides)
    )
