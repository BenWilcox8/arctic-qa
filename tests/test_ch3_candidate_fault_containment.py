"""The chapter 3 candidate fault slice: one option repair, one containment.

Three chapter 3 production runs ended on 2026-09-16 because one candidate of
one paper family raised an exception. The last of them, at 16:28 UTC, raised
``option repair trigger is invalid`` on family-a5bcbcf9a3aff33d55ed. The
recorded attempt and the recorded verdict of that family drive the first half
of this file; the second half holds the producer to the rule that only a money
stop ends it.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from arctic_qa import generation
from arctic_qa import streaming
from arctic_qa.broker_provider import broker_boundary
from arctic_qa.db import Database
from arctic_qa.errors import (
    BudgetError,
    CandidateRejectedError,
    PaperCostCapError,
    ProviderResponseError,
    is_run_stop,
)
from arctic_qa.paths import DataPaths
from arctic_qa.providers import FakeProvider
from arctic_qa.streaming import run_stream

from test_streaming import FIXTURES, streaming_fixture, write_json


# The generation attempt the producer built at 16:27:31 UTC on 2026-09-16 and
# refused at 16:28, read from the candidates row
# generation-call-5b789d0e60d108a40ada of run chapter3-7dc6485-r3.
RECORDED_OPTION_REPAIR_ATTEMPT: dict[str, Any] = {
    "attempt_id": "generation-attempt-2269f55cd9a1762a7666",
    "attempt_kind": "option_repair",
    "contract_version": "bounded-failure-routing-v5",
    "excluded_finding_span_ids": [],
    "finding_attempt_index": 1,
    "finding_policy_version": "one-finding-per-paper-ranked-context-v8:finding-1",
    "parent_attempt_id": "generation-attempt-b7aaa8b2aec3c5f232c3",
    "parent_item_id": "aqa-c19e58a71497c5bad90c",
    "question_revision_index": 1,
    "repair_numeric_rule": False,
    "slot_lookup": None,
    "trigger_reason_code": "option_set_not_mutually_exclusive",
}
# The reason codes of validation event validation-351efd82bc41a0765734, the
# whole-set verdict on candidate aqa-c19e58a71497c5bad90c. Its option set held
# both "higher" and "substantially higher", so the two overlap.
RECORDED_REASON_CODES = ["option_set_not_mutually_exclusive"]
RECORDED_ERROR_MESSAGE = "option repair trigger is invalid"


# The option repair trigger: one closed set, owned by the contract.


def test_the_recorded_option_repair_attempt_passes_the_generation_contract() -> None:
    """The 16:28 UTC exit of run chapter3-7dc6485-r3."""
    attempt = generation._validated_generation_attempt(
        dict(RECORDED_OPTION_REPAIR_ATTEMPT)
    )

    assert attempt is not None
    assert attempt["trigger_reason_code"] == "option_set_not_mutually_exclusive"


def test_the_recorded_option_repair_attempt_passes_the_routing_contract() -> None:
    attempt = streaming._validate_generation_attempt(
        dict(RECORDED_OPTION_REPAIR_ATTEMPT)
    )

    assert attempt["attempt_kind"] == "option_repair"


def test_the_recorded_whole_set_verdict_routes_to_the_option_repair_rung() -> None:
    assert streaming._repair_kind(RECORDED_REASON_CODES, 0) == "option_repair"


def test_the_contract_and_the_routing_layer_name_one_closed_trigger_set() -> None:
    """Routing accepted three codes and the contract named one; the run died."""
    assert streaming.OPTION_REPAIR_REASONS is generation.OPTION_REPAIR_TRIGGER_REASONS
    assert generation.OPTION_REPAIR_TRIGGER_REASONS == {
        "insufficient_verified_distractors",
        "option_set_not_mutually_exclusive",
        "option_set_answer_not_choosable",
    }


@pytest.mark.parametrize(
    "trigger",
    ["standalone_undefined_location", "question_context_missing", ""],
)
def test_a_trigger_outside_the_closed_set_is_still_refused(trigger: str) -> None:
    attempt = {**RECORDED_OPTION_REPAIR_ATTEMPT, "trigger_reason_code": trigger}

    with pytest.raises(ValueError):
        generation._validated_generation_attempt(attempt)


# Containment: only a money stop ends the producer.


def test_a_candidate_fault_is_not_a_run_stop() -> None:
    assert _ends_the_run(ValueError(RECORDED_ERROR_MESSAGE)) is False
    assert _ends_the_run(KeyError("option_display_prefilter")) is False
    assert (
        _ends_the_run(
            PaperCostCapError("the paid request exceeds the paper cost limit")
        )
        is False
    )


@pytest.mark.parametrize(
    "error",
    [
        BudgetError("the paid request exceeds the project lifetime ceiling"),
        BudgetError("the paid request exceeds the authorized away cap"),
        BudgetError("the away-session submission limit is complete"),
        BudgetError("the accepted-question target is complete"),
        ValueError("the paid-call broker is halted: ambiguous_generation_charge"),
        ValueError("the shared paid-call ledger has an integrity halt"),
    ],
)
def test_a_money_stop_ends_the_producer(error: Exception) -> None:
    assert _ends_the_run(error) is True


def _ends_the_run(error: BaseException) -> bool:
    return streaming._ends_the_run(error)


# The broker seam marks every whole-run refusal, so a new refusal message needs
# no second registration to keep ending the run.


@pytest.mark.parametrize(
    "message",
    [
        "streaming live generation is disabled",
        "the streaming execution gate does not allow this phase",
        "the integrated offline review did not pass",
        "unsupported streaming execution gate schema",
        "the paid-call broker is halted: ambiguous_generation_charge",
    ],
)
def test_the_broker_seam_marks_a_refusal_as_a_run_stop(message: str) -> None:
    with pytest.raises(ValueError) as raised:
        with broker_boundary():
            raise ValueError(message)

    assert str(raised.value) == message
    assert is_run_stop(raised.value) is True
    assert _ends_the_run(raised.value) is True


@pytest.mark.parametrize(
    "error",
    [
        CandidateRejectedError("option_pool_empty_after_prefilter", "no option left"),
        ProviderResponseError("the broker response contains malformed JSON"),
        PaperCostCapError("the paid request exceeds the paper cost limit"),
    ],
)
def test_the_broker_seam_leaves_a_paper_level_refusal_alone(error: Exception) -> None:
    with pytest.raises(type(error)) as raised:
        with broker_boundary():
            raise error

    assert is_run_stop(raised.value) is False


def test_a_disabled_execution_gate_still_ends_the_producer() -> None:
    """The gate is the authorization of the run, never one paper's business."""
    assert _ends_the_run(ValueError("streaming live generation is disabled")) is False
    with pytest.raises(ValueError) as raised:
        with broker_boundary():
            raise ValueError("streaming live generation is disabled")
    assert _ends_the_run(raised.value) is True


# The producer, driven end to end over two papers.


def two_paper_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Return the one-paper streaming fixture with a second paper behind it."""
    access, eligibility = streaming_fixture(tmp_path)
    first_key = "test-only:streaming-paper"
    second_key = "test-only:streaming-paper-2"
    source = access / "originals" / "source-2.html"
    source.write_bytes(
        (FIXTURES / "public-source.html").read_bytes()
        + b"\n<!-- second fixture paper -->\n"
    )
    extracted = access / "extracted" / "text-2.txt"
    extracted.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    source_hash = sha256(source.read_bytes()).hexdigest()
    extraction_hash = sha256(extracted.read_bytes()).hexdigest()

    item = json.loads(
        (access / "items" / "item-000001.json").read_text(encoding="utf-8")
    )
    write_json(
        access / "items" / "item-000002.json",
        {
            **item,
            "position": 2,
            "candidate_key": second_key,
            "source_path": str(source),
            "source_content_hash": source_hash,
            "extraction_path": str(extracted),
            "extraction_sha256": extraction_hash,
        },
    )
    manifest = json.loads((access / "run-manifest.json").read_text(encoding="utf-8"))
    selected = dict(manifest["selection"][0])
    manifest["target_total"] = 2
    manifest["selection"] = [
        selected,
        {**selected, "position": 2, "candidate_key": second_key},
    ]
    write_json(access / "run-manifest.json", manifest)

    job = json.loads(
        (eligibility / "jobs" / "fixture-job.json").read_text(encoding="utf-8")
    )
    parsed = {
        **job["parsed_response"],
        "request_id": "fixture-job-2",
        "input_echo": {
            **job["parsed_response"]["input_echo"],
            "source_version_sha256": source_hash,
            "extracted_text_sha256": extraction_hash,
        },
    }
    write_json(
        eligibility / "jobs" / "fixture-job-2.json",
        {
            **job,
            "job_key": "fixture-job-2",
            "candidate_key": second_key,
            "source_content_hash": source_hash,
            "extraction_sha256": extraction_hash,
            "parsed_response": parsed,
        },
    )
    assert first_key != second_key
    return access, eligibility


def _stream_arguments(tmp_path: Path, run_id: str) -> dict[str, Any]:
    access, eligibility = two_paper_fixture(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    return {
        "db": database,
        "namespace": paths.namespace,
        "run_id": run_id,
        "campaign_id": f"{run_id}-campaign",
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "author": FakeProvider("fake-author", FIXTURES / "fake-author.jsonl"),
        "verifier": FakeProvider("fake-verifier", FIXTURES / "fake-verifier.jsonl"),
        "max_papers": 2,
    }


def _fault_on_the_first_family(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> list[str]:
    """Raise ``error`` for the first paper only; the second runs for real."""
    real = streaming.generate_candidate
    faulted: list[str] = []

    def fake(*arguments: Any, **keywords: Any) -> Any:
        source_id = str(keywords["source_id"])
        if not faulted:
            faulted.append(source_id)
            raise error
        return real(*arguments, **keywords)

    monkeypatch.setattr(streaming, "generate_candidate", fake)
    return faulted


def test_the_recorded_fault_settles_its_paper_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 16:28 UTC fault, replayed over two papers."""
    arguments = _stream_arguments(tmp_path, "candidate-fault-containment")
    _fault_on_the_first_family(monkeypatch, ValueError(RECORDED_ERROR_MESSAGE))

    result = run_stream(**arguments)

    assert result["state"] == "completed"
    assert result["counts"]["processed"] == 2
    assert result["counts"]["candidate_processing_fault"] == 1
    first, second = result["paper_results"]
    assert first["disposition"] == "candidate_processing_fault"
    assert first["candidate_processing_fault"]["error_class"] == "ValueError"
    assert first["candidate_processing_fault"]["error_message"] == (
        RECORDED_ERROR_MESSAGE
    )
    assert first["candidate_processing_fault"]["stage"] == "generation"
    assert second["disposition"] != "candidate_processing_fault"


def test_a_contained_fault_leaves_a_generation_routing_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _stream_arguments(tmp_path, "candidate-fault-row")
    _fault_on_the_first_family(monkeypatch, ValueError(RECORDED_ERROR_MESSAGE))

    run_stream(**arguments)

    rows = arguments["db"].rows(
        """SELECT reason_code,detail_json FROM rejection_ledger
        WHERE stage='generation_routing' AND reason_code=?""",
        (streaming.CANDIDATE_PROCESSING_FAULT_REASON_CODE,),
    )
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail_json"])
    assert detail["error_class"] == "ValueError"
    assert detail["error_message"] == RECORDED_ERROR_MESSAGE
    assert detail["stage"] == "generation"
    assert detail["settled_call_records"]


def test_a_contained_fault_settles_its_in_flight_call_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _stream_arguments(tmp_path, "candidate-fault-record")
    _fault_on_the_first_family(monkeypatch, ValueError(RECORDED_ERROR_MESSAGE))

    result = run_stream(**arguments)

    settled = result["paper_results"][0]["candidate_processing_fault"][
        "settled_call_records"
    ]
    assert settled
    for item_id in settled:
        row = arguments["db"].one(
            "SELECT status,candidate_json FROM candidates WHERE item_id=?", (item_id,)
        )
        assert row["status"] == "incomplete_infra"
        record = json.loads(row["candidate_json"])
        assert record["reason_code"] == (
            streaming.CANDIDATE_PROCESSING_FAULT_REASON_CODE
        )
        assert record["candidate_processing_fault"] == {
            "contract_version": (streaming.CANDIDATE_PROCESSING_FAULT_CONTRACT_VERSION),
            "error_class": "ValueError",
            "error_message": RECORDED_ERROR_MESSAGE,
            "stage": "generation",
        }


def test_a_key_error_in_one_candidate_is_contained_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _stream_arguments(tmp_path, "candidate-fault-key-error")
    _fault_on_the_first_family(monkeypatch, KeyError("option_display_prefilter"))

    result = run_stream(**arguments)

    assert result["state"] == "completed"
    assert result["paper_results"][0]["candidate_processing_fault"]["error_class"] == (
        "KeyError"
    )


def test_a_ceiling_stop_still_ends_the_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _stream_arguments(tmp_path, "candidate-fault-ceiling")
    _fault_on_the_first_family(
        monkeypatch,
        BudgetError("the paid request exceeds the project lifetime ceiling"),
    )

    with pytest.raises(BudgetError, match="project lifetime ceiling"):
        run_stream(**arguments)


def test_a_ledger_integrity_failure_still_ends_the_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _stream_arguments(tmp_path, "candidate-fault-integrity")
    _fault_on_the_first_family(
        monkeypatch,
        ValueError("the shared paid-call ledger has an integrity halt"),
    )

    with pytest.raises(ValueError, match="ledger has an integrity halt"):
        run_stream(**arguments)
