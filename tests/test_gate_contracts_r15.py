"""Regression tests for the r15 audit defects fixed in the chapter 2 gates.

Every test names the audit finding it closes. The rule under test is always a
gate correction: a rejection that rested on wording, on a spelling, or on a
vocabulary the prompt never stated becomes a rejection that rests on the
frozen source evidence, or becomes a new rejection.
"""

from __future__ import annotations

import json

import pytest

from arctic_qa import generation, validation
from arctic_qa.util import sha256_bytes


# --- D1 and D7: one numeric metadata vocabulary ---------------------------


def _uncertainty_answer(**overrides: object) -> dict[str, object]:
    """The family-b38802bd8bf9137ec9d7 Vallakrabreen methane payload."""
    rule = {
        "canonical_value": "1.0",
        "tolerance": "0.3",
        "tolerance_basis": "0.3 t",
        "unit": "t",
        "reported_precision": "0.1",
        "rounding_rule": "1 decimal place",
        "conversion_rule": "direct source literal",
    }
    rule.update(overrides)
    return {
        "text": "1.0 t of methane",
        "evidence_quote": (
            "The total estimate of melt season emissions from the Vallakrabreen "
            "catchment equated to 1.0 t of methane (0.3 t)."
        ),
        "numeric_rule": rule,
    }


def test_numeric_contract_accepts_the_vocabulary_the_prompt_states() -> None:
    """D1: an uncertainty-bearing scalar had no wording it could choose."""
    assert validation.numeric_rule_is_source_bound(_uncertainty_answer()) is True
    assert (
        validation.numeric_rule_is_source_bound(
            _uncertainty_answer(rounding_rule="none")
        )
        is True
    )


def test_numeric_contract_refuses_a_vocabulary_the_prompt_does_not_state() -> None:
    assert (
        validation.numeric_rule_is_source_bound(
            _uncertainty_answer(
                rounding_rule="direct reporting without additional rounding"
            )
        )
        is False
    )
    assert (
        validation.numeric_rule_is_source_bound(
            _uncertainty_answer(
                conversion_rule="direct source reporting with no conversion"
            )
        )
        is False
    )


def test_numeric_contract_requires_the_unit_inside_tolerance_basis() -> None:
    """A bare tolerance_basis could bind a number the span never united."""
    assert (
        validation.numeric_rule_is_source_bound(
            _uncertainty_answer(tolerance_basis="0.3")
        )
        is False
    )


def test_numeric_contract_still_requires_every_literal_in_the_evidence() -> None:
    """The adversarial case: the tolerance literal is absent from the span."""
    answer = _uncertainty_answer()
    answer["evidence_quote"] = (
        "The total estimate of melt season emissions equated to 1.0 t of methane."
    )
    assert validation.numeric_rule_is_source_bound(answer) is False

    changed_value = _uncertainty_answer(canonical_value="2.0")
    assert validation.numeric_rule_is_source_bound(changed_value) is False


def test_numeric_contract_takes_the_unit_from_the_rule_not_a_whitelist() -> None:
    """D1 second defect: SAFE_UNIT_SPELLINGS held only percent and metres."""
    rule = {
        "canonical_value": "0.7",
        "tolerance": "0",
        "tolerance_basis": "0.7 degC",
        "unit": "degC",
        "reported_precision": "0.1",
        "rounding_rule": "1 decimal place",
        "conversion_rule": "direct source literal",
    }
    assert (
        validation._text_matches_typed_numeric(
            "0.7 degC", rule, validation.Decimal("0.7"), "degC"
        )
        is True
    )
    assert (
        validation._text_matches_typed_numeric(
            "0.7 degC", rule, validation.Decimal("0.7"), "m"
        )
        is False
    )


def test_numeric_vocabulary_is_stated_in_the_prompt_and_in_the_schema() -> None:
    """Design principle 2: every contract vocabulary appears in the prompt."""
    properties = generation.NUMERIC_RULE_SCHEMA["properties"]
    assert validation.DIRECT_CONVERSION_RULE == "direct source literal"
    assert "'direct source literal'" in properties["conversion_rule"]["description"]
    assert "'<N> decimal places'" in properties["rounding_rule"]["description"]
    assert "same unit as the unit field" in properties["tolerance_basis"]["description"]


# --- D2: eligibility contiguity ------------------------------------------


def _contiguity_candidate(selected: list[str]) -> tuple[dict, dict]:
    """The family-2d1bdbbb706bf10c36fc 239Pu ice-core span layout."""
    pieces = [
        ("s1", "concentration of 0.5 fg g-1 in the Arctic core. "),
        ("s2", "\n"),
        ("s3", "concentration from 1962 to 1965 CE was greater."),
    ]
    spans = []
    offset = 0
    for span_id, quote in pieces:
        length = len(quote.encode())
        spans.append(
            {
                "span_id": span_id,
                "quote": quote,
                "start_byte": offset,
                "end_byte": offset + length,
                "locator": {"section_id": "results"},
                "source_bytes_sha256": sha256_bytes(quote.encode()),
            }
        )
        offset += length
    by_id = {row["span_id"]: row for row in spans}
    resolved_scope = {
        "component": "separable_arctic_component",
        "finding_spans": spans,
        "question_scope_phrases": ["Arctic core"],
    }
    provenance_scope = {
        "component": "separable_arctic_component",
        "question_scope_phrases": ["Arctic core"],
        "eligibility_job_key": "job-1",
        "finding_spans": spans,
    }
    candidate = {
        "schema_version": "2.6.0",
        "question": "What concentration was measured in the Arctic core?",
        "answer": {
            "evidence_quote": "".join(by_id[span_id]["quote"] for span_id in selected),
            "eligibility_span_ids": selected,
            "evidence_components": [
                {
                    "eligibility_span_id": span_id,
                    "eligibility_quote_sha256": by_id[span_id]["source_bytes_sha256"],
                    "eligibility_locator": by_id[span_id]["locator"],
                }
                for span_id in selected
            ],
        },
        "provenance": {
            "eligible_arctic_scope": provenance_scope,
            "eligible_arctic_scope_sha256": sha256_bytes(
                json.dumps(
                    provenance_scope, sort_keys=True, separators=(",", ":")
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
    return candidate, source


def test_eligibility_contiguity_allows_a_whitespace_only_gap() -> None:
    """D2: the writer had no legal move when the classifier split on newlines."""
    candidate, source = _contiguity_candidate(["s1", "s3"])
    assert validation._eligible_arctic_scope_error(candidate, source) is None


def test_eligibility_contiguity_still_rejects_a_content_gap() -> None:
    candidate, source = _contiguity_candidate(["s1", "s3"])
    evidence = json.loads(source["scope_evidence_json"])
    spans = evidence["resolved_eligible_arctic_scope"]["finding_spans"]
    spans[1]["quote"] = "a sentence from another study"
    source["scope_evidence_json"] = json.dumps(evidence)
    candidate["provenance"]["eligible_arctic_scope"]["finding_spans"] = spans
    candidate["provenance"]["eligible_arctic_scope_sha256"] = sha256_bytes(
        json.dumps(
            candidate["provenance"]["eligible_arctic_scope"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    assert validation._eligible_arctic_scope_error(candidate, source) == (
        "finding_evidence_components_not_contiguous"
    )


def test_the_contiguity_code_is_honest_and_routes_to_an_alternative_finding() -> None:
    from arctic_qa import streaming

    assert (
        "finding_evidence_components_not_contiguous"
        in streaming.ALTERNATIVE_FINDING_REASONS
    )
    assert (
        "finding_evidence_components_not_contiguous"
        in streaming.IMMEDIATE_ALTERNATIVE_FINDING_REASONS
    )
    assert (
        streaming._failure_layer("finding_evidence_components_not_contiguous")
        == "finding"
    )


# --- D4 and SG-3: deterministic context rules ----------------------------


def test_a_context_the_deterministic_rule_demanded_is_never_unnecessary() -> None:
    """D4: the missing / unnecessary oscillation destroyed three families."""
    answer = {"text": "12 percent", "variants": []}
    verification = {
        "question_verification_contract_version": (
            validation.QUESTION_VERIFICATION_CONTRACT_VERSION
        ),
        "question_context_referent_resolved": True,
        "question_context_missing_detail": "",
        "question_answer_leakage_absent": True,
        "question_context_required": False,
        "question_context_source_supported": True,
        "question_context_answer_leakage_absent": True,
        "scope_value_contradicted_by_source": False,
        "contradicted_scope_field": "",
        "scope_representation_note": "",
    }
    question = "In the sampled group, what share of HPMTF was removed?"
    context = (
        "HPMTF stands for hydroperoxymethyl thioformate, an atmospheric "
        "sulfur-containing compound."
    )

    assert validation.benchmark_text_requires_context(question) is True
    assert (
        validation.question_context_verification_reason(
            context, answer, verification, question=question
        )
        is None
    )


def test_an_undemanded_context_is_still_judged_unnecessary() -> None:
    answer = {"text": "12 percent", "variants": []}
    verification = {
        "question_verification_contract_version": (
            validation.QUESTION_VERIFICATION_CONTRACT_VERSION
        ),
        "question_context_referent_resolved": True,
        "question_context_missing_detail": "",
        "question_answer_leakage_absent": True,
        "question_context_required": False,
        "question_context_source_supported": True,
        "question_context_answer_leakage_absent": True,
        "scope_value_contradicted_by_source": False,
        "contradicted_scope_field": "",
        "scope_representation_note": "",
    }
    question = "What share of glacial iron in Arctic fjord sediment is bioavailable?"

    assert validation.benchmark_text_requires_context(question) is False
    assert (
        validation.question_context_verification_reason(
            "Arctic fjords are cold.", answer, verification, question=question
        )
        == "question_context_unnecessary"
    )


@pytest.mark.parametrize(
    "token",
    [
        "UTC",
        "GMT",
        "AD",
        "BC",
        "CE",
        "BCE",
        "GPS",
        "PCR",
        "UV",
        "SI",
        "RMSE",
        "SD",
        "NAO",
        "ENSO",
    ],
)
def test_the_narrowed_allowlist_holds_only_one_meaning_tokens(token: str) -> None:
    """SG-3: UTC and CE forced a pointless context sentence."""
    assert token in validation._NON_ACRONYM_TOKENS
    assert validation._unresolved_acronym_tokens(f"measured at 06 {token} today") == []


@pytest.mark.parametrize(
    "token", ["POC", "TPM", "OTU", "ITP", "CTL", "DBO4", "AO", "SAUP"]
)
def test_a_study_local_label_is_never_allowlisted(token: str) -> None:
    assert token not in validation._NON_ACRONYM_TOKENS
    assert validation._unresolved_acronym_tokens(f"measured at {token} today") == [
        token
    ]


def test_a_definitional_gloss_resolves_an_acronym_but_a_repeat_does_not() -> None:
    assert validation._unresolved_acronym_tokens("GHSZ means gas hydrate zone") == []
    assert validation._unresolved_acronym_tokens("IMAC-SPE is the stated method") == []
    assert validation._unresolved_acronym_tokens(
        "pore overpressure beneath the GHSZ was predicted"
    ) == ["GHSZ"]
    assert validation._unresolved_acronym_tokens(
        "expedition PS80, expedition PS92"
    ) == [
        "PS80",
        "PS92",
    ]


def test_benchmark_text_malformed_catches_a_two_column_splice() -> None:
    assert validation.benchmark_text_is_malformed("a clean question?") is False
    assert validation.benchmark_text_is_malformed("a broken\nquestion?") is True
    assert validation.benchmark_text_is_malformed("a gutter   question?") is True
    assert (
        validation.benchmark_text_is_malformed(
            'Which species are noted when stating that "Three bumble bee species '
            'inhabit the southern edge of the island"?'
        )
        is True
    )


# --- RECON-1, RECON-5, RECON-6: the answer verifier split ----------------


def _verifier(**overrides: object) -> dict[str, object]:
    record = {
        "question_verification_contract_version": (
            validation.QUESTION_VERIFICATION_CONTRACT_VERSION
        ),
        "relation_scope_match": True,
        "scope_value_contradicted_by_source": False,
        "contradicted_scope_field": "",
        "scope_representation_note": "",
    }
    record.update(overrides)
    return record


def test_a_contradicted_scope_value_is_a_new_hard_reject() -> None:
    assert validation.answer_verifier_scope_reasons(_verifier()) == []
    assert validation.answer_verifier_scope_reasons(
        _verifier(
            scope_value_contradicted_by_source=True, contradicted_scope_field="period"
        )
    ) == ["scope_value_not_source_supported"]


def test_a_contradiction_without_a_named_field_is_not_a_verdict() -> None:
    assert validation.answer_verifier_scope_reasons(
        _verifier(scope_value_contradicted_by_source=True)
    ) == ["answer_verifier_scope_verdict_missing"]
    assert validation.answer_verifier_scope_reasons(
        _verifier(scope_representation_note=None)
    ) == ["answer_verifier_scope_verdict_missing"]


def test_a_wording_difference_is_recorded_and_never_gates() -> None:
    """RECON-1: 're-\\nfRun' broke a string match while entailment held."""
    assert (
        validation.answer_verifier_scope_reasons(
            _verifier(scope_representation_note="the span hyphenates refRun")
        )
        == []
    )


def test_the_verifier_prompt_no_longer_overloads_relation_scope_match() -> None:
    text = open(generation.__file__, encoding="utf-8").read()
    assert (
        "identify a referent or interpret scope, set relation_scope_match to false"
        not in text
    )
    assert (
        "Set relation_scope_match to false only when the selected span does not" in text
    )
    assert (
        "Do not use relation_scope_match or scope_value_contradicted_by_source" in text
    )
    assert "Record every wording, field-role, or span-containment difference in" in text


def test_a_question_qualifier_must_be_bound_to_the_frozen_evidence() -> None:
    """A reader-visible qualifier cannot rest on nothing."""
    answer = {
        "evidence_quote": "Mean flux was 12 percent in the peatland.",
        "scope": {"geography": "the peatland"},
    }
    question = "In the peatland during 2019, what was the mean flux?"
    unbound = {"scope": {"period": "during 2019"}, "evidence_quote": ""}
    neighbouring = {
        "scope": {"period": "during 2019"},
        "evidence_quote": "Sampling ran during 2019 at the same site.",
    }

    assert (
        validation.question_qualifier_binding_reason(question, answer, unbound, None)
        == "question_qualifier_not_evidence_bound"
    )
    # A qualifier in a neighbouring hashed span of the same paper is supported.
    assert (
        validation.question_qualifier_binding_reason(
            question, answer, neighbouring, None
        )
        is None
    )


def test_the_competing_alternatives_check_is_wired_as_a_hard_reject() -> None:
    """RECON-2: the evidence-bearing ambiguity check was never called."""
    answer = {"text": "12 percent", "variants": [], "evidence_quote": "12 percent"}
    reconstruction = {
        "answer": "12 percent",
        "alternatives": ["27 percent"],
        "ambiguity_label": "one_answer",
    }
    assert (
        validation.reconstruction_has_competing_alternatives(answer, reconstruction)
        is True
    )
    source = open(generation.__file__, encoding="utf-8").read()
    assert "reconstruction_has_competing_alternatives(answer, reconstruction)" in source


def test_the_alternatives_detail_does_not_split_the_repair_layer() -> None:
    """RECON-2: a co-fired detail must not end the family.

    The reconstructor's ambiguity label and the competing-alternatives check
    report the same defect. Routing keeps one root, so the repair budget is
    spent on one layer.
    """
    from arctic_qa import streaming

    assert streaming._routing_reason_codes(
        ["answer_ambiguous", "reconstruction_alternative_answer_present"]
    ) == ["answer_ambiguous"]
    assert streaming._routing_reason_codes(
        ["reconstruction_alternative_answer_present"]
    ) == ["reconstruction_alternative_answer_present"]


def test_a_garbled_text_rejection_is_not_reported_as_a_provider_fault() -> None:
    """The viewer must not read `benchmark_text_malformed` as a bad response."""
    from arctic_qa import pipeline_trace

    for reason in ("benchmark_text_malformed", "standalone_malformed_text"):
        record = pipeline_trace._plain_reason(
            reason, "rejected", "automated_acceptance"
        )
        assert record["category"] == "qa_rejection"
