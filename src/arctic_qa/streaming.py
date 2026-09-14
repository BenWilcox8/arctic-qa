from __future__ import annotations

import json
import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import generation as generation_contract
from .db import Database, now
from .discovery import manual_record
from .errors import BudgetError, CandidateRejectedError, ProviderResponseError
from .exporting import export_run
from .extraction import extract_source
from .generation import generate_candidate
from .gemini_eligibility import (
    ELIGIBILITY_RESPONSE_V2,
    ELIGIBILITY_RESPONSE_V3,
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
from .model_broker import PER_REQUEST_CAP_REASON, broker_request_key
from .storage import store_original
from .util import atomic_json, canonical_json, sha256_bytes, sha256_file, stable_id
from .validation import validate_candidate


GENERATION_ATTEMPT_CONTRACT_VERSION = "bounded-paper-progression-v2"
MAX_FINDING_ATTEMPTS = 2
MAX_QUESTION_REVISIONS = 2
MAX_CANDIDATE_PATHS = MAX_FINDING_ATTEMPTS * (MAX_QUESTION_REVISIONS + 1)
REPAIRABLE_QUESTION_REASONS = frozenset(
    {
        "answer_ambiguous",
        "alternative_answer_unresolved",
        "causal_overclaim",
        "question_context_invalid",
        "question_context_missing",
        "question_context_unnecessary",
        "question_context_not_source_supported",
        "question_context_answer_leakage",
        "question_context_required",
        "question_context_referent_unresolved",
        "question_answer_leakage",
        "revision_unchanged_payload",
        "question_claim_type_disagreement",
        "reconstruction_disagreement",
        "relation_scope_mismatch",
        "scope_qualifier_missing",
        "scope_qualifier_not_source_bound",
        "source_entailment_not_verified",
        "insufficient_verified_distractors",
    }
)
ALTERNATIVE_FINDING_REASONS = frozenset(
    {
        "finding_answer_phrase_in_required_question_phrases",
        "insufficient_verified_distractors",
        "reconstruction_disagreement",
    }
)
IMMEDIATE_ALTERNATIVE_FINDING_REASONS = frozenset(
    {"finding_answer_phrase_in_required_question_phrases"}
)
_DEPENDENT_ROUTING_REASONS = {
    "question_context_referent_unresolved": frozenset(
        {"relation_scope_mismatch", "answer_verifier_scope_not_source_bound"}
    ),
    "question_context_missing": frozenset(
        {"relation_scope_mismatch", "answer_verifier_scope_not_source_bound"}
    ),
    "finding_answer_phrase_in_required_question_phrases": frozenset(
        {
            "question_answer_leakage",
            "question_context_answer_leakage",
            "scope_qualifier_missing",
        }
    ),
}


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
    eligibility_jobs = _load_eligibility_jobs(
        eligibility_run_dir,
        prompt_file=eligibility_prompt_file,
        schema_file=eligibility_schema_file,
        policy_file=eligibility_policy_file,
    )
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
        operational_unresolved = (
            verifier_broker.operational_unresolved_families()
            if verifier_broker is not None
            else {}
        )
        if family_id in operational_unresolved:
            unresolved = operational_unresolved[family_id]
            request_key = unresolved["request_key"]
            reason_code = unresolved["reason_code"]
            _record_operational_unresolved(
                db,
                campaign_id=campaign_id,
                candidate_key=str(candidate_key),
                selected=selected,
                family_id=family_id,
                request_key=request_key,
                reason_code=reason_code,
            )
            counts.setdefault("operational_unresolved", 0)
            counts["operational_unresolved"] += 1
            counts["processed"] += 1
            progress.increment("unresolved")
            paper_results.append(
                {
                    "candidate_key": candidate_key,
                    "disposition": "operational_unresolved",
                    "reason_codes": [reason_code],
                    "source_id": None,
                    "broker_request_key": request_key,
                }
            )
            progress.paper(
                paper_id=candidate_key,
                title=access.get("title"),
                current_stage="completed",
                final_state="unresolved",
                final_reason=reason_code,
            )
            continue
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
        generation_result = _progress_generation(
            db,
            namespace,
            progress,
            campaign_id=campaign_id,
            candidate_key=str(candidate_key),
            source_id=source_id,
            family_id=family_id,
            selected=selected,
            title=access.get("title"),
            author=paper_author,
            verifier=paper_verifier,
        )
        if generation_result["resumed"]:
            resumed_papers += 1
        disposition = generation_result["disposition"]
        reason_codes = generation_result["reason_codes"]
        counts[
            {
                "generation_rejected": "generation_rejected",
                "accepted": "accepted_base_questions",
                "incomplete_non_mcq": "incomplete_non_mcq",
            }[disposition]
        ] += 1
        counts["processed"] += 1
        if disposition == "generation_rejected":
            progress.increment("generation_rejected")
        elif disposition == "accepted":
            progress.set_count("accepted_qa", _accepted_count(db, campaign_id))
        paper_results.append(
            {
                "candidate_key": candidate_key,
                "disposition": disposition,
                "reason_codes": reason_codes,
                "source_id": source_id,
            }
        )
        progress.paper(
            paper_id=source_id,
            title=access.get("title"),
            current_stage="completed",
            final_state=disposition,
            final_reason=(
                "machine_accepted_unverified"
                if disposition == "accepted"
                else (reason_codes or [disposition])[0]
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
    generation_counts = _generation_counts(db, campaign_id)
    result = {
        "state": "completed",
        "run_id": run_id,
        "campaign_id": campaign_id,
        "counts": counts,
        "paper_results": paper_results,
        "resumed_papers": resumed_papers,
        "generation_counts": generation_counts,
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


def _progress_generation(
    db: Database,
    namespace: Path,
    progress: _Progress,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str,
    family_id: str,
    selected: dict[str, Any],
    title: str | None,
    author: Provider,
    verifier: Provider,
) -> dict[str, Any]:
    paths = _generation_paths(
        db,
        campaign_id=campaign_id,
        source_id=source_id,
        family_id=family_id,
    )
    resumed = bool(paths)
    generation_attempt_supported = _supports_generation_attempt()

    while True:
        for path in sorted(paths.values(), key=_path_sort_key):
            candidate_row = path.get("candidate")
            if candidate_row is None:
                continue
            candidate = json.loads(candidate_row["candidate_json"])
            if candidate_row["status"] not in {
                "candidate",
                "qa_gate_failed",
            }:
                continue
            try:
                validation = validate_candidate(db, namespace, candidate).as_dict()
            except Exception as error:
                progress.error(source_id, title, "validation", error)
                raise
            path["validation"] = validation
            path["candidate"] = _candidate_row(db, candidate["item_id"])

        for path in paths.values():
            candidate_row = path.get("candidate")
            if candidate_row is None or path.get("validation") is not None:
                continue
            event = _require_validation_event(db, candidate_row)
            path["validation"] = _validation_result_from_event(db, candidate_row, event)

        accepted = _accepted_generation_path(paths)
        if accepted is not None:
            candidate_row = accepted["candidate"]
            event = _require_validation_event(db, candidate_row)
            reason_codes = _reason_codes(event)
            return {
                "disposition": "accepted",
                "reason_codes": reason_codes,
                "resumed": resumed,
            }

        budget_stop = next(
            (
                path
                for path in sorted(paths.values(), key=_path_sort_key)
                if path.get("budget_stop")
            ),
            None,
        )
        if budget_stop is not None:
            return {
                "disposition": "generation_rejected",
                "reason_codes": _path_failure(db, budget_stop)["reason_codes"],
                "resumed": resumed,
            }

        partial = next(
            (
                path
                for path in sorted(paths.values(), key=_path_sort_key)
                if path.get("candidate") is not None
                and path["candidate"]["status"]
                in {"candidate", "qa_gate_failed"}
            ),
            None,
        )
        if partial is not None:
            raise ValueError(
                "a generated candidate remained unvalidated after its validation checkpoint"
            )

        partial_finding = next(
            (
                path
                for path in sorted(paths.values(), key=_path_sort_key)
                if path.get("partial_finding")
            ),
            None,
        )
        if partial_finding is not None:
            next_attempt = partial_finding["attempt"]
        elif paths:
            failed_path = max(paths.values(), key=_path_sort_key)
            failure = _path_failure(db, failed_path)
            if not generation_attempt_supported and failed_path.get("legacy"):
                return {
                    "disposition": (
                        "incomplete_non_mcq"
                        if failed_path.get("candidate_status") == "incomplete_non_mcq"
                        else "generation_rejected"
                    ),
                    "reason_codes": failure["reason_codes"],
                    "resumed": resumed,
                }
            next_attempt = _next_generation_attempt(
                campaign_id=campaign_id,
                family_id=family_id,
                paths=paths,
                failed_path=failed_path,
                reason_codes=failure["reason_codes"],
            )
            if next_attempt is None:
                incomplete = next(
                    (
                        path
                        for path in sorted(paths.values(), key=_path_sort_key)
                        if path.get("candidate_status") == "incomplete_non_mcq"
                    ),
                    None,
                )
                if incomplete is not None:
                    return {
                        "disposition": "incomplete_non_mcq",
                        "reason_codes": _path_failure(db, incomplete)["reason_codes"],
                        "resumed": resumed,
                    }
                return {
                    "disposition": "generation_rejected",
                    "reason_codes": failure["reason_codes"],
                    "resumed": resumed,
                }
        else:
            next_attempt = _generation_attempt(
                campaign_id=campaign_id,
                family_id=family_id,
                finding_attempt_index=1,
                question_revision_index=0,
                attempt_kind="primary",
                parent_attempt_id=None,
                parent_item_id=None,
                trigger_reason_code=None,
                excluded_finding_span_ids=[],
            )

        progress.paper(
            paper_id=source_id,
            title=title,
            current_stage="generation",
        )
        try:
            candidate = _generate_candidate_attempt(
                db,
                namespace,
                source_id=source_id,
                run_id=campaign_id,
                attempt=next_attempt,
                author=author,
                verifier=verifier,
            )
        except (CandidateRejectedError, ProviderResponseError) as error:
            reason_code = error.reason_code
            _record_generation_rejection(
                db,
                campaign_id=campaign_id,
                candidate_key=candidate_key,
                source_id=source_id,
                selected=selected,
                attempt=next_attempt,
                reason_code=reason_code,
                error=error,
            )
            paths[_path_key(next_attempt)] = {
                "attempt": next_attempt,
                "candidate": None,
                "candidate_status": None,
                "rejection": {"reason_codes": [reason_code]},
                "legacy": not generation_attempt_supported,
            }
            continue
        except BudgetError as error:
            if str(error) != PER_REQUEST_CAP_REASON:
                progress.error(source_id, title, "generation", error)
                raise
            _record_budget_stop(
                db,
                campaign_id=campaign_id,
                candidate_key=candidate_key,
                source_id=source_id,
                selected=selected,
                attempt=next_attempt,
                error=error,
            )
            return {
                "disposition": "generation_rejected",
                "reason_codes": ["request_cost_bound_exceeded"],
                "resumed": resumed,
            }
        except Exception as error:
            progress.error(source_id, title, "generation", error)
            raise

        try:
            validation = validate_candidate(db, namespace, candidate).as_dict()
        except Exception as error:
            progress.error(source_id, title, "validation", error)
            raise
        candidate_row = _candidate_row(db, candidate["item_id"])
        if candidate_row is None:
            raise ValueError("generation returned a candidate that was not persisted")
        candidate_provenance = candidate.get("provenance") or {}
        if generation_attempt_supported and "generation_attempt" not in candidate_provenance:
            raise ValueError("generation did not persist its attempt provenance")
        candidate_attempt = _candidate_generation_attempt(candidate, next_attempt)
        if candidate_attempt != next_attempt:
            raise ValueError("generation returned a candidate for another attempt")
        path = {
            "attempt": candidate_attempt,
            "candidate": candidate_row,
            "candidate_status": candidate_row["status"],
            "validation": validation,
            "legacy": not generation_attempt_supported,
            "partial_finding": False,
        }
        paths[_path_key(path["attempt"])] = path
        if (
            validation["final_label"] == "machine_accepted_unverified"
            and validation["labels"]["mcq_eligible"]
        ):
            if hasattr(author, "record_accepted"):
                author.record_accepted(
                    family_id=family_id, item_id=candidate["item_id"]
                )
            return {
                "disposition": "accepted",
                "reason_codes": validation["reasons"],
                "resumed": resumed,
            }
        if validation["final_label"] == "machine_accepted_unverified":
            with db.transaction():
                db.connection.execute(
                    "UPDATE candidates SET status='incomplete_non_mcq',updated_at=? WHERE item_id=?",
                    (now(), candidate["item_id"]),
                )
            path["candidate"] = _candidate_row(db, candidate["item_id"])
            path["candidate_status"] = "incomplete_non_mcq"
            if not generation_attempt_supported:
                return {
                    "disposition": "incomplete_non_mcq",
                    "reason_codes": validation["reasons"],
                    "resumed": resumed,
                }


def _supports_generation_attempt() -> bool:
    try:
        parameters = inspect.signature(generate_candidate).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "generation_attempt"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _generate_candidate_attempt(
    db: Database,
    namespace: Path,
    *,
    source_id: str,
    run_id: str,
    attempt: dict[str, Any],
    author: Provider,
    verifier: Provider,
) -> dict[str, Any]:
    arguments = {
        "source_id": source_id,
        "run_id": run_id,
        "arm": "answer_first",
        "author": author,
        "verifier": verifier,
        "budget_mode": "tokens",
        "budget_limit": Decimal("1000000"),
        "reservation": Decimal("100"),
        "timeout": 30,
        "retries": 0,
        "rate_limit_seconds": 0,
    }
    if _supports_generation_attempt():
        arguments["generation_attempt"] = attempt
    return generate_candidate(db, namespace, **arguments)


def _candidate_row(db: Database, item_id: str) -> dict[str, Any] | None:
    return db.one(
        "SELECT item_id,status,candidate_json FROM candidates WHERE item_id=?",
        (item_id,),
    )


def _generation_attempt(
    *,
    campaign_id: str,
    family_id: str,
    finding_attempt_index: int,
    question_revision_index: int,
    attempt_kind: str,
    parent_attempt_id: str | None,
    parent_item_id: str | None,
    trigger_reason_code: str | None,
    excluded_finding_span_ids: list[str],
) -> dict[str, Any]:
    attempt_id = stable_id(
        "generation-attempt",
        campaign_id,
        family_id,
        finding_attempt_index,
        question_revision_index,
        GENERATION_ATTEMPT_CONTRACT_VERSION,
    )
    return {
        "contract_version": GENERATION_ATTEMPT_CONTRACT_VERSION,
        "attempt_id": attempt_id,
        "attempt_kind": attempt_kind,
        "finding_attempt_index": finding_attempt_index,
        "question_revision_index": question_revision_index,
        "parent_attempt_id": parent_attempt_id,
        "parent_item_id": parent_item_id,
        "trigger_reason_code": trigger_reason_code,
        "finding_policy_version": (
            f"{generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION}:finding-"
            f"{finding_attempt_index}"
        ),
        "excluded_finding_span_ids": sorted(set(excluded_finding_span_ids)),
    }


def _path_key(attempt: dict[str, Any]) -> tuple[int, int]:
    return (
        int(attempt["finding_attempt_index"]),
        int(attempt["question_revision_index"]),
    )


def _path_sort_key(path: dict[str, Any]) -> tuple[int, int, str]:
    attempt = path["attempt"]
    return (*_path_key(attempt), str(attempt["attempt_id"]))


def _validate_generation_attempt(attempt: Any) -> dict[str, Any]:
    fields = {
        "contract_version",
        "attempt_id",
        "attempt_kind",
        "finding_attempt_index",
        "question_revision_index",
        "parent_attempt_id",
        "parent_item_id",
        "trigger_reason_code",
        "finding_policy_version",
        "excluded_finding_span_ids",
    }
    if not isinstance(attempt, dict) or set(attempt) != fields:
        raise ValueError("the generation attempt object is malformed")
    if attempt["contract_version"] != GENERATION_ATTEMPT_CONTRACT_VERSION:
        raise ValueError("the generation attempt contract version is unsupported")
    finding_index = attempt["finding_attempt_index"]
    revision_index = attempt["question_revision_index"]
    if (
        type(finding_index) is not int
        or type(revision_index) is not int
        or not 1 <= finding_index <= MAX_FINDING_ATTEMPTS
        or not 0 <= revision_index <= MAX_QUESTION_REVISIONS
    ):
        raise ValueError("the generation attempt indexes are invalid")
    expected_kind = (
        "primary"
        if (finding_index, revision_index) == (1, 0)
        else "alternative_finding"
        if (finding_index, revision_index) == (2, 0)
        else "question_revision"
        if revision_index in {1, 2}
        else None
    )
    if attempt["attempt_kind"] != expected_kind:
        raise ValueError("the generation attempt kind is inconsistent")
    if not isinstance(attempt["attempt_id"], str) or not attempt["attempt_id"]:
        raise ValueError("the generation attempt ID is missing")
    if not isinstance(attempt["finding_policy_version"], str):
        raise ValueError("the generation finding policy is missing")
    if (
        not isinstance(attempt["excluded_finding_span_ids"], list)
        or any(
            not isinstance(span_id, str) or not span_id
            for span_id in attempt["excluded_finding_span_ids"]
        )
    ):
        raise ValueError("the excluded finding span IDs are invalid")
    if attempt["attempt_kind"] == "primary":
        if any(
            attempt[field] is not None
            for field in ("parent_attempt_id", "parent_item_id", "trigger_reason_code")
        ):
            raise ValueError("the primary generation attempt has a parent")
    elif not isinstance(attempt["parent_attempt_id"], str) or not isinstance(
        attempt["trigger_reason_code"], str
    ):
        raise ValueError("the non-primary generation attempt lineage is incomplete")
    if attempt["parent_item_id"] is not None and not isinstance(
        attempt["parent_item_id"], str
    ):
        raise ValueError("the generation parent item ID is invalid")
    return attempt


def _candidate_generation_attempt(
    candidate: dict[str, Any], fallback: dict[str, Any]
) -> dict[str, Any]:
    value = (candidate.get("provenance") or {}).get("generation_attempt")
    if value is None:
        return fallback
    return _validate_generation_attempt(value)


def _is_current_contract_candidate(candidate: dict[str, Any]) -> bool:
    provenance = candidate.get("provenance") or {}
    if (
        candidate.get("schema_version") != generation_contract.CANDIDATE_SCHEMA_VERSION
        or provenance.get("prompt_version") != generation_contract.PROMPT_VERSION
        or provenance.get("question_verification_contract_version")
        != generation_contract.QUESTION_VERIFICATION_CONTRACT_VERSION
        or provenance.get("numeric_rule_contract_version")
        != generation_contract.NUMERIC_RULE_CONTRACT_VERSION
        or provenance.get("direct_value_contract_version")
        != generation_contract.DIRECT_SOURCE_VALUE_CONTRACT_VERSION
        or provenance.get("scope_contract_version")
        != generation_contract.SCOPE_CONTRACT_VERSION
        or provenance.get("scope_role_semantics_version")
        != generation_contract.SCOPE_ROLE_SEMANTICS_VERSION
        or provenance.get("scope_role_binding_contract_version")
        != generation_contract.SCOPE_ROLE_BINDING_CONTRACT_VERSION
    ):
        return False
    attempt = provenance.get("generation_attempt")
    if attempt is None:
        return candidate.get("finding_policy_version") == (
            generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION
        )
    if not isinstance(attempt, dict) or attempt.get("contract_version") != (
        GENERATION_ATTEMPT_CONTRACT_VERSION
    ):
        return False
    _validate_generation_attempt(attempt)
    return candidate.get("finding_policy_version") in {
        generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION,
        attempt["finding_policy_version"],
    }


def _generation_paths(
    db: Database,
    *,
    campaign_id: str,
    source_id: str,
    family_id: str,
) -> dict[tuple[int, int], dict[str, Any]]:
    paths: dict[tuple[int, int], dict[str, Any]] = {}
    rows = db.rows(
        """SELECT item_id,status,candidate_json FROM candidates
        WHERE run_id=? AND source_id=? AND paper_family_id=?
        ORDER BY updated_at,item_id""",
        (campaign_id, source_id, family_id),
    )
    for row in rows:
        candidate = json.loads(row["candidate_json"])
        if not _is_current_contract_candidate(candidate):
            continue
        attempt_value = (candidate.get("provenance") or {}).get(
            "generation_attempt"
        )
        legacy = attempt_value is None
        attempt = (
            _generation_attempt(
                campaign_id=campaign_id,
                family_id=family_id,
                finding_attempt_index=1,
                question_revision_index=0,
                attempt_kind="primary",
                parent_attempt_id=None,
                parent_item_id=None,
                trigger_reason_code=None,
                excluded_finding_span_ids=[],
            )
            if legacy
            else _validate_generation_attempt(attempt_value)
        )
        _validate_attempt_identity(attempt, campaign_id, family_id)
        key = _path_key(attempt)
        if key in paths:
            raise ValueError("duplicate generation attempt path")
        paths[key] = {
            "attempt": attempt,
            "candidate": row,
            "candidate_status": row["status"],
            "legacy": legacy,
        }

    for row in db.rows(
        """SELECT rejection_id,stage,reason_code,detail_json FROM rejection_ledger
        WHERE source_id=? AND stage IN ('generation','generation_budget')
        ORDER BY rejection_id""",
        (source_id,),
    ):
        detail = json.loads(row["detail_json"])
        if detail.get("campaign_id") != campaign_id:
            continue
        attempt_value = detail.get("generation_attempt")
        if attempt_value is None or not isinstance(attempt_value, dict):
            continue
        if attempt_value.get("contract_version") != GENERATION_ATTEMPT_CONTRACT_VERSION:
            continue
        attempt = _validate_generation_attempt(attempt_value)
        _validate_attempt_identity(attempt, campaign_id, family_id)
        key = _path_key(attempt)
        if key in paths:
            raise ValueError("generation attempt has both candidate and rejection")
        paths[key] = {
            "attempt": attempt,
            "candidate": None,
            "candidate_status": None,
            "rejection": {"reason_codes": [row["reason_code"]]},
            "budget_stop": row["stage"] == "generation_budget",
        }

    finding_policy_prefix = generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION
    for row in db.rows(
        """SELECT finding_id,selection_policy_version,answer_json FROM findings
        WHERE run_id=? AND paper_family_id=? AND source_id=? ORDER BY finding_id""",
        (campaign_id, family_id, source_id),
    ):
        policy = row["selection_policy_version"]
        prefix = f"{finding_policy_prefix}:finding-"
        if not isinstance(policy, str) or not policy.startswith(prefix):
            continue
        try:
            finding_index = int(policy.removeprefix(prefix))
        except ValueError:
            continue
        if finding_index not in {1, 2}:
            raise ValueError("the finding policy index is outside the bounded range")
        parent_path = max(
            (
                path
                for path in paths.values()
                if path["attempt"]["finding_attempt_index"] == 1
            ),
            key=_path_sort_key,
            default=None,
        )
        attempt = _generation_attempt(
            campaign_id=campaign_id,
            family_id=family_id,
            finding_attempt_index=finding_index,
            question_revision_index=0,
            attempt_kind=(
                "primary" if finding_index == 1 else "alternative_finding"
            ),
            parent_attempt_id=(
                parent_path["attempt"]["attempt_id"] if parent_path else None
                if finding_index == 2
                else None
            ),
            parent_item_id=(
                parent_path["candidate"].get("item_id")
                if finding_index == 2
                and parent_path is not None
                and parent_path.get("candidate") is not None
                else None
            ),
            trigger_reason_code=(
                _path_failure(db, parent_path)["reason_codes"][0]
                if finding_index == 2 and parent_path is not None
                else "generation_rejected"
                if finding_index == 2
                else None
            ),
            excluded_finding_span_ids=(
                _prior_finding_span_ids(paths) if finding_index == 2 else []
            ),
        )
        _validate_attempt_identity(attempt, campaign_id, family_id)
        key = _path_key(attempt)
        if key in paths:
            paths[key]["finding"] = row
            paths[key]["partial_finding"] = False
            continue
        paths[key] = {
            "attempt": attempt,
            "candidate": None,
            "candidate_status": None,
            "finding": row,
            "partial_finding": True,
        }
    _validate_generation_lineage(paths)
    return paths


def _validate_generation_lineage(
    paths: dict[tuple[int, int], dict[str, Any]]
) -> None:
    if len(paths) > MAX_CANDIDATE_PATHS:
        raise ValueError("the paper exceeds the bounded generation path limit")
    for key, path in paths.items():
        attempt = path["attempt"]
        if key == (1, 0):
            continue
        parent = next(
            (
                other
                for other in paths.values()
                if other["attempt"]["attempt_id"] == attempt["parent_attempt_id"]
            ),
            None,
        )
        if parent is None:
            raise ValueError("the generation attempt parent is missing")
        if attempt["parent_item_id"] is not None:
            candidate = parent.get("candidate")
            if candidate is None or candidate["item_id"] != attempt["parent_item_id"]:
                raise ValueError("the generation attempt parent item is inconsistent")
        if key[1] in {1, 2} and _path_key(parent["attempt"])[0] != key[0]:
            raise ValueError("a question revision changed its finding")
        if key == (2, 0) and _path_key(parent["attempt"]) == (2, 0):
            raise ValueError("the alternative finding parent is invalid")


def _accepted_generation_path(
    paths: dict[tuple[int, int], dict[str, Any]]
) -> dict[str, Any] | None:
    for path in sorted(paths.values(), key=_path_sort_key):
        candidate = path.get("candidate")
        validation = path.get("validation")
        if candidate is None or validation is None:
            continue
        if (
            validation["final_label"] == "machine_accepted_unverified"
            and validation["labels"].get("mcq_eligible")
        ):
            return path
        if (
            candidate["status"] == "machine_accepted_unverified"
            and validation["labels"].get("mcq_eligible")
        ):
            return path
    return None


def _path_failure(db: Database, path: dict[str, Any]) -> dict[str, list[str]]:
    candidate = path.get("candidate")
    if candidate is not None:
        event = _require_validation_event(db, candidate)
        reasons = _reason_codes(event)
        if not reasons and candidate["status"] == "incomplete_non_mcq":
            reasons = ["insufficient_verified_distractors"]
        path["candidate_status"] = candidate["status"]
        return {"reason_codes": reasons or ["validation_rejected"]}
    rejection = path.get("rejection") or {}
    reasons = rejection.get("reason_codes") or ["generation_rejected"]
    return {"reason_codes": [str(reason) for reason in reasons]}


def _next_generation_attempt(
    *,
    campaign_id: str,
    family_id: str,
    paths: dict[tuple[int, int], dict[str, Any]],
    failed_path: dict[str, Any],
    reason_codes: list[str],
) -> dict[str, Any] | None:
    reason_codes = _routing_reason_codes(reason_codes)
    failed_attempt = failed_path["attempt"]
    finding_index = int(failed_attempt["finding_attempt_index"])
    revision_index = int(failed_attempt["question_revision_index"])
    if (
        finding_index == 1
        and revision_index == 0
        and len(reason_codes) == 1
        and reason_codes[0] in IMMEDIATE_ALTERNATIVE_FINDING_REASONS
        and (2, 0) not in paths
    ):
        excluded = _prior_finding_span_ids(paths)
        if excluded:
            return _generation_attempt(
                campaign_id=campaign_id,
                family_id=family_id,
                finding_attempt_index=2,
                question_revision_index=0,
                attempt_kind="alternative_finding",
                parent_attempt_id=failed_attempt["attempt_id"],
                parent_item_id=(failed_path.get("candidate") or {}).get("item_id"),
                trigger_reason_code=reason_codes[0],
                excluded_finding_span_ids=excluded,
            )
    reason_is_repairable = (
        len(reason_codes) == 1
        and reason_codes[0] in REPAIRABLE_QUESTION_REASONS
    )
    next_revision = int(failed_attempt["question_revision_index"]) + 1
    if reason_is_repairable and next_revision <= MAX_QUESTION_REVISIONS:
        key = (int(failed_attempt["finding_attempt_index"]), next_revision)
        if key not in paths:
            return _generation_attempt(
                campaign_id=campaign_id,
                family_id=family_id,
                finding_attempt_index=key[0],
                question_revision_index=next_revision,
                attempt_kind="question_revision",
                parent_attempt_id=failed_attempt["attempt_id"],
                parent_item_id=(
                    failed_path.get("candidate") or {}
                ).get("item_id"),
                trigger_reason_code=reason_codes[0],
                excluded_finding_span_ids=[],
            )
    if (
        finding_index != 1
        or revision_index < MAX_QUESTION_REVISIONS
    ):
        return None
    if (2, 0) in paths:
        return None
    if len(reason_codes) != 1 or reason_codes[0] not in ALTERNATIVE_FINDING_REASONS:
        return None
    excluded = _prior_finding_span_ids(paths)
    return _generation_attempt(
        campaign_id=campaign_id,
        family_id=family_id,
        finding_attempt_index=2,
        question_revision_index=0,
        attempt_kind="alternative_finding",
        parent_attempt_id=failed_attempt["attempt_id"],
        parent_item_id=(failed_path.get("candidate") or {}).get("item_id"),
        trigger_reason_code=reason_codes[0] if reason_codes else "generation_rejected",
        excluded_finding_span_ids=excluded,
    )


def _routing_reason_codes(reason_codes: list[str]) -> list[str]:
    """Collapse only documented downstream symptoms for one repair root."""
    normalized = list(dict.fromkeys(str(reason) for reason in reason_codes))
    roots = set(normalized).intersection(_DEPENDENT_ROUTING_REASONS)
    if not roots:
        return normalized
    dependent = set().union(*(_DEPENDENT_ROUTING_REASONS[root] for root in roots))
    return [reason for reason in normalized if reason not in dependent]


def _prior_finding_span_ids(paths: dict[tuple[int, int], dict[str, Any]]) -> list[str]:
    span_ids: set[str] = set()
    for path in paths.values():
        if path["attempt"]["finding_attempt_index"] != 1:
            continue
        candidate = path.get("candidate")
        if candidate is None:
            continue
        payload = json.loads(candidate["candidate_json"])
        answer = payload.get("answer") or {}
        source_span_id = answer.get("source_span_id")
        if isinstance(source_span_id, str) and source_span_id:
            span_ids.add(source_span_id)
    return sorted(span_ids)


def _reason_codes(event: dict[str, Any]) -> list[str]:
    reasons = json.loads(event["reason_codes_json"])
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in reasons
    ):
        raise ValueError("a streaming validation event has invalid reason codes")
    return reasons


def _validation_result_from_event(
    db: Database, candidate: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    details_row = db.one(
        """SELECT details_json FROM validation_events
        WHERE item_id=? AND json_extract(details_json, '$.candidate_hash')=?
        ORDER BY created_at DESC,event_id DESC LIMIT 1""",
        (
            candidate["item_id"],
            stable_id("candidate-payload", candidate["candidate_json"]),
        ),
    )
    if details_row is None:
        raise ValueError("a streaming validation event has no payload details")
    details = json.loads(details_row["details_json"])
    labels = details.get("labels")
    if not isinstance(labels, dict):
        raise ValueError("a streaming validation event has invalid labels")
    return {
        "final_label": event["label"],
        "reasons": _reason_codes(event),
        "labels": labels,
    }


def _validate_attempt_identity(
    attempt: dict[str, Any], campaign_id: str, family_id: str
) -> None:
    expected_id = stable_id(
        "generation-attempt",
        campaign_id,
        family_id,
        attempt["finding_attempt_index"],
        attempt["question_revision_index"],
        GENERATION_ATTEMPT_CONTRACT_VERSION,
    )
    expected_policy = (
        f"{generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION}:finding-"
        f"{attempt['finding_attempt_index']}"
    )
    if (
        attempt["attempt_id"] != expected_id
        or attempt["finding_policy_version"] != expected_policy
    ):
        raise ValueError("the generation attempt identity is not deterministic")


def _require_validation_event(
    db: Database, candidate: dict[str, Any]
) -> dict[str, Any]:
    event = _validation_event_for_stored_candidate(db, candidate)
    if event is None:
        raise ValueError(
            "a terminal streaming candidate lacks a validation event for its payload"
        )
    return event


def _record_generation_rejection(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str,
    selected: dict[str, Any],
    attempt: dict[str, Any],
    reason_code: str,
    error: Exception,
) -> None:
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": candidate_key,
        "error": str(error),
        "selection": selected,
        "generation_attempt": attempt,
    }
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
                    attempt["attempt_id"],
                    reason_code,
                ),
                source_id,
                reason_code,
                canonical_json(detail),
                now(),
            ),
        )


def _record_operational_unresolved(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    selected: dict[str, Any],
    family_id: str,
    request_key: str,
    reason_code: str,
) -> None:
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": candidate_key,
        "family_id": family_id,
        "broker_request_key": request_key,
        "selection": selected,
        "replay_prohibited": True,
        "operational_unresolved": True,
    }
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,NULL,'generation',?,?,?)""",
            (
                stable_id(
                    "rejection",
                    campaign_id,
                    candidate_key,
                    family_id,
                    request_key,
                    reason_code,
                ),
                reason_code,
                canonical_json(detail),
                now(),
            ),
        )


def _record_budget_stop(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    source_id: str,
    selected: dict[str, Any],
    attempt: dict[str, Any],
    error: Exception,
) -> None:
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": candidate_key,
        "error": str(error),
        "selection": selected,
        "budget_stop": True,
        "generation_attempt": attempt,
    }
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,?,'generation_budget',?,?,?)""",
            (
                stable_id(
                    "rejection",
                    campaign_id,
                    candidate_key,
                    "generation_budget",
                    attempt["attempt_id"],
                    "request_cost_bound_exceeded",
                ),
                source_id,
                "request_cost_bound_exceeded",
                canonical_json(detail),
                now(),
            ),
        )


def _generation_counts(db: Database, run_id: str) -> dict[str, int]:
    """Return reproducible path, candidate, finding, and settled-call counts."""
    attempt_ids: set[str] = set()
    finding_indexes: set[int] = set()
    revision_attempt_ids: set[str] = set()
    candidate_count = 0
    for row in db.rows(
        "SELECT candidate_json FROM candidates WHERE run_id=?", (run_id,)
    ):
        candidate = json.loads(row["candidate_json"])
        if not _is_current_contract_candidate(candidate):
            continue
        candidate_count += 1
        attempt = (candidate.get("provenance") or {}).get("generation_attempt")
        if isinstance(attempt, dict) and attempt.get("contract_version") == (
            GENERATION_ATTEMPT_CONTRACT_VERSION
        ):
            attempt_ids.add(str(attempt["attempt_id"]))
            finding_indexes.add(int(attempt["finding_attempt_index"]))
            if attempt.get("question_revision_index") in {1, 2}:
                revision_attempt_ids.add(str(attempt["attempt_id"]))
        else:
            family_id = candidate.get("source", {}).get("paper_family_id")
            attempt_ids.add(
                stable_id(
                    "generation-attempt",
                    run_id,
                    family_id,
                    1,
                    0,
                    GENERATION_ATTEMPT_CONTRACT_VERSION,
                )
            )
            finding_indexes.add(1)

    base_policy = generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION
    for row in db.rows(
        """SELECT paper_family_id,selection_policy_version FROM findings
        WHERE run_id=?""",
        (run_id,),
    ):
        policy = row["selection_policy_version"]
        if not isinstance(policy, str) or not policy.startswith(
            f"{base_policy}:finding-"
        ):
            continue
        try:
            index = int(policy.rsplit("-", 1)[1])
        except ValueError:
            continue
        if index not in {1, 2}:
            continue
        attempt_ids.add(
            stable_id(
                "generation-attempt",
                run_id,
                row["paper_family_id"],
                index,
                0,
                GENERATION_ATTEMPT_CONTRACT_VERSION,
            )
        )
        finding_indexes.add(index)

    for row in db.rows(
        """SELECT detail_json FROM rejection_ledger
        WHERE stage IN ('generation','generation_budget')"""
    ):
        detail = json.loads(row["detail_json"])
        if detail.get("campaign_id") != run_id:
            continue
        attempt = detail.get("generation_attempt")
        if not isinstance(attempt, dict) or attempt.get("contract_version") != (
            GENERATION_ATTEMPT_CONTRACT_VERSION
        ):
            continue
        attempt_ids.add(str(attempt["attempt_id"]))
        finding_indexes.add(int(attempt["finding_attempt_index"]))
        if attempt.get("question_revision_index") in {1, 2}:
            revision_attempt_ids.add(str(attempt["attempt_id"]))

    settled_call_count = db.one(
        """SELECT COUNT(*) AS count FROM calls
        WHERE run_id=? AND status <> 'started'""",
        (run_id,),
    )["count"]
    return {
        "model_call_count": int(settled_call_count),
        "qa_candidate_count": candidate_count,
        "finding_attempt_count": len(finding_indexes),
        "candidate_path_count": len(attempt_ids),
        "question_revision_count": len(revision_attempt_ids),
    }


def _validation_event_for_stored_candidate(
    db: Database, candidate: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the newest validation result bound to this exact stored payload."""
    candidate_hash = stable_id("candidate-payload", candidate["candidate_json"])
    return db.one(
        """SELECT label,reason_codes_json FROM validation_events
        WHERE item_id=? AND json_extract(details_json, '$.candidate_hash')=?
        ORDER BY created_at DESC,event_id DESC LIMIT 1""",
        (candidate["item_id"], candidate_hash),
    )


def _candidate_for_current_contract(
    db: Database,
    *,
    run_id: str,
    source_id: str,
    statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return only a candidate made with every current generation contract."""
    rows = db.rows(
        """SELECT item_id,status,candidate_json FROM candidates
        WHERE run_id=? AND source_id=?
        ORDER BY updated_at DESC,item_id DESC""",
        (run_id, source_id),
    )
    for row in rows:
        if statuses is not None and row["status"] not in statuses:
            continue
        candidate = json.loads(row["candidate_json"])
        if _is_current_contract_candidate(candidate):
            return row
    return None


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


def _load_eligibility_jobs(
    run_dir: Path,
    *,
    prompt_file: Path | None,
    schema_file: Path | None,
    policy_file: Path | None,
) -> dict[str, dict[str, Any]]:
    expected_hashes = (
        {
            "prompt_sha256": sha256_file(prompt_file),
            "schema_sha256": sha256_file(schema_file),
            "policy_sha256": sha256_file(policy_file),
        }
        if prompt_file is not None
        and schema_file is not None
        and policy_file is not None
        else None
    )
    selected: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "jobs").glob("*.json")):
        item = _read(path)
        if expected_hashes is not None and any(
            item.get(field) != expected for field, expected in expected_hashes.items()
        ):
            continue
        key = str(item["candidate_key"])
        if key in selected:
            raise ValueError(
                "a candidate has more than one eligibility job for the active version"
            )
        selected[key] = item
    return selected


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
        phase=provider.phase,
        stage="eligibility",
        paper_id=identity["paper_id"],
        family_id=identity["family_id"],
        source_version_id=identity["source_version_id"],
        payload=request,
    )
    receipt_path = broker.effective_receipt_path(request_key)
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
    response_version = (
        schema.get("properties", {}).get("schema_version", {}).get("const")
    )
    if response_version in {ELIGIBILITY_RESPONSE_V2, ELIGIBILITY_RESPONSE_V3}:
        manifest_path = Path(str(eligibility.get("span_manifest_path") or ""))
        expected_manifest = _span_manifest_v2(
            _span_blocks_v2(text, str(access["extraction_sha256"])),
            response_version,
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
        phase=provider.phase,
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
    receipt_path = broker.effective_receipt_path(request_key)
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
        version in {ELIGIBILITY_RESPONSE_V2, ELIGIBILITY_RESPONSE_V3}
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
            if parsed.get("schema_version")
            in {ELIGIBILITY_RESPONSE_V2, ELIGIBILITY_RESPONSE_V3}
            else {}
        ),
        "study_geography": geography,
        "resolved_evidence": [
            row
            for row in eligibility["validation"].get("resolved_evidence", [])
            if row.get("criterion") == "study_geography"
        ],
        **(
            {
                "eligible_arctic_scope": parsed.get("eligible_arctic_scope"),
                "resolved_eligible_arctic_scope": eligibility["validation"].get(
                    "resolved_eligible_arctic_scope"
                ),
            }
            if parsed.get("schema_version") == ELIGIBILITY_RESPONSE_V3
            else {}
        ),
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
                scope_rule_version=?,
                scope_evidence_json=?,updated_at=?
            WHERE source_id=?""",
            (
                "gemini-fulltext-arctic-eligibility-v2"
                if parsed.get("schema_version") == ELIGIBILITY_RESPONSE_V3
                else "gemini-fulltext-arctic-eligibility-v1",
                canonical_json(scope_evidence),
                now(),
                source["source_id"],
            ),
        )
    return source["source_id"]
