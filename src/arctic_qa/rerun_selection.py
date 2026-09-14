"""Build the immutable rerun-first selection for a production release."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .full_run_plan import materialize_frozen_access_run
from .generation import FINDING_POLICY_VERSION, PROMPT_VERSION
from .util import (
    atomic_json,
    atomic_write,
    canonical_json,
    jsonl_bytes,
    sha256_bytes,
    sha256_file,
    stable_id,
)
from .validation import NUMERIC_RULE_CONTRACT_VERSION, SCOPE_CONTRACT_VERSION


SELECTION_SCHEMA = "arctic-qa-rerun-selection-v1"
MANIFEST_SCHEMA = "arctic-qa-rerun-manifest-v1"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"nonempty JSONL objects required: {path}")
    return rows


def _candidate_history(
    state_db_file: Path, family_to_paper: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    connection = sqlite3.connect(
        f"file:{state_db_file.resolve()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        rows = connection.execute(
            """SELECT item_id,run_id,paper_family_id,status,candidate_json,created_at
            FROM candidates ORDER BY created_at,item_id"""
        ).fetchall()
        for row in rows:
            paper_id = family_to_paper.get(str(row["paper_family_id"]))
            if paper_id is None:
                continue
            candidate = json.loads(row["candidate_json"])
            result[paper_id].append(
                {
                    "item_id": row["item_id"],
                    "run_id": row["run_id"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "prompt_version": (candidate.get("provenance") or {}).get(
                        "prompt_version"
                    ),
                    "finding_policy_version": candidate.get(
                        "finding_policy_version"
                    ),
                }
            )
    finally:
        connection.close()
    return dict(result)


def _request_history(ledger: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    requests = ledger.get("requests")
    if not isinstance(requests, dict):
        raise ValueError("the shared ledger request map is missing")
    for request_key, request in sorted(requests.items()):
        if not isinstance(request, dict):
            raise ValueError("the shared ledger has an invalid request")
        paper_id = request.get("paper_id")
        if not isinstance(paper_id, str) or not paper_id:
            raise ValueError("a shared ledger request has no paper ID")
        result[paper_id].append(
            {
                "request_key": request_key,
                "run_id": request.get("run_id"),
                "stage": request.get("stage"),
                "state": request.get("state"),
                "submitted_at_utc": request.get("submitted_at_utc"),
                "completed_at_utc": request.get("completed_at_utc"),
                "gate_sha256": request.get("gate_sha256"),
                "policy_sha256": request.get("policy_sha256"),
            }
        )
    return dict(result)


def build_rerun_selection(
    *,
    source_manifest_file: Path,
    descriptor_file: Path,
    quality_order_file: Path,
    ledger_file: Path,
    state_db_file: Path,
    output_dir: Path,
    eligibility_run_dir: Path,
    eligibility_prompt_file: Path,
    eligibility_schema_file: Path,
    eligibility_policy_file: Path,
    producer_commit: str,
    invocation_run_id: str,
    campaign_id: str,
    limit: int = 800,
) -> dict[str, Any]:
    """Put all prior paid paper families before unseen ranked papers."""
    if not producer_commit or not invocation_run_id or not campaign_id:
        raise ValueError("the release identities are required")
    source_rows = _read_jsonl(source_manifest_file)
    descriptor = _read_json(descriptor_file)
    if (
        descriptor.get("schema") != "full-text-ready-freeze-descriptor-v1"
        or descriptor.get("state") != "frozen_offline"
        or (descriptor.get("counts") or {}).get("manifest_records")
        != len(source_rows)
    ):
        raise ValueError("the frozen source descriptor is inconsistent")
    if limit < 1 or limit > len(source_rows):
        raise ValueError("the release limit is outside the frozen source count")

    by_key: dict[str, dict[str, Any]] = {}
    for expected_position, row in enumerate(source_rows, start=1):
        candidate_key = row.get("candidate_key")
        if (
            row.get("manifest_position") != expected_position
            or not isinstance(candidate_key, str)
            or not candidate_key
            or candidate_key in by_key
        ):
            raise ValueError("the frozen source manifest identity is inconsistent")
        by_key[candidate_key] = row

    quality = _read_json(quality_order_file)
    ranked = quality.get("records")
    if (
        quality.get("source_manifest_sha256") != sha256_file(source_manifest_file)
        or not isinstance(ranked, list)
        or len(ranked) != len(source_rows)
    ):
        raise ValueError("the quality order does not match the frozen source manifest")
    ranked_keys: list[str] = []
    for expected_rank, row in enumerate(ranked, start=1):
        if not isinstance(row, dict) or row.get("rank") != expected_rank:
            raise ValueError("the quality order rank is inconsistent")
        candidate_key = row.get("candidate_key")
        if not isinstance(candidate_key, str) or candidate_key not in by_key:
            raise ValueError("the quality order source is unknown")
        ranked_keys.append(candidate_key)
    if len(set(ranked_keys)) != len(ranked_keys):
        raise ValueError("the quality order has a duplicate source")

    ledger = _read_json(ledger_file)
    paper_bindings = ledger.get("paper_bindings")
    family_bindings = ledger.get("family_bindings")
    if not isinstance(paper_bindings, dict) or not isinstance(family_bindings, dict):
        raise ValueError("the shared ledger paper bindings are missing")
    if len(paper_bindings) > limit:
        raise ValueError("the prior evaluated set exceeds the release limit")

    family_to_paper: dict[str, str] = {}
    for paper_id, binding in paper_bindings.items():
        if not isinstance(binding, dict):
            raise ValueError("the shared ledger has an invalid paper binding")
        family_id = binding.get("family_id")
        source_version_id = binding.get("source_version_id")
        frozen = by_key.get(str(paper_id))
        receipt = (frozen or {}).get("access_receipt") or {}
        if (
            not isinstance(family_id, str)
            or not family_id
            or family_id in family_to_paper
            or frozen is None
            or receipt.get("source_sha256") != source_version_id
            or (family_bindings.get(family_id) or {}).get("paper_id") != paper_id
        ):
            raise ValueError("a prior paper binding does not match the frozen source")
        family_to_paper[family_id] = str(paper_id)

    prior_keys = set(str(key) for key in paper_bindings)
    ordered_prior = [key for key in ranked_keys if key in prior_keys]
    if len(ordered_prior) != len(prior_keys):
        raise ValueError("the quality order does not contain every prior paper")
    ordered_unseen = [key for key in ranked_keys if key not in prior_keys]
    selected_keys = [*ordered_prior, *ordered_unseen[: limit - len(ordered_prior)]]

    selected_rows: list[dict[str, Any]] = []
    selection_records: list[dict[str, Any]] = []
    prompt_sha256 = sha256_file(eligibility_prompt_file)
    schema_sha256 = sha256_file(eligibility_schema_file)
    policy_sha256 = sha256_file(eligibility_policy_file)
    request_history = _request_history(ledger)
    candidate_history = _candidate_history(state_db_file, family_to_paper)
    for position, candidate_key in enumerate(selected_keys, start=1):
        frozen = dict(by_key[candidate_key])
        prior_binding = paper_bindings.get(candidate_key)
        family_id = (
            prior_binding["family_id"]
            if isinstance(prior_binding, dict)
            else stable_id("family", frozen.get("doi") or candidate_key)
        )
        frozen["manifest_position"] = position
        frozen["original_manifest_position"] = by_key[candidate_key][
            "manifest_position"
        ]
        frozen["paper_family_id"] = family_id
        frozen["release_selection"] = (
            "prior_evaluated_rerun" if candidate_key in prior_keys else "ranked_unseen"
        )
        selected_rows.append(frozen)
        if candidate_key not in prior_keys:
            continue
        source_version_id = prior_binding["source_version_id"]
        new_identity = {
            "producer_commit": producer_commit,
            "invocation_run_id": invocation_run_id,
            "campaign_id": campaign_id,
            "paper_id": candidate_key,
            "paper_family_id": family_id,
            "source_version_id": source_version_id,
            "selection_position": position,
            "eligibility_prompt_sha256": prompt_sha256,
            "eligibility_schema_sha256": schema_sha256,
            "eligibility_policy_sha256": policy_sha256,
            "generation_prompt_version": PROMPT_VERSION,
            "finding_policy_version": FINDING_POLICY_VERSION,
            "numeric_rule_contract_version": NUMERIC_RULE_CONTRACT_VERSION,
            "scope_contract_version": SCOPE_CONTRACT_VERSION,
        }
        selection_records.append(
            {
                "paper_id": candidate_key,
                "paper_family_id": family_id,
                "source_version_id": source_version_id,
                "selection_position": position,
                "prior_requests": request_history.get(candidate_key, []),
                "prior_candidates": candidate_history.get(candidate_key, []),
                "new_attempt": {
                    **new_identity,
                    "attempt_version_id": sha256_bytes(
                        canonical_json(new_identity).encode()
                    ),
                    "eligibility_run_dir": str(eligibility_run_dir.resolve()),
                    "state": "selected_pending",
                },
            }
        )

    output_dir = output_dir.resolve()
    selection_file = output_dir / f"rerun-first-{limit}.jsonl"
    selected_descriptor_file = output_dir / f"rerun-first-{limit}-descriptor.json"
    atomic_write(selection_file, jsonl_bytes(selected_rows), immutable=True)
    atomic_json(
        selected_descriptor_file,
        {
            "schema": "full-text-ready-freeze-descriptor-v1",
            "state": "frozen_offline",
            "freeze_id": f"{descriptor['freeze_id']}-rerun-first-{limit}-r1",
            "counts": {
                "manifest_records": limit,
                "unique_paper_families": limit,
                "prior_evaluated_reruns": len(ordered_prior),
                "ranked_unseen": limit - len(ordered_prior),
            },
            "source_freeze_id": descriptor["freeze_id"],
            "source_manifest_sha256": sha256_file(source_manifest_file),
            "quality_order_sha256": sha256_file(quality_order_file),
            "selection_policy": SELECTION_SCHEMA,
        },
        immutable=True,
    )
    access = materialize_frozen_access_run(
        source_manifest_file=selection_file,
        descriptor_file=selected_descriptor_file,
        output_dir=output_dir / f"materialized-rerun-first-{limit}",
    )
    unresolved_requests = [
        request_key
        for request_key, request in sorted((ledger.get("requests") or {}).items())
        if isinstance(request, dict) and request.get("state") != "completed"
    ]
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "producer_commit": producer_commit,
        "invocation_run_id": invocation_run_id,
        "campaign_id": campaign_id,
        "selection_policy": SELECTION_SCHEMA,
        "selection": {
            "limit": limit,
            "prior_evaluated_count": len(ordered_prior),
            "ranked_unseen_count": limit - len(ordered_prior),
            "prior_set_precedes_unseen": True,
            "all_prior_evaluated_included": len(ordered_prior)
            == len(paper_bindings),
            "selected_jsonl": str(selection_file),
            "selected_jsonl_sha256": sha256_file(selection_file),
            "descriptor": str(selected_descriptor_file),
            "descriptor_sha256": sha256_file(selected_descriptor_file),
            "materialized_access_run": access,
        },
        "source_inputs": {
            "frozen_manifest_sha256": sha256_file(source_manifest_file),
            "frozen_descriptor_sha256": sha256_file(descriptor_file),
            "quality_order_sha256": sha256_file(quality_order_file),
        },
        "accounting_before": {
            "ledger_sha256": sha256_file(ledger_file),
            "spent_usd": ledger.get("spent_usd"),
            "reserved_usd": ledger.get("reserved_usd"),
            "ambiguous_reserved_usd": ledger.get("ambiguous_reserved_usd"),
            "generation_submissions": ledger.get("generation_submissions"),
            "unresolved_request_keys": unresolved_requests,
        },
        "prior_papers": selection_records,
    }
    manifest_file = output_dir / "rerun-manifest.json"
    atomic_json(manifest_file, manifest, immutable=True)
    return {
        "manifest": str(manifest_file),
        "manifest_sha256": sha256_file(manifest_file),
        "prior_evaluated_count": len(ordered_prior),
        "ranked_unseen_count": limit - len(ordered_prior),
        "access_run": access,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the immutable rerun-first production selection."
    )
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--quality-order", type=Path, required=True)
    parser.add_argument("--shared-ledger-file", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--eligibility-run-dir", type=Path, required=True)
    parser.add_argument("--eligibility-prompt-file", type=Path, required=True)
    parser.add_argument("--eligibility-schema-file", type=Path, required=True)
    parser.add_argument("--eligibility-policy-file", type=Path, required=True)
    parser.add_argument("--producer-commit", required=True)
    parser.add_argument("--invocation-run-id", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--limit", type=int, default=800)
    args = parser.parse_args(argv)
    result = build_rerun_selection(
        source_manifest_file=args.source_manifest,
        descriptor_file=args.descriptor,
        quality_order_file=args.quality_order,
        ledger_file=args.shared_ledger_file,
        state_db_file=args.state_db,
        output_dir=args.output_dir,
        eligibility_run_dir=args.eligibility_run_dir,
        eligibility_prompt_file=args.eligibility_prompt_file,
        eligibility_schema_file=args.eligibility_schema_file,
        eligibility_policy_file=args.eligibility_policy_file,
        producer_commit=args.producer_commit,
        invocation_run_id=args.invocation_run_id,
        campaign_id=args.campaign_id,
        limit=args.limit,
    )
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
