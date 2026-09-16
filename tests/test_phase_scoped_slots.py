"""Phase-scoped slots and the resume of a transiently refused request.

On 2026-09-16 at 10:00 UTC two abstention evaluation requests were in flight
while the chapter 3 producer asked for a slot. The shared in-flight counter
refused the producer's request as "the paid-call concurrency limit is
complete", the refusal became an immutable not-submitted receipt, and every
relaunch returned that receipt as final. These tests pin the three parts of
the repair: each phase counts its own slots and its own per-minute window,
``execute`` waits a bounded time for room before it records a refusal, and a
request that a transient refusal stopped resumes under the next reviewed
transition.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from arctic_qa import model_broker  # noqa: E402
from arctic_qa.broker_provider import BrokerProvider, _request_payload  # noqa: E402
from arctic_qa.model_broker import (  # noqa: E402
    CONCURRENCY_LIMIT_REASON,
    EVALUATION_PHASE,
    MINUTE_LIMIT_REASON,
    RESUMABLE_NOT_SUBMITTED_REASONS,
    SharedGeminiBroker,
)
from test_model_broker import (  # noqa: E402
    ROOT as BROKER_ROOT,
    Transport,
    execute,
    fixture,
    policy_with_limits,
    reviewed_policy_transition,
)


def status_transition(broker: SharedGeminiBroker) -> str:
    return broker.status()["config_transition_sha256"]


class StopTransport(Transport):
    """The fake transport with the finish reason the provider requires."""

    def post(self, model: str, method: str, body: dict) -> dict:
        response = super().post(model, method, body)
        for candidate in response.get("candidates", []):
            candidate["finishReason"] = "STOP"
        return response


PARAMETERS = {"temperature": 0, "max_tokens": 8_192, "json_schema": {"type": "object"}}


def _stamp(seconds_ago: int) -> str:
    when = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_each_phase_counts_its_own_slots() -> None:
    ledger = {
        "inflight": 3,
        "requests": {
            "eval-a": {
                "phase": EVALUATION_PHASE,
                "state": "submitted",
                "reserved_usd": "0.01",
                "submitted_at_utc": _stamp(5),
            },
            "construction-b": {
                "phase": "away_production",
                "state": "submitted",
                "reserved_usd": "0.02",
                "submitted_at_utc": _stamp(30),
            },
            "construction-c": {
                "phase": "away_production",
                "state": "completed",
                "reserved_usd": "0.02",
                "submitted_at_utc": _stamp(90),
            },
            "construction-d": {
                "phase": "live_test",
                "state": "not_submitted",
                "reserved_usd": "0.02",
            },
        },
    }
    assert SharedGeminiBroker._phase_inflight(ledger, EVALUATION_PHASE) == 1
    assert SharedGeminiBroker._phase_inflight(ledger, "away_production") == 2
    assert SharedGeminiBroker._phase_inflight(ledger, "live_test") == 2
    # The shared counter never goes negative when the evaluation count leads.
    ledger["inflight"] = 0
    assert SharedGeminiBroker._phase_inflight(ledger, "away_production") == 0


def _refusing_reserve(monkeypatch, reason: str, times: int | None) -> list[int]:
    """Make ``_reserve`` raise ``reason`` ``times`` times (forever when None)."""
    real = SharedGeminiBroker._reserve
    calls: list[int] = []

    def reserve(self, **kwargs):
        calls.append(1)
        if times is None or len(calls) <= times:
            raise ValueError(reason)
        return real(self, **kwargs)

    monkeypatch.setattr(SharedGeminiBroker, "_reserve", reserve)
    return calls


@pytest.mark.parametrize("reason", [CONCURRENCY_LIMIT_REASON, MINUTE_LIMIT_REASON])
def test_execute_waits_for_room_before_it_records_a_transient_refusal(
    tmp_path: Path, monkeypatch, reason: str
) -> None:
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    calls = _refusing_reserve(monkeypatch, reason, times=2)
    sleeps: list[float] = []
    monkeypatch.setattr(
        model_broker.time, "sleep", lambda seconds: sleeps.append(seconds)
    )

    receipt = execute(values["broker"])

    assert receipt["state"] == "completed"
    assert len(calls) == 3
    assert sleeps == [model_broker.TRANSIENT_RESERVATION_RETRY_INTERVAL_SECONDS] * 2
    assert transport.methods == ["countTokens", "generateContent"]


def test_a_refusal_that_outlasts_the_wait_is_recorded_and_stops_no_phase(
    tmp_path: Path, monkeypatch
) -> None:
    values = fixture(tmp_path, transport=Transport())
    _refusing_reserve(monkeypatch, CONCURRENCY_LIMIT_REASON, times=None)
    monkeypatch.setattr(model_broker, "TRANSIENT_RESERVATION_RETRY_SECONDS", 0.0)

    blocked = execute(values["broker"])

    assert blocked["state"] == "not_submitted"
    assert blocked["reason"] == CONCURRENCY_LIMIT_REASON
    assert blocked["live_call_made"] is False
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["halted"] is False and ledger["inflight"] == 0
    assert values["broker"].status()["integrity_valid"] is True
    # Without a reviewed transition the key stays a refused record.
    monkeypatch.undo()
    with pytest.raises(ValueError, match="reviewed transition"):
        execute(values["broker"])


def _blocked_then_transitioned(
    tmp_path: Path, monkeypatch, body: dict, transport: Transport | None = None
) -> dict:
    """Refuse p2 under a first reviewed transition; open a second one."""
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    receipts = tmp_path / "receipts"
    credential = tmp_path / "private" / "gemini.key"
    price_config = BROKER_ROOT / "config" / "gemini-eligibility-v1.json"
    current_policy = policy_with_limits(tmp_path, paper_limit=40)
    current_transition = reviewed_policy_transition(tmp_path, values, current_policy)
    current = SharedGeminiBroker(
        policy_file=current_policy,
        price_config_file=price_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=receipts,
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=current_transition,
    )
    predecessor = current.status()["config_transition_sha256"]
    _refusing_reserve(monkeypatch, CONCURRENCY_LIMIT_REASON, times=None)
    monkeypatch.setattr(model_broker, "TRANSIENT_RESERVATION_RETRY_SECONDS", 0.0)
    blocked = execute(current, paper="p2", body=body)
    assert blocked["state"] == "not_submitted"
    assert blocked["reason"] in RESUMABLE_NOT_SUBMITTED_REASONS
    monkeypatch.undo()
    # Under the same transition the refused key stays a refused record.
    with pytest.raises(ValueError, match="reviewed transition"):
        execute(current, paper="p2", body=body)
    prior = {path.name: path.read_bytes() for path in receipts.glob("*.json")}
    proposed_policy = policy_with_limits(
        tmp_path, paper_limit=41, submission_limit=101, source_policy=current_policy
    )
    proposed_transition = reviewed_policy_transition(
        tmp_path,
        values,
        proposed_policy,
        from_config_transition_sha256=predecessor,
        from_policy=current_policy,
        changed_policy_fields={
            "live_test_maximum_papers": {"from": 40, "to": 41},
            "live_test_maximum_generation_submissions": {"from": 100, "to": 101},
        },
    )
    transport = transport or Transport()
    resumed = SharedGeminiBroker(
        policy_file=proposed_policy,
        price_config_file=price_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=receipts,
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=proposed_transition,
    )
    return {
        **values,
        "blocked": blocked,
        "prior_receipts": prior,
        "resumed_broker": resumed,
        "resumed_transport": transport,
        "predecessor": predecessor,
    }


def test_a_transient_refusal_resumes_under_the_next_reviewed_transition(
    tmp_path: Path, monkeypatch
) -> None:
    body = _request_payload(
        "Return JSON.",
        "Paper text.",
        PARAMETERS,
        fixture(tmp_path / "probe", transport=Transport())["broker"].config,
    )
    values = _blocked_then_transitioned(tmp_path, monkeypatch, body)
    broker = values["resumed_broker"]
    blocked = values["blocked"]

    resumed = execute(broker, paper="p2", body=body)

    assert resumed["state"] == "completed"
    assert resumed["request_key"] == blocked["request_key"]
    assert ".resume-" in broker.effective_receipt_path(blocked["request_key"]).name
    assert values["resumed_transport"].methods == ["generateContent"]
    for name, content in values["prior_receipts"].items():
        assert (tmp_path / "receipts" / name).read_bytes() == content
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    request = ledger["requests"][blocked["request_key"]]
    assert request["state"] == "completed"
    assert request["resumed_from_not_submitted_sha256"]
    assert request["resumed_from_config_transition_sha256"] == values["predecessor"]
    assert request["config_transition_sha256"] == status_transition(broker)
    status = broker.status()
    assert status["integrity_valid"] is True
    # A restart validates the resumed request and replays nothing.
    restarted = SharedGeminiBroker(
        policy_file=broker.policy_file,
        price_config_file=BROKER_ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
    )
    assert restarted.status()["integrity_valid"] is True
    with pytest.raises(ValueError, match="already exists"):
        execute(restarted, paper="p2", body=body)


def test_the_provider_reissues_a_transiently_refused_request(
    tmp_path: Path, monkeypatch
) -> None:
    probe = fixture(tmp_path / "probe", transport=Transport())["broker"]
    body = _request_payload("Return JSON.", "Paper text.", PARAMETERS, probe.config)
    values = _blocked_then_transitioned(
        tmp_path, monkeypatch, body, transport=StopTransport()
    )
    provider = BrokerProvider(
        broker=values["resumed_broker"], phase="live_test", invocation_run_id="run-1"
    ).bind(paper_id="p2", family_id="family-p2", source_version_id="source-p2")

    result = provider.invoke(
        "eligibility", "Return JSON.", "Paper text.", PARAMETERS, timeout=30
    )

    assert result.payload == {}
    receipt = values["resumed_broker"].effective_receipt(
        values["blocked"]["request_key"]
    )
    assert receipt["state"] == "completed"
    assert values["resumed_transport"].methods == ["generateContent"]
