"""A provider rejection before generation: recorded body, reviewed settlement.

Chapter 3's first production call returned HTTP 400 INVALID_ARGUMENT. The
broker booked an ambiguous charge and halted, as designed, but it kept no
error body and had no release path below 5xx. These tests pin both changes:
every non-2xx answer keeps its body in the receipt, and one reviewed
settlement releases a proven rejection at zero cost without a replay.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from arctic_qa.model_broker import (  # noqa: E402
    HTTP_REJECTION_EVIDENCE_SCHEMA,
    HTTP_REJECTION_SETTLEMENT_SCHEMA,
    SharedGeminiBroker,
)
from arctic_qa.util import sha256_file  # noqa: E402
from test_ambiguous_continuation import (  # noqa: E402
    Http500ThenSuccess,
    execute,
    fixture,
    write_json,
)

REJECTION_BODY = json.dumps(
    {
        "error": {
            "code": 400,
            "message": "Request contains an invalid argument.",
            "status": "INVALID_ARGUMENT",
        }
    }
)


class RejectingTransport(Http500ThenSuccess):
    """Reject the first generation with a 400 that carries a body."""

    status = 400

    def __init__(self, *, with_body: bool = True) -> None:
        super().__init__()
        self.with_body = with_body

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 100}
        self.generation_calls += 1
        if self.generation_calls == 1:
            raise urllib.error.HTTPError(
                "https://fake.invalid",
                400,
                "Bad Request",
                {},
                io.BytesIO(REJECTION_BODY.encode()) if self.with_body else None,
            )
        return super().post(model, method, body)


def _evidence(
    receipt: dict, path: Path, *, source: str = "receipt", reproduction=None
) -> Path:
    write_json(
        path,
        {
            "schema": HTTP_REJECTION_EVIDENCE_SCHEMA,
            "request_key": receipt["request_key"],
            "error_class": "known_http_response_unknown_charge",
            "http_status": 400,
            "provider_error_status": "INVALID_ARGUMENT",
            "error_body_source": source,
            "reproduction": reproduction,
            "live_call_made": True,
            "received_receipt_absent": True,
            "generation_started": False,
            "actual_cost_known": True,
            "actual_cost_usd": "0",
            "replay_prohibited": True,
            "affected_family_id": receipt["family_id"],
            "authorized_run_id": receipt["run_id"],
        },
    )
    return path


def _settle(values: dict, receipt: dict, evidence: Path) -> dict:
    return values["broker"].settle_http_rejection(
        request_key=receipt["request_key"],
        expected_ledger_sha256=sha256_file(values["ledger"]),
        review_file=values["review"],
        evidence_file=evidence,
        authorized_run_id="run-current",
        operator_id="test-operator",
    )


def _reopen(values: dict, transport: object) -> SharedGeminiBroker:
    return SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=values["receipts"],
        credential_file=values["ledger"].parent / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
    )


def test_a_rejection_keeps_its_provider_body_and_halts(tmp_path: Path) -> None:
    transport = RejectingTransport()
    values = fixture(tmp_path, transport)
    receipt = execute(values["broker"], paper="p1", run_id="run-current")
    assert receipt["state"] == "ambiguous_charge"
    assert receipt["http_status"] == 400
    assert receipt["error_body"] == REJECTION_BODY
    assert receipt["provider_error_status"] == "INVALID_ARGUMENT"
    ledger = json.loads(values["ledger"].read_text())
    assert ledger["halted"] is True
    assert Decimal(ledger["ambiguous_reserved_usd"]) == Decimal(receipt["reserved_usd"])
    stored = json.loads(
        (values["receipts"] / f"{receipt['request_key']}.json").read_text()
    )
    assert stored["error_body"] == REJECTION_BODY
    assert values["broker"].status()["integrity_valid"] is True


def test_settlement_releases_the_reservation_and_lifts_the_halt(
    tmp_path: Path,
) -> None:
    transport = RejectingTransport()
    values = fixture(tmp_path, transport)
    receipt = execute(values["broker"], paper="p1", run_id="run-current")
    before = values["broker"].status()
    evidence = _evidence(receipt, tmp_path / "evidence.json")

    result = _settle(values, receipt, evidence)

    assert result["applied"] is True
    assert result["released_usd"] == receipt["reserved_usd"]
    assert result["halted"] is False
    ledger = json.loads(values["ledger"].read_text())
    request = ledger["requests"][receipt["request_key"]]
    assert request["state"] == "completed"
    assert request["actual_cost_usd"] == "0"
    assert (
        request["http_rejection_settlement_sha256"]
        == result["settlement_receipt_sha256"]
    )
    assert Decimal(ledger["ambiguous_reserved_usd"]) == 0
    assert ledger["halted"] is False
    assert ledger["spent_usd"] == before["spent_usd"]
    event = json.loads(Path(result["settlement_receipt"]).read_text())
    assert event["schema"] == HTTP_REJECTION_SETTLEMENT_SCHEMA
    assert event["error_body_source"] == "receipt"
    assert event["actual_cost_usd"] == "0"
    # The immutable ambiguous receipt is unchanged and still bound.
    final = json.loads(
        (values["receipts"] / f"{receipt['request_key']}.json").read_text()
    )
    assert final["state"] == "ambiguous_charge"
    assert event["ambiguous_receipt_sha256"] == sha256_file(
        values["receipts"] / f"{receipt['request_key']}.json"
    )
    status = values["broker"].status()
    assert status["integrity_valid"] is True
    assert status["halted"] is False
    assert transport.generation_calls == 1
    # A restart validates the settlement and the settled view has no response.
    reopened = _reopen(values, RejectingTransport())
    assert reopened.status()["integrity_valid"] is True
    view = reopened.effective_receipt(receipt["request_key"])
    assert view["state"] == "rejected_settled"
    assert "response" not in view
    # Applying twice returns the existing settlement without a change.
    again = _settle(values, receipt, evidence)
    assert again["applied"] is False
    assert again["settlement_receipt_sha256"] == result["settlement_receipt_sha256"]
    # Unrelated work continues; the settled key is never replayed.
    follow = execute(values["broker"], paper="p2", run_id="run-current")
    assert follow["state"] == "completed"
    with pytest.raises(ValueError, match="already exists"):
        execute(values["broker"], paper="p1", run_id="run-current")


def test_a_receipt_without_a_body_settles_on_a_matching_reproduction(
    tmp_path: Path,
) -> None:
    transport = RejectingTransport(with_body=False)
    values = fixture(tmp_path, transport)
    receipt = execute(values["broker"], paper="p1", run_id="run-current")
    assert receipt["error_body"] is None
    assert receipt["provider_error_status"] is None
    record = tmp_path / "diagnostic.json"
    write_json(
        record,
        {
            "schema": "arctic-ch3-diagnostic-call-v1",
            "request_key": receipt["request_key"],
            "model": receipt["model"],
            "request_sha256_from_trace": receipt["request_sha256"],
            "http_status": 400,
            "response_body": REJECTION_BODY,
        },
    )
    reproduction = {
        "record_file": str(record),
        "record_file_sha256": sha256_file(record),
        "request_sha256": receipt["request_sha256"],
        "http_status": 400,
        "provider_error_status": "INVALID_ARGUMENT",
    }
    # A reproduction of a different request is refused.
    wrong = dict(reproduction, request_sha256="0" * 64)
    with pytest.raises(ValueError, match="does not match"):
        _settle(
            values,
            receipt,
            _evidence(
                receipt,
                tmp_path / "wrong.json",
                source="reproduction",
                reproduction=wrong,
            ),
        )
    assert json.loads(values["ledger"].read_text())["halted"] is True

    result = _settle(
        values,
        receipt,
        _evidence(
            receipt,
            tmp_path / "evidence.json",
            source="reproduction",
            reproduction=reproduction,
        ),
    )
    assert result["applied"] is True
    event = json.loads(Path(result["settlement_receipt"]).read_text())
    assert event["error_body_source"] == "reproduction"
    assert event["reproduction_record_sha256"] == sha256_file(record)
    assert json.loads(values["ledger"].read_text())["halted"] is False
    assert _reopen(values, RejectingTransport()).status()["integrity_valid"] is True


def test_a_server_error_is_not_a_rejection_case(tmp_path: Path) -> None:
    transport = Http500ThenSuccess()
    values = fixture(tmp_path, transport)
    receipt = execute(values["broker"], paper="p1", run_id="run-current")
    assert receipt["http_status"] == 500
    with pytest.raises(ValueError, match="not a provider rejection"):
        _settle(values, receipt, _evidence(receipt, tmp_path / "evidence.json"))
    assert json.loads(values["ledger"].read_text())["halted"] is True


def test_inexact_evidence_is_refused_without_a_change(tmp_path: Path) -> None:
    transport = RejectingTransport()
    values = fixture(tmp_path, transport)
    receipt = execute(values["broker"], paper="p1", run_id="run-current")
    ledger_before = sha256_file(values["ledger"])
    evidence = _evidence(receipt, tmp_path / "evidence.json")
    value = json.loads(evidence.read_text())
    value["actual_cost_known"] = False
    write_json(evidence, value)
    with pytest.raises(ValueError, match="not exact"):
        _settle(values, receipt, evidence)
    assert sha256_file(values["ledger"]) == ledger_before
    assert not (
        values["receipts"] / f"{receipt['request_key']}.http-rejection-settlement.json"
    ).exists()


def test_a_forged_settlement_event_halts_the_restart(tmp_path: Path) -> None:
    transport = RejectingTransport()
    values = fixture(tmp_path, transport)
    receipt = execute(values["broker"], paper="p1", run_id="run-current")
    result = _settle(values, receipt, _evidence(receipt, tmp_path / "evidence.json"))
    path = Path(result["settlement_receipt"])
    event = json.loads(path.read_text())
    event["actual_cost_usd"] = "0.01"
    path.chmod(0o644)
    path.write_text(json.dumps(event))
    with pytest.raises(ValueError, match="http rejection settlement event changed"):
        _reopen(values, RejectingTransport())
    assert (
        values["ledger"].parent / ".shared-ledger.json.integrity-halt.json"
    ).is_file()
