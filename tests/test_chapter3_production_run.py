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
    CHAPTER3_EXPANSION_ACCEPTED_TARGET,
    CHAPTER3_EXPANSION_ALLOCATION_USD,
    CHAPTER3_EXPANSION_CHANGE,
    CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD,
    CHAPTER3_EXPANSION_MAXIMUM_SUBMISSIONS,
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


def _chapter3_state(tmp_path: Path) -> dict:
    """Replay the deployment chain: a v3 price transition to v8, then the v2 ceiling."""
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
    return {
        **values,
        "chapter3_config": chapter3_config,
        "chapter3_policy": chapter3_policy,
        "chapter3_value": chapter3_value,
        "chapter3_predecessor": predecessor,
        "priced": priced,
        "chapter3": chapter3,
    }


def test_the_chapter_three_chain_reprices_then_moves_the_ceiling_down(
    tmp_path: Path,
) -> None:
    values = _chapter3_state(tmp_path)
    chapter3 = values["chapter3"]
    priced = values["priced"]
    chapter3_policy = values["chapter3_policy"]
    chapter3_config = values["chapter3_config"]
    predecessor = values["chapter3_predecessor"]
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


# The chapter 3 expansion: the allocation becomes USD 200.00 in total.


def test_the_chapter_three_expansion_moves_four_limits_together(
    tmp_path: Path,
) -> None:
    assert CHAPTER3_EXPANSION_ALLOCATION_USD == Decimal("200.00")
    assert CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD == Decimal("253.990121")
    assert CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD == (
        CHAPTER3_CONSTRUCTION_SPEND_BEFORE_USD + Decimal("200.00")
    )
    # The USD 20.00 already inside the chapter 3 ceiling is not added on top.
    assert (
        CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD - CHAPTER3_CUMULATIVE_CEILING_USD
        == (Decimal("180.00"))
    )
    assert CHAPTER3_EXPANSION_CHANGE == {
        "away_session_total_ceiling_usd": {"from": "73.990121", "to": "253.990121"},
        "away_maximum_generation_submissions": {"from": 5000, "to": 20000},
        "accepted_question_target": {"from": 500, "to": 2000},
        "construction_review_checkpoint_usd": {"from": "250.00", "to": "253.990121"},
    }
    assert CHAPTER3_EXPANSION_MAXIMUM_SUBMISSIONS == 20000
    assert CHAPTER3_EXPANSION_ACCEPTED_TARGET == 2000
    assert CHAPTER3_EXPANSION_CHANGE in POLICY_TRANSITION_CHANGES
    assert CHAPTER3_EXPANSION_CHANGE in CEILING_CHANGES

    policy = json.loads(POLICY_V1.read_text(encoding="utf-8"))
    path = tmp_path / "policy.json"

    def write(**fields: object) -> None:
        write_json(path, {**policy, **fields})

    expanded = {
        "away_session_total_ceiling_usd": "253.990121",
        "away_maximum_generation_submissions": 20000,
        "accepted_question_target": 2000,
        "construction_review_checkpoint_usd": "253.990121",
    }
    write(**expanded)
    value = _validate_policy(path)
    assert value["away_session_total_ceiling_usd"] == "253.990121"
    assert value["construction_review_checkpoint_usd"] == "253.990121"
    assert value["away_maximum_generation_submissions"] == 20000
    assert value["accepted_question_target"] == 2000

    # The checkpoint never exceeds the ceiling, and no field moves alone.
    write(**{**expanded, "away_session_total_ceiling_usd": "73.990121"})
    with pytest.raises(ValueError, match="checkpoint"):
        _validate_policy(path)
    write(**{**expanded, "construction_review_checkpoint_usd": "250.00"})
    _validate_policy(path)  # a lower checkpoint under the new ceiling is a valid value
    write(**{**expanded, "away_maximum_generation_submissions": 5000})
    with pytest.raises(ValueError, match="away_maximum_generation_submissions"):
        _validate_policy(path)
    write(**{**expanded, "accepted_question_target": 500})
    with pytest.raises(ValueError, match="accepted_question_target"):
        _validate_policy(path)
    write(away_maximum_generation_submissions=20000)
    with pytest.raises(ValueError, match="away_maximum_generation_submissions"):
        _validate_policy(path)
    write(accepted_question_target=2000)
    with pytest.raises(ValueError, match="accepted_question_target"):
        _validate_policy(path)
    write(construction_review_checkpoint_usd="300.00")
    with pytest.raises(ValueError, match="construction_review_checkpoint_usd"):
        _validate_policy(path)
    # The chapter 3 policy stays valid as it is.
    write(away_session_total_ceiling_usd="73.990121")
    assert _validate_policy(path)["accepted_question_target"] == 500


def _expansion_policy(values: dict, tmp_path: Path) -> Path:
    expansion_value = dict(values["chapter3_value"])
    for field, limits in CHAPTER3_EXPANSION_CHANGE.items():
        assert expansion_value[field] == limits["from"]
        expansion_value[field] = limits["to"]
    path = tmp_path / "streaming-dataset-budget-policy-chapter3-expansion.json"
    write_json(path, expansion_value)
    return path


def _expansion_transition(
    values: dict, tmp_path: Path, policy: Path, **changes
) -> Path:
    fields = {
        "changed_policy_fields": CHAPTER3_EXPANSION_CHANGE,
        "maximum_authorized_cumulative_tranche_usd": "253.990121",
        "reason": "Chapter 3 expansion: the allocation becomes USD 200.00 in total.",
        **changes,
    }
    return reviewed_policy_transition(
        tmp_path,
        values,
        policy,
        active_config=values["chapter3_config"],
        from_config_transition_sha256=values["chapter3"].status()[
            "config_transition_sha256"
        ],
        from_policy=values["chapter3_policy"],
        **fields,
    )


def _expansion_broker(values: dict, tmp_path: Path, policy: Path, transition: Path):
    return SharedGeminiBroker(
        policy_file=policy,
        price_config_file=values["chapter3_config"],
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=HighCostTransport(),
        config_transition_file=transition,
    )


def test_the_chapter_three_expansion_chains_onto_the_chapter_three_ceiling(
    tmp_path: Path,
) -> None:
    """The expansion is a v2 transition whose predecessor is the chapter 3 event."""
    values = _chapter3_state(tmp_path)
    before = values["chapter3"].status()
    policy = _expansion_policy(values, tmp_path)
    transition = _expansion_transition(values, tmp_path, policy)
    expanded = _expansion_broker(values, tmp_path, policy, transition)
    expanded.bind_stream_input(**values["stream_binding"])
    status = expanded.status()
    assert status["integrity_valid"] is True
    assert status["halted"] is False
    limits = status["limits"]
    assert limits["away_session_total_ceiling_usd"] == "253.990121"
    assert limits["construction_review_checkpoint_usd"] == "253.990121"
    assert limits["away_maximum_generation_submissions"] == 20000
    assert limits["accepted_question_target"] == 2000
    assert Decimal(status["remaining"]["away_session_usd"]) == (
        Decimal("253.990121") - Decimal(status["usage"]["away_session_usd"])
    )
    assert Decimal(status["remaining"]["construction_checkpoint_usd"]) == (
        Decimal("253.990121") - Decimal(status["usage"]["construction_checkpoint_usd"])
    )
    # Spend, requests, the price config and the evaluation reserve are untouched.
    assert status["spent_usd"] == before["spent_usd"]
    assert status["count_requests"] == before["count_requests"]
    assert status["price_config_sha256"] == before["price_config_sha256"]
    assert limits["reserved_for_benchmark_evaluation_usd"] == "500.00"
    assert limits["project_lifetime_ceiling_usd"] == "1000.00"
    assert status["config_transition_sha256"] != before["config_transition_sha256"]
    # A restart with the same transition file reuses the event.
    again = _expansion_broker(values, tmp_path, policy, transition).status()
    assert again["config_transition_sha256"] == status["config_transition_sha256"]
    assert again["integrity_valid"] is True


def test_the_chapter_three_expansion_refuses_a_partial_move_or_another_tranche(
    tmp_path: Path,
) -> None:
    values = _chapter3_state(tmp_path)
    policy = _expansion_policy(values, tmp_path)
    # The tranche must be the expansion ceiling, not the allocation.
    wrong_tranche = _expansion_transition(
        values, tmp_path, policy, maximum_authorized_cumulative_tranche_usd="200.00"
    )
    with pytest.raises(ValueError, match="transition"):
        _expansion_broker(values, tmp_path, policy, wrong_tranche).status()
    # The ceiling alone is not a registered change any more than the counts alone.
    ceiling_only = _expansion_transition(
        values,
        tmp_path,
        policy,
        changed_policy_fields={
            "away_session_total_ceiling_usd": {"from": "73.990121", "to": "253.990121"}
        },
    )
    with pytest.raises(ValueError, match="transition"):
        _expansion_broker(values, tmp_path, policy, ceiling_only).status()
    # A target policy that moves one field further than registered is refused.
    drifted_value = json.loads(policy.read_text(encoding="utf-8"))
    drifted_value["accepted_question_target"] = 2000
    drifted_value["maximum_generation_requests_per_minute"] = 11
    drifted = tmp_path / "drifted-policy.json"
    write_json(drifted, drifted_value)
    drifted_transition = _expansion_transition(values, tmp_path, drifted)
    with pytest.raises(ValueError):
        _expansion_broker(values, tmp_path, drifted, drifted_transition).status()


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
