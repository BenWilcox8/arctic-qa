"""Build a reviewer-only publication package from a read-only Arctic QA state DB."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util import atomic_json, atomic_write, canonical_json, jsonl_bytes, stable_id


SCHEMA_VERSION = "arctic-qa-publication-review-v1"
LIVE_SELECTION_SCHEMA = "arctic-qa-live-dataset-selection-v1"
LIVE_SNAPSHOT_SCHEMA = "arctic-qa-live-dataset-snapshot-v1"
LIVE_POINTER_SCHEMA = "arctic-qa-live-dataset-pointer-v1"
LIVE_REVIEWER_SCHEMA = "arctic-qa-live-reviewer-item-v1"
LIVE_MACHINE_VALIDATED_PREVIEW = "machine_validated_preview"
LIVE_PREVIEW_MINIMUM_MODEL_VERIFIED_DISTRACTORS = 3


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _live_selection(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != LIVE_SELECTION_SCHEMA:
        raise ValueError("the live dataset selection has an unsupported schema")
    required = (
        "candidate_schema_version",
        "generation_prompt_version",
        "scope_contract_version",
    )
    selection = {name: value.get(name) for name in required}
    if any(not isinstance(item, str) or not item for item in selection.values()):
        raise ValueError("the live dataset selection has an invalid contract value")
    if value.get("selection_mode") != LIVE_MACHINE_VALIDATED_PREVIEW:
        raise ValueError("the live dataset selection has an unsupported mode")
    if (
        value.get("minimum_model_verified_distractors")
        != LIVE_PREVIEW_MINIMUM_MODEL_VERIFIED_DISTRACTORS
    ):
        raise ValueError("the live dataset selection has an invalid preview threshold")
    selection["selection_mode"] = LIVE_MACHINE_VALIDATED_PREVIEW
    selection["minimum_model_verified_distractors"] = (
        LIVE_PREVIEW_MINIMUM_MODEL_VERIFIED_DISTRACTORS
    )
    return selection, hashlib.sha256(raw).hexdigest()


_LIVE_ROW_EXCLUDED_KEYS = {
    "acceptance_status",
    "actual_cost_usd",
    "benchmark_group",
    "call_id",
    "campaign_id",
    "completed_at",
    "completed_at_utc",
    "cost",
    "created_at",
    "final_label",
    "invocation_run_id",
    "release_label",
    "request_id",
    "request_key",
    "receipt_file",
    "receipt_sha256",
    "reserved_cost_usd",
    "review_group",
    "run_id",
    "started_at",
    "status",
    "system_prompt",
    "submitted_at_utc",
    "timestamp",
    "updated_at",
    "updated_at_utc",
}


def _live_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _live_safe(item)
            for key, item in value.items()
            if key not in _LIVE_ROW_EXCLUDED_KEYS
            and not key.endswith("_cost_usd")
            and not key.endswith("_at_utc")
        }
    if isinstance(value, list):
        return [_live_safe(item) for item in value]
    return value


def _live_validation_events(
    connection: sqlite3.Connection, item_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT stage,label,reason_codes_json,details_json FROM validation_events "
        "WHERE item_id=? ORDER BY created_at,rowid",
        (item_id,),
    ).fetchall()
    result = []
    for row in rows:
        details = _json(row["details_json"], {})
        if not isinstance(details, dict):
            details = {}
        labels = details.pop("labels", {})
        if isinstance(labels, dict):
            details["checks"] = {
                name: value
                for name, value in labels.items()
                if name
                not in {
                    "machine_accepted_unverified",
                    "rejected",
                    "mcq_eligible",
                    "unresolved",
                }
            }
        result.append(
            {
                "stage": row["stage"],
                "reason_codes": _json(row["reason_codes_json"], []),
                "details": _live_safe(details),
            }
        )
    return result


def _live_candidate_matches(
    candidate: dict[str, Any], selection: dict[str, Any]
) -> bool:
    provenance = candidate.get("provenance")
    return bool(
        isinstance(provenance, dict)
        and candidate.get("schema_version") == selection["candidate_schema_version"]
        and provenance.get("prompt_version") == selection["generation_prompt_version"]
        and provenance.get("scope_contract_version")
        == selection["scope_contract_version"]
    )


def _live_latest_validation(
    connection: sqlite3.Connection, item_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT label,reason_codes_json,details_json FROM validation_events "
        "WHERE item_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def _live_preview_distractors(
    validation_details: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return retained model-verified distractors for the live review preview."""
    return [
        item
        for item in validation_details.get("distractors", [])
        if isinstance(item, dict)
        and item.get("accepted") is True
        and item.get("model_verified") is True
    ]


def _live_reviewer_row(
    connection: sqlite3.Connection,
    candidate: dict[str, Any],
    source: sqlite3.Row,
    validation_details: dict[str, Any],
    seed: str,
) -> dict[str, Any]:
    accepted_text = {
        item.get("text") for item in _live_preview_distractors(validation_details)
    }
    distractors = [
        item
        for item in candidate.get("distractors", [])
        if isinstance(item, dict) and item.get("text") in accepted_text
    ]
    if len(distractors) < 3:
        raise ValueError("a live dataset row requires three accepted distractors")
    verdicts = {
        item.get("option_text"): item
        for item in candidate.get("option_verdicts", [])
        if isinstance(item, dict)
    }
    option_values = [
        {
            "text": candidate["answer"]["text"],
            "is_correct": True,
            "evidence": _short_evidence(candidate["answer"]),
            "generation_rationale": candidate["answer"].get("rationale"),
            "verification": None,
        },
        *[
            {
                "text": item["text"],
                "is_correct": False,
                "evidence": _short_evidence(item),
                "generation_rationale": item.get("generation_rationale"),
                "verification": _live_safe(verdicts.get(item["text"], {})),
            }
            for item in distractors[:3]
        ],
    ]
    option_values.sort(
        key=lambda item: stable_id(
            "live-publication-option", seed, candidate["item_id"], item["text"]
        )
    )
    item_id = stable_id("live-publication-item", seed, candidate["item_id"])
    options = [
        {
            "option_id": stable_id("live-publication-option-id", item_id, item["text"]),
            "position": position,
            **item,
        }
        for position, item in enumerate(option_values, start=1)
    ]
    answer = candidate.get("answer", {})
    reconstruction = candidate.get("reconstruction", {})
    answer_verification = candidate.get("answer_verification", {})
    answer_agreement = candidate.get("answer_agreement", {})
    return {
        "schema_version": LIVE_REVIEWER_SCHEMA,
        "item_id": item_id,
        "question_id": candidate["item_id"],
        "paper": {"doi": source["doi"], "title": source["title"]},
        "question": candidate["question"],
        "question_context": _question_context(candidate),
        "reference_answer": _live_safe(
            {
                name: answer[name]
                for name in (
                    "text",
                    "variants",
                    "claim_type",
                    "scope",
                    "deterministic_rule",
                    "numeric_rule",
                )
                if name in answer
            }
        ),
        "answer_evidence": _short_evidence(answer),
        "decision_evidence": [
            evidence
            for evidence in (
                _short_evidence(item) for item in candidate.get("decision_evidence", [])
            )
            if evidence is not None
        ],
        "options": options,
        "rationales": {
            name: value
            for name, value in (
                ("question", candidate.get("question_rationale")),
                ("answer_selection", answer.get("selection_rationale")),
                ("answer_generation", answer.get("rationale")),
                (
                    "reconstruction",
                    reconstruction.get("reconstruction_rationale"),
                ),
                (
                    "answer_verification",
                    answer_verification.get("verification_rationale"),
                ),
            )
            if value is not None
        },
        "stage_results": {
            name: _safe_stage_result(value)
            for name, value in (
                ("reconstruction", reconstruction),
                ("answer_verification", answer_verification),
                ("answer_agreement", answer_agreement),
            )
            if value
        },
        "model_trace": _model_trace(candidate),
        "validation": _live_validation_events(connection, candidate["item_id"]),
        "limitations": [
            "Automated validation and model rationales are not independent scientific review."
        ],
    }


def _live_rows(
    state_db: Path, selection: dict[str, Any], seed: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    exclusions = {
        "incomplete_or_rejected": 0,
        "superseded_contract": 0,
        "invalid_or_unbound_validation": 0,
        "duplicate_current_family": 0,
    }
    try:
        rows = connection.execute(
            "SELECT c.item_id,c.paper_family_id,c.candidate_json,c.status,c.updated_at,"
            "s.doi,s.title FROM candidates c JOIN sources s ON s.source_id=c.source_id "
            "ORDER BY c.paper_family_id,c.updated_at,c.item_id"
        ).fetchall()
        accepted: list[tuple[sqlite3.Row, dict[str, Any], dict[str, Any]]] = []
        for row in rows:
            if row["status"] != "machine_accepted_unverified":
                exclusions["incomplete_or_rejected"] += 1
                continue
            candidate = _json(row["candidate_json"], {})
            if not isinstance(candidate, dict) or not _live_candidate_matches(
                candidate, selection
            ):
                exclusions["superseded_contract"] += 1
                continue
            validation = _live_latest_validation(connection, row["item_id"])
            details = _json(validation["details_json"], {}) if validation else {}
            labels = details.get("labels", {}) if isinstance(details, dict) else {}
            accepted_distractors = _live_preview_distractors(details)
            payload_hash = stable_id("candidate-payload", row["candidate_json"])
            if not (
                validation
                and validation["label"] == "machine_accepted_unverified"
                and isinstance(details, dict)
                and details.get("candidate_hash") == payload_hash
                and isinstance(labels, dict)
                and labels.get("mcq_eligible") is True
                and labels.get("schema_valid") is True
                and labels.get("scope_complete") is True
                and labels.get("reconstruction_agreement") is True
                and labels.get("source_entailment_model_verified") is True
                and labels.get("alternative_answer_search_passed") is True
                and labels.get("model_verified") is True
                and len(accepted_distractors)
                >= selection["minimum_model_verified_distractors"]
            ):
                exclusions["invalid_or_unbound_validation"] += 1
                continue
            accepted.append((row, candidate, details))
        by_family: dict[str, tuple[sqlite3.Row, dict[str, Any], dict[str, Any]]] = {}
        for value in accepted:
            family_id = str(value[0]["paper_family_id"])
            if family_id in by_family:
                exclusions["duplicate_current_family"] += 1
            by_family[family_id] = value
        reviewer = [
            _live_reviewer_row(connection, candidate, row, details, seed)
            for row, candidate, details in by_family.values()
        ]
        reviewer.sort(key=lambda item: item["item_id"])
        return reviewer, exclusions
    finally:
        connection.close()


def _copy_live_prompts(
    output_dir: Path, prompt_files: list[Path]
) -> list[dict[str, str]]:
    result = []
    for source in prompt_files:
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        destination = output_dir / "prompts" / f"{digest[:16]}-{source.name}"
        atomic_write(destination, data, immutable=True)
        result.append({"filename": destination.name, "sha256": digest})
    return result


def refresh_live_publication_snapshot(
    state_db: Path,
    output_dir: Path,
    *,
    selection_file: Path,
    seed: str,
    prompt_files: list[Path] | None = None,
) -> dict[str, Any]:
    """Write one immutable current-contract snapshot and an atomic pointer."""
    selection, selection_sha256 = _live_selection(selection_file)
    reviewer_rows, exclusions = _live_rows(state_db, selection, seed)
    benchmark_rows = [
        {
            "item_id": row["item_id"],
            "question_id": row["question_id"],
            "question": row["question"],
            "question_context": row["question_context"],
            "options": [
                {
                    "option_id": option["option_id"],
                    "position": option["position"],
                    "text": option["text"],
                }
                for option in row["options"]
            ],
        }
        for row in reviewer_rows
    ]
    payloads = {
        "benchmark": jsonl_bytes(benchmark_rows),
        "reviewer": jsonl_bytes(reviewer_rows),
    }
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
    prompt_records = _copy_live_prompts(output_dir, prompt_files or [])
    snapshot_id = stable_id(
        "live-dataset-snapshot",
        selection_sha256,
        hashes,
        prompt_records,
        length=32,
    )
    snapshot_dir = output_dir / "snapshots" / snapshot_id
    filenames = {
        "benchmark": "accepted-benchmark.jsonl",
        "reviewer": "accepted-reviewer.jsonl",
    }
    for name, data in payloads.items():
        atomic_write(snapshot_dir / filenames[name], data, immutable=True)
    manifest_path = snapshot_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {
            "schema": LIVE_SNAPSHOT_SCHEMA,
            "snapshot_id": snapshot_id,
            "updated_at_utc": _utc_now(),
            "item_count": len(reviewer_rows),
            "selection": selection,
            "selection_sha256": selection_sha256,
            "files": {
                name: {
                    "path": filenames[name],
                    "sha256": hashes[name],
                    "size_bytes": len(payloads[name]),
                }
                for name in payloads
            },
            "prompt_files": prompt_records,
            "preview": {
                "label": "Machine-validated preview",
                "notice": (
                    "Some accepted distractors are model-verified but "
                    "non-deterministic. The reviewer rows retain their "
                    "validation and uncertainty details."
                ),
            },
            "excluded_counts": exclusions,
            "limitations": [
                "Only candidates that match the selected current contracts can enter this snapshot.",
                "Historical and rejected candidates remain in the state database.",
                "The benchmark rows contain no answer labels, evidence, rationales, or validation details.",
            ],
        }
        atomic_json(manifest_path, manifest, immutable=True)
    pointer = {
        "schema": LIVE_POINTER_SCHEMA,
        "snapshot_id": snapshot_id,
        "manifest": f"snapshots/{snapshot_id}/manifest.json",
        "manifest_sha256": _sha256(manifest_path),
        "updated_at_utc": manifest["updated_at_utc"],
        "item_count": manifest["item_count"],
    }
    atomic_json(output_dir / "current.json", pointer)
    return manifest


def poll_live_publication_snapshots(
    state_db: Path,
    output_dir: Path,
    *,
    selection_file: Path,
    seed: str,
    prompt_files: list[Path] | None = None,
    poll_seconds: float,
) -> None:
    if not 1 <= poll_seconds <= 300:
        raise ValueError("the poll interval must be between 1 and 300 seconds")
    while True:
        refresh_live_publication_snapshot(
            state_db,
            output_dir,
            selection_file=selection_file,
            seed=seed,
            prompt_files=prompt_files,
        )
        time.sleep(poll_seconds)


def load_authoritative_mcqs(
    export_manifest: Path, source_root: Path
) -> list[dict[str, Any]]:
    """Load the immutable accepted MCQ variants selected by an export manifest."""
    manifest = _json(export_manifest.read_text(encoding="utf-8"), {})
    relative = manifest.get("files", {}).get("mcq")
    if not isinstance(relative, str):
        raise ValueError("the export manifest has no MCQ file")
    path = Path(relative)
    path = path if path.is_absolute() else source_root / path
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


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
    details = _json(row["details_json"], {})
    return {
        "available": True,
        "reason_codes": _json(row["reason_codes_json"], []),
        "details": {"distractors": details.get("distractors", [])},
    }


def _accepted_options(
    candidate: dict[str, Any], validation: dict[str, Any]
) -> list[dict[str, Any]]:
    accepted = {
        row.get("text")
        for row in validation.get("details", {}).get("distractors", [])
        if row.get("accepted") is True and row.get("deterministic") is True
    }
    verdicts = {
        row.get("option_text"): row for row in candidate.get("option_verdicts") or []
    }
    return [
        {
            "text": row.get("text"),
            "is_correct": False,
            "distractor": row,
            "verdict": verdicts.get(row.get("text")),
        }
        for row in candidate.get("distractors") or []
        if row.get("text") in accepted
    ]


def _source_selection(source: sqlite3.Row) -> dict[str, Any] | None:
    metadata = _json(source["metadata_json"], {})
    selection = metadata.get("selection") if isinstance(metadata, dict) else None
    return selection if isinstance(selection, dict) else None


def _receipt_trace(
    connection: sqlite3.Connection, candidate: dict[str, Any]
) -> list[dict[str, Any]]:
    requests = []
    for value in (
        candidate.get("provenance", {}).get("verification_calls", {}) or {}
    ).values():
        if isinstance(value, dict) and value.get("request_id"):
            requests.append(value["request_id"])
    for value in candidate.get("option_verdicts") or []:
        provenance = value.get("provenance") or {}
        if provenance.get("request_id"):
            requests.append(provenance["request_id"])
    result = []
    for request_id in sorted(set(requests)):
        row = connection.execute(
            "SELECT call_id,role,request_id,returned_model,response_json FROM calls WHERE request_id=? ORDER BY attempt DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        if row and row["response_json"]:
            response = _json(row["response_json"], {})
            result.append(
                {
                    "call_id": row["call_id"],
                    "role": row["role"],
                    "request_id": row["request_id"],
                    "returned_model": row["returned_model"],
                    "response_sha256": hashlib.sha256(
                        row["response_json"].encode()
                    ).hexdigest(),
                    "rationale": response.get("rationale"),
                    "verdict": {
                        name: response.get(name)
                        for name in (
                            "contradiction_established",
                            "alternative_answer_search_passed",
                            "source_entailment_model_verified",
                        )
                        if name in response
                    },
                    "evidence": {
                        name: response.get(name)
                        for name in (
                            "evidence_quote",
                            "locator",
                            "source_span_id",
                            "span_contract_version",
                        )
                        if name in response
                    },
                }
            )
    return result


def _row(
    connection: sqlite3.Connection,
    candidate: dict[str, Any],
    source: sqlite3.Row,
    validation: dict[str, Any],
    seed: str,
) -> dict[str, Any]:
    accepted = _accepted_options(candidate, validation)
    if len(accepted) < 3:
        raise ValueError(
            "a publication row requires three accepted deterministic distractors"
        )
    selected = accepted[:3]
    options = [
        {
            "text": candidate["answer"]["text"],
            "is_correct": True,
            "distractor": None,
            "verdict": None,
        },
        *selected,
    ]
    options = sorted(
        options,
        key=lambda option: stable_id(
            "publication-option", seed, candidate["item_id"], option["text"]
        ),
    )
    review_options = []
    for position, option in enumerate(options, start=1):
        review_options.append(
            {
                "option_id": stable_id(
                    "publication-option-id", candidate["item_id"], option["text"]
                ),
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
            "source_id": source["source_id"],
            "paper_id": source["stable_id"],
            "doi": source["doi"],
            "title": source["title"],
            "year": source["year"],
            "content_hash": source["content_hash"],
        },
        "selection": {
            "source_selection": _source_selection(source),
            "inclusion_reason": source["inclusion_reason"],
        },
        "question": candidate["question"],
        "question_context": _question_context(candidate),
        "question_rationale": candidate.get("question_rationale"),
        "reference_answer": candidate["answer"],
        "decision_evidence": candidate.get("decision_evidence", []),
        "rationales": {
            "answer_selection": candidate.get("answer", {}).get("selection_rationale"),
            "answer_generation": candidate.get("answer", {}).get("rationale"),
            "reconstruction": candidate.get("reconstruction", {}).get(
                "reconstruction_rationale"
            ),
            "answer_verification": candidate.get("answer_verification", {}).get(
                "verification_rationale"
            ),
        },
        "options": review_options,
        "validation": validation,
        "provenance": {
            key: value
            for key, value in (candidate.get("provenance") or {}).items()
            if key
            not in {
                "run_id",
                "family_overlap_disclosure",
                "policy_ablation_metadata",
                "method_status",
                "construction_role_policy",
            }
        },
        "receipt_derived_verification": _receipt_trace(connection, candidate),
        "rationale_availability": {
            "question": bool(candidate.get("question_rationale")),
            "answer_selection": bool(
                candidate.get("answer", {}).get("selection_rationale")
            ),
            "answer_generation": bool(candidate.get("answer", {}).get("rationale")),
            "reconstruction": bool(
                candidate.get("reconstruction", {}).get("reconstruction_rationale")
            ),
            "answer_verification": bool(
                candidate.get("answer_verification", {}).get("verification_rationale")
            ),
            "distractor_generation": any(
                option["distractor"]
                and option["distractor"].get("generation_rationale")
                for option in review_options
            ),
            "option_verdict": any(
                option["verdict"] and option["verdict"].get("rationale")
                for option in review_options
            ),
            "note": "Null rationale fields mean that this input did not retain that rationale. They do not mean that a rationale passed.",
        },
    }


def _question_context(item: dict[str, Any]) -> str:
    value = item.get("question_context")
    return value if isinstance(value, str) else ""


def _manifest_question_context(item: dict[str, Any], candidate: dict[str, Any]) -> str:
    if isinstance(item.get("question_context"), str):
        return item["question_context"]
    return _question_context(candidate)


def _short_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    quote = value.get("quote") or value.get("evidence_quote")
    locator = value.get("locator")
    if not isinstance(quote, str) and not isinstance(locator, dict):
        return None
    result: dict[str, Any] = {}
    if isinstance(quote, str):
        result["excerpt"] = quote[:800]
        result["excerpt_truncated"] = len(quote) > 800
    if isinstance(locator, dict):
        result["locator"] = locator
    for name in (
        "source_span_id",
        "span_contract_version",
        "text_sha256",
        "evidence_text_sha256",
    ):
        if isinstance(value.get(name), str):
            result[name] = value[name]
    for name in ("source_span_ids", "eligibility_span_ids", "roles"):
        if isinstance(value.get(name), list):
            result[name] = list(value[name])
    components = value.get("evidence_components")
    if isinstance(components, list):
        result["evidence_components"] = [
            {
                name: component[name]
                for name in (
                    "source_span_id",
                    "locator",
                    "text_sha256",
                    "eligibility_span_id",
                    "eligibility_quote_sha256",
                    "eligibility_locator",
                    "eligibility_match_kind",
                )
                if name in component
            }
            for component in components
            if isinstance(component, dict)
        ]
    role_evidence = value.get("role_evidence")
    if isinstance(role_evidence, list):
        result["role_evidence"] = [
            {
                "role": role_record.get("role"),
                "evidence": _short_evidence(role_record),
            }
            for role_record in role_evidence
            if isinstance(role_record, dict)
        ]
    return result or None


def _safe_stage_result(value: dict[str, Any]) -> dict[str, Any]:
    excluded = {
        "run_id",
        "campaign_id",
        "request_id",
        "request_key",
        "receipt_file",
        "receipt_sha256",
        "prompt_hash",
        "system_prompt",
        "call_id",
        "status",
        "release_label",
        "created_at",
        "updated_at",
    }
    result: dict[str, Any] = {}
    evidence = _short_evidence(value)
    if evidence:
        result["evidence"] = evidence
    for name, item in value.items():
        if name in excluded or name in {
            "evidence_quote",
            "quote",
            "locator",
            "source_span_id",
            "span_contract_version",
            "text_sha256",
            "evidence_text_sha256",
        }:
            continue
        if isinstance(item, dict):
            result[name] = _safe_stage_result(item)
        elif isinstance(item, list):
            result[name] = [
                _safe_stage_result(entry) if isinstance(entry, dict) else entry
                for entry in item
            ]
        else:
            result[name] = item
    return result


def _public_validation(
    connection: sqlite3.Connection, candidate_id: str | None
) -> list[dict[str, Any]]:
    if not candidate_id:
        return []
    rows = connection.execute(
        "SELECT stage,label,reason_codes_json,details_json FROM validation_events "
        "WHERE item_id=? ORDER BY rowid",
        (candidate_id,),
    ).fetchall()
    result = []
    for row in rows:
        details = _json(row["details_json"], {})
        labels = details.get("labels", {}) if isinstance(details, dict) else {}
        if not isinstance(labels, dict):
            labels = {}
        event = {
            "stage": row["stage"],
            "reason_codes": _json(row["reason_codes_json"], []),
            "checks": {
                name: value
                for name, value in labels.items()
                if name
                not in {
                    "machine_accepted_unverified",
                    "rejected",
                    "mcq_eligible",
                    "unresolved",
                }
            },
        }
        if row["label"] != "machine_accepted_unverified":
            event["verdict"] = row["label"]
        result.append(event)
    return result


def _manifest_root(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    relative = manifest.get("files", {}).get("mcq")
    if not isinstance(relative, str):
        raise ValueError("the export manifest has no MCQ file")
    if Path(relative).is_absolute():
        return Path("/")
    for parent in (manifest_path.parent, *manifest_path.parents):
        if (parent / relative).is_file():
            return parent
    raise ValueError(
        "the export manifest MCQ file is not available from its parent directories"
    )


def _model_trace(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    provenance = (
        candidate.get("provenance")
        if isinstance(candidate.get("provenance"), dict)
        else {}
    )
    calls = provenance.get("verification_calls") if isinstance(provenance, dict) else {}
    result = []
    if isinstance(calls, dict):
        for value in calls.values():
            if isinstance(value, dict):
                result.append(
                    {
                        name: value.get(name)
                        for name in (
                            "role",
                            "provider",
                            "requested_model",
                            "returned_model",
                            "prompt_version",
                            "prompt_hash",
                        )
                        if value.get(name) is not None
                    }
                )
    for role, model in (
        ("author", provenance.get("author_model")),
        ("verifier", provenance.get("verifier_model")),
    ):
        if isinstance(model, str) and not any(
            entry.get("returned_model") == model for entry in result
        ):
            result.append({"role": role, "returned_model": model})
    return result


def _matching_candidate(
    connection: sqlite3.Connection, item: dict[str, Any]
) -> dict[str, Any] | None:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    source_id = source.get("source_id")
    question = item.get("question")
    question_context = _question_context(item)
    candidate_id = item.get("paired_item_id") or item.get("item_id")
    if not isinstance(source_id, str) or not isinstance(question, str):
        return None
    if isinstance(candidate_id, str):
        row = connection.execute(
            "SELECT item_id,candidate_json FROM candidates WHERE source_id=? AND item_id=?",
            (source_id, candidate_id),
        ).fetchone()
        if row:
            candidate = _json(row["candidate_json"], {})
            if (
                candidate.get("question") == question
                and _question_context(candidate) == question_context
            ):
                candidate["_database_item_id"] = row["item_id"]
                return candidate
    matches = []
    for row in connection.execute(
        "SELECT item_id,candidate_json FROM candidates WHERE source_id=? ORDER BY item_id",
        (source_id,),
    ):
        candidate = _json(row["candidate_json"], {})
        if (
            candidate.get("question") == question
            and _question_context(candidate) == question_context
        ):
            matches.append((row["item_id"], candidate))
    if len(matches) != 1:
        return None
    matched_id, candidate = matches[0]
    candidate["_database_item_id"] = matched_id
    return candidate


def _manifest_row(
    connection: sqlite3.Connection | None, item: dict[str, Any]
) -> dict[str, Any]:
    source_identity = item.get("source") if isinstance(item.get("source"), dict) else {}
    source: dict[str, Any] = {
        name: source_identity.get(name)
        for name in ("source_id", "content_hash", "chunk_id", "section_id")
        if source_identity.get(name) is not None
    }
    candidate: dict[str, Any] = {}
    validations: list[dict[str, Any]] = []
    if connection is not None and isinstance(source_identity.get("source_id"), str):
        database_source = connection.execute(
            "SELECT stable_id,doi,title,year,content_hash,metadata_json,inclusion_reason FROM sources WHERE source_id=?",
            (source_identity["source_id"],),
        ).fetchone()
        if database_source:
            source.update(
                {
                    name: database_source[name]
                    for name in (
                        "stable_id",
                        "doi",
                        "title",
                        "year",
                        "content_hash",
                        "inclusion_reason",
                    )
                    if database_source[name] is not None
                }
            )
            metadata = _json(database_source["metadata_json"], {})
            if isinstance(metadata, dict) and isinstance(
                metadata.get("selection"), dict
            ):
                source["selection"] = metadata["selection"]
        candidate = _matching_candidate(connection, item) or {}
        validations = _public_validation(connection, candidate.get("_database_item_id"))
    verdicts = {
        row.get("option_text"): row
        for row in candidate.get("option_verdicts", [])
        if isinstance(row, dict)
    }
    distractors = {
        row.get("text"): row
        for row in candidate.get("distractors", [])
        if isinstance(row, dict)
    }
    candidate_answer = (
        candidate.get("answer") if isinstance(candidate.get("answer"), dict) else {}
    )
    reconstruction = (
        candidate.get("reconstruction")
        if isinstance(candidate.get("reconstruction"), dict)
        else {}
    )
    answer_verification = (
        candidate.get("answer_verification")
        if isinstance(candidate.get("answer_verification"), dict)
        else {}
    )
    answer_agreement = (
        candidate.get("answer_agreement")
        if isinstance(candidate.get("answer_agreement"), dict)
        else {}
    )
    options = []
    for position, option in enumerate(item.get("options", []), start=1):
        if not isinstance(option, dict):
            continue
        verdict = verdicts.get(option.get("text"), {})
        distractor = distractors.get(option.get("text"), {})
        options.append(
            {
                "position": position,
                "option_id": stable_id(
                    "publication-option-id",
                    item.get("item_id"),
                    str(position),
                    option.get("text"),
                ),
                "text": option.get("text"),
                "is_correct": option.get("is_correct"),
                "generation_rationale": distractor.get("generation_rationale"),
                "verification": {
                    "label": option.get("verification_label"),
                    "evidence": _short_evidence(option.get("falsity_evidence")),
                    "rationale": verdict.get("rationale"),
                    "automated_checks": {
                        name: verdict.get(name)
                        for name in (
                            "contradiction_established",
                            "alternative_answer_search_passed",
                            "source_entailment_model_verified",
                        )
                        if name in verdict
                    },
                },
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "item_id": item.get("item_id"),
        "question_id": item.get("paired_item_id") or item.get("item_id"),
        "variant_id": item.get("task_type"),
        "paper": source,
        "question": item.get("question"),
        "question_context": _manifest_question_context(item, candidate),
        "reference_answer": {
            name: candidate_answer[name]
            for name in (
                "text",
                "claim_type",
                "scope",
                "deterministic_rule",
                "numeric_rule",
            )
            if candidate_answer.get(name) is not None
        }
        or None,
        "answer_evidence": _short_evidence(item.get("answer_evidence"))
        or _short_evidence(candidate_answer),
        "decision_evidence": [
            evidence
            for evidence in (
                _short_evidence(value)
                for value in candidate.get("decision_evidence", [])
            )
            if evidence is not None
        ],
        "options": options,
        "validation": validations,
        "rationales": {
            name: value
            for name, value in (
                ("question", candidate.get("question_rationale")),
                ("answer_selection", candidate_answer.get("selection_rationale")),
                ("answer_generation", candidate_answer.get("rationale")),
                ("reconstruction", reconstruction.get("reconstruction_rationale")),
                (
                    "answer_verification",
                    answer_verification.get("verification_rationale"),
                ),
            )
            if value is not None
        },
        "stage_results": {
            name: _safe_stage_result(value)
            for name, value in (
                ("reconstruction", reconstruction),
                ("answer_verification", answer_verification),
                ("answer_agreement", answer_agreement),
            )
            if value
        },
        "model_trace": _model_trace(candidate),
        "interpretation_limit": item.get("interpretation_limit"),
        "evidence_state": item.get("evidence_state"),
        "limitations": [
            "Automated validation and model-generated rationales are not independent scientific review.",
            "The source export selects the rows. State data only enriches them.",
        ],
    }


def _reviewer_csv_record(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        "item_id": row["item_id"],
        "question_id": row["question_id"],
        "variant_id": row["variant_id"],
        "doi": row["paper"].get("doi"),
        "title": row["paper"].get("title"),
        "question": row["question"],
        "question_context": row["question_context"],
        "reference_answer": (row.get("reference_answer") or {}).get("text"),
        "question_rationale": row["rationales"].get("question"),
        "answer_selection_rationale": row["rationales"].get("answer_selection"),
        "answer_rationale": row["rationales"].get("answer_generation"),
        "reconstruction_rationale": row["rationales"].get("reconstruction"),
        "verification_rationale": row["rationales"].get("answer_verification"),
        "paper_json": canonical_json(row["paper"]),
        "reference_answer_json": canonical_json(row["reference_answer"]),
        "answer_evidence_json": canonical_json(row["answer_evidence"]),
        "decision_evidence_json": canonical_json(row["decision_evidence"]),
        "options_json": canonical_json(row["options"]),
        "validation_json": canonical_json(row["validation"]),
        "rationales_json": canonical_json(row["rationales"]),
        "stage_results_json": canonical_json(row["stage_results"]),
        "model_trace_json": canonical_json(row["model_trace"]),
        "interpretation_limit": row.get("interpretation_limit"),
        "evidence_state": row.get("evidence_state"),
    }
    for index, letter in enumerate("abcd"):
        option = row["options"][index] if index < len(row["options"]) else {}
        result[f"option_{letter}"] = option.get("text")
        result[f"option_{letter}_generation_rationale"] = option.get(
            "generation_rationale"
        )
        result[f"option_{letter}_verification_rationale"] = (
            option.get("verification") or {}
        ).get("rationale")
    return result


def _write_manifest_package(
    export_manifest: Path,
    state_db: Path | None,
    output_dir: Path,
    seed: str,
    prompt_templates: list[Path] | None,
    historical_renderers: list[Path] | None,
) -> dict[str, Any]:
    source_manifest = _json(export_manifest.read_text(encoding="utf-8"), {})
    source_root = _manifest_root(export_manifest, source_manifest)
    items = load_authoritative_mcqs(export_manifest, source_root)
    connection = None
    if state_db:
        connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    try:
        records = [_manifest_row(connection, item) for item in items]
    finally:
        if connection is not None:
            connection.close()
    output_dir.mkdir(parents=True, exist_ok=True)
    reviewer = output_dir / "reviewer-items.jsonl"
    benchmark = output_dir / "benchmark-inputs.jsonl"
    scoring = output_dir / "scoring-labels.jsonl"
    reviewer.write_text(
        "".join(canonical_json(row) + "\n" for row in records), encoding="utf-8"
    )
    benchmark_rows = [
        {
            "item_id": row["item_id"],
            "question_id": row["question_id"],
            "variant_id": row["variant_id"],
            "question": row["question"],
            "question_context": row["question_context"],
            "options": [
                {name: option[name] for name in ("option_id", "position", "text")}
                for option in row["options"]
            ],
        }
        for row in records
    ]
    scoring_rows = [
        {
            "item_id": row["item_id"],
            "correct_option_id": next(
                (
                    option["option_id"]
                    for option in row["options"]
                    if option["is_correct"] is True
                ),
                None,
            ),
            "answer_present": any(
                option["is_correct"] is True for option in row["options"]
            ),
        }
        for row in records
    ]
    benchmark.write_text(
        "".join(canonical_json(row) + "\n" for row in benchmark_rows), encoding="utf-8"
    )
    scoring.write_text(
        "".join(canonical_json(row) + "\n" for row in scoring_rows), encoding="utf-8"
    )
    reviewer_csv = output_dir / "reviewer-items.csv"
    benchmark_csv = output_dir / "benchmark-inputs.csv"
    scoring_csv = output_dir / "scoring-labels.csv"
    reviewer_csv_rows = [_reviewer_csv_record(row) for row in records]
    for path, rows, fields in (
        (
            reviewer_csv,
            reviewer_csv_rows,
            [
                "item_id",
                "question_id",
                "variant_id",
                "doi",
                "title",
                "question",
                "question_context",
                "reference_answer",
                "question_rationale",
                "answer_selection_rationale",
                "answer_rationale",
                "reconstruction_rationale",
                "verification_rationale",
                "option_a",
                "option_a_generation_rationale",
                "option_a_verification_rationale",
                "option_b",
                "option_b_generation_rationale",
                "option_b_verification_rationale",
                "option_c",
                "option_c_generation_rationale",
                "option_c_verification_rationale",
                "option_d",
                "option_d_generation_rationale",
                "option_d_verification_rationale",
                "paper_json",
                "reference_answer_json",
                "answer_evidence_json",
                "decision_evidence_json",
                "options_json",
                "validation_json",
                "rationales_json",
                "stage_results_json",
                "model_trace_json",
                "interpretation_limit",
                "evidence_state",
            ],
        ),
        (
            benchmark_csv,
            benchmark_rows,
            [
                "item_id",
                "question_id",
                "variant_id",
                "question",
                "question_context",
                "options_json",
            ],
        ),
        (scoring_csv, scoring_rows, ["item_id", "correct_option_id", "answer_present"]),
    ):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({name: row.get(name) for name in fields})
    bundle = output_dir / "historical-prompt-bundle"
    templates = []
    for path, kind in [
        *((path, "template") for path in prompt_templates or []),
        *((path, "renderer") for path in historical_renderers or []),
    ]:
        destination = bundle / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        templates.append(
            {
                "path": str(destination.relative_to(output_dir)),
                "sha256": _sha256(destination),
                "historical": True,
                "kind": kind,
            }
        )
    files = {
        "reviewer_jsonl": reviewer,
        "reviewer_csv": reviewer_csv,
        "benchmark_jsonl": benchmark,
        "benchmark_csv": benchmark_csv,
        "scoring_jsonl": scoring,
        "scoring_csv": scoring_csv,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_export": {
            "export_id": source_manifest.get("export_id"),
            "manifest_sha256": _sha256(export_manifest),
            "mcq_sha256": source_manifest.get("file_sha256", {}).get("mcq"),
        },
        "shuffle_seed": seed,
        "reviewer_item_count": len(records),
        "benchmark_item_count": len(benchmark_rows),
        "files": {
            name: {"path": path.name, "sha256": _sha256(path)}
            for name, path in files.items()
        },
        "historical_prompt_templates": templates,
        "limitations": [
            "Rows are copied from the selected immutable MCQ export.",
            "No historical prompt template was asserted unless the caller supplied it explicitly.",
            "The package has no full papers, request bodies, costs, run IDs, timestamps, or release statuses.",
        ],
    }
    (output_dir / "manifest.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8"
    )
    return manifest


def export_publication_package(
    state_db: Path | None,
    output_dir: Path,
    *,
    run_id: str | None = None,
    seed: str,
    prompt_templates: list[Path] | None = None,
    historical_renderers: list[Path] | None = None,
    export_manifest: Path | None = None,
) -> dict[str, Any]:
    """Read a state database and write reviewer JSONL, CSV, and a hashed manifest."""
    if export_manifest is not None:
        return _write_manifest_package(
            export_manifest,
            state_db,
            output_dir,
            seed,
            prompt_templates,
            historical_renderers,
        )
    if state_db is None or run_id is None:
        raise ValueError(
            "--state-db and --run-id are required without --export-manifest"
        )
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
        templates.append(
            {
                "stage": path.stem,
                "path": str(destination.relative_to(output_dir)),
                "sha256": _sha256(destination),
            }
        )
    jsonl_path.write_text(
        "".join(canonical_json(row) + "\n" for row in records), encoding="utf-8"
    )
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fields = [
            "item_id",
            "question_id",
            "variant_id",
            "doi",
            "title",
            "selection_reason",
            "question",
            "question_context",
            "question_rationale",
            "reference_answer",
            "answer_selection_rationale",
            "answer_rationale",
            "reconstruction_rationale",
            "verification_rationale",
            "option_a",
            "option_a_generation_rationale",
            "option_a_rationale",
            "option_a_verdict",
            "option_b",
            "option_b_generation_rationale",
            "option_b_rationale",
            "option_b_verdict",
            "option_c",
            "option_c_generation_rationale",
            "option_c_rationale",
            "option_c_verdict",
            "option_d",
            "option_d_generation_rationale",
            "option_d_rationale",
            "option_d_verdict",
            "correct_option",
            "reference_answer_json",
            "decision_evidence_json",
            "options_json",
            "selection_json",
            "validation_json",
            "provenance_json",
            "rationale_availability_json",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in records:
            options = row["options"]
            readable_options = {}
            for index, letter in enumerate("abcd"):
                option = options[index]
                verdict = option.get("verdict") or {}
                readable_options[f"option_{letter}"] = option["text"]
                readable_options[f"option_{letter}_generation_rationale"] = (
                    option.get("distractor") or {}
                ).get("generation_rationale")
                readable_options[f"option_{letter}_rationale"] = verdict.get(
                    "rationale"
                )
                readable_options[f"option_{letter}_verdict"] = (
                    canonical_json(verdict) if verdict else None
                )
            writer.writerow(
                {
                    "item_id": row["item_id"],
                    "question_id": row["question_id"],
                    "variant_id": row["variant_id"],
                    "doi": row["paper"]["doi"],
                    "title": row["paper"]["title"],
                    "selection_reason": row["selection"]["inclusion_reason"],
                    "question": row["question"],
                    "question_context": row["question_context"],
                    "question_rationale": row["question_rationale"],
                    "reference_answer": row["reference_answer"].get("text"),
                    "answer_selection_rationale": row["rationales"]["answer_selection"],
                    "answer_rationale": row["rationales"]["answer_generation"],
                    "reconstruction_rationale": row["rationales"]["reconstruction"],
                    "verification_rationale": row["rationales"]["answer_verification"],
                    **readable_options,
                    "correct_option": next(
                        "abcd"[index]
                        for index, option in enumerate(options)
                        if option["is_correct"]
                    ),
                    "reference_answer_json": canonical_json(row["reference_answer"]),
                    "decision_evidence_json": canonical_json(row["decision_evidence"]),
                    "options_json": canonical_json(options),
                    "selection_json": canonical_json(row["selection"]),
                    "validation_json": canonical_json(row["validation"]),
                    "provenance_json": canonical_json(row["provenance"]),
                    "rationale_availability_json": canonical_json(
                        row["rationale_availability"]
                    ),
                }
            )
    benchmark = [
        {
            "item_id": row["item_id"],
            "question_id": row["question_id"],
            "variant_id": row["variant_id"],
            "question": row["question"],
            "question_context": row["question_context"],
            "options": [
                {
                    "option_id": option["option_id"],
                    "position": option["position"],
                    "text": option["text"],
                }
                for option in row["options"]
            ],
        }
        for row in records
    ]
    scoring = [
        {
            "item_id": row["item_id"],
            "correct_option_id": next(
                option["option_id"] for option in row["options"] if option["is_correct"]
            ),
        }
        for row in records
    ]
    benchmark_jsonl.write_text(
        "".join(canonical_json(row) + "\n" for row in benchmark), encoding="utf-8"
    )
    scoring_jsonl.write_text(
        "".join(canonical_json(row) + "\n" for row in scoring), encoding="utf-8"
    )
    for path, values, fields in (
        (
            benchmark_csv,
            benchmark,
            [
                "item_id",
                "question_id",
                "variant_id",
                "question",
                "question_context",
                "options_json",
            ],
        ),
        (scoring_csv, scoring, ["item_id", "correct_option_id"]),
    ):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for value in values:
                writer.writerow(
                    {
                        name: (
                            canonical_json(value["options"])
                            if name == "options_json" and "options" in value
                            else value.get(name)
                        )
                        for name in fields
                    }
                )
    files = {
        "reviewer_jsonl": jsonl_path,
        "reviewer_csv": csv_path,
        "benchmark_jsonl": benchmark_jsonl,
        "benchmark_csv": benchmark_csv,
        "scoring_jsonl": scoring_jsonl,
        "scoring_csv": scoring_csv,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "shuffle_seed": seed,
        "reviewer_item_count": len(records),
        "benchmark_item_count": len(benchmark),
        "files": {
            name: {"path": path.name, "sha256": _sha256(path)}
            for name, path in files.items()
        },
        "prompt_templates": templates,
        "limitations": [
            "Benchmark inputs contain no labels, rationales, evidence, or provenance.",
            "Reviewer files are not model-facing benchmark inputs.",
            "The package excludes full papers, extracted source blobs, and submitted prompts.",
            "Missing rationale fields remain explicit missing values.",
        ],
    }
    (output_dir / "manifest.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a reviewer publication package from immutable Arctic QA exports."
    )
    parser.add_argument("--export-manifest", type=Path)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--shuffle-seed", default="publication-review-v1")
    parser.add_argument("--prompt-template", type=Path, action="append", default=[])
    parser.add_argument("--historical-renderer", type=Path, action="append", default=[])
    parser.add_argument("--live-selection-file", type=Path)
    parser.add_argument("--live-prompt-file", type=Path, action="append", default=[])
    parser.add_argument("--poll-seconds", type=float)
    args = parser.parse_args(argv)
    if args.live_selection_file is not None:
        if args.state_db is None:
            parser.error("--state-db is required with --live-selection-file")
        if args.export_manifest is not None or args.run_id is not None:
            parser.error("live snapshots cannot select an export manifest or run ID")
        if args.poll_seconds is None:
            refresh_live_publication_snapshot(
                args.state_db,
                args.output_dir,
                selection_file=args.live_selection_file,
                seed=args.shuffle_seed,
                prompt_files=args.live_prompt_file,
            )
        else:
            poll_live_publication_snapshots(
                args.state_db,
                args.output_dir,
                selection_file=args.live_selection_file,
                seed=args.shuffle_seed,
                prompt_files=args.live_prompt_file,
                poll_seconds=args.poll_seconds,
            )
        return 0
    if args.export_manifest is None and (args.state_db is None or args.run_id is None):
        parser.error("--export-manifest or both --state-db and --run-id are required")
    export_publication_package(
        args.state_db,
        args.output_dir,
        run_id=args.run_id,
        seed=args.shuffle_seed,
        prompt_templates=args.prompt_template,
        historical_renderers=args.historical_renderer,
        export_manifest=args.export_manifest,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
