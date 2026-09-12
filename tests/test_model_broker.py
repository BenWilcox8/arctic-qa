from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.model_broker import SharedGeminiBroker, broker_request_key
from arctic_qa.providers import GeminiProvider, ProviderError, make_provider


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


def test_legacy_gemini_provider_cannot_bypass_shared_broker() -> None:
    with pytest.raises(ValueError, match="shared streaming broker"):
        make_provider("gemini", "gemini-3.8-flash", None)
    with pytest.raises(ProviderError, match="shared streaming broker"):
        GeminiProvider("gemini-3.8-flash").invoke("verifier", "system", "prompt", {}, 1)


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


def execute(
    broker: SharedGeminiBroker,
    *,
    stage: str = "eligibility",
    paper="p1",
    family=None,
    source=None,
    phase="live_test",
    run_id="run-1",
):
    body = payload()
    family = family or f"family-{paper}"
    source = source or f"source-{paper}"
    key = broker_request_key(
        model="gemini-3.8-flash",
        run_id=run_id,
        stage=stage,
        paper_id=paper,
        family_id=family,
        source_version_id=source,
        payload=body,
    )
    return broker.execute(
        phase=phase,
        run_id=run_id,
        stage=stage,
        paper_id=paper,
        family_id=family,
        source_version_id=source,
        request_key=key,
        payload=body,
    )


def test_disabled_gate_prevents_any_transport_call(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, enabled=False, transport=transport)["broker"]
    with pytest.raises(ValueError, match="disabled"):
        execute(broker)
    assert transport.methods == []


def test_missing_credential_does_not_create_a_request_state(tmp_path: Path):
    values = fixture(tmp_path, transport=None)
    (tmp_path / "private" / "gemini.key").unlink()
    with pytest.raises(ValueError, match="credential"):
        execute(values["broker"])
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["requests"] == {}
    assert ledger["count_requests"] == 0


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
    assert status["papers"]["family-p2"]["output_tokens"] == 10
    assert Decimal(status["spent_usd"]) > 0


def test_inconsistent_ledger_totals_fail_closed_on_restart(tmp_path: Path):
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert execute(values["broker"])["state"] == "completed"
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    ledger["spent_usd"] = "0"
    write_json(values["ledger"], ledger)
    with pytest.raises(ValueError, match="spent_usd total is inconsistent"):
        fixture(tmp_path, transport=transport)
    assert (tmp_path / ".shared-ledger.json.integrity-halt.json").is_file()


def test_consistently_lowered_ledger_spend_conflicts_with_immutable_receipt(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    request = next(iter(ledger["requests"].values()))
    request["actual_cost_usd"] = "0"
    ledger["spent_usd"] = "0"
    ledger["stages"]["eligibility"]["spent_usd"] = "0"
    ledger["papers"]["family-p1"]["spent_usd"] = "0"
    ledger["live_test_papers"]["family-p1"]["spent_usd"] = "0"
    write_json(values["ledger"], ledger)
    with pytest.raises(ValueError, match="immutable final event changed cost"):
        fixture(tmp_path, transport=Transport())


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
            source_version_id="source-p1",
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
            source_version_id="source-p1",
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
    with pytest.raises(ValueError, match="another paper family"):
        resumed.record_accepted(family_id="family-2", item_id="item-1")


def test_new_run_id_cannot_replay_the_same_request(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    assert execute(broker)["state"] == "completed"
    with pytest.raises(ValueError, match="already exists"):
        execute(broker, run_id="renamed-run")
    assert transport.methods == ["countTokens", "generateContent"]


def test_paper_alias_cannot_bypass_family_binding(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    assert (
        execute(broker, paper="canonical", family="family-1", source="source-v1")[
            "state"
        ]
        == "completed"
    )
    with pytest.raises(ValueError, match="already bound"):
        execute(broker, paper="alias", family="family-1", source="source-v1")
    assert transport.methods == ["countTokens", "generateContent"]


def test_source_version_cannot_move_to_an_alias_family(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    assert (
        execute(broker, paper="canonical", family="family-1", source="source-v1")[
            "state"
        ]
        == "completed"
    )
    with pytest.raises(ValueError, match="source version is already bound"):
        execute(broker, paper="canonical", family="family-alias", source="source-v1")
    assert transport.methods == ["countTokens", "generateContent"]


def test_paper_cannot_move_to_a_new_family_and_source_version(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    for stage in (
        "eligibility",
        "finding_answer_extraction",
        "question_generation",
        "blinded_reconstruction",
    ):
        assert (
            execute(
                broker,
                stage=stage,
                paper="canonical",
                family="family-1",
                source="source-v1",
            )["state"]
            == "completed"
        )
    with pytest.raises(ValueError, match="paper ID is already bound"):
        execute(
            broker,
            stage="answer_verification",
            paper="canonical",
            family="family-2",
            source="source-v2",
        )
    assert transport.methods == ["countTokens", "generateContent"] * 4


def test_received_response_is_recovered_after_final_receipt_write_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import arctic_qa.model_broker as broker_module

    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    original = broker_module.atomic_json

    def crash_final(path: Path, value: object, *, immutable: bool = False):
        if (
            path.parent.name == "receipts"
            and path.name.endswith(".json")
            and not (
                path.name.endswith(".submitted.json")
                or path.name.endswith(".received.json")
            )
        ):
            raise OSError("simulated final receipt crash")
        return original(path, value, immutable=immutable)

    monkeypatch.setattr(broker_module, "atomic_json", crash_final)
    with pytest.raises(OSError, match="simulated final receipt crash"):
        execute(values["broker"])
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    request_key = next(iter(ledger["requests"]))
    assert ledger["requests"][request_key]["state"] == "submitted"
    assert (tmp_path / "receipts" / f"{request_key}.received.json").is_file()

    monkeypatch.setattr(broker_module, "atomic_json", original)
    resumed = fixture(tmp_path, transport=transport)["broker"]
    with pytest.raises(ValueError, match="already exists"):
        execute(resumed)
    recovered = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert recovered["requests"][request_key]["state"] == "completed"
    final = json.loads(
        (tmp_path / "receipts" / f"{request_key}.json").read_text(encoding="utf-8")
    )
    assert final["response"]["usageMetadata"]["totalTokenCount"] == 115
    assert transport.methods == ["countTokens", "generateContent"]


def test_deleted_request_cannot_orphan_immutable_spend_events(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    ledger["requests"] = {}
    ledger["family_bindings"] = {}
    ledger["paper_bindings"] = {}
    ledger["stages"] = {}
    ledger["papers"] = {}
    ledger["live_test_papers"] = {}
    ledger["spent_usd"] = "0"
    ledger["generation_submissions"] = 0
    ledger["count_requests"] = 0
    ledger["recent_submission_times_utc"] = []
    write_json(values["ledger"], ledger)
    with pytest.raises(ValueError, match="immutable paid-call event"):
        fixture(tmp_path, transport=Transport())


def test_integrity_halt_republishes_honest_status(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    broker = values["broker"]
    status_path = tmp_path / "shared-ledger.status.json"
    observed: list[dict] = []
    broker.set_status_observer(
        lambda path: observed.append(json.loads(path.read_text(encoding="utf-8")))
    )
    before = json.loads(status_path.read_text(encoding="utf-8"))
    write_json(tmp_path / "receipts" / f"{'f' * 64}.json", {})

    with pytest.raises(ValueError, match="immutable paid-call event"):
        broker.status()

    after = json.loads(status_path.read_text(encoding="utf-8"))
    assert after != before
    assert after["halted"] is True
    assert after["integrity_valid"] is False
    assert "immutable paid-call event" in after["halt_reason"]
    assert observed[-1] == after


def test_production_spend_does_not_renew_or_inflate_live_test(tmp_path: Path):
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    assert execute(values["broker"])["state"] == "completed"
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate["allowed_phase"] = "away_production"
    write_json(values["gate"], gate)
    assert (
        execute(
            values["broker"],
            phase="away_production",
            stage="question_generation",
        )["state"]
        == "completed"
    )
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["live_test_papers"]["family-p1"]["spent_usd"] == "0.000132"
    assert ledger["generation_submissions"] == 2


def test_initialized_ledger_cannot_silently_reset(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    values["ledger"].unlink()
    with pytest.raises(ValueError, match="absent after initialization"):
        fixture(tmp_path, transport=Transport())
