"""Column-aware PDF text extraction.

The predecessor extractor called ``pdftotext -layout``. That mode keeps the
physical page layout, so a two-column page produces one output line per visual
line and glues the left and right column together with a run of spaces. Every
consumer downstream - eligibility spans, chunks, evidence quotes - then carried
the column gutter. This module reads the word geometry that poppler publishes
with ``pdftotext -bbox-layout`` and rebuilds the text in reading order instead.

Reading order is decided on the lines, not on poppler's own block grouping,
because poppler merges two narrow columns into one block. A recursive XY cut
splits the lines of a page into bands and columns, and each leaf of that cut is
then split into paragraphs. Line-level presentation damage (soft hyphens,
ligatures, a word split by a line wrap) is repaired inside the paragraph.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


EXTRACTOR_NAME = "pdftotext-bbox-layout-reading-order"
EXTRACTOR_VERSION = "1.0.0"

_XHTML = "{http://www.w3.org/1999/xhtml}"
_SOFT_HYPHEN = chr(0x00AD)
_ALLOWED_CONTROL = frozenset({0x09, 0x0A, 0x0D})

_HYPHENS = "".join(chr(point) for point in (0x2D, 0x2010, 0x00AD))
_LINE_BREAK_HYPHEN = re.compile(r"(?<=[^\W\d_])[" + _HYPHENS + "]$")
_SPACES = "".join(chr(point) for point in (0x20, 0x09, 0xA0, 0x2007, 0x202F))
_INLINE_SPACE = re.compile("[" + _SPACES + "]+")

# A gutter is a fraction of the page width. 1 percent of an A4 page is about 6
# points, which is wider than the space between two words of body text and
# narrower than the gutter of a two-column journal page.
_COLUMN_GAP_RATIO = 0.010
_MINIMUM_COLUMN_GAP = 5.0
# A band break is a vertical gap wider than this many line heights.
_BAND_GAP_RATIO = 1.6
# A paragraph break inside one column.
_PARAGRAPH_GAP_RATIO = 0.55
_PARAGRAPH_INDENT_RATIO = 0.018
_SHORT_LINE_RATIO = 0.90
# A page counts as multi-column only on this much repeated evidence.
_COLUMN_MINIMUM_LINES = 3
_COLUMN_BAND_SHARE = 0.4


def normalize_presentation(text: str) -> str:
    """Fold only the presentation damage that PDF extraction introduces.

    NFKC folds the fi and ffi ligatures back to plain letters. The soft hyphen
    carries no meaning once the line wrap is gone. Runs of spaces collapse.
    Nothing here changes a word, a number or a unit.
    """
    folded = unicodedata.normalize("NFKC", text.replace(_SOFT_HYPHEN, ""))
    return _INLINE_SPACE.sub(" ", folded).strip()


def extract_layout(
    path: Path, *, timeout: float = 300
) -> tuple[list[dict[str, Any]], set[int]]:
    """Return the reading-order blocks of the PDF and its multi-column pages.

    Each block carries its page number, its bounding box and its normalized
    text. Blocks keep the order a human reader follows, so the left column of a
    two-column page is complete before the right column starts.
    """
    blocks: list[dict[str, Any]] = []
    multi_column: set[int] = set()
    for page in read_pages(path, timeout=timeout):
        if _page_has_columns(page["lines"], page["width"]):
            multi_column.add(page["number"])
        for group in _reading_order(page["lines"], page["width"]):
            for block in _paragraphs(group, page["width"]):
                block["page"] = page["number"]
                block["page_width"] = page["width"]
                block["page_height"] = page["height"]
                blocks.append(block)
    return blocks, multi_column


def extract_blocks(path: Path, *, timeout: float = 300) -> list[dict[str, Any]]:
    """Return every text block of the PDF in reading order."""
    return extract_layout(path, timeout=timeout)[0]


def read_pages(path: Path, *, timeout: float = 300) -> list[dict[str, Any]]:
    """Return every page of the PDF with its text lines and their geometry."""
    result = subprocess.run(
        ["pdftotext", "-bbox-layout", "-nodiag", str(path), "-"],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:500]
        raise ValueError(f"pdftotext failed: {detail}")
    document = ET.fromstring(_xml_safe(result.stdout.decode("utf-8", errors="replace")))
    pages = []
    for number, page in enumerate(document.iter(f"{_XHTML}page"), start=1):
        lines = [
            row
            for row in map(_read_line, page.iter(f"{_XHTML}line"))
            if row is not None
        ]
        pages.append(
            {
                "number": number,
                "width": _number(page.get("width")),
                "height": _number(page.get("height")),
                "lines": lines,
            }
        )
    return pages


def _page_has_columns(lines: list[dict[str, Any]], width: float) -> bool:
    """Say whether the body text of one page sits in two or more columns.

    A page counts as multi-column when a real share of its horizontal bands
    hold text on both sides of a gutter. One aligned table row is not enough.
    """
    if width <= 0 or len(lines) < _COLUMN_MINIMUM_LINES * 2:
        return False
    gap = max(_MINIMUM_COLUMN_GAP, width * _COLUMN_GAP_RATIO)
    band_gap = _median([line["y1"] - line["y0"] for line in lines]) * _BAND_GAP_RATIO
    # A running head or a full-width caption joins the two columns into one
    # group, so look at the whole page and at each band separately.
    # The band must also carry a real share of the page. A two-column table
    # inside a one-column page splits the same way and is not a page layout.
    regions = [
        region
        for region in [lines, *_split(lines, "y0", "y1", band_gap)]
        if len(region) >= _COLUMN_BAND_SHARE * len(lines)
    ]
    return any(
        _column_groups(region, gap, _COLUMN_MINIMUM_LINES) >= 2 for region in regions
    )


def _column_groups(rows: list[dict[str, Any]], gap: float, minimum: int) -> int:
    """Count the horizontal groups that hold at least ``minimum`` lines."""
    return sum(1 for group in _split(rows, "x0", "x1", gap) if len(group) >= minimum)


def _xml_safe(text: str) -> str:
    """Drop the code points XML 1.0 forbids.

    poppler copies a stray control glyph straight through into its bbox output,
    and those bytes make the document unparsable.
    """
    return "".join(
        character
        for character in text
        if (point := ord(character)) in _ALLOWED_CONTROL
        or 0x20 <= point <= 0xD7FF
        or 0xE000 <= point <= 0xFFFD
        or 0x10000 <= point <= 0x10FFFF
    )


def _read_line(element: ET.Element) -> dict[str, Any] | None:
    words = [word.text or "" for word in element.iter(f"{_XHTML}word")]
    text = " ".join(word for word in words if word).strip()
    if not text:
        return None
    return {
        "x0": _number(element.get("xMin")),
        "x1": _number(element.get("xMax")),
        "y0": _number(element.get("yMin")),
        "y1": _number(element.get("yMax")),
        "text": text,
    }


def _reading_order(
    lines: list[dict[str, Any]], width: float
) -> list[list[dict[str, Any]]]:
    """Split the lines of one page into reading-order groups by a recursive XY cut.

    Each returned group is one band of one column, in the order a reader follows.
    The cut looks for a column gutter first. A full-width heading or figure
    blocks that cut for its own region, so the band cut separates it and the
    columns underneath split on the next step.
    """
    if not lines:
        return []
    column_gap = max(_MINIMUM_COLUMN_GAP, width * _COLUMN_GAP_RATIO)
    band_gap = _median([line["y1"] - line["y0"] for line in lines]) * _BAND_GAP_RATIO

    def cut(
        group: list[dict[str, Any]], rows_first: bool
    ) -> list[list[dict[str, Any]]]:
        if len(group) <= 1:
            return [list(group)]
        first = ("y0", "y1", band_gap) if rows_first else ("x0", "x1", column_gap)
        parts = _split(group, *first)
        if len(parts) > 1:
            return [row for part in parts for row in cut(part, not rows_first)]
        second = ("x0", "x1", column_gap) if rows_first else ("y0", "y1", band_gap)
        parts = _split(group, *second)
        if len(parts) > 1:
            return [row for part in parts for row in cut(part, rows_first)]
        return [sorted(group, key=lambda row: (row["y0"], row["x0"]))]

    return cut(list(lines), False)


def _paragraphs(lines: list[dict[str, Any]], width: float) -> list[dict[str, Any]]:
    """Group the lines of one reading-order leaf into paragraphs."""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda row: (row["y0"], row["x0"]))
    height = _median([line["y1"] - line["y0"] for line in ordered])
    left = min(line["x0"] for line in ordered)
    right = max(line["x1"] for line in ordered)
    span = max(1.0, right - left)
    indent = max(4.0, width * _PARAGRAPH_INDENT_RATIO)
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for line in ordered:
        if current and _starts_paragraph(
            current[-1], line, height, left, right, span, indent
        ):
            groups.append(current)
            current = []
        current.append(line)
    if current:
        groups.append(current)
    return [_block(group) for group in groups]


def _starts_paragraph(
    previous: dict[str, Any],
    line: dict[str, Any],
    height: float,
    left: float,
    right: float,
    span: float,
    indent: float,
) -> bool:
    if _LINE_BREAK_HYPHEN.search(previous["text"]) and line["text"][:1].islower():
        # The previous line stops in the middle of a word, so the paragraph runs on.
        return False
    if line["y0"] - previous["y1"] > height * _PARAGRAPH_GAP_RATIO:
        return True
    if line["x0"] - left > indent:
        return True
    return (
        previous["x1"] - left < span * _SHORT_LINE_RATIO
        and previous["x1"] < right - indent
    )


def _block(lines: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "x0": min(line["x0"] for line in lines),
        "x1": max(line["x1"] for line in lines),
        "y0": min(line["y0"] for line in lines),
        "y1": max(line["y1"] for line in lines),
        "line_count": len(lines),
        "text": normalize_presentation(_join_lines([line["text"] for line in lines])),
    }


def _join_lines(lines: list[str]) -> str:
    joined = ""
    for line in lines:
        current = line.strip()
        if not current:
            continue
        if not joined:
            joined = current
            continue
        if _LINE_BREAK_HYPHEN.search(joined) and current[:1].islower():
            joined = _LINE_BREAK_HYPHEN.sub("", joined) + current
        else:
            joined = f"{joined} {current}"
    return joined


def _split(
    rows: list[dict[str, Any]], low: str, high: str, gap: float
) -> list[list[dict[str, Any]]]:
    """Split rows into groups separated by a gap wider than ``gap``."""
    ordered = sorted(rows, key=lambda row: (row[low], row[high]))
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    edge = 0.0
    for row in ordered:
        if current and row[low] - edge > gap:
            groups.append(current)
            current = []
        edge = row[high] if not current else max(edge, row[high])
        current.append(row)
    if current:
        groups.append(current)
    return groups


def _median(values: list[float]) -> float:
    ordered = sorted(value for value in values if value > 0)
    if not ordered:
        return 1.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _number(value: str | None) -> float:
    try:
        return float(value or 0.0)
    except ValueError:
        return 0.0
