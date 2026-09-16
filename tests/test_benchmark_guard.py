from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.benchmark_guard import (
    CODEX_ATTRIBUTION_WINDOW_SECONDS,
    CODEX_WEEKLY_FLOOR_PERCENT,
    FABLE_MODEL,
    GUARD_LOG_FILENAME,
    GUARD_MEMORY_FILENAME,
    GUARD_OWNER,
    GUARD_STATE_FILENAME,
    PAUSE_SCHEMA,
    PLAN_MODELS_BY_VENDOR,
    REPAUSE_HOLD_SECONDS,
    RESUME_CLEAR_CYCLES,
    VENDOR_ANTHROPIC_CLAUDE_CODE,
    VENDOR_GOOGLE_GEMINI,
    VENDOR_OPENAI_CODEX,
    BenchmarkGuard,
    active_pauses,
    evaluate_rules,
    evaluation_phase_totals,
    expected_questions,
    extrapolate,
    model_readings,
    plan_models,
    quota_windows,
    read_pause_file,
    select_pause_model,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"
RECORDED_QUOTA = FIXTURES / "quota-axi-2026-09-16T10-41Z.json"
CLAUDE_SESSION_LOW = FIXTURES / "quota-axi-claude-session-low.json"
FABLE_LOW = FIXTURES / "quota-axi-fable-weekly-low.json"
CODEX_LOW = FIXTURES / "quota-axi-codex-weekly-low.json"
CODEX_EXHAUSTION_CLEAR = FIXTURES / "quota-axi-codex-exhaustion-clear.json"

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
CYCLE = timedelta(seconds=300)
# The live guard's Codex weekly window resets on 2026-09-20; a projection
# before that date is what the exhaustion rule reads.
EXHAUSTION_BEFORE_RESET = "2026-09-17T08:25:34.709Z"
EXHAUSTION_AFTER_RESET = "2026-09-21T00:00:00.000Z"


def windows_of(path: Path) -> dict:
    return quota_windows(json.loads(path.read_text(encoding="utf-8")))


def gemini_block(
    *, spend: str, evaluated: int, still: int, budget: str = "200.00"
) -> dict:
    block = extrapolate(
        spend_so_far=Decimal(spend),
        questions_evaluated=evaluated,
        questions_still_expected=still,
    )
    block["budget_usd"] = budget
    return block


def rule(findings: list[dict], name: str) -> dict:
    return next(finding for finding in findings if finding["rule"] == name)


def journal_row(
    item_id: str,
    *,
    gemini_usd: str = "0.000000",
    generation_usd: str = "0.500000",
    outcomes: dict | None = None,
    subscription: dict | None = None,
    complete: bool = True,
) -> dict:
    return {
        "schema": "abstention-eval-cost-row-v1",
        "kind": "item",
        "item_id": item_id,
        "family_id": f"family-{item_id}",
        "run_id": f"abstention-stream-r2-{item_id}",
        "recorded_at_utc": "2026-09-16T09:15:21Z",
        "complete": complete,
        "generation": {
            "campaign_id": "arctic-qa-production-campaign-003",
            "campaign_usd_per_accepted_item": "0.582476",
            "family": {"usd": generation_usd, "paid_calls": 15},
        },
        "evaluation": {
            "google_gemini": {"usd": gemini_usd, "charged_calls": 12},
            "subscription": subscription or {},
            "subscription_tokens": {"input": 100, "output": 10, "thinking": 20},
            "subscription_list_price_equivalent_usd": "0.741488",
            "recorded_trials": 36,
            "planned_trials": 48,
            "wall_seconds": 102.35,
            "vendors_paused": [],
        },
        "outcomes_by_model": outcomes or {},
        "invalid_count": 0,
        "invalid_rate": 0.0,
        "cumulative": {"items": 1},
    }


# --- The extrapolation arithmetic -------------------------------------------------


def test_expected_questions_divides_the_remaining_allocation_by_the_item_cost() -> None:
    record = expected_questions(
        accepted_now=32,
        construction_allocation_usd=Decimal("500.00"),
        construction_spent_usd=Decimal("57.690742"),
        questions_evaluated=5,
    )
    # 500.00 - 57.690742 = 442.309258 remaining; 57.690742 / 32 = 1.802836 per item.
    assert record["construction_remaining_usd"] == "442.309258"
    assert record["usd_per_accepted_item"] == "1.802836"
    assert record["further_accepted_items"] == 245
    assert record["expected_total_questions"] == 277
    assert record["questions_still_expected"] == 272
    assert record["binding_bound"] == "allocation"


def test_expected_questions_stops_at_the_accepted_question_target() -> None:
    record = expected_questions(
        accepted_now=32,
        construction_allocation_usd=Decimal("500.00"),
        construction_spent_usd=Decimal("10.00"),
        questions_evaluated=5,
        accepted_question_target=500,
    )
    assert record["allocation_total_questions"] > 500
    assert record["expected_total_questions"] == 500
    assert record["binding_bound"] == "accepted_question_target"
    assert record["questions_still_expected"] == 495


def test_expected_questions_never_goes_negative_past_the_allocation() -> None:
    record = expected_questions(
        accepted_now=40,
        construction_allocation_usd=Decimal("100.00"),
        construction_spent_usd=Decimal("140.00"),
        questions_evaluated=60,
    )
    assert record["construction_remaining_usd"] == "0.000000"
    assert record["further_accepted_items"] == 0
    assert record["expected_total_questions"] == 40
    assert record["questions_still_expected"] == 0


def test_expected_questions_without_an_accepted_item_projects_nothing() -> None:
    record = expected_questions(
        accepted_now=0,
        construction_allocation_usd=Decimal("500.00"),
        construction_spent_usd=Decimal("0"),
        questions_evaluated=0,
    )
    assert record["usd_per_accepted_item"] is None
    assert record["expected_total_questions"] == 0


def test_extrapolate_projects_the_total_at_the_observed_cost_per_question() -> None:
    record = extrapolate(
        spend_so_far=Decimal("12.50"),
        questions_evaluated=25,
        questions_still_expected=275,
    )
    # 12.50 / 25 = 0.50 per question; 12.50 + 0.50 * 275 = 150.00.
    assert record["usd_per_question"] == "0.500000"
    assert record["extrapolated_total_usd"] == "150.000000"


def test_extrapolate_reports_no_projection_before_the_first_question() -> None:
    record = extrapolate(
        spend_so_far=Decimal("0"), questions_evaluated=0, questions_still_expected=300
    )
    assert record["usd_per_question"] is None
    assert record["extrapolated_total_usd"] is None


def test_evaluation_phase_totals_count_reservations_and_ambiguous_charges() -> None:
    ledger = {
        "spent_usd": "60.00",
        "requests": {
            "a": {
                "phase": "benchmark_evaluation",
                "state": "completed",
                "stage": "evaluation_answer:gemini-3.8-flash",
                "family_id": "evaluation-item:aqa-1",
                "actual_cost_usd": "0.040000",
                "usage": {"promptTokenCount": 10, "candidatesTokenCount": 1},
            },
            "b": {
                "phase": "benchmark_evaluation",
                "state": "submitted",
                "stage": "evaluation_answer:gemini-3.7-flash",
                "family_id": "evaluation-item:aqa-1",
                "reserved_usd": "0.020000",
            },
            "c": {
                "phase": "benchmark_evaluation",
                "state": "ambiguous_charge",
                "stage": "evaluation_answer:gemini-3.7-flash",
                "family_id": "evaluation-item:aqa-2",
                "reserved_usd": "0.010000",
            },
            "d": {
                "phase": "away_production",
                "state": "completed",
                "stage": "question_generation",
                "actual_cost_usd": "5.000000",
            },
        },
    }
    totals = evaluation_phase_totals(ledger)
    assert totals["spent_usd"] == "0.040000"
    assert totals["reserved_usd"] == "0.020000"
    assert totals["ambiguous_usd"] == "0.010000"
    assert totals["used_usd"] == "0.070000"
    assert totals["questions"] == 2
    assert totals["by_model"]["gemini-3.8-flash"]["used_usd"] == "0.040000"
    assert totals["by_model"]["gemini-3.7-flash"]["used_usd"] == "0.030000"


# --- The pause rules against recorded quota-axi output ------------------------------


def test_recorded_quota_report_flattens_to_the_four_guarded_windows() -> None:
    windows = windows_of(RECORDED_QUOTA)
    assert windows["claude_session"]["percent_remaining"] == 84
    assert windows["claude_week"]["percent_remaining"] == 34
    assert windows["claude_fable"]["percent_remaining"] == 15
    assert windows["codex_weekly"]["percent_remaining"] == 23
    assert windows["codex_weekly"]["resets_at_utc"] == "2026-09-20T10:08:04.000Z"


def test_recorded_quota_fires_no_rule_at_the_present_spend() -> None:
    findings = evaluate_rules(
        gemini=gemini_block(spend="0.153364", evaluated=5, still=272),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("0.153364"),
        windows=windows_of(RECORDED_QUOTA),
        now=NOW,
    )
    assert [finding["rule"] for finding in findings if finding["fired"]] == []


def test_gemini_extrapolation_over_the_budget_fires_on_the_gemini_models() -> None:
    findings = evaluate_rules(
        gemini=gemini_block(spend="20.00", evaluated=20, still=280),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("20.00"),
        windows=windows_of(RECORDED_QUOTA),
        now=NOW,
    )
    finding = rule(findings, "gemini_extrapolated_over_budget")
    # 20.00 / 20 = 1.00 per question; 20.00 + 1.00 * 280 = 300.00 > 200.00.
    assert finding["fired"] is True
    assert finding["numbers"]["extrapolated_total_usd"] == "300.000000"
    assert finding["candidate_models"] == list(
        PLAN_MODELS_BY_VENDOR[VENDOR_GOOGLE_GEMINI]
    )


def test_gemini_extrapolation_exactly_on_the_budget_does_not_fire() -> None:
    findings = evaluate_rules(
        gemini=gemini_block(spend="20.00", evaluated=30, still=270),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("20.00"),
        windows=windows_of(RECORDED_QUOTA),
        now=NOW,
    )
    assert rule(findings, "gemini_extrapolated_over_budget")["fired"] is False


def test_evaluation_ceiling_margin_fires_inside_the_last_ten_dollars() -> None:
    findings = evaluate_rules(
        gemini=gemini_block(spend="192.00", evaluated=200, still=10),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("192.00"),
        windows=windows_of(RECORDED_QUOTA),
        now=NOW,
    )
    finding = rule(findings, "gemini_evaluation_ceiling_margin")
    assert finding["fired"] is True
    assert finding["numbers"]["remaining_usd"] == "8.000000"


def test_evaluation_ceiling_margin_is_inert_under_a_small_ceiling() -> None:
    findings = evaluate_rules(
        gemini=gemini_block(spend="0.15", evaluated=5, still=272),
        evaluation_ceiling_usd=Decimal("5.00"),
        evaluation_used_usd=Decimal("0.15"),
        windows=windows_of(RECORDED_QUOTA),
        now=NOW,
    )
    finding = rule(findings, "gemini_evaluation_ceiling_margin")
    assert finding["fired"] is False
    assert finding["numbers"]["rule_applies"] is False


def test_claude_session_floor_fires_on_the_recorded_low_session_window() -> None:
    findings = evaluate_rules(
        gemini=gemini_block(spend="1.00", evaluated=10, still=10),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("1.00"),
        windows=windows_of(CLAUDE_SESSION_LOW),
        now=NOW,
    )
    finding = rule(findings, "claude_session_window_floor")
    assert finding["fired"] is True
    assert finding["numbers"]["percent_remaining"] == "12"
    assert finding["candidate_models"] == list(
        PLAN_MODELS_BY_VENDOR[VENDOR_ANTHROPIC_CLAUDE_CODE]
    )


def test_fable_weekly_floor_holds_until_the_captains_resume_time() -> None:
    windows = windows_of(FABLE_LOW)
    resume = datetime(2026, 9, 16, 23, 0, tzinfo=UTC)
    before = evaluate_rules(
        gemini=gemini_block(spend="1.00", evaluated=10, still=10),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("1.00"),
        windows=windows,
        fable_rule_active_after=resume,
        now=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
    )
    after = evaluate_rules(
        gemini=gemini_block(spend="1.00", evaluated=10, still=10),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("1.00"),
        windows=windows,
        fable_rule_active_after=resume,
        now=datetime(2026, 9, 16, 23, 30, tzinfo=UTC),
    )
    assert rule(before, "fable_weekly_window_floor")["fired"] is False
    assert rule(after, "fable_weekly_window_floor")["fired"] is True
    assert rule(after, "fable_weekly_window_floor")["candidate_models"] == [FABLE_MODEL]


def test_fable_weekly_floor_ignores_the_recorded_fifteen_percent() -> None:
    findings = evaluate_rules(
        gemini=gemini_block(spend="1.00", evaluated=10, still=10),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("1.00"),
        windows=windows_of(RECORDED_QUOTA),
        now=datetime(2026, 9, 17, 1, 0, tzinfo=UTC),
    )
    assert rule(findings, "fable_weekly_window_floor")["fired"] is False


def test_codex_weekly_floor_fires_below_ten_percent() -> None:
    findings = evaluate_rules(
        gemini=gemini_block(spend="1.00", evaluated=10, still=10),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("1.00"),
        windows=windows_of(CODEX_LOW),
        now=NOW,
    )
    finding = rule(findings, "codex_weekly_window_floor")
    assert finding["fired"] is True
    assert Decimal(finding["numbers"]["percent_remaining"]) < CODEX_WEEKLY_FLOOR_PERCENT
    assert finding["candidate_models"] == list(
        PLAN_MODELS_BY_VENDOR[VENDOR_OPENAI_CODEX]
    )


def test_codex_projected_exhaustion_needs_the_benchmark_to_be_the_main_consumer() -> (
    None
):
    windows = windows_of(RECORDED_QUOTA)
    idle = evaluate_rules(
        gemini=gemini_block(spend="1.00", evaluated=10, still=10),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("1.00"),
        windows=windows,
        codex_attribution_record={"drives": False},
        now=NOW,
    )
    driving = evaluate_rules(
        gemini=gemini_block(spend="1.00", evaluated=10, still=10),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("1.00"),
        windows=windows,
        codex_attribution_record={"drives": True},
        now=NOW,
    )
    # The recorded report projects exhaustion on 2026-09-17, before the
    # 2026-09-20 weekly reset.
    assert rule(idle, "codex_projected_exhaustion")["numbers"]["projected_before_reset"]
    assert rule(idle, "codex_projected_exhaustion")["fired"] is False
    assert rule(driving, "codex_projected_exhaustion")["fired"] is True


def test_codex_projected_exhaustion_clears_when_the_projection_passes_the_reset() -> (
    None
):
    findings = evaluate_rules(
        gemini=gemini_block(spend="1.00", evaluated=10, still=10),
        evaluation_ceiling_usd=Decimal("200.00"),
        evaluation_used_usd=Decimal("1.00"),
        windows=windows_of(CODEX_EXHAUSTION_CLEAR),
        codex_attribution_record={"drives": True},
        now=NOW,
    )
    assert rule(findings, "codex_projected_exhaustion")["fired"] is False


def test_an_absent_quota_window_fires_nothing() -> None:
    findings = evaluate_rules(
        gemini=gemini_block(spend="1.00", evaluated=10, still=10),
        evaluation_ceiling_usd=None,
        evaluation_used_usd=Decimal("1.00"),
        windows={},
        codex_attribution_record={"drives": True},
        now=NOW,
    )
    assert [finding["rule"] for finding in findings if finding["fired"]] == []


# --- Choosing and holding one model ------------------------------------------------


def test_the_costliest_model_per_question_is_paused_first() -> None:
    readings = model_readings(
        [
            journal_row(
                "aqa-1",
                subscription={
                    "openai_codex": {
                        "by_model": {
                            "gpt-6-astra": {
                                "calls": 6,
                                "list_price_equivalent_usd": "0.163382",
                                "tokens": {"input": 1, "output": 1, "thinking": 1},
                            },
                            "gpt-5.6-sol": {
                                "calls": 6,
                                "list_price_equivalent_usd": "0.134654",
                                "tokens": {"input": 1, "output": 1, "thinking": 1},
                            },
                            "gpt-5.6-terra": {
                                "calls": 6,
                                "list_price_equivalent_usd": "0.056624",
                                "tokens": {"input": 1, "output": 1, "thinking": 1},
                            },
                        }
                    }
                },
                outcomes={
                    model: {"N0": 0, "N1": 3, "N2": 0, "N3": 0, "N4": 3, "N5": 0}
                    for model in PLAN_MODELS_BY_VENDOR[VENDOR_OPENAI_CODEX]
                },
            )
        ],
        {"by_model": {}},
    )
    candidates = PLAN_MODELS_BY_VENDOR[VENDOR_OPENAI_CODEX]
    assert select_pause_model(candidates, readings, set()) == "gpt-6-astra"
    assert select_pause_model(candidates, readings, {"gpt-6-astra"}) == "gpt-5.6-sol"


def test_a_model_without_a_measured_cost_is_paused_last() -> None:
    readings = model_readings([], {"by_model": {}})
    readings["gemini-3.7-flash"]["questions_evaluated"] = 4
    readings["gemini-3.7-flash"]["gemini_used_usd"] = Decimal("0.40")
    chosen = select_pause_model(
        PLAN_MODELS_BY_VENDOR[VENDOR_GOOGLE_GEMINI], readings, set()
    )
    assert chosen == "gemini-3.7-flash"


def test_an_expired_resume_time_stops_pausing_the_model() -> None:
    pause = {
        "schema": PAUSE_SCHEMA,
        "paused_models": {
            FABLE_MODEL: {
                "reason": "the captain's daily Claude Fable quota is about 80 "
                "percent used",
                "resume_at_utc": "2026-09-16T23:00:00Z",
            }
        },
    }
    assert FABLE_MODEL in active_pauses(pause, now=NOW)
    assert active_pauses(pause, now=datetime(2026, 9, 17, 0, 0, tzinfo=UTC)) == {}


def test_plan_models_prefer_the_plan_file_and_fall_back_to_the_captains_plan() -> None:
    assert plan_models(None) == dict(PLAN_MODELS_BY_VENDOR)
    override = plan_models(
        {"vendors": {"google_gemini": {"models": ["gemini-4.0-flash"]}}}
    )
    assert override[VENDOR_GOOGLE_GEMINI] == ("gemini-4.0-flash",)
    assert override[VENDOR_OPENAI_CODEX] == PLAN_MODELS_BY_VENDOR[VENDOR_OPENAI_CODEX]


# --- One whole guard cycle -----------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    journal = tmp_path / "streaming-r2"
    journal.mkdir()
    rows = [
        journal_row(
            f"aqa-{index}",
            subscription={
                "anthropic_claude_code": {
                    "by_model": {
                        FABLE_MODEL: {
                            "calls": 6,
                            "list_price_equivalent_usd": "0.281070",
                            "tokens": {"input": 4900, "output": 18, "thinking": 4379},
                        },
                        "claude-opus-5": {
                            "calls": 6,
                            "list_price_equivalent_usd": "0.095016",
                            "tokens": {"input": 4869, "output": 18, "thinking": 2566},
                        },
                        "claude-sonnet-5": {
                            "calls": 6,
                            "list_price_equivalent_usd": "0.010742",
                            "tokens": {"input": 5281, "output": 18, "thinking": 0},
                        },
                    }
                }
            },
            outcomes={
                model: {"N0": 0, "N1": 2, "N2": 1, "N3": 0, "N4": 0, "N5": 3}
                for model in PLAN_MODELS_BY_VENDOR[VENDOR_ANTHROPIC_CLAUDE_CODE]
            },
        )
        for index in range(1, 5)
    ]
    (journal / "cost-journal.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    (journal / "watch-state.json").write_text(
        json.dumps(
            {
                "schema": "abstention-streaming-watch-state-v1",
                "active_vendors": ["anthropic_claude_code", "openai_codex"],
                "paused_vendors": {},
                "polls": 26,
                "evaluated_items": [f"aqa-{index}" for index in range(1, 5)],
                "updated_at_utc": "2026-09-16T11:58:00Z",
            }
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "spent_usd": "60.000000",
                "accepted_question_count": 30,
                "requests": {
                    "a": {
                        "phase": "benchmark_evaluation",
                        "state": "completed",
                        "stage": "evaluation_answer:gemini-3.8-flash",
                        "family_id": "evaluation-item:aqa-1",
                        "actual_cost_usd": "0.300000",
                        "usage": {"promptTokenCount": 100},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    construction = tmp_path / "construction-policy.json"
    construction.write_text(
        json.dumps(
            {
                "dataset_construction_allocation_usd": "500.00",
                "accepted_question_target": 500,
            }
        ),
        encoding="utf-8",
    )
    evaluation = tmp_path / "evaluation-policy.json"
    evaluation.write_text(
        json.dumps(
            {"policy_id": "test-evaluation", "evaluation_ceiling_usd": "200.00"}
        ),
        encoding="utf-8",
    )
    return {
        "journal": journal,
        "guard": tmp_path / "guard",
        "pause": tmp_path / "guard" / "model-pause.json",
        "ledger": ledger,
        "construction": construction,
        "evaluation": evaluation,
        "status": tmp_path / "task.status",
    }


def codex_row(index: int) -> dict:
    """One evaluated question that booked the three Codex models."""
    return journal_row(
        f"codex-{index}",
        subscription={
            "openai_codex": {
                "calls": 18,
                "list_price_equivalent_usd": "0.500000",
                "by_model": {
                    "gpt-6-astra": {
                        "calls": 6,
                        "list_price_equivalent_usd": "0.250000",
                        "tokens": {"input": 5000, "output": 20, "thinking": 3000},
                    },
                    "gpt-5.6-sol": {
                        "calls": 6,
                        "list_price_equivalent_usd": "0.150000",
                        "tokens": {"input": 5000, "output": 20, "thinking": 1800},
                    },
                    "gpt-5.6-terra": {
                        "calls": 6,
                        "list_price_equivalent_usd": "0.100000",
                        "tokens": {"input": 5000, "output": 20, "thinking": 900},
                    },
                },
            }
        },
        outcomes={
            model: {"N0": 0, "N1": 2, "N2": 1, "N3": 0, "N4": 0, "N5": 3}
            for model in PLAN_MODELS_BY_VENDOR[VENDOR_OPENAI_CODEX]
        },
    )


def append_codex_row(workspace: dict[str, Path], index: int) -> None:
    with (workspace["journal"] / "cost-journal.jsonl").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(codex_row(index)) + "\n")


def codex_quota_file(path: Path, *, percent_remaining: int, projected: str) -> Path:
    """Write one recorded quota report with a chosen Codex weekly reading."""
    report = json.loads(RECORDED_QUOTA.read_text(encoding="utf-8"))
    for provider in report["providers"]:
        if provider["provider"] != "codex":
            continue
        for window in provider["windows"]:
            if window["id"] != "weekly":
                continue
            window["percentRemaining"] = percent_remaining
            window["percentUsed"] = 100 - percent_remaining
            window["pace"]["projectedExhaustedAt"] = projected
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def guard_for(workspace: dict[str, Path], quota: Path) -> BenchmarkGuard:
    return BenchmarkGuard(
        journal_dir=workspace["journal"],
        guard_dir=workspace["guard"],
        pause_file=workspace["pause"],
        shared_ledger_file=workspace["ledger"],
        construction_policy_file=workspace["construction"],
        evaluation_policy_file=workspace["evaluation"],
        status_file=workspace["status"],
        recorded_quota_file=quota,
    )


def test_a_quiet_cycle_writes_state_and_log_and_pauses_nothing(
    workspace: dict[str, Path],
) -> None:
    state = guard_for(workspace, RECORDED_QUOTA).cycle(now=NOW)
    assert state["actions"] == []
    assert state["paused_models"] == {}
    assert not workspace["pause"].exists()
    assert (workspace["guard"] / GUARD_STATE_FILENAME).is_file()
    log = (workspace["guard"] / GUARD_LOG_FILENAME).read_text(encoding="utf-8")
    assert json.loads(log.strip())["event"] == "poll"
    assert not workspace["status"].exists()
    assert state["questions_evaluated"] == 4
    assert state["evaluation_phase"]["used_usd"] == "0.300000"


def test_the_session_floor_pauses_one_claude_model_and_reports_it(
    workspace: dict[str, Path],
) -> None:
    state = guard_for(workspace, CLAUDE_SESSION_LOW).cycle(now=NOW)
    assert [action["model"] for action in state["actions"]] == [FABLE_MODEL]
    assert state["actions"][0]["action"] == "pause"
    assert state["actions"][0]["rule"] == "claude_session_window_floor"
    pause = read_pause_file(workspace["pause"])
    entry = pause["paused_models"][FABLE_MODEL]
    assert entry["owner"] == GUARD_OWNER
    assert entry["rule"] == "claude_session_window_floor"
    status = workspace["status"].read_text(encoding="utf-8").splitlines()
    assert status[0].startswith("working: paused claude-fable-5-1 on ")
    assert "percent_remaining=12" in status[0]


def test_a_second_cycle_pauses_the_next_model_while_the_condition_holds(
    workspace: dict[str, Path],
) -> None:
    guard = guard_for(workspace, CLAUDE_SESSION_LOW)
    guard.cycle(now=NOW)
    state = guard.cycle(now=NOW)
    assert [action["model"] for action in state["actions"]] == ["claude-opus-5"]
    assert sorted(state["paused_models"]) == [FABLE_MODEL, "claude-opus-5"]


def test_the_guard_resumes_its_own_pause_after_three_clear_cycles(
    workspace: dict[str, Path],
) -> None:
    guard_for(workspace, CLAUDE_SESSION_LOW).cycle(now=NOW)
    clear = guard_for(workspace, RECORDED_QUOTA)
    for index in range(1, RESUME_CLEAR_CYCLES):
        state = clear.cycle(now=NOW + CYCLE * index)
        assert state["actions"] == []
        assert FABLE_MODEL in state["paused_models"]
        assert state["hysteresis"][0]["hold"] == "clear_cycles"
        assert state["hysteresis"][0]["clear_cycles"] == index
    state = clear.cycle(now=NOW + CYCLE * RESUME_CLEAR_CYCLES)
    assert [action["action"] for action in state["actions"]] == ["resume"]
    assert state["actions"][0]["model"] == FABLE_MODEL
    assert read_pause_file(workspace["pause"])["paused_models"] == {}
    status = workspace["status"].read_text(encoding="utf-8").splitlines()
    assert status[-1].startswith("working: resumed claude-fable-5-1 on ")


def test_a_resumed_model_is_not_paused_again_by_the_same_rule_for_thirty_minutes(
    workspace: dict[str, Path],
) -> None:
    # The Fable rule names one model, so a held model leaves the rule nothing
    # else to pause and the whole rule waits out its hold.
    guard_for(workspace, FABLE_LOW).cycle(now=NOW)
    assert (
        read_pause_file(workspace["pause"])["paused_models"][FABLE_MODEL]["rule"]
        == "fable_weekly_window_floor"
    )
    clear = guard_for(workspace, RECORDED_QUOTA)
    for index in range(1, RESUME_CLEAR_CYCLES + 1):
        state = clear.cycle(now=NOW + CYCLE * index)
    assert [action["action"] for action in state["actions"]] == ["resume"]
    resumed_at = NOW + CYCLE * RESUME_CLEAR_CYCLES

    held = guard_for(workspace, FABLE_LOW).cycle(now=resumed_at + CYCLE)
    assert held["actions"] == []
    assert read_pause_file(workspace["pause"])["paused_models"] == {}
    hold = next(
        record for record in held["hysteresis"] if record["hold"] == "repause_hold"
    )
    assert hold["rule"] == "fable_weekly_window_floor"
    assert hold["models"] == [FABLE_MODEL]
    assert hold["hold_seconds"] == REPAUSE_HOLD_SECONDS

    after = guard_for(workspace, FABLE_LOW).cycle(
        now=resumed_at + timedelta(seconds=REPAUSE_HOLD_SECONDS + 1)
    )
    assert [action["model"] for action in after["actions"]] == [FABLE_MODEL]
    assert after["actions"][0]["rule"] == "fable_weekly_window_floor"


def test_the_guard_never_removes_a_pause_an_operator_wrote(
    workspace: dict[str, Path],
) -> None:
    workspace["guard"].mkdir(parents=True, exist_ok=True)
    workspace["pause"].write_text(
        json.dumps(
            {
                "schema": PAUSE_SCHEMA,
                "paused_models": {
                    FABLE_MODEL: {
                        "reason": "the captain's daily Claude Fable quota is "
                        "about 80 percent used",
                        "resume_at_utc": "2026-09-16T23:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    state = guard_for(workspace, CLAUDE_SESSION_LOW).cycle(now=NOW)
    pause = read_pause_file(workspace["pause"])
    assert pause["paused_models"][FABLE_MODEL]["resume_at_utc"] == (
        "2026-09-16T23:00:00Z"
    )
    assert "owner" not in pause["paused_models"][FABLE_MODEL]
    # The captain already holds Fable, so the rule takes the next Claude model.
    assert [action["model"] for action in state["actions"]] == ["claude-opus-5"]


def test_the_gemini_budget_extrapolation_reaches_the_guard_state(
    workspace: dict[str, Path],
) -> None:
    state = guard_for(workspace, RECORDED_QUOTA).cycle(now=NOW)
    budget = state["gemini_budget"]
    assert budget["budget_usd"] == "200.00"
    assert budget["spend_so_far_usd"] == "0.300000"
    assert budget["questions_evaluated"] == 1
    assert budget["usd_per_question"] == "0.300000"
    assert state["expectation"]["accepted_now"] == 30
    assert budget["extrapolated_total_usd"] is not None
    assert state["vendors"][VENDOR_ANTHROPIC_CLAUDE_CODE]["billing"] == "subscription"
    assert state["vendors"][VENDOR_ANTHROPIC_CLAUDE_CODE]["charged_usd"] == "0.000000"


def test_a_runaway_gemini_cost_pauses_the_costlier_gemini_model(
    workspace: dict[str, Path],
) -> None:
    ledger = json.loads(workspace["ledger"].read_text(encoding="utf-8"))
    ledger["requests"]["a"]["actual_cost_usd"] = "40.000000"
    workspace["ledger"].write_text(json.dumps(ledger), encoding="utf-8")
    state = guard_for(workspace, RECORDED_QUOTA).cycle(now=NOW)
    assert [action["model"] for action in state["actions"]] == ["gemini-3.8-flash"]
    assert state["actions"][0]["rule"] == "gemini_extrapolated_over_budget"
    assert Decimal(state["gemini_budget"]["extrapolated_total_usd"]) > Decimal("200.00")


def test_the_command_line_runs_one_cycle_against_a_recorded_report(
    workspace: dict[str, Path],
) -> None:
    from arctic_qa.benchmark_guard import main

    code = main(
        [
            "--journal-dir",
            str(workspace["journal"]),
            "--guard-dir",
            str(workspace["guard"]),
            "--pause-file",
            str(workspace["pause"]),
            "--shared-ledger-file",
            str(workspace["ledger"]),
            "--construction-policy-file",
            str(workspace["construction"]),
            "--evaluation-policy-file",
            str(workspace["evaluation"]),
            "--recorded-quota-file",
            str(RECORDED_QUOTA),
            "--quota-binary",
            "/does/not/exist/quota-axi",
            "--once",
        ]
    )
    assert code == 0
    state = json.loads(
        (workspace["guard"] / GUARD_STATE_FILENAME).read_text(encoding="utf-8")
    )
    assert state["schema"] == "benchmark-guard-state-v1"
    # This cycle runs on the real clock, and the workspace watch state is
    # fixed, so the guard reports the one thing it must: the evaluator stopped.
    assert state["evaluator"]["running"] is False
    assert state["errors"] == [
        "the streaming evaluator is not running: its watch state has not "
        f"moved for {state['evaluator']['age_seconds']} seconds (the bound is "
        f"900), last at {state['evaluator']['updated_at_utc']}"
    ]
    assert state["quota"]["claude_session"]["percent_remaining"] == 84


def test_two_rules_of_one_vendor_pause_only_one_model(
    workspace: dict[str, Path],
) -> None:
    # A Codex weekly window below 10 percent and an exhaustion projected before
    # the reset both say the same thing, so one model is enough.
    guard = BenchmarkGuard(
        journal_dir=workspace["journal"],
        guard_dir=workspace["guard"],
        pause_file=workspace["pause"],
        shared_ledger_file=workspace["ledger"],
        construction_policy_file=workspace["construction"],
        evaluation_policy_file=workspace["evaluation"],
        recorded_quota_file=CODEX_LOW,
    )
    state = guard.cycle(now=NOW)
    codex = [
        action for action in state["actions"] if action["vendor"] == VENDOR_OPENAI_CODEX
    ]
    assert len(codex) == 1
    assert codex[0]["rule"] == "codex_weekly_window_floor"


def test_the_codex_projection_waits_for_a_full_measurement_window(
    workspace: dict[str, Path],
) -> None:
    # The first cycle has no trailing window to measure, so the rule that reads
    # the attribution cannot fire however the projection looks.
    guard = guard_for(workspace, RECORDED_QUOTA)
    first = guard.cycle(now=NOW)
    assert first["benchmark_is_main_codex_consumer"] is False
    assert first["codex_attribution"]["measured"] is False
    assert str(CODEX_ATTRIBUTION_WINDOW_SECONDS) in first["codex_attribution"]["reason"]
    assert rule(first["rules"], "codex_projected_exhaustion")["fired"] is False
    memory = json.loads(
        (workspace["guard"] / GUARD_MEMORY_FILENAME).read_text(encoding="utf-8")
    )
    assert memory["schema"] == "benchmark-guard-memory-v1"
    assert memory["codex_samples"][0]["codex_percent_remaining"] == 23


def test_alternating_codex_cycles_under_another_sessions_burn_pause_nothing(
    workspace: dict[str, Path], tmp_path: Path
) -> None:
    """The flap of 2026-09-16: the evaluator books Codex calls every other cycle
    while the other sessions on this machine burn the whole weekly window.
    """
    quota = tmp_path / "quota"
    quota.mkdir()
    actions = []
    for index in range(24):
        moment = NOW + CYCLE * index
        if index % 2:
            append_codex_row(workspace, index)
        # Every cycle burns one percent point, whoever booked the calls.
        report = codex_quota_file(
            quota / f"{index}.json",
            percent_remaining=60 - index,
            projected=EXHAUSTION_BEFORE_RESET,
        )
        state = guard_for(workspace, report).cycle(now=moment)
        actions.extend(state["actions"])
    attribution = state["codex_attribution"]
    assert attribution["measured"] is True
    assert Decimal(attribution["window_percent_burn"]) > Decimal("1")
    assert Decimal(attribution["benchmark_usd_burn"]) > Decimal("0")
    assert Decimal(attribution["benchmark_share_of_window"]) < Decimal("0.5")
    assert attribution["drives"] is False
    assert rule(state["rules"], "codex_projected_exhaustion")["fired"] is False
    assert actions == []


def test_a_dominant_codex_share_pauses_one_model_and_holds_it(
    workspace: dict[str, Path], tmp_path: Path
) -> None:
    """The window burns only while this benchmark books Codex calls, so the
    benchmark owns the burn and the projection is urgent.
    """
    quota = tmp_path / "quota"
    quota.mkdir()
    percent = 60
    actions = []
    for index in range(14):
        moment = NOW + CYCLE * index
        if index % 2:
            append_codex_row(workspace, index)
            percent -= 3
        report = codex_quota_file(
            quota / f"{index}.json",
            percent_remaining=percent,
            projected=EXHAUSTION_BEFORE_RESET,
        )
        state = guard_for(workspace, report).cycle(now=moment)
        actions.extend(state["actions"])
    attribution = state["codex_attribution"]
    assert attribution["measured"] is True
    assert Decimal(attribution["benchmark_share_of_window"]) >= Decimal("0.5")
    assert Decimal(attribution["percent_per_usd"]) > Decimal("0")
    assert [action["model"] for action in actions] == ["gpt-6-astra"]
    assert actions[0]["rule"] == "codex_projected_exhaustion"
    numbers = actions[0]["numbers"]
    assert (
        numbers["benchmark_share_of_window"]
        == (attribution["benchmark_share_of_window"])
    )
    assert numbers["benchmark_percent_burn"] == attribution["benchmark_percent_burn"]
    assert numbers["window_percent_burn"] == attribution["window_percent_burn"]

    # One clear cycle does not lift the pause.
    clear = codex_quota_file(
        quota / "clear.json",
        percent_remaining=percent,
        projected=EXHAUSTION_AFTER_RESET,
    )
    after = guard_for(workspace, clear).cycle(now=NOW + CYCLE * 14)
    assert rule(after["rules"], "codex_projected_exhaustion")["fired"] is False
    assert after["actions"] == []
    assert "gpt-6-astra" in after["paused_models"]
    assert after["hysteresis"][0]["clear_cycles"] == 1
    assert after["hysteresis"][0]["clear_cycles_required"] == RESUME_CLEAR_CYCLES


def _set_watch_state(workspace: dict[str, Path], updated_at_utc: str) -> None:
    path = workspace["journal"] / "watch-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["updated_at_utc"] = updated_at_utc
    path.write_text(json.dumps(state), encoding="utf-8")


def test_a_stopped_evaluator_is_an_error_and_one_blocked_line(
    workspace: dict[str, Path],
) -> None:
    """A bound ends the evaluator with exit code 0, so the guard must say it.

    The unit met its item bound at 2026-09-16T19:31:44Z, exited 0, and no
    operator saw it until the next morning.
    """
    guard = guard_for(workspace, RECORDED_QUOTA)
    running = guard.cycle(now=NOW)
    assert running["evaluator"]["running"] is True
    assert running["errors"] == []
    assert not workspace["status"].exists()

    # The watch state stops moving.
    _set_watch_state(workspace, "2026-09-16T10:00:00Z")
    stopped = guard.cycle(now=NOW)
    assert stopped["evaluator"]["running"] is False
    assert stopped["errors"] == [
        "the streaming evaluator is not running: its watch state has not "
        "moved for 7200 seconds (the bound is 900), last at "
        "2026-09-16T10:00:00Z"
    ]
    lines = workspace["status"].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].startswith("blocked: the streaming evaluator")

    # The guard reports the change of state, not the state: a second quiet
    # cycle adds no line.
    guard.cycle(now=NOW)
    assert len(workspace["status"].read_text(encoding="utf-8").splitlines()) == 1

    # And it says so when the evaluator polls again.
    _set_watch_state(workspace, "2026-09-16T11:59:30Z")
    back = guard.cycle(now=NOW)
    assert back["errors"] == []
    lines = workspace["status"].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("working: the streaming evaluator is polling again")


def test_an_absent_watch_state_is_an_error_too(workspace: dict[str, Path]) -> None:
    (workspace["journal"] / "watch-state.json").unlink()
    state = guard_for(workspace, RECORDED_QUOTA).cycle(now=NOW)
    assert state["evaluator"]["present"] is False
    assert state["errors"] == [
        "the streaming evaluator has no watch state in this journal "
        "directory: it never started, or it runs elsewhere"
    ]
    assert workspace["status"].read_text(encoding="utf-8").startswith("blocked: ")
