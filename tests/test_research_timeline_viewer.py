from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arctic_qa.cli import parser
from arctic_qa.corpus_viewer import CorpusArtifacts, _safe_json_bytes


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def fixture_corpus(root: Path) -> None:
    run = root / "ledgers" / "run-test-run"
    write_json(
        run / "deduplicated-candidates.json",
        [
            {
                "candidate_key": "10.1234/test",
                "doi": "10.1234/test",
                "title": "Test paper",
                "reason_code": "metadata_discovered_source_screening_required",
            }
        ],
    )
    write_json(
        run / "summary.json",
        {
            "run_id": "test-run",
            "primary_queries_complete": 1,
            "primary_query_total": 1,
            "deduplicated_candidates": 1,
        },
    )
    write_json(
        run / "query-receipts.json",
        [
            {
                "service": "semantic_scholar",
                "query_id": "q1",
                "terminal": True,
                "complete": True,
                "completion_reason": "no_continuation_token",
            }
        ],
    )
    write_json(
        root / "protocol" / "protocol-v2.json",
        {"protocol_id": "test-protocol-v2", "frozen_at_utc": "2026-09-11T00:00:00Z"},
    )


def timeline_payload(*, updated_at: datetime | None = None) -> dict:
    now = (updated_at or datetime.now(UTC)).replace(microsecond=0)
    start = now - timedelta(hours=1)
    return {
        "schema": "research-fleet-timeline-v1",
        "updated_at_utc": now.isoformat(),
        "window_start_utc": start.isoformat(),
        "window_end_utc": now.isoformat(),
        "summary": "The pipeline is not complete.",
        "coverage_note": "Only bounded Research fleet records are included.",
        "source_types": ["Git commits", "review records"],
        "entries": [
            {
                "id": "builder-code-change",
                "at_utc": (start + timedelta(minutes=10)).isoformat(),
                "agent": "builder",
                "agent_label": "Builder",
                "stage": "answer-validation",
                "stage_label": "Answer validation",
                "kind": "code_change",
                "status": "completed",
                "artifact_state": "committed",
                "title": "Bound answers to source spans",
                "detail": "A checked commit added exact source-slice validation.",
                "version": "abc1234",
                "evidence_ref": "review record <private>",
                "evidence_url": "https://example.test/commit/abc1234",
            },
            {
                "id": "scope-contract-review",
                "at_utc": (start + timedelta(minutes=40)).isoformat(),
                "agent": "reviewer",
                "agent_label": "Independent reviewer",
                "stage": "scope-contract",
                "stage_label": "Scope contract",
                "kind": "review",
                "status": "in_progress",
                "artifact_state": "in_progress",
                "title": "Found an empty-scope gap",
                "detail": "The builder is testing a fail-closed repair.",
                "version": "uncommitted",
                "evidence_ref": "scope review",
                "evidence_url": None,
            },
        ],
    }


def artifacts(tmp_path: Path, timeline_file: Path | None = None) -> CorpusArtifacts:
    corpus = tmp_path / "corpus"
    fixture_corpus(corpus)
    return CorpusArtifacts(
        corpus,
        "test-run",
        tmp_path / "runtime",
        research_timeline_file=timeline_file,
    )


def test_timeline_is_exposed_in_order_without_private_path(tmp_path: Path) -> None:
    timeline_file = tmp_path / "private" / "timeline.json"
    write_json(timeline_file, timeline_payload())

    state = artifacts(tmp_path, timeline_file).state()
    timeline = state["research_timeline"]

    assert timeline["telemetry"] == "observed"
    assert [entry["id"] for entry in timeline["entries"]] == [
        "builder-code-change",
        "scope-contract-review",
    ]
    assert timeline["entries"][1]["artifact_state"] == "in_progress"
    body = _safe_json_bytes(state).decode()
    assert str(timeline_file) not in body
    assert "<private>" not in body
    assert r"\u003cprivate\u003e" in body


def test_timeline_refreshes_after_controlled_file_update(tmp_path: Path) -> None:
    timeline_file = tmp_path / "private" / "timeline.json"
    payload = timeline_payload()
    write_json(timeline_file, payload)
    configured = artifacts(tmp_path, timeline_file)
    assert (
        configured.state()["research_timeline"]["summary"]
        == "The pipeline is not complete."
    )

    payload["summary"] = "The checked timeline record changed."
    write_json(timeline_file, payload)

    assert (
        configured.state()["research_timeline"]["summary"]
        == "The checked timeline record changed."
    )


def test_absent_stale_and_invalid_timeline_states_are_distinct(tmp_path: Path) -> None:
    assert (
        artifacts(tmp_path / "absent").state()["research_timeline"]["telemetry"]
        == "absent"
    )

    stale_file = tmp_path / "stale.json"
    write_json(
        stale_file, timeline_payload(updated_at=datetime.now(UTC) - timedelta(days=2))
    )
    assert (
        artifacts(tmp_path / "stale", stale_file).state()["research_timeline"][
            "telemetry"
        ]
        == "stale"
    )

    invalid_file = tmp_path / "invalid.json"
    payload = timeline_payload()
    payload["entries"][0]["evidence_url"] = "file:///private/review.md"
    write_json(invalid_file, payload)
    invalid = artifacts(tmp_path / "invalid", invalid_file).state()["research_timeline"]
    assert invalid["telemetry"] == "invalid"
    assert invalid["entries"] == []


def test_timeline_rejects_reverse_chronology(tmp_path: Path) -> None:
    timeline_file = tmp_path / "reverse.json"
    payload = timeline_payload()
    payload["entries"].reverse()
    write_json(timeline_file, payload)

    timeline = artifacts(tmp_path, timeline_file).state()["research_timeline"]

    assert timeline["telemetry"] == "invalid"
    assert timeline["entries"] == []


def test_page_has_timeline_filters_and_safe_text_rendering() -> None:
    page = (
        Path(__file__).parents[1] / "src" / "arctic_qa" / "corpus_viewer.html"
    ).read_text(encoding="utf-8")

    assert 'id="research-timeline"' in page
    assert 'id="timeline-agent"' in page
    assert 'id="timeline-stage"' in page
    assert "renderResearchTimeline(data)" in page
    assert "entry.detail" in page
    assert "textContent" in page


def test_main_cli_accepts_explicit_research_timeline_path() -> None:
    args = parser().parse_args(
        [
            "--data-root",
            "/tmp/test-data",
            "corpus-view",
            "--corpus-root",
            "/tmp/corpus",
            "--run-id",
            "test-run",
            "--runtime-dir",
            "/tmp/runtime",
            "--research-timeline-file",
            "/private/status/research-timeline.json",
        ]
    )

    assert args.research_timeline_file == Path("/private/status/research-timeline.json")
