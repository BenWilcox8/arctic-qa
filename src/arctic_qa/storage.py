from __future__ import annotations

import json
import mimetypes
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import Database, now
from .util import atomic_json, atomic_write, sha256_bytes, stable_id


USER_AGENT = "arctic-qa/0.1 (source retrieval, no crawler)"


def fetch_source(
    db: Database,
    namespace: Path,
    source_id: str,
    url: str,
    *,
    max_bytes: int = 50 * 1024 * 1024,
    timeout: float = 30,
    media_type: str | None = None,
) -> dict[str, Any]:
    source = db.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    if not source:
        raise ValueError(f"unknown source: {source_id}")
    if (
        source.get("access_state") == "stored"
        and source.get("retrieval_url") == url
        and source.get("content_hash")
    ):
        artifact = db.one(
            "SELECT * FROM artifacts WHERE source_id=? AND kind='original' AND content_hash=?",
            (source_id, source["content_hash"]),
        )
        if artifact and (namespace / artifact["relative_path"]).is_file():
            metadata = json.loads(artifact["metadata_json"])
            return {
                **metadata,
                "relative_path": artifact["relative_path"],
                "artifact_id": artifact["artifact_id"],
                "resumed": True,
            }
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/pdf,application/xml,text/html,*/*;q=0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError(f"source exceeds maximum size of {max_bytes} bytes")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"source exceeds maximum size of {max_bytes} bytes")
        detected_type = media_type or response.headers.get_content_type()
        final_url = response.geturl()
    return store_original(db, namespace, source_id, body, detected_type, final_url)


def store_original(
    db: Database,
    namespace: Path,
    source_id: str,
    body: bytes,
    media_type: str,
    retrieval_url: str,
) -> dict[str, Any]:
    digest = sha256_bytes(body)
    suffix = _suffix(media_type, retrieval_url)
    relative = Path("originals") / digest[:2] / digest / f"source{suffix}"
    target = namespace / relative
    atomic_write(target, body, immutable=True)
    manifest_path = target.with_name("manifest.json")
    object_metadata = {
        "schema_version": "2.0.0",
        "sha256": digest,
        "size_bytes": len(body),
        "media_type": media_type,
    }
    if not manifest_path.exists():
        atomic_json(manifest_path, object_metadata, immutable=True)
    metadata = {
        "source_id": source_id,
        "sha256": digest,
        "size_bytes": len(body),
        "media_type": media_type,
        "retrieval_url": retrieval_url,
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    retrieval_id = stable_id("retrieval", source_id, digest, retrieval_url)
    atomic_json(
        namespace / "manifests" / f"{retrieval_id}.json",
        {"schema_version": "1.0.0", **metadata},
        immutable=True,
    )
    artifact_id = stable_id("artifact", source_id, "original", digest)
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO artifacts
            (artifact_id,source_id,kind,content_hash,relative_path,media_type,created_at,metadata_json)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                artifact_id,
                source_id,
                "original",
                digest,
                str(relative),
                media_type,
                now(),
                json.dumps(metadata, sort_keys=True),
            ),
        )
        db.connection.execute(
            """UPDATE sources SET content_hash=?,media_type=?,retrieval_url=?,retrieved_at=?,access_state='stored',updated_at=?
            WHERE source_id=?""",
            (
                digest,
                media_type,
                retrieval_url,
                metadata["retrieved_at"],
                now(),
                source_id,
            ),
        )
    return {**metadata, "relative_path": str(relative), "artifact_id": artifact_id}


def _suffix(media_type: str, url: str) -> str:
    known = {
        "application/pdf": ".pdf",
        "application/xml": ".xml",
        "text/xml": ".xml",
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "application/json": ".json",
        "text/plain": ".txt",
    }
    if media_type in known:
        return known[media_type]
    suffix = Path(urllib.request.url2pathname(url.split("?", 1)[0])).suffix
    return (
        suffix
        if suffix and len(suffix) <= 8
        else mimetypes.guess_extension(media_type) or ".bin"
    )
