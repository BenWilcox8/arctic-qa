from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctic_qa.source_pass import run_source_pass


BASE_POLICY = Path(__file__).parents[1] / "config" / "source-screening-policy-v1.json"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture_inputs(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    candidates = []
    queues = []
    for number in range(1, 7):
        key = f"10.1234/source-{number}"
        candidates.append(
            {
                "candidate_key": key,
                "doi": key,
                "stable_id": f"s2:{number}",
                "title": f"Fixture Arctic source number {number}",
                "authors": ["A. Researcher"],
                "year": 2026,
                "type": "JournalArticle",
                "origins": ["semantic_scholar:q01"],
                "open_access": (
                    {"url": f"https://example.test/{number}", "status": "GREEN"}
                    if number != 6
                    else {}
                ),
                "landing_url": f"https://example.test/landing/{number}",
                "version_relation": "unknown",
                "retraction_correction": "unknown_not_checked",
            }
        )
        queue_name = "priority_seed_review" if number in {1, 2} else "retrieval_review"
        queues.append(
            {
                "candidate_key": key,
                "sequence": 100 - number,
                "queue": {"name": queue_name, "rank": 1 if number < 3 else 2},
                "metadata_disposition": "priority_seed"
                if number < 3
                else "retained_article_type",
                "reason_code": "fixture_selection",
            }
        )
    queue_file = root / "queue.ndjson"
    queue_file.write_text("".join(json.dumps(row) + "\n" for row in queues))
    candidates_file = root / "candidates.json"
    protocol_file = root / "protocol.json"
    screening_file = root / "screening.json"
    policy_file = root / "policy.json"
    write_json(candidates_file, candidates)
    write_json(protocol_file, {"protocol_id": "arctic-corpus-search-r1-protocol-v2"})
    write_json(screening_file, [])
    policy = json.loads(BASE_POLICY.read_text())
    policy["selection_size"] = 6
    policy["smoke_size"] = 2
    policy["minimum_seconds_between_requests"] = 0
    write_json(policy_file, policy)
    return queue_file, candidates_file, protocol_file, screening_file, policy_file


def fixture_fetcher(calls: list[str]):
    def fetch(url: str, **_: object) -> dict[str, object]:
        calls.append(url)
        number = int(url.rsplit("/", 1)[1])
        title = f"Fixture Arctic source number {number}"
        doi = f"10.1234/source-{number}"
        if number == 1:
            body = f"<article><title>{title}</title><p>{doi}</p><p>All samples were collected at 71.3 N.</p></article>".encode()
            return {
                "state": "retrieved",
                "status": 200,
                "final_url": url,
                "media_type": "application/xml",
                "body": body,
            }
        if number == 2:
            return {
                "state": "retrieved",
                "status": 200,
                "final_url": url,
                "media_type": "text/html",
                "body": f"<html><title>{title}</title><p>{doi}</p></html>".encode(),
            }
        if number == 3:
            return {
                "state": "retrieved",
                "status": 200,
                "final_url": url,
                "media_type": "application/xml",
                "body": b"<article><title>A different paper</title></article>",
            }
        if number == 4:
            return {
                "state": "retrieved",
                "status": 200,
                "final_url": url,
                "media_type": "application/pdf",
                "body": b"not a PDF",
            }
        return {
            "state": "error",
            "reason_code": "content_length_over_limit",
            "declared_bytes": 60 * 1024 * 1024,
            "retryable": False,
        }

    return fetch


def run_fixture(
    root: Path,
    action: str,
    *,
    calls: list[str],
    decisions_file: Path | None = None,
) -> dict[str, object]:
    queue, candidates, protocol, screening, policy = fixture_inputs(root)
    return run_source_pass(
        action=action,
        queue_file=queue,
        candidates_file=candidates,
        protocol_file=protocol,
        prior_screening_file=screening,
        policy_file=policy,
        output_dir=root / "run",
        run_id="fixture-source-r1",
        code_commit="deadbeef",
        viewer_progress_file=root / "viewer-progress.json",
        decisions_file=decisions_file,
        fetcher=fixture_fetcher(calls),
        sleep_fn=lambda _: None,
    )


def test_prepare_smoke_resume_and_access_outcomes(tmp_path: Path) -> None:
    calls: list[str] = []
    prepared = run_fixture(tmp_path, "prepare", calls=calls)
    assert prepared["state"] == "prepared"
    manifest = json.loads((tmp_path / "run" / "run-manifest.json").read_text())
    assert [row["candidate_key"] for row in manifest["selection"][:2]] == [
        "10.1234/source-2",
        "10.1234/source-1",
    ]
    assert calls == []
    viewer_progress = json.loads((tmp_path / "viewer-progress.json").read_text())
    assert viewer_progress["state"] == "not_running"
    assert viewer_progress["stage"] == "source_screening"

    smoke = run_fixture(tmp_path, "smoke", calls=calls)
    assert smoke["state"] == "paused"
    assert smoke["counts"]["processed"] == 2
    assert smoke["counts"]["unattempted"] == 4

    completed_acquisition = run_fixture(tmp_path, "continue", calls=calls)
    assert completed_acquisition["state"] == "paused"
    assert completed_acquisition["counts"] == {
        "selected": 6,
        "processed": 6,
        "attempted": 5,
        "retrieved": 4,
        "full_text_retrieved": 1,
        "eligible": 0,
        "excluded": 0,
        "pending": 6,
        "unattempted": 0,
    }
    assert len(calls) == 5
    replay = run_fixture(tmp_path, "continue", calls=calls)
    assert replay["counts"]["processed"] == 6
    assert len(calls) == 5

    receipts = {
        row["candidate_key"]: row
        for row in (
            json.loads(path.read_text())
            for path in (tmp_path / "run" / "items").glob("*.json")
        )
    }
    assert (
        receipts["10.1234/source-3"]["access_state"] == "retrieved_identity_unresolved"
    )
    assert receipts["10.1234/source-4"]["access_state"] == "retrieved_extraction_error"
    assert receipts["10.1234/source-5"]["access_state"] == "not_retrieved"
    assert receipts["10.1234/source-5"]["reason_code"] == "content_length_over_limit"
    assert (
        receipts["10.1234/source-6"]["access_state"] == "unattempted_no_open_access_url"
    )


def test_changed_input_is_refused_after_manifest_freeze(tmp_path: Path) -> None:
    calls: list[str] = []
    run_fixture(tmp_path, "smoke", calls=calls)
    candidates_file = tmp_path / "candidates.json"
    candidates = json.loads(candidates_file.read_text())
    candidates[0]["title"] = "Changed title"
    write_json(candidates_file, candidates)
    queue = tmp_path / "queue.ndjson"
    protocol = tmp_path / "protocol.json"
    screening = tmp_path / "screening.json"
    policy = tmp_path / "policy.json"
    with pytest.raises(ValueError, match="manifest changed"):
        run_source_pass(
            action="continue",
            queue_file=queue,
            candidates_file=candidates_file,
            protocol_file=protocol,
            prior_screening_file=screening,
            policy_file=policy,
            output_dir=tmp_path / "run",
            run_id="fixture-source-r1",
            code_commit="deadbeef",
            fetcher=fixture_fetcher(calls),
        )


def test_retry_receipt_is_resumed_without_repeating_an_attempt(tmp_path: Path) -> None:
    calls: list[str] = []
    run_fixture(tmp_path, "prepare", calls=calls)
    write_json(
        tmp_path / "run" / "attempts" / "item-000001-attempt-1.json",
        {
            "candidate_key": "10.1234/source-2",
            "state": "error",
            "reason_code": "transport_error",
            "retryable": True,
            "attempt": 1,
            "attempted_at_utc": "2026-09-12T00:00:00Z",
        },
    )
    run_fixture(tmp_path, "smoke", calls=calls)
    item = json.loads((tmp_path / "run" / "items" / "item-000001.json").read_text())
    assert [row["attempt"] for row in item["attempts"]] == [1, 2]
    assert calls.count("https://example.test/2") == 1


def test_interrupted_success_is_not_downloaded_twice(tmp_path: Path) -> None:
    calls: list[str] = []
    run_fixture(tmp_path, "prepare", calls=calls)
    write_json(
        tmp_path / "run" / "attempts" / "item-000001-attempt-1.json",
        {
            "candidate_key": "10.1234/source-2",
            "state": "retrieved",
            "status": 200,
            "final_url": "https://example.test/2",
            "media_type": "text/html",
            "attempt": 1,
            "attempted_at_utc": "2026-09-12T00:00:00Z",
        },
    )
    run_fixture(tmp_path, "smoke", calls=calls)
    item = json.loads((tmp_path / "run" / "items" / "item-000001.json").read_text())
    assert item["access_state"] == "retrieval_interrupted_after_response"
    assert "https://example.test/2" not in calls


def proposal(root: Path, *, eligibility: str, geography: str, quote: str) -> Path:
    item = next(
        json.loads(path.read_text())
        for path in (root / "run" / "items").glob("*.json")
        if json.loads(path.read_text())["candidate_key"] == "10.1234/source-1"
    )
    text = Path(item["extraction_path"]).read_text()
    start = text.index("All samples")
    decision = {
        "candidate_key": "10.1234/source-1",
        "scientific_eligibility": eligibility,
        "reason_code": "fixture_source_decision",
        "source_content_hash": item["source_content_hash"],
        "source_version": item["source_version"],
        "decision_author": "fixture-reviewer",
        "decision_method_version": "codex-native-semantic-source-review-v1",
        "criteria": {
            "published_primary_findings": {"verdict": "met"},
            "stable_identity_version": {"verdict": "met"},
            "geography": {"verdict": geography},
            "lawful_access": {"verdict": "met"},
            "correction_retraction": {"verdict": "unknown_not_checked"},
        },
        "evidence_passages": [
            {
                "quote": quote,
                "locator": {
                    "extraction_sha256": item["extraction_sha256"],
                    "start_offset": start,
                    "end_offset": start + len(quote),
                    "section": "article",
                    "page": None,
                },
            }
        ],
        "limitations": ["Correction coverage is unknown."],
    }
    path = root / f"decision-{eligibility}-{geography}.json"
    write_json(
        path, {"schema": "source-decision-proposals-v1", "decisions": [decision]}
    )
    return path


def test_exact_quote_and_overall_eligibility_rules(tmp_path: Path) -> None:
    calls: list[str] = []
    run_fixture(tmp_path, "continue", calls=calls)
    good_quote = "All samples were collected at 71.3 N."

    bad_quote = proposal(
        tmp_path, eligibility="eligible", geography="core", quote="Wrong quote"
    )
    with pytest.raises(ValueError, match="quote does not equal"):
        run_fixture(tmp_path, "decide", calls=calls, decisions_file=bad_quote)

    mixed_eligible = proposal(
        tmp_path, eligibility="eligible", geography="mixed", quote=good_quote
    )
    with pytest.raises(ValueError, match="does not meet all"):
        run_fixture(tmp_path, "decide", calls=calls, decisions_file=mixed_eligible)

    wrong_hash = proposal(
        tmp_path, eligibility="excluded", geography="mixed", quote=good_quote
    )
    payload = json.loads(wrong_hash.read_text())
    payload["decisions"][0]["source_content_hash"] = "0" * 64
    write_json(wrong_hash, payload)
    with pytest.raises(ValueError, match="source hash does not match"):
        run_fixture(tmp_path, "decide", calls=calls, decisions_file=wrong_hash)

    mixed_excluded = proposal(
        tmp_path, eligibility="excluded", geography="mixed", quote=good_quote
    )
    receipt = run_fixture(
        tmp_path, "decide", calls=calls, decisions_file=mixed_excluded
    )
    assert receipt["state"] == "completed"
    assert receipt["counts"]["excluded"] == 1
    assert receipt["counts"]["pending"] == 4
    assert receipt["counts"]["unattempted"] == 1
    assert receipt["decision_method_counts"] == {
        "codex-native-semantic-source-review-v1": 1
    }
    assert receipt["deterministic_checks_are_not_semantic_screening"] is True
    overlay = json.loads((tmp_path / "run" / receipt["overlay_file"]).read_text())
    decided = next(
        row for row in overlay["records"] if row["candidate_key"] == "10.1234/source-1"
    )
    assert decided["geography_verdict"] == "mixed"
    assert decided["scientific_eligibility"] == "excluded"
    replay = run_fixture(tmp_path, "decide", calls=calls, decisions_file=mixed_excluded)
    assert replay["idempotent_replay"] is True
    assert len(list((tmp_path / "run").glob("source-screening-overlay-r*.json"))) == 1
