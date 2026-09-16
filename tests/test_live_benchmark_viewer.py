from __future__ import annotations

import json
import re
import threading
import urllib.request
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import pytest

from arctic_qa.benchmark_guard import FABLE_MODEL, benchmark_report
from arctic_qa.corpus_viewer import CorpusArtifacts, CorpusServer

from test_corpus_viewer import fixture_corpus


VIEWER_PAGE = Path("src/arctic_qa/corpus_viewer.html")

# The captain's Fable pause resumes at 23:00 UTC on 2026-09-16, so every
# assertion about it names its own instant instead of the wall clock.
BEFORE_RESUME = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
AFTER_RESUME = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)

JOURNAL_ROW = {
    "schema": "abstention-eval-cost-row-v1",
    "kind": "item",
    "item_id": "aqa-7f09e4bdf6bac5c50d4c",
    "family_id": "family-b1a8778edee7540f6917",
    "run_id": "abstention-stream-r2-aqa-7f09e4bdf6bac5c50d4c",
    "recorded_at_utc": "2026-09-16T09:15:21Z",
    "complete": True,
    "generation": {
        "campaign_id": "arctic-qa-production-campaign-003",
        "campaign_usd_per_accepted_item": "0.582476",
        "family": {"usd": "0.251680", "paid_calls": 15},
    },
    "evaluation": {
        "google_gemini": {"usd": "0.041200", "charged_calls": 12},
        "subscription": {
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
                }
            }
        },
        "subscription_tokens": {"input": 61663, "output": 432, "thinking": 13917},
        "subscription_list_price_equivalent_usd": "0.741488",
        "recorded_trials": 36,
        "planned_trials": 48,
        "wall_seconds": 102.35,
        "vendors_paused": ["google_gemini"],
    },
    "outcomes_by_model": {
        FABLE_MODEL: {"N0": 0, "N1": 0, "N2": 0, "N3": 3, "N4": 0, "N5": 3},
        "claude-opus-5": {"N0": 1, "N1": 0, "N2": 1, "N3": 2, "N4": 0, "N5": 3},
    },
    "invalid_count": 1,
    "invalid_rate": 0.0277,
    "cumulative": {"items": 1, "evaluation_gemini_usd": "0.041200"},
}

GUARD_STATE = {
    "schema": "benchmark-guard-state-v1",
    "generated_at_utc": "2026-09-16T12:00:00Z",
    "expectation": {
        "accepted_now": 32,
        "questions_still_expected": 272,
        "binding_bound": "allocation",
        "usd_per_accepted_item": "1.802836",
    },
    "evaluation_phase": {
        "used_usd": "0.041200",
        "ceiling_usd": "200.000000",
        "remaining_usd": "199.958800",
    },
    "gemini_budget": {
        "budget_usd": "200.00",
        "spend_so_far_usd": "0.041200",
        "usd_per_question": "0.041200",
        "extrapolated_total_usd": "11.247600",
        "questions_still_expected": 272,
    },
    "vendors": {"google_gemini": {"billing": "api_credits"}},
    "quota": {
        "claude_session": {
            "percent_remaining": 84,
            "resets_at_utc": "2026-09-16T14:10:01Z",
            "pace": "behind",
        },
        "claude_fable": {"percent_remaining": 15, "pace": "behind"},
        "codex_weekly": {
            "percent_remaining": 23,
            "resets_at_utc": "2026-09-20T10:08:04Z",
            "projected_exhausted_at_utc": "2026-09-17T08:21:05Z",
            "pace": "ahead",
        },
    },
    "rules": [
        {
            "rule": "gemini_extrapolated_over_budget",
            "vendor": "google_gemini",
            "fired": False,
            "detail": "the extrapolated Gemini total exceeds USD 200",
            "numbers": {},
        }
    ],
    "paused_models": {
        FABLE_MODEL: {
            "reason": "the captain's daily Claude Fable quota is about 80 percent used",
            "resume_at_utc": "2026-09-16T23:00:00Z",
        }
    },
    "models": [
        {
            "model": "gemini-3.8-flash",
            "vendor": "google_gemini",
            "gemini_used_usd": "0.041200",
            "calls": 12,
            "tokens": {"input": 900, "output": 12, "thinking": 400},
            "questions_evaluated": 1,
        }
    ],
    "errors": [],
}


@pytest.fixture()
def benchmark_files(tmp_path: Path) -> dict[str, Path]:
    journal = tmp_path / "streaming-r2"
    journal.mkdir()
    (journal / "cost-journal.jsonl").write_text(
        json.dumps(JOURNAL_ROW) + "\n", encoding="utf-8"
    )
    (journal / "watch-state.json").write_text(
        json.dumps(
            {
                "schema": "abstention-streaming-watch-state-v1",
                "active_vendors": ["anthropic_claude_code", "openai_codex"],
                "paused_vendors": {"google_gemini": {"reason": "budget precheck"}},
                "polls": 26,
                "evaluated_items": [JOURNAL_ROW["item_id"]],
                "updated_at_utc": "2026-09-16T09:54:14Z",
            }
        ),
        encoding="utf-8",
    )
    guard = tmp_path / "guard-state.json"
    guard.write_text(json.dumps(GUARD_STATE), encoding="utf-8")
    return {"journal": journal, "guard": guard}


def test_report_joins_the_journal_metrics_with_the_guard_budget(
    benchmark_files: dict[str, Path],
) -> None:
    report = benchmark_report(
        journal_dir=benchmark_files["journal"],
        guard_state_file=benchmark_files["guard"],
        now=BEFORE_RESUME,
    )
    assert report["availability"] == "available"
    assert report["questions_evaluated"] == 1
    assert report["trials_recorded"] == 36
    models = {model["model"]: model for model in report["models"]}
    opus = models["claude-opus-5"]
    # N1 0, N2 1, N3 2, N4 0, N5 3: ACC 3/6, precision 3/5, recall 3/4.
    assert opus["counts"] == {"N0": 1, "N1": 0, "N2": 1, "N3": 2, "N4": 0, "N5": 3}
    assert opus["metrics"]["acc"] == pytest.approx(0.5)
    assert opus["metrics"]["precision_abs"] == pytest.approx(0.6)
    assert opus["metrics"]["recall_abs"] == pytest.approx(0.75)
    assert opus["metrics"]["abstention_rate"] == pytest.approx(5 / 6)
    assert opus["invalid_count"] == 1
    assert opus["invalid_rate"] == pytest.approx(1 / 7, abs=1e-4)
    assert opus["state"] == "active"
    assert report["guard"]["gemini_budget"]["extrapolated_total_usd"] == "11.247600"
    assert report["guard"]["quota"]["codex_weekly"]["percent_remaining"] == 23


def test_report_marks_a_paused_model_with_its_reason(
    benchmark_files: dict[str, Path],
) -> None:
    report = benchmark_report(
        journal_dir=benchmark_files["journal"],
        guard_state_file=benchmark_files["guard"],
        now=BEFORE_RESUME,
    )
    fable = next(model for model in report["models"] if model["model"] == FABLE_MODEL)
    assert fable["paused"] is True
    assert fable["state"] == "paused"
    assert "Fable quota" in fable["pause_reason"]
    assert fable["pause_resume_at_utc"] == "2026-09-16T23:00:00Z"


def test_report_carries_every_per_question_cost_column(
    benchmark_files: dict[str, Path],
) -> None:
    report = benchmark_report(
        journal_dir=benchmark_files["journal"],
        guard_state_file=benchmark_files["guard"],
        now=BEFORE_RESUME,
    )
    question = report["questions"][0]
    assert question["item_id"] == JOURNAL_ROW["item_id"]
    assert question["generation_usd"] == "0.251680"
    assert question["evaluation_gemini_usd"] == "0.041200"
    assert question["subscription_tokens"]["thinking"] == 13917
    assert question["subscription_list_price_equivalent_usd"] == "0.741488"
    assert question["wall_seconds"] == 102.35
    assert question["recorded_trials"] == 36


def test_report_shows_the_evaluator_watch_state(
    benchmark_files: dict[str, Path],
) -> None:
    report = benchmark_report(
        journal_dir=benchmark_files["journal"],
        guard_state_file=benchmark_files["guard"],
        now=BEFORE_RESUME,
    )
    evaluator = report["evaluator"]
    assert evaluator["present"] is True
    assert evaluator["polls"] == 26
    assert evaluator["paused_vendors"]["google_gemini"]["reason"] == "budget precheck"
    assert evaluator["evaluated_items"] == 1


def test_report_without_a_guard_state_still_renders_the_journal(
    benchmark_files: dict[str, Path],
) -> None:
    report = benchmark_report(
        journal_dir=benchmark_files["journal"],
        guard_state_file=None,
        now=BEFORE_RESUME,
    )
    assert report["availability"] == "available"
    assert report["guard"]["present"] is False
    assert report["guard"]["gemini_budget"] is None
    assert report["models"]


def test_report_drops_a_pause_whose_resume_time_has_passed(
    benchmark_files: dict[str, Path],
) -> None:
    report = benchmark_report(
        journal_dir=benchmark_files["journal"],
        guard_state_file=benchmark_files["guard"],
        now=AFTER_RESUME,
    )
    fable = next(model for model in report["models"] if model["model"] == FABLE_MODEL)
    assert fable["paused"] is False
    assert fable["state"] == "active"
    assert report["guard"]["paused_models"] == {}


def test_report_without_a_journal_is_explicit() -> None:
    report = benchmark_report(journal_dir=None, guard_state_file=None)
    assert report["availability"] == "not_selected"
    assert report["models"] == []
    assert report["questions"] == []


def test_http_route_serves_the_section_payload(
    tmp_path: Path, benchmark_files: dict[str, Path]
) -> None:
    corpus = tmp_path / "corpus"
    fixture_corpus(corpus)
    artifacts = CorpusArtifacts(
        corpus,
        "test-run",
        tmp_path / "runtime",
        benchmark_journal_dir=benchmark_files["journal"],
        benchmark_guard_state_file=benchmark_files["guard"],
    )
    server = CorpusServer(("127.0.0.1", 0), artifacts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/live-benchmark", timeout=10
        ) as response:
            assert response.status == HTTPStatus.OK
            payload = json.loads(response.read())
        assert payload["availability"] == "available"
        assert {model["model"] for model in payload["models"]} >= {
            FABLE_MODEL,
            "claude-opus-5",
        }
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=10) as response:
            page = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert 'id="live-benchmark"' in page
    assert "Live benchmarking" in page
    assert "/api/live-benchmark" in page


def test_the_page_keeps_every_existing_section_and_binds_the_new_one() -> None:
    page = VIEWER_PAGE.read_text(encoding="utf-8")
    for section in (
        "project-progress",
        "pipeline-method",
        "pipeline-inspector",
        "research-timeline",
        "live-dataset",
        "live-benchmark",
    ):
        assert f'id="{section}"' in page
    for element in (
        "benchmark-state",
        "benchmark-counts",
        "benchmark-budget-counts",
        "benchmark-model-table",
        "benchmark-quota-table",
        "benchmark-rule-table",
        "benchmark-question-table",
        "benchmark-watch-table",
    ):
        assert f'id="{element}"' in page
    assert "loadLiveBenchmark()" in page
    # The section refreshes with the rest of the page and on the refresh button.
    assert re.search(r"setInterval\(.*loadLiveBenchmark\(\).*\}, 15000\)", page)
