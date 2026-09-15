from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database
from .util import atomic_json, atomic_write, jsonl_bytes, sha256_bytes, stable_id


def export_run(
    db: Database,
    namespace: Path,
    run_id: str,
    *,
    seed: str,
    candidate_schema_version: str | None = None,
    generation_prompt_version: str | None = None,
) -> dict[str, Any]:
    if (candidate_schema_version is None) is not (generation_prompt_version is None):
        raise ValueError(
            "candidate and prompt contract filters must be supplied together"
        )
    rows = db.rows(
        "SELECT * FROM candidates WHERE run_id=? AND status='machine_accepted_unverified' ORDER BY item_id",
        (run_id,),
    )
    incomplete_rows = db.rows(
        """SELECT * FROM candidates
        WHERE run_id=? AND status='incomplete_non_mcq'
        ORDER BY paper_family_id,updated_at DESC,item_id DESC""",
        (run_id,),
    )
    short_answers: list[dict[str, Any]] = []
    incomplete_short_answers: list[dict[str, Any]] = []
    mcqs: list[dict[str, Any]] = []
    for row in rows:
        candidate = json.loads(row["candidate_json"])
        if not _matches_generation_contract(
            candidate,
            candidate_schema_version=candidate_schema_version,
            generation_prompt_version=generation_prompt_version,
        ):
            continue
        validation = db.one(
            """SELECT * FROM validation_events
            WHERE item_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (candidate["item_id"],),
        )
        if not validation or validation["label"] != "machine_accepted_unverified":
            continue
        validation_details = json.loads(validation["details_json"])
        if validation_details.get("candidate_hash") != stable_id(
            "candidate-payload", row["candidate_json"]
        ):
            continue
        candidate["release_label"] = "machine_accepted_unverified"
        short_answers.append(_short_answer(candidate))
        accepted = [
            item for item in validation_details["distractors"] if item["accepted"]
        ]
        if validation_details["labels"].get("mcq_eligible") and len(accepted) >= 3:
            mcqs.append(_present_mcq(candidate, accepted[:3], seed))
            if len(accepted) >= 4:
                mcqs.append(_absent_mcq(candidate, accepted[:4], seed))
    incomplete_families: set[str] = set()
    for row in incomplete_rows:
        candidate = json.loads(row["candidate_json"])
        if not _matches_generation_contract(
            candidate,
            candidate_schema_version=candidate_schema_version,
            generation_prompt_version=generation_prompt_version,
        ):
            continue
        if row["paper_family_id"] in incomplete_families:
            continue
        incomplete_families.add(row["paper_family_id"])
        candidate["release_label"] = "incomplete_non_mcq"
        incomplete_short_answers.append(_short_answer(candidate))
    mcqs = sorted(mcqs, key=lambda row: row["item_id"])
    rejections = [
        {
            "rejection_id": row["rejection_id"],
            "item_id": row["item_id"],
            "source_id": row["source_id"],
            "stage": row["stage"],
            "reason_code": row["reason_code"],
        }
        for row in db.rows("SELECT * FROM rejection_ledger ORDER BY rejection_id")
    ]
    payloads = {
        "short_answer": jsonl_bytes(short_answers),
        "incomplete_short_answer": jsonl_bytes(incomplete_short_answers),
        "mcq": jsonl_bytes(mcqs),
        "rejections": jsonl_bytes(rejections),
    }
    payload_sha256 = {name: sha256_bytes(data) for name, data in payloads.items()}
    export_id = stable_id(
        "export",
        run_id,
        seed,
        payload_sha256,
    )
    destination = namespace / "exports" / export_id
    files = {
        "short_answer": destination / "short_answer.jsonl",
        "incomplete_short_answer": destination / "incomplete_short_answer.jsonl",
        "mcq": destination / "mcq.jsonl",
        "rejections": destination / "rejections.jsonl",
    }
    for name, path in files.items():
        atomic_write(path, payloads[name], immutable=True)
    manifest = {
        "schema_version": "1.0.0",
        "export_id": export_id,
        "run_id": run_id,
        "shuffle_seed": seed,
        "release_label_ceiling": "machine_accepted_unverified",
        "short_answer_count": len(short_answers),
        "incomplete_short_answer_count": len(incomplete_short_answers),
        "mcq_count": len(mcqs),
        "rejection_count": len(rejections),
        "files": {key: str(path.relative_to(namespace)) for key, path in files.items()},
        "file_sha256": payload_sha256,
    }
    atomic_json(destination / "manifest.json", manifest, immutable=True)
    return manifest


def _matches_generation_contract(
    candidate: dict[str, Any],
    *,
    candidate_schema_version: str | None,
    generation_prompt_version: str | None,
) -> bool:
    if candidate_schema_version is None:
        return True
    provenance = candidate.get("provenance") or {}
    return bool(
        candidate.get("schema_version") == candidate_schema_version
        and provenance.get("prompt_version") == generation_prompt_version
    )


def _short_answer(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": candidate["item_id"],
        "task_type": "short_answer",
        "question": candidate["question"],
        "question_context": candidate.get("question_context", ""),
        "reference_answers": [
            candidate["answer"]["text"],
            *candidate["answer"].get("variants", []),
        ],
        "evidence": _answer_evidence(candidate["answer"]),
        "source": candidate["source"],
        "scope": candidate["answer"]["scope"],
        "release_label": candidate["release_label"],
        "provenance": candidate["provenance"],
    }


def _present_mcq(
    candidate: dict[str, Any], distractors: list[dict[str, Any]], seed: str
) -> dict[str, Any]:
    options = [{"text": candidate["answer"]["text"], "is_correct": True}] + [
        {
            "text": row["text"],
            "is_correct": False,
            "verification_label": row["label"],
            "falsity_evidence": {
                "quote": row["evidence_quote"],
                "locator": row["locator"],
            },
        }
        for row in distractors
    ]
    options = _shuffle(
        options, stable_id("shuffle", seed, candidate["item_id"], "present")
    )
    return {
        "item_id": stable_id("mcq", candidate["item_id"], "present"),
        "paired_item_id": candidate["item_id"],
        "task_type": "answer_present_mcq",
        "question": candidate["question"],
        "question_context": candidate.get("question_context", ""),
        "options": options,
        "release_label": "machine_accepted_unverified",
        "source": candidate["source"],
        "answer_evidence": _answer_evidence(candidate["answer"]),
    }


def _answer_evidence(answer: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        "quote": answer["evidence_quote"],
        "locator": answer["locator"],
    }
    span_fields = {
        "source_span_id": answer.get("source_span_id"),
        "span_contract_version": answer.get("span_contract_version"),
        "text_sha256": answer.get("evidence_text_sha256"),
    }
    evidence.update({key: value for key, value in span_fields.items() if value})
    return evidence


def _absent_mcq(
    candidate: dict[str, Any], distractors: list[dict[str, Any]], seed: str
) -> dict[str, Any]:
    options = [
        {
            "text": row["text"],
            "is_correct": False,
            "verification_label": row["label"],
            "falsity_evidence": {
                "quote": row["evidence_quote"],
                "locator": row["locator"],
            },
        }
        for row in distractors
    ]
    options = _shuffle(
        options, stable_id("shuffle", seed, candidate["item_id"], "absent")
    )
    return {
        "item_id": stable_id("mcq", candidate["item_id"], "absent"),
        "paired_item_id": candidate["item_id"],
        "task_type": "answer_absent_mcq",
        "evidence_state": "invalid_option_set",
        "interpretation_limit": "This item tests rejection of an invalid option set. It does not establish model ignorance.",
        "question": candidate["question"],
        "question_context": candidate.get("question_context", ""),
        "options": options,
        "release_label": "machine_accepted_unverified",
        "source": candidate["source"],
    }


def _shuffle(values: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    return sorted(values, key=lambda row: stable_id("position", seed, row["text"]))
