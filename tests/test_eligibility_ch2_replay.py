"""Replay the 79 non-eligible chapter 2 papers through the corrected validation.

The fixture `fixtures/ch2-eligibility-non-eligible-v1.jsonl` holds one row for
each of the 79 papers that chapter 2 did not admit. Each row carries the recorded
decision, the recorded validation errors and the parsed criterion records, taken
from the yield audit evidence bundle
`data/arctic-ch2-yield-audit-r1/evidence/eligibility/batch-*.json`.

The replay answers one question: how many of those papers the corrected contract
would record as eligible, unresolved or excluded. It reads the real validation
rules, so a change in those rules changes these counts and this test fails.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import arctic_qa.gemini_eligibility as eligibility


FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "ch2-eligibility-non-eligible-v1.jsonl"
)


def _rows() -> list[dict]:
    return [
        json.loads(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _replayed(row: dict) -> dict:
    """Apply the corrected rules to one recorded screening."""
    by_id = {item["criterion_id"]: item for item in row["criteria"]}
    statuses = {name: item["status"] for name, item in by_id.items()}
    # The non-fatal rule (audit 4.7, finding E1). The criterion evidence of every
    # recorded row is otherwise complete, so the code never stays fatal here.
    errors = [
        code
        for code in row["recorded_errors"]
        if code.split(":")[0] != "criterion_missing_context_absent"
    ]
    decision, _ = eligibility._status_mapping_v2(by_id)
    return {
        "errors": errors,
        "repairable": bool(errors) and eligibility.format_repairable(errors),
        "decision": None if errors else decision,
        "rescreen_pool": (
            not errors and eligibility.geography_rescreen_eligible(statuses)
        ),
    }


def test_the_fixture_reproduces_the_chapter_2_eligibility_funnel() -> None:
    rows = _rows()
    assert len(rows) == 79
    assert Counter(row["recorded_decision"] for row in rows) == {
        "screening_error": 38,
        "excluded": 33,
        "uncertain": 8,
    }
    recorded = Counter(
        code.split(":")[0] for row in rows for code in row["recorded_errors"]
    )
    assert recorded["criterion_missing_context_absent"] == 22
    assert recorded["eligible_arctic_scope_phrase_unbound"] == 9
    assert recorded["eligible_arctic_scope_missing"] == 4
    assert recorded["criterion_evidence_missing"] == 3


def test_the_non_fatal_rule_turns_22_lost_calls_into_recorded_decisions() -> None:
    """Audit 4.7 E1: 22 papers, USD 0.44, bought no decision at all."""
    recovered = [
        _replayed(row)
        for row in _rows()
        if any(
            code.startswith("criterion_missing_context_absent")
            for code in row["recorded_errors"]
        )
    ]
    assert len(recovered) == 22
    assert all(row["errors"] == [] for row in recovered)
    assert Counter(row["decision"] for row in recovered) == {
        "uncertain": 18,
        "excluded": 4,
    }
    # The rule records a decision. It never makes a paper eligible on its own.
    assert not any(row["decision"] == "eligible" for row in recovered)


def test_the_replayed_funnel_over_all_79_papers() -> None:
    """The count this slice reports: eligible, unresolved, excluded, error."""
    replayed = [_replayed(row) for row in _rows()]
    decisions = Counter(row["decision"] for row in replayed)
    assert decisions["eligible"] == 0
    assert decisions["uncertain"] == 26
    assert decisions["excluded"] == 37
    # Screening errors fall from 38 to 16 of the same 200 screened papers.
    assert decisions[None] == 16
    # 13 of those 16 are format-repairable, so the streaming re-ask reaches them.
    assert sum(row["repairable"] for row in replayed) == 13


def test_the_corrected_re_screen_selects_24_papers_instead_of_zero() -> None:
    """Audit 4.7 E2: `geography_rescreen_keys` selected zero by construction."""
    replayed = [_replayed(row) for row in _rows()]
    assert sum(row["rescreen_pool"] for row in replayed) == 24
    # Every selected paper is unresolved, never excluded and never eligible.
    assert all(
        row["decision"] == "uncertain" for row in replayed if row["rescreen_pool"]
    )


def test_no_replayed_paper_becomes_eligible_without_a_new_judgment() -> None:
    """Rigor: no correction in this slice admits a paper on its own."""
    replayed = [_replayed(row) for row in _rows()]
    assert all(row["decision"] != "eligible" for row in replayed)
    excluded = [row for row in _rows() if row["recorded_decision"] == "excluded"]
    assert all(_replayed(row)["decision"] == "excluded" for row in excluded)
