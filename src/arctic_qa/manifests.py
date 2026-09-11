from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database
from .util import atomic_json, atomic_write, jsonl_bytes, sha256_bytes


def write_source_manifest(db: Database, namespace: Path) -> dict[str, Any]:
    records = []
    for row in db.rows("SELECT * FROM sources ORDER BY source_id"):
        records.append(
            {
                "schema_version": "1.0.0",
                "source_id": row["source_id"],
                "stable_id": row["stable_id"],
                "doi": row["doi"],
                "title": row["title"],
                "authors": json.loads(row["authors_json"]),
                "published_date": row["published_date"],
                "source_version": row["source_version"],
                "retrieval_url": row["retrieval_url"],
                "retrieved_at": row["retrieved_at"],
                "content_hash": row["content_hash"],
                "media_type": row["media_type"],
                "license": row["license"],
                "access_state": row["access_state"],
                "rights": {
                    "source_access": row["access_state"],
                    "source_storage": "local_only",
                    "dataset_text_redistribution": json.loads(row["metadata_json"]).get(
                        "dataset_text_redistribution", "unknown"
                    ),
                },
                "provenance": json.loads(row["provenance_json"]),
                "corrections": json.loads(row["corrections_json"]),
                "zotero": json.loads(row["zotero_json"]),
                "paper_family_id": row["paper_family_id"],
                "geography_state": row["geography_state"],
                "geography_confidence": row["geography_confidence"],
                "inclusion_reason": row["inclusion_reason"],
                "duplicate_of": row["duplicate_of"],
                "geography_policy_version": row["scope_rule_version"],
                "geography_evidence": json.loads(row["scope_evidence_json"]),
                "eligibility_state": row["eligibility_state"],
                "discipline": row["discipline"],
                "year": row["year"],
            }
        )
    body = jsonl_bytes(records)
    digest = sha256_bytes(body)
    relative = Path("manifests") / f"source-manifest-{digest}.jsonl"
    atomic_write(namespace / relative, body, immutable=True)
    descriptor = {
        "schema_version": "1.0.0",
        "sha256": digest,
        "record_count": len(records),
        "manifest_path": str(relative),
        "existing_source_bridge": "library-originals/catalog.tsv",
        "bridge_mode": "optional_read_only",
    }
    atomic_json(
        namespace / "manifests" / f"source-manifest-{digest}.json",
        descriptor,
        immutable=True,
    )
    return descriptor
