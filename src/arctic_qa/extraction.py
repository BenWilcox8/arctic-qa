from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .db import Database, now
from .pdf_layout import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    extract_layout,
    normalize_presentation,
)
from .text_structure import (
    STRUCTURE_VERSION,
    drop_running_heads,
    sections_from_blocks,
    sentence_spans,
)
from .util import atomic_write, jsonl_bytes, sha256_bytes, sha256_file, stable_id


PARSER_NAME = "arctic_qa.extraction"
PARSER_VERSION = "2.0.0"
# The identity key of a parsed section. Changing the extractor must change it,
# so a chapter 2 parse never collides with a stored predecessor parse.
PARSER_KEY = "parser-v2"


class StructuredHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[dict[str, Any]] = []
        self.heading_stack: list[str] = []
        self.current_heading = "Document"
        self.current_level = 0
        self.buffer: list[str] = []
        self.capture_heading: int | None = None
        self.heading_buffer: list[str] = []
        self.labels: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.capture_heading = int(tag[1])
            self.heading_buffer = []
        if tag in {"table", "figure", "math"}:
            self.labels.append(tag)
        if tag in {"p", "div", "li", "br", "tr"}:
            self.buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.capture_heading is not None and tag == f"h{self.capture_heading}":
            heading = (
                " ".join("".join(self.heading_buffer).split()) or "Untitled section"
            )
            level = self.capture_heading
            self.heading_stack = self.heading_stack[: level - 1]
            self.heading_stack.append(heading)
            self.current_heading = heading
            self.current_level = level
            self.capture_heading = None
            self.heading_buffer = []
        if tag in {"p", "div", "li", "table", "figure", "math", "tr"}:
            self.buffer.append("\n")

    def handle_data(self, data: str) -> None:
        if self.capture_heading is not None:
            self.heading_buffer.append(data)
        else:
            self.buffer.append(data)

    def flush(self) -> None:
        blocks = [
            normalize_presentation(line)
            for line in "".join(self.buffer).splitlines()
            if line.strip()
        ]
        if blocks:
            self.sections.append(
                {
                    "heading": self.current_heading,
                    "heading_level": self.current_level,
                    "heading_path": list(self.heading_stack),
                    "page": None,
                    "blocks": [
                        {
                            "page": None,
                            "text": text,
                            "object_labels": _detect_labels(text),
                        }
                        for text in blocks
                    ],
                    "object_labels": sorted(set(self.labels)),
                }
            )
        self.buffer = []
        self.labels = []


def extract_source(
    db: Database,
    namespace: Path,
    source_id: str,
    *,
    char_cap: int = 6000,
    overlap_chars: int = 500,
    corpus_root: Path | None = None,
) -> dict[str, Any]:
    """Parse one stored original into sections and sentence-complete chunks.

    ``corpus_root`` names where the parsed and chunk objects are written. It
    defaults to the chapter 2 corpus root when that root exists, and to the
    namespace otherwise. A chapter 2 root keeps the new objects out of every
    predecessor directory while the artifact rows stay in one database.

    When the chapter 2 root already holds a verified parse of the same bytes,
    this returns that frozen parse instead of extracting again. The run then
    reads the exact objects the chapter 2 freeze receipt hashed.
    """
    source = db.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    if not source or not source.get("content_hash"):
        raise ValueError(f"source has no stored original: {source_id}")
    artifact = db.one(
        "SELECT * FROM artifacts WHERE source_id=? AND kind='original' AND content_hash=?",
        (source_id, source["content_hash"]),
    )
    if not artifact:
        raise ValueError(f"original artifact is missing for source: {source_id}")
    root = corpus_root or _default_corpus_root(namespace)
    frozen = _frozen_parse(root, source_id, str(source["content_hash"]))
    if frozen is not None:
        parse_relative = Path(frozen["parse_relative_path"])
        chunk_relative = Path(frozen["chunk_relative_path"])
        parse_hash = str(frozen["parse_sha256"])
        chunk_hash = str(frozen["chunk_sha256"])
        parse_metadata = {
            "source_id": source_id,
            "raw_sha256": source["content_hash"],
            "parse_sha256": parse_hash,
            "chunk_sha256": chunk_hash,
            "sections": frozen["sections"],
            "chunks": frozen["chunks"],
            "warnings": frozen["warnings"],
            "coverage": frozen["coverage"],
            "parser_name": frozen["parser_name"],
            "parser_version": frozen["parser_version"],
            "extractor_name": frozen["extractor_name"],
            "extractor_version": frozen["extractor_version"],
            "structure_version": frozen["structure_version"],
            "parsed_at": frozen["parsed_at_utc"],
            "char_cap": frozen["char_cap"],
            "overlap_chars": frozen["overlap_chars"],
            "corpus_root": str(root),
            "frozen_corpus_parse": True,
        }
        _register(
            db,
            source_id,
            parse_hash,
            parse_relative,
            chunk_hash,
            chunk_relative,
            parse_metadata,
        )
        return parse_metadata
    original = namespace / artifact["relative_path"]
    sections, warnings, coverage = extract_document(
        original, source.get("media_type") or "application/octet-stream"
    )
    section_rows, chunk_rows = build_records(
        source_id, source["content_hash"], sections, char_cap, overlap_chars
    )
    section_body = jsonl_bytes(section_rows)
    parse_hash = sha256_bytes(section_body)
    chunk_body = jsonl_bytes(chunk_rows)
    chunk_hash = sha256_bytes(chunk_body)
    parse_relative = Path("parsed") / parse_hash[:2] / parse_hash / "sections.jsonl"
    chunk_relative = Path("chunks") / chunk_hash[:2] / chunk_hash / "chunks.jsonl"
    atomic_write(root / parse_relative, section_body, immutable=True)
    atomic_write(root / chunk_relative, chunk_body, immutable=True)
    parse_metadata = {
        "source_id": source_id,
        "raw_sha256": source["content_hash"],
        "parse_sha256": parse_hash,
        "chunk_sha256": chunk_hash,
        "sections": len(section_rows),
        "chunks": len(chunk_rows),
        "warnings": warnings,
        "coverage": coverage,
        "parser_name": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "structure_version": STRUCTURE_VERSION,
        "parsed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "char_cap": char_cap,
        "overlap_chars": overlap_chars,
        "corpus_root": str(root),
        "frozen_corpus_parse": False,
    }
    _register(
        db,
        source_id,
        parse_hash,
        parse_relative,
        chunk_hash,
        chunk_relative,
        parse_metadata,
    )
    return parse_metadata


def _register(
    db: Database,
    source_id: str,
    parse_hash: str,
    parse_relative: Path,
    chunk_hash: str,
    chunk_relative: Path,
    parse_metadata: dict[str, Any],
) -> None:
    """Record the parsed and chunk objects as artifacts of this source.

    Every insert is new: the digest is part of the artifact identity, so a
    chapter 2 parse adds rows and never rewrites a predecessor row.
    """
    with db.transaction():
        for kind, digest, relative in (
            ("sections", parse_hash, parse_relative),
            ("chunks", chunk_hash, chunk_relative),
        ):
            db.connection.execute(
                """INSERT OR IGNORE INTO artifacts
                (artifact_id,source_id,kind,content_hash,relative_path,media_type,created_at,metadata_json)
                VALUES (?,?,?,?,?,'application/x-ndjson',?,?)""",
                (
                    stable_id("artifact", source_id, kind, digest),
                    source_id,
                    kind,
                    digest,
                    str(relative),
                    now(),
                    json.dumps(parse_metadata, sort_keys=True),
                ),
            )


def _default_corpus_root(namespace: Path) -> Path:
    # Imported here because the chapter 2 module builds on this one.
    from .chapter2_corpus import chapter2_root

    return chapter2_root(namespace)


def _frozen_parse(
    root: Path, source_id: str, content_hash: str
) -> dict[str, Any] | None:
    """Return a verified frozen chapter 2 parse of these bytes, if there is one."""
    from .chapter2_corpus import index_record

    record = index_record(root, content_hash)
    if record is None or record.get("source_id") != source_id:
        return None
    for relative, digest in (
        (record["parse_relative_path"], record["parse_sha256"]),
        (record["chunk_relative_path"], record["chunk_sha256"]),
    ):
        path = root / relative
        if not path.is_file() or sha256_file(path) != digest:
            return None
    return record


def load_chunks(db: Database, namespace: Path, source_id: str) -> list[dict[str, Any]]:
    artifact = db.one(
        "SELECT * FROM artifacts WHERE source_id=? AND kind='chunks' ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    )
    if not artifact:
        raise ValueError(f"source has no chunks: {source_id}")
    metadata = json.loads(artifact["metadata_json"] or "{}")
    root = Path(metadata.get("corpus_root") or namespace)
    path = root / artifact["relative_path"]
    if not path.is_file():
        path = namespace / artifact["relative_path"]
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def build_records(
    source_id: str,
    content_hash: str,
    sections: list[dict[str, Any]],
    char_cap: int,
    overlap_chars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the stored section and chunk rows for one parsed document."""
    section_rows: list[dict[str, Any]] = []
    chunk_rows: list[dict[str, Any]] = []
    for number, section in enumerate(sections, start=1):
        section_id = stable_id(
            "section",
            source_id,
            content_hash,
            PARSER_KEY,
            number,
            section["heading"],
        )
        text, locators = _section_text(section["blocks"])
        labels = sorted(
            {
                label
                for block in section["blocks"]
                for label in block.get("object_labels", [])
            }
        )
        row = {
            "section_id": section_id,
            "source_id": source_id,
            "sequence": number,
            "heading": section["heading"],
            "heading_level": section["heading_level"],
            "heading_path": section["heading_path"],
            "page": section.get("page"),
            "page_start": locators[0]["page"] if locators else section.get("page"),
            "page_end": locators[-1]["page"] if locators else section.get("page"),
            "text": text,
            "blocks": locators,
            "object_labels": labels,
            "normalized_text_hash": sha256_bytes(" ".join(text.split()).encode()),
        }
        section_rows.append(row)
        chunk_rows.extend(_chunk_section(row, char_cap, overlap_chars))
    return section_rows, chunk_rows


def extract_document(
    path: Path, media_type: str
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Parse one stored original into sections, warnings and coverage counts."""
    if media_type == "application/pdf" or path.suffix.casefold() == ".pdf":
        return _extract_pdf(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    if (
        media_type in {"application/xml", "text/xml"}
        or path.suffix.casefold() == ".xml"
    ):
        sections = _extract_xml(raw)
    elif media_type in {
        "text/html",
        "application/xhtml+xml",
    } or path.suffix.casefold() in {".html", ".htm"}:
        parser = StructuredHTMLParser()
        parser.feed(raw)
        parser.flush()
        sections = parser.sections
    else:
        sections = [
            {
                "heading": "Document",
                "heading_level": 0,
                "heading_path": [],
                "page": None,
                "blocks": [
                    {
                        "page": None,
                        "text": normalize_presentation(line),
                        "object_labels": _detect_labels(line),
                    }
                    for line in raw.replace("\r\n", "\n").split("\n")
                    if line.strip()
                ],
            }
        ]
    text = "\n".join(
        block["text"] for section in sections for block in section["blocks"]
    )
    labels = [
        label
        for section in sections
        for block in section["blocks"]
        for label in block.get("object_labels", [])
    ]
    coverage = {
        "pages": 0,
        "blocks": sum(len(section["blocks"]) for section in sections),
        "column_pages": 0,
        "running_head_blocks_removed": 0,
        "characters": len(text),
    }
    return sections, _warnings(text, labels), coverage


def _extract_pdf(
    path: Path,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    blocks, multi_column = extract_layout(path)
    pages = {int(block["page"]) for block in blocks}
    kept, removed = drop_running_heads(blocks)
    for block in kept:
        block["object_labels"] = _detect_labels(block["text"])
    sections = sections_from_blocks(kept)
    text = "\n".join(block["text"] for block in kept)
    warnings = _warnings(
        text, [label for block in kept for label in block["object_labels"]]
    )
    if multi_column:
        warnings.append("column_layout_detected")
    if not kept:
        warnings.append("no_text_layer")
    coverage = {
        "pages": len(pages),
        "blocks": len(kept),
        "column_pages": len(multi_column),
        "running_head_blocks_removed": removed,
        "characters": len(text),
    }
    return sections, sorted(set(warnings)), coverage


def _extract_xml(raw: str) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    sections: list[dict[str, Any]] = []
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name not in {"sec", "abstract", "body", "p"}:
            continue
        title = next(
            (
                " ".join(child.itertext())
                for child in element
                if child.tag.rsplit("}", 1)[-1] == "title"
            ),
            name,
        )
        text = normalize_presentation(" ".join(" ".join(element.itertext()).split()))
        if text:
            sections.append(
                {
                    "heading": title,
                    "heading_level": 1 if name in {"sec", "abstract", "body"} else 2,
                    "heading_path": [title],
                    "page": None,
                    "blocks": [
                        {
                            "page": None,
                            "text": text,
                            "object_labels": _detect_labels(text),
                        }
                    ],
                }
            )
    if not sections:
        text = normalize_presentation(" ".join(root.itertext()))
        sections.append(
            {
                "heading": "Document",
                "heading_level": 0,
                "heading_path": [],
                "page": None,
                "blocks": [{"page": None, "text": text, "object_labels": []}],
            }
        )
    return sections


def _section_text(
    blocks: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    parts: list[str] = []
    locators: list[dict[str, Any]] = []
    offset = 0
    for index, block in enumerate(blocks, start=1):
        text = block["text"]
        parts.append(text)
        locators.append(
            {
                "block_index": index,
                "page": block.get("page"),
                "start_offset": offset,
                "end_offset": offset + len(text),
                "object_labels": block.get("object_labels", []),
            }
        )
        offset += len(text) + 1
    return "\n".join(parts), locators


def _chunk_section(
    section: dict[str, Any], cap: int, overlap: int
) -> list[dict[str, Any]]:
    """Split one section into chunks that start and end on a sentence boundary."""
    if cap <= 0 or overlap < 0 or overlap >= cap:
        raise ValueError(
            "chunk cap must be positive and overlap must be less than the cap"
        )
    text = section["text"]
    units = sentence_spans(text)
    chunks: list[dict[str, Any]] = []
    index = 0
    sequence = 1
    carried = 0
    while index < len(units):
        start, stop = units[index]
        if stop - start > cap:
            # One sentence longer than the whole cap. Nothing can keep this
            # chunk sentence-complete, so split it and say so on every piece.
            position = start
            while position < stop:
                edge = min(position + cap, stop)
                chunks.append(
                    _chunk_row(section, sequence, position, edge, text, 0, False)
                )
                sequence += 1
                position = edge
            index += 1
            carried = 0
            continue
        last = index
        end = stop
        while last + 1 < len(units) and units[last + 1][1] - start <= cap:
            last += 1
            end = units[last][1]
        chunks.append(_chunk_row(section, sequence, start, end, text, carried, True))
        sequence += 1
        if last + 1 >= len(units):
            break
        following = last + 1
        carried = 0
        if overlap:
            rewind = following
            span = 0
            while rewind - 1 > index and span < overlap:
                rewind -= 1
                span += units[rewind][1] - units[rewind][0]
            if rewind < following:
                carried = units[following - 1][1] - units[rewind][0]
                following = rewind
        index = following
    return chunks


def _chunk_row(
    section: dict[str, Any],
    sequence: int,
    start: int,
    end: int,
    section_text: str,
    carried: int,
    complete: bool,
) -> dict[str, Any]:
    locators = [
        block
        for block in section.get("blocks", [])
        if block["start_offset"] < end and block["end_offset"] > start
    ] or section.get("blocks", [])[:1]
    pages = [block["page"] for block in locators if block.get("page") is not None]
    labels = sorted({label for block in locators for label in block["object_labels"]})
    return {
        "chunk_id": stable_id("chunk", section["section_id"], start, end),
        "source_id": section["source_id"],
        "section_id": section["section_id"],
        "sequence": sequence,
        "heading": section["heading"],
        "heading_path": section["heading_path"],
        "page": pages[0] if pages else section.get("page"),
        "page_start": pages[0] if pages else section.get("page"),
        "page_end": pages[-1] if pages else section.get("page"),
        "block_index": locators[0]["block_index"] if locators else sequence,
        "start_offset": start,
        "end_offset": end,
        "overlap_from_previous": carried,
        "sentence_complete": complete,
        "text": section_text[start:end],
        "object_labels": labels,
    }


def _detect_labels(text: str) -> list[str]:
    labels = []
    for name, pattern in (
        ("table", r"(?im)^\s*table\s+\d+"),
        ("figure", r"(?im)^\s*(figure|fig\.)\s+\d+"),
        ("equation", r"(?im)^\s*(equation|eq\.)\s+\d+"),
        ("caption", r"(?im)^\s*(table|figure|fig\.)\s+\d+.*"),
    ):
        if re.search(pattern, text):
            labels.append(name)
    return labels


def _warnings(text: str, labels: list[str]) -> list[str]:
    warnings = []
    replacement_rate = text.count("�") / max(1, len(text))
    if replacement_rate > 0.001:
        warnings.append("possible_encoding_corruption")
    if re.search(
        r"\b\d+(?:\.\d+)?\s*[" + chr(0x00B1) + r"+/-]\s*\d", text
    ) and not re.search(
        r"\b(?:m|cm|mm|km|" + chr(0x00B0) + r"c|k|%|kg|g)\b", text, re.I
    ):
        warnings.append("possible_lost_units")
    if "table" in labels:
        warnings.append("table_content_present")
    if len("".join(text.split())) < 100:
        warnings.append("very_short_extraction")
    return sorted(set(warnings))
