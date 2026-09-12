from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.model_broker import SharedGeminiBroker, broker_request_key
from arctic_qa.providers import GeminiProvider, ProviderError, make_provider
from arctic_qa.util import canonical_json, sha256_bytes, sha256_file


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


def reviewed_transition(
    tmp_path: Path,
    values: dict,
    active_config: Path,
    **changes: object,
) -> Path:
    identity = tmp_path / ".shared-ledger.json.identity.json"
    review = tmp_path / "review.md"
    review.write_text("Exact integrated revision passed.\n", encoding="utf-8")
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate["review_record"] = str(review)
    gate["review_record_sha256"] = sha256_file(review)
    write_json(values["gate"], gate)
    authorization = {
        "schema": "shared-paid-call-config-transition-v1",
        "ledger_file": str(values["ledger"].resolve()),
        "from_price_config_sha256": json.loads(identity.read_text(encoding="utf-8"))[
            "price_config_sha256"
        ],
        "to_price_config_sha256": sha256_file(active_config),
        "expected_ledger_sha256": sha256_file(values["ledger"]),
        "expected_identity_sha256": sha256_file(identity),
        "execution_gate_sha256": sha256_file(values["gate"]),
        "integrated_code_commit": "fixture-commit",
        "review_record": str(review),
        "review_record_sha256": sha256_file(review),
        "reason": "Use the reviewed low-thinking request configuration.",
        "authorized_at_utc": "2026-09-12T09:00:00Z",
        **changes,
    }
    transition = tmp_path / "private" / "config-transition.json"
    write_json(transition, authorization)
    return transition


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


def test_reviewed_config_transition_preserves_spend_and_immutable_custody(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    ledger = values["ledger"]
    identity = tmp_path / ".shared-ledger.json.identity.json"
    original_ledger = ledger.read_bytes()
    original_identity = identity.read_bytes()
    original_receipts = {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    }

    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)

    resumed = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=ledger,
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=transition,
    )

    status = resumed.status()
    assert status["spent_usd"] == "0.000132"
    assert status["generation_submissions"] == 1
    assert status["initial_price_config_sha256"] != status["price_config_sha256"]
    assert ledger.read_bytes() == original_ledger
    assert identity.read_bytes() == original_identity
    for name, content in original_receipts.items():
        assert (tmp_path / "receipts" / name).read_bytes() == content
    transition_events = list((tmp_path / "receipts").glob("config-transition-*.json"))
    assert len(transition_events) == 1

    restarted = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=ledger,
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
    )
    assert restarted.status()["config_transition_sha256"] == sha256_file(
        transition_events[0]
    )
    new_receipt = execute(restarted, paper="p2")
    assert new_receipt["price_config_sha256"] == sha256_file(active_config)
    assert new_receipt["config_transition_sha256"] == sha256_file(transition_events[0])

    replay_transport = Transport()
    replayed = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=ledger,
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=replay_transport,
    )
    replay_status = replayed.status()
    assert replay_status["generation_submissions"] == 2
    with pytest.raises(ValueError, match="request key already exists"):
        execute(replayed, paper="p2")
    assert replay_transport.methods == []

    changed_event = json.loads(transition_events[0].read_text(encoding="utf-8"))
    changed_event["authorization"]["unreviewed_field"] = True
    changed_event["transition_authorization_sha256"] = sha256_bytes(
        canonical_json(changed_event["authorization"]).encode()
    )
    changed_path = transition_events[0].with_name(
        f"config-transition-{changed_event['transition_authorization_sha256']}.json"
    )
    transition_events[0].rename(changed_path)
    write_json(changed_path, changed_event)
    with pytest.raises(ValueError, match="applied price configuration transition"):
        restarted.status()
    assert (tmp_path / ".shared-ledger.json.integrity-halt.json").is_file()


def test_changed_config_without_reviewed_transition_fails_without_state_change(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    ledger = values["ledger"]
    identity = tmp_path / ".shared-ledger.json.identity.json"
    original_ledger = ledger.read_bytes()
    original_identity = identity.read_bytes()
    original_receipts = {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    }
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )

    with pytest.raises(ValueError, match="requires a reviewed transition"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=ledger,
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
        )

    assert ledger.read_bytes() == original_ledger
    assert identity.read_bytes() == original_identity
    assert {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    } == original_receipts


def test_transition_with_wrong_ledger_hash_creates_no_event(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(
        tmp_path,
        values,
        active_config,
        expected_ledger_sha256="0" * 64,
    )

    with pytest.raises(ValueError, match="transition ledger hash changed"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
            config_transition_file=transition,
        )

    assert list((tmp_path / "receipts").glob("config-transition-*.json")) == []


def test_forged_transition_event_cannot_authorize_restart_or_submit(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    identity = tmp_path / ".shared-ledger.json.identity.json"
    initial_config_hash = json.loads(identity.read_text(encoding="utf-8"))[
        "price_config_sha256"
    ]
    authorization = {
        "schema": "shared-paid-call-config-transition-v1",
        "ledger_file": str(values["ledger"].resolve()),
        "from_price_config_sha256": initial_config_hash,
        "to_price_config_sha256": sha256_file(active_config),
        "expected_ledger_sha256": "0" * 64,
        "expected_identity_sha256": sha256_file(identity),
        "execution_gate_sha256": "1" * 64,
        "integrated_code_commit": "not-reviewed",
        "review_record": str(tmp_path / "review-does-not-exist.md"),
        "review_record_sha256": "2" * 64,
        "reason": "Forged direct receipt event.",
        "authorized_at_utc": "2026-09-12T09:00:00Z",
    }
    authorization_hash = sha256_bytes(canonical_json(authorization).encode())
    event = tmp_path / "receipts" / f"config-transition-{authorization_hash}.json"
    write_json(
        event,
        {
            "schema": "shared-paid-call-config-transition-event-v1",
            "authorization": authorization,
            "transition_authorization_sha256": authorization_hash,
            "applied_at_utc": "2026-09-12T09:01:00Z",
        },
    )
    forged_transport = Transport()

    transition_error = None
    try:
        resumed = SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=forged_transport,
        )
        execute(resumed, paper="p2")
    except ValueError as error:
        transition_error = error

    assert forged_transport.methods == []
    assert transition_error is not None
    assert "transition" in str(transition_error)
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["generation_submissions"] == 1
    assert len(ledger["requests"]) == 1


def test_unreviewed_transition_event_stops_before_provider_submission(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    identity = tmp_path / ".shared-ledger.json.identity.json"
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    authorization = {
        "schema": "shared-paid-call-config-transition-v1",
        "ledger_file": str(values["ledger"].resolve()),
        "from_price_config_sha256": json.loads(identity.read_text(encoding="utf-8"))[
            "price_config_sha256"
        ],
        "to_price_config_sha256": sha256_file(active_config),
        "expected_ledger_sha256": sha256_file(values["ledger"]),
        "expected_identity_sha256": sha256_file(identity),
        "execution_gate_sha256": sha256_file(values["gate"]),
        "integrated_code_commit": gate["integrated_code_commit"],
        "review_record": gate["review_record"],
        "review_record_sha256": None,
        "reason": "Unreviewed direct receipt event.",
        "authorized_at_utc": "2026-09-12T09:00:00Z",
    }
    authorization_hash = sha256_bytes(canonical_json(authorization).encode())
    write_json(
        tmp_path / "receipts" / f"config-transition-{authorization_hash}.json",
        {
            "schema": "shared-paid-call-config-transition-event-v1",
            "authorization": authorization,
            "transition_authorization_sha256": authorization_hash,
            "applied_at_utc": "2026-09-12T09:01:00Z",
        },
    )
    unreviewed_transport = Transport()

    with pytest.raises(ValueError, match="transition review is invalid"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=unreviewed_transport,
        )

    assert unreviewed_transport.methods == []


def test_forged_transition_event_cannot_replace_request_anchor(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)
    transitioned = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=transition,
    )
    assert execute(transitioned, paper="p2")["state"] == "completed"

    original_event = next((tmp_path / "receipts").glob("config-transition-*.json"))
    forged_event = json.loads(original_event.read_text(encoding="utf-8"))
    forged_event["authorization"]["expected_ledger_sha256"] = "0" * 64
    forged_event["authorization"]["review_record"] = str(
        tmp_path / "review-does-not-exist.md"
    )
    forged_hash = sha256_bytes(canonical_json(forged_event["authorization"]).encode())
    forged_event["transition_authorization_sha256"] = forged_hash
    forged_path = original_event.with_name(f"config-transition-{forged_hash}.json")
    original_event.rename(forged_path)
    write_json(forged_path, forged_event)
    forged_transport = Transport()

    transition_error = None
    try:
        restarted = SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=forged_transport,
        )
        execute(restarted, paper="p3")
    except ValueError as error:
        transition_error = error

    assert forged_transport.methods == []
    assert transition_error is not None
    assert "transition" in str(transition_error)


def test_extra_transition_event_stops_active_broker_before_submission(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)
    active_transport = Transport()
    transitioned = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=active_transport,
        config_transition_file=transition,
    )
    original_event = next((tmp_path / "receipts").glob("config-transition-*.json"))
    extra_event = json.loads(original_event.read_text(encoding="utf-8"))
    extra_event["authorization"]["reason"] = "Crafted second transition event."
    extra_hash = sha256_bytes(canonical_json(extra_event["authorization"]).encode())
    extra_event["transition_authorization_sha256"] = extra_hash
    write_json(
        original_event.with_name(f"config-transition-{extra_hash}.json"),
        extra_event,
    )

    with pytest.raises(ValueError, match="multiple applied price configuration"):
        execute(transitioned, paper="p2")

    assert active_transport.methods == []


def test_transition_review_is_rechecked_before_first_submission(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)
    active_transport = Transport()
    transitioned = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=active_transport,
        config_transition_file=transition,
    )
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    Path(gate["review_record"]).write_text(
        "The review record changed.\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="transition review is invalid"):
        execute(transitioned, paper="p2")

    assert active_transport.methods == []


def test_transition_does_not_apply_to_an_unsettled_ledger(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport("generate"))
    assert execute(values["broker"])["state"] == "ambiguous_charge"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)

    with pytest.raises(ValueError, match="requires a settled ledger"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
            config_transition_file=transition,
        )

    assert list((tmp_path / "receipts").glob("config-transition-*.json")) == []
