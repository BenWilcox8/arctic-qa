"""Regressions for the chapter 2 yield audit gate corrections (chapter 3, gates slice).

Every test names the audit finding it closes: 4.3 (deterministic rules),
4.5 (reconstruction record), 4.8 (display rules). Each correction is a
wording test replaced by a meaning test, or a gloss the paper actually gives
recognized. Every relaxation ships with the adversarial cases that must still
fail: an unglossed study code, a scope value the displayed text does not
state, a tolerance literal absent from the evidence, a changed quantity.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arctic_qa import generation, streaming, validation
from arctic_qa.chapter2_replay import replay_chapter2_gates
from arctic_qa.model_roles import load_role_contract
from arctic_qa.validation import (
    CHAPTER2_NUMERIC_RULE_CONTRACT_VERSION,
    DETERMINISTIC_CONTEXT_RULES_VERSION,
    NUMERIC_RULE_CONTRACT_VERSION,
    RECONSTRUCTION_RECORD_CONTRACT_VERSION,
    STANDALONE_CALIBRATION_SET_VERSION,
    claim_type_reasons,
    numeric_rule_is_source_bound,
    reconstruction_matches,
    reconstruction_scope_reasons,
    rejection_diagnostic_detail,
    scope_phrase_is_displayed,
    scope_qualifier_not_displayed,
)


# --- contract versions ------------------------------------------------------


def test_the_slice_bumps_exactly_its_own_contract_versions() -> None:
    assert NUMERIC_RULE_CONTRACT_VERSION == "numeric-rule-source-support-v4"
    assert CHAPTER2_NUMERIC_RULE_CONTRACT_VERSION == "numeric-rule-source-support-v3"
    assert DETERMINISTIC_CONTEXT_RULES_VERSION == "deterministic-context-rules-v2"
    assert RECONSTRUCTION_RECORD_CONTRACT_VERSION == "reconstruction-record-v2"
    assert STANDALONE_CALIBRATION_SET_VERSION == "standalone-calibration-v2"
    assert validation.ANSWER_AGREEMENT_PROMPT_VERSION == "answer-agreement-judge-v2"
    # The rejection diagnostic records the rule versions a verdict rests on.
    detail = rejection_diagnostic_detail({})
    assert (
        detail["deterministic_context_rules_version"]
        == DETERMINISTIC_CONTEXT_RULES_VERSION
    )
    assert detail["reconstruction_record_contract_version"] == (
        RECONSTRUCTION_RECORD_CONTRACT_VERSION
    )
    assert detail["numeric_rule_contract_version"] == NUMERIC_RULE_CONTRACT_VERSION


# --- 4.3 c: the acronym gloss matcher --------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The Asian-Pacific Oscillation (APO) is an intrinsic atmospheric mode",
        "ice-nucleating particle (INP) concentrations",
        "bottom-simulating reflection (BSR) was mapped",
        "an inter-series interval (ISI) is defined as a call interval",
        "the Pan-Arctic Ice-Ocean Modeling and Assimilation System (PIOMAS) reanalysis",
        "the Community Earth System Model version 2 (CESM2) framework",
        "the Parallel Ocean Program version 2 (POP) ocean component",
        "Atmospheric Radiation Measurement North Slope of Alaska (ARM NSA) Facility",
        "the first principal component (PC1) accounted for the crustal species",
        "INP (ice-nucleating particles) were counted",
        "First-year ice is abbreviated as FYI. Ponds on FYI were counted.",
        "INP represents ice-nucleating particles and SML represents the surface mixed layer",
        "bla TEM genes with primers designed for the TEM-1 allele",
        "the CTL is a control run with no forcing",
    ],
)
def test_a_gloss_the_text_gives_resolves_the_acronym(text: str) -> None:
    assert validation._unresolved_acronym_tokens(text) == []


@pytest.mark.parametrize(
    ("text", "unresolved"),
    [
        ("during the CHINARE 2010 cruise", ["CHINARE"]),
        ("the 2012 Chinese Arctic Research Expedition (CHINARE 2012)", ["CHINARE"]),
        ("scaling applied to the AKMA3 dataset", ["AKMA3"]),
        ("within CESM2, CNTL_SOM denotes the control run", ["CESM2"]),
        (
            "site DBO3 in the Chukchi Sea. DBO refers to Distributed Biological Observatory sites",
            ["DBO3"],
        ),
        ("at the ARM NSA Facility", ["ARM", "NSA"]),
        ("type strain KMM 9724T", ["KMM"]),
        ("the CMP22 pyranometer", ["CMP22"]),
        ("pore overpressure beneath the GHSZ was predicted", ["GHSZ"]),
        ("expedition PS80, expedition PS92", ["PS80", "PS92"]),
        ("GHSZ is", ["GHSZ"]),
        ("measured at ITP today", ["ITP"]),
    ],
)
def test_an_unglossed_study_code_still_fails(text: str, unresolved: list[str]) -> None:
    """The 35 correct chapter 2 catches survive: no formula shape rule."""
    assert validation._unresolved_acronym_tokens(text) == unresolved


def test_the_allowlist_gains_only_units_statistics_phases_and_formulas() -> None:
    for token in (
        "CFU",
        "RMS",
        "RMSD",
        "SE",
        "SEM",
        "CMIP5",
        "CMIP6",
        "PM10",
        "NH4",
        "SO42",
        "SW",
        "NE",
    ):
        assert token in validation._NON_ACRONYM_TOKENS
    for token in (
        "DBO3",
        "DBO4",
        "AKMA3",
        "CESM2",
        "CMP22",
        "T1",
        "ITP",
        "CTL",
        "KMM",
        "API",
        "ZYM",
        "LNG",
        "CO",
    ):
        assert token not in validation._NON_ACRONYM_TOKENS


# --- 4.3 a and 4.8: display rules ------------------------------------------


@pytest.mark.parametrize(
    ("value", "question", "context"),
    [
        ("2008–2018", "did the date shift from 2008 to 2018?", ""),
        ("PM 10 samples", "For PM10 samples collected near Ny-Ålesund", ""),
        (
            "four chronosequences",
            "across the four glacier foreland chronosequences",
            "",
        ),
        (
            "amp r isolates from the rectal samples",
            "When 144 amp r isolates from polar bear rectal swab samples were screened",
            "",
        ),
        (
            "Profundicola chukchiensis sp.nov.",
            "",
            "Profundicola chukchiensis sp. nov. is a bacterium",
        ),
        ("all the 903 realizations", "Across all 903 winter realizations,", ""),
        ("kg m −3", "a density contrast of ~ 1,024 kg m−3 between water and gas", ""),
        ("HELiPOD data set", "in the HELiPOD probe data set", ""),
        (
            "summer APO",
            "the summer Asian-Pacific Oscillation (APO) towards its positive phase",
            "",
        ),
    ],
)
def test_the_display_rule_accepts_a_faithful_display(
    value: str, question: str, context: str
) -> None:
    assert scope_phrase_is_displayed(value, question, context)


@pytest.mark.parametrize(
    ("value", "question"),
    [
        ("adult females", "adult males and females"),
        ("adult females", "adult, females"),
        ("adult females", "adult males. Females were counted"),
        ("adult females", "adult (males) females"),
        ("northern Sweden", "in Abisko, Sweden"),
        ("early August 2015", "in August 2015"),
        ("humus soils", "in meadow soils"),
        ("February 2009", "Which markers were lower in women from NAO?"),
        ("ringed seals", "in samples from Resolute Bay"),
    ],
)
def test_the_display_rule_rejects_an_absent_or_coordinated_value(
    value: str, question: str
) -> None:
    assert not scope_phrase_is_displayed(value, question, "")
    assert scope_qualifier_not_displayed({"scope": {"population": value}}, question, "")


def test_source_binding_keeps_the_strict_projection() -> None:
    """Keep column: selected-evidence-literal-scope-v4 never gains gapped matching."""
    assert not validation._scope_phrase_in_text("15–20°N", "15-20°N")
    assert not validation._scope_phrase_in_text(
        "four chronosequences", "four glacier chronosequences"
    )
    assert not validation.scope_is_evidence_bound(
        {"period": "2008–2018"}, {"evidence_quote": "from 2008 to 2018"}
    )


# --- 4.3 d: numeric uncertainty notation ------------------------------------


def _rule(**overrides: object) -> dict[str, object]:
    rule = {
        "canonical_value": "13.0",
        "tolerance": "2.6",
        "unit": "°C",
        "tolerance_basis": "± 2.6 °C",
        "reported_precision": "0.1",
        "rounding_rule": "1 decimal place",
        "conversion_rule": "direct source literal",
    }
    rule.update(overrides)
    return rule


def test_value_plus_minus_tolerance_unit_binds() -> None:
    answer = {
        "text": "13.0 ± 2.6 °C",
        "evidence_quote": "with a mean value of 13.0 ± 2.6 °C across samples",
        "numeric_rule": _rule(),
    }
    assert numeric_rule_is_source_bound(answer) is True


def test_value_unit_sd_equals_tolerance_inherits_the_unit() -> None:
    answer = {
        "text": "0.122 mm",
        "evidence_quote": "growth measured 0.122 mm (sd = 0.04) for S. arctica",
        "numeric_rule": _rule(
            canonical_value="0.122",
            tolerance="0.04",
            unit="mm",
            tolerance_basis="sd = 0.04",
            reported_precision="0.001",
            rounding_rule="3 decimal places",
        ),
    }
    assert numeric_rule_is_source_bound(answer) is True


def test_a_tolerance_with_its_own_unit_never_inherits() -> None:
    """The bound of the inheritance: '13.0 degC (sd = 5%)' still fails."""
    answer = {
        "text": "13.0 °C",
        "evidence_quote": "13.0 °C (sd = 5%) was measured",
        "numeric_rule": _rule(tolerance="5", tolerance_basis="sd = 5%"),
    }
    assert numeric_rule_is_source_bound(answer) is False


def test_a_tolerance_literal_absent_from_the_evidence_still_fails() -> None:
    answer = {
        "text": "13.0 ± 2.6 °C",
        "evidence_quote": "with a mean value of 13.0 °C across samples",
        "numeric_rule": _rule(),
    }
    assert numeric_rule_is_source_bound(answer) is False
    changed = {
        "text": "13.0 ± 2.6 °C",
        "evidence_quote": "with a mean value of 13.0 ± 2.7 °C across samples",
        "numeric_rule": _rule(),
    }
    assert numeric_rule_is_source_bound(changed) is False


def test_a_bare_tolerance_basis_without_an_adjacent_clause_still_fails() -> None:
    """r15 D1 control: the predecessor could bind a number the span never united."""
    answer = {
        "text": "1.0 t of methane",
        "evidence_quote": "emissions equated to 1.0 t of methane (0.3 t).",
        "numeric_rule": _rule(
            canonical_value="1.0", tolerance="0.3", unit="t", tolerance_basis="0.3"
        ),
    }
    assert numeric_rule_is_source_bound(answer) is False


# --- 4.3 e: claim type as a signal ------------------------------------------


@pytest.mark.parametrize(
    ("reconstruction", "verification"),
    [
        ("observation", "definition"),
        ("observation", "association"),
        ("association", "definition"),
        ("observation", "observation"),
    ],
)
def test_a_compatible_label_pair_is_a_note_not_a_reject(
    reconstruction: str, verification: str
) -> None:
    reasons = claim_type_reasons(
        {"claim_type": "observation"},
        {"question_claim_type": reconstruction},
        {"question_claim_type": verification},
    )
    assert reasons == []


@pytest.mark.parametrize("other", ["observation", "association", "definition"])
def test_a_causal_label_against_any_other_label_rejects(other: str) -> None:
    reasons = claim_type_reasons(
        {"claim_type": "definition"},
        {"question_claim_type": "causal"},
        {"question_claim_type": other},
    )
    assert reasons == ["question_claim_type_disagreement"]


def test_causal_overclaim_fires_on_either_judge_label() -> None:
    assert claim_type_reasons(
        {"claim_type": "association"},
        {"question_claim_type": "causal"},
        {"question_claim_type": "association"},
    ) == ["question_claim_type_disagreement", "causal_overclaim"]
    assert claim_type_reasons(
        {"claim_type": "observation"},
        {"question_claim_type": "causal"},
        {"question_claim_type": "causal"},
    ) == ["causal_overclaim"]


def test_the_claim_type_definitions_reach_every_prompt_and_schema() -> None:
    definitions = validation.CLAIM_TYPE_DEFINITIONS
    schemas = generation.ROLE_SCHEMAS
    assert (
        schemas["reconstructor"]["properties"]["question_claim_type"]["description"]
        == definitions
    )
    assert (
        schemas["answer_verifier"]["properties"]["question_claim_type"]["description"]
        == definitions
    )
    assert (
        generation.ANSWER_SCHEMA["properties"]["claim_type"]["description"]
        == definitions
    )
    for label in validation.CLAIM_TYPE_LABELS:
        assert f"{label} is" in definitions


# --- 4.3 f and 4.5: the reconstruction record -------------------------------


def test_a_more_specific_reconstructor_paraphrase_is_not_a_kill() -> None:
    answer = {
        "scope": {
            "population": "little auks",
            "geography": "Hornsund",
            "period": "2011",
        }
    }
    reconstruction = {
        "scope": {
            "population": "chick-rearing little auks",
            "geography": "Hornsund",
            "period": "2011",
        },
        "evidence_quote": "positions of little auks in 2011 at Hornsund",
    }
    assert reconstruction_scope_reasons(answer, reconstruction) == []


def test_an_all_null_reconstructor_scope_is_allowed() -> None:
    answer = {"scope": {"period": "September 2016"}}
    reconstruction = {
        "scope": {"geography": None, "period": None},
        "evidence_quote": "x",
    }
    assert reconstruction_scope_reasons(answer, reconstruction) == []


def test_disjoint_calendar_years_contradict_the_answer() -> None:
    answer = {"scope": {"period": "2011"}}
    reconstruction = {"scope": {"period": "2012"}, "evidence_quote": "sampled in 2012"}
    assert reconstruction_scope_reasons(answer, reconstruction) == [
        "reconstruction_scope_contradicts_answer"
    ]
    inside = {"scope": {"period": "2011"}, "evidence_quote": "sampled in 2011"}
    assert (
        reconstruction_scope_reasons({"scope": {"period": "2008–2018"}}, inside) == []
    )


def test_an_unpaired_reconstructor_value_keeps_the_verbatim_binding() -> None:
    answer = {"scope": {"population": "little auks"}}
    reconstruction = {
        "scope": {"population": "little auks", "method": "proposed approach"},
        "evidence_quote": "little auks were tracked",
    }
    assert reconstruction_scope_reasons(answer, reconstruction) == [
        "reconstruction_scope_not_source_bound"
    ]


def test_a_paired_value_that_is_neither_entailed_nor_verbatim_still_fails() -> None:
    answer = {"scope": {"geography": "northern Sweden"}}
    reconstruction = {
        "scope": {"geography": "arctic tundra"},
        "evidence_quote": "soils in Abisko",
    }
    assert reconstruction_scope_reasons(answer, reconstruction) == [
        "reconstruction_scope_not_source_bound"
    ]


def test_an_acronym_entails_its_expansion() -> None:
    answer = {"scope": {"population": "first-year ice"}}
    reconstruction = {"scope": {"population": "FYI"}, "evidence_quote": "on FYI"}
    assert reconstruction_scope_reasons(answer, reconstruction) == []


def _count_answer(text: str = "24 species") -> dict[str, object]:
    return {
        "text": text,
        "variants": [],
        "evidence_quote": "A total of 24 species were recorded.",
        "numeric_rule": {
            "canonical_value": "24",
            "unit": "species",
            "tolerance": "0",
            "tolerance_basis": "count",
            "reported_precision": "exact integer",
            "rounding_rule": "none",
            "conversion_rule": "direct count of species",
        },
    }


@pytest.mark.parametrize(
    ("answer_text", "rebuilt", "rule"),
    [
        ("24 species", "24", None),
        ("70 %", "70", {"canonical_value": "70", "unit": "%"}),
        ("15 W m −2", "15", {"canonical_value": "15", "unit": "W m −2"}),
        (
            "1808 fin whale vocalizations",
            "1808",
            {"canonical_value": "1808", "unit": "fin whale vocalizations"},
        ),
        ("0.030 km", "0.030 kilometres", {"canonical_value": "0.030", "unit": "km"}),
    ],
)
def test_number_plus_unit_against_bare_number_is_settled_deterministically(
    answer_text: str, rebuilt: str, rule: dict | None
) -> None:
    answer = _count_answer(answer_text)
    if rule:
        answer["numeric_rule"] = {**answer["numeric_rule"], **rule}
    reconstruction = {
        "answer": rebuilt,
        "alternatives": [],
        "ambiguity_label": "one_answer",
    }
    assert reconstruction_matches(answer, reconstruction)


@pytest.mark.parametrize(
    ("answer_text", "rebuilt", "rule"),
    [
        ("24 species", "25", None),
        ("24 species", "24.0", None),
        ("24 species", "24 taxa", None),
        ("24 species", "not 24", None),
        ("0.81 ± 0.26 ng/m3", "0.81", {"canonical_value": "0.81", "unit": "ng/m3"}),
        ("7.3 ± 2.9 cm", "7.3", {"canonical_value": "7.3", "unit": "cm"}),
        ("at least 452 kPa", "452", {"canonical_value": "452", "unit": "kPa"}),
    ],
)
def test_the_deterministic_tier_refuses_a_changed_quantity_or_dropped_uncertainty(
    answer_text: str, rebuilt: str, rule: dict | None
) -> None:
    answer = _count_answer(answer_text)
    if rule:
        answer["numeric_rule"] = {**answer["numeric_rule"], **rule}
    reconstruction = {
        "answer": rebuilt,
        "alternatives": [],
        "ambiguity_label": "one_answer",
    }
    assert not validation._answer_rule_quantity_matches_rebuilt(answer, rebuilt)
    assert not reconstruction_matches(answer, reconstruction)


def test_the_deterministic_tier_defers_to_a_conflicting_typed_quantity() -> None:
    answer = _count_answer()
    reconstruction = {
        "answer": "24",
        "numeric": {"canonical_value": "25", "unit": "species"},
    }
    assert not reconstruction_matches(answer, reconstruction)


# --- 4.5 R6: the directional content test -----------------------------------


def _directional_answer() -> dict[str, object]:
    return {
        "text": "higher krill production",
        "variants": [],
        "evidence_quote": "whale migration resulted in higher krill production",
        "deterministic_rule": {
            "kind": "directional_relation",
            "source_value": "higher krill production",
        },
    }


def test_a_multi_word_directional_rebuilt_answer_matches() -> None:
    answer = _directional_answer()
    assert validation._source_bound_directional_answer_matches(
        answer, "higher krill production"
    )
    assert validation._source_bound_directional_answer_matches(answer, "higher")
    assert not validation.reconstruction_has_competing_alternatives(
        answer, {"answer": "higher", "alternatives": ["higher krill production"]}
    )


def test_the_must_fail_control_higher_krill_mortality() -> None:
    answer = _directional_answer()
    assert not validation._source_bound_directional_answer_matches(
        answer, "higher krill mortality"
    )
    assert not validation._source_bound_directional_answer_matches(
        answer, "lower krill production"
    )
    assert not validation._source_bound_directional_answer_matches(
        answer, "not higher krill production"
    )
    assert validation.reconstruction_has_competing_alternatives(
        answer, {"answer": "higher", "alternatives": ["higher krill mortality"]}
    )


# --- 4.3 f: allow_empty at both call sites ----------------------------------


CHUNK_TEXT = "The 24 species were recorded at Hornsund in 2011 by the survey."


def _gate_records() -> tuple[dict, dict, dict, dict]:
    chunk = {"chunk_id": "chunk-1", "text": CHUNK_TEXT}
    locator = {"chunk_id": "chunk-1", "start_offset": 0, "end_offset": len(CHUNK_TEXT)}
    answer = {
        "text": "24 species",
        "variants": [],
        "claim_type": "observation",
        "evidence_quote": CHUNK_TEXT,
        "locator": locator,
        "required_question_phrases": ["Hornsund"],
        "scope": {"geography": "Hornsund", "period": "2011"},
    }
    reconstruction = {
        "answer": "24 species",
        "alternatives": [],
        "ambiguity_label": "one_answer",
        "question_claim_type": "observation",
        "evidence_quote": CHUNK_TEXT,
        "locator": locator,
        "scope": {"geography": None, "period": None},
    }
    verification = {
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
        "question_claim_type": "observation",
        "evidence_quote": CHUNK_TEXT,
        "locator": locator,
        "scope": {"geography": "Hornsund", "period": "2011"},
    }
    return chunk, answer, reconstruction, verification


def test_qa_gate_reasons_pins_allow_empty_per_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site 1 of two: the generation-time gate."""
    chunk, answer, reconstruction, verification = _gate_records()
    seen: list[tuple[str, bool]] = []
    original = validation.scope_is_evidence_bound

    def spy(scope, record, interpretation_texts=(), *, allow_empty=False):
        name = {
            id(answer): "answer",
            id(reconstruction): "reconstruction",
            id(verification): "verification",
        }[id(record)]
        seen.append((name, allow_empty))
        return original(scope, record, interpretation_texts, allow_empty=allow_empty)

    monkeypatch.setattr(validation, "scope_is_evidence_bound", spy)
    monkeypatch.setattr(generation, "scope_is_evidence_bound", spy)

    reasons = generation._qa_gate_reasons(
        chunk,
        "How many species were recorded at Hornsund in 2011?",
        answer,
        reconstruction,
        verification,
        standalone_verification={
            "pass": True,
            "answer_leakage_absent": True,
            "reasons": [],
        },
    )

    assert dict(seen) == {
        "answer": False,
        "reconstruction": True,
        "verification": False,
    }
    assert "reconstruction_scope_not_source_bound" not in reasons
    assert "answer_scope_not_source_bound" not in reasons


def test_validate_candidate_pins_allow_empty_per_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Site 2 of two: the stored-candidate validator, on a smoke candidate."""
    from arctic_qa.db import Database
    from arctic_qa.paths import DataPaths
    from test_cli_integration import candidate as smoke_candidate, smoke

    smoke(tmp_path)
    item = smoke_candidate(tmp_path)
    item["reconstruction"]["scope"] = {
        key: None for key in item["reconstruction"]["scope"]
    }
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    seen: dict[str, bool] = {}
    original = validation.scope_is_evidence_bound
    records = {
        id(item["answer"]): "answer",
        id(item["reconstruction"]): "reconstruction",
        id(item["answer_verification"]): "verification",
    }

    def spy(scope, record, interpretation_texts=(), *, allow_empty=False):
        seen[records.get(id(record), "other")] = allow_empty
        return original(scope, record, interpretation_texts, allow_empty=allow_empty)

    monkeypatch.setattr(validation, "scope_is_evidence_bound", spy)
    try:
        result = validation.validate_candidate(
            database, paths.namespace, item, persist=False
        )
    finally:
        database.close()

    assert seen == {"answer": False, "reconstruction": True, "verification": False}
    assert "reconstruction_scope_not_source_bound" not in result.reasons


# --- 4.5 R5: the fallback judge ---------------------------------------------


def test_the_agreement_prompt_carries_three_worked_examples_and_the_judge_is_pro() -> (
    None
):
    system = validation.ANSWER_AGREEMENT_SYSTEM
    assert "'24 species' and '24' are the same answer" in system
    assert "restricted to the previous taxonomical category" in system
    assert "'0.81 +/- 0.26 ng/m3' and '0.81' are not the same answer" in system
    assert "Return only yes or no." in system
    contract = load_role_contract()
    for profile in contract["profiles"].values():
        assert profile["answer_judge"]["model"] == "gemini-3.1-pro-preview"
        assert profile["answer_judge"]["model"] != profile["question_writer"]["model"]


def test_a_stored_chapter_2_agreement_receipt_still_names_a_supported_prompt() -> None:
    assert (
        validation.PREDECESSOR_ANSWER_AGREEMENT_PROMPT_VERSION,
        validation.PREDECESSOR_ANSWER_AGREEMENT_SYSTEM,
    ) in validation.SUPPORTED_ANSWER_AGREEMENT_PROMPTS
    assert (
        validation.ANSWER_AGREEMENT_PROMPT_VERSION,
        validation.ANSWER_AGREEMENT_SYSTEM,
    ) in validation.SUPPORTED_ANSWER_AGREEMENT_PROMPTS


# --- routing registration ---------------------------------------------------


def test_the_new_reason_code_is_registered_in_the_routing_sets() -> None:
    assert (
        "reconstruction_scope_contradicts_answer"
        in streaming._STANDALONE_DEPENDENT_REASONS
    )
    assert (
        streaming._reason_family("reconstruction_scope_contradicts_answer") == "scope"
    )


# --- the deterministic replay of the 139 chapter 2 candidates ---------------


# The chapter 2 yield-audit evidence bundle. It is an external input, so these
# tests skip when it is absent. The default is the path of the machine that
# produced the run; `docs/REPRODUCTION.md` says how to point it somewhere else.
EVIDENCE_DIR = Path(
    os.environ.get(
        "ARCTIC_CH2_EVIDENCE_DIR",
        "/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch2-yield-audit-r1/evidence",
    )
)
EXPECTED_FREED = {
    "aqa-bfdf79ceb8e0c4c0e0be",
    "aqa-c491bc6dbdc60f2f507f",
    "aqa-3f6bcd96f09d48019173",
    "aqa-8aaeab6c42e173b2edc6",
    "aqa-c45137c902bda3e1d76f",
    "aqa-45b06ab59fd1cb1e831f",
    "aqa-64c1db140f14e9f2f3fe",
    "aqa-303b02c65012fdcd4a61",
    "aqa-2c32a5d8907f8343afbe",
    "aqa-b2d72a864493c406263f",
    "aqa-09374f7825097bfab2c2",
    "aqa-615b6a2d2f6ab21e7993",
    "aqa-cfd632a0e688f6299c59",
    "aqa-d63cb2893719e9cb344d",
    "aqa-4b25820e40f0c5f1494e",
    "aqa-28c6d5253470c8681a8f",
}


@pytest.mark.skipif(
    not (EVIDENCE_DIR / "families").is_dir(),
    reason="the chapter 2 evidence bundles are not available on this machine",
)
def test_the_chapter_2_replay_frees_exactly_the_recorded_candidates() -> None:
    report = replay_chapter2_gates(EVIDENCE_DIR)
    assert report["candidates"] == 139
    assert report["families"] == 56
    assert {row["item_id"] for row in report["freed"]} == EXPECTED_FREED
    assert len(report["freed_families"]) == 10
    assert all(row["standalone_pass"] is True for row in report["freed"])
    assert report["regressed"] == []
    # Integration: the writer-context binding pool (yield audit 4.1 d, DG-5)
    # also tests a qualifier that question_context displays. It fires on four
    # candidates whose scope was already unbound (answer_scope_not_source_bound),
    # so no candidate changes outcome.
    assert report["added_reason_counts"] == {"question_qualifier_not_evidence_bound": 4}
    for row in report["rows"]:
        if "question_qualifier_not_evidence_bound" in row["added"]:
            assert "answer_scope_not_source_bound" in row["recorded_reasons"]
    accepted = [row for row in report["rows"] if row["accepted_before"]]
    assert accepted and all(row["replayed_reasons"] == [] for row in accepted)
