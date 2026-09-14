from __future__ import annotations

import json
import sqlite3
import sys
import csv
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from arctic_qa.publication_export import export_publication_package  # noqa: E402


def test_reviewer_preserves_verdict_rationale_while_benchmark_hides_labels(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript("""
        CREATE TABLE sources (source_id TEXT,stable_id TEXT,doi TEXT,title TEXT,year INTEGER,content_hash TEXT,paper_family_id TEXT,inclusion_reason TEXT,metadata_json TEXT);
        CREATE TABLE candidates (item_id TEXT,run_id TEXT,source_id TEXT,status TEXT,candidate_json TEXT);
        CREATE TABLE validation_events (item_id TEXT,label TEXT,reason_codes_json TEXT,details_json TEXT,created_at TEXT);
        CREATE TABLE calls (call_id TEXT,role TEXT,request_id TEXT,returned_model TEXT,response_json TEXT,attempt INTEGER);
    """)
    source = ("source-1", "paper-1", "10.1/example", "Example paper", 2026, "hash-1", "family-1", "selected", json.dumps({"selection": {"position": 4, "reason_codes": ["arctic"]}}))
    candidate = {"item_id": "qa-1", "question": "Which value?", "answer": {"text": "Correct", "evidence_quote": "short evidence", "locator": {"page": 2}}, "distractors": [{"text": text, "type": "wrong", "evidence_quote": "evidence"} for text in ("Wrong A", "Wrong B", "Wrong C")], "option_verdicts": [{"option_text": text, "rationale": f"why {text} is wrong", "provenance": {"request_id": f"request-{index}"}} for index, text in enumerate(("Wrong A", "Wrong B", "Wrong C"))], "provenance": {"run_id": "trial-r1", "verification_calls": {}}}
    details = {"distractors": [{"text": text, "accepted": True, "deterministic": True} for text in ("Wrong A", "Wrong B", "Wrong C")]}
    connection.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?)", source)
    connection.execute("INSERT INTO candidates VALUES (?,?,?,?,?)", ("qa-1", "trial-r1", "source-1", "machine_accepted_unverified", json.dumps(candidate)))
    connection.execute("INSERT INTO validation_events VALUES (?,?,?,?,?)", ("qa-1", "machine_accepted_unverified", "[]", json.dumps(details), "now"))
    connection.commit()
    connection.close()
    manifest = export_publication_package(db_path, tmp_path / "package", run_id="trial-r1", seed="fixed")
    assert manifest["reviewer_item_count"] == 1
    reviewer = json.loads((tmp_path / "package" / "reviewer-items.jsonl").read_text())
    benchmark = json.loads((tmp_path / "package" / "benchmark-inputs.jsonl").read_text())
    assert reviewer["paper"]["doi"] == "10.1/example"
    assert any(option.get("verdict") for option in reviewer["options"])
    assert "is_correct" not in benchmark["options"][0]
    assert "rationale" not in (tmp_path / "package" / "benchmark-inputs.jsonl").read_text()
    with (tmp_path / "package" / "reviewer-items.csv").open(newline="", encoding="utf-8") as handle:
        csv_row = next(csv.DictReader(handle))
    assert csv_row["reference_answer"] == "Correct"
    assert csv_row["option_a_verdict"] or csv_row["option_b_verdict"]


def test_manifest_selected_variants_keep_their_exact_options(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports" / "trial"
    export_dir.mkdir(parents=True)
    variants = [
        {
            "item_id": "mcq-present",
            "paired_item_id": "question-1",
            "task_type": "answer_present_mcq",
            "question": "What changed?",
            "release_label": "machine_accepted_unverified",
            "source": {"source_id": "source-1", "content_hash": "source-hash"},
            "answer_evidence": {"quote": "The measured value increased.", "locator": {"page": 2}},
            "options": [
                {"text": "It increased", "is_correct": True},
                {"text": "It decreased", "is_correct": False, "verification_label": "model-verified"},
            ],
        },
        {
            "item_id": "mcq-absent",
            "paired_item_id": "question-1",
            "task_type": "answer_absent_mcq",
            "question": "What changed?",
            "evidence_state": "invalid_option_set",
            "interpretation_limit": "This is not an ignorance result.",
            "source": {"source_id": "source-1", "content_hash": "source-hash"},
            "options": [
                {"text": "It stayed constant", "is_correct": False},
                {"text": "It decreased", "is_correct": False},
            ],
        },
    ]
    (export_dir / "mcq.jsonl").write_text("".join(json.dumps(row) + "\n" for row in variants), encoding="utf-8")
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"export_id": "trial", "files": {"mcq": "exports/trial/mcq.jsonl"}, "file_sha256": {"mcq": "fixture-hash"}}), encoding="utf-8")
    renderer = tmp_path / "generation-v10.py"
    renderer.write_text("PROMPT_VERSION = 'arctic-qa-generation-v10'\n", encoding="utf-8")
    db_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript("""
        CREATE TABLE sources (source_id TEXT,stable_id TEXT,doi TEXT,title TEXT,year INTEGER,content_hash TEXT,metadata_json TEXT,inclusion_reason TEXT);
        CREATE TABLE candidates (item_id TEXT,source_id TEXT,candidate_json TEXT);
        CREATE TABLE validation_events (item_id TEXT,stage TEXT,label TEXT,reason_codes_json TEXT,details_json TEXT);
    """)
    connection.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)", ("source-1", "paper-1", "10.1/example", "Example paper", 2026, "source-hash", "{}", "selected"))
    candidate = {"question": "What changed?", "answer": {"text": "It increased", "claim_type": "observation", "evidence_quote": "The measured value increased.", "locator": {"page": 2}}}
    connection.execute("INSERT INTO candidates VALUES (?,?,?)", ("question-1", "source-1", json.dumps(candidate)))
    connection.commit()
    connection.close()

    manifest = export_publication_package(db_path, tmp_path / "package", seed="fixed", export_manifest=manifest_path, historical_renderers=[renderer])

    assert manifest["reviewer_item_count"] == 2
    reviewer = [json.loads(line) for line in (tmp_path / "package" / "reviewer-items.jsonl").read_text().splitlines()]
    benchmark = [json.loads(line) for line in (tmp_path / "package" / "benchmark-inputs.jsonl").read_text().splitlines()]
    scoring = [json.loads(line) for line in (tmp_path / "package" / "scoring-labels.jsonl").read_text().splitlines()]
    assert [row["item_id"] for row in reviewer] == ["mcq-present", "mcq-absent"]
    assert [option["text"] for option in reviewer[0]["options"]] == ["It increased", "It decreased"]
    assert reviewer[0]["options"][0]["is_correct"] is True
    assert "release_label" not in json.dumps(reviewer[0])
    assert reviewer[1]["reference_answer"]["text"] == "It increased"
    assert reviewer[1]["answer_evidence"]["excerpt"] == "The measured value increased."
    assert "is_correct" not in json.dumps(benchmark[0])
    assert scoring[0]["correct_option_id"] is not None
    assert scoring[1]["correct_option_id"] is None
    assert manifest["historical_prompt_templates"][0]["kind"] == "renderer"
    assert (tmp_path / "package" / "historical-prompt-bundle" / "generation-v10.py").read_text() == renderer.read_text()
