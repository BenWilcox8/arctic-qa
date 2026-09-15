"""Section, heading and sentence structure over ordered text blocks.

``pdf_layout`` returns reading-order blocks. This module turns those blocks into
the sections and sentence-complete chunks the pipeline stores. It also holds the
sentence splitter, so chunking and the extraction-quality measurements agree on
where a sentence ends.
"""

from __future__ import annotations

import re
from typing import Any


STRUCTURE_VERSION = "1.0.0"

_KNOWN_HEADINGS = frozenset(
    {
        "abstract",
        "acknowledgement",
        "acknowledgements",
        "acknowledgments",
        "appendix",
        "author contributions",
        "availability of data and materials",
        "background",
        "code availability",
        "competing interests",
        "conclusion",
        "conclusions",
        "conclusions and outlook",
        "data availability",
        "data availability statement",
        "discussion",
        "experimental section",
        "funding",
        "highlights",
        "introduction",
        "keywords",
        "limitations",
        "materials",
        "materials and methods",
        "method",
        "methodology",
        "methods",
        "methods and materials",
        "plain language summary",
        "references",
        "references cited",
        "result",
        "results",
        "results and discussion",
        "significance",
        "study area",
        "study site",
        "summary",
        "supplementary information",
        "supplementary material",
        "supporting information",
        "theory",
    }
)

_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
_CAPTION = re.compile(
    r"^\s*(?:supplementary\s+)?(?:fig(?:ure)?\.?|table|scheme|extended data)\s*"
    r"[0-9ivx]+",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = tuple(".,;:")
_HEADING_CHARACTERS = 140
_HEADING_LINES = 3
_HEADING_HEIGHT_RATIO = 1.08

_DIGIT_RUN = re.compile(r"\d+")
_RUNNING_HEAD_PAGES = 3
_RUNNING_HEAD_SHARE = 0.4
_RUNNING_HEAD_MARGIN = 0.08
_RUNNING_HEAD_CHARACTERS = 200

_SENTENCE_END = re.compile(
    r"[.!?" + chr(0x2026) + r"]+[\"'" + chr(0x2019) + chr(0x201D) + r")\]]*"
)
_CLOSERS = "".join(
    chr(point) for point in (0x29, 0x5D, 0x7D, 0x22, 0x27, 0x2019, 0x201D)
)
_WORD_BEFORE = re.compile(r"([\w." + chr(0x2019) + r"'-]+)$", re.UNICODE)
_ABBREVIATIONS = frozenset(
    {
        "al",
        "approx",
        "ca",
        "cf",
        "chap",
        "dr",
        "e.g",
        "ed",
        "eds",
        "eq",
        "eqs",
        "esp",
        "et",
        "etc",
        "fig",
        "figs",
        "i.e",
        "inc",
        "jr",
        "ltd",
        "max",
        "min",
        "mr",
        "mrs",
        "ms",
        "mt",
        "no",
        "nos",
        "p",
        "pp",
        "prof",
        "ref",
        "refs",
        "resp",
        "sec",
        "sr",
        "st",
        "suppl",
        "tab",
        "univ",
        "viz",
        "vol",
        "vs",
    }
)


def drop_running_heads(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Remove the repeated page header, footer and page number blocks.

    A running head repeats the journal name or the DOI on every page. Left in,
    it interrupts every section and lands inside evidence quotes.
    """
    pages = {int(block["page"]) for block in blocks}
    if len(pages) < _RUNNING_HEAD_PAGES:
        return list(blocks), 0
    seen: dict[str, set[int]] = {}
    for block in blocks:
        if not _in_page_margin(block) or len(block["text"]) > _RUNNING_HEAD_CHARACTERS:
            continue
        seen.setdefault(_running_head_key(block["text"]), set()).add(int(block["page"]))
    repeated = {
        key
        for key, hits in seen.items()
        if len(hits) >= _RUNNING_HEAD_PAGES
        and len(hits) >= _RUNNING_HEAD_SHARE * len(pages)
    }
    kept = []
    removed = 0
    for block in blocks:
        if (
            _in_page_margin(block)
            and len(block["text"]) <= _RUNNING_HEAD_CHARACTERS
            and _running_head_key(block["text"]) in repeated
        ):
            removed += 1
            continue
        kept.append(block)
    return kept, removed


def sections_from_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group reading-order blocks into heading-delimited sections.

    Every block reaches exactly one section, so no text is lost. A heading block
    opens its section and stays inside it.
    """
    body_height = _median(
        [
            _line_height(block)
            for block in blocks
            if len(block["text"]) >= _RUNNING_HEAD_CHARACTERS
        ]
    )
    sections: list[dict[str, Any]] = []
    stack: list[str] = []
    current: dict[str, Any] | None = None
    for block in blocks:
        heading = _heading(block, body_height)
        if heading is not None:
            title, level = heading
            stack = stack[: level - 1]
            stack.append(title)
            current = {
                "heading": title,
                "heading_level": level,
                "heading_path": list(stack),
                "page": int(block["page"]),
                # The heading is the first block of its own section. A heading
                # that only named the section would drop its text from the
                # document, and the reader needs that line.
                "blocks": [block],
            }
            sections.append(current)
            continue
        if current is None:
            current = {
                "heading": "Document",
                "heading_level": 0,
                "heading_path": [],
                "page": int(block["page"]),
                "blocks": [],
            }
            sections.append(current)
        current["blocks"].append(block)
    return [section for section in sections if section["blocks"]]


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return the (start, end) span of every sentence in one block of text.

    A newline always ends a sentence, so a block boundary never merges two
    paragraphs into one unit.
    """
    spans: list[tuple[int, int]] = []
    for line_start, line in _lines(text):
        start = 0
        for match in _SENTENCE_END.finditer(line):
            stop = match.end()
            if line[stop : stop + 1] not in ("", " "):
                continue
            if not _is_sentence_boundary(line, match.start(), stop):
                continue
            piece = line[start:stop]
            if piece.strip():
                spans.append((line_start + start + _lead(piece), line_start + stop))
            start = stop
        tail = line[start:]
        if tail.strip():
            spans.append(
                (line_start + start + _lead(tail), line_start + len(line.rstrip()))
            )
    return spans


def _lines(text: str) -> list[tuple[int, str]]:
    result = []
    offset = 0
    for line in text.split("\n"):
        result.append((offset, line))
        offset += len(line) + 1
    return result


def _lead(piece: str) -> int:
    return len(piece) - len(piece.lstrip())


def _is_sentence_boundary(text: str, dot: int, stop: int) -> bool:
    following = text[stop:].lstrip()
    if following and following[0].islower():
        return False
    head = text[:dot].rstrip(_CLOSERS)
    match = _WORD_BEFORE.search(head)
    token = (match.group(1) if match else "").rstrip(".")
    if not token:
        return False
    if len(token) == 1 and token.isalpha():
        return False
    if token.casefold() in _ABBREVIATIONS:
        return False
    # A bare number that opens the line is a list or section marker, not a
    # sentence end.
    return not (token.isdigit() and head.strip() == token)


def _heading(block: dict[str, Any], body_height: float) -> tuple[str, int] | None:
    text = block["text"]
    if (
        len(text) > _HEADING_CHARACTERS
        or int(block.get("line_count") or 1) > _HEADING_LINES
        or text.endswith(_TRAILING_PUNCTUATION)
        or _CAPTION.match(text)
    ):
        return None
    numbered = _NUMBERED_HEADING.match(text)
    if numbered and not numbered.group(2)[0].islower():
        return numbered.group(2).strip(), len(numbered.group(1).split("."))
    plain = text.strip().casefold().rstrip(":")
    if plain in _KNOWN_HEADINGS:
        return text.strip(), 1
    if body_height > 0 and _line_height(block) >= _HEADING_HEIGHT_RATIO * body_height:
        return text.strip(), 2
    return None


def _line_height(block: dict[str, Any]) -> float:
    count = max(1, int(block.get("line_count") or 1))
    return (float(block.get("y1") or 0.0) - float(block.get("y0") or 0.0)) / count


def _in_page_margin(block: dict[str, Any]) -> bool:
    height = float(block.get("page_height") or 0.0)
    if height <= 0:
        return False
    margin = height * _RUNNING_HEAD_MARGIN
    return (
        float(block.get("y1") or 0.0) <= margin
        or float(block.get("y0") or 0.0) >= height - margin
    )


def _running_head_key(text: str) -> str:
    return _DIGIT_RUN.sub("#", " ".join(text.split()).casefold())


def _median(values: list[float]) -> float:
    ordered = sorted(value for value in values if value > 0)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
