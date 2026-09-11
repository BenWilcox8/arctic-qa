from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"


def cli(
    root: Path, *arguments: str, expected: int = 0
) -> subprocess.CompletedProcess[str]:
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
    return result


def smoke(root: Path, run_id: str = "test-smoke") -> dict:
    result = cli(root, "smoke", "--fixture-dir", str(FIXTURES), "--run-id", run_id)
    return json.loads(result.stdout)


def database(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / "arctic-qa" / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def candidate(root: Path) -> dict:
    with database(root) as connection:
        row = connection.execute(
            "SELECT candidate_json FROM candidates LIMIT 1"
        ).fetchone()
    return json.loads(row[0])


def write_candidate(tmp_path: Path, payload: dict, name: str) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def author_script(tmp_path: Path, prefix: dict) -> Path:
    lines = [
        json.dumps(prefix),
        *FIXTURES.joinpath("fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines(),
    ]
    path = tmp_path / f"author-{prefix['kind']}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def generate_command(
    source_id: str,
    run_id: str,
    author: Path,
    *,
    retries: int = 1,
    budget: str = "10000",
    reservation: str = "100",
) -> tuple[str, ...]:
    return (
        "generate",
        "--source-id",
        source_id,
        "--run-id",
        run_id,
        "--author-provider",
        "fake",
        "--author-model",
        "claude-opus-5",
        "--author-script",
        str(author),
        "--verifier-provider",
        "fake",
        "--verifier-model",
        "gemini-3.1-pro-preview",
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
        "--budget-limit",
        budget,
        "--reservation",
        reservation,
        "--retries",
        str(retries),
    )


def test_absent_mount_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    result = cli(missing, "doctor", expected=2)
    assert "DATA_ROOT_UNAVAILABLE" in result.stderr
    assert not missing.exists()


def test_discovery_replay_pages_and_deduplicates(tmp_path: Path) -> None:
    replay = tmp_path / "discovery.json"
    record = {
        "doi": "10.1234/example",
        "title": "One result",
        "authors": [{"name": "A Researcher"}],
        "published_date": "2024-01-01",
    }
    replay.write_text(
        json.dumps(
            [
                {
                    "adapter": "replay",
                    "query": "q",
                    "page_number": 1,
                    "records": [record],
                },
                {
                    "adapter": "replay",
                    "query": "q",
                    "page_number": 2,
                    "records": [record],
                },
            ]
        ),
        encoding="utf-8",
    )
    result = json.loads(
        cli(tmp_path, "discover", "--adapter", "replay", "--input", str(replay)).stdout
    )
    assert result["added"] == 1
    assert result["duplicates"] == 1
    assert result["pages"] == 2
    assert result["manifest"]["record_count"] == 1


@pytest.mark.parametrize(
    ("mutation", "reason", "label"),
    [
        ("wrong_quote", "answer_evidence_not_located", "rejected"),
        ("qualifier_loss", "scope_qualifier_missing", "rejected"),
        ("ambiguous", "answer_ambiguous", "unresolved"),
        ("causal_overclaim", "causal_overclaim", "rejected"),
    ],
)
def test_validation_rejects_answer_failures(
    tmp_path: Path, mutation: str, reason: str, label: str
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    if mutation == "wrong_quote":
        item["answer"]["evidence_quote"] = (
            "A fabricated quote that is not in the source."
        )
    elif mutation == "qualifier_loss":
        item["question"] = "What water depth was reported?"
    elif mutation == "ambiguous":
        item["reconstruction"]["ambiguity_label"] = "multiple_answers"
    else:
        item["question_claim_type"] = "causal"
    path = write_candidate(tmp_path, item, f"{mutation}.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == label
    assert reason in result["reasons"]


def test_true_distractors_and_equivalent_units_are_not_false(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["distractors"][0]["text"] = "2 m"
    item["distractors"][0]["numeric"] = {"canonical_value": "200", "unit": "cm"}
    item["distractors"][1]["true_under_other_scope"] = True
    item["distractors"][2]["text"] = "2.0 m"
    item["distractors"][3]["text"] = "None of the above"
    path = write_candidate(tmp_path, item, "true-distractors.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "rejected"
    reasons = [reason for row in result["distractors"] for reason in row["reasons"]]
    assert "distractor_is_equivalent_numeric_answer" in reasons
    assert "distractor_true_under_other_scope" in reasons
    assert "distractor_matches_answer" in reasons
    assert "forbidden_meta_option" in reasons


def test_malformed_json_and_429_retry_are_recorded(tmp_path: Path) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    malformed = author_script(tmp_path, {"role": "extractor", "kind": "malformed"})
    result = cli(tmp_path, *generate_command(source_id, "malformed-run", malformed))
    assert json.loads(result.stdout)["item_id"]
    rate_limited = author_script(tmp_path, {"role": "extractor", "kind": "429"})
    result = cli(tmp_path, *generate_command(source_id, "rate-run", rate_limited))
    assert json.loads(result.stdout)["item_id"]
    with database(tmp_path) as connection:
        codes = {
            row[0]
            for row in connection.execute(
                "SELECT error_code FROM calls WHERE error_code IS NOT NULL"
            )
        }
    assert {"MALFORMED_RESPONSE", "HTTP_429"} <= codes


def test_timeout_creates_ambiguous_receipt_and_resume_gate(tmp_path: Path) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    timeout = author_script(tmp_path, {"role": "extractor", "kind": "timeout"})
    result = cli(
        tmp_path, *generate_command(source_id, "timeout-run", timeout), expected=2
    )
    assert "AMBIGUOUS_CHARGE" in result.stderr
    resumed = json.loads(cli(tmp_path, "resume", "--run-id", "timeout-run").stdout)
    assert resumed["manual_reconciliation_required"] is True
    assert resumed["incomplete_calls"][0]["status"] == "ambiguous_charge"


def test_budget_exhaustion_stops_before_provider_call(tmp_path: Path) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    result = cli(
        tmp_path,
        *generate_command(
            source_id,
            "budget-run",
            FIXTURES / "fake-author.jsonl",
            budget="50",
            reservation="100",
        ),
        expected=2,
    )
    assert "BUDGET_EXHAUSTED" in result.stderr
    with database(tmp_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM calls WHERE run_id='budget-run'"
        ).fetchone()[0]
    assert count == 0


def test_provider_errors_redact_secrets(tmp_path: Path) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    script = author_script(
        tmp_path,
        {
            "role": "extractor",
            "kind": "error",
            "message": "Authorization: sk-private-secret-123456789",
        },
    )
    cli(
        tmp_path,
        *generate_command(source_id, "secret-run", script, retries=0),
        expected=2,
    )
    with database(tmp_path) as connection:
        error_text = connection.execute(
            "SELECT error_text FROM calls WHERE run_id='secret-run'"
        ).fetchone()[0]
    assert "sk-private-secret" not in error_text
    assert "[REDACTED]" in error_text


def test_stable_exports_and_absent_answer_label(tmp_path: Path) -> None:
    receipt = smoke(tmp_path, "stable-run")
    first = cli(tmp_path, "export", "--run-id", "stable-run", "--seed", "fixed")
    second = cli(tmp_path, "export", "--run-id", "stable-run", "--seed", "fixed")
    assert first.stdout == second.stdout
    manifest = json.loads(first.stdout)
    mcq_path = tmp_path / "arctic-qa" / manifest["files"]["mcq"]
    rows = [
        json.loads(line) for line in mcq_path.read_text(encoding="utf-8").splitlines()
    ]
    absent = next(row for row in rows if row["task_type"] == "answer_absent_mcq")
    assert absent["evidence_state"] == "invalid_option_set"
    assert "does not establish model ignorance" in absent["interpretation_limit"]
    assert receipt["test_only"] is True


def test_smoke_resume_does_not_duplicate_calls(tmp_path: Path) -> None:
    first = smoke(tmp_path, "resume-run")
    second = smoke(tmp_path, "resume-run")
    assert second["resumed"] is True
    assert first["item_id"] == second["item_id"]
    with database(tmp_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM calls WHERE run_id='resume-run'"
        ).fetchone()[0]
    assert count == 5


def test_shared_content_keeps_per_source_provenance(tmp_path: Path) -> None:
    metadata = tmp_path / "two-sources.json"
    metadata.write_text(
        json.dumps(
            [
                {
                    "stable_id": "test:source-a",
                    "title": "Source A",
                    "year": 2026,
                    "discipline": "test",
                },
                {
                    "stable_id": "test:source-b",
                    "title": "Source B",
                    "year": 2026,
                    "discipline": "test",
                },
            ]
        ),
        encoding="utf-8",
    )
    cli(tmp_path, "discover", "--adapter", "manual", "--input", str(metadata))
    with database(tmp_path) as connection:
        source_ids = [
            row[0]
            for row in connection.execute(
                "SELECT source_id FROM sources ORDER BY source_id"
            )
        ]
    for source_id in source_ids:
        cli(
            tmp_path,
            "fetch",
            "--source-id",
            source_id,
            "--url",
            (FIXTURES / "public-source.html").as_uri(),
            "--media-type",
            "text/html",
        )
        cli(tmp_path, "extract", "--source-id", source_id)
    with database(tmp_path) as connection:
        original_rows = connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='original'"
        ).fetchone()[0]
    original_files = list(
        (tmp_path / "arctic-qa" / "originals").glob("*/*/source.html")
    )
    assert original_rows == 2
    assert len(original_files) == 1
