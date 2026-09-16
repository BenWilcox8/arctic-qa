"""Behavioral calibration of the source-blind standalone gate.

Contract ``source-blind-scientific-referent-v3`` is released only behind the
calibration set ``fixtures/standalone-calibration-v2.jsonl``. Every row
carries two labelers as fields and gates only when they agree. The ``core``
slice holds the r15 rows; the ``held_out`` slice holds chapter 2 candidates
the yield audit judged, and is never used to iterate the prompt text.

The deterministic half is exercised here directly. The judge half is
exercised through a recorded cassette: ``record`` mode calls the live judge
(paid, never in tests), ``replay`` mode applies the release rule offline. The
tests below record a cassette with the fake provider to prove the record and
replay paths without a live call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctic_qa import generation
from arctic_qa.providers import make_provider
from arctic_qa.standalone_calibration import (
    CASSETTE_CONTRACT_VERSION,
    CalibrationError,
    calibration_prompt,
    evaluate_cassette,
    load_calibration_set,
    record_cassette,
    standalone_system_sha256,
)
from arctic_qa.validation import (
    STANDALONE_CALIBRATION_MUST_PASS_RATE,
    STANDALONE_CALIBRATION_SET_VERSION,
    STANDALONE_VERIFICATION_CONTRACT_VERSION,
    benchmark_context_verification_reason,
    standalone_gate_decision,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "standalone-calibration-v2.jsonl"
CALIBRATION = load_calibration_set(FIXTURE)
HEADER = CALIBRATION.header
ROWS = CALIBRATION.rows
MUST_PASS = [row for row in ROWS if row["label"] == "must_pass"]
MUST_FAIL = [row for row in ROWS if row["label"] == "must_fail"]
DISPUTED = [row for row in ROWS if row["label"] == "disputed"]


def _identifier(row: dict[str, object]) -> str:
    return str(row["item_id"])


def _judge_response(row: dict) -> dict:
    """The response a correct judge gives: nothing for must_pass, its codes otherwise."""
    reasons = list(row["model_reasons"])
    if row["label"] == "must_pass":
        return {
            "pass": True,
            "answer_leakage_absent": True,
            "unresolved_phrases": [],
            "competing_readings": [],
            "missing_detail_types": [],
            "reasons": [],
            "review_rationale": "The task is interpretable without the paper.",
        }
    # Contract v4 binds every failing code to displayed evidence: a phrase for
    # an undefined_* code and two readings for multiple_interpretations.
    return {
        "pass": False,
        "answer_leakage_absent": "answer_leakage" not in reasons,
        "unresolved_phrases": ["the unresolved phrase"],
        "competing_readings": ["the first reading", "the second reading"],
        "missing_detail_types": ["other"],
        "reasons": [reason for reason in reasons if reason != "answer_leakage"],
        "review_rationale": "A necessary detail is missing.",
    }


def _write_fake_script(path: Path, responses: dict[str, dict]) -> Path:
    events = [
        {"role": "standalone_verifier", "response": responses[str(row["item_id"])]}
        for row in CALIBRATION.gating_rows
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    return path


def test_calibration_set_declares_its_contract_and_two_labelers() -> None:
    assert HEADER["record"] == "calibration_set_header"
    assert HEADER["calibration_set_version"] == STANDALONE_CALIBRATION_SET_VERSION
    assert HEADER["contract_version"] == STANDALONE_VERIFICATION_CONTRACT_VERSION
    assert HEADER["labeling_status"] == "two_labelers_recorded_unanimous_rows_gate"
    assert len(HEADER["labelers"]) == 3
    assert "Neither labeler is a human" in HEADER["blocking_note"]
    assert "held_out" in HEADER
    assert str(STANDALONE_CALIBRATION_MUST_PASS_RATE) == "0.8"
    assert len(MUST_PASS) >= 20
    assert len(MUST_FAIL) >= 20
    assert {row["slice"] for row in ROWS} == {"core", "held_out"}
    for row in ROWS:
        labels = row["labels"]
        assert set(labels) == {"labeler_1", "labeler_2"}
        assert labels["labeler_1"]["labeler"] != labels["labeler_2"]["labeler"]
        agreed = labels["labeler_1"]["label"] == labels["labeler_2"]["label"]
        assert row["label"] == (labels["labeler_1"]["label"] if agreed else "disputed")


def test_a_disputed_row_is_kept_but_never_gates() -> None:
    assert DISPUTED, "the set records at least one labeler disagreement"
    gating_ids = {row["item_id"] for row in CALIBRATION.gating_rows}
    assert all(row["item_id"] not in gating_ids for row in DISPUTED)


def test_every_row_keeps_the_typed_reason_codes() -> None:
    """Keep column of the audit: typed reason codes on every row."""
    enum = set(
        generation.ROLE_SCHEMAS["standalone_verifier"]["properties"]["reasons"][
            "items"
        ]["enum"]
    )
    for row in ROWS:
        assert isinstance(row["model_reasons"], list)
        assert set(row["model_reasons"]) <= enum
        if row["slice"] == "held_out":
            assert set(row["recorded_judge_reasons"]) <= enum


@pytest.mark.parametrize("row", MUST_PASS, ids=_identifier)
def test_every_must_pass_row_passes_the_composed_gate(row: dict) -> None:
    """A correct v3 judge reports nothing, so the gate must release the row."""
    assert (
        standalone_gate_decision(
            row["question"],
            row["question_context"],
            row["answer"],
            model_reasons=[],
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
        model_answer_leakage_absent="answer_leakage" not in row["model_reasons"],
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


def test_the_v4_prompt_states_every_rule_the_calibration_set_exercises() -> None:
    system = generation.STANDALONE_SYSTEM
    for clause in (
        "NECESSITY TEST",
        "two readers who both understand the task can defend answers about "
        "different things",
        "A named campaign, cruise, core, or project code does not resolve a referent",
        "A period fixed only by the publication date",
        "A pointer to source material",
        "Text that is broken, garbled, or cut in the middle of a word",
        "A task that states its own answer",
        "A quantity stated with its own sample size",
        "A period fixed by an event that the task names",
    ):
        assert clause in system


# --- the cassette: record with the fake provider, replay offline ------------


def test_calibration_prompt_is_the_pipeline_prompt() -> None:
    row = MUST_PASS[0]
    prompt = calibration_prompt(row)
    assert prompt.startswith("DISPLAYED_TASK\n")
    assert json.loads(prompt.split("\n", 1)[1]) == {
        "question": row["question"],
        "question_context": row["question_context"],
    }


def test_record_and_replay_a_correct_judge_passes_the_release_rule(
    tmp_path: Path,
) -> None:
    responses = {
        str(row["item_id"]): _judge_response(row) for row in CALIBRATION.gating_rows
    }
    script = _write_fake_script(tmp_path / "judge.jsonl", responses)
    provider = make_provider("fake", "fake-judge", script)
    cassette = tmp_path / "cassette.jsonl"

    recorded = record_cassette(CALIBRATION, provider, cassette)
    report = evaluate_cassette(CALIBRATION, cassette)

    assert recorded["standalone_system_sha256"] == standalone_system_sha256()
    assert recorded["cassette_contract_version"] == CASSETTE_CONTRACT_VERSION
    assert recorded["rows"] == len(CALIBRATION.gating_rows)
    assert report["passed"] is True
    assert report["must_fail_violations"] == []
    assert report["must_pass_passed"] == report["must_pass_total"] == len(MUST_PASS)
    assert {row["item_id"] for row in report["rows"]} == {
        row["item_id"] for row in CALIBRATION.gating_rows
    }
    # The disputed rows were neither recorded nor scored.
    assert all(
        row["item_id"] not in {r["item_id"] for r in report["rows"]} for row in DISPUTED
    )


def test_replay_fails_when_a_must_fail_row_passes(tmp_path: Path) -> None:
    """The must-fail controls keep a prompt relaxation honest."""
    responses = {
        str(row["item_id"]): _judge_response(row) for row in CALIBRATION.gating_rows
    }
    lenient = next(
        row for row in MUST_FAIL if row["deterministic_expectation"] == "defer"
    )
    responses[str(lenient["item_id"])] = _judge_response(
        {**lenient, "label": "must_pass"}
    )
    script = _write_fake_script(tmp_path / "judge.jsonl", responses)
    cassette = tmp_path / "cassette.jsonl"
    record_cassette(CALIBRATION, make_provider("fake", "fake-judge", script), cassette)

    report = evaluate_cassette(CALIBRATION, cassette)

    assert report["passed"] is False
    assert report["must_fail_violations"] == [lenient["item_id"]]


def test_replay_fails_below_the_must_pass_rate(tmp_path: Path) -> None:
    responses = {
        str(row["item_id"]): _judge_response(row) for row in CALIBRATION.gating_rows
    }
    strict = MUST_PASS[: len(MUST_PASS) // 2 + 1]
    for row in strict:
        responses[str(row["item_id"])] = _judge_response(
            {**row, "label": "must_fail", "model_reasons": ["multiple_interpretations"]}
        )
    script = _write_fake_script(tmp_path / "judge.jsonl", responses)
    cassette = tmp_path / "cassette.jsonl"
    record_cassette(CALIBRATION, make_provider("fake", "fake-judge", script), cassette)

    report = evaluate_cassette(CALIBRATION, cassette)

    assert report["passed"] is False
    assert report["must_fail_violations"] == []
    assert float(report["must_pass_rate"]) < 0.8


def test_a_cassette_recorded_under_another_prompt_is_stale(tmp_path: Path) -> None:
    responses = {
        str(row["item_id"]): _judge_response(row) for row in CALIBRATION.gating_rows
    }
    script = _write_fake_script(tmp_path / "judge.jsonl", responses)
    cassette = tmp_path / "cassette.jsonl"
    record_cassette(CALIBRATION, make_provider("fake", "fake-judge", script), cassette)
    lines = cassette.read_text().splitlines()
    header = json.loads(lines[0])
    header["standalone_system_sha256"] = "0" * 64
    cassette.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n")

    with pytest.raises(CalibrationError, match="another STANDALONE_SYSTEM"):
        evaluate_cassette(CALIBRATION, cassette)


def test_a_cassette_missing_a_gating_row_is_refused(tmp_path: Path) -> None:
    responses = {
        str(row["item_id"]): _judge_response(row) for row in CALIBRATION.gating_rows
    }
    script = _write_fake_script(tmp_path / "judge.jsonl", responses)
    cassette = tmp_path / "cassette.jsonl"
    record_cassette(CALIBRATION, make_provider("fake", "fake-judge", script), cassette)
    lines = cassette.read_text().splitlines()
    cassette.write_text("\n".join(lines[:-1]) + "\n")

    with pytest.raises(CalibrationError, match="no response for"):
        evaluate_cassette(CALIBRATION, cassette)


def test_a_malformed_judge_response_never_enters_a_cassette(tmp_path: Path) -> None:
    responses = {
        str(row["item_id"]): _judge_response(row) for row in CALIBRATION.gating_rows
    }
    first = str(CALIBRATION.gating_rows[0]["item_id"])
    responses[first] = {**responses[first], "reasons": ["not_a_code"]}
    script = _write_fake_script(tmp_path / "judge.jsonl", responses)
    cassette = tmp_path / "cassette.jsonl"

    with pytest.raises(Exception):
        record_cassette(
            CALIBRATION, make_provider("fake", "fake-judge", script), cassette
        )
    assert not cassette.exists()


def test_a_row_whose_labelers_disagree_must_be_labelled_disputed(
    tmp_path: Path,
) -> None:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["labels"]["labeler_2"]["label"] = (
        "must_fail"
        if row["labels"]["labeler_1"]["label"] == "must_pass"
        else "must_pass"
    )
    broken = tmp_path / "set.jsonl"
    broken.write_text("\n".join([lines[0], json.dumps(row), *lines[2:]]) + "\n")

    with pytest.raises(
        CalibrationError, match="does not equal the labelers' agreement"
    ):
        load_calibration_set(broken)
