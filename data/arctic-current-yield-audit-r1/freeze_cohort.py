#!/usr/bin/env python3
"""Freeze the bounded current-yield audit cohort from canonical records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


CUTOFF = "2026-09-14T08:33:02+00:00"
INVOCATION = "first-production-81b1976-live-rerun-r2"
CAMPAIGN = "arctic-qa-production-campaign-001"
DB_URI = "file:/mnt/crdata/research-abstention/arctic-qa/state.sqlite3?mode=ro"
ROOT = Path("/mnt/crdata/research-abstention/arctic-qa")
STREAM = ROOT / "streaming-dataset-r1"
JOB_DIR = ROOT / "gemini-eligibility-r1" / INVOCATION / "jobs"
RUN_MANIFEST = (
    STREAM
    / "production-campaign-r1/live-jsonl-rerun-r2/materialized-rerun-first-800/run-manifest.json"
)
INVOCATION_MANIFEST = STREAM / "runs/stream-invocation-f652115008bb83225ad1/run-manifest.json"
RECEIPT_DIR = STREAM / "model-receipts"
OUTPUT_DIR = Path(
    "/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-current-yield-audit-r1"
)


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def public_source(row: sqlite3.Row | None, selection: dict) -> dict:
    if row is None:
        return {
            "source_id": None,
            "stable_id": selection["candidate_key"],
            "doi": selection.get("doi"),
            "title": selection["title"],
            "paper_family_id": selection["paper_family_id"],
            "source_version": selection["source_content_hash"],
            "content_hash": selection["source_content_hash"],
            "scope_evidence": None,
        }
    result = dict(row)
    result["scope_evidence"] = json.loads(result.pop("scope_evidence_json"))
    return result


def main() -> None:
    access_manifest = load(RUN_MANIFEST)
    selection_by_key = {item["candidate_key"]: item for item in access_manifest["selection"]}

    jobs = []
    for path in sorted(JOB_DIR.glob("*.json")):
        job = load(path)
        if job.get("state") not in {"completed", "screening_error"}:
            continue
        if timestamp(job["completed_at_utc"]) > timestamp(CUTOFF):
            continue
        job["_path"] = str(path)
        job["_sha256"] = sha256_bytes(path.read_bytes())
        jobs.append(job)
    jobs.sort(key=lambda item: (item["completed_at_utc"], item["job_key"]))

    receipts_by_family: dict[str, list[dict]] = {}
    for path in sorted(RECEIPT_DIR.glob("*.json")):
        try:
            receipt = load(path)
        except (OSError, json.JSONDecodeError):
            continue
        if receipt.get("run_id") != INVOCATION:
            continue
        submitted = receipt.get("submitted_at_utc", "")
        if submitted and timestamp(submitted) > timestamp(CUTOFF):
            continue
        family = receipt.get("family_id")
        if not family:
            continue
        receipts_by_family.setdefault(family, []).append(
            {
                "file": path.name,
                "file_sha256": sha256_bytes(path.read_bytes()),
                "stage": receipt.get("stage"),
                "state": receipt.get("state"),
                "request_key": receipt.get("request_key"),
                "request_sha256": receipt.get("request_sha256"),
                "source_version_id": receipt.get("source_version_id"),
                "submitted_at_utc": submitted,
                "completed_at_utc": receipt.get("completed_at_utc"),
            }
        )

    db = sqlite3.connect(DB_URI, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    schema_version = db.execute("SELECT version FROM schema_info").fetchone()[0]
    decisions = []
    accepted = []
    for cohort_index, job in enumerate(jobs, start=1):
        key = job["candidate_key"]
        selection = selection_by_key[key]
        family = selection["paper_family_id"]
        source_row = db.execute(
            "SELECT source_id,stable_id,doi,title,paper_family_id,source_version,content_hash,"
            "scope_evidence_json,eligibility_state,geography_state,scope_rule_version,updated_at "
            "FROM sources WHERE paper_family_id=? OR stable_id=? OR doi=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (family, key, key),
        ).fetchone()
        source = public_source(source_row, selection)
        source_id = source.get("source_id")
        candidate_rows = db.execute(
            "SELECT * FROM candidates WHERE run_id=? AND paper_family_id=? AND created_at<=? "
            "ORDER BY created_at,item_id",
            (CAMPAIGN, family, CUTOFF),
        ).fetchall()
        current = []
        history = []
        for row in candidate_rows:
            payload = json.loads(row["candidate_json"])
            provenance = payload.get("provenance", {})
            summary = {
                "item_id": row["item_id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "schema_version": payload.get("schema_version"),
                "prompt_version": provenance.get("prompt_version"),
                "scope_contract_version": provenance.get("scope_contract_version"),
                "candidate_sha256": sha256_bytes(canonical(payload)),
            }
            if (
                payload.get("schema_version") == "2.2.0"
                and provenance.get("prompt_version") == "arctic-qa-generation-v16"
                and provenance.get("scope_contract_version") == "selected-evidence-literal-scope-v4"
            ):
                current.append((row, payload, summary))
            else:
                history.append(summary)
        current.sort(key=lambda item: (item[0]["created_at"], item[0]["item_id"]))
        current_row = current[-1][0] if current else None
        current_payload = current[-1][1] if current else None
        current_summary = current[-1][2] if current else None

        validation = job.get("validation", {})
        eligibility_decision = validation.get("decision", "unresolved")
        source_rejections = []
        if source_id:
            source_rejections = [
                dict(row)
                for row in db.execute(
                    "SELECT rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at "
                    "FROM rejection_ledger WHERE source_id=? AND created_at>=? AND created_at<=? "
                    "ORDER BY created_at,rejection_id",
                    (source_id, job["completed_at_utc"], CUTOFF),
                )
            ]
            for rejection in source_rejections:
                rejection["detail"] = json.loads(rejection.pop("detail_json"))
        if eligibility_decision == "eligible" and current_row is not None:
            final_state = (
                "generation_rejected" if current_row["status"] == "rejected" else current_row["status"]
            )
            reasons = current_payload.get("qa_gate_reasons", [])
            final_reason = reasons[0] if reasons else final_state
        elif eligibility_decision == "eligible" and source_rejections:
            final_state = "generation_rejected"
            final_reason = source_rejections[-1]["reason_code"]
        elif eligibility_decision == "eligible":
            final_state = "operational_unresolved"
            final_reason = "eligible_without_current_candidate_at_cutoff"
        elif eligibility_decision == "excluded":
            final_state = "scientific_exclusion"
            codes = validation.get("overall_reason_codes", [])
            final_reason = codes[0] if codes else "excluded"
        else:
            final_state = "eligibility_unresolved"
            codes = validation.get("errors", []) or validation.get("overall_reason_codes", [])
            final_reason = codes[0] if codes else "unresolved"

        if current_row is not None:
            item_id = current_row["item_id"]
            rejections = [
                dict(row)
                for row in db.execute(
                    "SELECT rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at "
                    "FROM rejection_ledger WHERE item_id=? ORDER BY created_at,rejection_id",
                    (item_id,),
                )
            ]
            for rejection in rejections:
                rejection["detail"] = json.loads(rejection.pop("detail_json"))
            validations = [
                dict(row)
                for row in db.execute(
                    "SELECT event_id,item_id,stage,label,reason_codes_json,details_json,created_at "
                    "FROM validation_events WHERE item_id=? ORDER BY created_at,event_id",
                    (item_id,),
                )
            ]
            for event in validations:
                event["reason_codes"] = json.loads(event.pop("reason_codes_json"))
                event["details"] = json.loads(event.pop("details_json"))
        else:
            rejections = source_rejections
            validations = []

        finding_row = db.execute(
            "SELECT finding_id,chunk_id,selection_policy_version,answer_json,status,created_at "
            "FROM findings WHERE run_id=? AND paper_family_id=? AND created_at<=? "
            "ORDER BY created_at DESC LIMIT 1",
            (CAMPAIGN, family, CUTOFF),
        ).fetchone()
        current_finding = dict(finding_row) if finding_row else None
        if current_finding:
            current_finding["answer"] = json.loads(current_finding.pop("answer_json"))

        record = {
            "record_type": "decision",
            "cohort_index": cohort_index,
            "cutoff_utc": CUTOFF,
            "invocation_run_id": INVOCATION,
            "campaign_id": CAMPAIGN,
            "access_selection": selection,
            "source": source,
            "eligibility": {
                "job_key": job["job_key"],
                "job_path": job["_path"],
                "job_sha256": job["_sha256"],
                "completed_at_utc": job["completed_at_utc"],
                "source_content_hash": job["source_content_hash"],
                "extraction_sha256": job["extraction_sha256"],
                "prompt_sha256": job["prompt_sha256"],
                "schema_sha256": job["schema_sha256"],
                "broker_request_key": job.get("broker_request_key"),
                "broker_receipt_sha256": job.get("broker_receipt_sha256"),
                "parsed_response": job.get("parsed_response"),
                "validation": validation,
            },
            "final_state": final_state,
            "final_reason": final_reason,
            "current_candidate": current_payload,
            "current_candidate_binding": current_summary,
            "current_finding": current_finding,
            "historical_candidate_versions": history,
            "rejection_events": rejections,
            "validation_events": validations,
            "model_receipt_bindings": sorted(
                receipts_by_family.get(family, []),
                key=lambda item: (item.get("submitted_at_utc") or "", item["file"]),
            ),
        }
        if final_state == "machine_accepted_unverified":
            accepted.append(record)
        else:
            decisions.append(record)
    db.rollback()
    db.close()

    counts = Counter(record["final_state"] for record in decisions + accepted)
    header = {
        "record_type": "cohort",
        "schema": "arctic-current-yield-audit-cohort-v1",
        "capture_cutoff_utc": CUTOFF,
        "invocation_run_id": INVOCATION,
        "campaign_id": CAMPAIGN,
        "generation_prompt_version": "arctic-qa-generation-v16",
        "scope_contract_version": "selected-evidence-literal-scope-v4",
        "candidate_schema_version": "2.2.0",
        "sqlite_schema_version": schema_version,
        "invocation_manifest_path": str(INVOCATION_MANIFEST),
        "invocation_manifest_sha256": sha256_bytes(INVOCATION_MANIFEST.read_bytes()),
        "access_manifest_path": str(RUN_MANIFEST),
        "access_manifest_sha256": sha256_bytes(RUN_MANIFEST.read_bytes()),
        "complete_eligibility_decisions": len(jobs),
        "review_decisions": len(decisions),
        "accepted_not_in_review_denominator": len(accepted),
        "derived_counts": dict(sorted(counts.items())),
        "terminal_errors_in_latest_decisions": sum(
            record["final_state"] == "operational_unresolved" for record in decisions
        ),
        "superseded_process_error_note": (
            "The 07:58 UTC process ValueError was an operational stop, not a scientific rejection. "
            "The recovery completed that paper before this cutoff."
        ),
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cohort_path = OUTPUT_DIR / "cohort.jsonl"
    lines = [canonical(header)] + [canonical(record) for record in decisions]
    cohort_path.write_bytes(b"\n".join(lines) + b"\n")
    (OUTPUT_DIR / "cohort.sha256").write_text(
        f"{sha256_bytes(cohort_path.read_bytes())}  cohort.jsonl\n"
    )
    summary = {
        "header": header,
        "review_ids": [record["access_selection"]["candidate_key"] for record in decisions],
        "accepted_ids": [record["access_selection"]["candidate_key"] for record in accepted],
    }
    (OUTPUT_DIR / "cohort-summary.json").write_bytes(canonical(summary) + b"\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
