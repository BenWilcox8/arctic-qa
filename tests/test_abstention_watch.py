from __future__ import annotations

import json
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Callable

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
    DEFAULT_PAUSE_FILE,
    load_pause,
    load_plan,
    paused_models,
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
    ITEM_SCOPED_REASONS,
    MAXIMUM_ITEM_WORKERS,
    WATCH_STATE_FILENAME,
    WAVE_CYCLES,
    authorization_record,
    estimated_gemini_item_usd,
    is_ambiguous_charge_reason,
    is_ceiling_reason,
    is_harness_unavailable_reason,
    is_item_scoped_reason,
    is_lock_busy_error,
    pending_item_ids,
    validate_authorization,
    vendor_stop_reason,
    watch,
    wave_order,
)
from arctic_qa.cli import main as cli_main
from arctic_qa.errors import BrokerOperationBusyError, HarnessUnavailableError
from arctic_qa.model_broker import (
    EVALUATION_CEILING_REASON,
    EVALUATION_ITEM_REPEAT_REASON,
    EVALUATION_PHASE,
    OPERATION_LOCK_BUSY_REASON,
    TRANSIENT_RESERVATION_REASONS,
)
from arctic_qa.util import atomic_json
from test_abstention_render import _candidate, _state_db, DISTRACTORS
from test_abstention_plan import future_resume_utc  # noqa: E402


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
    # `--vendors` is a scope, not a pause: the excluded vendor owes the
    # item nothing, so the item is complete for this run.
    assert row["evaluation"]["vendors_paused"] == []
    assert row["evaluation"]["vendors_excluded"] == [PROVIDER_GOOGLE_GEMINI]
    assert row["complete"] is True
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
    # Only a stop of this pass pauses a vendor. A recorded stop is permanent,
    # so `stopped_on` names it on every later pass over that item, and a
    # revisit of it must not take the arm down again.
    revisit = {
        "vendors": {
            PROVIDER_ANTHROPIC_CLAUDE_CODE: {
                "stopped_on": {"state": "failed", "error": "an old harness fault"},
                "stopped_this_pass": None,
            }
        }
    }
    assert vendor_stop_reason(revisit, PROVIDER_ANTHROPIC_CLAUDE_CODE) is None
    fresh = {
        "vendors": {
            PROVIDER_ANTHROPIC_CLAUDE_CODE: {
                "stopped_on": {"state": "failed", "error": "an old harness fault"},
                "stopped_this_pass": {
                    "state": "failed",
                    "error": "the login expired",
                },
            }
        }
    }
    assert vendor_stop_reason(fresh, PROVIDER_ANTHROPIC_CLAUDE_CODE) == (
        "failed: the login expired"
    )


def test_a_vendor_that_stops_is_paused_for_the_rest_of_the_watch(
    tmp_path: Path,
) -> None:
    """A harness failure pauses that vendor; the other vendor runs every item.

    One worker, so the second question starts after the first one finished:
    this pins the pause of a vendor against the very next question. Under
    several workers a question already in flight keeps the vendors it started
    with, which
    ``test_a_vendor_pause_inside_a_wave_holds_the_questions_dispatched_after_it``
    pins.
    """
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
            item_workers=1,
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
    # The second item runs the Codex arm only, so it is NOT complete: the
    # paused Claude arm owes it eighteen trials. An item recorded complete
    # with 30 of its 48 trials was never taken up again (2026-09-17 00:26 UTC).
    assert rows[1]["complete"] is False
    assert rows[1]["evaluation"]["recorded_trials"] == 18
    assert rows[1]["evaluation"]["vendors_paused"] == [PROVIDER_ANTHROPIC_CLAUDE_CODE]
    # Gemini is out of scope for this invocation, not paused, so it owes
    # nothing.
    assert rows[1]["evaluation"]["vendors_excluded"] == [PROVIDER_GOOGLE_GEMINI]
    # And the evaluator does not churn on it while the vendor stays paused.
    assert journal.items_awaiting_vendors({PROVIDER_ANTHROPIC_CLAUDE_CODE}) == {
        rows[1]["item_id"]
    }


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
        pause_models=parse_pause_models([f"claude-fable-5-1={future_resume_utc()}"]),
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
        pause_models=parse_pause_models([f"claude-fable-5-1={future_resume_utc()}"]),
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
    # come and go as an operator or the guard pauses a model, and the standing
    # one resumes on its own clock, so this asserts the recorded entry and
    # that the live list agrees with the rule, not the calendar.
    fable_entry = status["entries"]["claude-fable-5-1"]
    assert fable_entry["owner"] == "captain"
    assert fable_entry["reason"] and fable_entry["resume_at_utc"]
    assert set(status["paused_now"]) == set(
        paused_models(load_pause(DEFAULT_PAUSE_FILE))
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
            pause_models=parse_pause_models(
                [f"claude-fable-5-1={future_resume_utc()}"]
            ),
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
    held = parse_pause_models([f"claude-fable-5-1={future_resume_utc()}"])
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


def test_a_start_clears_the_vendor_pause_of_the_last_invocation(
    tmp_path: Path,
) -> None:
    """A start is an operator action that says to try again.

    The docs said a restart clears a vendor pause, and it did not: the pause
    lived in `watch-state.json` and the next start excluded that vendor. The
    running service showed it on 2026-09-16, when a transient npm upgrade hid
    the Claude Code binary for one item and the restart then measured five of
    the eight models. A model pause is not cleared, because it lives in the
    pause files that an operator and the cost guard own.
    """
    db = state_db(tmp_path, chapter3=["aqa-a"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    atomic_json(
        work / WATCH_STATE_FILENAME,
        {
            "schema": "abstention-streaming-watch-state-v1",
            "paused_vendors": {
                PROVIDER_ANTHROPIC_CLAUDE_CODE: {"reason": "failed: the binary is gone"}
            },
            "skipped_items": {},
        },
    )
    events: list[dict] = []
    result = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        log=events.append,
    )
    # Every vendor of the plan is active again, and the clearing is recorded.
    assert result["active_vendors"] == [
        PROVIDER_GOOGLE_GEMINI,
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        PROVIDER_OPENAI_CODEX,
    ]
    assert result["paused_vendors"] == {}
    cleared = [row for row in events if row["event"] == "vendor_pause_cleared"]
    assert len(cleared) == 1
    assert cleared[0]["vendor"] == PROVIDER_ANTHROPIC_CLAUDE_CODE
    assert cleared[0]["reason"] == "failed: the binary is gone"
    # The item ran on all three vendors.
    row = CostJournal(work).item_rows()[0]
    assert row["evaluation"]["vendors_paused"] == []
    assert row["evaluation"]["recorded_trials"] == 48


class StoppingGeminiProvider(ScriptedEvaluationProvider):
    """A Gemini provider that stops on its first calls, then answers.

    ``budget`` is a shared one-element list, so the count survives the fresh
    provider the watcher builds for every item. ``state`` and ``error`` are
    the response fields the run summary turns into the vendor stop reason:
    an ambiguous charge carries the state alone, which is the shape the
    running service recorded on 2026-09-16 at 17:53 UTC.
    """

    def __init__(
        self,
        *,
        budget: list[int],
        state: str = "ambiguous_charge",
        error: str | None = None,
        **changes,
    ) -> None:
        super().__init__(**changes)
        self.budget = budget
        self.stop_state = state
        self.stop_error = error

    def answer(self, request):  # type: ignore[no-untyped-def]
        response = super().answer(request)
        if self.budget[0] <= 0:
            return response
        self.budget[0] -= 1
        return replace(response, state=self.stop_state, error=self.stop_error)


def _halt_the_evaluation_phase(ledger_file: Path, *, halted: bool) -> None:
    """Set or clear the evaluation-phase halt of a shared ledger."""
    ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    ledger["evaluation_halted"] = halted
    ledger["evaluation_halt_reason"] = "ambiguous_generation_charge" if halted else None
    atomic_json(ledger_file, ledger)


def _watch_two_polls(
    *,
    db: Path,
    work: Path,
    ledger_file: Path,
    auth: Path,
    events: list[dict],
    between_polls: Callable[[], None],
    budget: list[int],
    stop_state: str = "ambiguous_charge",
    stop_error: str | None = None,
) -> dict:
    """Run two polls, with ``between_polls`` in the wait between them.

    The Gemini vendor stops on the first poll, so the second poll is the one
    that decides whether the vendor resumes.
    """
    import arctic_qa.abstention_watch as module

    def fake_build(*, plan, set_dir, run_id, gate_dir, vendors, **_: object):
        runs = {}
        for vendor in vendors:
            provider = (
                StoppingGeminiProvider(
                    budget=budget,
                    state=stop_state,
                    error=stop_error,
                    policy="gold",
                )
                if vendor == PROVIDER_GOOGLE_GEMINI
                else ScriptedEvaluationProvider(policy="gold")
            )
            runs[vendor] = VendorRun(
                vendor=vendor,
                provider=provider,
                decoding={"scripted": True, "vendor": vendor},
                models=plan["vendors"][vendor]["models"],
                concurrency=1,
            )
        return runs

    ticks = iter([0.0, 1.0, 100.0, 100.0])
    original = module.build_vendor_runs
    module.build_vendor_runs = fake_build  # type: ignore[assignment]
    try:
        return watch(
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
            log=events.append,
            clock=lambda: next(ticks, 100.0),
            sleep=lambda _seconds: between_polls(),
            deadline_seconds=10.0,
        )
    finally:
        module.build_vendor_runs = original  # type: ignore[assignment]


def test_a_released_ambiguous_charge_resumes_gemini_on_the_next_poll(
    tmp_path: Path,
) -> None:
    """A supervisor release unpauses the vendor without a restart.

    The broker keeps the reservation of a paid call whose charge it cannot
    prove and halts the evaluation phase. The evaluator paused the Gemini
    vendor for the rest of the invocation, and only a start cleared it, so the
    captain's Gemini arm stayed off after the 503 of 2026-09-16 at 17:53 UTC
    until an operator restarted the unit. The release is a ledger fact, so the
    evaluator reads it on every poll and resumes the vendor itself.
    """
    db = state_db(tmp_path / "now", chapter3=["aqa-a"])
    later = state_db(tmp_path / "later", chapter3=["aqa-a", "aqa-b"])
    ledger_file = construction_ledger(
        tmp_path, {"family-aqa-a": ["0.01"], "family-aqa-b": ["0.01"]}
    )
    _halt_the_evaluation_phase(ledger_file, halted=True)
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "released"
    events: list[dict] = []

    def between_polls() -> None:
        # The supervisor releases the ambiguous charge, and the producer
        # accepts one more question, while the evaluator waits.
        _halt_the_evaluation_phase(ledger_file, halted=False)
        db.write_bytes(later.read_bytes())

    result = _watch_two_polls(
        db=db,
        work=work,
        ledger_file=ledger_file,
        auth=auth,
        events=events,
        between_polls=between_polls,
        budget=[1],
    )
    assert result["errors"] == []
    paused = [row for row in events if row["event"] == "vendor_paused"]
    assert len(paused) == 1
    assert paused[0]["vendor"] == PROVIDER_GOOGLE_GEMINI
    assert paused[0]["reason"] == "ambiguous_charge"
    resumed = [row for row in events if row["event"] == "vendor_resumed"]
    assert len(resumed) == 1
    assert resumed[0]["vendor"] == PROVIDER_GOOGLE_GEMINI
    assert resumed[0]["paused_reason"] == "ambiguous_charge"
    # The vendor runs again on the next question, with no restart between.
    started = [row for row in events if row["event"] == "item_started"]
    assert [row["item_id"] for row in started] == ["aqa-a", "aqa-b"]
    assert PROVIDER_GOOGLE_GEMINI in started[1]["vendors"]
    assert result["paused_vendors"] == {}
    assert PROVIDER_GOOGLE_GEMINI in result["active_vendors"]
    rows = {row["item_id"]: row for row in CostJournal(work).item_rows()}
    assert rows["aqa-a"]["evaluation"]["vendors_paused"] == []
    assert rows["aqa-b"]["evaluation"]["vendors_paused"] == []
    assert rows["aqa-b"]["evaluation"]["recorded_trials"] == 48
    resumes = [
        row for row in CostJournal(work).rows() if row.get("kind") == "vendor_resume"
    ]
    assert len(resumes) == 1
    assert resumes[0]["vendor"] == PROVIDER_GOOGLE_GEMINI
    assert resumes[0]["paused_reason"] == "ambiguous_charge"


def test_a_released_ambiguous_charge_resumes_gemini_inside_the_wave(
    tmp_path: Path,
) -> None:
    """The arm comes back at the next question, not at the next poll cycle.

    One poll cycle scores four waves, which was 44 minutes of wall time at
    eight questions in flight on 2026-09-17. The Gemini arm stopped on an
    ambiguous charge at 11:31:26 UTC that the ledger had already released,
    and it stayed dark for the rest of the cycle because only the top of the
    loop asked. Every admission asks now.
    """
    import arctic_qa.abstention_watch as module

    db = state_db(tmp_path / "db", chapter3=["aqa-a", "aqa-b", "aqa-c"])
    ledger_file = construction_ledger(
        tmp_path,
        {
            "family-aqa-a": ["0.01"],
            "family-aqa-b": ["0.01"],
            "family-aqa-c": ["0.01"],
        },
    )
    _halt_the_evaluation_phase(ledger_file, halted=True)
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "inside"
    events: list[dict] = []
    budget = [1]

    class ReleasingGeminiProvider(StoppingGeminiProvider):
        """Stops once, and the supervisor releases the charge at that moment."""

        def answer(self, request):  # type: ignore[no-untyped-def]
            stopping = self.budget[0] > 0
            response = super().answer(request)
            if stopping:
                _halt_the_evaluation_phase(ledger_file, halted=False)
            return response

    def fake_build(*, plan, set_dir, run_id, gate_dir, vendors, **_: object):
        runs = {}
        for vendor in vendors:
            provider = (
                ReleasingGeminiProvider(budget=budget, policy="gold")
                if vendor == PROVIDER_GOOGLE_GEMINI
                else ScriptedEvaluationProvider(policy="gold")
            )
            runs[vendor] = VendorRun(
                vendor=vendor,
                provider=provider,
                decoding={"scripted": True, "vendor": vendor},
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
            item_workers=1,
            code_commit="test-commit",
            ledger_run_prefixes=("chapter3-",),
            backfill_contract_file=CH2_CONTRACT,
            log=events.append,
        )
    finally:
        module.build_vendor_runs = original  # type: ignore[assignment]

    assert result["errors"] == []
    # One poll cycle, so a resume at the top of the loop could not have done it.
    assert result["polls"] == 1
    paused = [row for row in events if row["event"] == "vendor_paused"]
    resumed = [row for row in events if row["event"] == "vendor_resumed"]
    assert len(paused) == 1
    assert len(resumed) == 1
    assert resumed[0]["paused_reason"] == "ambiguous_charge"
    started = [row for row in events if row["event"] == "item_started"]
    assert [row["item_id"] for row in started] == ["aqa-a", "aqa-b", "aqa-c"]
    # The resume lands inside the cycle, before the last question is admitted,
    # and that question runs the arm.
    order = [row["event"] for row in events]
    assert order.index("vendor_resumed") < len(order) - 1 - order[::-1].index(
        "item_started"
    )
    assert PROVIDER_GOOGLE_GEMINI in started[-1]["vendors"]
    assert result["paused_vendors"] == {}
    assert PROVIDER_GOOGLE_GEMINI in result["active_vendors"]


def test_an_unreleased_ambiguous_charge_keeps_gemini_paused(tmp_path: Path) -> None:
    """The evaluation halt is the release, so a standing halt keeps the pause."""
    db = state_db(tmp_path / "now", chapter3=["aqa-a"])
    later = state_db(tmp_path / "later", chapter3=["aqa-a", "aqa-b"])
    ledger_file = construction_ledger(
        tmp_path, {"family-aqa-a": ["0.01"], "family-aqa-b": ["0.01"]}
    )
    _halt_the_evaluation_phase(ledger_file, halted=True)
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "held"
    events: list[dict] = []
    result = _watch_two_polls(
        db=db,
        work=work,
        ledger_file=ledger_file,
        auth=auth,
        events=events,
        between_polls=lambda: db.write_bytes(later.read_bytes()),
        budget=[1],
    )
    assert result["errors"] == []
    assert [row for row in events if row["event"] == "vendor_resumed"] == []
    assert PROVIDER_GOOGLE_GEMINI in result["paused_vendors"]
    assert result["active_vendors"] == [
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        PROVIDER_OPENAI_CODEX,
    ]
    rows = {row["item_id"]: row for row in CostJournal(work).item_rows()}
    assert rows["aqa-b"]["evaluation"]["vendors_paused"] == [PROVIDER_GOOGLE_GEMINI]
    assert [
        row for row in CostJournal(work).rows() if row.get("kind") == "vendor_resume"
    ] == []


def test_a_clean_ledger_does_not_resume_a_vendor_paused_for_another_reason(
    tmp_path: Path,
) -> None:
    """Only an ambiguous charge waits on the ledger; every other stop stands."""
    db = state_db(tmp_path / "now", chapter3=["aqa-a"])
    later = state_db(tmp_path / "later", chapter3=["aqa-a", "aqa-b"])
    ledger_file = construction_ledger(
        tmp_path, {"family-aqa-a": ["0.01"], "family-aqa-b": ["0.01"]}
    )
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "other"
    events: list[dict] = []
    result = _watch_two_polls(
        db=db,
        work=work,
        ledger_file=ledger_file,
        auth=auth,
        events=events,
        between_polls=lambda: db.write_bytes(later.read_bytes()),
        budget=[1],
        stop_state="failed",
        stop_error="the provider refused the request",
    )
    assert result["errors"] == []
    paused = [row for row in events if row["event"] == "vendor_paused"]
    assert len(paused) == 1
    assert paused[0]["reason"] == "failed: the provider refused the request"
    assert [row for row in events if row["event"] == "vendor_resumed"] == []
    assert PROVIDER_GOOGLE_GEMINI in result["paused_vendors"]


def test_the_ambiguous_charge_reason_is_read_from_the_request_state() -> None:
    """The journalled reason is the request state, with any detail after it."""
    assert is_ambiguous_charge_reason("ambiguous_charge")
    assert is_ambiguous_charge_reason("ambiguous_charge: HTTP 503 UNAVAILABLE")
    assert not is_ambiguous_charge_reason("failed: the harness exited")
    assert not is_ambiguous_charge_reason(EVALUATION_CEILING_REASON)
    assert not is_ambiguous_charge_reason(None)
    assert not is_ambiguous_charge_reason("")


def test_the_item_bound_writes_one_blocked_line_to_the_status_file(
    tmp_path: Path,
) -> None:
    """A bound ends the run with exit code 0, so it must say so somewhere.

    The unit met its item bound at 2026-09-16T19:31:44Z, exited 0, and no
    operator saw it until the next morning.
    """
    db = state_db(tmp_path, chapter3=["aqa-a", "aqa-b", "aqa-c"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=2)
    status = tmp_path / "task.status"
    first = scripted_watch(
        db=db,
        work_dir=tmp_path / "bounded",
        ledger_file=ledger_file,
        authorization_file=auth,
        status_file=status,
    )
    # The first pass takes both items and stops inside the bound, so it has
    # nothing to report.
    assert len(first["items_this_invocation"]) == 2
    assert not status.exists()

    again = scripted_watch(
        db=db,
        work_dir=tmp_path / "bounded",
        ledger_file=ledger_file,
        authorization_file=auth,
        status_file=status,
    )
    assert again["items_this_invocation"] == []
    lines = status.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "blocked: the streaming evaluator met its item bound of 2 items and "
        "stopped; a larger run needs a new reviewed authorization"
    ]


def test_the_bound_needs_no_status_file(tmp_path: Path) -> None:
    """The option is optional: a run without it still stops the same way."""
    db = state_db(tmp_path, chapter3=["aqa-a"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=1)
    work_dir = tmp_path / "bounded"
    scripted_watch(
        db=db, work_dir=work_dir, ledger_file=ledger_file, authorization_file=auth
    )
    again = scripted_watch(
        db=db, work_dir=work_dir, ledger_file=ledger_file, authorization_file=auth
    )
    assert again["items_this_invocation"] == []


def test_the_watcher_publishes_its_state_after_every_item(tmp_path: Path) -> None:
    """One poll cycle covers every pending item, so it must not publish once.

    At sixteen pending items a cycle runs for about an hour. A watcher that
    published only at the end of its cycle looked stopped to the cost guard,
    whose staleness bound is 900 seconds.

    One worker here, so every question is finished before the next starts and
    the published list is the finished one. A wave of several workers
    publishes on its admissions as well, which
    ``test_the_wave_publishes_the_questions_it_holds_in_flight`` pins.
    """
    db = state_db(tmp_path, chapter3=["aqa-a", "aqa-b", "aqa-c"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=3)
    work_dir = tmp_path / "published"
    seen: list[int] = []

    def watch_state_size(_: dict) -> None:
        path = work_dir / WATCH_STATE_FILENAME
        if path.is_file():
            seen.append(
                len(json.loads(path.read_text(encoding="utf-8"))["evaluated_items"])
            )

    scripted_watch(
        db=db,
        work_dir=work_dir,
        ledger_file=ledger_file,
        authorization_file=auth,
        progress=watch_state_size,
        item_workers=1,
    )
    # The watcher published a growing item list while the one cycle ran, so a
    # reader saw progress before the cycle ended.
    assert seen and max(seen) >= 2


def test_a_paused_vendor_owes_its_trials_and_the_item_waits_for_a_start(
    tmp_path: Path,
) -> None:
    """A paused vendor makes an item incomplete, as a paused model does.

    The Claude Code arm stopped at 2026-09-17T00:26:03Z because its binary
    was missing for a moment during a package upgrade. The next item ran on
    the other two vendors and was journalled complete with 30 of its 48
    trials, so the evaluator never took it up again and 18 trials were lost.
    """
    journal = CostJournal(tmp_path / "journal")
    row = {
        "schema": "abstention-eval-cost-row-v1",
        "kind": "item",
        "item_id": "aqa-truncated",
        "run_id": "abstention-stream-test-aqa-truncated",
        "recorded_at_utc": "2026-09-17T00:34:33Z",
        "complete": True,
        "evaluation": {
            "planned_trials": 48,
            "recorded_trials": 30,
            "vendors_paused": [PROVIDER_ANTHROPIC_CLAUDE_CODE],
            "models_paused": [],
            "pending_paused_trials": 0,
            "complete": True,
        },
    }
    journal.append(row)
    # The reader refuses the flag, so the row the old code wrote re-opens.
    assert journal.completed_item_ids() == set()
    assert journal.held_item_ids() == {"aqa-truncated"}
    # While that vendor stays paused the item cannot move, so it waits.
    assert journal.items_awaiting_vendors({PROVIDER_ANTHROPIC_CLAUDE_CODE}) == {
        "aqa-truncated"
    }
    # A start clears the vendor pause, and then the item is pending again.
    assert journal.items_awaiting_vendors(set()) == set()


def test_an_excluded_vendor_owes_nothing(tmp_path: Path) -> None:
    """`--vendors` is a scope the operator chose, not a stop of an arm."""
    journal = CostJournal(tmp_path / "journal")
    journal.append(
        {
            "schema": "abstention-eval-cost-row-v1",
            "kind": "item",
            "item_id": "aqa-scoped",
            "run_id": "abstention-stream-test-aqa-scoped",
            "recorded_at_utc": "2026-09-17T00:34:33Z",
            "complete": True,
            "evaluation": {
                "planned_trials": 48,
                "recorded_trials": 36,
                "vendors_paused": [],
                "vendors_excluded": [PROVIDER_GOOGLE_GEMINI],
                "models_paused": [],
                "pending_paused_trials": 0,
                "complete": True,
            },
        }
    )
    assert journal.completed_item_ids() == {"aqa-scoped"}
    assert journal.items_awaiting_vendors({PROVIDER_GOOGLE_GEMINI}) == set()


# --- Several questions at once --------------------------------------------------


class _Rendezvous:
    """Prove that several questions really are scored at the same time.

    Every answer of the provider below reports its question here. A question
    waits until ``wanted`` different questions have reported, so the run
    finishes quickly when the wave is concurrent and times out when it is
    not. A sequential evaluator cannot pass: the first question would wait
    for a question that only starts after it.
    """

    def __init__(self, wanted: int, timeout: float = 60.0) -> None:
        self.wanted = wanted
        self.timeout = timeout
        self.condition = threading.Condition()
        self.seen: set[str] = set()
        self.timed_out = False

    def arrive(self, item_id: str) -> None:
        with self.condition:
            self.seen.add(item_id)
            if len(self.seen) >= self.wanted:
                self.condition.notify_all()
                return
            met = self.condition.wait_for(
                lambda: len(self.seen) >= self.wanted, timeout=self.timeout
            )
            if not met:
                self.timed_out = True


class RendezvousProvider(ScriptedEvaluationProvider):
    """A scripted provider that reports every trial to a rendezvous."""

    def __init__(self, rendezvous: _Rendezvous, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rendezvous = rendezvous

    def answer(self, request):
        self.rendezvous.arrive(request.trial["item_id"])
        return super().answer(request)


def rendezvous_watch(
    *,
    db: Path,
    work_dir: Path,
    ledger_file: Path,
    authorization_file: Path,
    rendezvous: _Rendezvous,
    **changes,
) -> dict:
    """Run the watcher with a rendezvous provider for every vendor."""
    import arctic_qa.abstention_watch as module

    def fake_build(*, plan, set_dir, run_id, gate_dir, vendors, **_: object):
        return {
            vendor: VendorRun(
                vendor=vendor,
                provider=RendezvousProvider(rendezvous, policy="gold", seed=run_id),
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


def test_the_watcher_scores_several_questions_at_once(tmp_path: Path) -> None:
    """Four questions in one wave, each with its own set, gates and row.

    One question alone cannot fill the slots the policy allows, so the
    evaluator scored 17 questions an hour against a producer that accepted 58
    (measured 2026-09-17 08:35 UTC). The wave is the fix, and nothing of one
    question moves: its own evaluation set, its own derived gates, its own run
    directory, and exactly one journal row.
    """
    items = ["aqa-w1", "aqa-w2", "aqa-w3", "aqa-w4"]
    db = state_db(tmp_path, chapter3=items)
    ledger_file = construction_ledger(tmp_path, {f"family-{i}": ["0.10"] for i in items})
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "wave"
    meeting = _Rendezvous(wanted=4)
    result = rendezvous_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        rendezvous=meeting,
        item_workers=4,
    )
    assert meeting.timed_out is False
    assert meeting.seen == set(items)
    assert result["item_workers"] == 4
    assert sorted(row["item_id"] for row in result["items_this_invocation"]) == items
    journal = CostJournal(work)
    rows = journal.item_rows()
    assert len(rows) == 4
    assert {row["item_id"] for row in rows} == set(items)
    # One row per question, and every question complete on its own evidence.
    assert all(row["complete"] is True for row in rows)
    assert all(row["evaluation"]["recorded_trials"] == 48 for row in rows)
    assert journal.completed_item_ids() == set(items)
    assert len({row["eval_set_id"] for row in rows}) == 4
    assert len({row["run_dir"] for row in rows}) == 4
    assert len({row["gate_dir"] for row in rows}) == 4
    for row in rows:
        assert Path(row["gate_dir"]).name == row["item_id"]
        assert (Path(row["run_dir"]) / PLAN_SUMMARY_FILENAME).is_file()
    assert journal.cumulative()["items"] == 4
    assert journal.cumulative()["trials"] == 4 * 48


def test_the_wave_publishes_the_questions_it_holds_in_flight(tmp_path: Path) -> None:
    """The cost guard reads watch-state.json, so a wave must publish itself.

    A guard that sees no movement for 900 seconds calls the evaluator
    stopped. One wave can run longer than that, so the state carries the
    questions in flight and grows as they finish.
    """
    items = ["aqa-f1", "aqa-f2", "aqa-f3"]
    db = state_db(tmp_path, chapter3=items)
    ledger_file = construction_ledger(tmp_path, {f"family-{i}": ["0.10"] for i in items})
    auth = authorization(tmp_path, db, maximum_items=3)
    work = tmp_path / "in-flight"
    meeting = _Rendezvous(wanted=3)
    seen: list[int] = []

    def sample(_: dict) -> None:
        path = work / WATCH_STATE_FILENAME
        if path.is_file():
            published = json.loads(path.read_text(encoding="utf-8"))
            seen.append(len(published["items_in_flight"]))

    result = rendezvous_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        rendezvous=meeting,
        item_workers=3,
        progress=sample,
    )
    assert meeting.timed_out is False
    assert max(seen) == 3
    published = json.loads((work / WATCH_STATE_FILENAME).read_text(encoding="utf-8"))
    assert published["item_workers"] == 3
    # Every question finished, so nothing is in flight at the end.
    assert published["items_in_flight"] == []
    assert sorted(published["evaluated_items"]) == items
    assert result["errors"] == []


def test_a_vendor_pause_inside_a_wave_holds_the_questions_dispatched_after_it(
    tmp_path: Path,
) -> None:
    """A vendor that stops is paused for every question dispatched after it.

    A question already in flight keeps the vendors it started with, and its
    own row records what they owe it. Two records come out of one incident,
    and they are not the same:

    - the questions the arm stopped *inside* keep their partial trials. The
      policy forbids a retry, so those trials never went out and never will
      under this contract: the journal closes them, their row-level
      ``complete`` flag is False and their recorded trial count says how much
      they hold. Reopening one is an operator action.
    - the questions dispatched after the pause carry ``vendors_paused``, so
      the arm owes them every trial and they reopen on the next start.
    """
    items = ["aqa-p1", "aqa-p2", "aqa-p3", "aqa-p4"]
    db = state_db(tmp_path, chapter3=items)
    ledger_file = construction_ledger(tmp_path, {f"family-{i}": ["0.10"] for i in items})
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "wave-pause"

    import arctic_qa.abstention_watch as module

    class BrokenProvider(ScriptedEvaluationProvider):
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
        return {
            vendor: VendorRun(
                vendor=vendor,
                provider=(
                    BrokenProvider(policy="gold")
                    if vendor == PROVIDER_ANTHROPIC_CLAUDE_CODE
                    else ScriptedEvaluationProvider(policy="gold")
                ),
                decoding={"scripted": True},
                models=plan["vendors"][vendor]["models"],
                concurrency=1,
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
            broker_factory=None,
            subscription_ledger_root=work / "subscription",
            list_price_file=LIST_PRICES,
            poll_seconds=5,
            once=True,
            code_commit="test-commit",
            ledger_run_prefixes=("chapter3-",),
            vendors=[PROVIDER_ANTHROPIC_CLAUDE_CODE, PROVIDER_OPENAI_CODEX],
            item_workers=2,
        )
    finally:
        module.build_vendor_runs = original  # type: ignore[assignment]
    assert result["errors"] == []
    assert result["active_vendors"] == [PROVIDER_OPENAI_CODEX]
    journal = CostJournal(work)
    # One pause, however many questions met the same broken vendor.
    pauses = [row for row in journal.rows() if row.get("kind") == "vendor_pause"]
    assert len(pauses) == 1
    assert pauses[0]["vendor"] == PROVIDER_ANTHROPIC_CLAUDE_CODE
    rows = journal.item_rows()
    assert len(rows) == 4
    assert {row["item_id"] for row in rows} == set(items)
    # No question holds all 48 of its trials.
    assert all(row["complete"] is False for row in rows)
    assert all(row["evaluation"]["recorded_trials"] < 48 for row in rows)
    held = [
        row
        for row in rows
        if row["evaluation"]["vendors_paused"] == [PROVIDER_ANTHROPIC_CLAUDE_CODE]
    ]
    inside = [row for row in rows if not row["evaluation"]["vendors_paused"]]
    # Two workers and four questions, so the second wave was dispatched after
    # the pause and ran the Codex arm alone.
    assert len(held) >= 2
    assert len(inside) >= 1
    assert all(row["evaluation"]["recorded_trials"] == 18 for row in held)
    # The questions the arm stopped inside are closed with what they hold.
    assert journal.completed_item_ids() == {row["item_id"] for row in inside}
    assert journal.items_awaiting_vendors({PROVIDER_ANTHROPIC_CLAUDE_CODE}) == {
        row["item_id"] for row in held
    }


def test_a_paused_model_leaves_every_question_of_a_wave_open(tmp_path: Path) -> None:
    """The completeness rule of 0c2614a under a wave and a paused arm.

    Three questions are scored at once while one Claude model is paused. Each
    one holds its six trials, so none of them is complete, and the next pass
    after the resume time finishes all three in place: one row each pass, the
    latest row per question, and no trial called twice.
    """
    items = ["aqa-h1", "aqa-h2", "aqa-h3"]
    db = state_db(tmp_path, chapter3=items)
    ledger_file = construction_ledger(tmp_path, {f"family-{i}": ["0.10"] for i in items})
    # The item bound counts the questions, not the passes, and a revisit needs
    # room under it: at three items and a bound of three the second pass meets
    # the bound instead of the held trials.
    auth = authorization(tmp_path, db, maximum_items=6)
    work = tmp_path / "wave-held"
    held = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        item_workers=3,
        pause_models=parse_pause_models([f"claude-opus-5={future_resume_utc()}"]),
    )
    assert held["paused_models"] == ["claude-opus-5"]
    journal = CostJournal(work)
    rows = journal.item_rows()
    assert len(rows) == 3
    for row in rows:
        assert row["evaluation"]["models_paused"] == ["claude-opus-5"]
        assert row["evaluation"]["pending_paused_trials"] == 6
        assert row["evaluation"]["recorded_trials"] == 42
        assert row["complete"] is False
    assert journal.completed_item_ids() == set()
    assert journal.held_item_ids() == set(items)
    # While the model stays paused the wave leaves every one of them alone.
    again = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        item_workers=3,
        pause_models=parse_pause_models([f"claude-opus-5={future_resume_utc()}"]),
    )
    assert again["items_this_invocation"] == []
    assert len(CostJournal(work).item_rows()) == 3
    # After the resume time the held trials run, in the same run directories.
    done = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        item_workers=3,
        pause_models=parse_pause_models(["claude-opus-5=2026-09-16T00:00:00Z"]),
    )
    assert done["paused_models"] == []
    journal = CostJournal(work)
    assert len(journal.item_rows()) == 6
    latest = journal.latest_item_rows()
    assert len(latest) == 3
    for row in latest:
        assert row["evaluation"]["recorded_trials"] == 48
        assert row["evaluation"]["pending_paused_trials"] == 0
        assert row["complete"] is True
        assert row["outcomes_by_model"]["claude-opus-5"]["N1"] == 3
    assert journal.completed_item_ids() == set(items)
    assert journal.cumulative()["items"] == 3
    assert journal.cumulative()["trials"] == 3 * 48


def test_the_ceiling_precheck_keeps_room_for_every_question_in_flight(
    tmp_path: Path,
) -> None:
    """Eight questions at once must not pass the authorized Gemini bound.

    The check runs under the admission lock, so it sees the questions already
    in flight. Their Gemini spend is not in the journal yet, so the bound
    needs room for all of them plus the one it is about to admit.
    """
    from arctic_qa.abstention_watch import _ceiling_precheck

    db = state_db(tmp_path, chapter3=["aqa-c1"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-c1": ["0.10"]})
    plan = load_plan(PLAN_FILE)
    price_config = json.loads(PRICES.read_text(encoding="utf-8"))
    policy = json.loads(POLICY_V2.read_text(encoding="utf-8"))
    estimate = estimated_gemini_item_usd(
        price_config,
        plan["vendors"][PROVIDER_GOOGLE_GEMINI]["models"],
        list(plan["arms"]),
        int(plan["repeats"]),
        output_cap=int(policy["maximum_output_tokens_including_thinking"]),
    )
    auth_file = authorization(
        tmp_path, db, maximum_gemini_usd=str(estimate * Decimal("1.5"))
    )
    authorization_record_value = json.loads(auth_file.read_text(encoding="utf-8"))
    journal = CostJournal(tmp_path / "precheck")
    arguments = dict(
        authorization=authorization_record_value,
        plan=plan,
        price_config=price_config,
        journal=journal,
        shared_ledger_file=ledger_file,
    )
    # Room for one question, and no room for a second one beside it.
    assert _ceiling_precheck(**arguments, items_in_flight=0) is None
    pause = _ceiling_precheck(**arguments, items_in_flight=1)
    assert pause is not None
    assert pause["reason"] == "one more item would pass the authorized Gemini USD bound"
    assert pause["items_in_flight"] == 1
    assert pause["estimate_usd"] == str(estimate)


def test_the_item_workers_are_bounded(tmp_path: Path) -> None:
    db = state_db(tmp_path, chapter3=["aqa-b1"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-b1": ["0.10"]})
    auth = authorization(tmp_path, db)
    for workers in (0, -1, MAXIMUM_ITEM_WORKERS + 1):
        with pytest.raises(ValueError, match="item workers"):
            scripted_watch(
                db=db,
                work_dir=tmp_path / f"bounded-{workers}",
                ledger_file=ledger_file,
                authorization_file=auth,
                item_workers=workers,
            )


def test_one_poll_cycle_scores_a_bounded_number_of_questions(tmp_path: Path) -> None:
    """A backlog must not hold the poll cycle for hours.

    The cycle does the work that belongs to no single question: it reads the
    shared ledger for a released ambiguous charge, it rebuilds the set of
    questions a paused arm holds, and it meets the questions the producer
    accepted since. One wave of every pending question would hold all of that
    for as long as the backlog takes.
    """
    items = [f"aqa-c{index}" for index in range(9)]
    db = state_db(tmp_path, chapter3=items)
    ledger_file = construction_ledger(tmp_path, {f"family-{i}": ["0.10"] for i in items})
    auth = authorization(tmp_path, db, maximum_items=9)
    work = tmp_path / "bounded-cycle"
    result = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        item_workers=2,
    )
    # Two workers and four waves a cycle: eight of the nine questions.
    assert len(result["items_this_invocation"]) == 2 * WAVE_CYCLES
    assert [row["item_id"] for row in result["items_this_invocation"]] == items[:8]
    # The ninth question is the first work of the next cycle.
    again = scripted_watch(
        db=db,
        work_dir=work,
        ledger_file=ledger_file,
        authorization_file=auth,
        item_workers=2,
    )
    assert [row["item_id"] for row in again["items_this_invocation"]] == items[8:]


def test_a_busy_lock_in_an_item_result_never_ends_the_watch(tmp_path: Path) -> None:
    """The route the error really takes is a journalled item result.

    `evaluate_item` catches what the plan raises, journals the question's row
    and hands the text back in `result["error"]`. The watch collected that
    text, halted the wave and raised, and the unit exited 1 at 12:14:20 UTC on
    2026-09-17 and again at 12:49:45 UTC, the second time with a containment
    written around the call to `evaluate_item`, which this path walks past.
    """
    import arctic_qa.abstention_watch as module

    assert is_lock_busy_error(
        "BrokerOperationBusyError: another paid broker operation is active"
    )
    assert is_lock_busy_error(f"not_submitted: {OPERATION_LOCK_BUSY_REASON}")
    assert not is_lock_busy_error("RuntimeError: the harness exited with 1")
    assert not is_lock_busy_error(None)

    db = state_db(tmp_path / "db", chapter3=["aqa-a", "aqa-b"])
    ledger_file = construction_ledger(
        tmp_path, {"family-aqa-a": ["0.01"], "family-aqa-b": ["0.01"]}
    )
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "result-busy"
    events: list[dict] = []
    seen: list[str] = []

    original = module.evaluate_item

    def busy_result(*, item_id: str, **changes):  # type: ignore[no-untyped-def]
        seen.append(item_id)
        result = original(item_id=item_id, **changes)
        if len(seen) == 1:
            result["error"] = (
                "BrokerOperationBusyError: another paid broker operation is active"
            )
        return result

    module.evaluate_item = busy_result  # type: ignore[assignment]
    try:
        result = scripted_watch(
            db=db,
            work_dir=work,
            ledger_file=ledger_file,
            authorization_file=auth,
            item_workers=1,
            log=events.append,
        )
    finally:
        module.evaluate_item = original  # type: ignore[assignment]

    # No raise, no halted wave, and the second question was taken.
    assert result["errors"] == []
    assert seen == ["aqa-a", "aqa-b"]
    assert [
        row["item_id"] for row in events if row["event"] == "item_lock_busy"
    ] == ["aqa-a"]


def test_the_watch_raises_for_a_real_error(tmp_path: Path) -> None:
    """The containment is for the busy lock alone, never for anything else."""
    import arctic_qa.abstention_watch as module

    db = state_db(tmp_path / "db", chapter3=["aqa-a"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-a": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=4)
    original = module.evaluate_item

    def broken(*, item_id: str, **changes):  # type: ignore[no-untyped-def]
        result = original(item_id=item_id, **changes)
        result["error"] = "RuntimeError: the evaluation set is unreadable"
        return result

    module.evaluate_item = broken  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="unreadable"):
            scripted_watch(
                db=db,
                work_dir=tmp_path / "real",
                ledger_file=ledger_file,
                authorization_file=auth,
                item_workers=1,
            )
    finally:
        module.evaluate_item = original  # type: ignore[assignment]


def test_a_busy_lock_around_the_plan_never_ends_the_watch(tmp_path: Path) -> None:
    """A bounded lock wait belongs to one question, wherever it is raised.

    A vendor that reports the busy lock is already item-scoped. Raised in the
    frame around the plan instead, it was this question's error, and one such
    error halts the wave and ends the watch non-zero. Every question of the
    wave raised it at 12:14:20 UTC on 2026-09-17 and the unit exited 1.
    """
    import arctic_qa.abstention_watch as module

    db = state_db(tmp_path / "db", chapter3=["aqa-a", "aqa-b"])
    ledger_file = construction_ledger(
        tmp_path, {"family-aqa-a": ["0.01"], "family-aqa-b": ["0.01"]}
    )
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "busy"
    events: list[dict] = []
    seen: list[str] = []

    original = module.evaluate_item

    def busy_first(*, item_id: str, **changes):  # type: ignore[no-untyped-def]
        seen.append(item_id)
        if len(seen) == 1:
            raise BrokerOperationBusyError(
                "another paid broker operation is active"
            )
        return original(item_id=item_id, **changes)

    module.evaluate_item = busy_first  # type: ignore[assignment]
    try:
        result = scripted_watch(
            db=db,
            work_dir=work,
            ledger_file=ledger_file,
            authorization_file=auth,
            item_workers=1,
            log=events.append,
        )
    finally:
        module.evaluate_item = original  # type: ignore[assignment]

    # The watch did not end, and the wave took its other question.
    assert result["errors"] == []
    assert seen == ["aqa-a", "aqa-b"]
    contained = [row for row in events if row["event"] == "item_lock_busy"]
    assert [row["item_id"] for row in contained] == ["aqa-a"]
    assert [row for row in events if row["event"] == "item_failed"] == []
    # The question it belongs to keeps its place: it has no journal row, so a
    # later pass takes it up and runs the whole plan on it.
    assert "aqa-a" not in {row["item_id"] for row in CostJournal(work).item_rows()}


def test_a_full_slot_or_minute_belongs_to_one_question() -> None:
    """A refusal that describes the moment must not take an arm down.

    The broker names the concurrency slots and the minute window together as
    the two refusals that describe the moment and not the request: `execute`
    waits a bounded time for room before it records one, nothing is reserved,
    submitted or charged, and the next question meets an emptier window.

    The evaluator treated them as a vendor stop, so the Gemini arm went dark
    for the rest of the invocation the first time a wave filled the slots. It
    happened at 11:55 UTC on 2026-09-17, minutes after the arm got fast enough
    to fill them.
    """
    for reason in TRANSIENT_RESERVATION_REASONS:
        assert reason in ITEM_SCOPED_REASONS
        assert is_item_scoped_reason(f"not_submitted: {reason}") is True
    # A budget wall, a harness fault and an ambiguous charge still pause the
    # arm, because none of them is about this question alone.
    assert is_item_scoped_reason(f"not_submitted: {EVALUATION_CEILING_REASON}") is False
    assert is_item_scoped_reason("failed: the harness exited with 1") is False
    assert is_item_scoped_reason("ambiguous_charge") is False


def test_a_busy_operation_lock_belongs_to_one_question(tmp_path: Path) -> None:
    """The exclusive lock of the shared ledger must not take an arm down.

    A `BrokerOperationBusyError` reserves nothing, submits nothing and charges
    nothing: the producer skips that paper and goes on. A wave meets it more
    often, because four Gemini threads queue on that lock, and it paused the
    Gemini arm of the live evaluator at 09:58 UTC on 2026-09-17 until an
    operator restarted the unit.
    """
    busy = f"not_submitted: {OPERATION_LOCK_BUSY_REASON}"
    assert is_item_scoped_reason(busy) is True
    assert OPERATION_LOCK_BUSY_REASON in ITEM_SCOPED_REASONS
    assert EVALUATION_ITEM_REPEAT_REASON in ITEM_SCOPED_REASONS
    # A budget wall, a harness fault and an ambiguous charge still pause it.
    assert is_item_scoped_reason(f"not_submitted: {EVALUATION_CEILING_REASON}") is False
    assert is_item_scoped_reason("failed: the harness exited with 1") is False
    assert is_item_scoped_reason("ambiguous_charge") is False

    db = state_db(tmp_path, chapter3=["aqa-busy1", "aqa-busy2"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-busy1": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=2)
    work = tmp_path / "busy-lock"

    import arctic_qa.abstention_watch as module

    stops = {"count": 0}
    original_reason = module.vendor_stop_reason

    def fake_reason(summary: dict, vendor: str) -> str | None:
        # The first question meets the busy lock on its Gemini arm.
        if vendor == PROVIDER_GOOGLE_GEMINI and stops["count"] < 1:
            stops["count"] += 1
            return busy
        return None

    module.vendor_stop_reason = fake_reason  # type: ignore[assignment]
    try:
        result = scripted_watch(
            db=db,
            work_dir=work,
            ledger_file=ledger_file,
            authorization_file=auth,
            item_workers=1,
        )
    finally:
        module.vendor_stop_reason = original_reason  # type: ignore[assignment]
    # The arm kept its place for the second question, and no pause was
    # journalled.
    assert result["paused_vendors"] == {}
    assert PROVIDER_GOOGLE_GEMINI in result["active_vendors"]
    assert [
        row for row in CostJournal(work).rows() if row.get("kind") == "vendor_pause"
    ] == []
    assert len(result["items_this_invocation"]) == 2


# --- The wave keeps every arm busy ----------------------------------------

VENDORS = ["google_gemini", "anthropic_claude_code", "openai_codex"]


def _backlog(claude_only: int, gemini_open: int) -> tuple[list[str], dict]:
    """The queue of 2026-09-17: a Claude backlog in front of the Gemini work."""
    pending = [f"claude-{index:02d}" for index in range(claude_only)]
    pending += [f"open-{index:02d}" for index in range(gemini_open)]
    owed = {item: {"anthropic_claude_code"} for item in pending[:claude_only]}
    owed.update({item: set(VENDORS) for item in pending[claude_only:]})
    return pending, owed


def test_each_arm_keeps_a_share_of_every_wave() -> None:
    """The oldest-first order left the Gemini arm with nothing to do.

    The captain's Claude pause of 2026-09-17 left 33 questions that owed
    Claude alone at the front of the queue. Eight of eight slots took them,
    so the Gemini arm made no paid call between 10:03 and 11:06 UTC while 23
    questions owed it trials.
    """
    pending, owed = _backlog(claude_only=33, gemini_open=23)
    # Before: the whole wave owes one arm.
    assert all(item.startswith("claude-") for item in pending[:8])

    ordered = wave_order(
        pending, open_vendors=owed, vendors=VENDORS, slots=8, limit=32
    )
    wave = ordered[:8]
    for vendor in VENDORS:
        open_here = sum(1 for item in wave if vendor in owed[item])
        assert open_here >= 3, (vendor, wave)
    # Nothing is lost and nothing is repeated.
    assert sorted(ordered) == sorted(pending)
    assert len(set(ordered)) == len(ordered)


def test_every_wave_of_the_backlog_is_mixed_not_only_the_first() -> None:
    """The limit covers the waves the caller keeps, and each one is mixed."""
    pending, owed = _backlog(claude_only=33, gemini_open=23)
    ordered = wave_order(
        pending, open_vendors=owed, vendors=VENDORS, slots=8, limit=32
    )
    for start in range(0, 32, 8):
        wave = ordered[start : start + 8]
        gemini = sum(1 for item in wave if "google_gemini" in owed[item])
        assert gemini >= 3, (start, wave)


def test_the_pick_up_order_holds_inside_each_group() -> None:
    """A reorder is a share of the wave, never a queue that jumps at random."""
    pending, owed = _backlog(claude_only=10, gemini_open=10)
    ordered = wave_order(
        pending, open_vendors=owed, vendors=VENDORS, slots=8, limit=16
    )
    for prefix in ("claude-", "open-"):
        group = [item for item in ordered if item.startswith(prefix)]
        assert group == [item for item in pending if item.startswith(prefix)]


def test_a_question_the_journal_never_saw_owes_every_arm() -> None:
    """A question with no journal row is the whole plan, so it feeds every arm."""
    pending = ["fresh-0", "fresh-1", "fresh-2"]
    ordered = wave_order(
        pending, open_vendors={}, vendors=VENDORS, slots=8, limit=8
    )
    assert ordered == pending


def test_an_arm_with_no_open_question_takes_no_slot() -> None:
    """A share is for an arm that has a question to give it, and no other."""
    pending = [f"claude-{index}" for index in range(8)]
    owed = {item: {"anthropic_claude_code"} for item in pending}
    ordered = wave_order(
        pending, open_vendors=owed, vendors=VENDORS, slots=8, limit=8
    )
    assert ordered == pending


def test_one_vendor_leaves_the_pick_up_order_alone() -> None:
    """With one arm there is nothing to share, so the order does not move."""
    pending = [f"item-{index}" for index in range(12)]
    ordered = wave_order(
        pending,
        open_vendors={item: {"google_gemini"} for item in pending},
        vendors=["google_gemini"],
        slots=8,
        limit=32,
    )
    assert ordered == pending


def test_the_journal_says_which_arms_a_question_still_owes(tmp_path: Path) -> None:
    """`outcomes_by_model` counts the trials, and a short count is an open arm."""
    journal = CostJournal(tmp_path)
    journal.append(
        {
            "schema": "abstention-eval-cost-row-v1",
            "item_id": "aqa-1",
            "recorded_at_utc": "2026-09-17T11:00:00Z",
            "evaluation": {"complete": False},
            "outcomes_by_model": {
                "gemini-3.8-flash": {"N1": 6},
                "gemini-3.7-flash": {"N4": 6},
                "claude-opus-5": {"N1": 2},
            },
        }
    )
    owed = journal.open_vendors_by_item(
        models_by_vendor={
            "google_gemini": ["gemini-3.8-flash", "gemini-3.7-flash"],
            "anthropic_claude_code": ["claude-opus-5", "claude-sonnet-5"],
        },
        trials_per_model=6,
    )
    assert owed == {"aqa-1": {"anthropic_claude_code"}}


# --- A trial the provider never saw keeps the question open -------------------


class _BusyOnceProvider(ScriptedEvaluationProvider):
    """Refuse the first ``refusals`` attempts with the exclusive-lock refusal."""

    def __init__(self, *, refusals: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.remaining = refusals
        self.lock = threading.Lock()

    def answer(self, request):
        with self.lock:
            refuse = self.remaining > 0
            if refuse:
                self.remaining -= 1
        if refuse:
            raise BrokerOperationBusyError(OPERATION_LOCK_BUSY_REASON)
        return super().answer(request)


def _watch_with(
    providers, *, db: Path, work: Path, ledger_file: Path, auth: Path, **changes
) -> dict:
    """Run one invocation of the watcher with a provider per vendor."""
    import arctic_qa.abstention_watch as module

    def fake_build(*, plan, set_dir, run_id, gate_dir, vendors, **_: object):
        return {
            vendor: VendorRun(
                vendor=vendor,
                provider=providers(vendor, run_id),
                decoding={"scripted": True},
                models=plan["vendors"][vendor]["models"],
                concurrency=1,
            )
            for vendor in vendors
        }

    original = module.build_vendor_runs
    module.build_vendor_runs = fake_build  # type: ignore[assignment]
    try:
        return watch(
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
            item_workers=1,
            **changes,
        )
    finally:
        module.build_vendor_runs = original  # type: ignore[assignment]


def test_a_busy_lock_leaves_the_question_open_and_the_next_pass_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question the busy lock touched is owed, never closed short.

    The containment of 88d2384 kept the unit alive but recorded the busy lock
    as a vendor stop on the question, and the journal still called the
    question complete: 73 of the 155 closed questions of the live streaming-r11
    work directory hold fewer than their 48 responses and none of them was
    ever taken up again. A trial the provider never saw reserved nothing,
    submitted nothing and charged nothing, so the question stays open and the
    next pass runs exactly the trials that are missing.
    """
    monkeypatch.setattr("arctic_qa.abstention_plan.PRE_PROVIDER_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr("arctic_qa.abstention_plan.PRE_PROVIDER_RETRY_ROUNDS", 1)
    db = state_db(tmp_path, chapter3=["aqa-open"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-open": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=2)
    work = tmp_path / "busy-open"

    def first(vendor: str, run_id: str):
        if vendor == PROVIDER_GOOGLE_GEMINI:
            return _BusyOnceProvider(refusals=1, policy="gold", seed=run_id)
        return ScriptedEvaluationProvider(policy="gold", seed=run_id)

    result = _watch_with(
        first, db=db, work=work, ledger_file=ledger_file, auth=auth
    )
    assert result["errors"] == []
    # The arm is not paused and the question is not a vendor stop.
    assert result["paused_vendors"] == {}
    journal = CostJournal(work)
    assert [row for row in journal.rows() if row.get("kind") == "vendor_pause"] == []
    row = journal.item_rows()[0]
    assert row["evaluation"]["pending_deferred_trials"] == 1
    assert row["evaluation"]["recorded_trials"] == 47
    assert row["evaluation"]["complete"] is False
    assert row["complete"] is False
    assert row["evaluation"]["deferred_reasons"] == [
        f"BrokerOperationBusyError: {OPERATION_LOCK_BUSY_REASON}"
    ]
    # This is the strand test: the question must still be owed.
    assert journal.completed_item_ids() == set()
    assert journal.row_is_complete(row) is False

    def clean(vendor: str, run_id: str):
        return ScriptedEvaluationProvider(policy="gold", seed=run_id)

    again = _watch_with(clean, db=db, work=work, ledger_file=ledger_file, auth=auth)
    assert again["errors"] == []
    later = CostJournal(work).latest_item_rows()[0]
    assert later["evaluation"]["recorded_trials"] == 48
    assert later["evaluation"]["pending_deferred_trials"] == 0
    assert later["evaluation"]["complete"] is True
    assert CostJournal(work).completed_item_ids() == {row["item_id"]}


def test_a_row_that_owes_a_deferred_trial_is_never_read_as_complete() -> None:
    """Every reader of the journal owes such a question its missing trials."""
    row = {
        "item_id": "aqa-deferred",
        "evaluation": {
            "planned_trials": 48,
            "recorded_trials": 38,
            "vendors_paused": [],
            "models_paused": [],
            "pending_paused_trials": 0,
            "pending_deferred_trials": 10,
            "complete": False,
        },
    }
    assert CostJournal.row_is_complete(row) is False
    # And the flag alone is not trusted: a writer that sets it wrongly, which
    # is exactly what closed 73 live questions, is still caught by the count.
    row["evaluation"]["complete"] = True
    assert CostJournal.row_is_complete(row) is False


def test_a_vanished_harness_binary_resumes_the_arm_when_it_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pause that describes the machine lifts itself, like the Gemini one.

    The Claude Code binary was reinstalled at 12:06 UTC on 2026-09-17 and the
    path was gone for a moment. The arm was paused at 12:06, 12:13, 12:48 and
    13:19 UTC; the binary was back within the minute each time, and nothing
    asked. The arm made no call between 13:21 UTC and the restart.
    """
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir(parents=True)
    entry = {"binary": str(binary), "call_timeout_seconds": 600, "models": {}}
    monkeypatch.setattr(
        "arctic_qa.abstention_watch.vendor_entry",
        lambda config, vendor: entry,
    )
    db = state_db(tmp_path, chapter3=["aqa-gone", "aqa-back"])
    ledger_file = construction_ledger(tmp_path, {"family-aqa-gone": ["0.01"]})
    auth = authorization(tmp_path, db, maximum_items=2)
    work = tmp_path / "harness-returns"

    class _VanishedHarnessProvider(ScriptedEvaluationProvider):
        """The shape the transport recorded before the probe existed."""

        def answer(self, request):
            from arctic_qa.abstention_providers import EvaluationResponse

            if binary.exists():
                return super().answer(request)
            # The package upgrade finishes right after it took the path away,
            # which is what the four pauses of 2026-09-17 looked like.
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
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
                error=(
                    "the harness exited with None: FileNotFoundError: "
                    f"[Errno 2] No such file or directory: '{binary}'"
                ),
            )

    def providers(vendor: str, run_id: str):
        if vendor == PROVIDER_ANTHROPIC_CLAUDE_CODE:
            return _VanishedHarnessProvider(policy="gold", seed=run_id)
        return ScriptedEvaluationProvider(policy="gold", seed=run_id)

    result = _watch_with(
        providers,
        db=db,
        work=work,
        ledger_file=ledger_file,
        auth=auth,
        vendors=[PROVIDER_ANTHROPIC_CLAUDE_CODE, PROVIDER_OPENAI_CODEX],
    )
    assert result["errors"] == []
    # The arm came back for the second question without a restart.
    assert result["paused_vendors"] == {}
    assert PROVIDER_ANTHROPIC_CLAUDE_CODE in result["active_vendors"]
    journal = CostJournal(work)
    kinds = [row.get("kind") for row in journal.rows()]
    assert "vendor_pause" in kinds
    assert "vendor_resume" in kinds
    rows = journal.item_rows()
    assert rows[1]["evaluation"]["vendors_paused"] == []
    assert rows[1]["evaluation"]["recorded_trials"] == 36


def test_a_harness_pause_is_told_apart_from_every_other_pause() -> None:
    """Only a pause about the machine may lift itself on a probe."""
    assert (
        is_harness_unavailable_reason(
            "failed: the harness exited with None: FileNotFoundError: "
            "[Errno 2] No such file or directory: '/home/ben/.npm-global/bin/claude'"
        )
        is True
    )
    assert (
        is_harness_unavailable_reason(
            "failed: the anthropic_claude_code harness binary is not executable "
            "now: /home/ben/.npm-global/bin/claude"
        )
        is True
    )
    # A harness that ran and failed, a budget wall and an ambiguous charge all
    # describe the run, not the machine, and none of them lifts on a probe.
    assert is_harness_unavailable_reason("failed: the harness exited with 1:") is False
    assert is_harness_unavailable_reason(f"not_submitted: {EVALUATION_CEILING_REASON}") is False
    assert is_harness_unavailable_reason("ambiguous_charge: ...") is False
    assert is_harness_unavailable_reason(None) is False


def test_a_vanished_harness_binary_around_the_plan_never_ends_the_watch(
    tmp_path: Path,
) -> None:
    """A harness that will not start belongs to one question, not to the watch.

    The read of the harness version runs while the question builds its run,
    which is the frame around the plan and not the trial's own bounded wait.
    It reserved nothing, submitted nothing and charged nothing, and the binary
    is usually back within the minute. Ending the watch on it took the unit
    down at 16:04:18 UTC on 2026-09-17, two minutes after it took up a wave.
    """
    import arctic_qa.abstention_watch as module

    db = state_db(tmp_path / "db", chapter3=["aqa-a", "aqa-b"])
    ledger_file = construction_ledger(
        tmp_path, {"family-aqa-a": ["0.01"], "family-aqa-b": ["0.01"]}
    )
    auth = authorization(tmp_path, db, maximum_items=4)
    work = tmp_path / "gone"
    events: list[dict] = []
    seen: list[str] = []

    original = module.evaluate_item

    def missing_first(*, item_id: str, **changes):  # type: ignore[no-untyped-def]
        seen.append(item_id)
        if len(seen) == 1:
            raise HarnessUnavailableError(
                "the anthropic_claude_code harness binary is not executable "
                "now: /home/ben/.npm-global/bin/claude"
            )
        return original(item_id=item_id, **changes)

    module.evaluate_item = missing_first  # type: ignore[assignment]
    try:
        result = scripted_watch(
            db=db,
            work_dir=work,
            ledger_file=ledger_file,
            authorization_file=auth,
            item_workers=1,
            log=events.append,
        )
    finally:
        module.evaluate_item = original  # type: ignore[assignment]

    assert result["errors"] == []
    assert seen == ["aqa-a", "aqa-b"]
    contained = [row for row in events if row["event"] == "item_harness_unavailable"]
    assert [row["item_id"] for row in contained] == ["aqa-a"]
    assert [row for row in events if row["event"] == "item_failed"] == []
    # The question keeps its place: it has no journal row, so a later pass
    # takes it up and runs the whole plan on it.
    assert "aqa-a" not in {row["item_id"] for row in CostJournal(work).item_rows()}


def test_the_version_read_of_a_missing_binary_is_a_harness_pause() -> None:
    """The message of the version read is read as a harness that would not start.

    `binary_version` raises it while the question builds its run. The watch
    must read it exactly as it reads the probe's own refusal, so the arm
    resumes by itself when the binary is back.
    """
    assert is_harness_unavailable_reason(
        "the anthropic_claude_code harness binary is not executable now: "
        "/home/ben/.npm-global/bin/claude: FileNotFoundError: [Errno 2] "
        "No such file or directory"
    )
    assert not is_harness_unavailable_reason(
        "/home/ben/.npm-global/bin/claude --version failed: the login expired"
    )
