from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database
from .util import atomic_json, atomic_write, jsonl_bytes, stable_id


def export_run(
    db: Database, namespace: Path, run_id: str, *, seed: str
) -> dict[str, Any]:
    rows = db.rows(
        "SELECT * FROM candidates WHERE run_id=? AND status='machine_accepted_unverified' ORDER BY item_id",
        (run_id,),
    )
    short_answers: list[dict[str, Any]] = []
    mcqs: list[dict[str, Any]] = []
    for row in rows:
        candidate = json.loads(row["candidate_json"])
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
            item
            for item in validation_details["distractors"]
            if item["accepted"] and item["deterministic"]
        ]
        if len(accepted) >= 3:
            mcqs.append(_present_mcq(candidate, accepted[:3], seed))
        if len(accepted) >= 4:
            mcqs.append(_absent_mcq(candidate, accepted[:4], seed))
    export_id = stable_id("export", run_id, seed, [row["item_id"] for row in rows])
    destination = namespace / "exports" / export_id
    files = {
        "short_answer": destination / "short_answer.jsonl",
        "mcq": destination / "mcq.jsonl",
        "rejections": destination / "rejections.jsonl",
    }
    atomic_write(files["short_answer"], jsonl_bytes(short_answers), immutable=True)
    atomic_write(
        files["mcq"],
        jsonl_bytes(sorted(mcqs, key=lambda row: row["item_id"])),
        immutable=True,
    )
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
    atomic_write(files["rejections"], jsonl_bytes(rejections), immutable=True)
    manifest = {
        "schema_version": "1.0.0",
        "export_id": export_id,
        "run_id": run_id,
        "shuffle_seed": seed,
        "release_label_ceiling": "machine_accepted_unverified",
        "short_answer_count": len(short_answers),
        "mcq_count": len(mcqs),
        "rejection_count": len(rejections),
        "files": {key: str(path.relative_to(namespace)) for key, path in files.items()},
    }
    atomic_json(destination / "manifest.json", manifest, immutable=True)
    return manifest


def _short_answer(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": candidate["item_id"],
        "task_type": "short_answer",
        "question": candidate["question"],
        "reference_answers": [
            candidate["answer"]["text"],
            *candidate["answer"].get("variants", []),
        ],
        "evidence": {
            "quote": candidate["answer"]["evidence_quote"],
            "locator": candidate["answer"]["locator"],
        },
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
        "options": options,
        "release_label": "machine_accepted_unverified",
        "source": candidate["source"],
        "answer_evidence": {
            "quote": candidate["answer"]["evidence_quote"],
            "locator": candidate["answer"]["locator"],
        },
    }


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
        "options": options,
        "release_label": "machine_accepted_unverified",
        "source": candidate["source"],
    }


def _shuffle(values: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    return sorted(values, key=lambda row: stable_id("position", seed, row["text"]))
