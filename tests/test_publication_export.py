from __future__ import annotations

import json
import sqlite3
import sys
import csv
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from arctic_qa.publication_export import (  # noqa: E402
    export_publication_package,
    refresh_live_publication_snapshot,
)
from arctic_qa.util import canonical_json, stable_id  # noqa: E402


def _live_candidate(item_id: str, question: str, *, prompt: str) -> dict:
    distractors = [
        {
            "text": f"Wrong {letter}",
            "evidence_quote": f"Evidence against {letter}",
            "locator": {"page": index},
            "generation_rationale": f"Rationale {letter}",
        }
        for index, letter in enumerate("ABC", start=2)
    ]
    current = prompt == "arctic-qa-generation-v23"
    agreement_contract = prompt in {
        "arctic-qa-generation-v19",
        "arctic-qa-generation-v20",
        "arctic-qa-generation-v23",
    }
    return {
        "schema_version": (
            "2.8.0"
            if current
            else "2.5.0"
            if prompt == "arctic-qa-generation-v20"
            else "2.4.0"
            if prompt == "arctic-qa-generation-v19"
            else "2.3.0"
        ),
        "item_id": item_id,
        "question": question,
        "question_context": "Annual mean.",
        "question_rationale": "The finding supports this question.",
        "answer": {
            "text": "Correct",
            "evidence_quote": "The value increased.",
            "locator": {"page": 1},
            "rationale": "The source gives the answer.",
            "selection_rationale": "This is a focused result.",
        },
        "distractors": distractors,
        "option_verdicts": [
            {
                "option_text": item["text"],
                "rationale": "The source contradicts this option.",
                "contradiction_established": True,
                "request_id": "private-request",
                "actual_cost_usd": "1.00",
            }
            for item in distractors
        ],
        "reconstruction": {
            "answer": "Correct",
            "reconstruction_rationale": "The question has one answer.",
        },
        "answer_verification": {
            "source_entailment_model_verified": True,
            "verification_rationale": "The evidence entails the answer.",
        },
        **(
            {
                "standalone_verification": {
                    "contract_version": (
                        "source-blind-scientific-referent-v5"
                        if current
                        else "source-blind-standalone-gate-v1"
                    ),
                    "pass": True,
                    "answer_leakage_absent": True,
                    "unresolved_phrases": [],
                    "missing_detail_types": [],
                    "reasons": [],
                    "review_rationale": "The displayed task is self-contained.",
                }
            }
            if agreement_contract
            else {}
        ),
        **(
            {
                "answer_agreement": {
                    "contract_version": "deterministic-first-answer-agreement-v1",
                    "method": "deterministic",
                    "confidence_category": "authoritative_deterministic",
                    "deterministic_match": True,
                    "agreement": True,
                    "judge": None,
                }
            }
            if agreement_contract
            else {}
        ),
        "provenance": {
            "prompt_version": prompt,
            "scope_contract_version": "selected-evidence-literal-scope-v4",
            "author_model": "gemini-current",
            "verifier_model": "gemini-current",
            "run_id": "private-run",
            **(
                {
                    "answer_agreement_contract_version": (
                        "deterministic-first-answer-agreement-v1"
                    ),
                }
                if agreement_contract
                else {}
            ),
            **(
                {
                    "standalone_verification_contract_version": (
                        "source-blind-scientific-referent-v5"
                    )
                }
                if current
                else {}
            ),
        },
    }


def _insert_live_candidate(
    connection: sqlite3.Connection,
    candidate: dict,
    *,
    family: str,
    status: str = "machine_accepted_unverified",
    updated_at: str = "2026-09-14T00:00:00Z",
    bind_validation: bool = True,
    validation_label: str = "machine_accepted_unverified",
    validation_distractors: list[dict] | None = None,
    validation_labels: dict | None = None,
) -> None:
    stored = canonical_json(candidate)
    connection.execute(
        "INSERT INTO candidates VALUES (?,?,?,?,?,?,?)",
        (
            candidate["item_id"],
            family,
            "source-1",
            status,
            stored,
            updated_at,
            "private-run",
        ),
    )
    details = {
        "candidate_hash": (
            stable_id("candidate-payload", stored) if bind_validation else "wrong"
        ),
        "labels": validation_labels
        or {
            "mcq_eligible": True,
            "machine_accepted_unverified": True,
            "schema_valid": True,
            "scope_complete": True,
            "reconstruction_agreement": True,
            "source_entailment_model_verified": True,
            "alternative_answer_search_passed": True,
            "model_verified": True,
        },
        "distractors": validation_distractors
        if validation_distractors is not None
        else [
            {
                "text": item["text"],
                "accepted": True,
                "deterministic": True,
                "model_verified": True,
            }
            for item in candidate["distractors"]
        ],
        "run_id": "private-run",
        "updated_at_utc": "2026-09-14T00:00:00Z",
        **(
            {"answer_agreement": candidate["answer_agreement"]}
            if candidate.get("answer_agreement")
            else {}
        ),
    }
    connection.execute(
        "INSERT INTO validation_events VALUES (?,?,?,?,?,?)",
        (
            candidate["item_id"],
            "automated_acceptance",
            validation_label,
            "[]",
            canonical_json(details),
            updated_at,
        ),
    )


def test_live_snapshot_updates_atomically_and_excludes_stale_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE sources (source_id TEXT,doi TEXT,title TEXT);
        CREATE TABLE candidates (
            item_id TEXT,paper_family_id TEXT,source_id TEXT,status TEXT,
            candidate_json TEXT,updated_at TEXT,run_id TEXT
        );
        CREATE TABLE validation_events (
            item_id TEXT,stage TEXT,label TEXT,reason_codes_json TEXT,
            details_json TEXT,created_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO sources VALUES (?,?,?)",
        ("source-1", "10.1/current", "Current paper"),
    )
    _insert_live_candidate(
        connection,
        _live_candidate(
            "old-contract", "Old question?", prompt="arctic-qa-generation-v15"
        ),
        family="old-family",
    )
    _insert_live_candidate(
        connection,
        _live_candidate(
            "aqa-0827ca49c94b12db254e",
            "What was the reported percentage increase?",
            prompt="arctic-qa-generation-v19",
        ),
        family="audited-old-family",
    )
    _insert_live_candidate(
        connection,
        _live_candidate(
            "rejected", "Rejected question?", prompt="arctic-qa-generation-v23"
        ),
        family="rejected-family",
        status="rejected",
    )
    _insert_live_candidate(
        connection,
        _live_candidate(
            "unbound", "Unbound question?", prompt="arctic-qa-generation-v23"
        ),
        family="unbound-family",
        bind_validation=False,
    )
    _insert_live_candidate(
        connection,
        _live_candidate(
            "current-old",
            "Earlier current question?",
            prompt="arctic-qa-generation-v23",
        ),
        family="current-family",
        updated_at="2026-09-14T00:00:00Z",
    )
    _insert_live_candidate(
        connection,
        _live_candidate(
            "current-new", "Newest current question?", prompt="arctic-qa-generation-v23"
        ),
        family="current-family",
        updated_at="2026-09-14T00:01:00Z",
    )
    connection.commit()
    selection = REPO / "config/live-dataset-current-contract-v1.json"
    output = tmp_path / "live"

    first = refresh_live_publication_snapshot(
        database,
        output,
        selection_file=selection,
        seed="fixed",
        prompt_files=[REPO / "config/gemini-eligibility-prompt-v6.txt"],
    )
    pointer_before = (output / "current.json").read_bytes()
    repeated = refresh_live_publication_snapshot(
        database,
        output,
        selection_file=selection,
        seed="fixed",
        prompt_files=[REPO / "config/gemini-eligibility-prompt-v6.txt"],
    )

    assert repeated == first
    assert (output / "current.json").read_bytes() == pointer_before
    assert len(list((output / "snapshots").iterdir())) == 1
    assert first["item_count"] == 1
    assert first["excluded_counts"] == {
        "incomplete_or_rejected": 1,
        "superseded_contract": 2,
        "invalid_or_unbound_validation": 1,
        "duplicate_current_family": 1,
    }
    snapshot = output / "snapshots" / first["snapshot_id"]
    benchmark = json.loads((snapshot / "accepted-benchmark.jsonl").read_text())
    reviewer = json.loads((snapshot / "accepted-reviewer.jsonl").read_text())
    assert benchmark["item_id"] == reviewer["item_id"]
    assert reviewer["question"] == "Newest current question?"
    assert reviewer["item_id"] != "aqa-0827ca49c94b12db254e"
    assert reviewer["paper"] == {
        "doi": "10.1/current",
        "title": "Current paper",
    }
    assert reviewer["model_trace"]
    assert reviewer["stage_results"]["answer_agreement"]["method"] == "deterministic"
    assert (
        reviewer["stage_results"]["answer_agreement"]["confidence_category"]
        == "authoritative_deterministic"
    )
    assert (
        reviewer["validation"][0]["details"]["answer_agreement"]["confidence_category"]
        == "authoritative_deterministic"
    )
    assert reviewer["answer_evidence"]["excerpt"] == "The value increased."
    assert all(option.get("evidence") for option in reviewer["options"])
    assert "candidate_hash" in reviewer["validation"][0]["details"]
    encoded_reviewer = canonical_json(reviewer)
    for excluded in (
        "private-run",
        "private-request",
        "actual_cost_usd",
        "updated_at_utc",
        "machine_accepted_unverified",
    ):
        assert excluded not in encoded_reviewer
    assert "is_correct" not in canonical_json(benchmark)
    assert len(list((output / "prompts").iterdir())) == 1

    _insert_live_candidate(
        connection,
        _live_candidate(
            "second-family", "Second question?", prompt="arctic-qa-generation-v23"
        ),
        family="second-family",
        updated_at="2026-09-14T00:02:00Z",
    )
    connection.commit()
    second = refresh_live_publication_snapshot(
        database,
        output,
        selection_file=selection,
        seed="fixed",
        prompt_files=[REPO / "config/gemini-eligibility-prompt-v6.txt"],
    )
    connection.close()

    assert second["item_count"] == 2
    assert second["snapshot_id"] != first["snapshot_id"]
    assert len(list((output / "snapshots").iterdir())) == 2


def test_live_preview_includes_retained_model_verified_distractors_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE sources (source_id TEXT,doi TEXT,title TEXT);
        CREATE TABLE candidates (
            item_id TEXT,paper_family_id TEXT,source_id TEXT,status TEXT,
            candidate_json TEXT,updated_at TEXT,run_id TEXT
        );
        CREATE TABLE validation_events (
            item_id TEXT,stage TEXT,label TEXT,reason_codes_json TEXT,
            details_json TEXT,created_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO sources VALUES (?,?,?)",
        ("source-1", "10.1/current", "Current paper"),
    )
    preview = _live_candidate(
        "preview", "Preview question?", prompt="arctic-qa-generation-v23"
    )
    preview_distractors = [
        {
            "text": item["text"],
            "accepted": True,
            "deterministic": False,
            "model_verified": True,
            "label": "model-verified",
            "reasons": ["residual_model_error_possible"],
        }
        for item in preview["distractors"]
    ]
    _insert_live_candidate(
        connection,
        preview,
        family="preview-family",
        validation_distractors=preview_distractors,
    )
    rejected = _live_candidate(
        "rejected", "Rejected question?", prompt="arctic-qa-generation-v23"
    )
    _insert_live_candidate(
        connection,
        rejected,
        family="rejected-family",
        status="rejected",
        validation_label="rejected",
        validation_distractors=[],
        validation_labels={"mcq_eligible": False, "rejected": True},
    )
    missing_model_verification = _live_candidate(
        "missing-model-verification",
        "Incomplete question?",
        prompt="arctic-qa-generation-v23",
    )
    _insert_live_candidate(
        connection,
        missing_model_verification,
        family="incomplete-family",
        validation_distractors=[
            {
                "text": item["text"],
                "accepted": True,
                "deterministic": False,
                "model_verified": False,
            }
            for item in missing_model_verification["distractors"]
        ],
    )
    connection.commit()

    manifest = refresh_live_publication_snapshot(
        database,
        tmp_path / "live",
        selection_file=REPO / "config/live-dataset-current-contract-v1.json",
        seed="fixed",
    )
    connection.close()

    assert manifest["item_count"] == 1
    assert manifest["excluded_counts"] == {
        "incomplete_or_rejected": 1,
        "superseded_contract": 0,
        "invalid_or_unbound_validation": 1,
        "duplicate_current_family": 0,
    }
    assert manifest["preview"] == {
        "label": "Machine-validated preview",
        "notice": (
            "Some accepted distractors are model-verified but non-deterministic. "
            "The reviewer rows retain their validation and uncertainty details."
        ),
    }
    snapshot = tmp_path / "live" / "snapshots" / manifest["snapshot_id"]
    benchmark = json.loads((snapshot / "accepted-benchmark.jsonl").read_text())
    reviewer = json.loads((snapshot / "accepted-reviewer.jsonl").read_text())
    assert benchmark["item_id"] == reviewer["item_id"]
    assert "is_correct" not in canonical_json(benchmark)
    validation = reviewer["validation"][0]
    assert validation["details"]["checks"]["model_verified"] is True
    assert all(
        item["deterministic"] is False
        and item["reasons"] == ["residual_model_error_possible"]
        for item in validation["details"]["distractors"]
    )


def test_reviewer_preserves_verdict_rationale_while_benchmark_hides_labels(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript("""
        CREATE TABLE sources (source_id TEXT,stable_id TEXT,doi TEXT,title TEXT,year INTEGER,content_hash TEXT,paper_family_id TEXT,inclusion_reason TEXT,metadata_json TEXT);
        CREATE TABLE candidates (item_id TEXT,run_id TEXT,source_id TEXT,status TEXT,candidate_json TEXT);
        CREATE TABLE validation_events (item_id TEXT,label TEXT,reason_codes_json TEXT,details_json TEXT,created_at TEXT);
        CREATE TABLE calls (call_id TEXT,role TEXT,request_id TEXT,returned_model TEXT,response_json TEXT,attempt INTEGER);
    """)
    source = (
        "source-1",
        "paper-1",
        "10.1/example",
        "Example paper",
        2026,
        "hash-1",
        "family-1",
        "selected",
        json.dumps({"selection": {"position": 4, "reason_codes": ["arctic"]}}),
    )
    candidate = {
        "item_id": "qa-1",
        "question": "Which value?",
        "question_context": "The measurement describes the yearly mean.",
        "answer": {
            "text": "Correct",
            "evidence_quote": "short evidence",
            "locator": {"page": 2},
        },
        "distractors": [
            {"text": text, "type": "wrong", "evidence_quote": "evidence"}
            for text in ("Wrong A", "Wrong B", "Wrong C")
        ],
        "option_verdicts": [
            {
                "option_text": text,
                "rationale": f"why {text} is wrong",
                "provenance": {"request_id": f"request-{index}"},
            }
            for index, text in enumerate(("Wrong A", "Wrong B", "Wrong C"))
        ],
        "provenance": {"run_id": "trial-r1", "verification_calls": {}},
    }
    details = {
        "distractors": [
            {"text": text, "accepted": True, "deterministic": True}
            for text in ("Wrong A", "Wrong B", "Wrong C")
        ]
    }
    connection.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?)", source)
    connection.execute(
        "INSERT INTO candidates VALUES (?,?,?,?,?)",
        (
            "qa-1",
            "trial-r1",
            "source-1",
            "machine_accepted_unverified",
            json.dumps(candidate),
        ),
    )
    connection.execute(
        "INSERT INTO validation_events VALUES (?,?,?,?,?)",
        ("qa-1", "machine_accepted_unverified", "[]", json.dumps(details), "now"),
    )
    connection.commit()
    connection.close()
    manifest = export_publication_package(
        db_path, tmp_path / "package", run_id="trial-r1", seed="fixed"
    )
    assert manifest["reviewer_item_count"] == 1
    reviewer = json.loads((tmp_path / "package" / "reviewer-items.jsonl").read_text())
    benchmark = json.loads(
        (tmp_path / "package" / "benchmark-inputs.jsonl").read_text()
    )
    assert reviewer["paper"]["doi"] == "10.1/example"
    assert reviewer["question_context"] == "The measurement describes the yearly mean."
    assert benchmark["question_context"] == reviewer["question_context"]
    assert any(option.get("verdict") for option in reviewer["options"])
    assert "is_correct" not in benchmark["options"][0]
    assert (
        "rationale" not in (tmp_path / "package" / "benchmark-inputs.jsonl").read_text()
    )
    with (tmp_path / "package" / "reviewer-items.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        csv_row = next(csv.DictReader(handle))
    assert csv_row["reference_answer"] == "Correct"
    assert csv_row["option_a_verdict"] or csv_row["option_b_verdict"]
    assert csv_row["question_context"] == reviewer["question_context"]
    with (tmp_path / "package" / "benchmark-inputs.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
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
            "answer_evidence": {
                "quote": "The measured value increased.",
                "locator": {"page": 2},
            },
            "options": [
                {"text": "It increased", "is_correct": True},
                {
                    "text": "It decreased",
                    "is_correct": False,
                    "verification_label": "model-verified",
                },
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
    (export_dir / "mcq.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in variants), encoding="utf-8"
    )
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "export_id": "trial",
                "files": {"mcq": "exports/trial/mcq.jsonl"},
                "file_sha256": {"mcq": "fixture-hash"},
            }
        ),
        encoding="utf-8",
    )
    renderer = tmp_path / "generation-v10.py"
    renderer.write_text(
        "PROMPT_VERSION = 'arctic-qa-generation-v10'\n", encoding="utf-8"
    )
    db_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript("""
        CREATE TABLE sources (source_id TEXT,stable_id TEXT,doi TEXT,title TEXT,year INTEGER,content_hash TEXT,metadata_json TEXT,inclusion_reason TEXT);
        CREATE TABLE candidates (item_id TEXT,source_id TEXT,candidate_json TEXT);
        CREATE TABLE validation_events (item_id TEXT,stage TEXT,label TEXT,reason_codes_json TEXT,details_json TEXT);
    """)
    connection.execute(
        "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
        (
            "source-1",
            "paper-1",
            "10.1/example",
            "Example paper",
            2026,
            "source-hash",
            "{}",
            "selected",
        ),
    )
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
        "distractors": [
            {
                "text": "It decreased",
                "generation_rationale": "This reverses the reported direction.",
            }
        ],
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
        "decision_evidence": [
            {
                "evidence_quote": "The measured value increased.",
                "locator": {"page": 2},
                "source_span_id": "combined-span-1",
                "source_span_ids": ["answer-span-1", "reconstruction-span-1"],
                "roles": ["answer", "reconstruction"],
                "role_evidence": [
                    {
                        "role": "answer",
                        "evidence_quote": "The measured value increased.",
                        "locator": {"page": 2},
                        "source_span_id": "answer-span-1",
                    },
                    {
                        "role": "reconstruction",
                        "evidence_quote": "The measured value increased.",
                        "locator": {"page": 2},
                        "source_span_id": "reconstruction-span-1",
                    },
                ],
            }
        ],
    }
    connection.execute(
        "INSERT INTO candidates VALUES (?,?,?)",
        ("question-1", "source-1", json.dumps(candidate)),
    )
    decoy = {
        "question": "What changed?",
        "answer": {
            "text": "It decreased",
            "evidence_quote": "Wrong attempt",
            "locator": {"page": 3},
        },
    }
    connection.execute(
        "INSERT INTO candidates VALUES (?,?,?)",
        ("question-9", "source-1", json.dumps(decoy)),
    )
    connection.execute(
        "INSERT INTO validation_events VALUES (?,?,?,?,?)",
        (
            "question-1",
            "automated_acceptance",
            "machine_accepted_unverified",
            '["model_only_distractor_verification"]',
            '{"labels": {"machine_accepted_unverified": true, "schema_valid": true}}',
        ),
    )
    connection.commit()
    connection.close()

    manifest = export_publication_package(
        db_path,
        tmp_path / "package",
        seed="fixed",
        export_manifest=manifest_path,
        historical_renderers=[renderer],
    )

    assert manifest["reviewer_item_count"] == 2
    reviewer = [
        json.loads(line)
        for line in (tmp_path / "package" / "reviewer-items.jsonl")
        .read_text()
        .splitlines()
    ]
    benchmark = [
        json.loads(line)
        for line in (tmp_path / "package" / "benchmark-inputs.jsonl")
        .read_text()
        .splitlines()
    ]
    scoring = [
        json.loads(line)
        for line in (tmp_path / "package" / "scoring-labels.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [row["item_id"] for row in reviewer] == ["mcq-present", "mcq-absent"]
    assert [option["text"] for option in reviewer[0]["options"]] == [
        "It increased",
        "It decreased",
    ]
    assert reviewer[0]["options"][0]["is_correct"] is True
    assert "release_label" not in json.dumps(reviewer[0])
    assert reviewer[1]["reference_answer"]["text"] == "It increased"
    assert reviewer[1]["answer_evidence"]["excerpt"] == "The measured value increased."
    assert reviewer[0]["reference_answer"]["numeric_rule"]["unit"] == "percent"
    assert (
        reviewer[0]["options"][1]["generation_rationale"]
        == "This reverses the reported direction."
    )
    assert reviewer[0]["rationales"] == {
        "question": "The selected finding states the change.",
        "answer_selection": "This answer uses the stated increase.",
        "answer_generation": "The evidence supports the answer.",
        "reconstruction": "The source supports one answer.",
        "answer_verification": "The answer matches the source.",
    }
    assert (
        reviewer[0]["stage_results"]["reconstruction"]["ambiguity_label"]
        == "one_answer"
    )
    assert reviewer[0]["stage_results"]["reconstruction"]["evidence"]["locator"] == {
        "page": 2
    }
    assert (
        reviewer[0]["stage_results"]["answer_verification"][
            "source_entailment_model_verified"
        ]
        is True
    )
    assert reviewer[0]["decision_evidence"][0]["roles"] == [
        "answer",
        "reconstruction",
    ]
    assert reviewer[0]["decision_evidence"][0]["role_evidence"][0]["role"] == ("answer")
    assert reviewer[0]["validation"] == [
        {
            "stage": "automated_acceptance",
            "reason_codes": ["model_only_distractor_verification"],
            "checks": {"schema_valid": True},
        }
    ]
    assert "machine_accepted_unverified" not in json.dumps(reviewer[0])
    assert "is_correct" not in json.dumps(benchmark[0])
    assert "rationale" not in json.dumps(benchmark[0])
    assert "decision_evidence" not in benchmark[0]
    assert scoring[0]["correct_option_id"] is not None
    assert scoring[1]["correct_option_id"] is None
    with (tmp_path / "package" / "reviewer-items.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        csv_row = next(csv.DictReader(handle))
    assert csv_row["doi"] == "10.1/example"
    assert csv_row["reference_answer"] == "It increased"
    assert (
        csv_row["option_b_generation_rationale"]
        == "This reverses the reported direction."
    )
    assert csv_row["reconstruction_rationale"] == "The source supports one answer."
    assert json.loads(csv_row["decision_evidence_json"])[0]["roles"] == [
        "answer",
        "reconstruction",
    ]
    assert (
        json.loads(csv_row["stage_results_json"])["answer_verification"][
            "source_entailment_model_verified"
        ]
        is True
    )
    assert manifest["historical_prompt_templates"][0]["kind"] == "renderer"
    assert (
        tmp_path / "package" / "historical-prompt-bundle" / "generation-v10.py"
    ).read_text() == renderer.read_text()


def test_manifest_package_exports_question_context_without_benchmark_leakage(
    tmp_path: Path,
) -> None:
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
    (export_dir / "mcq.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in variants), encoding="utf-8"
    )
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({"files": {"mcq": "exports/trial/mcq.jsonl"}}), encoding="utf-8"
    )
    db_path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript("""
        CREATE TABLE sources (source_id TEXT,stable_id TEXT,doi TEXT,title TEXT,year INTEGER,content_hash TEXT,metadata_json TEXT,inclusion_reason TEXT);
        CREATE TABLE candidates (item_id TEXT,source_id TEXT,candidate_json TEXT);
        CREATE TABLE validation_events (item_id TEXT,stage TEXT,label TEXT,reason_codes_json TEXT,details_json TEXT);
    """)
    connection.execute(
        "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
        (
            "source-1",
            "paper-1",
            "10.1/example",
            "Example paper",
            2026,
            "source-hash",
            "{}",
            "selected",
        ),
    )
    candidates = [
        (
            "question-context",
            {
                "question": "What changed?",
                "question_context": present_context,
                "answer": {"text": "Correct context match"},
            },
        ),
        (
            "question-decoy",
            {
                "question": "What changed?",
                "question_context": "The measurement is for sea ice concentration.",
                "answer": {"text": "Wrong context match"},
            },
        ),
        (
            "question-empty",
            {
                "question": "Which season?",
                "question_context": "",
                "answer": {"text": "Winter"},
            },
        ),
        (
            "question-legacy",
            {"question": "Which instrument?", "answer": {"text": "Satellite"}},
        ),
    ]
    connection.executemany(
        "INSERT INTO candidates VALUES (?,?,?)",
        [
            (item_id, "source-1", json.dumps(candidate))
            for item_id, candidate in candidates
        ],
    )
    connection.commit()
    connection.close()

    export_publication_package(
        db_path, tmp_path / "package", seed="fixed", export_manifest=manifest_path
    )

    reviewer = [
        json.loads(line)
        for line in (tmp_path / "package" / "reviewer-items.jsonl")
        .read_text()
        .splitlines()
    ]
    benchmark = [
        json.loads(line)
        for line in (tmp_path / "package" / "benchmark-inputs.jsonl")
        .read_text()
        .splitlines()
    ]
    with (tmp_path / "package" / "reviewer-items.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        reviewer_csv = list(csv.DictReader(handle))
    with (tmp_path / "package" / "benchmark-inputs.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        benchmark_reader = csv.DictReader(handle)
        benchmark_csv = list(benchmark_reader)
        assert (
            benchmark_reader.fieldnames[
                benchmark_reader.fieldnames.index("question") + 1
            ]
            == "question_context"
        )

    assert [row["question_context"] for row in reviewer] == [present_context, "", ""]
    assert [row["question_context"] for row in benchmark] == [present_context, "", ""]
    assert [row["question_context"] for row in reviewer_csv] == [
        present_context,
        "",
        "",
    ]
    assert [row["question_context"] for row in benchmark_csv] == [
        present_context,
        "",
        "",
    ]
    assert reviewer[0]["reference_answer"]["text"] == "Correct context match"
    assert set(benchmark[0]) == {
        "item_id",
        "question_id",
        "variant_id",
        "question",
        "question_context",
        "options",
    }
    assert not {
        "reference_answer",
        "answer_evidence",
        "rationales",
        "validation",
        "provenance",
        "selection",
    } & set(benchmark[0])
