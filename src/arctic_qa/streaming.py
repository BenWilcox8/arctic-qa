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
from .gemini_eligibility import (
    _correction_metadata,
    _job_key,
    _known_context_gaps,
    _request_payload,
    _segments,
    validate_response,
)
from .providers import Provider, call_provider
from .storage import store_original
from .util import atomic_json, canonical_json, sha256_file, stable_id
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
    progress_file: Path | None = None,
    eligibility_prompt_file: Path | None = None,
    eligibility_schema_file: Path | None = None,
    eligibility_policy_file: Path | None = None,
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
    progress = _Progress(
        progress_file or namespace / "streaming-dataset-r1" / "progress.json",
        run_id=run_id,
        counts={
            "full_text_ready": len(access_items),
            "eligible": sum(
                (item.get("validation") or {}).get("decision") == "eligible"
                for item in eligibility_jobs.values()
            ),
            "rejected": sum(
                (item.get("validation") or {}).get("decision")
                in {"excluded", "uncertain"}
                for item in eligibility_jobs.values()
            ),
            "accepted_qa": _accepted_count(db, campaign_id),
        },
    )
    progress.write("running", "eligibility", "Streaming pipeline started.")
    counts = {
        "accepted_base_questions": 0,
        "eligibility_rejected": 0,
        "generation_rejected": 0,
        "incomplete_non_mcq": 0,
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
        if access is None:
            continue
        paper_id = str(candidate_key)
        family_id = str(
            access.get("paper_family_id")
            or stable_id("family", access.get("doi") or candidate_key)
        )
        source_version_id = str(access["source_content_hash"])
        paper_author = _bind_provider(
            author,
            paper_id=paper_id,
            family_id=family_id,
            source_version_id=source_version_id,
        )
        paper_verifier = _bind_provider(
            verifier,
            paper_id=paper_id,
            family_id=family_id,
            source_version_id=source_version_id,
        )
        if eligibility is None:
            if not all(
                (
                    eligibility_prompt_file,
                    eligibility_schema_file,
                    eligibility_policy_file,
                )
            ):
                raise ValueError(
                    "streaming eligibility inputs are required for a newly ready paper"
                )
            eligibility = _run_eligibility(
                db,
                access,
                eligibility_run_dir,
                run_id=campaign_id,
                provider=paper_verifier,
                prompt_file=eligibility_prompt_file,
                schema_file=eligibility_schema_file,
                policy_file=eligibility_policy_file,
            )
            eligibility_jobs[candidate_key] = eligibility
            progress.set_count(
                "eligible",
                sum(
                    (item.get("validation") or {}).get("decision") == "eligible"
                    for item in eligibility_jobs.values()
                ),
            )
            progress.set_count(
                "rejected",
                sum(
                    (item.get("validation") or {}).get("decision")
                    in {"excluded", "uncertain"}
                    for item in eligibility_jobs.values()
                ),
            )
        progress.write("running", "eligibility", f"Checking {candidate_key}.")
        try:
            _validate_pair(access, eligibility)
        except Exception as error:
            progress.error(candidate_key, access.get("title"), "eligibility", error)
            raise
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
            progress.paper(
                paper_id=candidate_key,
                title=access.get("title"),
                current_stage="completed",
                final_state="rejected",
                final_reason=reason_codes[0],
            )
            continue
        try:
            source_id = _import_source(
                db,
                namespace,
                access,
                selected,
                eligibility,
                family_id=family_id,
            )
        except Exception as error:
            progress.error(candidate_key, access.get("title"), "source_import", error)
            raise
        if db.one(
            "SELECT item_id FROM candidates WHERE run_id=? AND source_id=? LIMIT 1",
            (campaign_id, source_id),
        ):
            resumed_papers += 1
        progress.paper(
            paper_id=source_id,
            title=access.get("title"),
            current_stage="generation",
        )
        try:
            candidate = generate_candidate(
                db,
                namespace,
                source_id=source_id,
                run_id=campaign_id,
                arm="answer_first",
                author=paper_author,
                verifier=paper_verifier,
                budget_mode="tokens",
                budget_limit=Decimal("1000000"),
                reservation=Decimal("100"),
                timeout=30,
                retries=0,
                rate_limit_seconds=0,
            )
        except Exception as error:
            progress.error(source_id, access.get("title"), "generation", error)
            raise
        try:
            validation = validate_candidate(db, namespace, candidate).as_dict()
        except Exception as error:
            progress.error(source_id, access.get("title"), "validation", error)
            raise
        if (
            validation["final_label"] == "machine_accepted_unverified"
            and validation["labels"]["mcq_eligible"]
        ):
            if hasattr(paper_author, "record_accepted"):
                paper_author.record_accepted(
                    family_id=family_id, item_id=candidate["item_id"]
                )
            counts["accepted_base_questions"] += 1
            disposition = "accepted"
        elif validation["final_label"] == "machine_accepted_unverified":
            with db.transaction():
                db.connection.execute(
                    "UPDATE candidates SET status='incomplete_non_mcq',updated_at=? WHERE item_id=?",
                    (now(), candidate["item_id"]),
                )
            counts["incomplete_non_mcq"] += 1
            disposition = "incomplete_non_mcq"
        else:
            counts["generation_rejected"] += 1
            disposition = "generation_rejected"
            progress.increment("rejected")
        paper_results.append(
            {
                "candidate_key": candidate_key,
                "disposition": disposition,
                "reason_codes": validation["reasons"],
                "source_id": source_id,
            }
        )
        counts["processed"] += 1
        if disposition == "accepted":
            progress.set_count("accepted_qa", _accepted_count(db, campaign_id))
        progress.paper(
            paper_id=source_id,
            title=access.get("title"),
            current_stage="completed",
            final_state=disposition,
            final_reason=(
                validation["final_label"]
                if disposition == "accepted"
                else (validation["reasons"] or ["validation_rejected"])[0]
            ),
        )
    progress.write("running", "export", "Writing validated dataset exports.")
    try:
        exported = export_run(db, namespace, campaign_id, seed="streaming-20260912")
    except Exception as error:
        progress.write(
            "error", "export", f"Streaming stopped on {type(error).__name__}."
        )
        raise
    same_model_roles = author.name == verifier.name and author.model == verifier.model
    live_provider = bool(getattr(author, "externally_metered", False))
    result = {
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
            "live_provider": live_provider,
        },
    }
    progress.write("completed", "completed", "Streaming pipeline completed.")
    return result


def _bind_provider(
    provider: Provider,
    *,
    paper_id: str,
    family_id: str,
    source_version_id: str,
) -> Provider:
    bind = getattr(provider, "bind", None)
    if bind is None:
        return provider
    return bind(
        paper_id=paper_id,
        family_id=family_id,
        source_version_id=source_version_id,
    )


def _accepted_count(db: Database, run_id: str) -> int:
    row = db.one(
        """SELECT COUNT(DISTINCT paper_family_id) AS count FROM candidates
        WHERE run_id=? AND status='machine_accepted_unverified'""",
        (run_id,),
    )
    return int(row["count"])


class _Progress:
    def __init__(self, path: Path, *, run_id: str, counts: dict[str, int]) -> None:
        self.path = path.resolve()
        self.run_id = run_id
        self.counts = counts
        self.recent: list[dict[str, Any]] = []

    def write(self, state: str, stage: str, message: str) -> None:
        atomic_json(
            self.path,
            {
                "schema": "streaming-dataset-progress-v1",
                "state": state,
                "run_id": self.run_id,
                "current_stage": stage,
                "updated_at_utc": now(),
                "message": message,
                "counts": self.counts,
                "recent_papers": self.recent[-100:],
            },
        )

    def increment(self, name: str) -> None:
        self.counts[name] = int(self.counts.get(name, 0)) + 1

    def set_count(self, name: str, value: int) -> None:
        self.counts[name] = value

    def paper(
        self,
        *,
        paper_id: str,
        title: str | None,
        current_stage: str,
        final_state: str | None = None,
        final_reason: str | None = None,
    ) -> None:
        row = {
            "paper_id": paper_id,
            "title": title,
            "current_stage": current_stage,
            "final_state": final_state,
            "final_reason": final_reason,
        }
        self.recent = [
            old for old in self.recent if old.get("paper_id") != paper_id
        ] + [row]
        self.write("running", current_stage, f"Processing {paper_id}.")

    def error(
        self,
        paper_id: str,
        title: str | None,
        stage: str,
        error: Exception,
    ) -> None:
        self.paper(
            paper_id=paper_id,
            title=title,
            current_stage=stage,
            final_state="error",
            final_reason=type(error).__name__,
        )
        self.write("error", stage, f"Streaming stopped on {type(error).__name__}.")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected one JSON object: {path}")
    return value


def _run_eligibility(
    db: Database,
    access: dict[str, Any],
    run_dir: Path,
    *,
    run_id: str,
    provider: Provider,
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
) -> dict[str, Any]:
    broker = getattr(provider, "broker", None)
    if broker is None:
        raise ValueError("new eligibility calls require the shared broker")
    config = broker.config
    prompt = prompt_file.read_text(encoding="utf-8")
    schema = _read(schema_file)
    policy = _read(policy_file)
    text = Path(access["extraction_path"]).read_text(encoding="utf-8")
    job_key = _job_key(access, config, prompt_file, schema_file, policy_file)
    request, hashes = _request_payload(
        source=access,
        policy=policy,
        text=text,
        prompt=prompt,
        schema=schema,
        config=config,
        request_id=job_key,
        policy_sha256=sha256_file(policy_file),
    )
    generation = request["generationConfig"]
    result = call_provider(
        db,
        provider,
        run_id=run_id,
        entity_id=stable_id("eligibility", access["candidate_key"], job_key),
        role="eligibility",
        system=request["systemInstruction"]["parts"][0]["text"],
        prompt=request["contents"][0]["parts"][0]["text"],
        prompt_version="gemini-eligibility-prompt-v1",
        parameters={
            "temperature": generation.get("temperature", 0),
            "max_tokens": generation["maxOutputTokens"],
            "json_schema": schema,
        },
        response_schema=schema,
        reservation=Decimal("0"),
        timeout=120,
        retries=0,
        rate_limit_seconds=0,
    )
    validation = validate_response(
        result.payload,
        _segments(text),
        expected={
            "request_id": job_key,
            "input_echo": hashes,
            "correction_metadata": _correction_metadata(access),
            "known_context_gaps": _known_context_gaps(access),
        },
        response_schema=schema,
    )
    job = {
        "schema": "gemini-eligibility-job-v1",
        "job_key": job_key,
        "candidate_key": access["candidate_key"],
        "model": provider.model,
        "model_version": result.returned_model,
        "response_id": result.request_id,
        "state": "completed" if validation["valid"] else "screening_error",
        "source_content_hash": access["source_content_hash"],
        "extraction_sha256": access["extraction_sha256"],
        "policy_sha256": sha256_file(policy_file),
        "prompt_sha256": sha256_file(prompt_file),
        "schema_sha256": sha256_file(schema_file),
        "parsed_response": result.payload,
        "validation": validation,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "actual_cost_usd": (
            str(result.actual_cost_usd) if result.actual_cost_usd is not None else None
        ),
        "completed_at_utc": now(),
    }
    atomic_json(run_dir / "jobs" / f"{job_key}.json", job, immutable=True)
    return job


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
    parsed = eligibility.get("parsed_response") or {}
    criteria = parsed.get("criteria")
    if (
        parsed.get("request_id") != eligibility.get("job_key")
        or not isinstance(criteria, list)
        or {row.get("criterion_id") for row in criteria if isinstance(row, dict)}
        != {
            "published_primary_findings",
            "stable_identity_version",
            "study_geography",
            "access_rights_evidence",
            "correction_retraction_coverage",
        }
    ):
        raise ValueError("the Gemini eligibility evidence shape is invalid")
    geography = next(
        (
            row
            for row in criteria
            if isinstance(row, dict) and row.get("criterion_id") == "study_geography"
        ),
        {},
    )
    if decision == "eligible" and (
        geography.get("status") != "satisfied" or not geography.get("evidence")
    ):
        raise ValueError("eligible geography lacks located evidence")
    echo = parsed.get("input_echo") or {}
    if echo.get("source_version_sha256") != access.get(
        "source_content_hash"
    ) or echo.get("extracted_text_sha256") != access.get("extraction_sha256"):
        raise ValueError("the Gemini eligibility input hashes do not match")
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
    *,
    family_id: str,
) -> str:
    source_path = Path(access["source_path"])
    metadata = {
        "stable_id": access["candidate_key"],
        "doi": access.get("doi"),
        "title": access["title"],
        "authors": access.get("authors") or selected.get("authors") or [],
        "published_date": access.get("published_date")
        or selected.get("published_date"),
        "year": access.get("year") or selected.get("year"),
        "discipline": access.get("discipline")
        or selected.get("discipline")
        or "unclassified",
        "source_version": access.get("source_version")
        or access["source_content_hash"],
        "retrieval_url": access.get("final_url"),
        "license": access.get("license"),
        "paper_family_id": family_id,
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
    parsed = eligibility["parsed_response"]
    geography = next(
        row for row in parsed["criteria"] if row["criterion_id"] == "study_geography"
    )
    scope_evidence = {
        "verification_label": "model_reviewed_unverified",
        "eligibility_job_key": eligibility["job_key"],
        "eligibility_model": eligibility.get("model"),
        "returned_model": eligibility.get("model_version"),
        "response_id": eligibility.get("response_id"),
        "decision": eligibility["validation"]["decision"],
        "overall_reason_codes": parsed["overall_reason_codes"],
        "study_geography": geography,
        "resolved_evidence": [
            row
            for row in eligibility["validation"].get("resolved_evidence", [])
            if row.get("criterion") == "study_geography"
        ],
        "known_missing_context": parsed.get("known_missing_context", []),
        "correction_metadata_used": parsed.get("correction_metadata_used"),
        "input_echo": parsed.get("input_echo"),
    }
    with db.transaction():
        db.connection.execute(
            """UPDATE sources
            SET eligibility_state='eligible',geography_state='core_arctic',
                geography_confidence='model_reviewed_unverified',
                inclusion_reason='gemini_full_text_eligibility_with_located_evidence',
                scope_rule_version='gemini-fulltext-arctic-eligibility-v1',
                scope_evidence_json=?,updated_at=?
            WHERE source_id=?""",
            (canonical_json(scope_evidence), now(), source["source_id"]),
        )
    return source["source_id"]
