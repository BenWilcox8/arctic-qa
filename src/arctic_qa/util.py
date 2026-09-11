from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable


SECRET_PATTERNS = (
    re.compile(
        r"(?i)(api[_-]?key|authorization|x-api-key|x-goog-api-key)(\s*[:=]\s*)([^\s,}\]]+)"
    ),
    re.compile(r"(?i)(bearer)(\s+)([^\s,}\]]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    payload = canonical_json(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:length]}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes, *, immutable: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == data:
            return False
        if immutable:
            raise FileExistsError(
                f"immutable path already contains different data: {path}"
            )
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def atomic_json(path: Path, value: Any, *, immutable: bool = False) -> bool:
    return atomic_write(
        path, (canonical_json(value) + "\n").encode(), immutable=immutable
    )


def redact(value: str) -> str:
    result = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups == 3:
            result = pattern.sub(r"\1\2[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")


def normalize_doi(value: str) -> str:
    doi = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
    if not doi.startswith("10.") or "/" not in doi or any(ch.isspace() for ch in doi):
        raise ValueError(f"invalid DOI: {value}")
    return doi.rstrip(". ")


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())
