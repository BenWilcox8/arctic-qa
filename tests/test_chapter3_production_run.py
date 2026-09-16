"""Chapter 3 production run: the ceiling transition and the live calibration path.

The chapter 3 run (captain order 2026-09-16) needs two things the landed code
did not provide: a registered policy transition that moves the construction
ceiling to the chapter 3 baseline plus USD 20.00, and a calibration recording
path that the shared broker accepts. The calibration rows carry chapter 2
family ids that the ledger already binds to their papers, and the production
gate binds a streaming input that every construction request must carry.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from arctic_qa.broker_provider import BrokerProvider  # noqa: E402
from arctic_qa.cli import _calibrate_standalone  # noqa: E402
from arctic_qa.model_broker import (  # noqa: E402
    CEILING_CHANGES,
    CHAPTER2_CUMULATIVE_CEILING_USD,
    CHAPTER3_ALLOCATION_USD,
    CHAPTER3_BUDGET_CHANGE,
    CHAPTER3_CONSTRUCTION_SPEND_BEFORE_USD,
    CHAPTER3_CUMULATIVE_CEILING_USD,
    POLICY_TRANSITION_CHANGES,
    SharedGeminiBroker,
    _validate_policy,
)
from arctic_qa.standalone_calibration import (  # noqa: E402
    CALIBRATION_PAPER_PREFIX,
    calibration_paper_identity,
    evaluate_cassette,
    record_cassette,
    standalone_system_sha256,
)
from arctic_qa.util import sha256_file  # noqa: E402
from test_chapter2_integration import _price_transition, _production_broker  # noqa: E402
from test_model_broker import (  # noqa: E402
    HighCostTransport,
    Transport,
    prepared_budget_bounded_transition,
    reviewed_policy_transition,
    write_json,
)
from test_standalone_calibration import CALIBRATION, _judge_response  # noqa: E402

PRICE_CONFIG = ROOT / "config" / "gemini-eligibility-v1.json"
POLICY_V1 = ROOT / "config" / "streaming-dataset-budget-policy-v1.json"


# The chapter 3 ceiling: USD 53.990121 of construction spend before, USD 20.00 added.


def test_the_chapter_three_ceiling_is_the_baseline_plus_twenty(tmp_path: Path) -> None:
    assert CHAPTER3_CONSTRUCTION_SPEND_BEFORE_USD == Decimal("53.990121")
    assert CHAPTER3_ALLOCATION_USD == Decimal("20.00")
    assert CHAPTER3_CUMULATIVE_CEILING_USD == Decimal("73.990121")
    # The retired chapter 2 headroom is not carried: the ceiling moves down.
    assert CHAPTER3_CUMULATIVE_CEILING_USD < CHAPTER2_CUMULATIVE_CEILING_USD
    assert CHAPTER3_BUDGET_CHANGE == {
        "away_session_total_ceiling_usd": {"from": "108.994972", "to": "73.990121"}
    }
    assert CHAPTER3_BUDGET_CHANGE in POLICY_TRANSITION_CHANGES
    assert CHAPTER3_BUDGET_CHANGE in CEILING_CHANGES

    policy = json.loads(POLICY_V1.read_text(encoding="utf-8"))
    policy["away_session_total_ceiling_usd"] = "73.990121"
    path = tmp_path / "policy.json"
    write_json(path, policy)
    assert _validate_policy(path)["away_session_total_ceiling_usd"] == "73.990121"

    policy["away_session_total_ceiling_usd"] = "74.00"
    write_json(path, policy)
    with pytest.raises(ValueError, match="away_session_total_ceiling_usd"):
        _validate_policy(path)


def _chapter2_state(tmp_path: Path) -> dict:
    """Replay the reviewed chain through the chapter 2 ceiling."""
    values = _production_broker(tmp_path)
    production = values["production"]
    predecessor = production.status()["config_transition_sha256"]
    chapter2_config_value = json.loads(PRICE_CONFIG.read_text(encoding="utf-8"))
    chapter2_config_value["documented_availability_checked_at_utc"] = (
        "2026-09-15T16:00:00Z"
    )
    chapter2_config = tmp_path / "gemini-eligibility-chapter2.json"
    write_json(chapter2_config, chapter2_config_value)
    price_transition = _price_transition(
        tmp_path,
        values,
        from_config=PRICE_CONFIG,
        to_config=chapter2_config,
        predecessor=predecessor,
        policy=values["production_policy"],
    )
    priced = SharedGeminiBroker(
        policy_file=values["production_policy"],
        price_config_file=chapter2_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=HighCostTransport(),
        config_transition_file=price_transition,
    )
    predecessor = priced.status()["config_transition_sha256"]
    chapter2_value = dict(values["production_value"])
    chapter2_value["away_session_total_ceiling_usd"] = "108.994972"
    chapter2_policy = tmp_path / "streaming-dataset-budget-policy-chapter2.json"
    write_json(chapter2_policy, chapter2_value)
    chapter2_transition = reviewed_policy_transition(
        tmp_path,
        values,
        chapter2_policy,
        active_config=chapter2_config,
        from_config_transition_sha256=predecessor,
        from_policy=values["production_policy"],
        changed_policy_fields={
            "away_session_total_ceiling_usd": {
                "from": "61.614496",
                "to": "108.994972",
            }
        },
        maximum_authorized_cumulative_tranche_usd="108.994972",
        reason="Add the chapter 2 allocation of USD 75.00; halt at exhaustion.",
    )
    chapter2 = SharedGeminiBroker(
        policy_file=chapter2_policy,
        price_config_file=chapter2_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=HighCostTransport(),
        config_transition_file=chapter2_transition,
    )
    assert chapter2.status()["limits"]["away_session_total_ceiling_usd"] == (
        "108.994972"
    )
    return {
        **values,
        "chapter2_config": chapter2_config,
        "chapter2_policy": chapter2_policy,
        "chapter2_value": chapter2_value,
        "chapter2_event": chapter2.status()["config_transition_sha256"],
    }


def test_the_chapter_three_chain_reprices_then_moves_the_ceiling_down(
    tmp_path: Path,
) -> None:
    """The deployment chain: a v3 price transition to v8, then the v2 ceiling."""
    values = _chapter2_state(tmp_path)

    # Step 1: the price config moves to the chapter 3 revision (a distinct hash).
    chapter3_config_value = json.loads(
        values["chapter2_config"].read_text(encoding="utf-8")
    )
    chapter3_config_value["documented_availability_checked_at_utc"] = (
        "2026-09-16T05:00:00Z"
    )
    chapter3_config = tmp_path / "gemini-eligibility-chapter3.json"
    write_json(chapter3_config, chapter3_config_value)
    price_transition = _price_transition(
        tmp_path,
        values,
        from_config=values["chapter2_config"],
        to_config=chapter3_config,
        predecessor=values["chapter2_event"],
        policy=values["chapter2_policy"],
    )
    priced = SharedGeminiBroker(
        policy_file=values["chapter2_policy"],
        price_config_file=chapter3_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=HighCostTransport(),
        config_transition_file=price_transition,
    )
    predecessor = priced.status()["config_transition_sha256"]
    assert predecessor

    # Step 2: the ceiling moves to the chapter 3 baseline plus the allocation.
    chapter3_value = dict(values["chapter2_value"])
    chapter3_value["away_session_total_ceiling_usd"] = "73.990121"
    chapter3_policy = tmp_path / "streaming-dataset-budget-policy-chapter3.json"
    write_json(chapter3_policy, chapter3_value)
    chapter3_transition = reviewed_policy_transition(
        tmp_path,
        values,
        chapter3_policy,
        active_config=chapter3_config,
        from_config_transition_sha256=predecessor,
        from_policy=values["chapter2_policy"],
        changed_policy_fields=CHAPTER3_BUDGET_CHANGE,
        maximum_authorized_cumulative_tranche_usd="73.990121",
        reason="Chapter 3 allocation of USD 20.00; the chapter 2 headroom retires.",
    )
    chapter3 = SharedGeminiBroker(
        policy_file=chapter3_policy,
        price_config_file=chapter3_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=HighCostTransport(),
        config_transition_file=chapter3_transition,
    )
    chapter3.bind_stream_input(**values["stream_binding"])
    status = chapter3.status()
    assert status["integrity_valid"] is True
    assert status["limits"]["away_session_total_ceiling_usd"] == "73.990121"
    assert Decimal(status["remaining"]["away_session_usd"]) == (
        Decimal("73.990121") - Decimal(status["usage"]["away_session_usd"])
    )
    # Spend, requests and the evaluation reserve are untouched by the move.
    assert status["spent_usd"] == priced.status()["spent_usd"]
    assert status["count_requests"] == priced.status()["count_requests"]
    assert status["limits"]["reserved_for_benchmark_evaluation_usd"] == "500.00"
    assert status["limits"]["authorized_live_test_ceiling_usd"] in (None, "10.00")

    # The tranche must name the chapter 3 ceiling, nothing else.
    wrong = reviewed_policy_transition(
        tmp_path,
        values,
        chapter3_policy,
        active_config=chapter3_config,
        from_config_transition_sha256=predecessor,
        from_policy=values["chapter2_policy"],
        changed_policy_fields=CHAPTER3_BUDGET_CHANGE,
        maximum_authorized_cumulative_tranche_usd="20.00",
    )
    with pytest.raises(ValueError, match="transition"):
        SharedGeminiBroker(
            policy_file=chapter3_policy,
            price_config_file=chapter3_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=HighCostTransport(),
            config_transition_file=wrong,
        ).status()


# The live calibration recording through the shared broker.


class JudgeTransport(Transport):
    """Answer each gating row with the response a correct judge gives."""

    def __init__(self) -> None:
        super().__init__()
        self.responses = [_judge_response(row) for row in CALIBRATION.gating_rows]

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 100}
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(self.responses.pop(0))}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 115,
            },
        }


def test_a_recording_is_one_synthetic_paper_keyed_to_the_set_and_the_prompt() -> None:
    identity = calibration_paper_identity(CALIBRATION)
    set_version = CALIBRATION.header["calibration_set_version"]
    prompt_sha256 = standalone_system_sha256()
    assert identity["paper_id"] == identity["family_id"]
    assert identity["paper_id"].startswith(f"{CALIBRATION_PAPER_PREFIX}:{set_version}:")
    assert identity["paper_id"].endswith(prompt_sha256[:12])
    assert identity["source_version_id"] == f"{set_version}:{prompt_sha256}"
    # No chapter 2 family id, which the ledger binds to a real paper, is reused.
    assert identity["family_id"] not in {
        row.get("family_id") for row in CALIBRATION.rows
    }


def test_recording_through_the_bound_broker_leaves_one_family_and_a_receipt_per_row(
    tmp_path: Path,
) -> None:
    values = prepared_budget_bounded_transition(tmp_path)
    transport = JudgeTransport()
    broker = SharedGeminiBroker(
        policy_file=values["budget_policy"],
        price_config_file=PRICE_CONFIG,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=values["budget_transition"],
    )
    assert broker.stream_input_binding_required() is True
    binding = values["stream_binding"]
    broker.bind_stream_input(**binding)
    provider = BrokerProvider(
        broker=broker, phase=binding["phase"], invocation_run_id=binding["run_id"]
    )
    identity = calibration_paper_identity(CALIBRATION)
    before = broker.status()

    cassette = tmp_path / "cassette.jsonl"
    recorded = record_cassette(
        CALIBRATION,
        provider,
        cassette,
        bind_row=lambda unbound, row: unbound.bind(**identity),
    )
    report = evaluate_cassette(CALIBRATION, cassette)

    gating = len(CALIBRATION.gating_rows)
    assert recorded["rows"] == gating
    assert report["passed"] is True
    assert transport.methods.count("generateContent") == gating
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    after = broker.status()
    assert after["count_requests"] - before["count_requests"] == gating
    assert ledger["family_bindings"][identity["family_id"]] == {
        "paper_id": identity["paper_id"],
        "source_version_id": identity["source_version_id"],
    }
    assert ledger["paper_bindings"][identity["paper_id"]] == {
        "family_id": identity["family_id"],
        "source_version_id": identity["source_version_id"],
    }
    calibration_requests = [
        request
        for request in ledger["requests"].values()
        if request["paper_id"] == identity["paper_id"]
    ]
    assert len(calibration_requests) == gating
    assert {request["stage"] for request in calibration_requests} == {
        "standalone_verification"
    }
    assert {request["state"] for request in calibration_requests} == {"completed"}
    assert all(
        request["run_id"] == binding["run_id"] for request in calibration_requests
    )
    assert Decimal(after["spent_usd"]) > Decimal(before["spent_usd"])
    assert after["integrity_valid"] is True
    # Every row's cost is booked on the one calibration paper.
    paper = after["papers"][identity["paper_id"]]
    assert Decimal(paper["spent_usd"]) == Decimal(after["spent_usd"]) - Decimal(
        before["spent_usd"]
    )
    assert all(
        row["actual_cost_usd"] is not None
        for row in [
            json.loads(line)
            for line in cassette.read_text(encoding="utf-8").splitlines()[1:]
        ]
    )


def test_the_cli_refuses_to_record_without_the_gate_stream_input(
    tmp_path: Path,
) -> None:
    values = prepared_budget_bounded_transition(tmp_path)
    args = SimpleNamespace(
        mode="record",
        cassette=tmp_path / "cassette.jsonl",
        calibration_set=None,
        provider="broker",
        provider_script=None,
        run_id="run-1",
        phase="live_test",
        timeout=30.0,
        streaming_budget_policy_file=values["budget_policy"],
        price_config_file=PRICE_CONFIG,
        execution_gate_file=values["gate"],
        shared_ledger_file=values["ledger"],
        model_receipts_dir=tmp_path / "receipts",
        ledger_config_transition_file=values["budget_transition"],
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        campaign_id=None,
        access_run_dir=None,
        eligibility_prompt_file=None,
        eligibility_schema_file=None,
        eligibility_policy_file=None,
    )
    ledger_before = sha256_file(values["ledger"])
    receipts = tmp_path / "receipts"

    def request_receipts() -> list[str]:
        # The broker constructor applies the reviewed transition event; no
        # request receipt may appear.
        return sorted(
            path.name
            for path in receipts.iterdir()
            if not path.name.startswith("config-transition-")
        )

    receipts_before = request_receipts()
    with pytest.raises(ValueError, match="--access-run-dir"):
        _calibrate_standalone(args)
    assert not (tmp_path / "cassette.jsonl").exists()
    assert request_receipts() == receipts_before
    assert sha256_file(values["ledger"]) == ledger_before
