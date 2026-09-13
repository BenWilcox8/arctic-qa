from __future__ import annotations

import json
import threading
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from arctic_qa.cli import parser
from arctic_qa.corpus_viewer import (
    CorpusArtifacts,
    CorpusServer,
    _safe_json_bytes,
)


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
                "stable_id": "s2:test",
                "title": "Test paper",
                "authors": ["A. Researcher"],
                "year": 2026,
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


def overview_payload(*, explanation: str = "The design record is complete.") -> dict:
    scientific = [
        ("research-design", "Research design", "completed"),
        ("paper-discovery", "Paper discovery", "completed"),
        ("working-full-text", "Working full text", "completed"),
        ("scientific-eligibility", "Scientific eligibility", "in_progress"),
        ("qa-answer", "QA and answer", "not_finished"),
        ("answer-verification", "Answer verification", "not_finished"),
        (
            "distractor-verification",
            "Distractors and verification",
            "not_finished",
        ),
        ("usable-dataset", "Usable dataset", "not_finished"),
        ("model-evaluation", "Model evaluation", "not_finished"),
    ]
    engineering = [
        ("api-accounting", "API and accounting", "completed"),
        ("batch-progression", "Batch progression", "completed"),
        ("export-counts", "Export and counts", "completed"),
        ("source-span-evidence", "Source-span evidence", "in_progress"),
        ("live-end-to-end-proof", "Live end-to-end proof", "not_finished"),
    ]

    def stages(rows: list[tuple[str, str, str]]) -> list[dict[str, str]]:
        return [
            {
                "id": stage_id,
                "label": stage_label,
                "status": status,
                "explanation": (
                    explanation
                    if stage_id == "research-design"
                    else "Fixture stage explanation."
                ),
                "next_action": "Complete the next bounded action.",
            }
            for stage_id, stage_label, status in rows
        ]

    return {
        "schema": "project-progress-overview-v1",
        "updated_at_utc": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "summary": "The scientific dataset is not complete.",
        "distinction": (
            "Code completion is separate from scientific-stage completion. "
            "Green engineering can support unfinished scientific work."
        ),
        "scientific_stages": stages(scientific),
        "engineering_stages": stages(engineering),
        "notes": ["Evaluation is future work outside dataset construction."],
    }


def artifacts(
    tmp_path: Path,
    overview_file: Path | None = None,
    streaming_progress_file: Path | None = None,
) -> CorpusArtifacts:
    corpus = tmp_path / "corpus"
    fixture_corpus(corpus)
    return CorpusArtifacts(
        corpus,
        "test-run",
        tmp_path / "runtime",
        project_overview_file=overview_file,
        streaming_progress_file=streaming_progress_file,
    )


def test_configured_project_overview_is_exposed_without_private_path(
    tmp_path: Path,
) -> None:
    overview_file = tmp_path / "private" / "project-overview.json"
    write_json(overview_file, overview_payload())

    state = artifacts(tmp_path, overview_file).state()

    overview = state["project_overview"]
    assert overview["telemetry"] == "observed"
    assert overview["schema"] == "project-progress-overview-v1"
    assert overview["scientific_stages"][0]["status"] == "completed"
    assert overview["engineering_stages"][3]["status"] == "in_progress"
    assert str(overview_file) not in _safe_json_bytes(state).decode()


def test_absent_or_malformed_overview_does_not_invent_stage_states(
    tmp_path: Path,
) -> None:
    absent = artifacts(tmp_path / "absent").state()["project_overview"]
    assert absent["telemetry"] == "absent"
    assert absent["scientific_stages"] == []
    assert absent["engineering_stages"] == []

    invalid_file = tmp_path / "invalid" / "overview.json"
    write_json(invalid_file, {"schema": "unexpected", "scientific_stages": []})
    invalid = artifacts(tmp_path / "malformed", invalid_file).state()[
        "project_overview"
    ]
    assert invalid["telemetry"] == "invalid"
    assert invalid["scientific_stages"] == []
    assert invalid["engineering_stages"] == []


def test_untrusted_overview_text_is_json_escaped(tmp_path: Path) -> None:
    overview_file = tmp_path / "private" / "project-overview.json"
    write_json(overview_file, overview_payload(explanation="<script>alert(1)</script>"))

    body = _safe_json_bytes(artifacts(tmp_path, overview_file).state()).decode()

    assert "<script>" not in body
    assert r"\u003cscript\u003ealert(1)\u003c/script\u003e" in body


def test_existing_page_contains_progress_anchor_and_inline_svg_targets() -> None:
    page = (
        Path(__file__).parents[1] / "src" / "arctic_qa" / "corpus_viewer.html"
    ).read_text(encoding="utf-8")

    assert 'id="project-progress"' in page
    assert 'id="scientific-progress-diagram"' in page
    assert 'id="engineering-progress-diagram"' in page
    assert "<svg" in page
    assert "Completed" in page
    assert "In progress" in page
    assert "Not finished" in page
    assert "textContent" in page
    assert "current incremental invocation" in page


def test_project_overview_uses_streaming_counts_as_separate_live_metrics(
    tmp_path: Path,
) -> None:
    overview_file = tmp_path / "private" / "project-overview.json"
    progress_file = tmp_path / "private" / "streaming-progress.json"
    write_json(overview_file, overview_payload())
    write_json(
        progress_file,
        {
            "schema": "streaming-dataset-progress-v1",
            "state": "paused",
            "run_id": "stream-r1",
            "current_stage": "scientific_eligibility",
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "message": "Fixture progress.",
            "counts": {
                "eligible": 0,
                "excluded": 1,
                "unresolved": 3,
                "accepted_qa": 0,
            },
            "recent_papers": [],
        },
    )

    overview = artifacts(tmp_path, overview_file, progress_file).state()[
        "project_overview"
    ]

    assert overview["live_metrics"] == {
        "source": "viewer_validated_pipeline_records",
        "telemetry": "observed",
        "scientific_count_scope": "current_incremental_invocation",
        "accepted_qa_scope": "shared_ledger_cumulative",
        "spent_usd": None,
        "reserved_usd": None,
        "ambiguous_reserved_usd": None,
        "generation_submissions": None,
        "inflight": None,
        "eligible": 0,
        "excluded": 1,
        "unresolved": 3,
        "accepted_qa": None,
    }


def test_http_page_and_state_keep_overview_text_in_safe_boundaries(
    tmp_path: Path,
) -> None:
    overview_file = tmp_path / "private" / "project-overview.json"
    write_json(overview_file, overview_payload(explanation="<script>alert(1)</script>"))
    configured = artifacts(tmp_path, overview_file)
    server = CorpusServer(("127.0.0.1", 0), configured)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/") as response:
            page = response.read()
            assert b'id="project-progress"' in page
            assert response.headers["Content-Security-Policy"]
        with urllib.request.urlopen(f"{base}/api/state") as response:
            body = response.read()
            assert b"<script>" not in body
            assert b"\\u003cscript\\u003ealert(1)" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_main_cli_accepts_explicit_project_overview_path() -> None:
    args = parser().parse_args(
        [
            "corpus-view",
            "--corpus-root",
            "/mnt/example",
            "--run-id",
            "test-run",
            "--runtime-dir",
            "/private/runtime",
            "--project-overview-file",
            "/private/status/project-overview.json",
            "--pipeline-namespace",
            "/private/arctic-qa",
            "--pipeline-db-file",
            "/private/arctic-qa/state.sqlite3",
            "--pipeline-receipts-dir",
            "/private/arctic-qa/model-receipts",
            "--pipeline-eligibility-root",
            "/private/arctic-qa/eligibility-a",
            "--pipeline-eligibility-root",
            "/private/arctic-qa/eligibility-b",
        ]
    )

    assert args.project_overview_file == Path("/private/status/project-overview.json")
    assert args.pipeline_namespace == Path("/private/arctic-qa")
    assert args.pipeline_db_file == Path("/private/arctic-qa/state.sqlite3")
    assert args.pipeline_receipts_dir == Path("/private/arctic-qa/model-receipts")
    assert args.pipeline_eligibility_root == [
        Path("/private/arctic-qa/eligibility-a"),
        Path("/private/arctic-qa/eligibility-b"),
    ]
