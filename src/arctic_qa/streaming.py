from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .db import Database, now
from .discovery import manual_record
from .exporting import export_run
from .extraction import extract_source
from .generation import generate_candidate
from .providers import Provider
from .storage import store_original
from .util import canonical_json, sha256_file, stable_id
from .validation import validate_candidate


def run_stream(
    db: Database,
    namespace: Path,
    *,
    run_id: str,
    campaign_id: str,
    access_run_dir: Path,
    eligibility_run_dir: Path,
    author: Provider,
    verifier: Provider,
    max_papers: int,
) -> dict[str, Any]:
    if not 1 <= max_papers <= 500:
        raise ValueError("max papers must be between 1 and 500")
    access_manifest = _read(access_run_dir / "run-manifest.json")
    if _read(access_run_dir / "progress.json").get("state") != "completed":
        raise ValueError("the article-access run is not complete")
    if not (access_run_dir / "run-receipt.json").is_file():
        raise ValueError("the article-access completion receipt is missing")
    access_items = {
        item["candidate_key"]: item
        for item in (
            _read(path) for path in sorted((access_run_dir / "items").glob("*.json"))
        )
    }
    eligibility_jobs = {
        item["candidate_key"]: item
        for item in (
            _read(path)
            for path in sorted((eligibility_run_dir / "jobs").glob("*.json"))
        )
    }
    selection = access_manifest.get("selection")
    if not isinstance(selection, list):
        raise ValueError("the article-access selection is missing")
    if len(selection) != access_manifest.get("target_total"):
        raise ValueError("the ordered selection count is inconsistent")
    for position, selected in enumerate(selection, start=1):
        access = access_items.get(selected.get("candidate_key"))
        if (
            access is None
            or selected.get("position") != position
            or access.get("position") != position
            or access.get("run_id") != access_manifest.get("run_id")
            or access.get("subgroup") != selected.get("subgroup")
        ):
            raise ValueError("the ordered selection does not match its access item")
    counts = {
        "accepted_base_questions": 0,
        "eligibility_rejected": 0,
        "generation_rejected": 0,
        "processed": 0,
    }
    paper_results: list[dict[str, Any]] = []
    resumed_papers = 0
    for selected in selection:
        if counts["processed"] >= max_papers:
            break
        candidate_key = selected.get("candidate_key")
        access = access_items.get(candidate_key)
        eligibility = eligibility_jobs.get(candidate_key)
        if access is None or eligibility is None:
            continue
        _validate_pair(access, eligibility)
        decision = eligibility["validation"]["decision"]
        if decision != "eligible":
            reason_codes = eligibility["parsed_response"].get(
                "overall_reason_codes", ["eligibility_unresolved"]
            )
            for reason_code in reason_codes:
                with db.transaction():
                    db.connection.execute(
                        """INSERT OR IGNORE INTO rejection_ledger
                        (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
                        VALUES (?,NULL,NULL,'scientific_eligibility',?,?,?)""",
                        (
                            stable_id(
                                "rejection",
                                campaign_id,
                                candidate_key,
                                "scientific_eligibility",
                                reason_code,
                            ),
                            reason_code,
                            canonical_json(
                                {
                                    "candidate_key": candidate_key,
                                    "eligibility_job_key": eligibility["job_key"],
                                    "selection": selected,
                                }
                            ),
                            now(),
                        ),
                    )
            paper_results.append(
                {
                    "candidate_key": candidate_key,
                    "disposition": "eligibility_rejected",
                    "reason_codes": reason_codes,
                    "source_id": None,
                }
            )
            counts["eligibility_rejected"] += 1
            counts["processed"] += 1
            continue
        source_id = _import_source(db, namespace, access, selected, eligibility)
        if db.one(
            "SELECT item_id FROM candidates WHERE run_id=? AND source_id=? LIMIT 1",
            (campaign_id, source_id),
        ):
            resumed_papers += 1
        candidate = generate_candidate(
            db,
            namespace,
            source_id=source_id,
            run_id=campaign_id,
            arm="answer_first",
            author=author,
            verifier=verifier,
            budget_mode="tokens",
            budget_limit=Decimal("1000000"),
            reservation=Decimal("100"),
            timeout=30,
            retries=0,
            rate_limit_seconds=0,
        )
        validation = validate_candidate(db, namespace, candidate).as_dict()
        if validation["final_label"] == "machine_accepted_unverified":
            counts["accepted_base_questions"] += 1
            disposition = "accepted"
        else:
            counts["generation_rejected"] += 1
            disposition = "generation_rejected"
        paper_results.append(
            {
                "candidate_key": candidate_key,
                "disposition": disposition,
                "reason_codes": validation["reasons"],
                "source_id": source_id,
            }
        )
        counts["processed"] += 1
    exported = export_run(db, namespace, campaign_id, seed="streaming-20260912")
    same_model_roles = author.name == verifier.name and author.model == verifier.model
    return {
        "state": "completed",
        "run_id": run_id,
        "campaign_id": campaign_id,
        "counts": counts,
        "paper_results": paper_results,
        "resumed_papers": resumed_papers,
        "export": exported,
        "provider_policy": {
            "model": author.model,
            "same_model_roles": same_model_roles,
            "correlated_error_disclosed": same_model_roles,
            "live_provider": False,
        },
    }


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected one JSON object: {path}")
    return value


def _validate_pair(access: dict[str, Any], eligibility: dict[str, Any]) -> None:
    if (
        access.get("schema") != "article-access-item-v1"
        or access.get("access_state") != "full_text_ready"
        or access.get("identity_verified") is not True
    ):
        raise ValueError("the article-access item is not full-text ready")
    if (
        eligibility.get("schema") != "gemini-eligibility-job-v1"
        or eligibility.get("state") != "completed"
        or (eligibility.get("validation") or {}).get("valid") is not True
    ):
        raise ValueError("the Gemini eligibility receipt is not valid and complete")
    decision = (eligibility.get("validation") or {}).get("decision")
    overall = (eligibility.get("parsed_response") or {}).get("overall")
    if decision not in {"eligible", "excluded", "uncertain"} or overall != decision:
        raise ValueError("the Gemini eligibility decision is inconsistent")
    if (
        access.get("candidate_key") != eligibility.get("candidate_key")
        or access.get("source_content_hash") != eligibility.get("source_content_hash")
        or access.get("extraction_sha256") != eligibility.get("extraction_sha256")
    ):
        raise ValueError("the access and eligibility receipts do not match")
    source_path = Path(str(access.get("source_path") or ""))
    extraction_path = Path(str(access.get("extraction_path") or ""))
    if not source_path.is_file() or sha256_file(source_path) != access.get(
        "source_content_hash"
    ):
        raise ValueError("the ready source object is missing or changed")
    if not extraction_path.is_file() or sha256_file(extraction_path) != access.get(
        "extraction_sha256"
    ):
        raise ValueError("the ready extraction is missing or changed")


def _import_source(
    db: Database,
    namespace: Path,
    access: dict[str, Any],
    selected: dict[str, Any],
    eligibility: dict[str, Any],
) -> str:
    source_path = Path(access["source_path"])
    metadata = {
        "stable_id": access["candidate_key"],
        "doi": access.get("doi"),
        "title": access["title"],
        "authors": access.get("authors", []),
        "published_date": access.get("published_date"),
        "year": access.get("year"),
        "discipline": access.get("discipline"),
        "source_version": access.get("source_version"),
        "retrieval_url": access.get("final_url"),
        "license": access.get("license"),
        "paper_family_id": access.get("paper_family_id"),
        "query": "streaming eligibility bridge",
        "selection": selected,
        "eligibility_job_key": eligibility["job_key"],
        "eligibility_model": eligibility.get("model"),
    }
    source = manual_record(metadata, "streaming_bridge")
    db.upsert_source(source)
    existing = db.one(
        """SELECT relative_path FROM artifacts
        WHERE source_id=? AND kind='original' AND content_hash=?""",
        (source["source_id"], access["source_content_hash"]),
    )
    if existing:
        if (
            sha256_file(namespace / existing["relative_path"])
            != access["source_content_hash"]
        ):
            raise ValueError("the stored source object changed")
    else:
        stored = store_original(
            db,
            namespace,
            source["source_id"],
            source_path.read_bytes(),
            access["media_type"],
            access["final_url"],
        )
        if stored["sha256"] != access["source_content_hash"]:
            raise ValueError(
                "the imported source hash does not match the access receipt"
            )
    extract_source(db, namespace, source["source_id"])
    with db.transaction():
        db.connection.execute(
            """UPDATE sources
            SET eligibility_state='eligible',geography_state='core_arctic',
                geography_confidence='model_reviewed_unverified',
                inclusion_reason='validated_gemini_full_text_eligibility',updated_at=?
            WHERE source_id=?""",
            (now(), source["source_id"]),
        )
    return source["source_id"]
