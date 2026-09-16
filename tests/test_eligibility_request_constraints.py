"""The eligibility request must stay inside what the provider accepts.

Chapter 3's first production call (2026-09-16 07:24:51 UTC) returned HTTP 400
INVALID_ARGUMENT. Four diagnostic calls with the traced payload isolated the
cause: an ``enum`` inside the ``items`` of the ``reason_codes`` array. The
same request with that enum removed returned 200. Schema v3, which chapter 2
sent live 202 times, used only the keywords listed here, plus v4's
``description``, which the diagnostic calls showed the provider accepts.
"""

from __future__ import annotations

import json
from pathlib import Path

from arctic_qa import gemini_eligibility as eligibility

ROOT = Path(__file__).parents[1]
SCHEMAS = ROOT / "schemas"
# Keywords the provider accepted in live chapter 2 and chapter 3 calls.
ACCEPTED_KEYWORDS = {
    "$defs",
    "$ref",
    "$schema",
    "additionalProperties",
    "const",
    "description",
    "enum",
    "items",
    "maxItems",
    "minItems",
    "minLength",
    "properties",
    "required",
    "type",
}


def _walk(node: object, path: str, out: list[tuple[str, dict]]) -> None:
    if isinstance(node, dict):
        out.append((path, node))
        for key, value in node.items():
            if key in {"properties", "$defs"} and isinstance(value, dict):
                for name, child in value.items():
                    _walk(child, f"{path}/{key}/{name}", out)
            else:
                _walk(value, f"{path}/{key}", out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, f"{path}[{index}]", out)


def _keywords(node: object, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"properties", "$defs"} and isinstance(value, dict):
                for child in value.values():
                    _keywords(child, out)
            else:
                out.add(key)
                _keywords(value, out)
    elif isinstance(node, list):
        for value in node:
            _keywords(value, out)


def _schema(version: str) -> dict:
    return json.loads(
        (SCHEMAS / f"gemini-eligibility.{version}.schema.json").read_text(
            encoding="utf-8"
        )
    )


def test_the_live_eligibility_schemas_use_only_accepted_keywords() -> None:
    for version in ("v3", "v4"):
        used: set[str] = set()
        _keywords(_schema(version), used)
        assert used <= ACCEPTED_KEYWORDS, (version, sorted(used - ACCEPTED_KEYWORDS))


def test_no_enum_sits_inside_array_items() -> None:
    """The provider rejected this shape with HTTP 400 on 2026-09-16."""
    for version in ("v3", "v4"):
        nodes: list[tuple[str, dict]] = []
        _walk(_schema(version), "", nodes)
        for path, node in nodes:
            items = node.get("items")
            if isinstance(items, dict):
                assert "enum" not in items, (version, path)


def test_v4_states_the_reason_code_vocabulary_in_the_item_description() -> None:
    items = _schema("v4")["$defs"]["criterion"]["properties"]["reason_codes"]["items"]
    assert items["type"] == "string"
    listed = items["description"].split("One of: ", 1)[1].split(".", 1)[0]
    assert set(listed.split(", ")) == set(eligibility.ELIGIBILITY_REASON_CODES)


def test_the_validator_relaxation_is_a_no_op_on_the_shipped_schema() -> None:
    schema = _schema("v4")
    assert eligibility._measurement_relaxed_schema(schema) == schema
