from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .db import Database, now
from .extraction import load_chunks


RULE_VERSION = "arctic-core-v1"
ARCTIC_LATITUDE = 66.56


def _load_regions() -> dict[str, set[str]]:
    path = Path(__file__).with_name("data") / "arctic_regions_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: {str(value).casefold() for value in values}
        for key, values in payload.items()
        if isinstance(values, list)
    }


@dataclass(frozen=True)
class ScreenDecision:
    state: str
    confidence: str
    eligibility: str
    reason: str


def decide(evidence: dict[str, Any]) -> ScreenDecision:
    regions = _load_regions()
    source_kind = evidence.get("evidence_kind")
    if source_kind not in {"study_setting", "methods_coordinates", "site_coordinates"}:
        return ScreenDecision(
            "unresolved", "unresolved", "pending", "study_setting_evidence_required"
        )
    if evidence.get("site_coverage") != "complete":
        return ScreenDecision(
            "unresolved", "unresolved", "pending", "complete_site_scope_required"
        )
    latitudes = evidence.get("latitudes") or []
    if not isinstance(latitudes, list) or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < -90
        or value > 90
        for value in latitudes
    ):
        return ScreenDecision(
            "unresolved", "unresolved", "pending", "invalid_coordinates"
        )
    has_core = any(value >= ARCTIC_LATITUDE for value in latitudes)
    has_noncore = any(value < ARCTIC_LATITUDE for value in latitudes)
    if has_core and has_noncore:
        return ScreenDecision(
            "mixed", "high", "excluded", "mixed_core_and_noncore_results"
        )
    if has_core:
        return ScreenDecision(
            "core_arctic", "high", "eligible", "latitude_at_or_above_boundary"
        )
    named = {str(value).casefold() for value in evidence.get("named_regions", [])}
    if named & regions["arctic_ocean_regions"]:
        return ScreenDecision(
            "core_arctic", "high", "eligible", "reviewed_arctic_ocean_region"
        )
    if named & regions["sub_arctic_related"]:
        return ScreenDecision(
            "subarctic_related", "high", "excluded", "subarctic_excluded_by_default"
        )
    if latitudes and all(
        isinstance(value, (int, float)) and value < ARCTIC_LATITUDE
        for value in latitudes
    ):
        return ScreenDecision(
            "excluded", "high", "excluded", "coordinates_below_boundary"
        )
    return ScreenDecision("unresolved", "unresolved", "pending", "geography_unresolved")


def screen_source(
    db: Database,
    source_id: str,
    evidence: dict[str, Any],
    namespace: Path | None = None,
) -> dict[str, Any]:
    source = db.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    if not source:
        raise ValueError(f"unknown source: {source_id}")
    if not _evidence_resolves(db, namespace, source, evidence):
        decision = ScreenDecision(
            "unresolved",
            "unresolved",
            "pending",
            "source_bound_geography_evidence_required",
        )
    else:
        decision = decide(evidence)
        if decision.reason not in {
            "invalid_coordinates",
            "complete_site_scope_required",
            "mixed_core_and_noncore_results",
            "study_setting_evidence_required",
        } and not _geographic_values_resolve(evidence):
            decision = ScreenDecision(
                "unresolved",
                "unresolved",
                "pending",
                "geographic_values_not_in_evidence",
            )
    with db.transaction():
        db.connection.execute(
            """UPDATE sources SET geography_state=?, geography_confidence=?, eligibility_state=?,
            inclusion_reason=?, scope_rule_version=?, scope_evidence_json=?, updated_at=? WHERE source_id=?""",
            (
                decision.state,
                decision.confidence,
                decision.eligibility,
                decision.reason,
                RULE_VERSION,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                now(),
                source_id,
            ),
        )
    return {
        "source_id": source_id,
        "geography_state": decision.state,
        "geography_confidence": decision.confidence,
        "eligibility_state": decision.eligibility,
        "reason": decision.reason,
        "rule_version": RULE_VERSION,
    }


def _evidence_resolves(
    db: Database,
    namespace: Path | None,
    source: dict[str, Any],
    evidence: dict[str, Any],
) -> bool:
    if namespace is None or not source.get("content_hash"):
        return False
    if evidence.get("source_content_hash") != source["content_hash"]:
        return False
    quote = evidence.get("evidence_quote")
    locator = evidence.get("locator") or {}
    if not isinstance(quote, str) or not quote or not isinstance(locator, dict):
        return False
    try:
        chunks = {
            row["chunk_id"]: row
            for row in load_chunks(db, namespace, source["source_id"])
        }
        chunk = chunks.get(locator.get("chunk_id"))
        start = int(locator["start_offset"])
        end = int(locator["end_offset"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        chunk
        and 0 <= start < end <= len(chunk["text"])
        and chunk["text"][start:end] == quote
    )


def _geographic_values_resolve(evidence: dict[str, Any]) -> bool:
    quote = str(evidence.get("evidence_quote", ""))
    normalized_quote = " ".join(quote.casefold().split())
    if not {"all", "entire", "complete"}.intersection(normalized_quote.split()):
        return False
    try:
        quote_numbers = {
            Decimal(value) for value in re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?", quote)
        }
        latitudes = {Decimal(str(value)) for value in evidence.get("latitudes", [])}
    except (InvalidOperation, TypeError, ValueError):
        return False
    regions = {
        " ".join(str(value).casefold().split())
        for value in evidence.get("named_regions", [])
        if str(value).strip()
    }
    if not latitudes and not regions:
        return False
    if latitudes and (
        not latitudes.issubset(quote_numbers)
        or not re.search(r"\b(?:n|north)\b", normalized_quote)
    ):
        return False
    return all(region in normalized_quote for region in regions)
