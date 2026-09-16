"""Phase E measures for the chapter 3 run, from run artifacts only.

Reads the shared ledger, the state database, the eligibility run directory and
the streaming input manifest of one chapter 3 run and prints measures 1, 2, 3
and 6 of ``data/arctic-ch3-integration-r1/phase-e-measurement-plan.md`` plus
the free measures the integrated runtime records. Measures 4 and 5 need reader
labels and are not computed here. No model call, no write.

Usage (inside the devshell of the deployed runtime):
    PYTHONPATH=src python data/arctic-ch3-production-run-r1/phase_e_measures.py \
        --ledger <shared ledger> --state-db <state.sqlite3> \
        --eligibility-run-dir <chapter3 eligibility run dir> \
        --stream-input-dir <chapter3 streaming input> \
        --eligibility-prompt-file <prompt v8> --rescreen-prompt-file <re-screen v2> \
        [--run-id chapter3-7dc6485-r1] [--campaign-id arctic-qa-production-campaign-003]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arctic_qa.gemini_eligibility import _DIMENSION_MARKERS  # noqa: E402
from arctic_qa.streaming import BENCHMARK_CANDIDATE_PREDICATE  # noqa: E402

CALIBRATION_PAPER_PREFIX = "standalone-calibration:"
WATCH_STATES = ("screening_error", "unresolved_rescreenable")
ATTEMPT_RANK = {"initial": 0, "format_repair": 1, "geography_rescreen": 2}
# Chapter 2 values from the phase E plan and the yield audit cost summary.
CHAPTER2 = {
    "measure_1_zero_context_share": {"families_without_span": 23, "families": 56},
    "measure_2_no_date_share": {"families_without_date": 42, "families": 56},
    "measure_3_screening_error_share": {"errors": 38, "papers": 200},
    "measure_6_usd_per_accepted_item": {"spent_usd": "19.995149", "accepted": 6},
}
TARGETS = {
    "measure_1_zero_context_share": "under 0.10",
    "measure_2_no_date_share": "under 0.30",
    "measure_3_screening_error_share": "under 0.05",
    "measure_6_usd_per_accepted_item": "under USD 1.00",
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def share(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def last_rows(run_dir: Path, prompt_hashes: set[str]) -> dict[str, dict]:
    """The last eligibility row per paper under the active prompt versions."""
    selected: dict[str, dict] = {}
    for path in sorted((run_dir / "jobs").glob("*.json")):
        item = read_json(path)
        if item.get("prompt_sha256") not in prompt_hashes:
            continue
        attempt = item.get("eligibility_attempt") or {}
        order = (
            ATTEMPT_RANK.get(str(attempt.get("kind") or "initial"), 0),
            int(attempt.get("attempt") or 1),
        )
        key = str(item["candidate_key"])
        held = selected.get(key)
        if held is None or order > held["_order"]:
            selected[key] = {**item, "_order": order}
    return selected


def all_rows(run_dir: Path, prompt_hashes: set[str]) -> list[dict]:
    rows = []
    for path in sorted((run_dir / "jobs").glob("*.json")):
        item = read_json(path)
        if item.get("prompt_sha256") in prompt_hashes:
            rows.append(item)
    return rows


def has_date(text: str) -> bool:
    return bool(_DIMENSION_MARKERS["period"].search(text))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--eligibility-run-dir", type=Path, required=True)
    parser.add_argument("--stream-input-dir", type=Path, required=True)
    parser.add_argument("--eligibility-prompt-file", type=Path, required=True)
    parser.add_argument("--rescreen-prompt-file", type=Path, required=True)
    parser.add_argument("--run-id", default="chapter3-7dc6485-r1")
    parser.add_argument("--campaign-id", default="arctic-qa-production-campaign-003")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    # ---- ledger: the run's calls by stage, calibration booked separately ----
    ledger = read_json(args.ledger)
    stage_cost: Counter = Counter()
    stage_calls: Counter = Counter()
    stage_states: dict[str, Counter] = defaultdict(Counter)
    calibration_cost = Decimal("0")
    calibration_calls = 0
    cost_by_family: Counter = Counter()
    cost_by_paper: Counter = Counter()
    slot_lookup_by_paper: Counter = Counter()
    for request in ledger["requests"].values():
        if request.get("run_id") != args.run_id:
            continue
        cost = Decimal(request.get("actual_cost_usd") or 0)
        if str(request.get("paper_id", "")).startswith(CALIBRATION_PAPER_PREFIX):
            calibration_cost += cost
            calibration_calls += 1
            continue
        stage = str(request.get("stage"))
        stage_cost[stage] += cost
        stage_calls[stage] += 1
        stage_states[stage][str(request.get("state"))] += 1
        cost_by_family[str(request.get("family_id"))] += cost
        cost_by_paper[str(request.get("paper_id"))] += cost
        if stage == "repair":
            slot_lookup_by_paper[str(request.get("paper_id"))] += 1
    run_spent = sum(stage_cost.values(), Decimal("0"))

    # ---- state database ------------------------------------------------------
    con = sqlite3.connect(f"file:{args.state_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    def rows(sql: str, *params) -> list[dict]:
        try:
            return [dict(row) for row in con.execute(sql, params)]
        except sqlite3.OperationalError as error:
            # A table the chapter 3 migration adds is absent until the runtime
            # first opens the database; report it as empty, not as a crash.
            if "no such table" in str(error):
                return []
            raise

    benchmark = rows(
        f"SELECT * FROM candidates WHERE run_id=? AND {BENCHMARK_CANDIDATE_PREDICATE} "
        "ORDER BY created_at",
        args.campaign_id,
    )
    call_records = rows(
        f"SELECT status, COUNT(*) AS n FROM candidates WHERE run_id=? AND NOT "
        f"({BENCHMARK_CANDIDATE_PREDICATE}) GROUP BY status",
        args.campaign_id,
    )
    for row in benchmark:
        row["candidate"] = json.loads(row["candidate_json"])
    families: dict[str, list[dict]] = defaultdict(list)
    for row in benchmark:
        families[row["paper_family_id"]].append(row)
    accepted = [
        row for row in benchmark if row["status"] == "machine_accepted_unverified"
    ]
    status_counts = Counter(row["status"] for row in benchmark)

    # Measure 1: candidates with zero forwarded context-only spans.
    zero_context = 0
    for row in benchmark:
        spans = (
            (row["candidate"].get("provenance") or {}).get("context_only_source") or {}
        ).get("spans") or []
        if not spans:
            zero_context += 1
    families_without_span = 0
    for family_rows in families.values():
        first = family_rows[0]["candidate"]
        spans = ((first.get("provenance") or {}).get("context_only_source") or {}).get(
            "spans"
        ) or []
        if not spans:
            families_without_span += 1

    # Measure 2: families whose first candidate carries no date in the forwarded
    # spans or in question_context.
    families_without_date = 0
    for family_rows in families.values():
        first = family_rows[0]["candidate"]
        spans = ((first.get("provenance") or {}).get("context_only_source") or {}).get(
            "spans"
        ) or []
        text = "\n".join(
            str(span.get("text") or span.get("display_text") or "") for span in spans
        )
        text += "\n" + str(first.get("question_context") or "")
        if not has_date(text):
            families_without_date += 1

    # Measure 3: screening errors on the last eligibility row per paper.
    prompt_hashes = {
        sha256_file(args.eligibility_prompt_file),
        sha256_file(args.rescreen_prompt_file),
    }
    last = last_rows(args.eligibility_run_dir, prompt_hashes)
    every = all_rows(args.eligibility_run_dir, prompt_hashes)
    papers_screened = len(last)
    last_states = Counter(str(row.get("state")) for row in last.values())
    last_decisions = Counter(
        str((row.get("validation") or {}).get("decision")) for row in last.values()
    )
    screening_errors = sum(
        1 for row in last.values() if row.get("state") in WATCH_STATES
    )
    attempt_kinds = Counter(
        str((row.get("eligibility_attempt") or {}).get("kind") or "initial")
        for row in every
    )
    initial_states = Counter(
        str(row.get("state"))
        for row in every
        if str((row.get("eligibility_attempt") or {}).get("kind") or "initial")
        == "initial"
    )
    two_pass = [
        (row.get("validation") or {}).get("shadow_two_pass")
        or row.get("shadow_two_pass")
        for row in every
    ]
    two_pass = [record for record in two_pass if isinstance(record, dict)]
    two_pass_summary = {
        "rows_with_record": len(two_pass),
        "decision_path_true": sum(1 for r in two_pass if r.get("decision_path")),
        "short_view_holds_arctic_marker": sum(
            1 for r in two_pass if r.get("short_view_holds_arctic_marker")
        ),
        "activity_spans_inside_short_view": sum(
            int(r.get("activity_spans_inside_short_view") or 0) for r in two_pass
        ),
        "selected_activity_spans": sum(
            int(r.get("selected_activity_spans") or 0) for r in two_pass
        ),
        "booked_usd": "0",
    }

    # Free measures from provenance.
    skip_reasons: Counter = Counter()
    shadow_cohort = 0
    shadow_recall_failures = []
    reask_count = 0
    reask_second_evidenced = 0
    option_plan = {"proposed": 0, "prefiltered": 0, "verified": 0, "reserve": 0}
    option_malformed_reask = 0
    option_set_failures = 0
    superlative_shadow = 0
    served_from_bank = 0
    resolvability_stated = 0
    resolvability_unresolved = 0
    resolvability_reasons: Counter = Counter()
    rebind_outcomes: Counter = Counter()
    for row in benchmark:
        candidate = row["candidate"]
        provenance = candidate.get("provenance") or {}
        plan = provenance.get("judge_call_plan") or {}
        skip_reasons[str(plan.get("skip_reason"))] += 1
        if plan.get("shadow_cohort"):
            shadow_cohort += 1
            shadow = plan.get("shadow_gate_reasons")
            persisted = candidate.get("qa_gate_reasons") or []
            if isinstance(shadow, list):
                extra = sorted(set(shadow) - set(persisted))
                if extra:
                    shadow_recall_failures.append(
                        {"item_id": row["item_id"], "extra_full_suite_codes": extra}
                    )
        reask = provenance.get("standalone_verification_reask")
        if isinstance(reask, dict):
            reask_count += 1
            verdict = candidate.get("standalone_verification") or {}
            if verdict.get("evidence") or verdict.get("evidence_spans"):
                reask_second_evidenced += 1
        option = provenance.get("option_verification_call_plan") or {}
        if option:
            option_plan["proposed"] += int(option.get("proposed") or 0)
            option_plan["prefiltered"] += int(option.get("prefiltered") or 0)
            option_plan["verified"] += int(option.get("verified") or 0)
            option_plan["reserve"] += len(option.get("reserve") or [])
            option_malformed_reask += int(option.get("malformed_reasks") or 0)
        if (candidate.get("option_set_verdict") or {}).get("pass") is False:
            option_set_failures += 1
        for verdict in candidate.get("option_verdicts") or []:
            if "superlative_closure_would_establish_set" in (
                verdict.get("shadow_labels") or []
            ):
                superlative_shadow += 1
        admission = provenance.get("finding_admission") or {}
        if admission.get("served_from_bank"):
            served_from_bank += 1
        resolvability = provenance.get("referent_slot_resolvability") or {}
        slots = candidate.get("referent_slots") or []
        resolvability_stated += sum(
            1
            for slot in slots
            if isinstance(slot, dict)
            and slot.get("state") in {"stated_in_question", "stated_in_context"}
        )
        for unresolved in resolvability.get("unresolved_slots") or []:
            resolvability_unresolved += 1
            resolvability_reasons[str(unresolved.get("reason"))] += 1
        attempt = (
            provenance.get("generation_attempt")
            or candidate.get("generation_attempt")
            or {}
        )
        if attempt.get("attempt_kind") == "frozen_scope_rebind":
            rebind_outcomes[row["status"]] += 1

    routing_rows = rows(
        "SELECT reason_code, detail_json FROM rejection_ledger WHERE stage='generation_routing'"
    )
    gate_review_required = Counter()
    for row in routing_rows:
        detail = json.loads(row["detail_json"])
        if detail.get("campaign_id") == args.campaign_id and detail.get(
            "gate_review_required"
        ):
            gate_review_required[row["reason_code"]] += 1
    repeat_stops = rows(
        "SELECT reason_code, COUNT(*) AS n FROM rejection_ledger "
        "WHERE reason_code LIKE '%repeat%' GROUP BY reason_code"
    )

    bank_rows = rows(
        "SELECT admission_status, COUNT(*) AS n FROM finding_bank WHERE run_id IN (?, ?) "
        "GROUP BY admission_status",
        args.campaign_id,
        args.run_id,
    )
    no_admissible = rows(
        "SELECT COUNT(*) AS n FROM rejection_ledger WHERE reason_code='no_admissible_finding'"
    )
    prescreen = rows(
        "SELECT verdict_json FROM finding_prescreen_shadow WHERE run_id IN (?, ?)",
        args.campaign_id,
        args.run_id,
    )
    prescreen_verdicts = Counter()
    for row in prescreen:
        verdict = json.loads(row["verdict_json"])
        prescreen_verdicts[
            str(verdict.get("verdict") or verdict.get("would_block"))
        ] += 1
    findings = rows(
        "SELECT paper_family_id, status FROM findings WHERE run_id=?", args.campaign_id
    )

    # Measure 6.
    accepted_count = len(accepted)
    usd_per_item = (
        str((run_spent / accepted_count).quantize(Decimal("0.0001")))
        if accepted_count
        else None
    )
    shadow_extra_calls = sum(
        len(
            (row["candidate"].get("provenance") or {})
            .get("judge_call_plan", {})
            .get("calls_made_for_shadow")
            or []
        )
        for row in benchmark
    )

    manifest = read_json(args.stream_input_dir / "run-manifest.json")
    result = {
        "schema": "arctic-ch3-phase-e-measures-v1",
        "run_id": args.run_id,
        "campaign_id": args.campaign_id,
        "frozen_order_sha256": manifest.get("remaining_order_sha256"),
        "papers_screened": papers_screened,
        "benchmark_candidates": len(benchmark),
        "call_records_by_status": {row["status"]: row["n"] for row in call_records},
        "candidate_status_counts": dict(status_counts),
        "families_with_candidates": len(families),
        "families_with_findings": len({row["paper_family_id"] for row in findings}),
        "accepted_items": accepted_count,
        "measures": {
            "measure_1_zero_context_share": {
                "candidates_with_zero_spans": zero_context,
                "candidates": len(benchmark),
                "share": share(zero_context, len(benchmark)),
                "families_without_span": families_without_span,
                "families": len(families),
                "family_share": share(families_without_span, len(families)),
                "target": TARGETS["measure_1_zero_context_share"],
                "chapter2": CHAPTER2["measure_1_zero_context_share"],
                "slice_tested": "writer-context",
            },
            "measure_2_no_date_share": {
                "families_without_date": families_without_date,
                "families": len(families),
                "share": share(families_without_date, len(families)),
                "target": TARGETS["measure_2_no_date_share"],
                "chapter2": CHAPTER2["measure_2_no_date_share"],
                "slice_tested": "writer-context",
            },
            "measure_3_screening_error_share": {
                "errors_or_unresolved_on_last_row": screening_errors,
                "papers": papers_screened,
                "share": share(screening_errors, papers_screened),
                "last_row_states": dict(last_states),
                "last_row_decisions": dict(last_decisions),
                "initial_row_states": dict(initial_states),
                "attempt_rows_by_kind": dict(attempt_kinds),
                "target": TARGETS["measure_3_screening_error_share"],
                "chapter2": CHAPTER2["measure_3_screening_error_share"],
                "slice_tested": "eligibility",
            },
            "measure_6_usd_per_accepted_item": {
                "run_spent_usd": str(run_spent),
                "accepted_items": accepted_count,
                "usd_per_accepted_item": usd_per_item,
                "by_stage_usd": {k: str(v) for k, v in stage_cost.most_common()},
                "by_stage_calls": dict(stage_calls.most_common()),
                "by_stage_states": {k: dict(v) for k, v in stage_states.items()},
                "shadow_cohort_extra_calls": shadow_extra_calls,
                "calibration_calls": calibration_calls,
                "calibration_spent_usd": str(calibration_cost),
                "target": TARGETS["measure_6_usd_per_accepted_item"],
                "chapter2": CHAPTER2["measure_6_usd_per_accepted_item"],
                "slice_tested": "cost, judge-options, routing",
            },
        },
        "free_measures": {
            "judge_call_plan": {
                "skip_reason_counts": dict(skip_reasons),
                "shadow_cohort_size": shadow_cohort,
                "shadow_recall_failures": shadow_recall_failures,
            },
            "standalone_reask": {
                "reasks": reask_count,
                "candidates": len(benchmark),
                "rate": share(reask_count, len(benchmark)),
                "second_verdict_evidenced": reask_second_evidenced,
            },
            "option_stage": {
                **option_plan,
                "malformed_reasks": option_malformed_reask,
                "option_set_verdict_failures": option_set_failures,
                "superlative_closure_shadow_labels": superlative_shadow,
            },
            "routing": {
                "gate_review_required_by_outcome": dict(gate_review_required),
                "repeat_stop_codes": {
                    row["reason_code"]: row["n"] for row in repeat_stops
                },
                "slot_lookup_calls_by_paper": dict(slot_lookup_by_paper),
                "frozen_scope_rebind_outcomes": dict(rebind_outcomes),
            },
            "finding_bank": {
                "rows_by_admission_status": {
                    row["admission_status"]: row["n"] for row in bank_rows
                },
                "candidates_served_from_bank": served_from_bank,
                "no_admissible_finding_rows": no_admissible[0]["n"]
                if no_admissible
                else 0,
                "prescreen_shadow_verdicts": dict(prescreen_verdicts),
            },
            "referent_slot_resolvability": {
                "stated_slots": resolvability_stated,
                "unresolved_slots": resolvability_unresolved,
                "share": share(resolvability_unresolved, resolvability_stated),
                "reasons": dict(resolvability_reasons),
            },
            "two_pass_eligibility_shadow": two_pass_summary,
        },
        "cost_by_family_top": [
            {"family_id": family, "cost_usd": str(cost)}
            for family, cost in cost_by_family.most_common(10)
        ],
        "papers_with_paid_calls": len(cost_by_paper),
    }
    text = json.dumps(result, indent=1, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
