from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctic_qa.metadata_prefilter import run_metadata_prefilter


POLICY = Path(__file__).parents[1] / "config" / "metadata-prefilter-policy-v1.json"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def candidate(
    key: str,
    title: str,
    item_type: str | None,
    *,
    origins: list[str] | None = None,
    abstract_present: bool = True,
) -> dict[str, object]:
    return {
        "candidate_key": key,
        "doi": key if key.startswith("10.") else None,
        "stable_id": f"s2:{key}",
        "title": title,
        "authors": ["A. Researcher"],
        "year": 2025,
        "venue": "Fixture Journal",
        "type": item_type,
        "landing_url": f"https://example.test/{key}",
        "open_access": None,
        "origins": origins or ["semantic_scholar:q01"],
        "abstract_present": abstract_present,
        "version_relation": "unknown",
        "retraction_correction": "unknown_not_checked",
    }


def inputs(root: Path) -> tuple[Path, Path, Path, Path]:
    candidates = [
        candidate(
            "10.1/seed",
            "Seed outside the title term list",
            "JournalArticle",
            origins=["collaborator_seed:Example", "semantic_scholar:q01"],
        ),
        candidate("10.1/review", "Arctic synthesis", "Review"),
        candidate("10.1/mixed", "Mixed metadata", "JournalArticle;Review"),
        candidate("s2:missing", "Arctic field report", None, abstract_present=False),
        candidate("10.1/article", "Temperate article title", "journal-article"),
        candidate("10.1/conference", "Conference paper", "Conference"),
    ]
    screening = [
        {
            "candidate_key": "10.1/seed",
            "decision": "include",
            "access_status": "retrieved_original_pdf",
            "reason_code": "published_source_evidence",
            "study_setting_evidence": {"locator": "page 2"},
        },
        {
            "candidate_key": "10.1/review",
            "decision": "exclude",
            "access_status": "retrieved_original_pdf",
            "reason_code": "review_not_primary_source",
        },
        {
            "candidate_key": "10.1/mixed",
            "decision": "pending",
            "access_status": "original_file_unavailable",
            "reason_code": "access_failure_not_scientific_exclusion",
        },
    ]
    candidates_file = root / "candidates.json"
    screening_file = root / "screening.json"
    protocol_file = root / "protocol.json"
    policy_file = root / "policy.json"
    write_json(candidates_file, candidates)
    write_json(screening_file, screening)
    write_json(
        protocol_file,
        {"protocol_id": "arctic-corpus-search-r1-protocol-v2"},
    )
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["batch_size"] = 2
    write_json(policy_file, policy)
    return candidates_file, screening_file, protocol_file, policy_file


def run(root: Path, *, max_batches: int | None = None) -> dict[str, object]:
    candidates_file, screening_file, protocol_file, policy_file = inputs(root)
    return run_metadata_prefilter(
        candidates_file=candidates_file,
        screening_file=screening_file,
        protocol_file=protocol_file,
        policy_file=policy_file,
        output_dir=root / "output" / "run-fixture-r1",
        run_id="fixture-r1",
        code_commit="deadbeef",
        viewer_progress_file=root / "viewer-progress.json",
        max_batches=max_batches,
    )


def read_records(root: Path) -> dict[str, dict[str, object]]:
    path = root / "output" / "run-fixture-r1" / "metadata-dispositions.ndjson"
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return {record["candidate_key"]: record for record in records}


def test_interruption_resume_reconciliation_and_idempotency(tmp_path: Path) -> None:
    paused = run(tmp_path, max_batches=1)
    assert paused["state"] == "paused"
    assert paused["processed"] == 2
    viewer = json.loads((tmp_path / "viewer-progress.json").read_text())
    assert viewer["state"] == "paused"
    assert viewer["processed"] == 2

    completed = run(tmp_path)
    assert completed["state"] == "completed"
    assert completed["processed"] == 6
    assert completed["unique_candidate_keys"] == 6
    assert sum(completed["disposition_counts"].values()) == 6
    assert sum(completed["queue_counts"].values()) == 6
    queue_path = tmp_path / "output" / "run-fixture-r1" / "review-queue.ndjson"
    queue_records = [json.loads(line) for line in queue_path.read_text().splitlines()]
    assert [record["queue"]["rank"] for record in queue_records] == sorted(
        record["queue"]["rank"] for record in queue_records
    )
    assert [entry["event"] for entry in completed["resume_history"]] == [
        "started",
        "controlled_pause",
        "resumed",
        "completed",
    ]
    receipt = tmp_path / "output" / "run-fixture-r1" / "run-receipt.json"
    before = receipt.read_bytes()
    progress_path = tmp_path / "output" / "run-fixture-r1" / "progress.json"
    progress = json.loads(progress_path.read_text())
    progress["state"] = "running"
    progress["completed_at_utc"] = None
    write_json(progress_path, progress)
    viewer["state"] = "running"
    viewer["completed_at_utc"] = None
    write_json(tmp_path / "viewer-progress.json", viewer)
    replay = run(tmp_path)
    assert replay["idempotent_replay"] is True
    assert receipt.read_bytes() == before
    assert json.loads(progress_path.read_text())["state"] == "completed"
    assert json.loads((tmp_path / "viewer-progress.json").read_text())["state"] == (
        "completed"
    )


def test_changed_input_is_refused_on_resume(tmp_path: Path) -> None:
    run(tmp_path, max_batches=1)
    candidates_file = tmp_path / "candidates.json"
    candidates = json.loads(candidates_file.read_text())
    candidates.append(candidate("10.1/changed", "Changed input", "JournalArticle"))
    write_json(candidates_file, candidates)
    with pytest.raises(ValueError, match="inputs changed"):
        run_metadata_prefilter(
            candidates_file=candidates_file,
            screening_file=tmp_path / "screening.json",
            protocol_file=tmp_path / "protocol.json",
            policy_file=tmp_path / "policy.json",
            output_dir=tmp_path / "output" / "run-fixture-r1",
            run_id="fixture-r1",
            code_commit="deadbeef",
        )


def test_conservative_dispositions_and_source_status_are_separate(
    tmp_path: Path,
) -> None:
    run(tmp_path)
    records = read_records(tmp_path)
    assert records["10.1/seed"]["metadata_disposition"] == "priority_seed"
    assert records["10.1/seed"]["provisional"] is True
    assert records["10.1/seed"]["scientific_eligibility_effect"] == "none"
    assert records["10.1/seed"]["source_screening_snapshot"] == {
        "eligibility": "eligible",
        "decision": "include",
        "reason_code": "published_source_evidence",
        "access_status": "retrieved_original_pdf",
        "evidence_locator": "page 2",
    }
    assert records["10.1/review"]["metadata_disposition"] == "flagged_nonresearch_type"
    assert records["10.1/review"]["source_screening_snapshot"]["eligibility"] == (
        "excluded"
    )
    assert records["10.1/mixed"]["metadata_disposition"] == "unresolved_mixed_type"
    assert records["10.1/mixed"]["source_screening_snapshot"]["eligibility"] == (
        "pending"
    )
    assert records["s2:missing"]["metadata_disposition"] == ("unresolved_missing_type")
    assert "missing_abstract" in records["s2:missing"]["metadata_flags"]
    assert records["10.1/article"]["metadata_disposition"] == ("retained_article_type")
    assert records["10.1/conference"]["metadata_disposition"] == (
        "unresolved_other_type"
    )


def test_title_terms_are_flags_not_geography_or_removal(tmp_path: Path) -> None:
    run(tmp_path)
    records = read_records(tmp_path)
    missing = records["s2:missing"]
    article = records["10.1/article"]
    assert missing["title_review_terms"] == ["arctic"]
    assert missing["source_screening_snapshot"]["eligibility"] == "unreviewed"
    assert article["title_review_terms"] == []
    assert article["metadata_disposition"] == "retained_article_type"
