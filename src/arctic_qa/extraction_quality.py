"""Measure the extraction defects the r15 audit found, corpus against corpus.

The audit found a column gutter in 81 of 95 evidence quotes. This module counts
the same four defects on any text, so the chapter 1 extraction and the chapter 2
extraction of the same papers can be compared with one number each.

The four measures:

``gutter_rate``            share of text lines that glue two columns together.
``mid_word_break_rate``    mid-word line breaks per thousand characters.
``presentation_rate``      ligatures and soft hyphens per thousand characters.
``sentence_complete_rate`` share of chunks that start and end on a sentence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .util import atomic_json

# Two column fragments on one physical line, glued by the gutter. Both sides
# must be prose, so an aligned numeric table column does not count.
_GUTTER = re.compile(r"[A-Za-z,.;:)][ ]{3,}[A-Za-z(]")
# A word cut by a line wrap, still carrying its hyphen and the line break.
_MID_WORD_BREAK = re.compile(r"[^\W\d_][-" + chr(0x2010) + chr(0x00AD) + r"]\n[a-z]")
_LIGATURES = "".join(chr(point) for point in range(0xFB00, 0xFB07))
_PRESENTATION = re.compile("[" + _LIGATURES + chr(0x00AD) + "]")
_SENTENCE_TAIL = re.compile(r"[.!?" + chr(0x2026) + r"][\"'" + chr(0x201D) + r")\]]*$")

QUALITY_SCHEMA = "arctic-qa-extraction-quality-v1"
_THOUSAND = 1000.0


def measure_text(text: str) -> dict[str, Any]:
    """Count the extraction defects in one extracted document."""
    lines = [line for line in text.splitlines() if line.strip()]
    gutter_lines = sum(1 for line in lines if _GUTTER.search(line))
    characters = max(1, len(text))
    return {
        "lines": len(lines),
        "gutter_lines": gutter_lines,
        "gutter_rate": gutter_lines / max(1, len(lines)),
        "mid_word_breaks": len(_MID_WORD_BREAK.findall(text)),
        "mid_word_break_rate": len(_MID_WORD_BREAK.findall(text))
        * _THOUSAND
        / characters,
        "presentation_marks": len(_PRESENTATION.findall(text)),
        "presentation_rate": len(_PRESENTATION.findall(text)) * _THOUSAND / characters,
        "characters": len(text),
    }


def measure_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Count the defects a reader sees in the stored chunks of one document."""
    complete = 0
    gutter = 0
    for chunk in chunks:
        text = str(chunk.get("text") or "").strip()
        if text and _SENTENCE_TAIL.search(text):
            complete += 1
        if _GUTTER.search(text):
            gutter += 1
    total = max(1, len(chunks))
    return {
        "chunks": len(chunks),
        "sentence_complete_chunks": complete,
        "sentence_complete_rate": complete / total,
        "gutter_chunks": gutter,
        "gutter_chunk_rate": gutter / total,
    }


def legacy_chunks(
    text: str, cap: int = 6000, overlap: int = 500
) -> list[dict[str, Any]]:
    """Chunk text the way the chapter 1 extractor did.

    The chapter 1 extractor made one section for each page and cut a fixed
    window, so a chunk could start or end inside a sentence. This function
    repeats that algorithm, so the two corpora can be compared chunk for chunk.
    """
    chunks: list[dict[str, Any]] = []
    for page in text.split("\f"):
        body = page.strip()
        start = 0
        while start < len(body):
            end = min(len(body), start + cap)
            if end < len(body):
                boundary = max(
                    body.rfind("\n", start, end), body.rfind(". ", start, end)
                )
                if boundary > start + cap // 2:
                    end = boundary + 1
            chunks.append({"text": body[start:end]})
            if end == len(body):
                break
            start = end - overlap
    return chunks


def quality_report(
    root: Path,
    *,
    access_run_dir: Path,
    sample_size: int = 50,
    report_file: Path | None = None,
) -> dict[str, Any]:
    """Compare chapter 1 and chapter 2 extraction on the two-column papers.

    The sample is every re-extracted paper whose chapter 2 parse recorded at
    least one multi-column page, taken in the frozen order until the sample size
    is reached. Both corpora are measured on the same papers.
    """
    from .chapter2_corpus import index_record, ready_items

    rows: list[dict[str, Any]] = []
    for item in ready_items(access_run_dir):
        if len(rows) >= sample_size:
            break
        index = index_record(root, str(item["source_content_hash"]))
        if index is None or int(index["coverage"]["column_pages"]) < 1:
            continue
        legacy_path = Path(str(item.get("extraction_path") or ""))
        chapter2_path = Path(index["extraction_path"])
        if not legacy_path.is_file() or not chapter2_path.is_file():
            continue
        legacy = measure_text(legacy_path.read_text(encoding="utf-8", errors="replace"))
        chapter2 = measure_text(
            chapter2_path.read_text(encoding="utf-8", errors="replace")
        )
        rows.append(
            {
                "candidate_key": item["candidate_key"],
                "column_pages": index["coverage"]["column_pages"],
                "pages": index["coverage"]["pages"],
                "legacy": legacy,
                "chapter2": chapter2,
                "legacy_chunks": measure_chunks(
                    legacy_chunks(
                        legacy_path.read_text(encoding="utf-8", errors="replace")
                    )
                ),
                "chapter2_chunks": measure_chunks(_load_chunks(root, index)),
            }
        )
    report = {
        "schema": QUALITY_SCHEMA,
        "sample_size": len(rows),
        "sample_rule": (
            "Papers whose chapter 2 parse recorded at least one multi-column "
            "page, in the chapter 1 access order."
        ),
        "measures": {
            "gutter_rate": "share of non-empty text lines that glue two columns",
            "mid_word_break_rate": "mid-word line breaks per thousand characters",
            "presentation_rate": "ligatures and soft hyphens per thousand characters",
            "sentence_complete_rate": "share of chunks ending on a sentence",
        },
        "legacy": _summarize(rows, "legacy"),
        "chapter2": _summarize(rows, "chapter2"),
        "legacy_chunks": _summarize_chunks(rows, "legacy_chunks"),
        "chapter2_chunks": _summarize_chunks(rows, "chapter2_chunks"),
        "documents_with_any_gutter_line": {
            "legacy": sum(1 for row in rows if row["legacy"]["gutter_lines"] > 0),
            "chapter2": sum(1 for row in rows if row["chapter2"]["gutter_lines"] > 0),
        },
        "documents": rows,
    }
    if report_file is not None:
        atomic_json(report_file, report)
    return report


def _summarize_chunks(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return {
        "documents": len(rows),
        "chunks": sum(row[key]["chunks"] for row in rows),
        "sentence_complete_chunks": sum(
            row[key]["sentence_complete_chunks"] for row in rows
        ),
        "sentence_complete_rate": _mean(
            [row[key]["sentence_complete_rate"] for row in rows]
        ),
        "gutter_chunks": sum(row[key]["gutter_chunks"] for row in rows),
        "gutter_chunk_rate": _mean([row[key]["gutter_chunk_rate"] for row in rows]),
    }


def _summarize(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return {
        "documents": len(rows),
        "lines": sum(row[key]["lines"] for row in rows),
        "gutter_lines": sum(row[key]["gutter_lines"] for row in rows),
        "gutter_rate": _mean([row[key]["gutter_rate"] for row in rows]),
        "mid_word_breaks": sum(row[key]["mid_word_breaks"] for row in rows),
        "mid_word_break_rate": _mean([row[key]["mid_word_break_rate"] for row in rows]),
        "presentation_marks": sum(row[key]["presentation_marks"] for row in rows),
        "presentation_rate": _mean([row[key]["presentation_rate"] for row in rows]),
    }


def _load_chunks(root: Path, index: dict[str, Any]) -> list[dict[str, Any]]:
    path = root / index["chunk_relative_path"]
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
