"""Chapter 3 integration: the six slices work as one release.

Two invariants the integration owns (plan ``data/arctic-chapter3/plan.md``):

1. Phase D sits behind phase C. The pre-judge free checks of the judge call
   plan (cost slice) are exactly the corrected gates (gates slice): the same
   code names, from the same functions, so a candidate the corrected rules
   free is never re-killed for free before the judges run.
2. Every reason code a slice introduced has one routing layer entry.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arctic_qa import generation, streaming, validation
from arctic_qa.chapter2_replay import load_family_bundles, replay_candidate

EVIDENCE_DIR = Path(
    os.environ.get(
        "ARCTIC_CH2_EVIDENCE_DIR",
        "/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch2-yield-audit-r1/evidence",
    )
)
# The codes the free pre-judge pass may emit (cost slice report, section 2.1).
FREE_CHECK_CODES = frozenset(
    {
        "answer_evidence_not_located",
        "answer_scope_not_source_bound",
        "interpretation_span_contains_answer",
        "benchmark_text_raw_source_artifact",
        "scope_qualifier_not_displayed",
        "question_context_invalid",
        "scope_qualifier_missing",
        "finding_answer_phrase_in_required_question_phrases",
        "scope_qualifier_not_source_bound",
    }
)
# Every reason code the six chapter 3 slices introduced at candidate level.
NEW_CANDIDATE_CODES = {
    # gates
    "reconstruction_scope_contradicts_answer",
    # writer-context
    "finding_scope_value_unsourced",
    # judge-options
    "standalone_verdict_unevidenced",
    "standalone_det_question_context_missing",
    "standalone_det_question_context_referent_unresolved",
    "standalone_det_benchmark_text_malformed",
    "standalone_det_source_dependent_locator",
    "standalone_det_publication_relative_period",
    "option_pool_empty_after_prefilter",
    "closed_set_closure_not_source_established",
    "option_set_not_mutually_exclusive",
    "option_set_answer_not_choosable",
    # routing
    "gate_contradiction_unroutable",
    "empty_diagnostic_unroutable",
    # cost
    "no_admissible_finding",
    "finding_required_phrase_artifact",
    "finding_context_over_budget",
    *generation.WRITER_SLOT_UNAVAILABLE_REASONS,
}
NEW_ELIGIBILITY_CODES = {
    "eligible_arctic_scope_dimension_unsupported",
    "eligible_arctic_scope_phrase_not_specific",
}
ROUTING_SETS = {
    "REPAIRABLE_QUESTION_REASONS": streaming.REPAIRABLE_QUESTION_REASONS,
    "ALTERNATIVE_FINDING_REASONS": streaming.ALTERNATIVE_FINDING_REASONS,
    "IMMEDIATE_ALTERNATIVE_FINDING_REASONS": (
        streaming.IMMEDIATE_ALTERNATIVE_FINDING_REASONS
    ),
    "OPTION_REPAIR_REASONS": streaming.OPTION_REPAIR_REASONS,
    "ANSWER_RULE_REPAIR_REASONS": streaming.ANSWER_RULE_REPAIR_REASONS,
    "TERMINAL_GENERATION_REASONS": streaming.TERMINAL_GENERATION_REASONS,
    "UNROUTABLE_OUTCOME_REASONS": streaming.UNROUTABLE_OUTCOME_REASONS,
    "SCOPE_FAMILY_REASONS": streaming.SCOPE_FAMILY_REASONS,
    "ELIGIBILITY_CONTRACT_REASONS": streaming.ELIGIBILITY_CONTRACT_REASONS,
}


def _free_reasons(bundle: dict, row: dict) -> list[str]:
    candidate = row["candidate"]
    chunk_id = candidate["source"]["chunk_id"]
    chunk = {
        "chunk_id": chunk_id,
        "text": bundle["source_chunk_texts"][chunk_id]["text"],
    }
    return generation._pre_judge_gate_reasons(
        chunk,
        str(candidate["question"]),
        candidate["answer"],
        str(candidate.get("question_context", "") or ""),
        interpretation_spans=validation.context_only_span_records(
            candidate.get("provenance")
        ),
    )


@pytest.mark.skipif(
    not (EVIDENCE_DIR / "families").is_dir(),
    reason="the chapter 2 evidence bundles are not available on this machine",
)
def test_the_pre_judge_free_checks_are_exactly_the_corrected_gates() -> None:
    """Phase D behind phase C, on all 139 recorded chapter 2 candidates."""
    seen = 0
    freed = 0
    for bundle in load_family_bundles(EVIDENCE_DIR):
        for row in bundle.get("candidates", []):
            seen += 1
            free = _free_reasons(bundle, row)
            replayed = replay_candidate(bundle, row)
            judged = set(replayed["replayed_reasons"])
            # The free pass emits only free codes, and every free code the
            # judged pass emits is emitted for free too: same names, same rules.
            assert set(free) <= FREE_CHECK_CODES, (row["item_id"], free)
            assert set(free) == judged & FREE_CHECK_CODES, (
                row["item_id"],
                free,
                judged,
            )
            if replayed["freed"]:
                freed += 1
                assert free == [], (row["item_id"], free)
    assert seen == 139
    assert freed == 16


def test_the_free_checks_read_the_corrected_display_rule() -> None:
    """A dash variant no longer kills a candidate for free (audit 4.3 a)."""
    answer = {
        "text": "12 percent",
        "evidence_quote": "Ice loss reached 12 percent in the 2010–2012 period.",
        "scope": {"period": "2010–2012"},
        "required_question_phrases": ["Ice loss"],
    }
    chunk = {"chunk_id": "c1", "text": answer["evidence_quote"]}
    question = "What ice loss did the study report for the 2010-2012 period?"
    assert "scope_qualifier_not_displayed" not in generation._pre_judge_gate_reasons(
        chunk, question, answer, ""
    )
    assert validation.scope_phrase_is_displayed("2010–2012", question, "")


def test_every_new_candidate_code_has_one_routing_layer_entry() -> None:
    for code in sorted(NEW_CANDIDATE_CODES):
        layer = streaming._failure_layer(code)
        assert layer in {"context", "evidence", "finding", "options", "contract"}, code
        holders = [name for name, members in ROUTING_SETS.items() if code in members]
        assert holders, f"{code} sits in no routing set"


def test_the_eligibility_codes_claim_no_candidate_rung() -> None:
    for code in sorted(NEW_ELIGIBILITY_CODES):
        assert code in streaming.ELIGIBILITY_CONTRACT_REASONS
        assert not any(
            code in members
            for name, members in ROUTING_SETS.items()
            if name != "ELIGIBILITY_CONTRACT_REASONS"
        ), code


def test_the_reconciled_contract_versions() -> None:
    """One version string per contract after the six slices merged."""
    assert generation.PROMPT_VERSION == "arctic-qa-generation-v23"
    assert generation.CANDIDATE_SCHEMA_VERSION == "2.8.0"
    row = validation.CANDIDATE_CONTRACTS["2.8.0"]
    assert row["prompt_version"] == "arctic-qa-generation-v23"
    assert row["standalone_verification_contract_version"] == (
        "source-blind-scientific-referent-v4"
    )
    assert row["generation_attempt_contract_version"] == "bounded-failure-routing-v5"
    assert row["numeric_rule_contract_version"] == "numeric-rule-source-support-v4"
    assert row["option_verification_contract_version"] == (
        "option-admitting-interpretation-v1"
    )
    old = validation.CANDIDATE_CONTRACTS["2.7.0"]
    assert old["prompt_version"] == "arctic-qa-generation-v22"
    assert old["standalone_verification_contract_version"] == (
        "source-blind-scientific-referent-v3"
    )
    assert old["generation_attempt_contract_version"] == "bounded-failure-routing-v4"
    assert old["numeric_rule_contract_version"] == "numeric-rule-source-support-v3"
    assert "option_verification_contract_version" not in old


def test_a_stored_chapter_two_verdict_keeps_its_recorded_codes() -> None:
    """The v4 evidence rule never re-labels a v3 verdict (integration fix)."""
    stored = {
        "contract_version": "source-blind-scientific-referent-v3",
        "pass": False,
        "reasons": ["undefined_location"],
        "unresolved_phrases": [],
        "missing_detail_types": ["location"],
        "answer_leakage_absent": True,
    }
    assert validation._standalone_reason_codes(stored) == [
        "standalone_undefined_location"
    ]
    live = {**stored, "contract_version": "source-blind-scientific-referent-v4"}
    assert validation._standalone_reason_codes(live) == [
        "standalone_verdict_unevidenced"
    ]
    raw = {key: value for key, value in stored.items() if key != "contract_version"}
    assert generation._standalone_gate_reasons(raw) == [
        "standalone_verdict_unevidenced"
    ]
