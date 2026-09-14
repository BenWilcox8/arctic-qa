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
    candidate = {"item_id": "qa-1", "question": "Which value?", "question_context": "The measurement describes the yearly mean.", "answer": {"text": "Correct", "evidence_quote": "short evidence", "locator": {"page": 2}}, "distractors": [{"text": text, "type": "wrong", "evidence_quote": "evidence"} for text in ("Wrong A", "Wrong B", "Wrong C")], "option_verdicts": [{"option_text": text, "rationale": f"why {text} is wrong", "provenance": {"request_id": f"request-{index}"}} for index, text in enumerate(("Wrong A", "Wrong B", "Wrong C"))], "provenance": {"run_id": "trial-r1", "verification_calls": {}}}
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
    assert reviewer["question_context"] == "The measurement describes the yearly mean."
    assert benchmark["question_context"] == reviewer["question_context"]
    assert any(option.get("verdict") for option in reviewer["options"])
    assert "is_correct" not in benchmark["options"][0]
    assert "rationale" not in (tmp_path / "package" / "benchmark-inputs.jsonl").read_text()
    with (tmp_path / "package" / "reviewer-items.csv").open(newline="", encoding="utf-8") as handle:
        csv_row = next(csv.DictReader(handle))
    assert csv_row["reference_answer"] == "Correct"
    assert csv_row["option_a_verdict"] or csv_row["option_b_verdict"]
    assert csv_row["question_context"] == reviewer["question_context"]
    with (tmp_path / "package" / "benchmark-inputs.csv").open(newline="", encoding="utf-8") as handle:
        benchmark_csv = next(csv.DictReader(handle))
    assert benchmark_csv["question_context"] == benchmark["question_context"]


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
    candidate = {
        "question": "What changed?",
        "question_rationale": "The selected finding states the change.",
        "answer": {
            "text": "It increased",
            "claim_type": "observation",
            "numeric_rule": {"unit": "percent", "canonical_value": "4"},
            "selection_rationale": "This answer uses the stated increase.",
            "rationale": "The evidence supports the answer.",
            "evidence_quote": "The measured value increased.",
            "locator": {"page": 2},
        },
        "distractors": [{"text": "It decreased", "generation_rationale": "This reverses the reported direction."}],
        "reconstruction": {
            "alternatives": [],
            "ambiguity_label": "one_answer",
            "scope": {"geography": "north of 85N"},
            "evidence_quote": "The measured value increased.",
            "locator": {"page": 2},
            "reconstruction_rationale": "The source supports one answer.",
        },
        "answer_verification": {
            "alternative_answer_search_passed": True,
            "source_entailment_model_verified": True,
            "evidence_quote": "The measured value increased.",
            "locator": {"page": 2},
            "verification_rationale": "The answer matches the source.",
        },
    }
    connection.execute("INSERT INTO candidates VALUES (?,?,?)", ("question-1", "source-1", json.dumps(candidate)))
    decoy = {"question": "What changed?", "answer": {"text": "It decreased", "evidence_quote": "Wrong attempt", "locator": {"page": 3}}}
    connection.execute("INSERT INTO candidates VALUES (?,?,?)", ("question-9", "source-1", json.dumps(decoy)))
    connection.execute("INSERT INTO validation_events VALUES (?,?,?,?,?)", ("question-1", "automated_acceptance", "machine_accepted_unverified", "[\"model_only_distractor_verification\"]", "{\"labels\": {\"machine_accepted_unverified\": true, \"schema_valid\": true}}"))
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
    assert reviewer[0]["reference_answer"]["numeric_rule"]["unit"] == "percent"
    assert reviewer[0]["options"][1]["generation_rationale"] == "This reverses the reported direction."
    assert reviewer[0]["rationales"] == {
        "question": "The selected finding states the change.",
        "answer_selection": "This answer uses the stated increase.",
        "answer_generation": "The evidence supports the answer.",
        "reconstruction": "The source supports one answer.",
        "answer_verification": "The answer matches the source.",
    }
    assert reviewer[0]["stage_results"]["reconstruction"]["ambiguity_label"] == "one_answer"
    assert reviewer[0]["stage_results"]["reconstruction"]["evidence"]["locator"] == {"page": 2}
    assert reviewer[0]["stage_results"]["answer_verification"]["source_entailment_model_verified"] is True
    assert reviewer[0]["validation"] == [{"stage": "automated_acceptance", "reason_codes": ["model_only_distractor_verification"], "checks": {"schema_valid": True}}]
    assert "machine_accepted_unverified" not in json.dumps(reviewer[0])
    assert "is_correct" not in json.dumps(benchmark[0])
    assert "rationale" not in json.dumps(benchmark[0])
    assert scoring[0]["correct_option_id"] is not None
    assert scoring[1]["correct_option_id"] is None
    with (tmp_path / "package" / "reviewer-items.csv").open(encoding="utf-8", newline="") as handle:
        csv_row = next(csv.DictReader(handle))
    assert csv_row["doi"] == "10.1/example"
    assert csv_row["reference_answer"] == "It increased"
    assert csv_row["option_b_generation_rationale"] == "This reverses the reported direction."
    assert csv_row["reconstruction_rationale"] == "The source supports one answer."
    assert json.loads(csv_row["stage_results_json"])["answer_verification"]["source_entailment_model_verified"] is True
    assert manifest["historical_prompt_templates"][0]["kind"] == "renderer"
    assert (tmp_path / "package" / "historical-prompt-bundle" / "generation-v10.py").read_text() == renderer.read_text()


def test_manifest_package_exports_question_context_without_benchmark_leakage(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports" / "trial"
    export_dir.mkdir(parents=True)
    present_context = "The measurement is for sea ice extent."
    variants = [
        {
            "item_id": "mcq-context",
            "paired_item_id": "missing-candidate-id",
            "task_type": "answer_present_mcq",
            "question": "What changed?",
            "question_context": present_context,
            "source": {"source_id": "source-1"},
            "options": [{"text": "It increased", "is_correct": True}],
        },
        {
            "item_id": "mcq-empty",
            "paired_item_id": "question-empty",
            "task_type": "answer_present_mcq",
            "question": "Which season?",
            "question_context": "",
            "source": {"source_id": "source-1"},
            "options": [{"text": "Winter", "is_correct": True}],
        },
        {
            "item_id": "mcq-legacy",
            "paired_item_id": "question-legacy",
            "task_type": "answer_present_mcq",
            "question": "Which instrument?",
            "source": {"source_id": "source-1"},
            "options": [{"text": "Satellite", "is_correct": True}],
        },
    ]
    (export_dir / "mcq.jsonl").write_text("".join(json.dumps(row) + "\n" for row in variants), encoding="utf-8")
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"files": {"mcq": "exports/trial/mcq.jsonl"}}), encoding="utf-8")
    db_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript("""
        CREATE TABLE sources (source_id TEXT,stable_id TEXT,doi TEXT,title TEXT,year INTEGER,content_hash TEXT,metadata_json TEXT,inclusion_reason TEXT);
        CREATE TABLE candidates (item_id TEXT,source_id TEXT,candidate_json TEXT);
        CREATE TABLE validation_events (item_id TEXT,stage TEXT,label TEXT,reason_codes_json TEXT,details_json TEXT);
    """)
    connection.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)", ("source-1", "paper-1", "10.1/example", "Example paper", 2026, "source-hash", "{}", "selected"))
    candidates = [
        ("question-context", {"question": "What changed?", "question_context": present_context, "answer": {"text": "Correct context match"}}),
        ("question-decoy", {"question": "What changed?", "question_context": "The measurement is for sea ice concentration.", "answer": {"text": "Wrong context match"}}),
        ("question-empty", {"question": "Which season?", "question_context": "", "answer": {"text": "Winter"}}),
        ("question-legacy", {"question": "Which instrument?", "answer": {"text": "Satellite"}}),
    ]
    connection.executemany("INSERT INTO candidates VALUES (?,?,?)", [(item_id, "source-1", json.dumps(candidate)) for item_id, candidate in candidates])
    connection.commit()
    connection.close()

    export_publication_package(db_path, tmp_path / "package", seed="fixed", export_manifest=manifest_path)

    reviewer = [json.loads(line) for line in (tmp_path / "package" / "reviewer-items.jsonl").read_text().splitlines()]
    benchmark = [json.loads(line) for line in (tmp_path / "package" / "benchmark-inputs.jsonl").read_text().splitlines()]
    with (tmp_path / "package" / "reviewer-items.csv").open(encoding="utf-8", newline="") as handle:
        reviewer_csv = list(csv.DictReader(handle))
    with (tmp_path / "package" / "benchmark-inputs.csv").open(encoding="utf-8", newline="") as handle:
        benchmark_reader = csv.DictReader(handle)
        benchmark_csv = list(benchmark_reader)
        assert benchmark_reader.fieldnames[benchmark_reader.fieldnames.index("question") + 1] == "question_context"

    assert [row["question_context"] for row in reviewer] == [present_context, "", ""]
    assert [row["question_context"] for row in benchmark] == [present_context, "", ""]
    assert [row["question_context"] for row in reviewer_csv] == [present_context, "", ""]
    assert [row["question_context"] for row in benchmark_csv] == [present_context, "", ""]
    assert reviewer[0]["reference_answer"]["text"] == "Correct context match"
    assert set(benchmark[0]) == {"item_id", "question_id", "variant_id", "question", "question_context", "options"}
    assert not {"reference_answer", "answer_evidence", "rationales", "validation", "provenance", "selection"} & set(benchmark[0])
