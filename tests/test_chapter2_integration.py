"""Chapter 2 integration: the launch contract for the merged slices.

Covers the run blockers the core-routing slice handed to integration: a role
profile that runs inside the one metered provider, the judge stage models with
verified pricing, the chapter 2 budget ceiling with its chained transition, the
live export pin, and the gate-bindable chapter 2 streaming input.
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

from arctic_qa import validation  # noqa: E402
from arctic_qa.chapter2_corpus import (  # noqa: E402
    STREAM_INPUT_DIRECTORY,
    freeze,
    materialize_stream_input,
    reextract,
)
from arctic_qa.gemini_eligibility import (  # noqa: E402
    PRO_JUDGE_STAGES,
    _config,
    model_config_for_stage,
)
from arctic_qa.model_broker import (  # noqa: E402
    CHAPTER2_BUDGET_EXTENSION_CHANGE,
    CHAPTER2_CUMULATIVE_CEILING_USD,
    POLICY_TRANSITION_CHANGES,
    SharedGeminiBroker,
    _validate_policy,
)
from arctic_qa.model_roles import (  # noqa: E402
    JUDGE_ROLES,
    STRONGEST_JUDGE_ROLES,
    WRITER_ROLE,
    load_role_contract,
    resolve_roles,
)
from arctic_qa.util import sha256_file  # noqa: E402
from test_chapter2_corpus import corpus  # noqa: E402, F401
from test_model_broker import (  # noqa: E402
    HighCostTransport,
    Transport,
    execute,
    payload,
    prepared_ceiling_extension,
    reviewed_policy_transition,
    write_json,
)

ROLES_FILE = ROOT / "config" / "roles.v1.json"
PRICE_CONFIG = ROOT / "config" / "gemini-eligibility-v1.json"
POLICY_V1 = ROOT / "config" / "streaming-dataset-budget-policy-v1.json"


# Run blocker 2: a profile that runs inside the one metered provider.


def test_the_launch_profile_runs_inside_one_provider_with_separated_models() -> None:
    contract = load_role_contract(ROLES_FILE)
    roles = resolve_roles(contract, "gemini_separated")
    strength = contract["model_strength_rank"]

    assert {row["provider"] for row in roles.values()} == {"gemini"}
    writer = roles[WRITER_ROLE]
    for role in JUDGE_ROLES:
        assert roles[role]["model"] != writer["model"]
    best = max(strength[roles[role]["model"]] for role in JUDGE_ROLES)
    for role in STRONGEST_JUDGE_ROLES:
        assert strength[roles[role]["model"]] == best
    assert roles["answer_judge"]["model"] == "gemini-3.1-flash-lite"


def test_a_mixed_provider_profile_still_keeps_judges_out_of_the_writer_family(
    tmp_path: Path,
) -> None:
    contract = json.loads(ROLES_FILE.read_text(encoding="utf-8"))
    profile = contract["profiles"]["strongest"]
    profile["reconstructor"] = {"provider": "claude", "model": "claude-sonnet-5"}
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(ValueError, match="inside the writer family"):
        load_role_contract(path)


def test_the_launch_profile_matches_the_broker_stage_models() -> None:
    """The broker must serve exactly the models the profile names, per role."""
    from arctic_qa.broker_provider import ROLE_STAGES

    contract = load_role_contract(ROLES_FILE)
    roles = resolve_roles(contract, "gemini_separated")
    config = _config(PRICE_CONFIG)
    for role, assignment in roles.items():
        stage = ROLE_STAGES[role]
        assert model_config_for_stage(config, stage)["model"] == assignment["model"]


# Run blocker 1: judge stage models with verified pricing.


def test_price_config_v7_pins_the_judge_model_and_its_price() -> None:
    config = _config(PRICE_CONFIG)

    assert config["config_id"] == "arctic-gemini-eligibility-r1-config-v7"
    assert config["model"] == "gemini-3.8-flash"
    for stage in PRO_JUDGE_STAGES:
        row = config["stage_models"][stage]
        assert row["model"] == "gemini-3.1-pro-preview"
        assert row["input_usd_per_million_tokens"] == "2.00"
        assert row["output_usd_per_million_tokens_including_thinking"] == "12.00"
        assert row["maximum_input_tokens"] == 200_000
        assert row["maximum_output_tokens"] == 8192
        assert row["thinking_level"] == "low"
        assert row["call_timeout_seconds"] == 300
    assert model_config_for_stage(config, "eligibility")["model"] == "gemini-3.8-flash"
    assert model_config_for_stage(config, "question_generation")["model"] == (
        "gemini-3.8-flash"
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("input_usd_per_million_tokens", "1.00", "judge model configuration changed"),
        ("maximum_input_tokens", 1_048_576, "judge model configuration changed"),
        ("thinking_level", "high", "judge model configuration changed"),
        ("price_valid_through", "2026-09-01", "pricing is not active"),
    ],
)
def test_a_changed_judge_price_record_is_refused(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    config = json.loads(PRICE_CONFIG.read_text(encoding="utf-8"))
    config["stage_models"]["standalone_verification"][field] = value
    path = tmp_path / "config.json"
    write_json(path, config)

    with pytest.raises(ValueError, match=match):
        _config(path)


def test_a_missing_judge_stage_is_refused(tmp_path: Path) -> None:
    config = json.loads(PRICE_CONFIG.read_text(encoding="utf-8"))
    config["stage_models"].pop("option_verification")
    path = tmp_path / "config.json"
    write_json(path, config)

    with pytest.raises(ValueError, match="stage model registry changed"):
        _config(path)


# The chapter 2 budget ceiling: USD 33.994972 spent before, USD 75.00 added.


def test_the_chapter_two_ceiling_is_the_only_new_allowed_ceiling(
    tmp_path: Path,
) -> None:
    assert CHAPTER2_CUMULATIVE_CEILING_USD == Decimal("33.994972") + Decimal("75")
    assert CHAPTER2_BUDGET_EXTENSION_CHANGE in POLICY_TRANSITION_CHANGES
    policy = json.loads(POLICY_V1.read_text(encoding="utf-8"))
    policy["away_session_total_ceiling_usd"] = "108.994972"
    path = tmp_path / "policy.json"
    write_json(path, policy)
    assert _validate_policy(path)["away_session_total_ceiling_usd"] == "108.994972"

    policy["away_session_total_ceiling_usd"] = "110.00"
    write_json(path, policy)
    with pytest.raises(ValueError, match="away_session_total_ceiling_usd"):
        _validate_policy(path)


def _production_broker(tmp_path: Path) -> dict:
    """Replay the reviewed chain up to the first production ceiling."""
    values = prepared_ceiling_extension(tmp_path)
    ten_dollar = SharedGeminiBroker(
        policy_file=values["budget_policy"],
        price_config_file=PRICE_CONFIG,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=values["ten_dollar_transition"],
    )
    predecessor = ten_dollar.status()["config_transition_sha256"]
    twenty_value = json.loads(values["budget_policy"].read_text(encoding="utf-8"))
    twenty_value["live_test_suballocation_usd"] = "20.00"
    twenty_policy = tmp_path / "streaming-dataset-budget-policy-twenty.json"
    write_json(twenty_policy, twenty_value)
    twenty_transition = reviewed_policy_transition(
        tmp_path,
        values,
        twenty_policy,
        from_config_transition_sha256=predecessor,
        from_policy=values["budget_policy"],
        changed_policy_fields={
            "live_test_suballocation_usd": {"from": "10.00", "to": "20.00"}
        },
        maximum_authorized_cumulative_tranche_usd="20.00",
    )
    twenty = SharedGeminiBroker(
        policy_file=twenty_policy,
        price_config_file=PRICE_CONFIG,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=twenty_transition,
    )
    predecessor = twenty.status()["config_transition_sha256"]
    stream_binding = {
        **values["stream_binding"],
        "phase": "away_production",
        "run_id": "production-run-1",
        "campaign_id": "production-campaign-1",
    }
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate.update(
        {
            "allowed_phase": "away_production",
            "authorized_new_run_id": stream_binding["run_id"],
            "authorized_campaign_id": stream_binding["campaign_id"],
        }
    )
    write_json(values["gate"], gate)
    production_value = dict(twenty_value)
    production_value["away_session_total_ceiling_usd"] = "61.614496"
    production_policy = tmp_path / "streaming-dataset-budget-policy-production.json"
    write_json(production_policy, production_value)
    production_transition = reviewed_policy_transition(
        tmp_path,
        values,
        production_policy,
        from_config_transition_sha256=predecessor,
        from_policy=twenty_policy,
        changed_policy_fields={
            "away_session_total_ceiling_usd": {"from": "25.00", "to": "61.614496"}
        },
        maximum_authorized_cumulative_tranche_usd="61.614496",
        reason="Authorize only the first production campaign cumulative ceiling.",
    )
    production = SharedGeminiBroker(
        policy_file=production_policy,
        price_config_file=PRICE_CONFIG,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=HighCostTransport(),
        config_transition_file=production_transition,
    )
    production.bind_stream_input(**stream_binding)
    return {
        **values,
        "production": production,
        "production_policy": production_policy,
        "production_value": production_value,
        "stream_binding": stream_binding,
    }


def _price_transition(
    tmp_path: Path,
    values: dict,
    *,
    from_config: Path,
    to_config: Path,
    predecessor: str,
    policy: Path,
) -> Path:
    identity = tmp_path / ".shared-ledger.json.identity.json"
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    review = Path(gate["review_record"])
    authorization = {
        "schema": "shared-paid-call-config-transition-v3",
        "ledger_file": str(values["ledger"].resolve()),
        "from_price_config_sha256": sha256_file(from_config),
        "to_price_config_sha256": sha256_file(to_config),
        "from_config_transition_sha256": predecessor,
        "from_policy_file": str(policy.resolve()),
        "from_policy_sha256": sha256_file(policy),
        "to_policy_sha256": sha256_file(policy),
        "expected_ledger_sha256": sha256_file(values["ledger"]),
        "expected_identity_sha256": sha256_file(identity),
        "execution_gate_sha256": sha256_file(values["gate"]),
        "integrated_code_commit": "fixture-commit",
        "review_record": str(review),
        "review_record_sha256": sha256_file(review),
        "reason": "Add the chapter 2 judge stage models with verified pricing.",
        "authorized_at_utc": "2026-09-15T16:00:00Z",
    }
    transition = tmp_path / "private" / "price-transition-v3.json"
    write_json(transition, authorization)
    return transition


def test_the_chapter_two_chain_adds_judge_pricing_then_the_ceiling(
    tmp_path: Path,
) -> None:
    """The deployment chain: a v3 price transition, then the v2 ceiling."""
    values = _production_broker(tmp_path)
    production = values["production"]
    high_cost = payload()
    high_cost["generationConfig"]["maxOutputTokens"] = 8_192
    assert (
        execute(
            production,
            phase="away_production",
            run_id="production-run-1",
            paper="p3",
            body=high_cost,
        )["state"]
        == "completed"
    )
    predecessor = production.status()["config_transition_sha256"]

    # Step 1: the price config gains the judge stages (a distinct file hash).
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
    assert predecessor

    # Step 2: the ceiling moves by exactly the chapter 2 allocation.
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
        changed_policy_fields=CHAPTER2_BUDGET_EXTENSION_CHANGE,
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
    chapter2.bind_stream_input(**values["stream_binding"])
    status = chapter2.status()
    assert status["limits"]["away_session_total_ceiling_usd"] == "108.994972"
    assert Decimal(status["remaining"]["away_session_usd"]) == (
        Decimal("108.994972") - Decimal(status["spent_usd"])
    )
    # The live-test cap is not narrowed by a production ceiling change.
    assert status["limits"]["authorized_live_test_ceiling_usd"] in (None, "10.00")

    # A wrong tranche is refused.
    wrong = reviewed_policy_transition(
        tmp_path,
        values,
        chapter2_policy,
        active_config=chapter2_config,
        from_config_transition_sha256=predecessor,
        from_policy=values["production_policy"],
        changed_policy_fields=CHAPTER2_BUDGET_EXTENSION_CHANGE,
        maximum_authorized_cumulative_tranche_usd="75.00",
    )
    with pytest.raises(ValueError, match="transition"):
        SharedGeminiBroker(
            policy_file=chapter2_policy,
            price_config_file=chapter2_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=HighCostTransport(),
            config_transition_file=wrong,
        ).status()


# The live export pin.


def test_the_live_export_selects_the_chapter_two_contract() -> None:
    selection = json.loads(
        (ROOT / "config" / "live-dataset-current-contract-v1.json").read_text(
            encoding="utf-8"
        )
    )
    # The live export still selects the chapter 2 contract. Chapter 3 bumps the
    # generation prompt to v23 and schema 2.8.0; the export moves only with a
    # new captain instruction, after a chapter 3 run has items to show.
    assert selection["candidate_schema_version"] == "2.7.0"
    assert (
        selection["generation_prompt_version"]
        == validation.PREDECESSOR_GENERATION_PROMPT_VERSION
    )
    assert validation.PREDECESSOR_GENERATION_PROMPT_VERSION == (
        "arctic-qa-generation-v22"
    )
    assert validation.CANDIDATE_CONTRACTS["2.7.0"]["prompt_version"] == (
        "arctic-qa-generation-v22"
    )
    assert validation.CANDIDATE_CONTRACTS["2.6.0"]["prompt_version"] == (
        "arctic-qa-generation-v21"
    )


# The gate-bindable chapter 2 streaming input.


def test_the_chapter_two_stream_input_satisfies_the_gate_binding(corpus) -> None:  # noqa: F811
    reextract(corpus["root"], access_run_dir=corpus["access"], jobs=1)
    freeze(
        corpus["root"],
        access_run_dir=corpus["access"],
        legacy_freeze_dir=corpus["legacy"],
        freeze_id="chapter2-test-r1",
        run_id="chapter2-test",
        code_commit="test",
    )
    result = materialize_stream_input(
        corpus["root"], freeze_id="chapter2-test-r1", run_id="chapter2-test-stream"
    )
    stream_dir = corpus["root"] / STREAM_INPUT_DIRECTORY / "chapter2-test-stream"
    freeze_dir = corpus["root"] / "corpus-freeze" / "chapter2-test-r1"
    descriptor = json.loads((freeze_dir / "manifest-descriptor.json").read_text())
    manifest = json.loads((stream_dir / "run-manifest.json").read_text())
    receipt = json.loads((stream_dir / "run-receipt.json").read_text())

    assert result["frozen_manifest_sha256"] == descriptor["manifest_sha256"]
    assert result["order_sha256"] == descriptor["order_sha256"]
    assert manifest["frozen_manifest_sha256"] == descriptor["manifest_sha256"]
    assert manifest["remaining_order_sha256"] == descriptor["order_sha256"]
    assert receipt["run_manifest_sha256"] == sha256_file(
        stream_dir / "run-manifest.json"
    )
    assert receipt["state"] == "completed" and receipt["paid_calls"] == 0
    items = sorted((stream_dir / "items").glob("item-*.json"))
    assert len(items) == manifest["target_total"] == descriptor["record_count"]
    assert all(
        json.loads(path.read_text())["run_id"] == "chapter2-test-stream"
        for path in items
    )

    gate = {
        "continuation_input_binding_version": "stream-input-binding-v1",
        "continuation_artifact": str(stream_dir),
        "continuation_access_run_id": "chapter2-test-stream",
        "continuation_run_manifest_sha256": result["run_manifest_sha256"],
        "continuation_run_receipt_sha256": result["run_receipt_sha256"],
        "continuation_frozen_manifest_sha256": result["frozen_manifest_sha256"],
        "continuation_order_sha256": result["order_sha256"],
        "continuation_family_count": result["target_total"],
        "authorized_new_run_id": "chapter2-run",
        "authorized_campaign_id": "arctic-qa-production-campaign-002",
        "eligibility_prompt_sha256": sha256_file(
            ROOT / "config" / "gemini-eligibility-prompt-v7.txt"
        ),
        "eligibility_schema_sha256": sha256_file(
            ROOT / "schemas" / "gemini-eligibility.v3.schema.json"
        ),
        "eligibility_policy_sha256": sha256_file(
            ROOT / "config" / "arctic-eligibility-policy-v3.json"
        ),
    }
    binding = {
        "access_run_dir": stream_dir,
        "run_id": "chapter2-run",
        "campaign_id": "arctic-qa-production-campaign-002",
        "eligibility_prompt_file": ROOT / "config" / "gemini-eligibility-prompt-v7.txt",
        "eligibility_schema_file": ROOT
        / "schemas"
        / "gemini-eligibility.v3.schema.json",
        "eligibility_policy_file": ROOT
        / "config"
        / "arctic-eligibility-policy-v3.json",
    }
    shim = SimpleNamespace(
        _validate_stream_input_gate=SharedGeminiBroker._validate_stream_input_gate
    )
    assert (
        SharedGeminiBroker._validate_stream_input_binding(shim, gate, binding)
        is binding
    )
    # The write is idempotent and verifies what it stored.
    again = materialize_stream_input(
        corpus["root"], freeze_id="chapter2-test-r1", run_id="chapter2-test-stream"
    )
    assert again == result
