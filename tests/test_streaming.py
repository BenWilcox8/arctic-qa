from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def run_cli(root: Path, *arguments: str, expected: int = 0) -> dict:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "arctic_qa",
            "--data-root",
            str(root),
            "--test-mode",
            "--json",
            *arguments,
        ],
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected, result.stderr or result.stdout
    return json.loads(result.stdout if expected == 0 else result.stderr)


def streaming_fixture(tmp_path: Path) -> tuple[Path, Path]:
    access = tmp_path / "access"
    eligibility = tmp_path / "eligibility"
    source = access / "originals" / "source.html"
    source.parent.mkdir(parents=True)
    source.write_bytes((FIXTURES / "public-source.html").read_bytes())
    extracted = access / "extracted" / "text.txt"
    extracted.parent.mkdir(parents=True)
    extracted.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    candidate_key = "test-only:streaming-paper"
    source_hash = sha256(source.read_bytes()).hexdigest()
    extraction_hash = sha256(extracted.read_bytes()).hexdigest()
    write_json(
        access / "items" / "item-000001.json",
        {
            "schema": "article-access-item-v1",
            "run_id": "access-fixture",
            "position": 1,
            "candidate_key": candidate_key,
            "subgroup": "test_only",
            "title": "Synthetic public Arctic extraction fixture",
            "doi": None,
            "authors": [{"name": "Arctic QA test suite"}],
            "published_date": "2026-09-11",
            "year": 2026,
            "discipline": "synthetic calibration",
            "paper_family_id": "family-test-only-public-arctic-v1",
            "access_state": "full_text_ready",
            "identity_verified": True,
            "source_path": str(source),
            "source_content_hash": source_hash,
            "extraction_path": str(extracted),
            "extraction_sha256": extraction_hash,
            "extraction_coverage": {"article_body_recognized": True},
            "media_type": "text/html",
            "final_url": "https://example.invalid/public-source.html",
            "license": "CC0-1.0",
        },
    )
    write_json(
        access / "run-manifest.json",
        {
            "run_id": "access-fixture",
            "target_total": 1,
            "selection": [
                {
                    "position": 1,
                    "candidate_key": candidate_key,
                    "subgroup": "test_only",
                }
            ],
        },
    )
    write_json(access / "progress.json", {"state": "completed"})
    write_json(access / "run-receipt.json", {"state": "completed"})
    write_json(
        eligibility / "jobs" / "fixture-job.json",
        {
            "schema": "gemini-eligibility-job-v1",
            "job_key": "fixture-job",
            "candidate_key": candidate_key,
            "model": "gemini-3.8-flash",
            "state": "completed",
            "source_content_hash": source_hash,
            "extraction_sha256": extraction_hash,
            "parsed_response": {
                "schema_version": "eligibility-response-v1",
                "request_id": "fixture-job",
                "overall": "eligible",
                "overall_reason_codes": ["all_required_criteria_satisfied"],
            },
            "validation": {
                "valid": True,
                "errors": [],
                "decision": "eligible",
            },
            "actual_cost_usd": "0.01",
        },
    )
    return access, eligibility


def test_streaming_cli_moves_one_eligible_paper_to_validated_export(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-fixture",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
        "--max-papers",
        "1",
    )

    assert result["state"] == "completed"
    assert result["counts"] == {
        "accepted_base_questions": 1,
        "eligibility_rejected": 0,
        "generation_rejected": 0,
        "processed": 1,
    }
    assert result["export"]["short_answer_count"] == 1
    assert result["export"]["mcq_count"] == 2
    assert result["provider_policy"] == {
        "model": "fake-gemini-3.8-flash",
        "same_model_roles": True,
        "correlated_error_disclosed": True,
        "live_provider": False,
    }


def test_streaming_cli_stops_after_eligibility_rejection(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    receipt_path = eligibility / "jobs" / "fixture-job.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["parsed_response"]["overall"] = "excluded"
    receipt["validation"]["decision"] = "excluded"
    write_json(receipt_path, receipt)
    empty_author = tmp_path / "empty-author.jsonl"
    empty_verifier = tmp_path / "empty-verifier.jsonl"
    empty_author.write_text("", encoding="utf-8")
    empty_verifier.write_text("", encoding="utf-8")

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-rejected",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(empty_author),
        "--verifier-script",
        str(empty_verifier),
        "--max-papers",
        "1",
    )

    assert result["state"] == "completed"
    assert result["counts"] == {
        "accepted_base_questions": 0,
        "eligibility_rejected": 1,
        "generation_rejected": 0,
        "processed": 1,
    }
    assert result["export"]["short_answer_count"] == 0
    assert result["export"]["mcq_count"] == 0


def test_streaming_cli_reports_the_eligibility_rejection_reason(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    receipt_path = eligibility / "jobs" / "fixture-job.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["parsed_response"]["overall"] = "excluded"
    receipt["parsed_response"]["overall_reason_codes"] = ["study_geography_not_arctic"]
    receipt["validation"]["decision"] = "excluded"
    write_json(receipt_path, receipt)
    empty_author = tmp_path / "unused-author.jsonl"
    empty_verifier = tmp_path / "unused-verifier.jsonl"
    empty_author.write_text("", encoding="utf-8")
    empty_verifier.write_text("", encoding="utf-8")

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-rejection-reason",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(empty_author),
        "--verifier-script",
        str(empty_verifier),
    )

    assert result["paper_results"] == [
        {
            "candidate_key": "test-only:streaming-paper",
            "disposition": "eligibility_rejected",
            "reason_codes": ["study_geography_not_arctic"],
            "source_id": None,
        }
    ]


def test_streaming_cli_resumes_without_a_duplicate_model_call(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    arguments = (
        "stream",
        "--run-id",
        "stream-resume",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
    )
    first = run_cli(tmp_path, *arguments)
    empty_author = tmp_path / "empty-resume-author.jsonl"
    empty_verifier = tmp_path / "empty-resume-verifier.jsonl"
    empty_author.write_text("", encoding="utf-8")
    empty_verifier.write_text("", encoding="utf-8")
    resumed_arguments = list(arguments)
    resumed_arguments[resumed_arguments.index(str(FIXTURES / "fake-author.jsonl"))] = (
        str(empty_author)
    )
    resumed_arguments[
        resumed_arguments.index(str(FIXTURES / "fake-verifier.jsonl"))
    ] = str(empty_verifier)

    second = run_cli(tmp_path, *resumed_arguments)

    assert first["counts"]["accepted_base_questions"] == 1
    assert second["counts"]["accepted_base_questions"] == 1
    assert second["resumed_papers"] == 1
    status = run_cli(tmp_path, "status", "--run-id", "stream-resume")
    assert status["calls"] == [{"count": 9, "status": "completed"}]


def test_streaming_cli_fails_closed_on_an_unbound_selection(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    manifest_path = access / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["selection"][0]["candidate_key"] = "different-paper"
    write_json(manifest_path, manifest)

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-unbound-selection",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
        expected=2,
    )

    assert result["code"] == "VALUEERROR"
    assert result["message"] == "the ordered selection does not match its access item"


def test_streaming_finding_selection_reads_results_not_only_the_longest_chunk(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    source_path = access / "originals" / "source.html"
    source_path.write_text(
        "<html><body><h1>Synthetic public Arctic extraction fixture</h1>"
        "<h2>Introduction</h2><p>"
        + ("Background material without a reported finding. " * 140)
        + "</p><h2>Results</h2><p>"
        "The complete study site was at 71.3 N. "
        "The reported water depth was 2.0 m with a source-grounded tolerance of 0.1 m."
        "</p></body></html>",
        encoding="utf-8",
    )
    extracted_path = access / "extracted" / "text.txt"
    extracted_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    source_hash = sha256(source_path.read_bytes()).hexdigest()
    extraction_hash = sha256(extracted_path.read_bytes()).hexdigest()
    access_path = access / "items" / "item-000001.json"
    access_item = json.loads(access_path.read_text(encoding="utf-8"))
    access_item["source_content_hash"] = source_hash
    access_item["extraction_sha256"] = extraction_hash
    write_json(access_path, access_item)
    eligibility_path = eligibility / "jobs" / "fixture-job.json"
    eligibility_item = json.loads(eligibility_path.read_text(encoding="utf-8"))
    eligibility_item["source_content_hash"] = source_hash
    eligibility_item["extraction_sha256"] = extraction_hash
    write_json(eligibility_path, eligibility_item)
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    author_events[0]["require_prompt_contains"] = [
        '"heading":"Results"',
        "The reported water depth was 2.0 m",
    ]
    author_script = tmp_path / "results-author.jsonl"
    author_script.write_text(
        "\n".join(json.dumps(event) for event in author_events) + "\n",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-results",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(author_script),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
    )

    assert result["counts"]["accepted_base_questions"] == 1


def test_streaming_family_freeze_spans_test_and_production_phases(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    shared = (
        "--campaign-id",
        "streaming-commission",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
    )
    first = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "live-test-phase",
        *shared,
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
    )
    empty_author = tmp_path / "unused-production-author.jsonl"
    empty_verifier = tmp_path / "unused-production-verifier.jsonl"
    empty_author.write_text("", encoding="utf-8")
    empty_verifier.write_text("", encoding="utf-8")

    second = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "production-phase",
        *shared,
        "--author-script",
        str(empty_author),
        "--verifier-script",
        str(empty_verifier),
    )

    assert first["campaign_id"] == "streaming-commission"
    assert second["campaign_id"] == "streaming-commission"
    assert second["resumed_papers"] == 1
    status = run_cli(tmp_path, "status", "--run-id", "streaming-commission")
    assert status["calls"] == [{"count": 9, "status": "completed"}]


def test_streaming_export_discloses_same_model_correlated_error(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-disclosure",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
    )
    short_answer_path = (
        tmp_path / "arctic-qa" / result["export"]["files"]["short_answer"]
    )
    exported = json.loads(short_answer_path.read_text(encoding="utf-8"))

    assert exported["provenance"]["construction_role_policy"] == {
        "author_model": "fake-gemini-3.8-flash",
        "verifier_model": "fake-gemini-3.8-flash",
        "same_provider_family": True,
        "separate_blinded_calls": True,
        "independent_error_evidence": False,
    }
