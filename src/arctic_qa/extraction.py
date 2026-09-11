from __future__ import annotations

import json
import re
import subprocess
import tempfile
from datetime import UTC, datetime
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .db import Database, now
from .util import atomic_write, jsonl_bytes, sha256_bytes, stable_id


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
        text = "\n".join(
            line.strip() for line in "".join(self.buffer).splitlines() if line.strip()
        )
        if text:
            self.sections.append(
                {
                    "heading": self.current_heading,
                    "heading_level": self.current_level,
                    "heading_path": list(self.heading_stack),
                    "text": text,
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
) -> dict[str, Any]:
    source = db.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    if not source or not source.get("content_hash"):
        raise ValueError(f"source has no stored original: {source_id}")
    artifact = db.one(
        "SELECT * FROM artifacts WHERE source_id=? AND kind='original' AND content_hash=?",
        (source_id, source["content_hash"]),
    )
    if not artifact:
        raise ValueError(f"original artifact is missing for source: {source_id}")
    original = namespace / artifact["relative_path"]
    sections, warnings = _extract(
        original, source.get("media_type") or "application/octet-stream"
    )
    section_rows: list[dict[str, Any]] = []
    chunk_rows: list[dict[str, Any]] = []
    for number, section in enumerate(sections, start=1):
        section_id = stable_id(
            "section",
            source_id,
            source["content_hash"],
            "parser-v1",
            number,
            section["heading"],
        )
        row = {
            "section_id": section_id,
            "source_id": source_id,
            "sequence": number,
            "normalized_text_hash": sha256_bytes(
                " ".join(section["text"].split()).encode()
            ),
            **section,
        }
        section_rows.append(row)
        chunk_rows.extend(_chunk_section(row, char_cap, overlap_chars))
    section_body = jsonl_bytes(section_rows)
    parse_hash = sha256_bytes(section_body)
    chunk_body = jsonl_bytes(chunk_rows)
    chunk_hash = sha256_bytes(chunk_body)
    parse_relative = Path("parsed") / parse_hash[:2] / parse_hash / "sections.jsonl"
    chunk_relative = Path("chunks") / chunk_hash[:2] / chunk_hash / "chunks.jsonl"
    atomic_write(namespace / parse_relative, section_body, immutable=True)
    atomic_write(namespace / chunk_relative, chunk_body, immutable=True)
    parse_metadata = {
        "source_id": source_id,
        "raw_sha256": source["content_hash"],
        "parse_sha256": parse_hash,
        "sections": len(section_rows),
        "chunks": len(chunk_rows),
        "warnings": warnings,
        "parser_name": "arctic_qa.extraction",
        "parser_version": "1.0.0",
        "parsed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "char_cap": char_cap,
        "overlap_chars": overlap_chars,
    }
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
    return parse_metadata


def load_chunks(db: Database, namespace: Path, source_id: str) -> list[dict[str, Any]]:
    artifact = db.one(
        "SELECT * FROM artifacts WHERE source_id=? AND kind='chunks' ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    )
    if not artifact:
        raise ValueError(f"source has no chunks: {source_id}")
    return [
        json.loads(line)
        for line in (namespace / artifact["relative_path"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]


def _extract(path: Path, media_type: str) -> tuple[list[dict[str, Any]], list[str]]:
    if media_type == "application/pdf" or path.suffix.casefold() == ".pdf":
        return _extract_pdf(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    if (
        media_type in {"application/xml", "text/xml"}
        or path.suffix.casefold() == ".xml"
    ):
        return _extract_xml(raw)
    if media_type in {
        "text/html",
        "application/xhtml+xml",
    } or path.suffix.casefold() in {".html", ".htm"}:
        parser = StructuredHTMLParser()
        parser.feed(raw)
        parser.flush()
        return parser.sections, _warnings(
            "\n".join(row["text"] for row in parser.sections), parser.labels
        )
    text = raw.replace("\r\n", "\n")
    return [
        {
            "heading": "Document",
            "heading_level": 0,
            "heading_path": [],
            "text": text,
            "page": None,
            "object_labels": [],
        }
    ], _warnings(text, [])


def _extract_pdf(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    with tempfile.TemporaryDirectory(prefix="arctic-qa-pdf-") as directory:
        target = Path(directory) / "text.txt"
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), str(target)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"pdftotext failed: {result.stderr.strip()}")
        text = target.read_text(encoding="utf-8", errors="replace")
    pages = text.split("\f")
    sections = [
        {
            "heading": f"Page {number}",
            "heading_level": 0,
            "heading_path": [],
            "text": page.strip(),
            "page": number,
            "object_labels": _detect_labels(page),
        }
        for number, page in enumerate(pages, start=1)
        if page.strip()
    ]
    return sections, _warnings(
        text, [label for section in sections for label in section["object_labels"]]
    )


def _extract_xml(raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    root = ET.fromstring(raw)
    sections: list[dict[str, Any]] = []
    for number, element in enumerate(root.iter(), start=1):
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
        text = " ".join(" ".join(element.itertext()).split())
        if text:
            sections.append(
                {
                    "heading": title,
                    "heading_level": 1 if name in {"sec", "abstract", "body"} else 2,
                    "heading_path": [title],
                    "text": text,
                    "page": None,
                    "object_labels": _detect_labels(text),
                }
            )
    if not sections:
        text = " ".join(root.itertext())
        sections.append(
            {
                "heading": "Document",
                "heading_level": 0,
                "heading_path": [],
                "text": text,
                "page": None,
                "object_labels": [],
            }
        )
    return sections, _warnings(
        raw, [label for section in sections for label in section["object_labels"]]
    )


def _chunk_section(
    section: dict[str, Any], cap: int, overlap: int
) -> list[dict[str, Any]]:
    if cap <= 0 or overlap < 0 or overlap >= cap:
        raise ValueError(
            "chunk cap must be positive and overlap must be less than the cap"
        )
    text = section["text"]
    chunks = []
    start = 0
    sequence = 1
    while start < len(text):
        end = min(len(text), start + cap)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if boundary > start + cap // 2:
                end = boundary + 1
        chunk_text = text[start:end]
        chunks.append(
            {
                "chunk_id": stable_id("chunk", section["section_id"], start, end),
                "source_id": section["source_id"],
                "section_id": section["section_id"],
                "sequence": sequence,
                "heading": section["heading"],
                "heading_path": section["heading_path"],
                "page": section.get("page"),
                "block_index": sequence,
                "start_offset": start,
                "end_offset": end,
                "overlap_from_previous": overlap if start else 0,
                "text": chunk_text,
                "object_labels": section.get("object_labels", []),
            }
        )
        if end == len(text):
            break
        start = end - overlap
        sequence += 1
    return chunks


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
    if re.search(r"\b\d+(?:\.\d+)?\s*[±+/-]\s*\d", text) and not re.search(
        r"\b(?:m|cm|mm|km|°c|k|%|kg|g)\b", text, re.I
    ):
        warnings.append("possible_lost_units")
    if "table" in labels:
        warnings.append("table_content_present")
    if len("".join(text.split())) < 100:
        warnings.append("very_short_extraction")
    return sorted(set(warnings))
