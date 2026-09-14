from __future__ import annotations

import json
import urllib.error
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.model_broker import SharedGeminiBroker, broker_request_key
from arctic_qa.util import canonical_json, sha256_file


ROOT = Path(__file__).parents[1]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def request_payload() -> dict:
    return {
        "systemInstruction": {"parts": [{"text": "Return JSON."}]},
        "contents": [{"role": "user", "parts": [{"text": "Paper text."}]}],
        "generationConfig": {
            "candidateCount": 1,
            "responseMimeType": "application/json",
            "responseJsonSchema": {"type": "object"},
            "maxOutputTokens": 1000,
            "thinkingConfig": {"thinkingLevel": "medium"},
        },
        "store": False,
    }


class Http500ThenSuccess:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.generation_calls = 0

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 100}
        self.generation_calls += 1
        if self.generation_calls == 1:
            raise urllib.error.HTTPError(
                "https://fake.invalid", 500, "upstream failure", {}, None
            )
        return {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 115,
            },
        }


def fixture(tmp_path: Path, transport: object) -> dict[str, object]:
    review = tmp_path / "review.md"
    review.write_text("The bounded continuation passed independent review.\n")
    gate = tmp_path / "gate.json"
    write_json(
        gate,
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": True,
            "allowed_phase": "live_test",
            "integrated_code_commit": "fixture-commit",
            "independent_review_verdict": "pass",
            "review_record": str(review),
            "review_record_sha256": sha256_file(review),
            "authorized_new_run_id": "run-current",
        },
    )
    credential = tmp_path / "private" / "gemini.key"
    credential.parent.mkdir(mode=0o700, exist_ok=True)
    credential.write_text("unused-test-key\n", encoding="utf-8")
    credential.chmod(0o600)
    broker = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=gate,
        ledger_file=tmp_path / "shared-ledger.json",
        receipts_dir=tmp_path / "receipts",
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
    )
    return {
        "broker": broker,
        "gate": gate,
        "ledger": tmp_path / "shared-ledger.json",
        "receipts": tmp_path / "receipts",
        "review": review,
    }


def execute(broker: SharedGeminiBroker, *, paper: str, run_id: str) -> dict:
    payload = request_payload()
    family = f"family-{paper}"
    source = f"source-{paper}"
    key = broker_request_key(
        model=broker.config["model"],
        run_id=run_id,
        phase="live_test",
        stage="question_generation",
        paper_id=paper,
        family_id=family,
        source_version_id=source,
        payload=payload,
    )
    return broker.execute(
        phase="live_test",
        run_id=run_id,
        stage="question_generation",
        paper_id=paper,
        family_id=family,
        source_version_id=source,
        request_key=key,
        payload=payload,
    )


def evidence_for(receipt: dict, path: Path) -> None:
    write_json(
        path,
        {
            "schema": "shared-paid-call-ambiguous-continuation-evidence-v1",
            "request_key": receipt["request_key"],
            "error_class": "known_http_response_unknown_charge",
            "http_status": 500,
            "live_call_made": True,
            "received_receipt_absent": True,
            "actual_cost_known": False,
            "replay_prohibited": True,
            "affected_family_id": receipt["family_id"],
            "authorized_run_id": receipt["run_id"],
        },
    )


def authorize(values: dict[str, object], receipt: dict) -> dict:
    evidence = Path(values["ledger"].parent) / "evidence.json"
    evidence_for(receipt, evidence)
    broker = values["broker"]
    return broker.authorize_ambiguous_continuation(
        request_key=receipt["request_key"],
        expected_ledger_sha256=sha256_file(values["ledger"]),
        review_file=values["review"],
        evidence_file=evidence,
        authorized_run_id="run-current",
        operator_id="test-operator",
    )


def test_unknown_charge_continuation_retains_cap_and_never_replays(tmp_path: Path):
    transport = Http500ThenSuccess()
    values = fixture(tmp_path, transport)
    broker = values["broker"]
    first = execute(broker, paper="affected", run_id="run-current")
    assert first["state"] == "ambiguous_charge"
    before = json.loads(values["ledger"].read_text(encoding="utf-8"))
    result = authorize(values, first)
    assert result["applied"] is True
    after = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert after["halted"] is False
    assert after["requests"][first["request_key"]]["state"] == "ambiguous_charge"
    assert after["ambiguous_reserved_usd"] == first["reserved_usd"]
    assert after["spent_usd"] == before["spent_usd"]
    assert broker.operational_unresolved_family_ids() == {
        first["family_id"]: first["request_key"]
    }
    with pytest.raises(ValueError, match="request key already exists"):
        execute(broker, paper="affected", run_id="run-current")
    broker.policy["away_session_total_ceiling_usd"] = first["reserved_usd"]
    near_cap = execute(broker, paper="near-cap", run_id="run-current")
    assert near_cap["state"] == "not_submitted"
    assert "authorized away cap" in near_cap["reason"]
    broker.policy["away_session_total_ceiling_usd"] = "25.00"
    assert execute(broker, paper="unrelated", run_id="run-current")["state"] == "completed"
    assert transport.methods == [
        "countTokens",
        "generateContent",
        "countTokens",
        "countTokens",
        "generateContent",
    ]
    repeated = authorize(values, first)
    assert repeated["applied"] is False
    assert repeated["continuation_receipt_sha256"] == result["continuation_receipt_sha256"]
    assert json.loads(values["ledger"].read_text(encoding="utf-8"))["ambiguous_reserved_usd"] == first[
        "reserved_usd"
    ]


def test_continuation_requires_exact_http500_evidence_and_run(tmp_path: Path):
    values = fixture(tmp_path, Http500ThenSuccess())
    first = execute(values["broker"], paper="affected", run_id="run-current")
    evidence = tmp_path / "evidence.json"
    evidence_for(first, evidence)
    with pytest.raises(ValueError, match="authorized continuation run"):
        values["broker"].authorize_ambiguous_continuation(
            request_key=first["request_key"],
            expected_ledger_sha256=sha256_file(values["ledger"]),
            review_file=values["review"],
            evidence_file=evidence,
            authorized_run_id="another-run",
            operator_id="test-operator",
        )
    assert json.loads(values["ledger"].read_text(encoding="utf-8"))["halted"] is True
    assert not list(values["receipts"].glob("ambiguous-continuation-*.json"))


def test_integrity_failure_still_halts_after_continuation(tmp_path: Path):
    values = fixture(tmp_path, Http500ThenSuccess())
    first = execute(values["broker"], paper="affected", run_id="run-current")
    authorize(values, first)
    event_path = next(values["receipts"].glob("ambiguous-continuation-*.json"))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event["scope"] = "all_families"
    event_path.write_text(canonical_json(event), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        values["broker"].status()
    assert (values["ledger"].parent / ".shared-ledger.json.integrity-halt.json").is_file()
