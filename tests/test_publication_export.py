from __future__ import annotations

import json
import sqlite3
import sys
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
