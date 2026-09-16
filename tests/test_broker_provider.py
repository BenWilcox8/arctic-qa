from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.broker_provider import BrokerProvider, _request_payload
from arctic_qa.db import Database
from arctic_qa.errors import PaperCostCapError, ProviderError
from arctic_qa.model_broker import (
    PAPER_COST_CAP_REASON,
    SharedGeminiBroker,
    broker_request_key,
)
from arctic_qa.providers import call_provider, provider_prompt_hash
from arctic_qa.util import canonical_json, stable_id


ROOT = Path(__file__).parents[1]
SOURCE_VERSION = "a" * 64


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class Transport:
    def __init__(self) -> None:
        self.methods: list[str] = []

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 100}
        return {
            "responseId": "broker-response-1",
            "modelVersion": "gemini-3.8-flash",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [{"text": json.dumps({"question": "What changed?"})}]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 115,
            },
        }


class MalformedTransport(Transport):
    def post(self, model: str, method: str, body: dict) -> dict:
        response = super().post(model, method, body)
        if method == "generateContent":
            response["candidates"][0]["content"]["parts"] = [{"text": "{"}]
        return response


class JudgeTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.models: list[str] = []
        self.bodies: list[dict] = []

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        self.models.append(model)
        self.bodies.append(body)
        if method == "countTokens":
            return {"totalTokens": 100}
        return {
            "responseId": "judge-response-1",
            "modelVersion": "gemini-3.1-flash-lite",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "yes"}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 1,
                "thoughtsTokenCount": 0,
                "totalTokenCount": 101,
            },
        }


def broker_fixture(
    tmp_path: Path, transport: Transport, *, phase: str = "live_test"
) -> SharedGeminiBroker:
    gate = tmp_path / "gate.json"
    write_json(
        gate,
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": True,
            "allowed_phase": phase,
            "integrated_code_commit": "test-only-commit",
            "independent_review_verdict": "pass",
            "review_record": "test-only-review",
        },
    )
    credential = tmp_path / "private" / "gemini.key"
    credential.parent.mkdir(mode=0o700)
    credential.write_text("test-only-unused-key", encoding="utf-8")
    credential.chmod(0o600)
    return SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=gate,
        ledger_file=tmp_path / "shared-ledger.json",
        receipts_dir=tmp_path / "receipts",
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
    )


def test_broker_provider_binds_role_and_reuses_completed_receipt(
    tmp_path: Path,
) -> None:
    transport = Transport()
    broker = broker_fixture(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
    )
    parameters = {
        "temperature": 0,
        "max_tokens": 1000,
        "json_schema": {
            "type": "object",
            "required": ["question"],
            "properties": {"question": {"type": "string"}},
            "additionalProperties": False,
        },
    }

    first = provider.invoke(
        "question_writer", "System", "Prompt", parameters, timeout=30
    )
    second = provider.invoke(
        "question_writer", "System", "Prompt", parameters, timeout=30
    )
    renamed = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="renamed-live-test-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
    )
    third = renamed.invoke(
        "question_writer", "System", "Prompt", parameters, timeout=30
    )

    assert first.payload == {"question": "What changed?"}
    assert second == first
    assert third == first
    assert transport.methods == ["countTokens", "generateContent"]
    status = broker.status()
    assert status["generation_submissions"] == 1
    assert status["stages"]["question_generation"]["submissions"] == 1


def test_answer_judge_uses_the_pro_judge_and_reuses_immutable_receipt(
    tmp_path: Path,
) -> None:
    transport = JudgeTransport()
    broker = broker_fixture(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="judge-live-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
    )
    schema = {"type": "string", "enum": ["yes", "no"]}
    parameters = {
        "temperature": 0,
        "max_tokens": 128,
        "response_mime_type": "text/x.enum",
        "json_schema": schema,
    }

    first = provider.invoke("answer_judge", "System", "DATA\n{}", parameters, 30)
    second = provider.invoke("answer_judge", "System", "DATA\n{}", parameters, 30)
    reference = provider.receipt_reference(
        "answer_judge", "System", "DATA\n{}", parameters
    )

    assert first.payload == "yes"
    assert second == first
    assert transport.methods == ["countTokens", "generateContent"]
    # Chapter 3 (yield audit 4.5 R5): the fallback judge is the Pro judge.
    assert transport.models == ["gemini-3.1-pro-preview"] * 2
    generation = transport.bodies[1]["generationConfig"]
    assert generation["responseMimeType"] == "text/x.enum"
    assert generation["responseJsonSchema"] == schema
    assert generation["maxOutputTokens"] == 128
    assert generation["thinkingConfig"] == {"thinkingLevel": "low"}
    receipt = json.loads(Path(reference["receipt_file"]).read_text(encoding="utf-8"))
    assert receipt["model"] == "gemini-3.1-pro-preview"
    assert receipt["stage"] == "answer_agreement"
    # Pro pricing: USD 2.00 in and USD 12.00 out per million tokens.
    assert receipt["actual_cost_usd"] == "0.000212"
    assert broker.status()["stages"]["answer_agreement"]["submissions"] == 1


def test_new_away_invocation_does_not_reuse_same_campaign_call_journal(
    tmp_path: Path,
) -> None:
    transport = Transport()
    broker = broker_fixture(tmp_path, transport, phase="away_production")
    database = Database(tmp_path / "state.sqlite3")
    database.migrate(tmp_path / "backups")
    schema = {
        "type": "object",
        "required": ["question"],
        "properties": {"question": {"type": "string"}},
        "additionalProperties": False,
    }
    parameters = {
        "temperature": 0,
        "max_tokens": 1000,
        "json_schema": schema,
    }

    for invocation_run_id in ("historical-segment", "successor-segment"):
        provider = BrokerProvider(
            broker=broker,
            phase="away_production",
            invocation_run_id=invocation_run_id,
        ).bind(
            paper_id="paper-1",
            family_id="family-1",
            source_version_id=SOURCE_VERSION,
        )
        for _ in range(2):
            call_provider(
                database,
                provider,
                run_id="same-campaign",
                entity_id="same-unit",
                role="question_writer",
                system="System",
                prompt="Prompt",
                prompt_version="test-v1",
                parameters=parameters,
                response_schema=schema,
                reservation=Decimal("999999"),
                timeout=30,
                retries=0,
                rate_limit_seconds=0,
            )

    assert transport.methods == [
        "countTokens",
        "generateContent",
        "countTokens",
        "generateContent",
    ]
    assert database.one("SELECT COUNT(*) AS count FROM calls")["count"] == 2
    assert broker.status()["generation_submissions"] == 2


def test_call_journal_does_not_create_a_second_broker_budget(tmp_path: Path) -> None:
    transport = Transport()
    broker = broker_fixture(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
    )
    schema = {
        "type": "object",
        "required": ["question"],
        "properties": {"question": {"type": "string"}},
        "additionalProperties": False,
    }
    database = Database(tmp_path / "state.sqlite3")
    database.migrate(tmp_path / "backups")

    result = call_provider(
        database,
        provider,
        run_id="campaign-r1",
        entity_id="unit-1",
        role="question_writer",
        system="System",
        prompt="Prompt",
        prompt_version="test-v1",
        parameters={
            "temperature": 0,
            "max_tokens": 1000,
            "json_schema": schema,
        },
        response_schema=schema,
        reservation=Decimal("999999"),
        timeout=30,
        retries=0,
        rate_limit_seconds=0,
    )

    assert result.payload == {"question": "What changed?"}
    assert database.one("SELECT * FROM budgets WHERE run_id='campaign-r1'") is None
    call = database.one("SELECT * FROM calls WHERE run_id='campaign-r1'")
    assert call["status"] == "completed"
    assert call["provider"] == "gemini"
    assert call["actual_cost_usd"] == "0.000132"

    second_provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    ).bind(
        paper_id="paper-2",
        family_id="family-2",
        source_version_id="b" * 64,
    )
    call_provider(
        database,
        second_provider,
        run_id="campaign-r1",
        entity_id="unit-1",
        role="question_writer",
        system="System",
        prompt="Prompt",
        prompt_version="test-v1",
        parameters={
            "temperature": 0,
            "max_tokens": 1000,
            "json_schema": schema,
        },
        response_schema=schema,
        reservation=Decimal("999999"),
        timeout=30,
        retries=0,
        rate_limit_seconds=0,
    )
    assert database.one("SELECT COUNT(*) AS count FROM calls")["count"] == 2
    assert broker.status()["generation_submissions"] == 2

    changed_source = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id="c" * 64,
    )
    with pytest.raises(ValueError, match="paper family is already bound"):
        call_provider(
            database,
            changed_source,
            run_id="campaign-r1",
            entity_id="unit-1",
            role="question_writer",
            system="System",
            prompt="Prompt",
            prompt_version="test-v1",
            parameters={
                "temperature": 0,
                "max_tokens": 1000,
                "json_schema": schema,
            },
            response_schema=schema,
            reservation=Decimal("999999"),
            timeout=30,
            retries=0,
            rate_limit_seconds=0,
        )
    assert database.one("SELECT COUNT(*) AS count FROM calls")["count"] == 3
    assert broker.status()["generation_submissions"] == 2


def test_malformed_completed_response_is_not_retried_on_resume(tmp_path: Path) -> None:
    transport = MalformedTransport()
    broker = broker_fixture(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
    )
    database = Database(tmp_path / "state.sqlite3")
    database.migrate(tmp_path / "backups")
    schema = {
        "type": "object",
        "required": ["question"],
        "properties": {"question": {"type": "string"}},
        "additionalProperties": False,
    }
    arguments = {
        "run_id": "campaign-r1",
        "entity_id": "unit-1",
        "role": "question_writer",
        "system": "System",
        "prompt": "Prompt",
        "prompt_version": "test-v1",
        "parameters": {
            "temperature": 0,
            "max_tokens": 1000,
            "json_schema": schema,
        },
        "response_schema": schema,
        "reservation": Decimal("999999"),
        "timeout": 30,
        "retries": 0,
        "rate_limit_seconds": 0,
    }

    with pytest.raises(ProviderError, match="malformed JSON"):
        call_provider(database, provider, **arguments)
    with pytest.raises(ProviderError, match="malformed JSON"):
        call_provider(database, provider, **arguments)

    assert transport.methods == ["countTokens", "generateContent"]
    call = database.one("SELECT status,error_code FROM calls")
    assert call == {"status": "failed", "error_code": "PROVIDER_ERROR"}
    assert broker.status()["generation_submissions"] == 1


def test_started_local_journal_resumes_through_broker(tmp_path: Path) -> None:
    transport = Transport()
    broker = broker_fixture(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
    )
    database = Database(tmp_path / "state.sqlite3")
    database.migrate(tmp_path / "backups")
    schema = {
        "type": "object",
        "required": ["question"],
        "properties": {"question": {"type": "string"}},
        "additionalProperties": False,
    }
    parameters = {
        "temperature": 0,
        "max_tokens": 1000,
        "json_schema": schema,
    }
    prompt_hash = provider_prompt_hash(
        provider, "System", "Prompt", "test-v1", parameters
    )
    call_id = stable_id(
        "call", "campaign-r1", "unit-1", "question_writer", prompt_hash, 1
    )
    with database.transaction():
        database.connection.execute(
            """INSERT INTO calls
            (call_id,run_id,entity_id,role,provider,requested_model,prompt_version,
             prompt_hash,parameters_json,attempt,status,started_at)
            VALUES (?,?,?,?,?,?,?,?,?,1,'started','test-only-interruption')""",
            (
                call_id,
                "campaign-r1",
                "unit-1",
                "question_writer",
                provider.name,
                provider.model,
                "test-v1",
                prompt_hash,
                canonical_json(parameters),
            ),
        )

    result = call_provider(
        database,
        provider,
        run_id="campaign-r1",
        entity_id="unit-1",
        role="question_writer",
        system="System",
        prompt="Prompt",
        prompt_version="test-v1",
        parameters=parameters,
        response_schema=schema,
        reservation=Decimal("999999"),
        timeout=30,
        retries=0,
        rate_limit_seconds=0,
    )

    assert result.payload == {"question": "What changed?"}
    assert database.one("SELECT status FROM calls") == {"status": "completed"}
    assert transport.methods == ["countTokens", "generateContent"]


def test_adapter_rejects_receipt_that_is_absent_from_broker_ledger(
    tmp_path: Path,
) -> None:
    transport = Transport()
    broker = broker_fixture(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
    )
    schema = {
        "type": "object",
        "required": ["question"],
        "properties": {"question": {"type": "string"}},
        "additionalProperties": False,
    }
    parameters = {
        "temperature": 0,
        "max_tokens": 1000,
        "json_schema": schema,
    }
    payload = _request_payload("System", "Prompt", parameters, broker.config)
    request_key = broker_request_key(
        model=provider.model,
        run_id="live-test-r1",
        stage="question_generation",
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
        payload=payload,
    )
    write_json(broker.receipts_dir / f"{request_key}.json", {})

    with pytest.raises(ValueError, match="immutable paid-call event"):
        provider.invoke("question_writer", "System", "Prompt", parameters, 30)

    assert transport.methods == []


class ExpensivePaperTransport(Transport):
    """Report one costly call, so a family reaches the per-paper cost cap.

    The reservation stays under ``maximum_request_reserved_cost_usd``, so the
    refusal that follows is the per-paper cap and nothing else.
    """

    INPUT_TOKENS = 280_000
    OUTPUT_TOKENS = 900
    THINKING_TOKENS = 100

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": self.INPUT_TOKENS}
        return {
            "responseId": "expensive-response",
            "modelVersion": "gemini-3.8-flash",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [{"text": json.dumps({"question": "What changed?"})}]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": self.INPUT_TOKENS,
                "candidatesTokenCount": self.OUTPUT_TOKENS,
                "thoughtsTokenCount": self.THINKING_TOKENS,
                "totalTokenCount": (
                    self.INPUT_TOKENS + self.OUTPUT_TOKENS + self.THINKING_TOKENS
                ),
            },
        }


def test_paper_cost_cap_refusal_names_the_family_and_charges_nothing_past_it(
    tmp_path: Path,
) -> None:
    transport = ExpensivePaperTransport()
    broker = broker_fixture(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="paper-cap-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
    )
    parameters = {
        "temperature": 0,
        "max_tokens": 1000,
        "json_schema": {
            "type": "object",
            "required": ["question"],
            "properties": {"question": {"type": "string"}},
            "additionalProperties": False,
        },
    }

    accepted = 0
    error: PaperCostCapError | None = None
    for index in range(8):
        try:
            provider.invoke(
                "question_writer", "System", f"Prompt {index}", parameters, timeout=30
            )
        except PaperCostCapError as raised:
            error = raised
            break
        accepted += 1

    assert error is not None
    assert str(error) == PAPER_COST_CAP_REASON
    assert error.stage == "question_generation"
    assert error.code == "PAPER_COST_CAP_REACHED"
    # The cap bounds the family, not the run: the broker is not halted and no
    # money moved past the cap.
    status = broker.status()
    assert not status["halted"]
    cost_state = broker.family_cost_state("family-1")
    assert Decimal(cost_state["committed_usd"]) <= Decimal(
        cost_state["maximum_paper_cost_usd"]
    )
    assert Decimal(cost_state["committed_usd"]) == Decimal("0.21375") * accepted
    assert status["generation_submissions"] == accepted

    # The refusal is on the record as a receipt that no call was made under.
    refused = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "receipts").glob("*.json"))
    ]
    refusals = [row for row in refused if row.get("state") == "not_submitted"]
    assert len(refusals) == 1
    assert refusals[0]["reason"] == PAPER_COST_CAP_REASON
    assert refusals[0]["family_id"] == "family-1"
    assert refusals[0]["stage"] == "question_generation"
    assert refusals[0]["live_call_made"] is False


def test_a_capped_family_is_not_retried_on_relaunch(tmp_path: Path) -> None:
    transport = ExpensivePaperTransport()
    broker = broker_fixture(tmp_path, transport)
    parameters = {
        "temperature": 0,
        "max_tokens": 1000,
        "json_schema": {
            "type": "object",
            "required": ["question"],
            "properties": {"question": {"type": "string"}},
            "additionalProperties": False,
        },
    }

    def run_until_capped(invocation_run_id: str) -> tuple[int, PaperCostCapError]:
        provider = BrokerProvider(
            broker=broker,
            phase="live_test",
            invocation_run_id=invocation_run_id,
        ).bind(
            paper_id="paper-1",
            family_id="family-1",
            source_version_id=SOURCE_VERSION,
        )
        calls = 0
        for index in range(8):
            try:
                provider.invoke(
                    "question_writer",
                    "System",
                    f"Prompt {index}",
                    parameters,
                    timeout=30,
                )
            except PaperCostCapError as raised:
                return calls, raised
            calls += 1
        raise AssertionError("the per-paper cost cap never stopped the family")

    first_calls, first_error = run_until_capped("paper-cap-r1")
    methods_after_first = list(transport.methods)
    spend_after_first = broker.family_cost_state("family-1")["committed_usd"]

    second_calls, second_error = run_until_capped("paper-cap-r2")

    # The relaunch replays the completed receipts and the stored refusal. No new
    # provider call is made, and the family's spend does not move.
    assert second_calls == first_calls
    assert str(second_error) == str(first_error) == PAPER_COST_CAP_REASON
    assert second_error.stage == first_error.stage == "question_generation"
    assert transport.methods == methods_after_first
    assert broker.family_cost_state("family-1")["committed_usd"] == spend_after_first
