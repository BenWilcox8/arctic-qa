from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util import atomic_json, atomic_write, canonical_json, sha256_file


OVERLAY_SCHEMA = "arctic-geography-correction-overlay-v1"


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected one JSON object: {path}")
    return value


def write_geography_correction_overlay(
    *,
    affected_papers_file: Path,
    historical_jobs_dir: Path,
    old_policy_file: Path,
    new_policy_file: Path,
    output_dir: Path,
    decision_source: str,
    decision_at_utc: str,
) -> dict[str, Any]:
    if not decision_source.strip():
        raise ValueError("the correction decision source is required")
    try:
        observed = datetime.fromisoformat(decision_at_utc.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("the correction decision time is invalid") from error
    if observed.tzinfo is None:
        raise ValueError("the correction decision time needs a UTC offset")
    old_policy_sha256 = sha256_file(old_policy_file)
    new_policy_sha256 = sha256_file(new_policy_file)
    jobs_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(historical_jobs_dir.glob("*.json")):
        job = _read(path)
        if job.get("policy_sha256") != old_policy_sha256:
            continue
        candidate_key = job.get("candidate_key")
        if isinstance(candidate_key, str) and candidate_key:
            jobs_by_candidate.setdefault(candidate_key, []).append(job)
    rows: list[dict[str, Any]] = []
    with affected_papers_file.open(encoding="utf-8", newline="") as handle:
        for reviewed in csv.DictReader(handle):
            candidate_key = str(reviewed.get("doi") or "").strip()
            if not candidate_key:
                raise ValueError("an affected-paper row lacks a DOI")
            jobs = jobs_by_candidate.get(candidate_key, [])
            if len(jobs) != 1:
                raise ValueError(
                    "the correction overlay needs one historical job for each affected paper"
                )
            job = jobs[0]
            disposition = str(reviewed.get("recommended_disposition") or "").strip()
            locator = str(reviewed.get("evidence_locator") or "").strip()
            summary = str(reviewed.get("evidence_summary") or "").strip()
            if not disposition or not locator or not summary:
                raise ValueError("an affected-paper row lacks correction evidence")
            rows.append(
                {
                    "schema": OVERLAY_SCHEMA,
                    "authority": "reviewed_correction_proposal",
                    "model_decision": False,
                    "candidate_key": candidate_key,
                    "old_job_key": job["job_key"],
                    "old_policy_sha256": old_policy_sha256,
                    "old_prompt_sha256": job.get("prompt_sha256"),
                    "old_schema_sha256": job.get("schema_sha256"),
                    "source_content_hash": job["source_content_hash"],
                    "extraction_sha256": job["extraction_sha256"],
                    "old_validation_decision": (job.get("validation") or {}).get(
                        "decision"
                    ),
                    "proposed_disposition": disposition,
                    "scope_evidence": {
                        "locator": locator,
                        "summary": summary,
                    },
                    "new_policy_sha256": new_policy_sha256,
                    "decision_source": decision_source,
                    "decision_at_utc": observed.astimezone(UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
    if len(rows) != 16 or len({row["candidate_key"] for row in rows}) != len(rows):
        raise ValueError("the correction overlay requires 16 unique reviewed papers")
    output_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = output_dir / "geography-correction-overlay.ndjson"
    payload = b"".join((canonical_json(row) + "\n").encode() for row in rows)
    atomic_write(overlay_path, payload, immutable=True)
    manifest = {
        "schema": "arctic-geography-correction-overlay-manifest-v1",
        "overlay": overlay_path.name,
        "overlay_sha256": sha256_file(overlay_path),
        "rows": len(rows),
        "old_policy_sha256": old_policy_sha256,
        "new_policy_sha256": new_policy_sha256,
        "decision_source": decision_source,
        "decision_at_utc": rows[0]["decision_at_utc"],
    }
    atomic_json(output_dir / "geography-correction-overlay-manifest.json", manifest, immutable=True)
    return {**manifest, "output_dir": str(output_dir.resolve())}
