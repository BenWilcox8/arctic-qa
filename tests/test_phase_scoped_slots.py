"""Phase-scoped slots and the resume of a transiently refused request.

On 2026-09-16 at 10:00 UTC two abstention evaluation requests were in flight
while the chapter 3 producer asked for a slot. The shared in-flight counter
refused the producer's request as "the paid-call concurrency limit is
complete", the refusal became an immutable not-submitted receipt, and every
relaunch returned that receipt as final. These tests pin the repair: each
phase counts its own slots, ``execute`` waits a bounded time for room before
it records a refusal, a request that a transient refusal stopped resumes under
the next reviewed transition, and evaluation requests made after an applied
transition and before its first construction request do not stop a start.
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

from arctic_qa import ledger_store, model_broker  # noqa: E402
from arctic_qa.broker_provider import BrokerProvider, _request_payload  # noqa: E402
from arctic_qa.errors import TransientReservationError  # noqa: E402
from arctic_qa.model_broker import (  # noqa: E402
    CONCURRENCY_LIMIT_REASON,
    EVALUATION_PHASE,
    MINUTE_LIMIT_REASON,
    RESUMABLE_NOT_SUBMITTED_REASONS,
    SharedGeminiBroker,
)
from test_abstention_broker import LetterTransport, bind, evaluation_fixture  # noqa: E402
from test_abstention_broker import execute as evaluation_execute  # noqa: E402
from test_model_broker import (  # noqa: E402
    ROOT as BROKER_ROOT,
    Transport,
    execute,
    fixture,
    policy_with_limits,
    reviewed_policy_transition,
    write_json,
    rewrite_ledger,
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


def _construction_broker(values: dict, tmp_path: Path, policy: Path, transition: Path):
    return SharedGeminiBroker(
        policy_file=policy,
        price_config_file=BROKER_ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["construction_gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=transition,
    )


def _applied_construction_transition(tmp_path: Path) -> dict:
    """An evaluation-capable ledger with one applied construction transition."""
    values = evaluation_fixture(tmp_path, transport=LetterTransport("B"))
    bind(values)
    write_json(
        values["construction_gate"],
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": True,
            "allowed_phase": "live_test",
            "integrated_code_commit": "fixture-commit",
            "independent_review_verdict": "pass",
            "review_record": "fixture-review",
        },
    )
    construction = {"gate": values["construction_gate"], "ledger": values["ledger"]}
    policy = policy_with_limits(tmp_path, paper_limit=40)
    transition = reviewed_policy_transition(tmp_path, construction, policy)
    applied = _construction_broker(values, tmp_path, policy, transition)
    assert applied.status()["limits"]["live_test_maximum_papers"] == 40
    return {**values, "policy": policy, "transition": transition}


def test_an_applied_transition_survives_evaluation_activity_before_its_first_request(
    tmp_path: Path,
) -> None:
    values = _applied_construction_transition(tmp_path)
    # The evaluator, still on the initial construction policy, keeps working.
    assert evaluation_execute(values, trial_id="t1")["state"] == "completed"
    assert evaluation_execute(values, trial_id="t2", repeat=2)["state"] == "completed"

    restarted = _construction_broker(
        values, tmp_path, values["policy"], values["transition"]
    )
    status = restarted.status()
    assert status["integrity_valid"] is True
    assert status["limits"]["live_test_maximum_papers"] == 40
    assert status["evaluation"]["submissions"] == 2
    # The first construction request binds the event; a later start needs no
    # snapshot comparison at all.
    assert execute(restarted, paper="p1")["state"] == "completed"
    again = _construction_broker(
        values, tmp_path, values["policy"], values["transition"]
    )
    assert again.status()["integrity_valid"] is True


def test_construction_activity_before_the_first_transitioned_request_still_stops(
    tmp_path: Path,
) -> None:
    values = _applied_construction_transition(tmp_path)
    # A construction request under the initial policy after the application is
    # the change the snapshot check exists for.
    assert execute(values["broker"], paper="p1")["state"] == "completed"
    with pytest.raises(ValueError, match="ledger hash changed"):
        _construction_broker(values, tmp_path, values["policy"], values["transition"])


def test_an_applied_transition_survives_a_phase_less_evaluation_row(
    tmp_path: Path,
) -> None:
    """The 23:04 UTC exit.

    A request refused before its reservation recorded no phase: the row was
    created by the count event and the phase was written by the reserve. The
    production ledger held thirty-one evaluation rows of that shape. Reading
    one of them as construction activity refused every start of the producer
    after an applied transition.

    The writer records the phase from the first record now, so this test makes
    the historical shape itself: it strips the phase from the refused row. The
    reader must survive such a row whatever wrote it, because the ledger keeps
    the rows the old writer left.
    """
    values = _applied_construction_transition(tmp_path)
    repeats = int(
        values["broker"].evaluation_policy["maximum_calls_per_item_condition_model_arm"]
    )
    for index in range(repeats):
        assert evaluation_execute(values, trial_id=f"t{index}")["state"] == "completed"
    refused = evaluation_execute(values, trial_id="t-over")
    assert refused["state"] == "not_submitted"
    assert refused["reason"] == model_broker.EVALUATION_ITEM_REPEAT_REASON

    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    row = ledger["requests"][refused["request_key"]]
    # The writer records the phase now, which is the other half of the repair.
    assert row["phase"] == model_broker.EVALUATION_PHASE
    assert str(row["stage"]).startswith("evaluation_")
    # Make the historical shape the old writer left, and read it again.
    del row["phase"]
    rewrite_ledger(values["ledger"], ledger)

    restarted = _construction_broker(
        values, tmp_path, values["policy"], values["transition"]
    )

    assert restarted.status()["integrity_valid"] is True
    assert execute(restarted, paper="p1")["state"] == "completed"


def test_the_tolerance_reads_the_stage_family_not_only_the_phase() -> None:
    """A phase-less row is evaluation activity only when its stage says so."""
    applied = _stamp(60)
    later = _stamp(5)

    def ledger_with(stage: str) -> dict:
        return {
            "requests": {
                "k": {
                    "state": "not_submitted",
                    "stage": stage,
                    "completed_at_utc": later,
                }
            }
        }

    only_evaluation = SharedGeminiBroker._only_evaluation_activity_since
    assert only_evaluation(ledger_with("evaluation_answer:gemini-3.8-flash"), applied)
    assert not only_evaluation(ledger_with("eligibility"), applied)
    # A row that predates the application is not activity since it at all.
    stale = ledger_with("eligibility")
    stale["requests"]["k"]["completed_at_utc"] = _stamp(120)
    assert only_evaluation(stale, applied)


# --- A refusal the evaluator can never re-ask ---------------------------------


def test_a_deferring_caller_hears_the_refusal_and_nothing_is_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    """The bound raises instead of recording, and the row stays ``counting``.

    The recorded ``not_submitted`` refusal is immutable and resumes only under
    a later reviewed transition, which is right for the producer: it skips the
    paper and the next transition brings the request back. It is wrong for the
    streaming evaluator, whose policy forbids a re-ask of a recorded trial, so
    a refused trial is a question that can never reach its 48 responses. Five
    questions were stranded that way in the thirty minutes to 16:43 UTC on
    2026-09-17.

    A caller that sets ``defer_transient_reservations`` is told instead. It
    holds the trial pending and asks again, and here the same request key
    reaches the provider under the same authorization, with no transition at
    all.
    """
    transport = LetterTransport()
    values = evaluation_fixture(
        tmp_path, transport=transport, defer_transient_reservations=True
    )
    bind(values)
    _refusing_reserve(monkeypatch, CONCURRENCY_LIMIT_REASON, times=None)
    monkeypatch.setattr(model_broker, "TRANSIENT_RESERVATION_RETRY_SECONDS", 0.0)

    with pytest.raises(TransientReservationError, match=CONCURRENCY_LIMIT_REASON):
        evaluation_execute(values, trial_id="trial-deferred")

    # Nothing was written: no receipt, and the row is the free count alone.
    assert list((tmp_path / "receipts").glob("*.json")) == []
    ledger = ledger_store.read_ledger(values["ledger"])
    rows = list(ledger["requests"].values())
    assert [row["state"] for row in rows] == ["counting"]
    assert ledger["halted"] is False and ledger["inflight"] == 0
    assert values["broker"].status()["integrity_valid"] is True

    # The next attempt of the same key runs, and reuses the counting row.
    monkeypatch.undo()
    receipt = evaluation_execute(values, trial_id="trial-deferred")
    assert receipt["state"] == "completed"
    ledger = ledger_store.read_ledger(values["ledger"])
    assert len(ledger["requests"]) == 1
    assert values["broker"].status()["integrity_valid"] is True


@pytest.mark.parametrize("reason", [CONCURRENCY_LIMIT_REASON, MINUTE_LIMIT_REASON])
def test_a_deferring_caller_still_records_every_other_refusal(
    tmp_path: Path, monkeypatch, reason: str
) -> None:
    """Only the two scheduling refusals are deferred, and both of them are.

    Every other refusal describes the request, its authorization or the money,
    so it stays the recorded ``not_submitted`` refusal it has always been.
    """
    values = evaluation_fixture(
        tmp_path, transport=LetterTransport(), defer_transient_reservations=True
    )
    bind(values)
    monkeypatch.setattr(model_broker, "TRANSIENT_RESERVATION_RETRY_SECONDS", 0.0)

    _refusing_reserve(monkeypatch, reason, times=None)
    with pytest.raises(TransientReservationError):
        evaluation_execute(values, trial_id="trial-scheduling")
    monkeypatch.undo()

    monkeypatch.setattr(model_broker, "TRANSIENT_RESERVATION_RETRY_SECONDS", 0.0)
    _refusing_reserve(monkeypatch, model_broker.AUTHORIZED_CAP_REASON, times=None)
    recorded = evaluation_execute(values, trial_id="trial-cap")
    assert recorded["state"] == "not_submitted"
    assert recorded["reason"] == model_broker.AUTHORIZED_CAP_REASON


def test_the_default_broker_keeps_the_recorded_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    """The producer's broker is unchanged: it records and skips the paper."""
    values = fixture(tmp_path, transport=Transport())
    assert values["broker"].defer_transient_reservations is False
    _refusing_reserve(monkeypatch, CONCURRENCY_LIMIT_REASON, times=None)
    monkeypatch.setattr(model_broker, "TRANSIENT_RESERVATION_RETRY_SECONDS", 0.0)

    blocked = execute(values["broker"])

    assert blocked["state"] == "not_submitted"
    assert blocked["reason"] == CONCURRENCY_LIMIT_REASON
