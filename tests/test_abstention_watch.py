from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.abstention_cost import (
    CostJournal,
    cost_row,
    family_generation_cost,
    gemini_evaluation_cost,
    generation_cost_record,
    list_price_equivalent_usd,
    load_list_prices,
    model_metrics,
    subscription_evaluation_cost,
    summarize_journal,
)
from arctic_qa.abstention_plan import (
    PLAN_SUMMARY_FILENAME,
    VendorRun,
    parse_pause_models,
)
from arctic_qa.abstention_providers import (
    PROVIDER_GOOGLE_GEMINI,
    ScriptedEvaluationProvider,
)
from arctic_qa.abstention_subscription import (
    PROVIDER_ANTHROPIC_CLAUDE_CODE,
    PROVIDER_OPENAI_CODEX,
)
from arctic_qa.abstention_watch import (
    WATCH_STATE_FILENAME,
    authorization_record,
    estimated_gemini_item_usd,
    is_ceiling_reason,
    is_item_scoped_reason,
    pending_item_ids,
    validate_authorization,
    vendor_stop_reason,
    watch,
)
from arctic_qa.cli import main as cli_main
from arctic_qa.model_broker import (
    EVALUATION_CEILING_REASON,
    EVALUATION_ITEM_REPEAT_REASON,
    EVALUATION_PHASE,
)
from arctic_qa.util import atomic_json
from test_abstention_render import _candidate, _state_db, DISTRACTORS


ROOT = Path(__file__).parents[1]
PLAN_FILE = ROOT / "config" / "benchmark-evaluation-plan-high-v1.json"
POLICY_V2 = ROOT / "config" / "benchmark-evaluation-policy-v2.json"
PRICES = ROOT / "config" / "benchmark-evaluation-prices-v1.json"
MODELS_FILE = ROOT / "config" / "benchmark-evaluation-subscription-models-v1.json"
LIST_PRICES = ROOT / "config" / "benchmark-evaluation-list-prices-v1.json"
CH3_CONTRACT = ROOT / "config" / "live-dataset-current-contract-v1.json"
CH2_CONTRACT = ROOT / "config" / "abstention-eval-chapter2-contract-v1.json"
CAMPAIGN = "arctic-qa-production-campaign-003"
OLD_CAMPAIGN = "arctic-qa-production-campaign-002"


def state_db(tmp_path: Path, *, chapter3: list[str], chapter2: list[str] = ()) -> Path:
    """One state database with accepted chapter 3 and chapter 2 items."""
    rows = []
    for index, item_id in enumerate(chapter3):
        rows.append(
            (
                _candidate(
                    item_id,
                    schema="2.8.0",
                    prompt="arctic-qa-generation-v23",
                    distractors=DISTRACTORS,
                    order=True,
                ),
                CAMPAIGN,
                f"family-{item_id}",
                [True] * 5,
                f"2026-09-16T0{index}:00:00",
            )
        )
    for index, item_id in enumerate(chapter2 or []):
        rows.append(
            (
                _candidate(
                    item_id,
                    schema="2.7.0",
                    prompt="arctic-qa-generation-v22",
                    distractors=DISTRACTORS[:4],
                    order=False,
                ),
                OLD_CAMPAIGN,
                f"family-{item_id}",
                [True] * 4,
                f"2026-09-15T0{index}:00:00",
            )
        )
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "state.sqlite3"
    _state_db(path, rows)
    return path


def construction_ledger(tmp_path: Path, families: dict[str, list[str]]) -> Path:
    """A minimal construction ledger: completed calls per paper family."""
    requests: dict[str, dict] = {}
    for index, (family, costs) in enumerate(families.items()):
        for number, cost in enumerate(costs):
            key = f"{index:02d}{number:02d}" + "0" * 60
            requests[key] = {
                "request_key": key,
                "run_id": "chapter3-7dc6485-r1",
                "phase": "away_production",
                "stage": "question_generation",
                "family_id": family,
                "paper_id": family.replace("family-", "paper-"),
                "state": "completed",
                "actual_cost_usd": cost,
                "usage": {
                    "promptTokenCount": 1000,
                    "candidatesTokenCount": 100,
                    "thoughtsTokenCount": 50,
                },
            }
    ledger = {
        "schema": "shared-paid-call-ledger-v1",
        "requests": requests,
        "papers": {
            family: {"spent_usd": str(sum(Decimal(c) for c in costs))}
            for family, costs in families.items()
        },
    }
    path = tmp_path / "shared-paid-call-ledger.json"
    atomic_json(path, ledger)
    return path


def authorization(
    tmp_path: Path,
    db: Path,
    *,
    maximum_items: int = 5,
    maximum_gemini_usd: str = "1.00",
    verdict: str = "pass",
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    review = tmp_path / "streaming-review.md"
    review.write_text("The captain authorized the streaming evaluation.\n")
    record = authorization_record(
        contract_file=CH3_CONTRACT,
        plan_file=PLAN_FILE,
        evaluation_policy_file=POLICY_V2,
        evaluation_price_config_file=PRICES,
        subscription_models_file=MODELS_FILE,
        state_db=db,
        campaign_id=CAMPAIGN,
        run_id_prefix="abstention-stream-test",
        maximum_items=maximum_items,
        maximum_gemini_usd=maximum_gemini_usd,
        integrated_code_commit="test-commit",
        review_record=review,
        review_verdict=verdict,
    )
    path = tmp_path / "streaming-authorization.json"
    atomic_json(path, record)
    return path


def scripted_watch(
    *,
    db: Path,
    work_dir: Path,
    ledger_file: Path,
    authorization_file: Path,
    policy: str = "gold",
    **changes,
) -> dict:
    """Run the watcher with a scripted provider for every vendor."""
    import arctic_qa.abstention_watch as module

    def fake_build(*, plan, set_dir, run_id, gate_dir, vendors, **_: object):
        return {
            vendor: VendorRun(
                vendor=vendor,
                provider=ScriptedEvaluationProvider(policy=policy, seed=run_id),
                decoding={"scripted": True, "vendor": vendor},
                models=plan["vendors"][vendor]["models"],
                concurrency=2,
            )
            for vendor in vendors
        }

    original = module.build_vendor_runs
    module.build_vendor_runs = fake_build  # type: ignore[assignment]
    try:
        return watch(
            authorization_file=authorization_file,
            plan_file=PLAN_FILE,
            contract_file=CH3_CONTRACT,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            state_db=db,
            work_dir=work_dir,
            shared_ledger_file=ledger_file,
            broker_factory=None,
            subscription_ledger_root=work_dir / "subscription",
            list_price_file=LIST_PRICES,
            poll_seconds=5,
            once=True,
            code_commit="test-commit",
            ledger_run_prefixes=("chapter3-",),
            backfill_contract_file=CH2_CONTRACT,
            **changes,
        )
    finally:
        module.build_vendor_runs = original  # type: ignore[assignment]


def test_authorization_binds_contract_plan_policy_and_commit(tmp_path: Path) -> None:
    db = state_db(tmp_path, chapter3=["aqa-one"])
    path = authorization(tmp_path, db)
    record = validate_authorization(
        path,
        plan_file=PLAN_FILE,
        contract_file=CH3_CONTRACT,
        evaluation_policy_file=POLICY_V2,
        evaluation_price_config_file=PRICES,
        subscription_models_file=MODELS_FILE,
        code_commit="test-commit",
    )
    assert record["trials_per_item"] == 48 and record["k"] == 4
    assert record["contract"]["generation_prompt_version"] == "arctic-qa-generation-v23"
    with pytest.raises(ValueError, match="another code commit"):
        validate_authorization(
            path,
            plan_file=PLAN_FILE,
            contract_file=CH3_CONTRACT,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            code_commit="other",
        )
    with pytest.raises(ValueError, match="another evaluation_policy_sha256"):
        validate_authorization(
            path,
            plan_file=PLAN_FILE,
            contract_file=CH3_CONTRACT,
            evaluation_policy_file=ROOT
            / "config"
            / "benchmark-evaluation-policy-v1.json",
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            code_commit="test-commit",
        )
    with pytest.raises(ValueError, match="another contract"):
        validate_authorization(
            path,
            plan_file=PLAN_FILE,
            contract_file=CH2_CONTRACT,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            code_commit="test-commit",
        )
    pending = authorization(tmp_path / "pending", db, verdict="pending")
    with pytest.raises(ValueError, match="disabled"):
        validate_authorization(
            pending,
            plan_file=PLAN_FILE,
            contract_file=CH3_CONTRACT,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            code_commit="test-commit",
        )


def test_watcher_selects_the_current_contract_and_ignores_chapter_2(
    tmp_path: Path,
) -> None:
    db = state_db(tmp_path, chapter3=["aqa-c3a", "aqa-c3b"], chapter2=["aqa-c2a"])
    current = pending_item_ids(
        state_db=db,
        campaign_id=CAMPAIGN,
        contract=json.loads(CH3_CONTRACT.read_text()),
        exclude=set(),
    )
    assert current == ["aqa-c3a", "aqa-c3b"]
    assert pending_item_ids(
        state_db=db,
        campaign_id=CAMPAIGN,
        contract=json.loads(CH3_CONTRACT.read_text()),
        exclude={"aqa-c3a"},
    ) == ["aqa-c3b"]
    backfill = pending_item_ids(
        state_db=db,
        campaign_id=None,
        contract=json.loads(CH2_CONTRACT.read_text()),
        exclude=set(),
    )
    assert backfill == ["aqa-c2a"]


def test_watcher_idles_evaluates_and_never_repeats_a_finished_question(
    tmp_path: Path,
) -> None:
    db = state_db(tmp_path, chapter3=[], chapter2=["aqa-old"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-c3a": ["0.10", "0.20"]})
    auth = authorization(tmp_path, db)
    work = tmp_path / "work"
    idle = scripted_watch(
        db=db, work_dir=work, ledger_file=ledger_file, authorization_file=auth
    )
    assert idle["items_this_invocation"] == []
    assert idle["summary"]["accepted_items_evaluated"] == 0
    assert not (work / "cost-journal.jsonl").is_file()
    assert json.loads((work / WATCH_STATE_FILENAME).read_text())["polls"] == 1
    # One chapter 3 item lands.
    db = state_db(tmp_path / "second", chapter3=["aqa-c3a"], chapter2=["aqa-old"])
    auth = authorization(tmp_path / "second", db)
    first = scripted_watch(
        db=db, work_dir=work, ledger_file=ledger_file, authorization_file=auth
    )
    assert [row["item_id"] for row in first["items_this_invocation"]] == ["aqa-c3a"]
    journal = CostJournal(work)
    rows = journal.item_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["evaluation"]["recorded_trials"] == 48
    assert row["evaluation"]["planned_trials"] == 48
    assert row["complete"] is True
    assert row["generation"]["family"]["paid_calls"] == 2
    assert row["generation"]["family"]["usd"] == "0.300000"
    assert row["generation"]["campaign_accepted_items"] == 1
    assert row["generation"]["campaign_usd_per_accepted_item"] == "0.300000"
    assert set(row["outcomes_by_model"]) == {
        "claude-fable-5-1",
        "claude-opus-5",
        "claude-sonnet-5",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-6-astra",
    }
    for counts in row["outcomes_by_model"].values():
        assert counts["N1"] == 3 and counts["N5"] == 3
    # The scripted provider is not a subscription harness, so the token
    # counts are present and the charged USD is zero.
    assert row["evaluation"]["charged_usd"] == "0.000000"
    assert row["evaluation"]["subscription_tokens"]["input"] > 0
    # A restart re-reads the journal and re-runs nothing.
    second = scripted_watch(
        db=db, work_dir=work, ledger_file=ledger_file, authorization_file=auth
    )
    assert second["items_this_invocation"] == []
    assert len(CostJournal(work).item_rows()) == 1
    run_dir = Path(row["run_dir"])
    assert json.loads((run_dir / PLAN_SUMMARY_FILENAME).read_text())["complete"] is True
    assert (Path(row["gate_dir"]) / "google_gemini.json").is_file()
    gate = json.loads((Path(row["gate_dir"]) / "openai_codex.json").read_text())
    assert gate["review_record"] == str(auth.resolve())
    assert gate["evaluation_enabled"] is True and gate["arms"] == ["high"]


def test_backfill_is_off_by_default_and_evaluates_chapter_2_when_asked(
    tmp_path: Path,
) -> None:
    db = state_db(tmp_path, chapter3=["aqa-c3a"], chapter2=["aqa-c2a"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-c3a": ["0.05"]})
    auth = authorization(tmp_path, db)
    off = scripted_watch(
        db=db,
        work_dir=tmp_path / "off",
        ledger_file=ledger_file,
        authorization_file=auth,
    )
    assert [row["item_id"] for row in off["items_this_invocation"]] == ["aqa-c3a"]
    on = scripted_watch(
        db=db,
        work_dir=tmp_path / "on",
        ledger_file=ledger_file,
        authorization_file=auth,
        backfill=True,
    )
    assert sorted(row["item_id"] for row in on["items_this_invocation"]) == [
        "aqa-c2a",
        "aqa-c3a",
    ]
    # A vendor subset runs the rest of the plan and journals the others as
    # paused, which is how the evaluator runs while one arm waits for a gate.
    subset = scripted_watch(
        db=db,
        work_dir=tmp_path / "subset",
        ledger_file=ledger_file,
        authorization_file=auth,
        vendors=[PROVIDER_ANTHROPIC_CLAUDE_CODE, PROVIDER_OPENAI_CODEX],
    )
    assert subset["active_vendors"] == [
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        PROVIDER_OPENAI_CODEX,
    ]
    row = CostJournal(tmp_path / "subset").item_rows()[0]
    assert row["evaluation"]["recorded_trials"] == 36
    assert row["evaluation"]["vendors_paused"] == [PROVIDER_GOOGLE_GEMINI]
    with pytest.raises(ValueError, match="no vendor"):
        scripted_watch(
            db=db,
            work_dir=tmp_path / "bogus",
            ledger_file=ledger_file,
            authorization_file=auth,
            vendors=["nope"],
        )


def test_item_bound_stops_the_watcher(tmp_path: Path) -> None:
    db = state_db(tmp_path, chapter3=["aqa-a", "aqa-b", "aqa-c"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=2)
    result = scripted_watch(
        db=db,
        work_dir=tmp_path / "bounded",
        ledger_file=ledger_file,
        authorization_file=auth,
    )
    assert len(result["items_this_invocation"]) == 2
    again = scripted_watch(
        db=db,
        work_dir=tmp_path / "bounded",
        ledger_file=ledger_file,
        authorization_file=auth,
    )
    assert again["items_this_invocation"] == []


def test_ceiling_pause_keeps_the_subscription_vendors_running(tmp_path: Path) -> None:
    db = state_db(tmp_path, chapter3=["aqa-a"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    # A USD bound below one item's Gemini estimate pauses Gemini at once.
    auth = authorization(tmp_path, db, maximum_gemini_usd="0.0001")
    work = tmp_path / "paused"

    import arctic_qa.abstention_watch as module

    def fake_build(*, plan, set_dir, run_id, gate_dir, vendors, **_: object):
        assert PROVIDER_GOOGLE_GEMINI not in vendors
        return {
            vendor: VendorRun(
                vendor=vendor,
                provider=ScriptedEvaluationProvider(policy="abstain"),
                decoding={"scripted": True},
                models=plan["vendors"][vendor]["models"],
                concurrency=2,
            )
            for vendor in vendors
        }

    original = module.build_vendor_runs
    module.build_vendor_runs = fake_build  # type: ignore[assignment]
    try:
        result = watch(
            authorization_file=auth,
            plan_file=PLAN_FILE,
            contract_file=CH3_CONTRACT,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            state_db=db,
            work_dir=work,
            shared_ledger_file=ledger_file,
            broker_factory=lambda gate: None,  # type: ignore[arg-type,return-value]
            subscription_ledger_root=work / "subscription",
            list_price_file=LIST_PRICES,
            poll_seconds=5,
            once=True,
            code_commit="test-commit",
            ledger_run_prefixes=("chapter3-",),
        )
    finally:
        module.build_vendor_runs = original  # type: ignore[assignment]
    assert result["errors"] == []
    assert PROVIDER_GOOGLE_GEMINI in result["paused_vendors"]
    assert result["active_vendors"] == [
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        PROVIDER_OPENAI_CODEX,
    ]
    journal = CostJournal(work)
    pauses = [row for row in journal.rows() if row.get("kind") == "vendor_pause"]
    assert pauses and pauses[0]["vendor"] == PROVIDER_GOOGLE_GEMINI
    row = journal.item_rows()[0]
    assert row["evaluation"]["recorded_trials"] == 36
    assert row["evaluation"]["vendors_paused"] == [PROVIDER_GOOGLE_GEMINI]
    assert row["evaluation"]["google_gemini"]["usd"] == "0.000000"


def test_vendor_stop_reason_reads_every_stop_and_marks_the_budget_wall() -> None:
    ceiling = {
        "vendors": {
            PROVIDER_GOOGLE_GEMINI: {
                "stopped_on": {
                    "state": "not_submitted",
                    "error": EVALUATION_CEILING_REASON,
                }
            }
        }
    }
    reason = vendor_stop_reason(ceiling, PROVIDER_GOOGLE_GEMINI)
    assert reason is not None and EVALUATION_CEILING_REASON in reason
    assert is_ceiling_reason(reason) is True
    assert vendor_stop_reason(ceiling, PROVIDER_OPENAI_CODEX) is None
    harness = {
        "vendors": {
            PROVIDER_ANTHROPIC_CLAUDE_CODE: {
                "stopped_on": {
                    "state": "failed",
                    "error": "the harness exited with None: FileNotFoundError",
                }
            }
        }
    }
    reason = vendor_stop_reason(harness, PROVIDER_ANTHROPIC_CLAUDE_CODE)
    assert reason.startswith("failed: ") and "FileNotFoundError" in reason
    assert is_ceiling_reason(reason) is False
    raised = {"vendors": {PROVIDER_OPENAI_CODEX: {"error": "ValueError: gate"}}}
    assert vendor_stop_reason(raised, PROVIDER_OPENAI_CODEX) == "ValueError: gate"
    assert vendor_stop_reason({"vendors": {}}, PROVIDER_OPENAI_CODEX) is None
    assert is_ceiling_reason(None) is False


def test_a_vendor_that_stops_is_paused_for_the_rest_of_the_watch(
    tmp_path: Path,
) -> None:
    """A harness failure pauses that vendor; the other vendor runs every item."""
    db = state_db(tmp_path, chapter3=["aqa-a", "aqa-b"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=2)
    work = tmp_path / "paused-vendor"

    import arctic_qa.abstention_watch as module

    class BrokenProvider(ScriptedEvaluationProvider):
        """Answer with a harness failure instead of a letter."""

        def answer(self, request):
            from arctic_qa.abstention_providers import EvaluationResponse

            return EvaluationResponse(
                state="failed",
                raw_text=None,
                finish_reason=None,
                usage=None,
                cost_usd=None,
                latency_seconds=0.0,
                request_key=None,
                request_sha256=None,
                receipt_sha256=None,
                receipt_file=None,
                model_version=None,
                response_id=None,
                error="the harness exited with None: FileNotFoundError",
            )

    def fake_build(*, plan, set_dir, run_id, gate_dir, vendors, **_: object):
        runs = {}
        for vendor in vendors:
            provider = (
                BrokenProvider(policy="gold")
                if vendor == PROVIDER_ANTHROPIC_CLAUDE_CODE
                else ScriptedEvaluationProvider(policy="gold")
            )
            runs[vendor] = VendorRun(
                vendor=vendor,
                provider=provider,
                decoding={"scripted": True},
                models=plan["vendors"][vendor]["models"],
                concurrency=1,
            )
        return runs

    original = module.build_vendor_runs
    module.build_vendor_runs = fake_build  # type: ignore[assignment]
    try:
        result = watch(
            authorization_file=auth,
            plan_file=PLAN_FILE,
            contract_file=CH3_CONTRACT,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            state_db=db,
            work_dir=work,
            shared_ledger_file=ledger_file,
            broker_factory=None,
            subscription_ledger_root=work / "subscription",
            list_price_file=LIST_PRICES,
            poll_seconds=5,
            once=True,
            code_commit="test-commit",
            ledger_run_prefixes=("chapter3-",),
            vendors=[PROVIDER_ANTHROPIC_CLAUDE_CODE, PROVIDER_OPENAI_CODEX],
        )
    finally:
        module.build_vendor_runs = original  # type: ignore[assignment]
    assert result["errors"] == []
    assert PROVIDER_ANTHROPIC_CLAUDE_CODE in result["paused_vendors"]
    assert result["active_vendors"] == [PROVIDER_OPENAI_CODEX]
    journal = CostJournal(work)
    pauses = [row for row in journal.rows() if row.get("kind") == "vendor_pause"]
    assert len(pauses) == 1
    assert pauses[0]["vendor"] == PROVIDER_ANTHROPIC_CLAUDE_CODE
    assert "FileNotFoundError" in pauses[0]["reason"]
    rows = journal.item_rows()
    assert len(rows) == 2
    # The first item holds the one failed Claude call plus the Codex arm.
    assert rows[0]["complete"] is False
    assert rows[0]["evaluation"]["recorded_trials"] == 19
    # The second item runs the Codex arm only and is complete.
    assert rows[1]["complete"] is True
    assert rows[1]["evaluation"]["recorded_trials"] == 18
    assert rows[1]["evaluation"]["vendors_paused"] == [
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        PROVIDER_GOOGLE_GEMINI,
    ]


def test_gemini_item_estimate_matches_the_reservation_formula() -> None:
    price_config = json.loads(PRICES.read_text())
    estimate = estimated_gemini_item_usd(
        price_config,
        ["gemini-3.8-flash", "gemini-3.7-flash"],
        ["high"],
        3,
        output_cap=8192,
        assumed_input_tokens=1000,
    )
    # Both models cost 0.75 in and 3.75 out per million; six trials each.
    per_call = (
        Decimal(1000) * Decimal("0.75") + Decimal(8192) * Decimal("3.75")
    ) / Decimal(1000000)
    assert estimate == per_call * 12


# --- Cost journal arithmetic ---------------------------------------------------


def test_list_price_equivalent_uses_the_native_token_split() -> None:
    prices = load_list_prices(LIST_PRICES)
    claude = list_price_equivalent_usd(
        prices,
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        "claude-opus-5",
        {
            "promptTokenCount": 800,
            "candidatesTokenCount": 3,
            "thoughtsTokenCount": 120,
            "native": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 798,
                "cache_read_input_tokens": 0,
                "output_tokens": 123,
            },
        },
    )
    # 2 x 5.00 + 798 x 6.25 + 123 x 25.00, per million.
    expected = (
        Decimal(2) * Decimal("5.00")
        + Decimal(798) * Decimal("6.25")
        + Decimal(123) * Decimal("25.00")
    ) / Decimal(1000000)
    assert claude == expected
    codex = list_price_equivalent_usd(
        prices,
        PROVIDER_OPENAI_CODEX,
        "gpt-5.6-terra",
        {
            "promptTokenCount": 2400,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 180,
            "native": {
                "input_tokens": 2400,
                "cached_input_tokens": 400,
                "output_tokens": 200,
            },
        },
    )
    expected_codex = (
        Decimal(2000) * Decimal("2.00")
        + Decimal(400) * Decimal("0.20")
        + Decimal(200) * Decimal("12.00")
    ) / Decimal(1000000)
    assert codex == expected_codex
    # No native block: the normalized counts are used instead.
    fallback = list_price_equivalent_usd(
        prices,
        PROVIDER_OPENAI_CODEX,
        "gpt-5.6-terra",
        {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 10,
            "thoughtsTokenCount": 90,
        },
    )
    assert fallback == (
        Decimal(1000) * Decimal("2.00") + Decimal(100) * Decimal("12.00")
    ) / Decimal(1000000)
    assert (
        list_price_equivalent_usd(prices, PROVIDER_OPENAI_CODEX, "unknown", {}) is None
    )


def _response_row(model: str, *, vendor: str, outcome: str, usage: dict) -> dict:
    return {
        "model": model,
        "arm": "high",
        "item_id": "aqa-a",
        "condition": "gold_absent",
        "outcome": outcome,
        "valid": outcome != "N0",
        "response": {"state": "completed", "cost_usd": "0", "usage": usage},
    }


def test_cost_row_and_summary_arithmetic(tmp_path: Path) -> None:
    prices = load_list_prices(LIST_PRICES)
    ledger = {
        "requests": {
            "a": {
                "phase": EVALUATION_PHASE,
                "family_id": "evaluation-item:aqa-a",
                "run_id": "run-1",
                "state": "completed",
                "actual_cost_usd": "0.030000",
                "usage": {
                    "promptTokenCount": 900,
                    "candidatesTokenCount": 1,
                    "thoughtsTokenCount": 700,
                },
            },
            "b": {
                "phase": EVALUATION_PHASE,
                "family_id": "evaluation-item:aqa-a",
                "run_id": "run-1",
                "state": "completed",
                "actual_cost_usd": "0.010000",
                "usage": {
                    "promptTokenCount": 900,
                    "candidatesTokenCount": 1,
                    "thoughtsTokenCount": 200,
                },
            },
            "c": {
                "phase": EVALUATION_PHASE,
                "family_id": "evaluation-item:aqa-other",
                "run_id": "run-1",
                "state": "completed",
                "actual_cost_usd": "9.99",
            },
            "d": {
                "phase": "away_production",
                "family_id": "family-aqa-a",
                "run_id": "chapter3-x",
                "state": "completed",
                "actual_cost_usd": "0.250000",
                "usage": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 2,
                    "thoughtsTokenCount": 1,
                },
            },
            "e": {
                "phase": "away_production",
                "family_id": "family-other",
                "run_id": "chapter3-x",
                "state": "completed",
                "actual_cost_usd": "0.750000",
                "usage": {},
            },
            "f": {
                "phase": "away_production",
                "family_id": "family-aqa-a",
                "run_id": "other-campaign",
                "state": "completed",
                "actual_cost_usd": "5.00",
                "usage": {},
            },
        },
        "papers": {"family-aqa-a": {"spent_usd": "5.250000"}},
    }
    gemini = gemini_evaluation_cost(ledger, run_id="run-1", item_id="aqa-a")
    assert gemini["charged_calls"] == 2 and gemini["usd"] == "0.040000"
    assert gemini["tokens"]["thinking"] == 900
    family = family_generation_cost(ledger, "family-aqa-a", run_prefixes=("chapter3-",))
    assert family["paid_calls"] == 1 and family["usd"] == "0.250000"
    assert family["ledger_paper_spent_usd"] == "5.250000"
    db = state_db(tmp_path, chapter3=["aqa-a", "aqa-b"])
    generation = generation_cost_record(
        ledger=ledger,
        state_db=db,
        campaign_id=CAMPAIGN,
        family_id="family-aqa-a",
        run_prefixes=("chapter3-",),
    )
    assert generation["campaign_spent_usd"] == "1.000000"
    assert generation["campaign_accepted_items"] == 2
    assert generation["campaign_usd_per_accepted_item"] == "0.500000"
    claude_usage = {
        "promptTokenCount": 800,
        "candidatesTokenCount": 3,
        "thoughtsTokenCount": 120,
        "native": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 798,
            "cache_read_input_tokens": 0,
            "output_tokens": 123,
        },
    }
    rows_by_vendor = {
        PROVIDER_GOOGLE_GEMINI: [
            _response_row(
                "gemini-3.8-flash",
                vendor=PROVIDER_GOOGLE_GEMINI,
                outcome="N5",
                usage={
                    "promptTokenCount": 900,
                    "candidatesTokenCount": 1,
                    "thoughtsTokenCount": 700,
                },
            ),
            _response_row(
                "gemini-3.7-flash",
                vendor=PROVIDER_GOOGLE_GEMINI,
                outcome="N0",
                usage={
                    "promptTokenCount": 900,
                    "candidatesTokenCount": 1,
                    "thoughtsTokenCount": 200,
                },
            ),
        ],
        PROVIDER_ANTHROPIC_CLAUDE_CODE: [
            _response_row(
                "claude-opus-5",
                vendor=PROVIDER_ANTHROPIC_CLAUDE_CODE,
                outcome="N5",
                usage=claude_usage,
            ),
            _response_row(
                "claude-opus-5",
                vendor=PROVIDER_ANTHROPIC_CLAUDE_CODE,
                outcome="N4",
                usage=claude_usage,
            ),
        ],
    }
    subscription = subscription_evaluation_cost(
        rows_by_vendor[PROVIDER_ANTHROPIC_CLAUDE_CODE],
        prices,
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
    )
    assert subscription["calls"] == 2 and subscription["charged_usd"] == "0"
    assert subscription["tokens"] == {"input": 1600, "output": 6, "thinking": 240}
    one_call = (
        Decimal(2) * Decimal("5.00")
        + Decimal(798) * Decimal("6.25")
        + Decimal(123) * Decimal("25.00")
    ) / Decimal(1000000)
    assert Decimal(subscription["list_price_equivalent_usd"]) == (
        one_call * 2
    ).quantize(Decimal("0.000001"))
    journal = CostJournal(tmp_path / "journal")
    row = cost_row(
        item={
            "item_id": "aqa-a",
            "family_id": "family-aqa-a",
            "eval_set_id": "set-1",
            "source": None,
        },
        run_id="run-1",
        plan_id="plan-1",
        rows_by_vendor=rows_by_vendor,
        ledger=ledger,
        prices=prices,
        generation=generation,
        wall_seconds=42.5,
        planned_trials=48,
        cumulative=journal.cumulative(),
    )
    assert row["evaluation"]["google_gemini"]["usd"] == "0.040000"
    assert row["evaluation"]["charged_usd"] == "0.040000"
    assert row["evaluation"]["subscription_tokens"] == {
        "input": 1600,
        "output": 6,
        "thinking": 240,
    }
    assert row["invalid_count"] == 1 and row["invalid_rate"] == 0.25
    assert row["cumulative"] == {
        "items": 0,
        "trials": 0,
        "generation_family_usd": "0.000000",
        "evaluation_gemini_usd": "0.000000",
        "subscription_tokens": {"input": 0, "output": 0, "thinking": 0},
        "subscription_list_price_equivalent_usd": "0.000000",
        "wall_seconds": 0.0,
    }
    journal.append(row)
    journal.append({**row, "item_id": "aqa-b", "cumulative": journal.cumulative()})
    summary = summarize_journal(tmp_path / "journal", project_items=500)
    assert summary["accepted_items_evaluated"] == 2
    assert summary["totals"]["evaluation_gemini_usd"] == "0.080000"
    assert summary["per_item"]["evaluation_gemini_usd"] == "0.040000"
    assert summary["per_item"]["generation_family_usd"] == "0.250000"
    assert summary["per_item"]["subscription_tokens"]["input"] == 1600.0
    assert summary["projection"]["items"] == 500
    assert summary["projection"]["evaluation_gemini_usd"] == "20.000000"
    assert summary["projection"]["generation_family_usd"] == "125.000000"
    assert summary["campaign"]["campaign_usd_per_accepted_item"] == "0.500000"
    metrics = summary["per_model"]["claude-opus-5"]
    assert metrics["counts"]["N5"] == 2 and metrics["counts"]["N4"] == 2
    # The scorer's definitions: precision over abstained trials (N5/(N3+N5)),
    # recall over the trials where abstention was the better answer
    # (N5/(N2+N4+N5)).
    assert metrics["recall_abs"] == 0.5 and metrics["precision_abs"] == 1.0
    assert metrics["abstention_rate"] == 0.5 and metrics["invalid_rate"] == 0.0
    assert metrics["valid_trials"] == 4
    assert summary["per_model"]["gemini-3.7-flash"]["invalid_rate"] == 1.0
    assert summary["per_model"]["gemini-3.7-flash"]["acc"] is None


def test_model_metrics_match_the_scorer_on_a_known_tally() -> None:
    from arctic_qa.abstention_score import metrics_from_counts

    counts = {"N0": 2, "N1": 5, "N2": 3, "N3": 2, "N4": 4, "N5": 6}
    mine = model_metrics(counts)
    theirs = metrics_from_counts(counts)
    for name, value in theirs.items():
        assert mine[name] == pytest.approx(value, abs=1e-4), name
    assert mine["invalid_rate"] == pytest.approx(2 / 22, abs=1e-4)
    assert mine["valid_trials"] == 20


def test_cli_cost_summary_reads_the_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = CostJournal(tmp_path / "work")
    journal.append(
        {
            "schema": "abstention-eval-cost-row-v1",
            "kind": "item",
            "item_id": "aqa-a",
            "family_id": "family-a",
            "generation": {
                "campaign_id": CAMPAIGN,
                "family": {"usd": "0.400000", "paid_calls": 3},
                "campaign_spent_usd": "1.200000",
                "campaign_accepted_items": 3,
                "campaign_usd_per_accepted_item": "0.400000",
            },
            "evaluation": {
                "planned_trials": 48,
                "recorded_trials": 48,
                "google_gemini": {"usd": "0.035000"},
                "subscription_tokens": {
                    "input": 30000,
                    "output": 500,
                    "thinking": 4000,
                },
                "subscription_list_price_equivalent_usd": "0.900000",
                "wall_seconds": 120.0,
            },
            "outcomes_by_model": {
                "claude-opus-5": {"N0": 0, "N1": 3, "N2": 0, "N3": 0, "N4": 1, "N5": 2}
            },
            "invalid_count": 0,
            "recorded_at_utc": "2026-09-16T10:00:00Z",
        }
    )
    assert (
        cli_main(
            [
                "--json",
                "--test-mode",
                "--data-root",
                str(tmp_path / "data"),
                "abstention-eval",
                "--action",
                "cost-summary",
                "--work-dir",
                str(tmp_path / "work"),
                "--project-items",
                "400",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["accepted_items_evaluated"] == 1
    assert summary["per_item"]["evaluation_gemini_usd"] == "0.035000"
    assert summary["projection"]["evaluation_gemini_usd"] == "14.000000"
    assert summary["projection"]["subscription_tokens"]["input"] == 12000000
    assert summary["projection"]["wall_hours"] == 13.33
    assert summary["per_model"]["claude-opus-5"]["ssr"] == pytest.approx(
        5 / 6, abs=1e-4
    )


def test_cli_watch_authorization_writes_a_pending_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = state_db(tmp_path, chapter3=["aqa-a"])
    review = tmp_path / "review.md"
    review.write_text("r\n")
    assert (
        cli_main(
            [
                "--json",
                "--test-mode",
                "--data-root",
                str(tmp_path / "data"),
                "abstention-eval",
                "--action",
                "watch-authorization",
                "--state-db",
                str(db),
                "--campaign-id",
                CAMPAIGN,
                "--run-id-prefix",
                "abstention-stream-r1",
                "--contract-file",
                str(CH3_CONTRACT),
                "--plan-file",
                str(PLAN_FILE),
                "--evaluation-policy-file",
                str(POLICY_V2),
                "--evaluation-price-config-file",
                str(PRICES),
                "--subscription-models-file",
                str(MODELS_FILE),
                "--maximum-items",
                "3",
                "--maximum-gemini-usd",
                "0.50",
                "--review-record",
                str(review),
                "--output-file",
                str(tmp_path / "auth.json"),
                "--code-commit",
                "abc1234",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    record = result["authorization"]
    assert record["authorization_enabled"] is False
    assert record["independent_review_verdict"] == "pending"
    assert record["maximum_items"] == 3 and record["maximum_gemini_usd"] == "0.50"
    assert record["trials_per_item"] == 48
    assert record["integrated_code_commit"] == "abc1234"


def test_the_fable_pause_holds_its_trials_and_the_item_is_revisited(
    tmp_path: Path,
) -> None:
    """The per-model pause of the streaming evaluator.

    Captain order 2026-09-16: "pause the fable evaluation because I only have
    ~80% fable usage left today; I will run the fable benchmarking after the
    reset at 6:00pm today". The other seven models run on the item, the six
    Fable trials stay pending, and the item stays open. After the resume time
    the next pass runs only the six held trials and appends a later row that
    supersedes the first.
    """
    db = state_db(tmp_path, chapter3=["aqa-c3a"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-c3a": ["0.10"]})
    auth = authorization(tmp_path, db)
    work = tmp_path / "work"
    held = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        pause_models=parse_pause_models(["claude-fable-5-1=2026-09-16T23:00:00Z"]),
    )
    assert held["paused_models"] == ["claude-fable-5-1"]
    journal = CostJournal(work)
    rows = journal.item_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["evaluation"]["models_paused"] == ["claude-fable-5-1"]
    assert row["evaluation"]["pending_paused_trials"] == 6
    assert row["evaluation"]["recorded_trials"] == 42
    assert row["evaluation"]["complete"] is False and row["complete"] is False
    assert "claude-fable-5-1" not in row["outcomes_by_model"]
    assert len(row["outcomes_by_model"]) == 7
    # The item is not finished, so a later pass takes it up again.
    assert journal.completed_item_ids() == set()
    assert journal.held_item_ids() == {"aqa-c3a"}
    summary = held["summary"]
    assert summary["items_held_by_a_paused_model"] == 1
    assert summary["complete_items"] == 0
    assert summary["paused_models"] == ["claude-fable-5-1"]
    assert summary["pending_paused_trials"] == 6
    # A pass before the resume time leaves the item alone: it can only
    # advance when the pause lifts, so a revisit would record nothing, call
    # nothing and append one more journal row.
    again = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        pause_models=parse_pause_models(["claude-fable-5-1=2026-09-16T23:00:00Z"]),
    )
    assert again["items_this_invocation"] == []
    assert len(CostJournal(work).item_rows()) == 1
    assert (
        CostJournal(work).latest_item_rows()[-1]["evaluation"]["pending_paused_trials"]
        == 6
    )
    # After the resume time the held trials run.
    done = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        pause_models=parse_pause_models(["claude-fable-5-1=2026-09-16T00:00:00Z"]),
    )
    assert done["paused_models"] == []
    journal = CostJournal(work)
    latest = journal.latest_item_rows()
    # One item, whatever the number of passes: the totals never double-count.
    assert len(latest) == 1 and len(journal.item_rows()) == 2
    final = latest[0]
    assert final["evaluation"]["recorded_trials"] == 48
    assert final["evaluation"]["pending_paused_trials"] == 0
    assert final["evaluation"]["complete"] is True and final["complete"] is True
    assert final["outcomes_by_model"]["claude-fable-5-1"]["N1"] == 3
    assert journal.completed_item_ids() == {"aqa-c3a"}
    assert journal.cumulative()["items"] == 1
    assert journal.cumulative()["trials"] == 48
    assert done["summary"]["accepted_items_evaluated"] == 1
    assert done["summary"]["items_held_by_a_paused_model"] == 0
    # The finished item is never evaluated again.
    after = scripted_watch(
        db=db, work_dir=work, ledger_file=ledger_file, authorization_file=auth
    )
    assert after["items_this_invocation"] == []


def test_cli_pause_status_shows_the_shipped_pause_and_the_options(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The read side of the pause interface that a cost guard calls."""
    base = [
        "--json",
        "--test-mode",
        "--data-root",
        str(tmp_path / "data"),
        "abstention-eval",
        "--action",
        "pause-status",
    ]
    assert cli_main(base) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["schema"] == "abstention-eval-pause-status-v1"
    # The committed file holds the captain's standing order. Other entries
    # come and go as an operator or the guard pauses a model, so this asserts
    # the standing one and not the whole list.
    assert "claude-fable-5-1" in status["paused_now"]
    assert (
        status["entries"]["claude-fable-5-1"]["resume_at_utc"] == "2026-09-16T23:00:00Z"
    )
    assert status["pause_files"] == ["config/benchmark-evaluation-model-pause-v1.json"]
    # The command line adds a model, and --no-pause-file drops every file.
    assert cli_main([*base, "--no-pause-file", "--pause-model", "gpt-5.6-sol"]) == 0
    only = json.loads(capsys.readouterr().out)
    assert only["pause_file"] is None and only["pause_files"] == []
    assert only["paused_now"] == ["gpt-5.6-sol"]
    # A cost guard owns its own file; a later file wins for the same model.
    # Both files here belong to the test: the committed file is operational,
    # so its contents must not decide what this merge asserts.
    standing = tmp_path / "standing-model-pause.json"
    atomic_json(
        standing,
        {
            "schema": "benchmark-evaluation-model-pause-v1",
            "paused_models": {
                "claude-fable-5-1": {
                    "reason": "the captain's daily quota",
                    "resume_at_utc": "2026-09-16T23:00:00Z",
                },
                "gemini-3.7-flash": {"reason": "an operator held this model"},
            },
        },
    )
    guard = tmp_path / "guard-model-pause.json"
    atomic_json(
        guard,
        {
            "schema": "benchmark-evaluation-model-pause-v1",
            "paused_models": {
                "gemini-3.8-flash": {
                    "owner": "benchmark-cost-guard",
                    "reason": "the Gemini allocation is nearly spent",
                },
                "claude-fable-5-1": {
                    "owner": "benchmark-cost-guard",
                    "reason": "the Fable window reopened",
                    "resume_at_utc": "2026-09-16T00:00:00Z",
                },
            },
        },
    )
    assert (
        cli_main(
            [
                *base,
                "--pause-file",
                str(standing),
                "--pause-file",
                str(guard),
            ]
        )
        == 0
    )
    merged = json.loads(capsys.readouterr().out)
    # The guard's entry for the same model wins, and its resume time frees it.
    assert merged["paused_now"] == ["gemini-3.7-flash", "gemini-3.8-flash"]
    assert merged["entries"]["claude-fable-5-1"]["owner"] == "benchmark-cost-guard"


def test_the_model_pause_survives_the_gemini_ceiling_check(tmp_path: Path) -> None:
    """The ceiling precheck must not drop the model pause.

    The live run of 2026-09-16 found this: the precheck of the Gemini ceiling
    assigned its vendor-pause record to the same name as the model-pause
    record, so with the Gemini broker active the evaluator called every model,
    the paused one included. The precheck runs only when a broker factory
    exists, which is why the scripted watcher never met it.
    """
    db = state_db(tmp_path, chapter3=["aqa-a"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    # A Gemini USD bound with room, so the precheck returns no pause.
    auth = authorization(tmp_path, db, maximum_gemini_usd="5.00")
    work = tmp_path / "work"

    import arctic_qa.abstention_watch as module

    def fake_build(*, plan, set_dir, run_id, gate_dir, vendors, **_: object):
        assert PROVIDER_GOOGLE_GEMINI in vendors
        return {
            vendor: VendorRun(
                vendor=vendor,
                provider=ScriptedEvaluationProvider(policy="gold", seed=run_id),
                decoding={"scripted": True, "vendor": vendor},
                models=plan["vendors"][vendor]["models"],
                concurrency=2,
            )
            for vendor in vendors
        }

    original = module.build_vendor_runs
    module.build_vendor_runs = fake_build  # type: ignore[assignment]
    try:
        result = watch(
            authorization_file=auth,
            plan_file=PLAN_FILE,
            contract_file=CH3_CONTRACT,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            state_db=db,
            work_dir=work,
            shared_ledger_file=ledger_file,
            broker_factory=lambda gate: None,  # type: ignore[arg-type,return-value]
            subscription_ledger_root=work / "subscription",
            list_price_file=LIST_PRICES,
            poll_seconds=5,
            once=True,
            code_commit="test-commit",
            ledger_run_prefixes=("chapter3-",),
            pause_models=parse_pause_models(["claude-fable-5-1=2026-09-16T23:00:00Z"]),
        )
    finally:
        module.build_vendor_runs = original  # type: ignore[assignment]
    assert result["errors"] == []
    # Gemini kept its slot: the ceiling had room, so no vendor was paused.
    assert result["paused_vendors"] == {}
    assert PROVIDER_GOOGLE_GEMINI in result["active_vendors"]
    # And the paused model was still held.
    row = CostJournal(work).item_rows()[0]
    assert row["evaluation"]["models_paused"] == ["claude-fable-5-1"]
    assert row["evaluation"]["pending_paused_trials"] == 6
    assert row["evaluation"]["recorded_trials"] == 42
    assert "claude-fable-5-1" not in row["outcomes_by_model"]
    assert row["complete"] is False


def test_an_item_scoped_stop_keeps_the_vendor_running(tmp_path: Path) -> None:
    """The per-item repeat limit must not pause a vendor for the whole watch.

    The live service met this on 2026-09-16: a re-evaluated item exhausted its
    Gemini repeat budget, the stop was read as a vendor stop, and the Gemini
    arm was then off for every later question. The repeat limit counts one
    item, condition, model and arm, so it says nothing about the next item.
    """
    assert is_item_scoped_reason(f"not_submitted: {EVALUATION_ITEM_REPEAT_REASON}")
    assert not is_item_scoped_reason(f"not_submitted: {EVALUATION_CEILING_REASON}")
    assert not is_item_scoped_reason(None)
    assert not is_item_scoped_reason("harness_error: the login expired")
    db = state_db(tmp_path, chapter3=["aqa-a", "aqa-b"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=2)
    work = tmp_path / "work"

    import arctic_qa.abstention_watch as module

    stops = {"count": 0}

    def fake_build(*, plan, set_dir, run_id, gate_dir, vendors, **_: object):
        return {
            vendor: VendorRun(
                vendor=vendor,
                provider=ScriptedEvaluationProvider(policy="gold", seed=run_id),
                decoding={"scripted": True, "vendor": vendor},
                models=plan["vendors"][vendor]["models"],
                concurrency=2,
            )
            for vendor in vendors
        }

    original_build = module.build_vendor_runs
    original_reason = module.vendor_stop_reason

    def fake_reason(summary: dict, vendor: str) -> str | None:
        # The first item reports the item-scoped stop for Gemini only.
        if vendor == PROVIDER_GOOGLE_GEMINI and stops["count"] < 1:
            stops["count"] += 1
            return f"not_submitted: {EVALUATION_ITEM_REPEAT_REASON}"
        return None

    module.build_vendor_runs = fake_build  # type: ignore[assignment]
    module.vendor_stop_reason = fake_reason  # type: ignore[assignment]
    try:
        result = watch(
            authorization_file=auth,
            plan_file=PLAN_FILE,
            contract_file=CH3_CONTRACT,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            state_db=db,
            work_dir=work,
            shared_ledger_file=ledger_file,
            broker_factory=None,
            subscription_ledger_root=work / "subscription",
            list_price_file=LIST_PRICES,
            poll_seconds=5,
            once=True,
            code_commit="test-commit",
            ledger_run_prefixes=("chapter3-",),
        )
    finally:
        module.build_vendor_runs = original_build  # type: ignore[assignment]
        module.vendor_stop_reason = original_reason  # type: ignore[assignment]
    # The vendor is not paused, and it kept its place for the second item.
    assert result["paused_vendors"] == {}
    assert PROVIDER_GOOGLE_GEMINI in result["active_vendors"]
    assert [row["item_id"] for row in result["items_this_invocation"]] == [
        "aqa-a",
        "aqa-b",
    ]
    # No pause row was journalled for an item-scoped stop.
    assert [
        row for row in CostJournal(work).rows() if row.get("kind") == "vendor_pause"
    ] == []


def test_the_watcher_publishes_its_state_after_every_poll(tmp_path: Path) -> None:
    """A long-running unit must show its progress while it runs.

    The state file used to be written only after the loop ended, and a service
    loop never ends, so `watch-state.json` stood at the values of the first
    pass. An operator reads that file to see whether the evaluator still
    polls.
    """
    db = state_db(tmp_path, chapter3=[])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db)
    work = tmp_path / "work"
    seen: list[int] = []

    import arctic_qa.abstention_watch as module

    def fake_sleep(seconds: float) -> None:
        # Read the published state between two polls of an idle watcher.
        seen.append(
            json.loads((work / WATCH_STATE_FILENAME).read_text(encoding="utf-8"))[
                "polls"
            ]
        )

    original = module.build_vendor_runs
    module.build_vendor_runs = lambda **_: {}  # type: ignore[assignment]
    try:
        result = watch(
            authorization_file=auth,
            plan_file=PLAN_FILE,
            contract_file=CH3_CONTRACT,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            state_db=db,
            work_dir=work,
            shared_ledger_file=ledger_file,
            broker_factory=None,
            subscription_ledger_root=work / "subscription",
            list_price_file=LIST_PRICES,
            poll_seconds=5,
            code_commit="test-commit",
            ledger_run_prefixes=("chapter3-",),
            sleep=fake_sleep,
            clock=lambda: len(seen) * 10.0,
            deadline_seconds=25.0,
        )
    finally:
        module.build_vendor_runs = original  # type: ignore[assignment]
    # The poll count rose between the sleeps, so the file tracked the loop.
    assert seen == sorted(seen) and len(seen) >= 2
    assert seen[0] == 1 and seen[-1] > seen[0]
    state = json.loads((work / WATCH_STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["polls"] == result["polls"] >= len(seen)
    assert state["evaluated_items"] == []
    assert state["started_at_utc"]


def test_a_fully_held_item_waits_for_the_resume_instead_of_every_poll(
    tmp_path: Path,
) -> None:
    """A held item must not be re-journalled on every poll.

    The running service showed this on 2026-09-16: an item whose six Claude
    Fable trials were held was revisited every 30 seconds, and each revisit
    recorded nothing, called nothing and appended one more journal row. The
    item can only advance when the pause lifts, so it waits until then.
    """
    db = state_db(tmp_path, chapter3=["aqa-a"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db)
    work = tmp_path / "work"
    held = parse_pause_models(["claude-fable-5-1=2026-09-16T23:00:00Z"])
    first = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        pause_models=held,
    )
    assert [row["item_id"] for row in first["items_this_invocation"]] == ["aqa-a"]
    journal = CostJournal(work)
    assert len(journal.item_rows()) == 1
    assert journal.items_held_by(frozenset({"claude-fable-5-1"})) == {"aqa-a"}
    # A second pass under the same pause leaves the item alone.
    again = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        pause_models=held,
    )
    assert again["items_this_invocation"] == []
    assert len(CostJournal(work).item_rows()) == 1
    # The pause of another model does not hold this item.
    assert journal.items_held_by(frozenset({"gpt-5.6-sol"})) == set()
    # After the resume time the item is taken up and finished.
    done = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        pause_models=parse_pause_models(["claude-fable-5-1=2026-09-16T00:00:00Z"]),
    )
    assert [row["item_id"] for row in done["items_this_invocation"]] == ["aqa-a"]
    journal = CostJournal(work)
    assert len(journal.item_rows()) == 2
    assert journal.completed_item_ids() == {"aqa-a"}
    assert journal.latest_item_rows()[0]["evaluation"]["recorded_trials"] == 48
