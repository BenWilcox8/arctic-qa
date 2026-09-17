from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import arctic_qa.corpus_viewer as corpus_viewer
from arctic_qa.corpus_viewer import CorpusArtifacts, CorpusServer, _safe_json_bytes
from arctic_qa.metadata_prefilter import run_metadata_prefilter
from arctic_qa.model_broker import SharedGeminiBroker, broker_request_key
from arctic_qa.util import sha256_file


# The corpus search run of the paper. These tests skip when it is absent, so a
# clean checkout without the mounted research drive still runs the suite. The
# defaults are the values of the machine that produced the run.
REAL_CORPUS = Path(
    os.environ.get(
        "ARCTIC_REAL_CORPUS_DIR",
        "/mnt/crdata/research-abstention/arctic-qa/corpus-search-r1",
    )
)
REAL_RUN = os.environ.get("ARCTIC_REAL_CORPUS_RUN", "20260911T232247Z")
REAL_ZOTERO = Path(
    os.environ.get(
        "ARCTIC_ZOTERO_RECEIPTS_DIR",
        "/home/ben/.treehouse/firstmate-c40011/6/firstmate/"
        "data/research-workbench/zotero/receipts",
    )
)
POLICY = Path(__file__).parents[1] / "config" / "metadata-prefilter-policy-v1.json"


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def fixture_corpus(root: Path, *, title: str = "Safe title") -> tuple[Path, Path]:
    run = root / "ledgers" / "run-test-run"
    candidates = [
        {
            "candidate_key": "10.1234/test",
            "doi": "10.1234/test",
            "stable_id": "s2:test",
            "title": title,
            "authors": ["A. Researcher"],
            "year": 2026,
            "venue": "Test Journal",
            "type": "JournalArticle",
            "landing_url": "https://example.test/paper",
            "open_access": {"url": "https://example.test/paper.pdf"},
            "rights_access": "metadata_only",
            "reason_code": "metadata_discovered_source_screening_required",
        },
        {
            "candidate_key": "s2:second",
            "doi": None,
            "stable_id": "s2:second",
            "title": "Second record",
            "authors": [],
            "year": None,
            "landing_url": "https://example.test/second",
            "open_access": None,
            "reason_code": "metadata_discovered_source_screening_required",
        },
    ]
    write_json(run / "deduplicated-candidates.json", candidates)
    write_json(
        run / "summary.json",
        {
            "run_id": "test-run",
            "primary_queries_complete": 1,
            "primary_query_total": 1,
            "deduplicated_candidates": 2,
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
        run / "initial-screening-ledger-r1.json",
        [
            {
                "candidate_key": "10.1234/test",
                "decision": "pending",
                "access_status": "original_file_unavailable_doi_http_403",
                "reason_code": "access_failure_not_scientific_exclusion",
            }
        ],
    )
    write_json(
        root / "protocol" / "protocol-v2.json",
        {
            "protocol_id": "test-protocol-v2",
            "frozen_at_utc": "2026-09-11T00:00:00Z",
        },
    )
    return run, root / "progress-v1.json"


def fixture_metadata_run(root: Path) -> Path:
    run = root / "ledgers" / "run-test-run"
    output = root / "metadata" / "run-metadata-r1"
    policy_file = root / "metadata-policy.json"
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["source_protocol_id"] = "test-protocol-v2"
    write_json(policy_file, policy)
    run_metadata_prefilter(
        candidates_file=run / "deduplicated-candidates.json",
        screening_file=run / "initial-screening-ledger-r1.json",
        protocol_file=root / "protocol" / "protocol-v2.json",
        policy_file=policy_file,
        output_dir=output,
        run_id="metadata-r1",
        code_commit="deadbeef",
    )
    return output


def write_source_revision(
    source_run: Path, *, revision: int, eligibility: str = "eligible"
) -> None:
    decision = "include" if eligibility == "eligible" else "exclude"
    counts = {
        "selected": 2,
        "processed": 2,
        "attempted": 1,
        "retrieved": 1,
        "full_text_retrieved": 1,
        "full_text_reviewed": 1,
        "full_text_review_pending": 0,
        "eligible": int(eligibility == "eligible"),
        "excluded": int(eligibility == "excluded"),
        "pending": 0,
        "unattempted": 1,
    }
    overlay = {
        "schema": "source-screening-overlay-v1",
        "run_id": "source-test-r1",
        "policy_id": "source-test-policy-v1",
        "revision": revision,
        "records": [
            {
                "candidate_key": "10.1234/test",
                "position": 1,
                "decision": decision,
                "scientific_eligibility": eligibility,
                "geography_verdict": "core" if eligibility == "eligible" else "mixed",
                "access_state": "retrieved_full_text",
                "reason_code": f"source_supported_{eligibility}",
                "decision_method_version": "codex-native-semantic-source-review-v1",
                "evidence_passages": [
                    {
                        "quote": "<script>source evidence</script>",
                        "locator": {
                            "section": "methods",
                            "page": 2,
                            "start_offset": 10,
                            "end_offset": 42,
                        },
                    }
                ],
                "limitations": ["Correction coverage is unknown."],
            },
            {
                "candidate_key": "s2:second",
                "position": 2,
                "decision": "unreviewed",
                "scientific_eligibility": "unreviewed",
                "geography_verdict": "unresolved",
                "access_state": "unattempted_no_open_access_url",
                "reason_code": "no_open_access_source_url",
                "decision_method_version": None,
                "evidence_passages": [],
                "limitations": ["No source review was completed."],
            },
        ],
        "counts": counts,
    }
    overlay_path = source_run / f"source-screening-overlay-r{revision}.json"
    write_json(overlay_path, overlay)
    receipt = {
        "schema": "source-screening-run-receipt-v1",
        "state": "completed",
        "run_id": "source-test-r1",
        "policy_id": "source-test-policy-v1",
        "producer_code_commit": "deadbeef",
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "selection_size": 2,
        "overlay_revision": revision,
        "overlay_file": overlay_path.name,
        "overlay_sha256": sha256_file(overlay_path),
        "counts": counts,
    }
    receipt_path = source_run / f"run-receipt-r{revision}.json"
    write_json(receipt_path, receipt)
    write_json(
        source_run / "overlay-current.json",
        {
            "revision": revision,
            "file": overlay_path.name,
            "sha256": sha256_file(overlay_path),
        },
    )
    write_json(
        source_run / "run-receipt-current.json",
        {
            "revision": revision,
            "file": receipt_path.name,
            "sha256": sha256_file(receipt_path),
        },
    )


def fixture_source_run(root: Path) -> Path:
    source_run = root / "source" / "run-source-test-r1"
    write_json(
        source_run / "progress.json",
        {
            "schema": "source-screening-progress-v1",
            "state": "completed",
            "run_id": "source-test-r1",
            "policy_id": "source-test-policy-v1",
            "producer_code_commit": "deadbeef",
            "started_at_utc": datetime.now(UTC).isoformat(),
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "counts": {},
            "message": "Fixture source pass complete.",
        },
    )
    write_source_revision(source_run, revision=1)
    return source_run


@pytest.fixture(scope="module")
def real_artifacts(tmp_path_factory: pytest.TempPathFactory) -> CorpusArtifacts:
    if not (
        REAL_CORPUS / "ledgers" / f"run-{REAL_RUN}" / "deduplicated-candidates.json"
    ).is_file():
        pytest.skip("the retained Arctic corpus is not mounted")
    return CorpusArtifacts(
        REAL_CORPUS,
        REAL_RUN,
        tmp_path_factory.mktemp("real-corpus-view"),
        zotero_receipts_dir=REAL_ZOTERO,
    )


def test_real_counts_and_known_screening_states(
    real_artifacts: CorpusArtifacts,
) -> None:
    state = real_artifacts.state()
    assert state["counts"] == {
        "discovered": 84829,
        "selected": 10,
        "retrieved": 5,
        "eligible": 1,
        "excluded": 2,
        "pending": 7,
        "unreviewed": 84819,
    }
    assert state["readiness"]["metadata_discovery"]["verdict"] == "ready"
    assert state["readiness"]["eligible_corpus"]["verdict"] == "not_ready"

    included = real_artifacts.candidates(
        {"q": ["10.1002/2013jd021234"], "page_size": ["10"]}
    )["records"][0]
    excluded = real_artifacts.candidates(
        {"q": ["10.1002/2013jc009342"], "page_size": ["10"]}
    )["records"][0]
    access_pending = real_artifacts.candidates(
        {"q": ["10.1001/jama.1890.02410160013001c"], "page_size": ["10"]}
    )["records"][0]
    geography_pending = real_artifacts.candidates(
        {"q": ["10.1002/2014jg002883"], "page_size": ["10"]}
    )["records"][0]
    assert included["eligibility"] == "eligible"
    assert excluded["eligibility"] == "excluded"
    assert access_pending["pending_reason"] == "access-pending"
    assert geography_pending["pending_reason"] == "geography-pending"
    imported_items = {
        "10.1002/2013eo100006": "TMEMXQHI",
        "10.1002/2013jc009342": "VXZRRIKF",
        "10.1002/2014jg002883": "7YGWAGRY",
        "10.1002/2013jg002587": "BSS7IXN9",
        "10.1002/2013jd021234": "S6XA9S9S",
    }
    for doi, item_key in imported_items.items():
        record = real_artifacts.candidates({"q": [doi], "page_size": ["10"]})[
            "records"
        ][0]
        assert record["zotero_url"] == f"zotero://select/library/items/{item_key}"


def test_real_filters_and_bounded_pagination(real_artifacts: CorpusArtifacts) -> None:
    first = real_artifacts.candidates(
        {"eligibility": ["unreviewed"], "page": ["1"], "page_size": ["10"]}
    )
    second = real_artifacts.candidates(
        {"eligibility": ["unreviewed"], "page": ["2"], "page_size": ["10"]}
    )
    access = real_artifacts.candidates(
        {"pending_reason": ["access-pending"], "page_size": ["100"]}
    )
    assert first["total"] == 84819
    assert len(first["records"]) == 10
    assert first["records"][0]["candidate_key"] != second["records"][0]["candidate_key"]
    assert access["total"] == 5
    with pytest.raises(ValueError, match="page-size"):
        real_artifacts.candidates({"page_size": ["1000"]})


def test_progress_and_screening_updates_refresh_from_fixture(tmp_path: Path) -> None:
    run, progress = fixture_corpus(tmp_path)
    write_json(
        progress,
        {
            "schema": "corpus-progress-v1",
            "state": "paused",
            "stage": "eligibility_screening",
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "message": "Paused after a controlled fixture update.",
        },
    )
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        progress_file=progress,
    )
    assert artifacts.state()["progress"]["state"] == "paused"
    write_json(
        run / "initial-screening-ledger-r2.json",
        [
            {
                "candidate_key": "10.1234/test",
                "decision": "include",
                "access_status": "retrieved_original_pdf",
                "reason_code": "published_primary_journal_article_core_terrestrial_setting",
            }
        ],
    )
    updated = artifacts.candidates({"q": ["10.1234/test"], "page_size": ["10"]})
    assert updated["records"][0]["eligibility"] == "eligible"
    assert artifacts.state()["counts"]["eligible"] == 1


def test_metadata_results_are_separate_and_filterable(tmp_path: Path) -> None:
    fixture_corpus(tmp_path)
    metadata_run = fixture_metadata_run(tmp_path)
    receipt_path = metadata_run / "run-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["completed_at_utc"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    write_json(receipt_path, receipt)
    global_progress = tmp_path / "global-progress.json"
    write_json(
        global_progress,
        {
            "schema": "corpus-progress-v1",
            "state": "completed",
            "stage": "metadata_prefilter",
            "updated_at_utc": receipt["completed_at_utc"],
            "message": "Old completion observation.",
        },
    )
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        metadata_run_dir=metadata_run,
        progress_file=global_progress,
        process_stale_after_seconds=60,
    )
    state = artifacts.state()
    assert state["metadata_processing"]["state"] == "completed"
    assert state["progress"]["state"] == "completed"
    assert state["progress"]["telemetry"] == "observed"
    assert state["readiness"]["metadata_prefilter"]["verdict"] == "ready"
    assert state["metadata_counts"] == {
        "retained_article_type": 1,
        "unresolved_missing_type": 1,
    }
    retained = artifacts.candidates(
        {"metadata_disposition": ["retained_article_type"], "page_size": ["10"]}
    )
    unresolved = artifacts.candidates(
        {"metadata_disposition": ["unresolved_missing_type"], "page_size": ["10"]}
    )
    assert retained["total"] == 1
    assert retained["records"][0]["eligibility"] == "pending"
    assert retained["records"][0]["metadata_reason_code"] == (
        "provider_article_type_for_review"
    )
    assert unresolved["total"] == 1
    assert unresolved["records"][0]["eligibility"] == "unreviewed"


def test_completed_source_overlay_is_visible_and_filterable(tmp_path: Path) -> None:
    fixture_corpus(tmp_path)
    source_run = fixture_source_run(tmp_path)
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        source_run_dir=source_run,
    )
    state = artifacts.state()
    assert state["source_pass"]["state"] == "completed"
    assert state["source_pass"]["overlay_revision"] == 1
    assert state["source_pass_counts"]["eligible"] == 1
    eligible = artifacts.candidates({"eligibility": ["eligible"], "page_size": ["10"]})
    assert eligible["total"] == 1
    record = eligible["records"][0]
    assert record["source_geography"] == "core"
    assert record["source_pass_position"] == 1
    assert record["source_decision_method"] == (
        "codex-native-semantic-source-review-v1"
    )
    assert b"<script>" not in _safe_json_bytes(eligible)


def test_access_and_gemini_state_are_separate_and_filterable(tmp_path: Path) -> None:
    fixture_corpus(tmp_path)
    access_run = tmp_path / "access"
    access_run.mkdir()
    progress = {
        "schema": "article-access-progress-v1",
        "state": "running",
        "run_id": "access-r1",
        "updated_at_utc": datetime.now(UTC).isoformat(),
        "message": "Checking one candidate.",
        "counts": {"target": 2, "checked": 1, "checking": 1, "full_text_ready": 1},
    }
    write_json(access_run / "progress.json", progress)
    (access_run / "access-overlay.ndjson").write_text(
        json.dumps(
            {
                "candidate_key": "10.1234/test",
                "access_state": "full_text_ready",
                "reason_code": "full_text_extracted_and_identity_verified",
                "checked_at_utc": datetime.now(UTC).isoformat(),
                "final_url": "https://example.test/paper.pdf",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gemini_run = tmp_path / "gemini"
    write_json(
        gemini_run / "progress.json",
        {
            "schema": "gemini-eligibility-progress-v1",
            "state": "disabled_no_key",
            "model": "gemini-3.8-flash",
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "counts": {"queued": 1, "completed": 0},
        },
    )
    gemini_overlay = gemini_run / "gemini-overlay.ndjson"
    gemini_overlay.write_text(
        json.dumps(
            {
                "schema": "gemini-eligibility-overlay-row-v1",
                "run_id": "gemini",
                "ready_source_keys_sha256": None,
                "candidate_key": "10.1234/test",
                "gemini_status": "queued",
                "gemini_decision": None,
                "job_key": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    gemini_progress = json.loads((gemini_run / "progress.json").read_text())
    gemini_progress["overlay"] = {
        "file": "gemini-overlay.ndjson",
        "sha256": sha256_file(gemini_overlay),
        "rows": 1,
        "ready_source_keys_sha256": None,
    }
    write_json(gemini_run / "progress.json", gemini_progress)
    connection = tmp_path / "gemini-connection.json"
    write_json(
        connection,
        {
            "schema": "gemini-readonly-connection-check-v1",
            "checked_at": datetime.now(UTC).isoformat(),
            "method": "GET",
            "model": "gemini-3.8-flash",
            "generation_requests": 0,
            "article_uploads": 0,
            "http_status": 200,
            "authentication_verified": True,
            "returned_model": "models/gemini-3.8-flash",
            "input_token_limit": 1048576,
            "output_token_limit": 65536,
        },
    )
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        access_run_dir=access_run,
        gemini_run_dir=gemini_run,
        gemini_connection_file=connection,
    )
    state = artifacts.state()
    assert state["progress"]["stage"] == "article_access_readiness"
    assert state["gemini_screening"]["state"] == "not_started"
    assert state["gemini_screening"]["historical_setup"]["state"] == ("disabled_no_key")
    assert state["gemini_screening"]["counts"]["queued"] == 0
    ready = artifacts.candidates(
        {"access_readiness": ["full_text_ready"], "page_size": ["10"]}
    )
    assert ready["total"] == 1
    assert state["gemini_screening"]["connection"]["state"] == (
        "authenticated_read_only"
    )
    assert ready["records"][0]["gemini_status"] == "queued"
    queued = artifacts.candidates({"gemini_status": ["queued"], "page_size": ["10"]})
    assert queued["total"] == 1

    progress["counts"]["checked"] = 2
    progress["counts"]["checking"] = 0
    progress["updated_at_utc"] = datetime.now(UTC).isoformat()
    write_json(access_run / "progress.json", progress)
    assert artifacts.state()["access_readiness"]["counts"]["checked"] == 2


def test_source_overlay_revision_refreshes_changed_decision(tmp_path: Path) -> None:
    fixture_corpus(tmp_path)
    source_run = fixture_source_run(tmp_path)
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        source_run_dir=source_run,
    )
    assert (
        artifacts.candidates({"q": ["10.1234/test"]})["records"][0]["eligibility"]
        == "eligible"
    )
    write_source_revision(source_run, revision=2, eligibility="excluded")
    changed = artifacts.candidates({"q": ["10.1234/test"]})["records"][0]
    assert changed["eligibility"] == "excluded"
    assert changed["source_geography"] == "mixed"
    assert artifacts.state()["source_pass"]["overlay_revision"] == 2


def test_stale_source_progress_does_not_claim_a_live_process(tmp_path: Path) -> None:
    fixture_corpus(tmp_path)
    source_run = tmp_path / "source" / "run-source-test-r1"
    write_json(
        source_run / "progress.json",
        {
            "schema": "source-screening-progress-v1",
            "state": "running",
            "run_id": "source-test-r1",
            "policy_id": "source-test-policy-v1",
            "producer_code_commit": "deadbeef",
            "started_at_utc": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "updated_at_utc": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "completed_at_utc": None,
            "counts": {"selected": 100, "processed": 3},
            "message": "Old fixture observation.",
        },
    )
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        source_run_dir=source_run,
        process_stale_after_seconds=60,
    )
    source = artifacts.state()["source_pass"]
    assert source["telemetry"] == "stale"
    assert source["state"] is None
    assert source["last_observed_state"] == "running"


def test_stale_access_progress_reports_unknown_stage_state(tmp_path: Path) -> None:
    fixture_corpus(tmp_path)
    access_run = tmp_path / "access"
    write_json(
        access_run / "progress.json",
        {
            "schema": "article-access-progress-v1",
            "state": "running",
            "run_id": "access-r1",
            "policy_id": "access-policy-v1",
            "started_at_utc": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "updated_at_utc": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "completed_at_utc": None,
            "counts": {"target": 2, "checked": 1, "full_text_ready": 1},
            "message": "Old access observation.",
        },
    )
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        access_run_dir=access_run,
        process_stale_after_seconds=60,
    )
    state = artifacts.state()
    assert state["progress"]["telemetry"] == "stale"
    assert state["progress"]["stage"] == "article_access_readiness"
    source = next(row for row in state["stages"] if row["id"] == "source_retrieval")
    assert source["state"] == "unknown"


def test_persistent_cache_does_not_reread_discovery_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = fixture_corpus(tmp_path)
    runtime = tmp_path / "runtime"
    CorpusArtifacts(tmp_path, "test-run", runtime)
    original_read = corpus_viewer._read_json

    def guarded_read(path: Path) -> object:
        if path == run / "deduplicated-candidates.json":
            raise AssertionError("the unchanged discovery ledger was read again")
        return original_read(path)

    monkeypatch.setattr(corpus_viewer, "_read_json", guarded_read)
    restarted = CorpusArtifacts(tmp_path, "test-run", runtime)
    assert restarted.state()["counts"]["discovered"] == 2


def test_absent_stale_and_invalid_progress_are_distinct(tmp_path: Path) -> None:
    _, progress = fixture_corpus(tmp_path)
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        progress_file=progress,
        stale_after_seconds=60,
    )
    assert artifacts.state()["progress"]["telemetry"] == "absent"
    write_json(
        progress,
        {
            "schema": "corpus-progress-v1",
            "state": "running",
            "stage": "source_retrieval",
            "updated_at_utc": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "message": "Old observation.",
        },
    )
    stale = artifacts.state()["progress"]
    assert stale["telemetry"] == "stale"
    assert stale["state"] is None
    assert stale["last_observed_state"] == "running"
    assert artifacts.state()["freshness"]["state"] == "fresh"
    write_json(progress, {"schema": "corpus-progress-v1", "state": "running"})
    invalid = artifacts.state()["progress"]
    assert invalid["telemetry"] == "invalid"
    assert invalid["state"] is None


def test_unavailable_and_stale_artifacts_fail_closed(tmp_path: Path) -> None:
    missing = CorpusArtifacts(tmp_path, "missing-run", tmp_path / "missing-runtime")
    assert missing.state()["availability"] == "unavailable"

    run, _ = fixture_corpus(tmp_path / "stale")
    old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    for path in [
        run / "deduplicated-candidates.json",
        run / "summary.json",
        run / "query-receipts.json",
        run / "initial-screening-ledger-r1.json",
        tmp_path / "stale" / "protocol" / "protocol-v2.json",
    ]:
        os.utime(path, (old, old))
    stale = CorpusArtifacts(
        tmp_path / "stale",
        "test-run",
        tmp_path / "stale-runtime",
        stale_after_seconds=60,
    )
    assert stale.state()["freshness"]["state"] == "stale"
    (run / "summary.json").write_text("{broken", encoding="utf-8")
    corrupt = stale.state()
    assert corrupt["availability"] == "unavailable"
    assert corrupt["readiness"]["metadata_discovery"]["verdict"] == "not_ready"


def test_source_text_is_escaped_in_json(tmp_path: Path) -> None:
    fixture_corpus(tmp_path, title="<script>alert('x')</script>")
    artifacts = CorpusArtifacts(tmp_path, "test-run", tmp_path / "runtime")
    payload = artifacts.candidates({"page_size": ["10"]})
    encoded = _safe_json_bytes(payload)
    assert b"<script>" not in encoded
    assert b"\\u003cscript\\u003e" in encoded


def test_streaming_budget_and_progress_are_bounded_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_corpus(tmp_path)
    ledger = tmp_path / "shared-ledger.json"
    policy = tmp_path / "budget-policy.json"
    policy.write_bytes(
        (
            Path(__file__).parents[1] / "config/streaming-dataset-budget-policy-v1.json"
        ).read_bytes()
    )
    gate = tmp_path / "gate.json"
    write_json(
        gate,
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": True,
            "allowed_phase": "live_test",
            "integrated_code_commit": "fixture",
            "independent_review_verdict": "pass",
            "review_record": "fixture",
        },
    )
    credential = tmp_path / "private" / "gemini.key"
    credential.parent.mkdir(mode=0o700)
    credential.write_text("unused-test-key", encoding="utf-8")
    credential.chmod(0o600)

    class Transport:
        def post(self, model: str, method: str, body: dict) -> dict:
            if method == "countTokens":
                return {"totalTokens": 100}
            return {
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 20,
                    "thoughtsTokenCount": 5,
                    "totalTokenCount": 125,
                }
            }

    broker = SharedGeminiBroker(
        policy_file=policy,
        price_config_file=Path(__file__).parents[1]
        / "config/gemini-eligibility-v1.json",
        execution_gate_file=gate,
        ledger_file=ledger,
        receipts_dir=tmp_path / "receipts",
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
    )
    request = {
        "systemInstruction": {"parts": [{"text": "Return JSON."}]},
        "contents": [{"role": "user", "parts": [{"text": "Paper text."}]}],
        "generationConfig": {
            "candidateCount": 1,
            "responseMimeType": "application/json",
            "responseJsonSchema": {"type": "object"},
            "maxOutputTokens": 1000,
            "thinkingConfig": {"thinkingLevel": "medium"},
        },
        "store": False,
    }
    request_key = broker_request_key(
        model="gemini-3.8-flash",
        run_id="stream-r1",
        stage="eligibility",
        paper_id="<paper>",
        family_id="family-paper",
        source_version_id="source-version-1",
        payload=request,
    )
    broker.execute(
        phase="live_test",
        run_id="stream-r1",
        stage="eligibility",
        paper_id="<paper>",
        family_id="family-paper",
        source_version_id="source-version-1",
        request_key=request_key,
        payload=request,
    )
    status = tmp_path / "shared-ledger.status.json"
    metadata = tmp_path / "dataset-metadata.json"
    write_json(
        metadata,
        {
            "schema_version": "1.0.0",
            "export_id": "export-1",
            "run_id": "stream-r1",
            "files": {"mcq": "exports/mcq.jsonl"},
        },
    )
    progress = tmp_path / "streaming-progress.json"
    write_json(
        progress,
        {
            "schema": "streaming-dataset-progress-v1",
            "state": "paused",
            "run_id": "stream-r1",
            "current_stage": "question_generation",
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "message": "Paused at the offline gate.",
            "counts": {
                "full_text_ready": 4,
                "eligible": 0,
                "rejected": 4,
                "accepted_qa": 0,
            },
            "recent_papers": [
                {
                    "paper_id": "excluded-paper",
                    "title": "Excluded paper",
                    "current_stage": "completed",
                    "final_state": "rejected",
                    "final_reason": "excluded_study_geography",
                },
                *[
                    {
                        "paper_id": f"unresolved-paper-{number}",
                        "title": "<script>unsafe</script>",
                        "current_stage": "completed",
                        "final_state": "unresolved",
                        "final_reason": "evidence_unmatched_or_ambiguous",
                    }
                    for number in range(3)
                ],
            ],
            "broker_status_sha256": sha256_file(status),
            "budget_policy_sha256": sha256_file(policy),
            "dataset_metadata_sha256": sha256_file(metadata),
        },
    )
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        shared_ledger_file=ledger,
        streaming_budget_policy_file=policy,
        streaming_progress_file=progress,
        dataset_metadata_file=metadata,
    )
    state = artifacts.state()
    streaming = state["streaming_pipeline"]
    assert streaming["state"] == "paused"
    assert streaming["broker"]["stages"]["eligibility"]["input_tokens"] == 100
    assert streaming["broker"]["papers"][0]["paper_id"] == "<paper>"
    assert streaming["broker"]["limits"]["accepted_question_target"] == 500
    assert streaming["broker"]["remaining"]["away_generation_submissions"] == 4999
    assert state["gemini_screening"]["counts"] == {
        "queued": 0,
        "completed": 4,
        "eligible": 0,
        "excluded": 1,
        "uncertain": 3,
        "screening_error": 0,
        "too_large_not_ready": 0,
        "ambiguous_charge": 0,
    }
    assert streaming["counts"]["accepted_qa"] == 0
    assert state["project_overview"]["live_metrics"]["accepted_qa"] == 0
    assert b"<script>" not in _safe_json_bytes(streaming)
    assert json.loads(artifacts.dataset_metadata())["export_id"] == "export-1"

    original_sha256_file = corpus_viewer.sha256_file
    ledger_hash_reads = 0

    def advance_custody_during_read(path: Path) -> str:
        nonlocal ledger_hash_reads
        if Path(path) == ledger:
            ledger_hash_reads += 1
            if ledger_hash_reads == 2:
                replacement_ledger = json.loads(ledger.read_text(encoding="utf-8"))
                replacement_ledger["viewer_snapshot_revision"] = 2
                write_json(ledger, replacement_ledger)
                replacement_status = json.loads(status.read_text(encoding="utf-8"))
                replacement_status["ledger_sha256"] = original_sha256_file(ledger)
                write_json(status, replacement_status)
                replacement_progress = json.loads(progress.read_text(encoding="utf-8"))
                replacement_progress["broker_status_sha256"] = original_sha256_file(
                    status
                )
                write_json(progress, replacement_progress)
        return original_sha256_file(path)

    monkeypatch.setattr(corpus_viewer, "sha256_file", advance_custody_during_read)
    retried = artifacts._streaming_state()
    assert retried["telemetry"] == "observed"
    assert retried["state"] == "paused"
    monkeypatch.setattr(corpus_viewer, "sha256_file", original_sha256_file)

    unrestricted_policy = json.loads(policy.read_text(encoding="utf-8"))
    unrestricted_policy["live_test_maximum_papers"] = None
    unrestricted_policy["live_test_maximum_generation_submissions"] = None
    write_json(policy, unrestricted_policy)
    unrestricted_status = json.loads(status.read_text(encoding="utf-8"))
    unrestricted_status["policy_sha256"] = sha256_file(policy)
    unrestricted_status["limits"]["live_test_maximum_papers"] = None
    unrestricted_status["limits"]["live_test_maximum_generation_submissions"] = None
    unrestricted_status["remaining"]["live_test_papers"] = None
    unrestricted_status["remaining"]["live_test_generation_submissions"] = None
    write_json(status, unrestricted_status)
    unrestricted_progress = json.loads(progress.read_text(encoding="utf-8"))
    unrestricted_progress["broker_status_sha256"] = sha256_file(status)
    unrestricted_progress["budget_policy_sha256"] = sha256_file(policy)
    write_json(progress, unrestricted_progress)
    unrestricted = artifacts.state()["streaming_pipeline"]
    assert unrestricted["state"] == "paused"
    assert unrestricted["budget_policy"]["live_test_maximum_papers"] is None
    assert (
        unrestricted["broker"]["remaining"]["live_test_generation_submissions"] is None
    )
    viewer_html = (
        Path(__file__).parents[1] / "src/arctic_qa/corpus_viewer.html"
    ).read_text(encoding="utf-8")
    assert "Cap Unrestricted | Used ${display(used ?? 0)}" in viewer_html

    changed_ledger = json.loads(ledger.read_text(encoding="utf-8"))
    changed_ledger["spent_usd"] = "0"
    write_json(ledger, changed_ledger)
    invalid = artifacts.state()["streaming_pipeline"]
    assert invalid["state"] == "error"
    assert invalid["telemetry"] == "invalid"


def test_polled_routes_answer_from_the_background_snapshot(tmp_path: Path) -> None:
    """A polled route reads the refresher's bytes and never the files again.

    Rebuilding the whole state inside the request thread answered /api/state
    in 6 seconds on 2026-09-17, so a page that polls every 15 seconds queued
    its own requests. The refresher does that work once per cycle, whatever
    the number of clients, and a request only copies out what it left.
    """
    fixture_corpus(tmp_path)
    artifacts = CorpusArtifacts(tmp_path, "test-run", tmp_path / "runtime")
    server = CorpusServer(("127.0.0.1", 0), artifacts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    built: list[str] = []
    artifacts.state = lambda: built.append("state")  # type: ignore[method-assign]
    try:
        for route in ("/api/state", "/api/live-benchmark", "/api/live-papers"):
            with urllib.request.urlopen(f"{base}{route}") as response:
                assert response.status == 200
                assert json.loads(response.read())
                assert int(response.headers["X-Snapshot-Age-Seconds"]) >= 0
        with urllib.request.urlopen(f"{base}/healthz") as response:
            health = json.loads(response.read())
            assert health["status"] == "available"
            assert health["snapshot_age_seconds"] >= 0
            assert health["refresh_error"] is None
        # Four polled answers, and the artifacts were never asked to rebuild.
        assert built == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_failed_refresh_keeps_the_last_good_answer(tmp_path: Path) -> None:
    """A cycle that fails serves the bytes of the last one that worked.

    The refresher owns every polled route, so a refresher that dies or that
    lets one failure through freezes the whole page. The failure is reported
    in a header beside the answer, and the header is safe to send whatever the
    failure said.
    """
    fixture_corpus(tmp_path)
    artifacts = CorpusArtifacts(tmp_path, "test-run", tmp_path / "runtime")
    server = CorpusServer(("127.0.0.1", 0), artifacts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/api/state") as response:
            good = response.read()
            assert response.headers.get("X-Snapshot-Refresh-Error") is None

        def broken() -> dict[str, object]:
            raise RuntimeError("the index is gone\nand the line broke\u2014here")

        artifacts.state = broken  # type: ignore[method-assign]
        server.snapshot.refresh_once()

        with urllib.request.urlopen(f"{base}/api/state") as response:
            assert response.read() == good
            reported = response.headers["X-Snapshot-Refresh-Error"]
        assert "the index is gone" in reported
        assert "\n" not in reported and reported.isascii()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_request_threads_are_a_fixed_pool(tmp_path: Path) -> None:
    """The viewer answers every request on the same few threads.

    `ThreadingHTTPServer` started one thread per request, and glibc gives each
    new thread a 64 MB malloc arena that it never returns. Six hours of
    15-second polling walked the viewer to 3.8 GB and empty responses on
    2026-09-17, so the pool is fixed and the arena count with it.
    """
    fixture_corpus(tmp_path)
    artifacts = CorpusArtifacts(tmp_path, "test-run", tmp_path / "runtime")
    server = CorpusServer(("127.0.0.1", 0), artifacts, workers=3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        before = threading.active_count()
        for _ in range(25):
            with urllib.request.urlopen(f"{base}/healthz") as response:
                response.read()
        assert threading.active_count() == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_full_backlog_is_refused_and_never_dropped(tmp_path: Path) -> None:
    """A request the viewer cannot take is answered, not left unanswered.

    An accepted connection that returns nothing is what the captain saw on
    2026-09-17, and it reads as a dead site. A refusal says which it is.
    """
    fixture_corpus(tmp_path)
    artifacts = CorpusArtifacts(tmp_path, "test-run", tmp_path / "runtime")
    server = CorpusServer(("127.0.0.1", 0), artifacts, workers=1, queue_limit=1)

    class FullBacklog:
        def put_nowait(self, item: object) -> None:
            raise queue.Full

    # A full backlog, held full, so the refusal is what the socket sees.
    server._requests = FullBacklog()  # type: ignore[assignment]
    # serve_forever is not running, so this test accepts the connection
    # itself and hands the server exactly what its accept loop would.
    client = socket.create_connection(("127.0.0.1", server.server_port))
    try:
        accepted, address = server.socket.accept()
        server.process_request(accepted, address)
        answer = client.recv(4096).decode()
        assert "503 Service Unavailable" in answer
        assert "backlog is full" in answer
    finally:
        client.close()
        server.server_close()


def test_http_surface_is_read_only_and_restricted(tmp_path: Path) -> None:
    fixture_corpus(tmp_path, title="<script>alert('x')</script>")
    artifacts = CorpusArtifacts(tmp_path, "test-run", tmp_path / "runtime")
    server = CorpusServer(("127.0.0.1", 0), artifacts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/api/candidates?page_size=10") as response:
            body = response.read()
            assert response.headers["Content-Type"].startswith("application/json")
            assert b"<script>" not in body
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/source.pdf")
        assert missing.value.code == 404
        request = urllib.request.Request(f"{base}/api/state", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as write:
            urllib.request.urlopen(request)
        assert write.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_production_campaign_and_trial_package_are_explicit_and_allowlisted(
    tmp_path: Path,
) -> None:
    fixture_corpus(tmp_path)
    progress = tmp_path / "streaming-progress.json"
    write_json(
        progress,
        {
            "schema": "streaming-dataset-progress-v1",
            "state": "running",
            "run_id": "campaign-1",
            "invocation_run_id": "production-1",
            "current_stage": "generation",
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "counts": {"full_text_ready": 2, "accepted_qa": 0},
            "recent_papers": [],
        },
    )
    plan = tmp_path / "production-plan.json"
    write_json(
        plan,
        {
            "schema": "arctic-qa-full-run-plan-v1",
            "future_scientific_run": {
                "campaign_id": "campaign-1",
                "run_id": "production-1",
                "phase": "away_production",
            },
            "budget_and_ledger": {
                "remaining_to_planning_cap_usd": "50.000000",
                "spent_usd": "11.614496",
                "planning_cumulative_cap_usd": "61.614496",
            },
            "generation_configuration": {"budget_policy": {}},
        },
    )
    package = tmp_path / "trial-package"
    data_names = {
        "benchmark_csv": "benchmark-inputs.csv",
        "benchmark_jsonl": "benchmark-inputs.jsonl",
        "reviewer_csv": "reviewer-items.csv",
        "reviewer_jsonl": "reviewer-items.jsonl",
        "scoring_csv": "scoring-labels.csv",
        "scoring_jsonl": "scoring-labels.jsonl",
    }
    file_records = {}
    for key, name in data_names.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{key}\n", encoding="utf-8")
        file_records[key] = {"path": name, "sha256": sha256_file(path)}
    templates = []
    for role in ("generation", "validation"):
        path = package / "historical-prompt-bundle" / f"{role}-v10-fixture.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {role}\n", encoding="utf-8")
        templates.append(
            {
                "historical": True,
                "kind": "renderer",
                "path": str(path.relative_to(package)),
                "sha256": sha256_file(path),
            }
        )
    write_json(
        package / "manifest.json",
        {
            "schema_version": "arctic-qa-publication-review-v1",
            "benchmark_item_count": 2,
            "reviewer_item_count": 2,
            "files": file_records,
            "historical_prompt_templates": templates,
        },
    )
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        streaming_progress_file=progress,
        production_plan_file=plan,
        publication_package_dir=package,
    )
    state = artifacts.state()
    campaign = state["streaming_pipeline"]["production_campaign"]
    assert campaign == {
        "campaign_id": "campaign-1",
        "invocation_run_id": "production-1",
        "phase": "away_production",
        "state": "running",
        "incremental_ceiling_usd": "50.000000",
        "prior_test_spend_usd": "11.614496",
        "cumulative_ceiling_usd": "61.614496",
    }
    publication = state["publication_package"]
    assert publication["trial_example"] is True
    assert len(publication["files"]) == 9
    assert "path" not in publication["files"][0]

    server = CorpusServer(("127.0.0.1", 0), artifacts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(
            f"{base}/downloads/trial-publication/benchmark_csv"
        ) as response:
            assert response.read() == b"benchmark_csv\n"
            assert response.headers["Content-Type"].startswith("text/csv")
        with urllib.request.urlopen(
            f"{base}/downloads/trial-publication/manifest"
        ) as response:
            assert json.loads(response.read())["benchmark_item_count"] == 2
        with pytest.raises(urllib.error.HTTPError) as arbitrary:
            urllib.request.urlopen(f"{base}/downloads/trial-publication/source_custody")
        assert arbitrary.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_live_dataset_browser_and_downloads_are_joined_and_allowlisted(
    tmp_path: Path,
) -> None:
    fixture_corpus(tmp_path)
    live = tmp_path / "live-dataset"
    snapshot_id = "live-dataset-snapshot-" + "a" * 32
    snapshot = live / "snapshots" / snapshot_id
    snapshot.mkdir(parents=True)
    benchmark_rows = [
        {
            "item_id": "item-1",
            "question_id": "question-1",
            "question": "What changed?",
            "question_context": "The annual mean.",
            "options": [
                {"option_id": "option-1", "position": 1, "text": "It increased"}
            ],
        },
        {
            "item_id": "item-2",
            "question_id": "question-2",
            "question": "Where was it measured?",
            "question_context": "",
            "options": [
                {"option_id": "option-2", "position": 1, "text": "Beaufort Sea"}
            ],
        },
    ]
    reviewer_rows = [
        {
            "item_id": "item-1",
            "paper": {"doi": "10.1/arctic", "title": "Arctic change"},
            "reference_answer": {"text": "It increased"},
            "answer_evidence": {"excerpt": "The annual mean increased."},
            "options": [{"text": "It increased", "is_correct": True}],
            "rationales": {"answer_generation": "The source states the change."},
            "validation": [{"stage": "automated_acceptance", "details": {}}],
        },
        {
            "item_id": "item-2",
            "paper": {"doi": "10.1/beaufort", "title": "Beaufort observations"},
            "reference_answer": {"text": "Beaufort Sea"},
            "answer_evidence": {"excerpt": "Measurements used the Beaufort Sea."},
            "options": [{"text": "Beaufort Sea", "is_correct": True}],
            "rationales": {},
            "validation": [],
        },
    ]
    benchmark = "".join(json.dumps(row) + "\n" for row in benchmark_rows)
    reviewer = "".join(json.dumps(row) + "\n" for row in reviewer_rows)
    benchmark_path = snapshot / "accepted-benchmark.jsonl"
    reviewer_path = snapshot / "accepted-reviewer.jsonl"
    benchmark_path.write_text(benchmark, encoding="utf-8")
    reviewer_path.write_text(reviewer, encoding="utf-8")
    manifest_path = snapshot / "manifest.json"
    write_json(
        manifest_path,
        {
            "schema": "arctic-qa-live-dataset-snapshot-v1",
            "snapshot_id": snapshot_id,
            "updated_at_utc": "2026-09-14T01:00:00Z",
            "item_count": 2,
            "selection": {
                "candidate_schema_version": "2.2.0",
                "generation_prompt_version": "arctic-qa-generation-v16",
                "scope_contract_version": "selected-evidence-literal-scope-v4",
            },
            "preview": {
                "label": "Machine-validated preview",
                "notice": "The reviewer rows retain validation details.",
            },
            "files": {
                "benchmark": {
                    "path": benchmark_path.name,
                    "sha256": sha256_file(benchmark_path),
                    "size_bytes": benchmark_path.stat().st_size,
                },
                "reviewer": {
                    "path": reviewer_path.name,
                    "sha256": sha256_file(reviewer_path),
                    "size_bytes": reviewer_path.stat().st_size,
                },
            },
        },
    )
    write_json(
        live / "current.json",
        {
            "schema": "arctic-qa-live-dataset-pointer-v1",
            "snapshot_id": snapshot_id,
            "manifest": f"snapshots/{snapshot_id}/manifest.json",
            "manifest_sha256": sha256_file(manifest_path),
            "updated_at_utc": "2026-09-14T01:00:00Z",
            "item_count": 2,
        },
    )
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        live_dataset_dir=live,
    )

    live_state = artifacts.state()["live_dataset"]
    assert live_state["item_count"] == 2
    assert live_state["preview"] == {
        "label": "Machine-validated preview",
        "notice": "The reviewer rows retain validation details.",
    }
    page = artifacts.live_dataset_records(
        {"q": ["Beaufort"], "page": ["1"], "page_size": ["10"]}
    )
    assert page["total"] == 1
    assert page["records"][0]["reviewer"]["paper"]["doi"] == "10.1/beaufort"

    server = CorpusServer(("127.0.0.1", 0), artifacts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(
            f"{base}/api/live-dataset?page=1&page_size=10"
        ) as response:
            assert json.loads(response.read())["total"] == 2
        with urllib.request.urlopen(
            f"{base}/downloads/live-dataset/benchmark"
        ) as response:
            assert response.read() == benchmark.encode()
        with pytest.raises(urllib.error.HTTPError) as arbitrary:
            urllib.request.urlopen(f"{base}/downloads/live-dataset/../current.json")
        assert arbitrary.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    reviewer_path.write_text("changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unavailable or invalid"):
        artifacts.live_dataset_records({})


def test_successor_production_plan_binds_active_segment_to_retained_predecessor(
    tmp_path: Path,
) -> None:
    fixture_corpus(tmp_path)
    progress = tmp_path / "streaming-progress.json"
    write_json(
        progress,
        {
            "schema": "streaming-dataset-progress-v1",
            "state": "running",
            "run_id": "campaign-1",
            "invocation_run_id": "production-1-geo-v3",
            "current_stage": "generation",
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "counts": {"full_text_ready": 2, "accepted_qa": 0},
            "recent_papers": [],
        },
    )
    plan = tmp_path / "successor-production-plan.json"
    plan_payload = {
        "schema": "arctic-qa-full-run-plan-v2",
        "future_scientific_run": {
            "campaign_id": "campaign-1",
            "run_id": "production-1-geo-v3",
            "phase": "away_production",
        },
        "run_segment_lineage": {
            "active_segment": {
                "segment_id": "geography-v3",
                "campaign_id": "campaign-1",
                "run_id": "production-1-geo-v3",
                "execution_gate_sha256": "b" * 64,
            },
            "predecessor_segments": [
                {
                    "segment_id": "initial-v2",
                    "campaign_id": "campaign-1",
                    "run_id": "production-1",
                    "run_manifest_sha256": "a" * 64,
                }
            ],
        },
        "methodology": {
            "eligibility_policy_version": "arctic-eligibility-policy-v3",
            "eligibility_prompt_version": "gemini-eligibility-prompt-v5",
            "eligibility_schema_version": "gemini-eligibility-v3",
            "generation_prompt_version": "arctic-qa-generation-v15",
        },
        "budget_and_ledger": {
            "remaining_to_planning_cap_usd": "50.000000",
            "spent_usd": "11.614496",
            "planning_cumulative_cap_usd": "61.614496",
        },
        "generation_configuration": {"budget_policy": {}},
    }
    write_json(plan, plan_payload)
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        streaming_progress_file=progress,
        production_plan_file=plan,
    )
    campaign = artifacts.state()["streaming_pipeline"]["production_campaign"]
    assert campaign["state"] == "running"
    assert campaign["segment_id"] == "geography-v3"
    assert campaign["predecessor_segments"] == [
        {"segment_id": "initial-v2", "run_id": "production-1"}
    ]
    assert campaign["methodology"]["generation_prompt_version"] == (
        "arctic-qa-generation-v15"
    )

    plan_payload["run_segment_lineage"]["active_segment"]["run_id"] = "unrelated-run"
    write_json(plan, plan_payload)
    invalid = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "invalid-runtime",
        streaming_progress_file=progress,
        production_plan_file=plan,
    ).state()["streaming_pipeline"]
    assert invalid["telemetry"] == "invalid"
    assert invalid["state"] == "error"
    assert invalid["message"] == (
        "Streaming-pipeline record error: the production plan lineage is invalid"
    )

    plan_payload["run_segment_lineage"]["active_segment"]["run_id"] = (
        "production-1-geo-v3"
    )
    write_json(plan, plan_payload)
    progress_payload = json.loads(progress.read_text(encoding="utf-8"))
    progress_payload["invocation_run_id"] = "unrelated-run"
    write_json(progress, progress_payload)
    mismatch = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "mismatch-runtime",
        streaming_progress_file=progress,
        production_plan_file=plan,
    ).state()["streaming_pipeline"]
    assert mismatch["telemetry"] == "invalid"
    assert mismatch["state"] == "error"
    assert mismatch["message"] == (
        "Streaming-pipeline record error: the production plan and progress run do not match"
    )


class FakePipelineTraceStore:
    def __init__(self) -> None:
        self.state = "rejected"
        self.list_arguments: dict[str, object] = {}
        self.include_question_context = True

    def list_papers(self, **arguments: object) -> dict[str, object]:
        self.list_arguments = arguments
        return {
            "schema": "pipeline-trace-list-v1",
            "generated_at_utc": "2026-09-13T19:30:00Z",
            "freshness": {"state": "fresh"},
            "items": [
                {
                    "paper_key": "paper-safe-key",
                    "paper_id": "10.1234/test",
                    "source_id": "src-test",
                    "doi": "10.1234/test",
                    "title": "<script>retained title</script>",
                    "run_ids": ["run-test"],
                    "state": self.state,
                    "current_stage": "answer_verification",
                    "attempt_count": 2,
                    "latest_at_utc": "2026-09-13T19:29:00Z",
                    "final_reason": "answer_verifier_scope_not_source_bound",
                    "reason": {
                        "category": "qa_rejection",
                        "summary": "The paper stayed eligible, but the QA candidate failed source-bound scope.",
                    },
                }
            ],
            "next_cursor": "next-safe-cursor",
        }

    def paper_detail(self, paper_key: str) -> dict[str, object]:
        if paper_key != "paper-safe-key":
            raise KeyError("unknown pipeline paper key")
        candidate = {
            "question": "What changed?",
            "answer": {
                "text": "The measured value increased.",
                "evidence_quote": "The value increased during the period.",
                "locator": {"chunk_id": "chunk-readable"},
            },
            "distractors": [{"text": "It decreased.", "type": "contradiction"}],
            "options": [
                {"text": "It increased.", "is_correct": True},
                {"text": "It decreased.", "is_correct": False},
            ],
        }
        if self.include_question_context:
            candidate["question_context"] = (
                "The question compares measurements from two study periods."
            )
        return {
            "schema": "pipeline-trace-paper-v1",
            "identity": {
                "paper_id": "10.1234/test",
                "source_id": "src-test",
                "doi": "10.1234/test",
                "title": "<script>retained title</script>",
            },
            "runs": [{"run_id": "run-test", "state": self.state}],
            "plain_reason": {
                "category": "qa_rejection",
                "summary": "The paper stayed eligible, but the QA candidate failed source-bound scope.",
                "explanation": "This applies to the generated candidate, not the paper.",
                "failed_stage": "automated_acceptance",
                "failed_check": "answer_verifier_scope_not_source_bound",
                "reason_code": "answer_verifier_scope_not_source_bound",
                "reason_codes": ["answer_verifier_scope_not_source_bound"],
                "model_statements": [
                    {
                        "label": "Answer verifier statement",
                        "text": "<script>The scope was not bound.</script>",
                    }
                ],
                "comparisons": [
                    {
                        "label": "Recorded comparison",
                        "proposed_answer": "It increased.",
                        "reconstructed_answer": "It changed.",
                    }
                ],
                "evidence": [
                    {
                        "quote": "The value increased during the period.",
                        "locator": {"chunk_id": "chunk-readable"},
                    }
                ],
            },
            "source": {"context": "<b>full retained context</b>"},
            "findings": [],
            "candidates": [
                {
                    "item_id": "qa-readable",
                    "status": "machine_accepted_unverified",
                    "candidate": candidate,
                }
            ],
            "validation_events": [],
            "rejections": [{"reason_code": "scope_qualifier_missing"}],
            "exports": [],
            "stages": [
                {
                    "stage_key": "stage-safe-key",
                    "run_id": "run-test",
                    "stage": "answer_verification",
                    "role": "answer_verifier",
                    "state": "completed",
                    "attempt": 2,
                    "timing": {},
                    "model": {},
                    "cost": {},
                    "usage": {},
                    "payload_availability": "retained",
                }
            ],
        }

    def stage_payload(self, paper_key: str, stage_key: str) -> dict[str, object]:
        if (paper_key, stage_key) != ("paper-safe-key", "stage-safe-key"):
            raise KeyError("unknown pipeline stage key")
        return {
            "schema": "pipeline-trace-stage-v1",
            "paper_key": paper_key,
            "stage_key": stage_key,
            "request": {"context": "<script>source and prompt</script>"},
            "raw_response": "<img src=x onerror=alert(1)>",
            "parsed_response": {"answer": "retained"},
            "payload_availability": "retained",
        }


def test_pipeline_trace_adapter_is_bounded_and_observes_live_updates(
    tmp_path: Path,
) -> None:
    fixture_corpus(tmp_path)
    store = FakePipelineTraceStore()
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        pipeline_trace_store=store,
    )

    first = artifacts.pipeline_trace_list(
        {
            "q": ["Arctic"],
            "run_id": ["run-test"],
            "state": ["rejected"],
            "stage": ["answer_verification"],
            "limit": ["25"],
            "cursor": ["cursor-safe"],
        }
    )
    assert first["items"][0]["state"] == "rejected"
    assert store.list_arguments == {
        "query": "Arctic",
        "run_id": "run-test",
        "state": "rejected",
        "stage": "answer_verification",
        "limit": 25,
        "cursor": "cursor-safe",
    }
    store.state = "unresolved"
    assert (
        artifacts.pipeline_trace_list({"limit": ["10"]})["items"][0]["state"]
        == "unresolved"
    )

    with pytest.raises(ValueError, match="limit must be"):
        artifacts.pipeline_trace_list({"limit": ["1000"]})
    with pytest.raises(ValueError, match="unsupported characters"):
        artifacts.pipeline_trace_paper({"paper_key": ["bad\nkey"]})


def test_pipeline_trace_http_routes_escape_payloads_and_reject_paths(
    tmp_path: Path,
) -> None:
    fixture_corpus(tmp_path)
    store = FakePipelineTraceStore()
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        pipeline_trace_store=store,
    )
    server = CorpusServer(("127.0.0.1", 0), artifacts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for route in (
            "/api/pipeline-trace?limit=10",
            "/api/pipeline-trace/paper?paper_key=paper-safe-key",
            "/api/pipeline-trace/stage?paper_key=paper-safe-key&stage_key=stage-safe-key",
        ):
            with urllib.request.urlopen(f"{base}{route}") as response:
                body = response.read()
                assert response.headers["Cache-Control"] == "no-store"
                assert b"<script>" not in body
                assert b"<img" not in body
        with urllib.request.urlopen(
            f"{base}/api/pipeline-trace/paper?paper_key=paper-safe-key"
        ) as response:
            retained = json.loads(response.read())
        candidate = retained["candidates"][0]["candidate"]
        assert candidate["question_context"] == (
            "The question compares measurements from two study periods."
        )
        store.include_question_context = False
        with urllib.request.urlopen(
            f"{base}/api/pipeline-trace/paper?paper_key=paper-safe-key"
        ) as response:
            legacy = json.loads(response.read())
        assert "question_context" not in legacy["candidates"][0]["candidate"]
        with pytest.raises(urllib.error.HTTPError) as arbitrary:
            urllib.request.urlopen(f"{base}/api/pipeline-trace/paper/paper-safe-key")
        assert arbitrary.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as unknown:
            urllib.request.urlopen(
                f"{base}/api/pipeline-trace/paper?paper_key=unknown-key"
            )
        assert unknown.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_page_contains_vertical_activity_and_persistent_on_demand_inspector() -> None:
    page = (Path(__file__).parents[1] / "src/arctic_qa/corpus_viewer.html").read_text(
        encoding="utf-8"
    )

    assert 'id="pipeline-method"' in page
    assert 'id="activity-eligibility-gate"' in page
    assert 'id="activity-option-verification"' in page
    assert 'id="activity-machine-accepted"' in page
    assert 'id="pipeline-inspector"' in page
    assert "initialParameters.get('trace_paper')" in page
    assert "sessionStorage.setItem(`trace-open:" in page
    assert "/api/pipeline-trace/stage?paper_key=" in page
    assert "rawJsonDetails('selected stage', payload)" in page
    assert "detailController: null" in page
    assert "state.trace.detailController.abort()" in page
    assert "state.trace.selectedPaper !== paperKey" in page
    assert "Machine acceptance is a retained engineering label" in page
    assert 'id="live-dataset-preview"' in page
    assert "liveDataset.preview?.label" in page
    assert (
        ".telemetry-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));"
        in page
    )
    assert ".telemetry-grid > div { min-width: 0; }" in page


def test_page_contains_readable_trace_views_and_bounded_table_widths() -> None:
    page = (Path(__file__).parents[1] / "src/arctic_qa/corpus_viewer.html").read_text(
        encoding="utf-8"
    )

    assert "renderCandidatesReadable" in page
    assert "renderEligibilityReadable" in page
    assert "renderStagePayloadReadable" in page
    assert "renderPlainReason" in page
    assert "Why this paper stopped" in page
    assert "Failed stage" in page
    assert "Source evidence used for this check" in page
    assert "item.reason?.summary" in page
    assert "Complete raw JSON for ${caption}" in page
    assert "appendLabeledReadableText(card, 'Question', record.question" in page
    assert "Context for benchmark model" in page
    assert "Legacy record: no question context was retained." in page
    assert "The benchmark model receives it with the question." in page
    assert "Reference answer" in page
    assert "Answer choices" in page
    assert "Distractors" in page
    assert ".trace-table { table-layout: fixed; }" in page
    assert ".trace-table .trace-paper-column { width: 40%; }" in page
    assert ".trace-table .trace-time-column" not in page
    assert "Current state since (UTC)" not in page
    assert "const centralStateTime = (value)" in page
    assert "timeZone: 'America/Chicago'" in page
    assert (
        "stateTime.dataset.stateEnteredAtUtc = item.state_entered_at_utc || ''" in page
    )
    assert "stateTime.title = item.state_entered_at_utc" in page
    assert "paper.append(select, stateTime);" in page
    assert "item.state_entered_at_utc" in page
    assert "grid-template-columns: minmax(460px, .95fr) minmax(0, 1.35fr)" in page
    assert "function displayTextBlocks(value)" in page
    assert "function combineContiguousEvidence(values)" in page
    assert (
        "evidenceOffset(previous, 'end') === evidenceOffset(current, 'start')" in page
    )
    assert "['Reconstruction rationale', record.reconstruction_rationale]" in page
    assert "function appendCandidateRejectionReasons(target, wrapper)" in page
    assert "function appendRejectedDistractors(target, records)" in page
    assert "Raw recorded reason and details" in page
    assert (
        "This rejected QA item has no reason recorded for its current payload." in page
    )
    assert "Recorded unresolved exit" in page
    assert "Model response text (display formatting)" in page
    assert "keeps the exact retained response" in page
    assert "The active latitude-first policy is v3." in page
    assert "eligibility prompt v6" in page
    assert "sites at least 66.56° N, land or sea" in page
    assert "Insufficient or inseparable evidence is unresolved" in page

    detail = FakePipelineTraceStore().paper_detail("paper-safe-key")
    candidate = detail["candidates"][0]["candidate"]
    assert candidate["question"] == "What changed?"
    assert candidate["answer"]["text"] == "The measured value increased."
    assert candidate["options"][0]["is_correct"] is True
    assert candidate["answer"]["locator"]["chunk_id"] == "chunk-readable"


def test_readable_evidence_resolver_uses_exact_payload_quotes_by_source_version() -> (
    None
):
    page_path = Path(__file__).parents[1] / "src/arctic_qa/corpus_viewer.html"
    payload = {
        "identity": {
            "paper_id": "paper-a",
            "source_id": "source-a",
            "source_version_id": "version-a",
        },
        "sources": [
            {
                "paper_id": "paper-a",
                "source_id": "source-a",
                "source_version_id": "version-a",
                "scope_evidence": {
                    "resolved_evidence": [
                        {
                            "spans": [
                                {
                                    "span_id": "shared-span",
                                    "quote": "<script>Exact source A quotation.</script>",
                                    "locator": {"section_id": "Results", "page": 3},
                                },
                                {
                                    "span_id": "combined-span",
                                    "quote": "First retained passage. Second retained passage.",
                                    "source_span_ids": [
                                        "component-one",
                                        "component-two",
                                    ],
                                },
                            ]
                        }
                    ]
                },
            },
            {
                "paper_id": "paper-a",
                "source_id": "source-b",
                "source_version_id": "version-b",
                "scope_evidence": {
                    "resolved_evidence": [
                        {
                            "spans": [
                                {
                                    "span_id": "shared-span",
                                    "quote": "Different source B quotation.",
                                }
                            ]
                        }
                    ]
                },
            },
        ],
        "candidates": [
            {
                "candidate": {
                    "answer": {"source_span_id": "shared-span"},
                    "decision_evidence": [{"source_span_id": "combined-span"}],
                }
            }
        ],
    }
    script = r"""
const fs = require('fs');
const page = fs.readFileSync(process.argv[1], 'utf8');
const start = page.indexOf('    function evidenceQuote(value)');
const end = page.indexOf('    function appendEvidenceLocator', start);
function hasTraceValue(value) { return value !== null && value !== undefined && value !== ''; }
eval(page.slice(start, end));
const payload = JSON.parse(process.argv[2]);
const resolver = createEvidenceResolver(payload);
const answer = resolver.resolve({ source_span_id: 'shared-span', source_id: 'source-a', source_version_id: 'version-a' });
const otherVersion = resolver.resolve({ source_span_id: 'shared-span', source_id: 'source-b', source_version_id: 'version-b' });
const combined = resolver.resolve({ source_span_id: 'combined-span', source_id: 'source-a', source_version_id: 'version-a' });
const missing = resolver.resolve({ source_span_id: 'missing-span', source_id: 'source-a', source_version_id: 'version-a' });
process.stdout.write(JSON.stringify({
  answer: answer.value.quote,
  otherVersion: otherVersion.value.quote,
  combined: combined.value.quote,
  missing: missing.found,
  preservedId: payload.candidates[0].candidate.answer.source_span_id
}));
"""
    result = subprocess.run(
        ["node", "-e", script, str(page_path), json.dumps(payload)],
        check=True,
        capture_output=True,
        text=True,
    )
    resolved = json.loads(result.stdout)
    assert resolved == {
        "answer": "<script>Exact source A quotation.</script>",
        "otherVersion": "Different source B quotation.",
        "combined": "First retained passage. Second retained passage.",
        "missing": False,
        "preservedId": "shared-span",
    }
    page = page_path.read_text(encoding="utf-8")
    assert "node.textContent = value" in page
    resolver = page[
        page.index("function createEvidenceResolver") : page.index(
            "function appendEvidenceLocator"
        )
    ]
    assert "fetch(" not in resolver
    assert "Quotation not recorded or available for this source span." in page
