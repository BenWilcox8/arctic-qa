from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util import atomic_json, atomic_write, canonical_json, sha256_file


DISPOSITIONS = {
    "priority_seed",
    "retained_article_type",
    "flagged_nonresearch_type",
    "unresolved_missing_type",
    "unresolved_mixed_type",
    "unresolved_other_type",
}
PROGRESS_SCHEMA = "metadata-prefilter-progress-v1"
RECORD_SCHEMA = "metadata-disposition-v1"
RUN_SCHEMA = "metadata-prefilter-run-manifest-v1"
RECEIPT_SCHEMA = "metadata-prefilter-run-receipt-v1"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _file_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def _validate_policy(policy: Any) -> dict[str, Any]:
    if (
        not isinstance(policy, dict)
        or policy.get("schema") != "metadata-prefilter-policy-v1"
    ):
        raise ValueError("the metadata policy has an unsupported schema")
    required = (
        "policy_id",
        "source_protocol_id",
        "batch_size",
        "article_type_tokens",
        "nonresearch_type_tokens",
        "title_review_terms",
        "disposition_precedence",
        "queue_order",
        "rules",
        "constraints",
    )
    missing = [name for name in required if name not in policy]
    if missing:
        raise ValueError(f"the metadata policy is missing: {', '.join(missing)}")
    if (
        not isinstance(policy["batch_size"], int)
        or not 1 <= policy["batch_size"] <= 10000
    ):
        raise ValueError("the metadata policy batch size must be between 1 and 10000")
    dispositions = set(policy["disposition_precedence"])
    if dispositions != DISPOSITIONS:
        raise ValueError("the metadata policy disposition set is incomplete")
    rule_dispositions = {rule.get("disposition") for rule in policy["rules"]}
    if rule_dispositions != DISPOSITIONS:
        raise ValueError("the metadata policy rules do not cover every disposition")
    return policy


def _type_tokens(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    return [
        token.strip().casefold() for token in re.split(r"[;,]", value) if token.strip()
    ]


def _title_terms(title: Any, terms: list[str]) -> list[str]:
    if not isinstance(title, str):
        return []
    normalized = " ".join(title.casefold().split())
    return [
        term
        for term in terms
        if re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", normalized)
    ]


def _source_snapshot(item: dict[str, Any] | None) -> dict[str, Any]:
    if item is None:
        return {
            "eligibility": "unreviewed",
            "decision": "pending",
            "reason_code": "source_screening_not_started",
            "access_status": None,
            "evidence_locator": None,
        }
    decision = str(item.get("decision") or "pending")
    eligibility = {
        "include": "eligible",
        "exclude": "excluded",
    }.get(decision, "pending")
    evidence = item.get("study_setting_evidence")
    return {
        "eligibility": eligibility,
        "decision": decision,
        "reason_code": item.get("reason_code") or "source_reason_not_recorded",
        "access_status": item.get("access_status"),
        "evidence_locator": evidence.get("locator")
        if isinstance(evidence, dict)
        else None,
    }


def classify_candidate(
    candidate: dict[str, Any],
    *,
    sequence: int,
    run_id: str,
    policy: dict[str, Any],
    policy_hash: str,
    source_screening: dict[str, Any] | None,
) -> dict[str, Any]:
    key = str(candidate.get("candidate_key") or "").strip()
    if not key:
        raise ValueError(f"candidate at sequence {sequence} has no candidate_key")
    origins = candidate.get("origins")
    origins = origins if isinstance(origins, list) else []
    seed_origins = [
        str(origin)
        for origin in origins
        if str(origin).startswith("collaborator_seed:")
    ]
    raw_type = candidate.get("type")
    tokens = _type_tokens(raw_type)
    article = set(policy["article_type_tokens"])
    nonresearch = set(policy["nonresearch_type_tokens"])
    token_set = set(tokens)
    has_article = bool(token_set & article)
    has_nonresearch = bool(token_set & nonresearch)

    if seed_origins:
        disposition = "priority_seed"
        reason = "collaborator_seed_priority"
        evidence = {"field": "origins", "value": seed_origins}
        queue = "priority_seed_review"
    elif not tokens:
        disposition = "unresolved_missing_type"
        reason = "provider_type_missing"
        evidence = {"field": "type", "value": raw_type}
        queue = "metadata_resolution_review"
    elif has_nonresearch and token_set <= nonresearch:
        disposition = "flagged_nonresearch_type"
        reason = "provider_type_explicit_nonresearch_flag"
        evidence = {"field": "type", "value": raw_type}
        queue = "nonresearch_type_review"
    elif has_article and has_nonresearch:
        disposition = "unresolved_mixed_type"
        reason = "provider_type_mixes_primary_and_nonresearch"
        evidence = {"field": "type", "value": raw_type}
        queue = "metadata_resolution_review"
    elif has_article:
        disposition = "retained_article_type"
        reason = "provider_article_type_for_review"
        evidence = {"field": "type", "value": raw_type}
        queue = "retrieval_review"
    else:
        disposition = "unresolved_other_type"
        reason = "provider_type_requires_review"
        evidence = {"field": "type", "value": raw_type}
        queue = "metadata_resolution_review"

    flags = []
    checks = (
        ("missing_doi", not candidate.get("doi")),
        ("missing_stable_id", not candidate.get("stable_id")),
        ("missing_authors", not candidate.get("authors")),
        ("missing_year", candidate.get("year") in (None, "")),
        ("missing_venue", not candidate.get("venue")),
        ("missing_type", not tokens),
        ("missing_abstract", candidate.get("abstract_present") is not True),
        ("missing_landing_url", not candidate.get("landing_url")),
        (
            "missing_open_access_url",
            not isinstance(candidate.get("open_access"), dict)
            or not candidate["open_access"].get("url"),
        ),
        (
            "version_relation_unknown",
            candidate.get("version_relation") in (None, "", "unknown"),
        ),
        (
            "correction_status_unknown",
            candidate.get("retraction_correction")
            in (None, "", "unknown", "unknown_not_checked"),
        ),
    )
    flags.extend(name for name, active in checks if active)
    title_terms = _title_terms(candidate.get("title"), policy["title_review_terms"])
    open_access = candidate.get("open_access")
    open_access = open_access if isinstance(open_access, dict) else {}
    doi_present = bool(candidate.get("doi"))
    stable_id_present = bool(candidate.get("stable_id"))
    if doi_present and stable_id_present:
        identity_status = "doi_and_stable_id"
    elif stable_id_present:
        identity_status = "stable_id_only"
    elif doi_present:
        identity_status = "doi_only"
    else:
        identity_status = "missing_identity"
    if open_access.get("url"):
        access_status = "open_access_url_present"
    elif candidate.get("landing_url"):
        access_status = "landing_url_only"
    else:
        access_status = "no_access_url"
    queue_rank = policy["queue_order"].index(queue) + 1
    return {
        "schema": RECORD_SCHEMA,
        "run_id": run_id,
        "policy_id": policy["policy_id"],
        "policy_hash": policy_hash,
        "sequence": sequence,
        "candidate_key": key,
        "metadata_disposition": disposition,
        "provisional": True,
        "scientific_eligibility_effect": "none",
        "reason_code": reason,
        "evidence": evidence,
        "queue": {"name": queue, "rank": queue_rank},
        "identity": {
            "status": identity_status,
            "doi_present": doi_present,
            "stable_id_present": stable_id_present,
        },
        "type_metadata": {"raw": raw_type, "tokens": tokens},
        "access_metadata": {
            "status": access_status,
            "open_access_status": open_access.get("status"),
            "open_access_license": open_access.get("license"),
        },
        "version_relation": candidate.get("version_relation"),
        "retraction_correction": candidate.get("retraction_correction"),
        "metadata_flags": flags,
        "title_review_terms": title_terms,
        "source_screening_snapshot": _source_snapshot(source_screening),
    }


def _jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return "".join(canonical_json(record) + "\n" for record in records).encode()


def _write_viewer_progress(
    path: Path | None,
    progress: dict[str, Any],
    *,
    message: str,
) -> None:
    if path is None:
        return
    atomic_json(
        path,
        {
            "schema": "corpus-progress-v1",
            "state": progress["state"],
            "stage": "metadata_prefilter",
            "updated_at_utc": progress["updated_at_utc"],
            "message": message[:500],
            "run_id": progress["run_id"],
            "policy_id": progress["policy_id"],
            "processed": progress["processed"],
            "total": progress["total"],
            "started_at_utc": progress.get("started_at_utc"),
            "completed_at_utc": progress.get("completed_at_utc"),
            "disposition_counts": progress["disposition_counts"],
        },
    )


def _validate_existing_manifest(
    manifest: dict[str, Any], expected: dict[str, Any]
) -> None:
    for field in (
        "schema",
        "run_id",
        "policy_id",
        "policy_hash",
        "producer_code_commit",
        "record_schema",
        "batch_size",
        "ordering",
        "inputs",
    ):
        if manifest.get(field) != expected.get(field):
            raise ValueError(f"cannot resume because {field} changed")


def _validate_completed_batch(
    output_dir: Path, batch: dict[str, Any]
) -> tuple[Counter[str], Counter[str], int]:
    path = output_dir / batch["file"]
    if not path.is_file() or sha256_file(path) != batch["sha256"]:
        raise ValueError(f"completed batch is missing or changed: {batch['file']}")
    dispositions: Counter[str] = Counter()
    queues: Counter[str] = Counter()
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            dispositions[record["metadata_disposition"]] += 1
            queues[record["queue"]["name"]] += 1
            count += 1
    if count != batch["count"]:
        raise ValueError(f"completed batch count changed: {batch['file']}")
    return dispositions, queues, count


def _concat_batches(
    output_dir: Path, batches: list[dict[str, Any]], target: Path
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output:
            for batch in batches:
                with (output_dir / batch["file"]).open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if target.exists():
            if sha256_file(target) != sha256_file(temporary):
                raise FileExistsError(
                    f"completed disposition file already differs: {target}"
                )
            temporary.unlink()
        else:
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _write_queue(
    dispositions_file: Path,
    target: Path,
    queue_order: list[str],
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as output:
            for queue_name in queue_order:
                with dispositions_file.open(encoding="utf-8") as source:
                    for line in source:
                        record = json.loads(line)
                        if record["queue"]["name"] == queue_name:
                            output.write(line.encode())
            output.flush()
            os.fsync(output.fileno())
        if target.exists():
            if sha256_file(target) != sha256_file(temporary):
                raise FileExistsError(
                    f"completed review queue already differs: {target}"
                )
            temporary.unlink()
        else:
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _completed_progress(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": PROGRESS_SCHEMA,
        "run_id": receipt["run_id"],
        "policy_id": receipt["policy_id"],
        "policy_hash": receipt["policy_hash"],
        "producer_code_commit": receipt["producer_code_commit"],
        "state": "completed",
        "started_at_utc": receipt["started_at_utc"],
        "updated_at_utc": receipt["completed_at_utc"],
        "completed_at_utc": receipt["completed_at_utc"],
        "processed": receipt["processed"],
        "total": receipt["total"],
        "disposition_counts": receipt["disposition_counts"],
        "queue_counts": receipt["queue_counts"],
        "completed_batches": receipt["completed_batches"],
        "resume_history": receipt["resume_history"],
    }


def run_metadata_prefilter(
    *,
    candidates_file: Path,
    screening_file: Path,
    protocol_file: Path,
    policy_file: Path,
    output_dir: Path,
    run_id: str,
    code_commit: str,
    viewer_progress_file: Path | None = None,
    max_batches: int | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,100}", run_id):
        raise ValueError("run-id contains unsupported characters")
    if not re.fullmatch(r"[0-9a-f]{7,40}", code_commit):
        raise ValueError("code-commit must be a lowercase Git object name")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max-batches must be positive")
    paths = {
        "candidates": candidates_file.resolve(),
        "screening": screening_file.resolve(),
        "protocol": protocol_file.resolve(),
    }
    for name, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"{name} input is unavailable: {path}")
    policy_path = policy_file.resolve()
    policy = _validate_policy(_read_json(policy_path))
    policy_hash = sha256_file(policy_path)
    protocol = _read_json(paths["protocol"])
    if protocol.get("protocol_id") != policy["source_protocol_id"]:
        raise ValueError("the policy and source protocol identities do not match")
    inputs = {name: _file_record(path) for name, path in paths.items()}
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "run-manifest.json"
    progress_path = output_dir / "progress.json"
    receipt_path = output_dir / "run-receipt.json"
    final_path = output_dir / "metadata-dispositions.ndjson"
    queue_path = output_dir / "review-queue.ndjson"
    manifest = {
        "schema": RUN_SCHEMA,
        "run_id": run_id,
        "policy_id": policy["policy_id"],
        "policy_hash": policy_hash,
        "producer_code_commit": code_commit,
        "record_schema": RECORD_SCHEMA,
        "batch_size": policy["batch_size"],
        "ordering": "immutable discovery input sequence",
        "inputs": inputs,
        "created_at_utc": utc_now(),
    }
    if manifest_path.exists():
        existing_manifest = _read_json(manifest_path)
        _validate_existing_manifest(existing_manifest, manifest)
        manifest = existing_manifest
    else:
        atomic_json(manifest_path, manifest, immutable=True)
        atomic_write(
            output_dir / "policy.json", policy_path.read_bytes(), immutable=True
        )
    candidates = _read_json(paths["candidates"])
    screening = _read_json(paths["screening"])
    if not isinstance(candidates, list) or not isinstance(screening, list):
        raise ValueError("candidate and screening inputs must contain JSON arrays")
    keys = [str(item.get("candidate_key") or "").strip() for item in candidates]
    if not all(keys) or len(keys) != len(set(keys)):
        raise ValueError("candidate keys must be present and unique")
    input_key_set = set(keys)
    candidate_keys_sha256 = hashlib.sha256(
        "\n".join(sorted(input_key_set)).encode()
    ).hexdigest()
    if receipt_path.exists():
        receipt = _read_json(receipt_path)
        if (
            receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("run_id") != run_id
            or receipt.get("policy_hash") != policy_hash
            or receipt.get("producer_code_commit") != code_commit
            or receipt.get("candidate_keys_sha256") != candidate_keys_sha256
        ):
            raise ValueError("the completed receipt does not match this run")
        if not final_path.is_file() or sha256_file(final_path) != receipt.get(
            "dispositions_sha256"
        ):
            raise ValueError("the completed disposition file is missing or changed")
        if not queue_path.is_file() or sha256_file(queue_path) != receipt.get(
            "queue_sha256"
        ):
            raise ValueError("the completed review queue is missing or changed")
        final_keys = {
            json.loads(line)["candidate_key"]
            for line in final_path.read_text(encoding="utf-8").splitlines()
        }
        if final_keys != input_key_set:
            raise ValueError("the completed disposition keys do not match the input")
        progress = _completed_progress(receipt)
        atomic_json(progress_path, progress)
        _write_viewer_progress(
            viewer_progress_file,
            progress,
            message=f"Metadata-only processing completed: {receipt['total']} of {receipt['total']} records processed.",
        )
        return {**receipt, "idempotent_replay": True}
    screening_by_key = {
        str(item.get("candidate_key")): item
        for item in screening
        if item.get("candidate_key")
    }
    total = len(candidates)
    if progress_path.exists():
        progress = _read_json(progress_path)
        if (
            progress.get("schema") != PROGRESS_SCHEMA
            or progress.get("run_id") != run_id
            or progress.get("policy_hash") != policy_hash
            or progress.get("producer_code_commit") != code_commit
            or progress.get("total") != total
        ):
            raise ValueError("the saved progress record does not match this run")
    else:
        progress = {
            "schema": PROGRESS_SCHEMA,
            "run_id": run_id,
            "policy_id": policy["policy_id"],
            "policy_hash": policy_hash,
            "producer_code_commit": code_commit,
            "state": "not_running",
            "started_at_utc": None,
            "updated_at_utc": utc_now(),
            "completed_at_utc": None,
            "processed": 0,
            "total": total,
            "disposition_counts": {},
            "queue_counts": {},
            "completed_batches": [],
            "resume_history": [],
        }

    dispositions: Counter[str] = Counter()
    queues: Counter[str] = Counter()
    processed = 0
    completed_batches = progress.get("completed_batches")
    if not isinstance(completed_batches, list):
        raise ValueError("the saved completed batch list is invalid")
    for expected_index, batch in enumerate(completed_batches, 1):
        if batch.get("index") != expected_index:
            raise ValueError("completed batches must be contiguous")
        batch_dispositions, batch_queues, count = _validate_completed_batch(
            output_dir, batch
        )
        dispositions.update(batch_dispositions)
        queues.update(batch_queues)
        processed += count
    if processed != progress.get("processed"):
        raise ValueError("the saved processed count does not match completed batches")

    start_time = utc_now()
    event = "resumed" if processed else "started"
    progress.update(
        {
            "state": "running",
            "started_at_utc": progress.get("started_at_utc") or start_time,
            "updated_at_utc": start_time,
            "completed_at_utc": None,
        }
    )
    progress["resume_history"].append(
        {"event": event, "at_utc": start_time, "processed": processed}
    )
    atomic_json(progress_path, progress)
    _write_viewer_progress(
        viewer_progress_file,
        progress,
        message=f"Metadata-only processing is running: {processed} of {total} records processed.",
    )

    batch_size = policy["batch_size"]
    new_batches = 0
    try:
        for offset in range(processed, total, batch_size):
            index = offset // batch_size + 1
            batch_candidates = candidates[offset : offset + batch_size]
            records = [
                classify_candidate(
                    candidate,
                    sequence=offset + position + 1,
                    run_id=run_id,
                    policy=policy,
                    policy_hash=policy_hash,
                    source_screening=screening_by_key.get(candidate["candidate_key"]),
                )
                for position, candidate in enumerate(batch_candidates)
            ]
            content = _jsonl_bytes(records)
            relative = f"batches/batch-{index:06d}.ndjson"
            batch_path = output_dir / relative
            atomic_write(batch_path, content, immutable=True)
            batch_receipt = {
                "index": index,
                "file": relative,
                "sha256": sha256_file(batch_path),
                "count": len(records),
                "first_sequence": records[0]["sequence"],
                "last_sequence": records[-1]["sequence"],
                "completed_at_utc": utc_now(),
            }
            completed_batches.append(batch_receipt)
            dispositions.update(record["metadata_disposition"] for record in records)
            queues.update(record["queue"]["name"] for record in records)
            processed += len(records)
            progress.update(
                {
                    "processed": processed,
                    "updated_at_utc": batch_receipt["completed_at_utc"],
                    "disposition_counts": dict(sorted(dispositions.items())),
                    "queue_counts": dict(sorted(queues.items())),
                    "completed_batches": completed_batches,
                }
            )
            atomic_json(progress_path, progress)
            _write_viewer_progress(
                viewer_progress_file,
                progress,
                message=f"Metadata-only processing is running: {processed} of {total} records processed.",
            )
            new_batches += 1
            if (
                max_batches is not None
                and new_batches >= max_batches
                and processed < total
            ):
                paused_at = utc_now()
                progress.update({"state": "paused", "updated_at_utc": paused_at})
                progress["resume_history"].append(
                    {
                        "event": "controlled_pause",
                        "at_utc": paused_at,
                        "processed": processed,
                    }
                )
                atomic_json(progress_path, progress)
                _write_viewer_progress(
                    viewer_progress_file,
                    progress,
                    message=f"Metadata-only processing is paused: {processed} of {total} records processed.",
                )
                return {**progress, "run_directory": str(output_dir)}

        if processed != total or sum(dispositions.values()) != total:
            raise ValueError("final disposition counts do not reconcile to the input")
        if sum(queues.values()) != total:
            raise ValueError("final queue counts do not reconcile to the input")
        _concat_batches(output_dir, completed_batches, final_path)
        final_count = 0
        final_keys: set[str] = set()
        with final_path.open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                final_count += 1
                final_keys.add(record["candidate_key"])
        if final_count != total or final_keys != input_key_set:
            raise ValueError(
                "the final disposition file does not contain each candidate once"
            )
        _write_queue(final_path, queue_path, policy["queue_order"])
        queue_keys = [
            json.loads(line)["candidate_key"]
            for line in queue_path.read_text(encoding="utf-8").splitlines()
        ]
        if len(queue_keys) != total or set(queue_keys) != input_key_set:
            raise ValueError("the review queue does not contain each candidate once")
        inputs_after = {name: _file_record(path) for name, path in paths.items()}
        if inputs_after != inputs:
            raise ValueError("an input changed during metadata processing")
        completed_at = utc_now()
        history = [
            *progress["resume_history"],
            {"event": "completed", "at_utc": completed_at, "processed": total},
        ]
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "state": "completed",
            "run_id": run_id,
            "policy_id": policy["policy_id"],
            "policy_hash": policy_hash,
            "producer_code_commit": code_commit,
            "record_schema": RECORD_SCHEMA,
            "started_at_utc": progress["started_at_utc"],
            "completed_at_utc": completed_at,
            "total": total,
            "processed": total,
            "unique_candidate_keys": len(final_keys),
            "candidate_keys_sha256": candidate_keys_sha256,
            "disposition_counts": dict(sorted(dispositions.items())),
            "queue_counts": dict(sorted(queues.items())),
            "batch_count": len(completed_batches),
            "completed_batches": completed_batches,
            "batch_size": batch_size,
            "ordering": manifest["ordering"],
            "inputs_before": inputs,
            "inputs_after": inputs_after,
            "inputs_unchanged": True,
            "dispositions_file": final_path.name,
            "dispositions_sha256": sha256_file(final_path),
            "queue_file": queue_path.name,
            "queue_sha256": sha256_file(queue_path),
            "resume_history": history,
            "source_eligibility_is_separate": True,
            "scientific_eligibility_changed": False,
        }
        atomic_json(receipt_path, receipt, immutable=True)
        progress.update(
            {
                "state": "completed",
                "updated_at_utc": completed_at,
                "completed_at_utc": completed_at,
                "resume_history": history,
            }
        )
        atomic_json(progress_path, progress)
        _write_viewer_progress(
            viewer_progress_file,
            progress,
            message=f"Metadata-only processing completed: {total} of {total} records processed.",
        )
        return {**receipt, "run_directory": str(output_dir)}
    except Exception as error:
        failed_at = utc_now()
        progress.update({"state": "error", "updated_at_utc": failed_at})
        progress["error"] = str(error)
        progress["resume_history"].append(
            {"event": "error", "at_utc": failed_at, "processed": processed}
        )
        atomic_json(progress_path, progress)
        _write_viewer_progress(
            viewer_progress_file,
            progress,
            message=f"Metadata-only processing failed after {processed} of {total} records: {error}",
        )
        raise
