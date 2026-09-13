from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_module_reports_fixture_coverage_spend_and_accuracy_limit(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path / "exports" / "fixture-export"
    short_answers = export_dir / "short_answer.jsonl"
    mcqs = export_dir / "mcq.jsonl"
    rejections = export_dir / "rejections.jsonl"
    _write_jsonl(
        short_answers,
        [
            {
                "item_id": "qa-1",
                "reference_answers": ["Ice"],
                "evidence": {"quote": "Ice forms.", "locator": {"chunk_id": "c1"}},
                "source": {"source_id": "source-1"},
                "qa_check_passed": True,
                "reconstruction_agreement": True,
            }
        ],
    )
    _write_jsonl(
        mcqs,
        [
            {
                "item_id": "mcq-1",
                "source": {"source_id": "source-1"},
                "options": [
                    {"text": "Ice", "is_correct": True},
                    {
                        "text": "Sand",
                        "is_correct": False,
                        "falsity_evidence": {"quote": "Ice forms."},
                    },
                    {
                        "text": "Rock",
                        "is_correct": False,
                        "falsity_evidence": {"quote": "Ice forms."},
                    },
                    {
                        "text": "Rain",
                        "is_correct": False,
                        "falsity_evidence": {"quote": "Ice forms."},
                    },
                ],
            }
        ],
    )
    _write_jsonl(
        rejections,
        [{"stage": "validation", "reason_code": "reconstruction_disagreement"}],
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "export_id": "fixture-export",
                "run_id": "fixture-run",
                "test_only": True,
                "files": {
                    "short_answer": "exports/fixture-export/short_answer.jsonl",
                    "mcq": "exports/fixture-export/mcq.jsonl",
                    "rejections": "exports/fixture-export/rejections.jsonl",
                },
            }
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps({"spent_usd": "1.25", "reserved_usd": "0.25"}), encoding="utf-8"
    )
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({"provider_policy": {"same_model_roles": True}}), encoding="utf-8"
    )
    json_out = tmp_path / "summary.json"
    markdown_out = tmp_path / "summary.md"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "arctic_qa.quality_summary",
            "--export-manifest",
            str(manifest),
            "--source-root",
            str(tmp_path),
            "--ledger",
            str(ledger),
            "--status",
            str(status),
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
        ],
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    summary = json.loads(json_out.read_text(encoding="utf-8"))
    assert summary["input"]["evidence_mode"] == "fixture"
    assert summary["items"]["accepted_short_answer"] == 1
    assert summary["items"]["incomplete_short_answer"] == 0
    assert summary["items"]["mcq"] == 1
    assert summary["items"]["unique_sources"] == 1
    assert summary["source_identity"]["source_ids"] == ["source-1"]
    assert summary["check_coverage"]["recorded_qa_check_pass"]["percent"] == 100.0
    assert summary["check_coverage"]["distractor_falsity_evidence"]["passed"] == 3
    assert summary["rejections"]["recorded_disagreement_count"] == 1
    assert summary["spend"]["spent_usd"] == "1.25"
    assert summary["validity"]["target_validity_percent"] == 95
    assert (
        summary["validity"]["independently_established_scientific_accuracy"]
        == "unknown_not_yet_estimable"
    )
    markdown = markdown_out.read_text(encoding="utf-8")
    assert "Fixture input" in markdown
    assert "not scientific accuracy" in markdown
