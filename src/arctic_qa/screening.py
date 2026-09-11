from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Database, now


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
    if evidence.get("mixed"):
        return ScreenDecision(
            "mixed", "high", "excluded", "mixed_core_and_noncore_results"
        )
    latitudes = evidence.get("latitudes") or []
    if any(
        isinstance(value, (int, float)) and value >= ARCTIC_LATITUDE
        for value in latitudes
    ):
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
    db: Database, source_id: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    source = db.one("SELECT * FROM sources WHERE source_id=?", (source_id,))
    if not source:
        raise ValueError(f"unknown source: {source_id}")
    decision = decide(evidence)
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
