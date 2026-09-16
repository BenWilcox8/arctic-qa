"""Replay the recorded chapter 2 candidates through the current deterministic gates.

The chapter 2 yield audit (``data/arctic-ch2-yield-audit-r1``) recorded every
candidate of run ``chapter2-e8d4cad-r1`` in one evidence bundle per paper
family. Each bundle holds the candidate record, the cited chunk text and the
gate reasons the deployed code produced. This module runs those records
through ``_qa_gate_reasons`` as it is now and reports which candidates change
outcome. No model is called: every judge verdict is the recorded one, and the
answer-agreement record is recomputed only when the deterministic tier now
settles a case the recorded judge decided.

A candidate is "freed" when the replay leaves it with zero gate reasons. The
audit expected about 14 candidates in 9 families.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .generation import _qa_gate_reasons
from .validation import (
    ANSWER_AGREEMENT_CONTRACT_VERSION,
    DETERMINISTIC_CONTEXT_RULES_VERSION,
    NUMERIC_RULE_CONTRACT_VERSION,
    RECONSTRUCTION_RECORD_CONTRACT_VERSION,
    STANDALONE_DETERMINISTIC_REASON_PREFIX,
    context_only_span_records,
    question_answer_leaks_answer,
    reconstruction_matches,
    standalone_deterministic_reason,
)

CHAPTER2_REPLAY_CONTRACT_VERSION = "chapter2-gate-replay-v1"
DETERMINISTIC_AGREEMENT = {
    "contract_version": ANSWER_AGREEMENT_CONTRACT_VERSION,
    "method": "deterministic",
    "confidence_category": "authoritative_deterministic",
    "deterministic_match": True,
    "agreement": True,
    "judge": None,
}


def load_family_bundles(evidence_dir: Path) -> list[dict[str, Any]]:
    families_dir = evidence_dir / "families"
    if not families_dir.is_dir():
        raise FileNotFoundError(f"no family bundles under {evidence_dir}")
    bundles = []
    for path in sorted(families_dir.glob("family-*.json")):
        bundles.append(json.loads(path.read_text(encoding="utf-8")))
    return bundles


def _replay_agreement(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Return the agreement record the current tier implies, and how it was settled."""
    recorded = candidate.get("answer_agreement")
    matches = reconstruction_matches(
        candidate.get("answer") or {}, candidate.get("reconstruction") or {}
    )
    if matches:
        settled = "deterministic"
        if isinstance(recorded, dict) and recorded.get("method") == "llm_judge":
            settled = "deterministic_replaces_judge"
        return dict(DETERMINISTIC_AGREEMENT), settled
    if isinstance(recorded, dict) and recorded.get("method") == "llm_judge":
        return recorded, "recorded_judge"
    # The recorded tier matched and the current one does not: the case would
    # now go to the judge, whose verdict is unknown offline.
    return None, "judge_required"


def _collapse_namespace(reason: str) -> str:
    if reason.startswith(STANDALONE_DETERMINISTIC_REASON_PREFIX):
        return reason[len(STANDALONE_DETERMINISTIC_REASON_PREFIX) :]
    return reason


def replay_candidate(
    bundle: dict[str, Any], row: dict[str, Any]
) -> dict[str, Any]:
    candidate = row["candidate"]
    chunk_id = candidate["source"]["chunk_id"]
    chunk_text = bundle.get("source_chunk_texts", {}).get(chunk_id)
    if chunk_text is None:
        raise ValueError(f"bundle {bundle['family_id']} lacks chunk {chunk_id}")
    chunk = {"chunk_id": chunk_id, "text": chunk_text["text"]}
    question = str(candidate["question"])
    question_context = str(candidate.get("question_context", "") or "")
    answer = candidate["answer"]
    provenance = candidate.get("provenance")
    agreement, agreement_path = _replay_agreement(candidate)
    reasons = _qa_gate_reasons(
        chunk,
        question,
        answer,
        candidate.get("reconstruction") or {},
        candidate.get("answer_verification") or {},
        question_context,
        provenance,
        answer_agreement=agreement,
        standalone_verification=candidate.get("standalone_verification"),
        interpretation_spans=context_only_span_records(provenance),
    )
    if agreement is None and "reconstruction_disagreement" in reasons:
        # Offline the judge cannot answer; record the open question, not a kill.
        reasons = [reason for reason in reasons if reason != "reconstruction_disagreement"]
        reasons.append("answer_agreement_judge_required")
    # generate_candidate inserts the writer-time context reason ahead of the
    # gate reasons; the replay does the same so the sets are comparable.
    creation_reason = (
        "question_answer_leakage"
        if question_answer_leaks_answer(question, answer)
        else standalone_deterministic_reason(question, question_context)
    )
    if creation_reason and creation_reason not in reasons:
        reasons.insert(0, creation_reason)
    recorded = [
        reason for reason in (candidate.get("qa_gate_reasons") or []) if isinstance(reason, str)
    ]
    # Chapter 3 moved the free source-blind screen into its own
    # ``standalone_det_`` namespace (yield audit 4.2, F7). Chapter 2 recorded
    # the same verdicts under the bare code, so the comparison collapses the
    # prefix; the raw replayed list keeps the namespaced code.
    recorded_set = {_collapse_namespace(reason) for reason in recorded}
    replayed_set = {_collapse_namespace(reason) for reason in reasons}
    standalone = candidate.get("standalone_verification") or {}
    return {
        "family_id": bundle["family_id"],
        "item_id": row["item_id"],
        "recorded_status": row.get("status"),
        "standalone_pass": standalone.get("pass"),
        "recorded_reasons": recorded,
        "replayed_reasons": reasons,
        "removed": sorted(recorded_set - replayed_set),
        "added": sorted(replayed_set - recorded_set),
        "agreement_path": agreement_path,
        "freed": not reasons and bool(recorded),
        "accepted_before": row.get("status") in {"machine_accepted_unverified", "incomplete_non_mcq"},
        "regressed": not recorded and bool(reasons),
    }


def replay_chapter2_gates(evidence_dir: Path) -> dict[str, Any]:
    bundles = load_family_bundles(evidence_dir)
    rows: list[dict[str, Any]] = []
    for bundle in bundles:
        for row in bundle.get("candidates", []):
            rows.append(replay_candidate(bundle, row))
    freed = [row for row in rows if row["freed"]]
    regressed = [row for row in rows if row["regressed"]]
    changed = [row for row in rows if row["removed"] or row["added"]]
    removed_counts: dict[str, int] = {}
    added_counts: dict[str, int] = {}
    for row in rows:
        for reason in row["removed"]:
            removed_counts[reason] = removed_counts.get(reason, 0) + 1
        for reason in row["added"]:
            added_counts[reason] = added_counts.get(reason, 0) + 1
    return {
        "contract_version": CHAPTER2_REPLAY_CONTRACT_VERSION,
        "rule_versions": {
            "deterministic_context_rules_version": DETERMINISTIC_CONTEXT_RULES_VERSION,
            "reconstruction_record_contract_version": RECONSTRUCTION_RECORD_CONTRACT_VERSION,
            "numeric_rule_contract_version": NUMERIC_RULE_CONTRACT_VERSION,
        },
        "evidence_dir": str(evidence_dir),
        "candidates": len(rows),
        "families": len(bundles),
        "changed": len(changed),
        "freed_count": len(freed),
        "freed_families": sorted({row["family_id"] for row in freed}),
        "freed": [
            {
                "family_id": row["family_id"],
                "item_id": row["item_id"],
                "standalone_pass": row["standalone_pass"],
                "recorded_reasons": row["recorded_reasons"],
                "agreement_path": row["agreement_path"],
            }
            for row in freed
        ],
        "regressed": [
            {"family_id": row["family_id"], "item_id": row["item_id"], "added": row["added"]}
            for row in regressed
        ],
        "removed_reason_counts": dict(sorted(removed_counts.items())),
        "added_reason_counts": dict(sorted(added_counts.items())),
        "rows": rows,
    }
