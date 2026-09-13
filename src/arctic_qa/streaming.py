from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .db import Database, now
from .discovery import manual_record
from .errors import CandidateRejectedError
from .exporting import export_run
from .extraction import extract_source
from .generation import generate_candidate
from .gemini_eligibility import (
    ELIGIBILITY_RESPONSE_V2,
    ELIGIBILITY_STATUS_MAPPING_VERSION,
    _correction_metadata,
    _job_key,
    _known_context_gaps,
    _persist_span_manifest_v2,
    _request_payload,
    _span_blocks_v2,
    _span_manifest_v2,
    _validation_evidence,
    validate_response,
)
from .providers import Provider, call_provider
from .model_broker import broker_request_key
from .storage import store_original
from .util import atomic_json, canonical_json, sha256_bytes, sha256_file, stable_id
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
    if max_papers < 1:
        raise ValueError("max papers must be at least 1")
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
    eligibility_jobs: dict[str, dict[str, Any]] = {}
    for item in (
        _read(path) for path in sorted((eligibility_run_dir / "jobs").glob("*.json"))
    ):
        key = item["candidate_key"]
        if key not in eligibility_jobs or item.get("execution_authority") == (
            "shared_gemini_broker"
        ):
            eligibility_jobs[key] = item
    selection = access_manifest.get("selection")
    if not isinstance(selection, list):
        raise ValueError("the article-access selection is missing")
    if len(selection) != access_manifest.get("target_total"):
        raise ValueError("the ordered selection count is inconsistent")
    if max_papers > len(selection):
        raise ValueError("max papers cannot exceed the ordered selection count")
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
    author_broker = getattr(author, "broker", None)
    verifier_broker = getattr(verifier, "broker", None)
    if author_broker is not None or verifier_broker is not None:
        if author_broker is None or author_broker is not verifier_broker:
            raise ValueError("streaming live providers must use one shared broker")
        if not all(
            (
                eligibility_prompt_file,
                eligibility_schema_file,
                eligibility_policy_file,
            )
        ):
            raise ValueError("streaming live eligibility inputs are required")
        if author_broker.stream_input_binding_required():
            author_broker.bind_stream_input(
                access_run_dir,
                phase=str(getattr(author, "phase", "")),
                run_id=run_id,
                campaign_id=campaign_id,
                eligibility_prompt_file=eligibility_prompt_file,
                eligibility_schema_file=eligibility_schema_file,
                eligibility_policy_file=eligibility_policy_file,
            )
    run_manifest_file = _write_run_manifest(
        namespace,
        run_id=run_id,
        campaign_id=campaign_id,
        access_run_dir=access_run_dir,
        eligibility_run_dir=eligibility_run_dir,
        access_manifest=access_manifest,
        author=author,
        verifier=verifier,
        eligibility_prompt_file=eligibility_prompt_file,
        eligibility_schema_file=eligibility_schema_file,
        eligibility_policy_file=eligibility_policy_file,
    )
    trusted_eligibility_decisions: dict[str, str] = {}
    if verifier_broker is not None:
        trusted_eligibility_decisions = _trusted_brokered_eligibility_decisions(
            selection=selection,
            access_items=access_items,
            eligibility_jobs=eligibility_jobs,
            verifier=verifier,
            max_papers=max_papers,
            prompt_file=eligibility_prompt_file,
            schema_file=eligibility_schema_file,
            policy_file=eligibility_policy_file,
        )
    progress = _Progress(
        progress_file or namespace / "streaming-dataset-r1" / "progress.json",
        run_id=campaign_id,
        invocation_run_id=run_id,
        run_manifest_file=run_manifest_file,
        counts={
            "full_text_ready": sum(
                item.get("access_state") == "full_text_ready"
                for item in access_items.values()
            ),
            "eligibility_completed": len(trusted_eligibility_decisions)
            if verifier_broker is not None
            else sum(
                (item.get("validation") or {}).get("decision")
                in {"eligible", "excluded", "uncertain"}
                for item in eligibility_jobs.values()
            ),
            "eligible": sum(
                value == "eligible" for value in trusted_eligibility_decisions.values()
            )
            if verifier_broker is not None
            else sum(
                (item.get("validation") or {}).get("decision") == "eligible"
                for item in eligibility_jobs.values()
            ),
            "excluded": sum(
                value == "excluded" for value in trusted_eligibility_decisions.values()
            )
            if verifier_broker is not None
            else sum(
                (item.get("validation") or {}).get("decision") == "excluded"
                for item in eligibility_jobs.values()
            ),
            "unresolved": sum(
                value == "uncertain" for value in trusted_eligibility_decisions.values()
            )
            if verifier_broker is not None
            else sum(
                (item.get("validation") or {}).get("decision") == "uncertain"
                for item in eligibility_jobs.values()
            ),
            "generation_rejected": 0,
            "accepted_qa": _accepted_count(db, campaign_id),
        },
    )
    if author_broker is not None or verifier_broker is not None:
        progress.attach_broker(author_broker)
    progress.write("running", "eligibility", "Streaming pipeline started.")
    counts = {
        "accepted_base_questions": 0,
        "eligibility_rejected": 0,
        "eligibility_unresolved": 0,
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
        if access is None or access.get("access_state") != "full_text_ready":
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
        if verifier_broker is not None and (
            eligibility is None
            or eligibility.get("execution_authority") != "shared_gemini_broker"
        ):
            eligibility = None
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
            try:
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
            except Exception as error:
                progress.error(candidate_key, access.get("title"), "eligibility", error)
                raise
            eligibility_jobs[candidate_key] = eligibility
            if verifier_broker is None:
                progress.set_count(
                    "eligible",
                    sum(
                        (item.get("validation") or {}).get("decision") == "eligible"
                        for item in eligibility_jobs.values()
                    ),
                )
                progress.set_count(
                    "eligibility_completed",
                    sum(
                        (item.get("validation") or {}).get("decision")
                        in {"eligible", "excluded", "uncertain"}
                        for item in eligibility_jobs.values()
                    ),
                )
                progress.set_count(
                    "excluded",
                    sum(
                        (item.get("validation") or {}).get("decision") == "excluded"
                        for item in eligibility_jobs.values()
                    ),
                )
                progress.set_count(
                    "unresolved",
                    sum(
                        (item.get("validation") or {}).get("decision") == "uncertain"
                        for item in eligibility_jobs.values()
                    ),
                )
        progress.write("running", "eligibility", f"Checking {candidate_key}.")
        try:
            deterministic_unresolved = False
            if verifier_broker is not None:
                _validate_access_integrity(access, eligibility)
                validation = _validate_brokered_eligibility(
                    eligibility,
                    paper_verifier,
                    access=access,
                    prompt_file=eligibility_prompt_file,
                    schema_file=eligibility_schema_file,
                    policy_file=eligibility_policy_file,
                )
                eligibility = {
                    **eligibility,
                    "state": "completed" if validation["valid"] else "screening_error",
                    "validation": validation,
                }
                deterministic_unresolved = validation["valid"] is not True
            if deterministic_unresolved:
                if validation.get("decision") != "uncertain":
                    raise ValueError(
                        "invalid deterministic eligibility must remain uncertain"
                    )
            else:
                _validate_pair(access, eligibility)
        except Exception as error:
            progress.error(candidate_key, access.get("title"), "eligibility", error)
            raise
        decision = eligibility["validation"]["decision"]
        if verifier_broker is not None:
            trusted_eligibility_decisions[candidate_key] = decision
            progress.set_count(
                "eligibility_completed", len(trusted_eligibility_decisions)
            )
            progress.set_count(
                "eligible",
                sum(
                    value == "eligible"
                    for value in trusted_eligibility_decisions.values()
                ),
            )
            progress.set_count(
                "excluded",
                sum(
                    value == "excluded"
                    for value in trusted_eligibility_decisions.values()
                ),
            )
            progress.set_count(
                "unresolved",
                sum(
                    value == "uncertain"
                    for value in trusted_eligibility_decisions.values()
                ),
            )
        if decision != "eligible":
            validation = eligibility["validation"]
            unresolved = validation["valid"] is not True or decision == "uncertain"
            reason_codes = (
                validation.get("errors") or ["eligibility_validation_unresolved"]
                if validation["valid"] is not True
                else validation.get(
                    "overall_reason_codes",
                    eligibility["parsed_response"].get(
                        "overall_reason_codes", ["eligibility_unresolved"]
                    ),
                )
            )
            disposition = (
                "eligibility_unresolved" if unresolved else "eligibility_rejected"
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
                                    "decision": decision,
                                    "validation_valid": validation["valid"],
                                    "validation_errors": validation.get("errors", []),
                                    "selection": selected,
                                }
                            ),
                            now(),
                        ),
                    )
            paper_results.append(
                {
                    "candidate_key": candidate_key,
                    "disposition": disposition,
                    "reason_codes": reason_codes,
                    "source_id": None,
                }
            )
            counts[disposition] += 1
            counts["processed"] += 1
            progress.paper(
                paper_id=candidate_key,
                title=access.get("title"),
                current_stage="completed",
                final_state="unresolved" if unresolved else "rejected",
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
        except CandidateRejectedError as error:
            reason_code = error.reason_code
            with db.transaction():
                db.connection.execute(
                    """INSERT OR IGNORE INTO rejection_ledger
                    (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
                    VALUES (?,NULL,?,'generation',?,?,?)""",
                    (
                        stable_id(
                            "rejection",
                            campaign_id,
                            candidate_key,
                            "generation",
                            reason_code,
                        ),
                        source_id,
                        reason_code,
                        canonical_json(
                            {
                                "candidate_key": candidate_key,
                                "error": str(error),
                                "selection": selected,
                            }
                        ),
                        now(),
                    ),
                )
            counts["generation_rejected"] += 1
            counts["processed"] += 1
            progress.increment("generation_rejected")
            paper_results.append(
                {
                    "candidate_key": candidate_key,
                    "disposition": "generation_rejected",
                    "reason_codes": [reason_code],
                    "source_id": source_id,
                }
            )
            progress.paper(
                paper_id=source_id,
                title=access.get("title"),
                current_stage="completed",
                final_state="generation_rejected",
                final_reason=reason_code,
            )
            continue
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
            progress.increment("generation_rejected")
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
    progress.set_dataset_metadata(
        namespace / "exports" / exported["export_id"] / "manifest.json"
    )
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


def _trusted_brokered_eligibility_decisions(
    *,
    selection: list[dict[str, Any]],
    access_items: dict[str, dict[str, Any]],
    eligibility_jobs: dict[str, dict[str, Any]],
    verifier: Provider,
    max_papers: int,
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
) -> dict[str, str]:
    decisions: dict[str, str] = {}
    processed = 0
    for selected in selection:
        if processed >= max_papers:
            break
        candidate_key = selected.get("candidate_key")
        access = access_items.get(candidate_key)
        if access is None or access.get("access_state") != "full_text_ready":
            continue
        processed += 1
        eligibility = eligibility_jobs.get(candidate_key)
        if eligibility is None or eligibility.get("execution_authority") != (
            "shared_gemini_broker"
        ):
            continue
        family_id = str(
            access.get("paper_family_id")
            or stable_id("family", access.get("doi") or candidate_key)
        )
        paper_verifier = _bind_provider(
            verifier,
            paper_id=str(candidate_key),
            family_id=family_id,
            source_version_id=str(access["source_content_hash"]),
        )
        _validate_access_integrity(access, eligibility)
        validation = _validate_brokered_eligibility(
            eligibility,
            paper_verifier,
            access=access,
            prompt_file=prompt_file,
            schema_file=schema_file,
            policy_file=policy_file,
        )
        decision = validation["decision"]
        if validation["valid"] is not True and decision != "uncertain":
            raise ValueError("invalid deterministic eligibility must remain uncertain")
        decisions[str(candidate_key)] = decision
    return decisions


def _accepted_count(db: Database, run_id: str) -> int:
    row = db.one(
        """SELECT COUNT(DISTINCT paper_family_id) AS count FROM candidates
        WHERE run_id=? AND status='machine_accepted_unverified'""",
        (run_id,),
    )
    return int(row["count"])


def _write_run_manifest(
    namespace: Path,
    *,
    run_id: str,
    campaign_id: str,
    access_run_dir: Path,
    eligibility_run_dir: Path,
    access_manifest: dict[str, Any],
    author: Provider,
    verifier: Provider,
    eligibility_prompt_file: Path | None,
    eligibility_schema_file: Path | None,
    eligibility_policy_file: Path | None,
) -> Path:
    def file_hash(path: Path | None) -> str | None:
        return sha256_file(path) if path is not None and path.is_file() else None

    manifest = {
        "schema": "streaming-dataset-run-manifest-v1",
        "run_id": run_id,
        "campaign_id": campaign_id,
        "phase": getattr(author, "phase", "offline"),
        "access_run_dir": str(access_run_dir.resolve()),
        "access_manifest_sha256": sha256_file(access_run_dir / "run-manifest.json"),
        "access_completion_receipt_sha256": sha256_file(
            access_run_dir / "run-receipt.json"
        ),
        "selection_sha256": sha256_bytes(
            canonical_json(access_manifest["selection"]).encode()
        ),
        "eligibility_run_dir": str(eligibility_run_dir.resolve()),
        "eligibility_prompt_sha256": file_hash(eligibility_prompt_file),
        "eligibility_schema_sha256": file_hash(eligibility_schema_file),
        "eligibility_policy_sha256": file_hash(eligibility_policy_file),
        "author": {"provider": author.name, "model": author.model},
        "verifier": {"provider": verifier.name, "model": verifier.model},
        "generation_arm": "answer_first",
        "export_seed": "streaming-20260912",
    }
    path = (
        namespace
        / "streaming-dataset-r1"
        / "runs"
        / stable_id("stream-invocation", run_id)
        / "run-manifest.json"
    )
    try:
        atomic_json(path, manifest, immutable=True)
    except FileExistsError as error:
        raise ValueError("the immutable streaming run inputs changed") from error
    return path


class _Progress:
    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        invocation_run_id: str,
        run_manifest_file: Path,
        counts: dict[str, int],
    ) -> None:
        self.path = path.resolve()
        self.run_id = run_id
        self.invocation_run_id = invocation_run_id
        self.run_manifest_file = run_manifest_file.resolve()
        self.counts = counts
        self.recent: list[dict[str, Any]] = []
        self.state = "running"
        self.stage = "eligibility"
        self.message = "Streaming pipeline started."
        self.broker_status_file: Path | None = None
        self.budget_policy_file: Path | None = None
        self.dataset_metadata_file: Path | None = None

    def write(self, state: str, stage: str, message: str) -> None:
        self.state = state
        self.stage = stage
        self.message = message
        custody: dict[str, str] = {
            "run_manifest_sha256": sha256_file(self.run_manifest_file)
        }
        if self.broker_status_file is not None:
            custody["broker_status_sha256"] = sha256_file(self.broker_status_file)
            custody["budget_policy_sha256"] = sha256_file(self.budget_policy_file)
        if self.dataset_metadata_file is not None:
            custody["dataset_metadata_sha256"] = sha256_file(self.dataset_metadata_file)
        atomic_json(
            self.path,
            {
                "schema": "streaming-dataset-progress-v1",
                "state": state,
                "run_id": self.run_id,
                "invocation_run_id": self.invocation_run_id,
                "current_stage": stage,
                "updated_at_utc": now(),
                "message": message,
                "counts": self.counts,
                "recent_papers": self.recent[-100:],
                **custody,
            },
        )

    def attach_broker(self, broker: Any) -> None:
        self.broker_status_file = broker.ledger_file.with_name(
            f"{broker.ledger_file.stem}.status.json"
        )
        self.budget_policy_file = broker.policy_file
        broker.set_status_observer(self._broker_state_changed)

    def _broker_state_changed(self, status_file: Path) -> None:
        if status_file != self.broker_status_file:
            raise ValueError("the broker status observer received another ledger")
        self.write(self.state, self.stage, self.message)

    def set_dataset_metadata(self, path: Path) -> None:
        metadata = _read(path)
        if metadata.get("run_id") != self.run_id:
            raise ValueError("the dataset metadata and progress run IDs do not match")
        self.dataset_metadata_file = path.resolve()
        self.write(self.state, self.stage, self.message)

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
    job_key = sha256_bytes(
        canonical_json(
            {
                "base_job_key": _job_key(
                    access, config, prompt_file, schema_file, policy_file
                ),
                "provider_identity": provider.request_identity(),
                "model": provider.model,
                "authority": "shared_gemini_broker",
            }
        ).encode()
    )
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
    span_manifest_reference = _persist_span_manifest_v2(
        run_dir,
        job_key,
        text,
        str(access["extraction_sha256"]),
        schema,
    )
    generation = request["generationConfig"]
    system = request["systemInstruction"]["parts"][0]["text"]
    user_prompt = request["contents"][0]["parts"][0]["text"]
    parameters = {
        "temperature": generation.get("temperature", 0),
        "max_tokens": generation["maxOutputTokens"],
        "json_schema": schema,
    }
    result = call_provider(
        db,
        provider,
        run_id=run_id,
        entity_id=stable_id("eligibility", access["candidate_key"], job_key),
        role="eligibility",
        system=system,
        prompt=user_prompt,
        prompt_version=prompt_file.stem,
        parameters=parameters,
        response_schema=schema,
        reservation=Decimal("0"),
        timeout=120,
        retries=0,
        rate_limit_seconds=0,
    )
    validation = validate_response(
        result.payload,
        _validation_evidence(text, str(access["extraction_sha256"]), schema),
        expected={
            "request_id": job_key,
            "input_echo": hashes,
            "correction_metadata": _correction_metadata(access),
            "known_context_gaps": _known_context_gaps(access),
        },
        response_schema=schema,
    )
    identity = provider.request_identity()
    request_key = broker_request_key(
        model=provider.model,
        run_id=provider.invocation_run_id,
        stage="eligibility",
        paper_id=identity["paper_id"],
        family_id=identity["family_id"],
        source_version_id=identity["source_version_id"],
        payload=request,
    )
    receipt_path = broker.receipts_dir / f"{request_key}.json"
    if not receipt_path.is_file():
        raise ValueError("the brokered eligibility receipt is missing")
    job = {
        "schema": "gemini-eligibility-job-v1",
        "job_key": job_key,
        "execution_authority": "shared_gemini_broker",
        "broker_request_key": request_key,
        "broker_receipt_sha256": sha256_file(receipt_path),
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
        **span_manifest_reference,
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


def _validate_brokered_eligibility(
    eligibility: dict[str, Any],
    provider: Provider,
    *,
    access: dict[str, Any],
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
) -> dict[str, Any]:
    if eligibility.get("execution_authority") != "shared_gemini_broker":
        raise ValueError("the eligibility job did not use the shared broker")
    broker = provider.broker
    prompt = prompt_file.read_text(encoding="utf-8")
    schema = _read(schema_file)
    policy = _read(policy_file)
    expected_job_key = sha256_bytes(
        canonical_json(
            {
                "base_job_key": _job_key(
                    access, broker.config, prompt_file, schema_file, policy_file
                ),
                "provider_identity": provider.request_identity(),
                "model": provider.model,
                "authority": "shared_gemini_broker",
            }
        ).encode()
    )
    text = Path(access["extraction_path"]).read_text(encoding="utf-8")
    request, hashes = _request_payload(
        source=access,
        policy=policy,
        text=text,
        prompt=prompt,
        schema=schema,
        config=broker.config,
        request_id=expected_job_key,
        policy_sha256=sha256_file(policy_file),
    )
    if schema.get("properties", {}).get("schema_version", {}).get("const") == (
        ELIGIBILITY_RESPONSE_V2
    ):
        manifest_path = Path(str(eligibility.get("span_manifest_path") or ""))
        expected_manifest = _span_manifest_v2(
            _span_blocks_v2(text, str(access["extraction_sha256"]))
        )
        expected_manifest_sha256 = sha256_bytes(
            canonical_json(expected_manifest).encode()
        )
        if (
            manifest_path.name != f"{expected_job_key}.json"
            or manifest_path.parent.name != "span-manifests"
            or not manifest_path.is_file()
            or _read(manifest_path) != expected_manifest
            or eligibility.get("span_manifest_sha256") != expected_manifest_sha256
            or hashes.get("span_manifest_sha256") != expected_manifest_sha256
        ):
            raise ValueError("the eligibility span manifest does not match")
    identity = provider.request_identity()
    expected_request_key = broker_request_key(
        model=provider.model,
        run_id=provider.invocation_run_id,
        stage="eligibility",
        paper_id=identity["paper_id"],
        family_id=identity["family_id"],
        source_version_id=identity["source_version_id"],
        payload=request,
    )
    request_key = eligibility.get("broker_request_key")
    if (
        eligibility.get("job_key") != expected_job_key
        or request_key != expected_request_key
        or eligibility.get("model") != provider.model
        or eligibility.get("policy_sha256") != sha256_file(policy_file)
        or eligibility.get("prompt_sha256") != sha256_file(prompt_file)
        or eligibility.get("schema_sha256") != sha256_file(schema_file)
    ):
        raise ValueError("the brokered eligibility request identity does not match")
    if not isinstance(request_key, str):
        raise ValueError("the brokered eligibility request key is missing")
    receipt, result = provider.read_receipt(
        request_key=request_key,
        role="eligibility",
        request_sha256=sha256_bytes(canonical_json(request).encode()),
    )
    receipt_path = broker.receipts_dir / f"{request_key}.json"
    if (
        eligibility.get("broker_receipt_sha256") != sha256_file(receipt_path)
        or receipt.get("state") != "completed"
        or result.payload != eligibility.get("parsed_response")
        or result.returned_model != eligibility.get("model_version")
        or result.request_id != eligibility.get("response_id")
    ):
        raise ValueError("the brokered eligibility job and receipt do not match")
    validation = validate_response(
        result.payload,
        _validation_evidence(text, str(access["extraction_sha256"]), schema),
        expected={
            "request_id": expected_job_key,
            "input_echo": hashes,
            "correction_metadata": _correction_metadata(access),
            "known_context_gaps": _known_context_gaps(access),
        },
        response_schema=schema,
    )
    return validation


def _validate_access_integrity(
    access: dict[str, Any], eligibility: dict[str, Any]
) -> None:
    if (
        access.get("schema") != "article-access-item-v1"
        or access.get("access_state") != "full_text_ready"
        or access.get("identity_verified") is not True
    ):
        raise ValueError("the article-access item is not full-text ready")
    if eligibility.get("schema") != "gemini-eligibility-job-v1":
        raise ValueError("the Gemini eligibility receipt schema is invalid")
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


def _validate_pair(access: dict[str, Any], eligibility: dict[str, Any]) -> None:
    _validate_access_integrity(access, eligibility)
    if (
        eligibility.get("state") != "completed"
        or (eligibility.get("validation") or {}).get("valid") is not True
    ):
        raise ValueError("the Gemini eligibility receipt is not valid and complete")
    decision = (eligibility.get("validation") or {}).get("decision")
    parsed = eligibility.get("parsed_response") or {}
    version = parsed.get("schema_version")
    decision_consistent = (
        version == "eligibility-response-v1" and parsed.get("overall") == decision
    ) or (
        version == ELIGIBILITY_RESPONSE_V2
        and "overall" not in parsed
        and "overall_reason_codes" not in parsed
        and (eligibility.get("validation") or {}).get("mapping_version")
        == ELIGIBILITY_STATUS_MAPPING_VERSION
    )
    if decision not in {"eligible", "excluded", "uncertain"} or not decision_consistent:
        raise ValueError("the Gemini eligibility decision is inconsistent")
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
        "source_version": access.get("source_version") or access["source_content_hash"],
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
        "overall_reason_codes": eligibility["validation"].get(
            "overall_reason_codes", parsed.get("overall_reason_codes", [])
        ),
        **(
            {"status_mapping_version": eligibility["validation"].get("mapping_version")}
            if parsed.get("schema_version") == ELIGIBILITY_RESPONSE_V2
            else {}
        ),
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
