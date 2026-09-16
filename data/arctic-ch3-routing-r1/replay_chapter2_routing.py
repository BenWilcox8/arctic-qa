"""Replay the chapter 3 routing rules over the 139 recorded chapter 2 candidates.

The chapter 2 yield audit (section 4.6 e) requires the lineage-wide repeat
detector to be replayed before it ships, and to show that it suppresses no
accepted item and no ``context_widened_revision`` attempt. This script replays
the whole new router, not the detector alone, so the no-retry guards, the
orthogonal numeric repair, the scope rungs and the widened slot pool are
measured on the same recorded evidence.

The evidence bundles are read-only. The script writes only its own report.

Run: nix develop -c bash -c 'PYTHONPATH=src python \
  data/arctic-ch3-routing-r1/replay_chapter2_routing.py'
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from arctic_qa import streaming as routing
from arctic_qa.util import canonical_json


EVIDENCE = Path(
    "/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/"
    "arctic-ch2-yield-audit-r1/evidence/families"
)
REPORT = Path(__file__).with_name("replay-chapter2-routing.json")
ACCEPTED_STATUS = "machine_accepted_unverified"


def _normalised_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    """Return one recorded chapter 2 attempt under the chapter 3 contract."""
    value = dict(attempt)
    value["contract_version"] = routing.GENERATION_ATTEMPT_CONTRACT_VERSION
    value.setdefault("repair_numeric_rule", False)
    value.setdefault("slot_lookup", None)
    return value


def _reason_codes(entry: dict[str, Any], bundle: dict[str, Any]) -> list[str]:
    """Return the reason codes chapter 2 recorded for one candidate."""
    candidate = entry.get("candidate") or {}
    codes = [
        str(code)
        for code in candidate.get("qa_gate_reasons") or []
        if isinstance(code, str)
    ]
    if codes:
        return codes
    for event in bundle.get("validation_events", {}).get(entry["item_id"], []) or []:
        event_codes = [
            str(code)
            for code in event.get("reason_codes") or []
            if isinstance(code, str)
        ]
        if event_codes:
            return event_codes
    if entry.get("status") == "incomplete_non_mcq":
        return ["insufficient_verified_distractors"]
    return []


def _activity_spans(bundle: dict[str, Any]) -> list[str]:
    """Return the eligibility activity span texts of one paper."""
    screen = bundle.get("eligibility_screen") or {}
    texts: list[str] = []
    for key in ("selected_activity_spans", "activity_spans", "arctic_activity_spans"):
        for span in screen.get(key) or []:
            if isinstance(span, dict):
                texts.append(str(span.get("text") or span.get("quote") or ""))
            elif isinstance(span, str):
                texts.append(span)
    for source in bundle.get("sources") or []:
        for span in (source.get("scope_evidence") or {}).get("activity_spans") or []:
            if isinstance(span, dict):
                texts.append(str(span.get("text") or span.get("quote") or ""))
    return [text for text in texts if text.strip()]


def _path(entry: dict[str, Any]) -> dict[str, Any]:
    candidate = entry.get("candidate") or {}
    attempt = _normalised_attempt(
        (candidate.get("provenance") or {}).get("generation_attempt") or {}
    )
    return {
        "attempt": attempt,
        "candidate": {
            "item_id": entry["item_id"],
            "candidate_json": canonical_json(candidate),
        },
        "candidate_status": entry.get("status"),
    }


def _census_row(
    entry: dict[str, Any], path: dict[str, Any], bundle: dict[str, Any]
) -> dict[str, Any]:
    """Report what the new router reads from one recorded rejected candidate."""
    codes = routing._routing_reason_codes(_reason_codes(entry, bundle))
    evidence = routing._routing_evidence(path)
    return {
        "item_id": entry["item_id"],
        "status": entry.get("status"),
        "reason_codes": codes,
        "scope_defect_demands": sorted(
            {str(defect["demand"]) for defect in evidence.get("scope_defect") or []}
        ),
        "unroutable_outcome": routing._unroutable_outcome(codes, evidence),
        "numeric_repair_rides_along": bool(
            "source_bound_numeric_rule_missing" in codes and len(codes) > 1
        ),
        "agreement_trigger_barred": bool(
            "reconstruction_disagreement" in codes
            and not routing._agreement_verdict_is_authoritative(
                evidence.get("agreement") or {}
            )
        ),
    }


def _replay_family(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Replay one family and report the fate of every recorded repair."""
    entries = [
        entry
        for entry in bundle.get("candidates") or []
        if ((entry.get("candidate") or {}).get("provenance") or {}).get(
            "generation_attempt"
        )
    ]
    entries.sort(key=lambda entry: (entry.get("created_at") or "", entry["item_id"]))
    family_id = str(bundle.get("family_id") or "family")
    activity = _activity_spans(bundle)
    by_attempt_id: dict[str, dict[str, Any]] = {}
    paths: dict[tuple[int, int], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    census: list[dict[str, Any]] = []
    for entry in entries:
        path = _path(entry)
        if entry.get("status") != ACCEPTED_STATUS:
            census.append(_census_row(entry, path, bundle))
        attempt = path["attempt"]
        parent_id = attempt.get("parent_attempt_id")
        if parent_id and parent_id in by_attempt_id:
            parent_path = by_attempt_id[parent_id]
            parent_entry = parent_path["entry"]
            codes = _reason_codes(parent_entry, bundle)
            evidence = routing._routing_evidence(parent_path["path"])
            texts = [*(evidence.get("writer_texts") or []), *activity]
            slot_evidence = (
                routing._slot_evidence_types(texts) if texts else None
            )
            unroutable: list[str] = []
            replayed = routing._next_generation_attempt(
                campaign_id="chapter2-e8d4cad-r1",
                family_id=family_id,
                paths=dict(paths),
                failed_path=parent_path["path"],
                reason_codes=codes,
                slot_evidence=slot_evidence,
                evidence=evidence,
                unroutable=unroutable,
            )
            rows.append(
                {
                    "family_id": family_id,
                    "item_id": entry["item_id"],
                    "recorded_kind": attempt.get("attempt_kind"),
                    "recorded_trigger": attempt.get("trigger_reason_code"),
                    "recorded_status": entry.get("status"),
                    "parent_reason_codes": codes,
                    "replayed_kind": (
                        replayed.get("attempt_kind") if replayed else None
                    ),
                    "stopped": replayed is None,
                    "stop_outcome": unroutable[0] if unroutable else None,
                    "accepted": entry.get("status") == ACCEPTED_STATUS,
                }
            )
        key = routing._path_key(attempt)
        paths[key] = path
        by_attempt_id[str(attempt.get("attempt_id"))] = {
            "path": path,
            "entry": entry,
        }
    return rows, census


def main() -> None:
    rows: list[dict[str, Any]] = []
    census: list[dict[str, Any]] = []
    families = sorted(EVIDENCE.glob("family-*.json"))
    candidates = 0
    for file in families:
        bundle = json.loads(file.read_text(encoding="utf-8"))
        candidates += len(bundle.get("candidates") or [])
        family_rows, family_census = _replay_family(bundle)
        rows.extend(family_rows)
        census.extend(family_census)
    stopped = [row for row in rows if row["stopped"]]
    suppressed_accepted = [row for row in stopped if row["accepted"]]
    # The audit's rule is about the lineage-wide repeat detector, which stops a
    # repair with no outcome code. A guard stop is a separate, named decision.
    detector_stops = [row for row in stopped if row["stop_outcome"] is None]
    suppressed_widened = [
        row
        for row in detector_stops
        if row["recorded_kind"] == "context_widened_revision"
    ]
    changed = [
        row
        for row in rows
        if not row["stopped"] and row["replayed_kind"] != row["recorded_kind"]
    ]
    report = {
        "schema": "arctic-ch3-routing-replay-v1",
        "families": len(families),
        "recorded_candidates": candidates,
        "recorded_repairs": len(rows),
        "stopped_repairs": len(stopped),
        "stop_outcomes": dict(
            Counter(row["stop_outcome"] or "no_rung" for row in stopped)
        ),
        "stopped_by_recorded_kind": dict(
            Counter(row["recorded_kind"] for row in stopped)
        ),
        "suppressed_accepted_items": suppressed_accepted,
        "repeat_detector_stops": len(detector_stops),
        "repeat_detector_stops_by_kind": dict(
            Counter(row["recorded_kind"] for row in detector_stops)
        ),
        "detector_suppressed_context_widened_revisions": len(suppressed_widened),
        "detector_suppressed_accepted_items": [
            row for row in detector_stops if row["accepted"]
        ],
        "rerouted_repairs": dict(
            Counter(
                f"{row['recorded_kind']} -> {row['replayed_kind']}" for row in changed
            )
        ),
        "rejected_candidates_examined": len(census),
        "guard_would_stop": dict(
            Counter(
                row["unroutable_outcome"]
                for row in census
                if row["unroutable_outcome"]
            )
        ),
        "scope_defect_demands": dict(
            Counter(
                demand for row in census for demand in row["scope_defect_demands"]
            )
        ),
        "candidates_with_a_scope_defect": sum(
            1 for row in census if row["scope_defect_demands"]
        ),
        "numeric_repair_rides_along": sum(
            1 for row in census if row["numeric_repair_rides_along"]
        ),
        "agreement_triggers_barred": sum(
            1 for row in census if row["agreement_trigger_barred"]
        ),
        "guard_stopped_candidates": [
            {"item_id": row["item_id"], "outcome": row["unroutable_outcome"],
             "reason_codes": row["reason_codes"]}
            for row in census
            if row["unroutable_outcome"]
        ],
        "stopped_attempts": [
            {
                "family_id": row["family_id"],
                "item_id": row["item_id"],
                "recorded_kind": row["recorded_kind"],
                "recorded_trigger": row["recorded_trigger"],
                "parent_reason_codes": row["parent_reason_codes"],
                "stop_outcome": row["stop_outcome"],
            }
            for row in stopped
        ],
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"stopped_attempts", "guard_stopped_candidates"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
