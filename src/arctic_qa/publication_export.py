"""Build a reviewer-only publication package from a read-only Arctic QA state DB."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .util import canonical_json, stable_id


SCHEMA_VERSION = "arctic-qa-publication-review-v1"


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _latest_validation(connection: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT label,reason_codes_json,details_json,created_at FROM validation_events "
        "WHERE item_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    if not row:
        return {"available": False}
    return {
        "available": True,
        "label": row["label"],
        "reason_codes": _json(row["reason_codes_json"], []),
        "details": _json(row["details_json"], {}),
    }


def _accepted_options(candidate: dict[str, Any], validation: dict[str, Any]) -> list[dict[str, Any]]:
    accepted = {
        row.get("text")
        for row in validation.get("details", {}).get("distractors", [])
        if row.get("accepted") is True and row.get("deterministic") is True
    }
    verdicts = {row.get("option_text"): row for row in candidate.get("option_verdicts") or []}
    return [
        {"text": row.get("text"), "is_correct": False, "distractor": row, "verdict": verdicts.get(row.get("text"))}
        for row in candidate.get("distractors") or []
        if row.get("text") in accepted
    ]


def _source_selection(source: sqlite3.Row) -> dict[str, Any] | None:
    metadata = _json(source["metadata_json"], {})
    selection = metadata.get("selection") if isinstance(metadata, dict) else None
    return selection if isinstance(selection, dict) else None


def _receipt_trace(connection: sqlite3.Connection, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    requests = []
    for value in (candidate.get("provenance", {}).get("verification_calls", {}) or {}).values():
        if isinstance(value, dict) and value.get("request_id"):
            requests.append(value["request_id"])
    for value in candidate.get("option_verdicts") or []:
        provenance = value.get("provenance") or {}
        if provenance.get("request_id"):
            requests.append(provenance["request_id"])
    result = []
    for request_id in sorted(set(requests)):
        row = connection.execute("SELECT call_id,role,request_id,returned_model,response_json FROM calls WHERE request_id=? ORDER BY attempt DESC LIMIT 1", (request_id,)).fetchone()
        if row and row["response_json"]:
            result.append({"call_id": row["call_id"], "role": row["role"], "request_id": row["request_id"], "returned_model": row["returned_model"], "response_sha256": hashlib.sha256(row["response_json"].encode()).hexdigest(), "response": _json(row["response_json"], None)})
    return result


def _row(connection: sqlite3.Connection, candidate: dict[str, Any], source: sqlite3.Row, validation: dict[str, Any], seed: str) -> dict[str, Any]:
    accepted = _accepted_options(candidate, validation)
    if len(accepted) < 3:
        raise ValueError("a publication row requires three accepted deterministic distractors")
    selected = accepted[:3]
    options = [{"text": candidate["answer"]["text"], "is_correct": True, "distractor": None, "verdict": None}, *selected]
    options = sorted(options, key=lambda option: stable_id("publication-option", seed, candidate["item_id"], option["text"]))
    review_options = []
    for position, option in enumerate(options, start=1):
        review_options.append(
            {
                "option_id": stable_id("publication-option-id", candidate["item_id"], option["text"]),
                "position": position,
                "text": option["text"],
                "is_correct": option["is_correct"],
                "distractor": option["distractor"],
                "verdict": option["verdict"],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "item_id": stable_id("publication-item", candidate["item_id"], seed),
        "question_id": candidate["item_id"],
        "variant_id": "answer_present_mcq",
        "paper": {
            "source_id": source["source_id"], "paper_id": source["stable_id"], "doi": source["doi"],
            "title": source["title"], "year": source["year"], "content_hash": source["content_hash"],
        },
        "selection": {"source_selection": _source_selection(source), "inclusion_reason": source["inclusion_reason"]},
        "question": candidate["question"],
        "question_rationale": candidate.get("question_rationale"),
        "reference_answer": candidate["answer"],
        "rationales": {
            "answer_selection": candidate.get("answer", {}).get("selection_rationale"),
            "answer_generation": candidate.get("answer", {}).get("rationale"),
            "reconstruction": candidate.get("reconstruction", {}).get("reconstruction_rationale"),
            "answer_verification": candidate.get("answer_verification", {}).get("verification_rationale"),
        },
        "options": review_options,
        "validation": validation,
        "provenance": {key: value for key, value in (candidate.get("provenance") or {}).items() if key != "run_id"},
        "receipt_trace": _receipt_trace(connection, candidate),
        "rationale_availability": {
            "question": bool(candidate.get("question_rationale")),
            "answer_selection": bool(candidate.get("answer", {}).get("selection_rationale")),
            "answer_generation": bool(candidate.get("answer", {}).get("rationale")),
            "reconstruction": bool(candidate.get("reconstruction", {}).get("reconstruction_rationale")),
            "answer_verification": bool(candidate.get("answer_verification", {}).get("verification_rationale")),
            "distractor_generation": any(option["distractor"] and option["distractor"].get("generation_rationale") for option in review_options),
            "option_verdict": any(option["verdict"] and option["verdict"].get("rationale") for option in review_options),
            "note": "Null rationale fields mean that this input did not retain that rationale. They do not mean that a rationale passed.",
        },
    }


def export_publication_package(state_db: Path, output_dir: Path, *, run_id: str, seed: str, prompt_templates: list[Path] | None = None) -> dict[str, Any]:
    """Read a state database and write reviewer JSONL, CSV, and a hashed manifest."""
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT c.*,s.* FROM candidates c JOIN sources s ON s.source_id=c.source_id "
        "WHERE c.run_id=? AND c.status='machine_accepted_unverified' ORDER BY c.item_id",
        (run_id,),
    ).fetchall()
    records = []
    for database_row in rows:
        candidate = _json(database_row["candidate_json"], {})
        validation = _latest_validation(connection, candidate["item_id"])
        try:
            records.append(_row(connection, candidate, database_row, validation, seed))
        except ValueError:
            continue
    connection.close()
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "reviewer-items.jsonl"
    csv_path = output_dir / "reviewer-items.csv"
    benchmark_jsonl = output_dir / "benchmark-inputs.jsonl"
    benchmark_csv = output_dir / "benchmark-inputs.csv"
    scoring_jsonl = output_dir / "scoring-labels.jsonl"
    scoring_csv = output_dir / "scoring-labels.csv"
    template_dir = output_dir / "prompt-templates"
    templates = []
    for path in prompt_templates or []:
        destination = template_dir / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        templates.append({"stage": path.stem, "path": str(destination.relative_to(output_dir)), "sha256": _sha256(destination)})
    jsonl_path.write_text("".join(canonical_json(row) + "\n" for row in records), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["item_id", "question_id", "variant_id", "doi", "title", "selection_reason", "question", "question_rationale", "reference_answer", "answer_selection_rationale", "answer_rationale", "reconstruction_rationale", "verification_rationale", "option_a", "option_a_generation_rationale", "option_a_rationale", "option_a_verdict", "option_b", "option_b_generation_rationale", "option_b_rationale", "option_b_verdict", "option_c", "option_c_generation_rationale", "option_c_rationale", "option_c_verdict", "option_d", "option_d_generation_rationale", "option_d_rationale", "option_d_verdict", "correct_option", "reference_answer_json", "options_json", "selection_json", "validation_json", "provenance_json", "rationale_availability_json"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in records:
            options = row["options"]
            readable_options = {}
            for index, letter in enumerate("abcd"):
                option = options[index]
                verdict = option.get("verdict") or {}
                readable_options[f"option_{letter}"] = option["text"]
                readable_options[f"option_{letter}_generation_rationale"] = (option.get("distractor") or {}).get("generation_rationale")
                readable_options[f"option_{letter}_rationale"] = verdict.get("rationale")
                readable_options[f"option_{letter}_verdict"] = canonical_json(verdict) if verdict else None
            writer.writerow({
                "item_id": row["item_id"], "question_id": row["question_id"], "variant_id": row["variant_id"],
                "doi": row["paper"]["doi"], "title": row["paper"]["title"], "selection_reason": row["selection"]["inclusion_reason"],
                "question": row["question"], "question_rationale": row["question_rationale"], "reference_answer": row["reference_answer"].get("text"), "answer_selection_rationale": row["rationales"]["answer_selection"], "answer_rationale": row["rationales"]["answer_generation"], "reconstruction_rationale": row["rationales"]["reconstruction"], "verification_rationale": row["rationales"]["answer_verification"],
                **readable_options,
                "correct_option": next("abcd"[index] for index, option in enumerate(options) if option["is_correct"]),
                "reference_answer_json": canonical_json(row["reference_answer"]), "options_json": canonical_json(options),
                "selection_json": canonical_json(row["selection"]), "validation_json": canonical_json(row["validation"]),
                "provenance_json": canonical_json(row["provenance"]), "rationale_availability_json": canonical_json(row["rationale_availability"]),
            })
    benchmark = [{"item_id": row["item_id"], "question_id": row["question_id"], "variant_id": row["variant_id"], "question": row["question"], "options": [{"option_id": option["option_id"], "position": option["position"], "text": option["text"]} for option in row["options"]]} for row in records]
    scoring = [{"item_id": row["item_id"], "correct_option_id": next(option["option_id"] for option in row["options"] if option["is_correct"])} for row in records]
    benchmark_jsonl.write_text("".join(canonical_json(row) + "\n" for row in benchmark), encoding="utf-8")
    scoring_jsonl.write_text("".join(canonical_json(row) + "\n" for row in scoring), encoding="utf-8")
    for path, values, fields in ((benchmark_csv, benchmark, ["item_id", "question_id", "variant_id", "question", "options_json"]), (scoring_csv, scoring, ["item_id", "correct_option_id"])):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for value in values:
                writer.writerow({name: (canonical_json(value["options"]) if name == "options_json" and "options" in value else value.get(name)) for name in fields})
    files = {"reviewer_jsonl": jsonl_path, "reviewer_csv": csv_path, "benchmark_jsonl": benchmark_jsonl, "benchmark_csv": benchmark_csv, "scoring_jsonl": scoring_jsonl, "scoring_csv": scoring_csv}
    manifest = {"schema_version": SCHEMA_VERSION, "shuffle_seed": seed, "reviewer_item_count": len(records), "benchmark_item_count": len(benchmark), "files": {name: {"path": path.name, "sha256": _sha256(path)} for name, path in files.items()}, "prompt_templates": templates, "limitations": ["Benchmark inputs contain no labels, rationales, evidence, or provenance.", "Reviewer files are not model-facing benchmark inputs.", "The package excludes full papers, extracted source blobs, and submitted prompts.", "Missing rationale fields remain explicit missing values."]}
    (output_dir / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a reviewer publication package from a read-only Arctic QA state DB.")
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--shuffle-seed", default="publication-review-v1")
    parser.add_argument("--prompt-template", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    export_publication_package(args.state_db, args.output_dir, run_id=args.run_id, seed=args.shuffle_seed, prompt_templates=args.prompt_template)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
