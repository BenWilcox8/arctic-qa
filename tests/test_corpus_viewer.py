from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import arctic_qa.corpus_viewer as corpus_viewer
from arctic_qa.corpus_viewer import CorpusArtifacts, CorpusServer, _safe_json_bytes
from arctic_qa.metadata_prefilter import run_metadata_prefilter


REAL_CORPUS = Path("/mnt/crdata/research-abstention/arctic-qa/corpus-search-r1")
REAL_RUN = "20260911T232247Z"
REAL_ZOTERO = Path(
    "/home/ben/.treehouse/firstmate-c40011/6/firstmate/"
    "data/research-workbench/zotero/receipts"
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
    artifacts = CorpusArtifacts(
        tmp_path,
        "test-run",
        tmp_path / "runtime",
        metadata_run_dir=metadata_run,
        process_stale_after_seconds=60,
    )
    state = artifacts.state()
    assert state["metadata_processing"]["state"] == "completed"
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
