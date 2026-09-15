"""Behavioral calibration of the source-blind standalone gate.

Contract ``source-blind-scientific-referent-v3`` is released only behind this
set. The rows come from the r15 holistic acceptance audit: every ``must_pass``
row is an item the auditors judged source-blind answerable and paper-supported,
and every ``must_fail`` row is an item that is not interpretable without the
paper. Two of the ``must_fail`` rows are items the predecessor contract wrongly
passed.

The set is PROVISIONAL. It carries one labeler. Two independent human labelers
must answer the north-star question for every ``must_pass`` row, and only
unanimous rows may stay, before it gates a production release. Until then it
gates the contract in tests only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctic_qa import generation
from arctic_qa.validation import (
    STANDALONE_CALIBRATION_MUST_PASS_RATE,
    STANDALONE_CALIBRATION_SET_VERSION,
    STANDALONE_VERIFICATION_CONTRACT_VERSION,
    benchmark_context_verification_reason,
    standalone_gate_decision,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "standalone-calibration-v1.jsonl"
RECORDS = [
    json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
]
HEADER = RECORDS[0]
ROWS = RECORDS[1:]
MUST_PASS = [row for row in ROWS if row["label"] == "must_pass"]
MUST_FAIL = [row for row in ROWS if row["label"] == "must_fail"]


def _identifier(row: dict[str, object]) -> str:
    return str(row["item_id"])


def test_calibration_set_declares_its_contract_and_its_blocking_note() -> None:
    assert HEADER["record"] == "calibration_set_header"
    assert HEADER["calibration_set_version"] == STANDALONE_CALIBRATION_SET_VERSION
    assert HEADER["contract_version"] == STANDALONE_VERIFICATION_CONTRACT_VERSION
    assert HEADER["labeling_status"] == "single_labeler_provisional"
    assert "Two independent human labelers" in HEADER["blocking_note"]
    assert "held_out" in HEADER
    assert str(STANDALONE_CALIBRATION_MUST_PASS_RATE) == "0.8"
    assert len(MUST_PASS) >= 15
    assert len(MUST_FAIL) >= 12


@pytest.mark.parametrize("row", MUST_PASS, ids=_identifier)
def test_every_must_pass_row_passes_the_composed_gate(row: dict) -> None:
    """A correct v3 judge reports nothing, so the gate must release the row."""
    assert (
        standalone_gate_decision(
            row["question"],
            row["question_context"],
            row["answer"],
            model_reasons=row["model_reasons"],
            model_answer_leakage_absent=True,
        )
        == []
    )


@pytest.mark.parametrize("row", MUST_PASS, ids=_identifier)
def test_the_deterministic_half_never_kills_a_must_pass_row(row: dict) -> None:
    assert row["deterministic_expectation"] == "pass"
    assert (
        benchmark_context_verification_reason(row["question"], row["question_context"])
        is None
    )


@pytest.mark.parametrize("row", MUST_FAIL, ids=_identifier)
def test_every_must_fail_row_fails_the_composed_gate(row: dict) -> None:
    assert standalone_gate_decision(
        row["question"],
        row["question_context"],
        row["answer"],
        model_reasons=row["model_reasons"],
        model_answer_leakage_absent=True,
    )


@pytest.mark.parametrize(
    "row",
    [row for row in MUST_FAIL if row["deterministic_expectation"] == "reject"],
    ids=_identifier,
)
def test_the_deterministic_half_alone_kills_the_named_rows(row: dict) -> None:
    """These classes must not depend on a model call to be caught."""
    assert standalone_gate_decision(
        row["question"], row["question_context"], row["answer"]
    )


def test_the_two_predecessor_false_passes_are_closed_deterministically() -> None:
    """r15 audit section 4.8 item 2: opaque cruise codes and a bare
    'the combined expeditions' passed the predecessor contract."""
    cruise_codes = next(
        row for row in MUST_FAIL if row["item_id"] == "aqa-0e07e2ac78c048db8c31"
    )
    combined = next(
        row for row in MUST_FAIL if row["item_id"] == "aqa-3af119e6f5b0e447d8fd"
    )
    # Judge the text with its PDF line break repaired, so the verdict rests on
    # the referent defect and not on the extraction artefact.
    assert (
        benchmark_context_verification_reason(
            cruise_codes["question"].replace("\n", " "),
            cruise_codes["question_context"],
        )
        == "question_context_referent_unresolved"
    )
    assert (
        benchmark_context_verification_reason(
            combined["question"].replace("\n", " "), combined["question_context"]
        )
        == "question_context_missing"
    )


def test_the_v3_prompt_states_every_rule_the_calibration_set_exercises() -> None:
    system = generation.STANDALONE_SYSTEM
    for clause in (
        "NECESSITY TEST",
        "Two readers who both understand the task can defend different answers",
        "A named campaign, cruise, core, or project code does not resolve a referent",
        "A period fixed only by the publication date",
        "A pointer to source material",
        "Text that is broken, garbled, or cut in the middle of a word",
        "A task that states its own answer",
        "A quantity stated with its own sample size",
        "A period fixed by an event that the task names",
    ):
        assert clause in system
