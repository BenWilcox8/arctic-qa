"""The chapter 3 iteration slice of the standalone calibration set.

ch2 yield audit section 4.2 (F6). The rows are the confirmed false fails of
chapter 2 and their control siblings. Contract v4 was derived from them, so
they are an iteration slice and never a held-out slice. The deterministic half
is exercised here for free. The live judge is exercised only by the
``calibrate-standalone`` harness (gates slice), which needs a paid,
captain-approved run before any judge prompt ships.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctic_qa import generation
from arctic_qa.validation import (
    STANDALONE_CALIBRATION_SET_VERSION,
    STANDALONE_VERIFICATION_CONTRACT_VERSION,
    standalone_gate_decision,
    standalone_verdict_is_unevidenced,
)

FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "standalone-calibration-ch3-judge-slice-v1.jsonl"
)
RECORDS = [
    json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
]
HEADER = RECORDS[0]
ROWS = RECORDS[1:]
MUST_PASS = [row for row in ROWS if row["label"] == "must_pass"]
MUST_FAIL = [row for row in ROWS if row["label"] == "must_fail"]


def _identifier(row: dict[str, object]) -> str:
    return str(row["item_id"])


def test_slice_header_declares_its_contract_and_its_limits() -> None:
    assert HEADER["record"] == "calibration_slice_header"
    assert HEADER["slice"] == "iteration"
    assert HEADER["parent_set_version"] == STANDALONE_CALIBRATION_SET_VERSION
    assert HEADER["contract_version"] == STANDALONE_VERIFICATION_CONTRACT_VERSION
    assert (
        "No judge prompt ships without a live calibration run"
        in (HEADER["blocking_note"])
    )
    assert "captain approval" in HEADER["blocking_note"]
    assert "forbids them in the held-out slice" in HEADER["why_iteration"]
    assert len(MUST_PASS) >= 6
    assert len(MUST_FAIL) >= 3
    assert all(row["record"] == "calibration_row" for row in ROWS)


@pytest.mark.parametrize("row", MUST_PASS, ids=_identifier)
def test_every_must_pass_row_was_killed_by_the_deleted_clause(row: dict) -> None:
    """Each row is a chapter 2 false fail whose verdict the v4 rule rejects."""
    verdict = {"pass": False, **row["chapter2_verdict"]}
    reasons = verdict["reasons"]
    laundered = "multiple_interpretations" in reasons or any(
        reason.startswith("undefined_") for reason in reasons
    )
    assert laundered
    # A multiple_interpretations verdict with no second reading is unevidenced
    # under v4, so it is re-asked instead of routed to a rewrite.
    if "multiple_interpretations" in reasons:
        assert standalone_verdict_is_unevidenced(verdict)


@pytest.mark.parametrize("row", MUST_PASS, ids=_identifier)
def test_a_correct_v4_judge_releases_every_must_pass_row(row: dict) -> None:
    """The judge half releases the row. Only the free screen may still hold it.

    The free acronym screen still flags CMP22, CO 2, ITP and CHINARE on these
    rows. That matcher is audit 4.3 and belongs to the gates slice, so the row
    records ``reject_pending_gates_fix`` and this test pins the hand-off: when
    the matcher lands, the row flips to ``pass`` and the whole decision empties.
    """
    assert row["model_reasons"] == []
    decision = standalone_gate_decision(
        row["question"],
        row["question_context"],
        row["answer"],
        model_reasons=row["model_reasons"],
        model_answer_leakage_absent=True,
    )
    assert all(code.startswith("standalone_det_") for code in decision)
    if row["deterministic_expectation"] == "pass":
        assert decision == []
    else:
        assert row["deterministic_expectation"] == "reject_pending_gates_fix"
        assert row["deterministic_note"]
        assert decision == ["standalone_det_question_context_referent_unresolved"]


@pytest.mark.parametrize("row", MUST_FAIL, ids=_identifier)
def test_every_must_fail_row_still_fails_the_composed_gate(row: dict) -> None:
    decision = standalone_gate_decision(
        row["question"],
        row["question_context"],
        row["answer"],
        model_reasons=row["model_reasons"],
        model_answer_leakage_absent=True,
    )
    assert decision
    # The judge code alone must fail the row, whatever the free screen says.
    assert any(not code.startswith("standalone_det_") for code in decision)
    assert row["evidence_expectation"]["unresolved_phrases_non_empty"] is True
    if row["deterministic_expectation"] == "reject":
        assert standalone_gate_decision(
            row["question"], row["question_context"], row["answer"]
        )


def test_the_must_fail_controls_name_no_value_based_reason() -> None:
    """The fail side moves only on referents. No control may rest on the value."""
    for row in MUST_FAIL:
        assert all(
            reason.startswith("undefined_") or reason == "multiple_interpretations"
            for reason in row["model_reasons"]
        )
        assert "arbitrary" not in row["auditor_note"].split("Control")[0]


def test_the_v4_prompt_names_the_rule_each_control_exercises() -> None:
    system = generation.STANDALONE_SYSTEM
    assert "including when it names no site and no time" in system
    assert "A definite description with no antecedent in the task" in system
    assert "An acronym, run label, station code, or expedition code" in system
    assert "arbitrary study-specific quantity" not in system
