"""Chapter 2 corpus: column-aware re-extraction, re-chunking and re-freeze.

The chapter 1 corpus extracted every PDF with ``pdftotext -layout``. That mode
interleaves the two columns of a journal page line by line, so a column gutter
reached the eligibility spans, the chunks and the evidence quotes. This module
rebuilds the corpus with the reading-order extractor in ``pdf_layout`` and
freezes the result under a new root.

Nothing here writes into a chapter 1 directory and nothing here deletes. The
chapter 1 originals stay where they are and every chapter 2 object is new.

Three stages, each resumable:

``prepare_root``  marks the chapter 2 root and records what it derives from.
``reextract``     re-extracts every usable full text and checkpoints each one.
``freeze``        writes the chapter 2 manifest, order, receipt and access run.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .extraction import (
    PARSER_KEY,
    PARSER_NAME,
    PARSER_VERSION,
    build_records,
    extract_document,
)
from .pdf_layout import EXTRACTOR_NAME, EXTRACTOR_VERSION
from .text_structure import STRUCTURE_VERSION
from .util import (
    atomic_json,
    atomic_write,
    canonical_json,
    jsonl_bytes,
    normalize_doi,
    sha256_bytes,
    sha256_file,
    stable_id,
)


CHAPTER2_DIRECTORY = "chapter2"
ROOT_SCHEMA = "arctic-qa-chapter2-corpus-root-v1"
INDEX_SCHEMA = "arctic-qa-chapter2-parse-index-v1"
MANIFEST_SCHEMA = "arctic-qa-chapter2-corpus-manifest-v1"
DESCRIPTOR_SCHEMA = "arctic-qa-chapter2-corpus-manifest-descriptor-v1"
RECEIPT_SCHEMA = "arctic-qa-chapter2-corpus-freeze-receipt-v1"
PROGRESS_SCHEMA = "arctic-qa-chapter2-reextraction-progress-v1"
CORPUS_SOURCE_VERSION = "chapter2-corpus-v1"

ACCESS_MANIFEST_SCHEMA = "article-access-manifest-v1"
ACCESS_ITEM_SCHEMA = "article-access-item-v1"
ACCESS_PROGRESS_SCHEMA = "article-access-progress-v1"
ACCESS_RECEIPT_SCHEMA = "article-access-run-receipt-v1"

STREAM_INPUT_DIRECTORY = "streaming-input"

DEFAULT_JOBS = 2
_READY = "full_text_ready"


def chapter2_root(namespace: Path) -> Path:
    """Return the chapter 2 corpus root when it exists, else the namespace.

    The captain's chapter 2 decision makes the new corpus authoritative for
    every later extraction. The marker file is the single switch, so no caller
    has to pass a path and no chapter 1 object is ever rewritten.
    """
    root = namespace / CHAPTER2_DIRECTORY
    marker = root / "corpus-root.json"
    if not marker.is_file():
        return namespace
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return namespace
    return root if record.get("schema") == ROOT_SCHEMA else namespace


def index_record(root: Path, raw_sha256: str) -> dict[str, Any] | None:
    """Return the frozen parse receipt for one stored original, if any."""
    path = _index_path(root, raw_sha256)
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema") != INDEX_SCHEMA:
        return None
    if (
        record.get("parser_version") != PARSER_VERSION
        or record.get("extractor_version") != EXTRACTOR_VERSION
        or record.get("structure_version") != STRUCTURE_VERSION
    ):
        return None
    return record


def prepare_root(
    root: Path,
    *,
    access_run_dir: Path,
    legacy_freeze_dir: Path,
    code_commit: str,
) -> dict[str, Any]:
    """Create the chapter 2 corpus root and record what it derives from."""
    for name in ("extracted", "parsed", "chunks", "index", "progress"):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "schema": ROOT_SCHEMA,
        "created_at_utc": _now(),
        "producer_code_commit": code_commit,
        "purpose": (
            "Chapter 2 corpus objects. Column-aware reading-order extraction, "
            "sentence-complete chunks, and their hashes."
        ),
        "derives_from": {
            "access_run_dir": str(access_run_dir.resolve()),
            "legacy_freeze_dir": str(legacy_freeze_dir.resolve()),
        },
        "legacy_is_read_only": True,
        "parser_name": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "parser_key": PARSER_KEY,
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "structure_version": STRUCTURE_VERSION,
    }
    marker = root / "corpus-root.json"
    if marker.is_file():
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if _root_identity(existing) != _root_identity(record):
            raise ValueError("the chapter 2 corpus root changed between runs")
        return existing
    atomic_json(marker, record, immutable=True)
    return record


def ready_items(access_run_dir: Path) -> list[dict[str, Any]]:
    """Return every usable full text of the chapter 1 access run, in order."""
    manifest = _read_json(access_run_dir / "run-manifest.json")
    items = []
    for path in sorted((access_run_dir / "items").glob("item-*.json")):
        row = _read_json(path)
        if row.get("access_state") != _READY:
            continue
        if row.get("schema") != ACCESS_ITEM_SCHEMA or row.get("run_id") != manifest.get(
            "run_id"
        ):
            raise ValueError(f"a ready access receipt is inconsistent: {path}")
        row["_item_path"] = str(path)
        items.append(row)
    return items


def frozen_order(legacy_freeze_dir: Path) -> list[str]:
    """Return the chapter 1 frozen order of candidate keys.

    The chapter 2 order derives from this order, so the two corpora stay
    comparable and the re-freeze receipt can name its predecessor.
    """
    path = legacy_freeze_dir / "full-text-ready-manifest.jsonl"
    order: list[tuple[int, str]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            order.append((int(row["manifest_position"]), str(row["candidate_key"])))
    order.sort()
    if [position for position, _ in order] != list(range(1, len(order) + 1)):
        raise ValueError("the chapter 1 frozen order is not a strict sequence")
    return [key for _, key in order]


def reextract(
    root: Path,
    *,
    access_run_dir: Path,
    jobs: int = DEFAULT_JOBS,
    limit: int | None = None,
    char_cap: int = 6000,
    overlap_chars: int = 500,
    on_result: Any = None,
) -> dict[str, Any]:
    """Re-extract every usable full text into the chapter 2 corpus root.

    Completed documents are checkpointed one line at a time, so an interrupted
    run resumes without repeating work.
    """
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    items = ready_items(access_run_dir)
    if limit is not None:
        items = items[:limit]
    pending: list[dict[str, Any]] = []
    queued: set[str] = set()
    for item in items:
        digest = str(item["source_content_hash"])
        # One parse per stored object: two candidate keys can name the same PDF.
        if digest in queued or index_record(root, digest) is not None:
            continue
        queued.add(digest)
        pending.append(item)
    progress_path = root / "progress" / "reextraction.ndjson"
    counts = {"total": len(items), "resumed": len(items) - len(pending), "failed": 0}
    counts["extracted"] = 0
    tasks = [
        (
            str(item["source_path"]),
            str(item["source_content_hash"]),
            str(item.get("media_type") or "application/pdf"),
            str(item["candidate_key"]),
            item.get("doi"),
            str(root),
            char_cap,
            overlap_chars,
        )
        for item in pending
    ]
    for result in _run_tasks(tasks, jobs):
        if result.get("state") == "extracted":
            counts["extracted"] += 1
        else:
            counts["failed"] += 1
        _append(progress_path, {**result, "schema": PROGRESS_SCHEMA})
        if on_result is not None:
            on_result(result, counts)
    return counts


def extract_one(
    source_path: str,
    raw_sha256: str,
    media_type: str,
    candidate_key: str,
    doi: str | None,
    root: str,
    char_cap: int,
    overlap_chars: int,
) -> dict[str, Any]:
    """Re-extract one stored original into the chapter 2 corpus root."""
    corpus_root = Path(root)
    path = Path(source_path)
    started = _now()
    try:
        if sha256_file(path) != raw_sha256:
            raise ValueError("the stored original does not match its recorded hash")
        source_id = corpus_source_id(candidate_key, doi)
        sections, warnings, coverage = extract_document(path, media_type)
        section_rows, chunk_rows = build_records(
            source_id, raw_sha256, sections, char_cap, overlap_chars
        )
        text = "\n".join(
            block["text"] for section in sections for block in section["blocks"]
        )
        body = text.encode("utf-8")
        extraction_sha256 = sha256_bytes(body)
        extraction_relative = (
            Path("extracted") / raw_sha256[:2] / raw_sha256 / "text.txt"
        )
        section_body = jsonl_bytes(section_rows)
        parse_sha256 = sha256_bytes(section_body)
        chunk_body = jsonl_bytes(chunk_rows)
        chunk_sha256 = sha256_bytes(chunk_body)
        parse_relative = (
            Path("parsed") / parse_sha256[:2] / parse_sha256 / "sections.jsonl"
        )
        chunk_relative = (
            Path("chunks") / chunk_sha256[:2] / chunk_sha256 / "chunks.jsonl"
        )
        atomic_write(corpus_root / extraction_relative, body, immutable=True)
        atomic_write(corpus_root / parse_relative, section_body, immutable=True)
        atomic_write(corpus_root / chunk_relative, chunk_body, immutable=True)
        record = {
            "schema": INDEX_SCHEMA,
            "raw_sha256": raw_sha256,
            "source_id": source_id,
            "candidate_key": candidate_key,
            "doi": doi,
            "media_type": media_type,
            "source_path": str(path),
            "extraction_path": str(corpus_root / extraction_relative),
            "extraction_relative_path": str(extraction_relative),
            "extraction_sha256": extraction_sha256,
            "extraction_characters": len(text),
            "parse_sha256": parse_sha256,
            "parse_relative_path": str(parse_relative),
            "chunk_sha256": chunk_sha256,
            "chunk_relative_path": str(chunk_relative),
            "sections": len(section_rows),
            "chunks": len(chunk_rows),
            "sentence_complete_chunks": sum(
                1 for row in chunk_rows if row["sentence_complete"]
            ),
            "warnings": warnings,
            "coverage": coverage,
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "extractor_name": EXTRACTOR_NAME,
            "extractor_version": EXTRACTOR_VERSION,
            "structure_version": STRUCTURE_VERSION,
            "char_cap": char_cap,
            "overlap_chars": overlap_chars,
            "parsed_at_utc": started,
        }
        atomic_json(_index_path(corpus_root, raw_sha256), record, immutable=True)
        return {
            "state": "extracted",
            "candidate_key": candidate_key,
            "raw_sha256": raw_sha256,
            "extraction_sha256": extraction_sha256,
            "parse_sha256": parse_sha256,
            "chunk_sha256": chunk_sha256,
            "chunks": len(chunk_rows),
            "characters": len(text),
            "warnings": warnings,
            "at_utc": _now(),
        }
    except Exception as error:  # noqa: BLE001 - one bad PDF must not end the pass
        return {
            "state": "failed",
            "candidate_key": candidate_key,
            "raw_sha256": raw_sha256,
            "error": f"{type(error).__name__}: {error}"[:500],
            "at_utc": _now(),
        }


def corpus_source_id(candidate_key: str, doi: str | None) -> str:
    """Return the source id the streaming bridge derives for this paper.

    The bridge builds its source record from the access receipt, so the same
    key reproduces the same id here. A chapter 2 parse therefore carries the
    exact ids the run will look for, and no record is rewritten at run time.
    """
    stable = candidate_key
    if doi:
        try:
            stable = normalize_doi(str(doi))
        except ValueError:
            stable = candidate_key
    return stable_id("src", stable)


def freeze(
    root: Path,
    *,
    access_run_dir: Path,
    legacy_freeze_dir: Path,
    freeze_id: str,
    run_id: str,
    code_commit: str,
) -> dict[str, Any]:
    """Write the chapter 2 manifest, order, receipt and access run directory."""
    items = {str(item["candidate_key"]): item for item in ready_items(access_run_dir)}
    order = [key for key in frozen_order(legacy_freeze_dir) if key in items]
    missing = sorted(set(items) - set(order))
    if missing:
        raise ValueError(
            f"{len(missing)} usable full texts are absent from the chapter 1 order"
        )
    records: list[dict[str, Any]] = []
    for position, key in enumerate(order, start=1):
        item = items[key]
        index = index_record(root, str(item["source_content_hash"]))
        if index is None:
            raise ValueError(f"the chapter 2 parse is missing for {key}")
        _verify_object(Path(index["extraction_path"]), index["extraction_sha256"])
        _verify_object(root / index["parse_relative_path"], index["parse_sha256"])
        _verify_object(root / index["chunk_relative_path"], index["chunk_sha256"])
        records.append(_manifest_record(position, item, index, root))
    body = jsonl_bytes(records)
    manifest_sha256 = sha256_bytes(body)
    order_sha256 = sha256_bytes(canonical_json(order).encode())
    freeze_dir = root / "corpus-freeze" / freeze_id
    freeze_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(freeze_dir / "chapter2-corpus-manifest.jsonl", body, immutable=True)
    descriptor = {
        "schema": DESCRIPTOR_SCHEMA,
        "freeze_id": freeze_id,
        "created_at_utc": _now(),
        "producer_code_commit": code_commit,
        "record_count": len(records),
        "manifest_sha256": manifest_sha256,
        "order_sha256": order_sha256,
        "ordering": {
            "rule": (
                "The chapter 1 frozen order of the same usable full texts, "
                "renumbered over the chapter 2 records."
            ),
            "derives_from": str(
                (legacy_freeze_dir / "full-text-ready-manifest.jsonl").resolve()
            ),
        },
        "corpus_source_version": CORPUS_SOURCE_VERSION,
        "bias_note": (
            "The manifest contains only previously accessible full texts. "
            "Access availability and metadata cues can bias later processing."
        ),
    }
    atomic_json(freeze_dir / "manifest-descriptor.json", descriptor, immutable=True)
    access_dir = root / "article-access" / freeze_id
    access = _write_access_run(
        access_dir,
        records=records,
        items=items,
        run_id=run_id,
        code_commit=code_commit,
        access_run_dir=access_run_dir,
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "freeze_id": freeze_id,
        "created_at_utc": _now(),
        "producer_code_commit": code_commit,
        "actions_not_performed": [
            "retrieval",
            "discovery",
            "model_request",
            "scientific_eligibility_decision",
            "modification of any chapter 1 object",
        ],
        "extractor": {
            "name": EXTRACTOR_NAME,
            "version": EXTRACTOR_VERSION,
            "command": "pdftotext -bbox-layout -nodiag",
            "poppler": _poppler_version(),
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "parser_key": PARSER_KEY,
            "structure_version": STRUCTURE_VERSION,
        },
        "derives_from": {
            "legacy_manifest": _file_record(
                legacy_freeze_dir / "full-text-ready-manifest.jsonl"
            ),
            "legacy_freeze_receipt": _file_record(
                legacy_freeze_dir / "freeze-receipt.json"
            ),
            "access_manifest": _file_record(access_run_dir / "run-manifest.json"),
            "access_receipt": _file_record(access_run_dir / "run-receipt.json"),
        },
        "counts": {
            "manifest_records": len(records),
            "unique_candidate_keys": len({row["candidate_key"] for row in records}),
            "unique_source_hashes": len({row["raw_sha256"] for row in records}),
            "chunks": sum(int(row["chunks"]) for row in records),
            "sentence_complete_chunks": sum(
                int(row["sentence_complete_chunks"]) for row in records
            ),
            "documents_with_column_pages": sum(
                1 for row in records if int(row["coverage"]["column_pages"]) > 0
            ),
        },
        "hashes": {
            "manifest_sha256": manifest_sha256,
            "order_sha256": order_sha256,
            "access_selection_keys_sha256": access["selection_keys_sha256"],
        },
        "outputs": {
            "manifest": str(freeze_dir / "chapter2-corpus-manifest.jsonl"),
            "descriptor": str(freeze_dir / "manifest-descriptor.json"),
            "access_run_dir": str(access_dir),
        },
        "checks": {
            "all_extraction_hashes_verified": True,
            "all_parse_hashes_verified": True,
            "all_chunk_hashes_verified": True,
            "candidate_keys_unique": len({row["candidate_key"] for row in records})
            == len(records),
            "output_order_strict": True,
            "ready_count_exact": len(records) == len(items),
        },
    }
    atomic_json(freeze_dir / "freeze-receipt.json", receipt, immutable=True)
    return receipt


def _manifest_record(
    position: int, item: dict[str, Any], index: dict[str, Any], root: Path
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "manifest_position": position,
        "candidate_key": str(item["candidate_key"]),
        "doi": item.get("doi"),
        "title": item.get("title"),
        "source_id": index["source_id"],
        "source_version": CORPUS_SOURCE_VERSION,
        "raw_sha256": index["raw_sha256"],
        "media_type": index["media_type"],
        "source_path": index["source_path"],
        "extraction_path": index["extraction_path"],
        "extraction_sha256": index["extraction_sha256"],
        "extraction_characters": index["extraction_characters"],
        "parse_sha256": index["parse_sha256"],
        "parse_path": str(root / index["parse_relative_path"]),
        "chunk_sha256": index["chunk_sha256"],
        "chunk_path": str(root / index["chunk_relative_path"]),
        "sections": index["sections"],
        "chunks": index["chunks"],
        "sentence_complete_chunks": index["sentence_complete_chunks"],
        "coverage": index["coverage"],
        "warnings": index["warnings"],
        "legacy_access_item": _file_record(Path(item["_item_path"])),
        "legacy_extraction_sha256": item.get("extraction_sha256"),
    }


def _write_access_run(
    access_dir: Path,
    *,
    records: list[dict[str, Any]],
    items: dict[str, Any],
    run_id: str,
    code_commit: str,
    access_run_dir: Path,
) -> dict[str, Any]:
    """Write a chapter 2 access run directory the streaming CLI can consume."""
    (access_dir / "items").mkdir(mode=0o700, parents=True, exist_ok=True)
    legacy_manifest = _read_json(access_run_dir / "run-manifest.json")
    legacy_selection = {
        str(row["candidate_key"]): row for row in legacy_manifest["selection"]
    }
    selection = []
    for record in records:
        key = record["candidate_key"]
        item = items[key]
        selected = dict(legacy_selection[key])
        selected["position"] = record["manifest_position"]
        selected["subgroup"] = item.get("subgroup")
        selection.append(selected)
    selection_keys_sha256 = sha256_bytes(
        canonical_json([row["candidate_key"] for row in selection]).encode()
    )
    manifest = {
        "schema": ACCESS_MANIFEST_SCHEMA,
        "run_id": run_id,
        "created_at_utc": _now(),
        "producer_code_commit": code_commit,
        "policy_id": legacy_manifest.get("policy_id"),
        "protocol_id": legacy_manifest.get("protocol_id"),
        "inputs": legacy_manifest.get("inputs", {}),
        "target_counts": legacy_manifest.get("target_counts", {}),
        "target_total": len(selection),
        "selection_keys_sha256": selection_keys_sha256,
        "limits": legacy_manifest.get("limits", {}),
        "smoke_sizes": legacy_manifest.get("smoke_sizes", []),
        "reuse_source_run_dir": None,
        "reuse_access_run_dir": str(access_run_dir.resolve()),
        "chapter2_corpus_source_version": CORPUS_SOURCE_VERSION,
        "selection": selection,
    }
    _write_once(access_dir / "run-manifest.json", manifest)
    for record in records:
        item = dict(items[record["candidate_key"]])
        item.pop("_item_path", None)
        item.update(
            {
                "schema": ACCESS_ITEM_SCHEMA,
                "run_id": run_id,
                "position": record["manifest_position"],
                "extraction_path": record["extraction_path"],
                "extraction_sha256": record["extraction_sha256"],
                "extraction_coverage": {
                    "article_body_recognized": True,
                    "characters": record["extraction_characters"],
                    "ocr": "not_applied",
                    "figures": "unknown_not_extracted",
                    "supplements": "unknown_not_extracted",
                    "tables": "unknown_not_extracted",
                    "pages": record["coverage"]["pages"],
                    "column_pages": record["coverage"]["column_pages"],
                },
                "reason_code": "chapter2_column_aware_reextraction",
                "reused_from": record["legacy_access_item"]["path"],
                "provenance": [
                    {
                        "kind": "chapter1_verified_object",
                        "path": record["legacy_access_item"]["path"],
                        "sha256": record["legacy_access_item"]["sha256"],
                    }
                ],
            }
        )
        _write_once(
            access_dir / "items" / f"item-{record['manifest_position']:06d}.json", item
        )
    counts = {
        "target": len(selection),
        "checked": len(selection),
        _READY: len(selection),
        "ready_for_eligibility": len(selection),
        "no_source_found": 0,
        "retryable_error": 0,
    }
    moment = _now()
    _write_once(
        access_dir / "progress.json",
        {
            "schema": ACCESS_PROGRESS_SCHEMA,
            "run_id": run_id,
            "stage": "chapter2_column_aware_reextraction",
            "state": "completed",
            "active": [],
            "counts": counts,
            "message": (
                "Every chapter 1 usable full text was re-extracted "
                "column aware for chapter 2."
            ),
            "policy_id": legacy_manifest.get("policy_id"),
            "producer_code_commit": code_commit,
            "selection_keys_sha256": selection_keys_sha256,
            "started_at_utc": moment,
            "updated_at_utc": moment,
            "latest_event_at_utc": moment,
            "completed_at_utc": moment,
        },
    )
    _write_once(
        access_dir / "run-receipt.json",
        {
            "schema": ACCESS_RECEIPT_SCHEMA,
            "run_id": run_id,
            "counts": counts,
            "selection_keys_sha256": selection_keys_sha256,
            "completed_at_utc": moment,
        },
    )
    return {"selection_keys_sha256": selection_keys_sha256}


def materialize_stream_input(
    root: Path, *, freeze_id: str, run_id: str
) -> dict[str, Any]:
    """Write the gate-bindable streaming input for one chapter 2 freeze.

    The streaming execution gate binds its input through the hashes of the
    access run manifest, the access run receipt, the frozen manifest and the
    frozen order. The chapter 2 access run records its selection but not those
    bindings, so this writes a second access run, under the chapter 2 root,
    that carries them and repeats the same items. Nothing is retrieved, no
    model runs, and no existing chapter 2 object changes. The write is
    idempotent: a repeated call verifies the stored objects and writes nothing.
    """
    if not freeze_id or not run_id or "/" in run_id:
        raise ValueError("the chapter 2 stream input needs a freeze id and a run id")
    freeze_dir = root / "corpus-freeze" / freeze_id
    access_dir = root / "article-access" / freeze_id
    descriptor_file = freeze_dir / "manifest-descriptor.json"
    manifest_file = freeze_dir / "chapter2-corpus-manifest.jsonl"
    descriptor = _read_json(descriptor_file)
    if (
        descriptor.get("schema") != DESCRIPTOR_SCHEMA
        or descriptor.get("freeze_id") != freeze_id
    ):
        raise ValueError("the chapter 2 freeze descriptor is invalid")
    manifest_sha256 = sha256_file(manifest_file)
    if manifest_sha256 != descriptor.get("manifest_sha256"):
        raise ValueError("the chapter 2 corpus manifest hash changed")
    access_manifest = _read_json(access_dir / "run-manifest.json")
    access_receipt = _read_json(access_dir / "run-receipt.json")
    selection = access_manifest.get("selection")
    if not isinstance(selection, list) or not selection:
        raise ValueError("the chapter 2 access run has no selection")
    order_sha256 = sha256_bytes(
        canonical_json([row["candidate_key"] for row in selection]).encode()
    )
    if (
        access_manifest.get("schema") != ACCESS_MANIFEST_SCHEMA
        or access_manifest.get("target_total") != len(selection)
        or access_manifest.get("selection_keys_sha256") != order_sha256
        or access_receipt.get("selection_keys_sha256") != order_sha256
        or descriptor.get("order_sha256") != order_sha256
        or descriptor.get("record_count") != len(selection)
    ):
        raise ValueError("the chapter 2 access run does not match its freeze")
    output_dir = root / STREAM_INPUT_DIRECTORY / run_id
    (output_dir / "items").mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = {
        "schema": ACCESS_MANIFEST_SCHEMA,
        "run_id": run_id,
        "target_total": len(selection),
        "selection_keys_sha256": order_sha256,
        "remaining_order_sha256": order_sha256,
        "frozen_manifest_sha256": manifest_sha256,
        "frozen_manifest_descriptor_sha256": sha256_file(descriptor_file),
        "freeze_id": freeze_id,
        "purpose": "Chapter 2 streaming input bound to the chapter 2 corpus freeze.",
        "scientific_eligibility_effect": "none",
        "chapter2_corpus_source_version": CORPUS_SOURCE_VERSION,
        "chapter2_access_run_dir": str(access_dir.resolve()),
        "chapter2_access_manifest_sha256": sha256_file(
            access_dir / "run-manifest.json"
        ),
        "chapter2_access_receipt_sha256": sha256_file(access_dir / "run-receipt.json"),
        "selection": selection,
    }
    _write_once(output_dir / "run-manifest.json", manifest)
    for position, row in enumerate(selection, start=1):
        item = _read_json(access_dir / "items" / f"item-{position:06d}.json")
        if (
            item.get("schema") != ACCESS_ITEM_SCHEMA
            or item.get("candidate_key") != row["candidate_key"]
            or item.get("access_state") != _READY
            or item.get("identity_verified") is not True
        ):
            raise ValueError("a chapter 2 access item does not match its selection")
        item["run_id"] = run_id
        _write_once(output_dir / "items" / f"item-{position:06d}.json", item)
    counts = {
        "checked": len(selection),
        "full_text_ready": len(selection),
        "ready_for_eligibility": len(selection),
        "target": len(selection),
    }
    _write_once(
        output_dir / "progress.json",
        {
            "schema": ACCESS_PROGRESS_SCHEMA,
            "state": "completed",
            "run_id": run_id,
            "current_stage": "materialized_from_chapter2_freeze",
            "counts": counts,
            "model_calls": 0,
            "paid_calls": 0,
        },
    )
    receipt = {
        "schema": ACCESS_RECEIPT_SCHEMA,
        "state": "completed",
        "run_id": run_id,
        "run_manifest_sha256": sha256_file(output_dir / "run-manifest.json"),
        "frozen_manifest_sha256": manifest_sha256,
        "frozen_manifest_descriptor_sha256": manifest[
            "frozen_manifest_descriptor_sha256"
        ],
        "selection_keys_sha256": order_sha256,
        "remaining_order_sha256": order_sha256,
        "counts": counts,
        "model_calls": 0,
        "paid_calls": 0,
        "scientific_eligibility_effect": "none",
    }
    _write_once(output_dir / "run-receipt.json", receipt)
    return {
        "access_run_dir": str(output_dir.resolve()),
        "run_id": run_id,
        "target_total": len(selection),
        "run_manifest_sha256": receipt["run_manifest_sha256"],
        "run_receipt_sha256": sha256_file(output_dir / "run-receipt.json"),
        "frozen_manifest_sha256": manifest_sha256,
        "order_sha256": order_sha256,
    }


def _run_tasks(tasks: list[tuple[Any, ...]], jobs: int) -> Iterator[dict[str, Any]]:
    if jobs == 1 or len(tasks) <= 1:
        for task in tasks:
            yield extract_one(*task)
        return
    with ProcessPoolExecutor(max_workers=jobs, initializer=_lower_priority) as pool:
        for result in pool.map(_extract_task, tasks, chunksize=1):
            yield result


def _extract_task(task: tuple[Any, ...]) -> dict[str, Any]:
    return extract_one(*task)


def _lower_priority() -> None:
    """Keep the re-extraction pass behind every interactive process."""
    try:
        os.nice(19)
    except OSError:
        pass


def _index_path(root: Path, raw_sha256: str) -> Path:
    return root / "index" / raw_sha256[:2] / f"{raw_sha256}.json"


def _verify_object(path: Path, digest: str) -> None:
    if not path.is_file() or sha256_file(path) != digest:
        raise ValueError(f"a chapter 2 corpus object does not verify: {path}")


def _file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    if path.is_file():
        existing = _read_json(path)
        comparable = dict(payload)
        for field in (
            "created_at_utc",
            "started_at_utc",
            "updated_at_utc",
            "latest_event_at_utc",
            "completed_at_utc",
        ):
            if field in existing and field in comparable:
                comparable[field] = existing[field]
        if existing != comparable:
            raise ValueError(f"a chapter 2 access object changed between runs: {path}")
        return
    atomic_json(path, payload, immutable=True)


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _root_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "created_at_utc"}


def _poppler_version() -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["pdftotext", "-v"], capture_output=True, text=True, timeout=30, check=False
        )
    except OSError:
        return "unknown"
    line = (result.stderr or result.stdout).splitlines()
    return line[0].strip() if line else "unknown"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
