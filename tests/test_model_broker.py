from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.model_broker import SharedGeminiBroker, broker_request_key


ROOT = Path(__file__).parents[1]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def payload() -> dict:
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


class Transport:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.methods: list[str] = []

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            if self.failure == "count":
                raise TimeoutError("count unavailable")
            return {"totalTokens": 100}
        if self.failure == "generate":
            raise TimeoutError("outcome unknown")
        return {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 115,
            },
        }


def fixture(tmp_path: Path, *, enabled: bool = True, transport=None) -> dict:
    gate = tmp_path / "gate.json"
    write_json(
        gate,
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": enabled,
            "allowed_phase": "live_test" if enabled else None,
            "integrated_code_commit": "fixture-commit" if enabled else None,
            "independent_review_verdict": "pass" if enabled else "pending",
            "review_record": "fixture-review" if enabled else None,
        },
    )
    credential = tmp_path / "private" / "gemini.key"
    credential.parent.mkdir(mode=0o700, exist_ok=True)
    credential.write_text("unused-test-key", encoding="utf-8")
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
    return {"broker": broker, "gate": gate, "ledger": tmp_path / "shared-ledger.json"}


def execute(broker: SharedGeminiBroker, *, stage: str = "eligibility", paper="p1"):
    body = payload()
    key = broker_request_key(
        model="gemini-3.8-flash",
        run_id="run-1",
        stage=stage,
        paper_id=paper,
        family_id=f"family-{paper}",
        payload=body,
    )
    return broker.execute(
        phase="live_test",
        run_id="run-1",
        stage=stage,
        paper_id=paper,
        family_id=f"family-{paper}",
        request_key=key,
        payload=body,
    )


def test_disabled_gate_prevents_any_transport_call(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, enabled=False, transport=transport)["broker"]
    with pytest.raises(ValueError, match="disabled"):
        execute(broker)
    assert transport.methods == []


def test_all_stages_share_one_durable_ledger(tmp_path: Path):
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    assert execute(values["broker"])["state"] == "completed"
    assert (
        execute(values["broker"], stage="question_generation", paper="p2")["state"]
        == "completed"
    )
    resumed = fixture(tmp_path, transport=transport)["broker"]
    status = resumed.status()
    assert status["generation_submissions"] == 2
    assert status["stages"]["eligibility"]["input_tokens"] == 100
    assert status["stages"]["question_generation"]["thinking_tokens"] == 5
    assert status["papers"]["p2"]["output_tokens"] == 10
    assert Decimal(status["spent_usd"]) > 0


def test_live_test_cap_cannot_reset_with_a_new_broker(tmp_path: Path):
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    ledger["spent_usd"] = "9.999900"
    ledger["live_test_papers"]["old"] = {
        "submissions": 1,
        "reserved_usd": "0",
        "spent_usd": "9.999900",
        "ambiguous_usd": "0",
    }
    write_json(values["ledger"], ledger)
    resumed = fixture(tmp_path, transport=transport)["broker"]
    receipt = execute(resumed)
    assert receipt["state"] == "not_submitted"
    assert "USD 10" in receipt["reason"]
    assert transport.methods == ["countTokens"]
    assert resumed.status()["generation_submissions"] == 0


def test_ambiguous_generation_halts_without_replay(tmp_path: Path):
    transport = Transport("generate")
    broker = fixture(tmp_path, transport=transport)["broker"]
    receipt = execute(broker)
    assert receipt["state"] == "ambiguous_charge"
    assert broker.status()["halted"] is True
    with pytest.raises(ValueError, match="halted"):
        execute(broker, paper="p2")
    assert transport.methods == ["countTokens", "generateContent"]


def test_count_error_is_durable_and_never_generates(tmp_path: Path):
    transport = Transport("count")
    values = fixture(tmp_path, transport=transport)
    receipt = execute(values["broker"])
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert receipt["state"] == "count_error"
    assert ledger["requests"][receipt["request_key"]]["state"] == "count_error"
    assert values["broker"].status()["halted"] is True
    assert transport.methods == ["countTokens"]


def test_request_key_and_payload_features_fail_closed(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    body = payload()
    with pytest.raises(ValueError, match="bind"):
        broker.execute(
            phase="live_test",
            run_id="run-1",
            stage="eligibility",
            paper_id="p1",
            family_id="family-p1",
            request_key="0" * 64,
            payload=body,
        )
    body["tools"] = []
    with pytest.raises(ValueError, match="top-level"):
        broker.execute(
            phase="live_test",
            run_id="run-1",
            stage="eligibility",
            paper_id="p1",
            family_id="family-p1",
            request_key="0" * 64,
            payload=body,
        )
    assert transport.methods == []


def test_one_accepted_item_per_family_survives_restart(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    values["broker"].record_accepted(family_id="family-1", item_id="item-1")
    resumed = fixture(tmp_path, transport=Transport())["broker"]
    assert resumed.status()["accepted_question_count"] == 1
    with pytest.raises(ValueError, match="already"):
        resumed.record_accepted(family_id="family-1", item_id="item-2")


def test_initialized_ledger_cannot_silently_reset(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    values["ledger"].unlink()
    with pytest.raises(ValueError, match="absent after initialization"):
        fixture(tmp_path, transport=Transport())
