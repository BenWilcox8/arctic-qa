"""Project the chapter 2 spend under the chapter 3 call plan, step by step.

Inputs: the chapter 2 family evidence bundles of the yield audit (candidates,
paid calls), the chapter 2 cost summary, and the token delta measured from
the recorded request traces (token-delta.json in this directory).
Every step holds acceptance at 6 items, as the audit's cost plan does.

Usage: python3 cost-projection.py <audit-evidence-dir> <token-delta.json> <out.json>
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter, defaultdict

EVIDENCE, TOKEN_DELTA, OUT = sys.argv[1:4]
SHADOW_RATE = 0.05
OPTION_VERIFIED_TARGET = 4

# Codes the free pre-judge pass emits (same names as the QA gate).
FREE_CODES = {
    "question_answer_leakage",
    "benchmark_text_malformed",
    "publication_relative_period",
    "answer_evidence_not_located",
    "answer_scope_not_source_bound",
    "interpretation_span_contains_answer",
    "benchmark_text_raw_source_artifact",
    "scope_qualifier_not_displayed",
    "question_context_invalid",
    "scope_qualifier_missing",
    "finding_answer_phrase_in_required_question_phrases",
    "scope_qualifier_not_source_bound",
    "finding_span_is_table_or_caption",
    "finding_span_figure_defined_referent",
}
JUDGE_STAGES = ("standalone_verification", "blinded_reconstruction", "answer_verification")

summary = json.load(open(os.path.join(EVIDENCE, "cost-summary.json")))
delta = json.load(open(TOKEN_DELTA))
total0 = summary["chapter2_spent_usd"]
accepted = summary["accepted_items"]

step1 = Counter()
step1_usd = defaultdict(float)
step3_calls = 0
step3_usd = 0.0
step4_calls_upper = 0
step4_usd_upper = 0.0
step4_calls_strict = 0
step4_usd_strict = 0.0
step6_calls = Counter()
step6_usd = defaultdict(float)
kept_judge_usd = defaultdict(float)
families = 0
skip_matrix = Counter()

for path in sorted(glob.glob(os.path.join(EVIDENCE, "families", "*.json"))):
    bundle = json.load(open(path))
    families += 1
    calls = bundle["paid_calls"]
    by_stage = defaultdict(list)
    for call in calls:
        if call.get("state") == "completed":
            by_stage[call["stage"]].append(call)
    stage_avg = {
        stage: sum(float(c["actual_cost_usd"]) for c in rows) / len(rows)
        for stage, rows in by_stage.items()
        if rows
    }
    candidates = bundle["candidates"]
    # Step 1: the judge call plan on every recorded candidate.
    for row in candidates:
        candidate = row["candidate"]
        reasons = set(candidate.get("qa_gate_reasons") or [])
        slots = candidate.get("referent_slots") or []
        unavailable = any(s.get("state") == "unavailable_in_source" for s in slots)
        free_fail = bool(reasons & FREE_CODES)
        standalone = candidate.get("standalone_verification") or {}
        standalone_fail = standalone.get("pass") is False
        if unavailable:
            skipped = JUDGE_STAGES
            cls = "slot"
        elif free_fail or standalone_fail:
            skipped = JUDGE_STAGES[1:]
            cls = "free_check" if free_fail else "standalone"
        else:
            skipped = ()
            cls = "full_suite"
        skip_matrix[cls] += 1
        was_good = row.get("status") in ("machine_accepted_unverified", "incomplete_non_mcq")
        if skipped and was_good:
            skip_matrix["WOULD_HAVE_SKIPPED_A_GOOD_ITEM"] += 1
        for stage in JUDGE_STAGES:
            cost = stage_avg.get(stage, 0.0)
            if stage in skipped:
                step1[stage] += 1
                step1_usd[stage] += cost
            else:
                kept_judge_usd[stage] += cost
    # Step 3: extraction calls after the family's first question-generation call.
    generation_times = sorted(c["submitted_at_utc"] for c in by_stage.get("question_generation", []))
    first_generation = generation_times[0] if generation_times else None
    extraction = sorted(by_stage.get("finding_answer_extraction", []), key=lambda c: c["submitted_at_utc"])
    for call in extraction:
        if first_generation and call["submitted_at_utc"] > first_generation:
            step3_calls += 1
            step3_usd += float(call["actual_cost_usd"])
    # Step 4: extraction re-asks before the first candidate (pass 2 of admission).
    before = [c for c in extraction if not first_generation or c["submitted_at_utc"] <= first_generation]
    separable = any(
        ((row["candidate"].get("provenance") or {}).get("eligible_arctic_scope") or {}).get("component")
        == "separable_arctic_component"
        for row in candidates
    )
    for call in before[1:]:
        step4_calls_upper += 1
        step4_usd_upper += float(call["actual_cost_usd"])
        if separable:
            step4_calls_strict += 1
            step4_usd_strict += float(call["actual_cost_usd"])
    # Step 6: option verification in rank order to the export need.
    option_avg = stage_avg.get("option_verification", 0.0)
    for row in candidates:
        verdicts = row["candidate"].get("option_verdicts") or []
        if not verdicts:
            continue
        for scenario in ("as_recorded", "false_flag_fixed"):
            verified = 0
            calls_made = 0
            for verdict in verdicts:
                if verified >= OPTION_VERIFIED_TARGET:
                    step6_calls[scenario] += 1
                    step6_usd[scenario] += option_avg
                    continue
                calls_made += 1
                ok = bool(verdict.get("contradiction_established")) and bool(
                    verdict.get("alternative_answer_search_passed")
                )
                if scenario == "as_recorded" and verdict.get("question_admits_option_as_correct"):
                    ok = False
                if ok:
                    verified += 1

step1_total = sum(step1_usd.values())
shadow_cost = step1_total * SHADOW_RATE
after1 = total0 - step1_total + shadow_cost
kept_fraction = {
    stage: kept_judge_usd[stage] / (kept_judge_usd[stage] + step1_usd[stage])
    if (kept_judge_usd[stage] + step1_usd[stage])
    else 1.0
    for stage in JUDGE_STAGES
}
input_saving = {
    "finding_answer_extraction": delta["finding_answer_extraction"]["estimated_input_saving_usd"],
    "question_generation": delta["question_generation"]["estimated_input_saving_usd"],
    "distractor_generation": delta["distractor_generation"]["estimated_input_saving_usd"],
    "repair": delta["repair"]["estimated_input_saving_usd"],
    "blinded_reconstruction": delta["blinded_reconstruction"]["estimated_input_saving_usd"]
    * (kept_fraction["blinded_reconstruction"] * (1 - SHADOW_RATE) + SHADOW_RATE),
    "answer_verification": delta["answer_verification"]["estimated_input_saving_usd"]
    * (kept_fraction["answer_verification"] * (1 - SHADOW_RATE) + SHADOW_RATE),
    "option_verification": delta["option_verification"]["estimated_input_saving_usd"],
}
step2_total = sum(input_saving.values())
after2 = after1 - step2_total
after3 = after2 - step3_usd
after4 = after3 - step4_usd_strict
step6_saving = step6_usd["false_flag_fixed"]
after6 = after4 - step6_saving

result = {
    "basis": {
        "chapter2_spent_usd": total0,
        "accepted_items": accepted,
        "families_in_bundles": families,
        "candidates": sum(skip_matrix[k] for k in ("slot", "free_check", "standalone", "full_suite")),
        "note": "Acceptance held at 6 items. Codes as recorded by the chapter 2 gates; the gate corrections of the sibling slices change which candidates fail free checks, not the plan.",
    },
    "step_1_judge_call_plan": {
        "candidates_by_class": dict(skip_matrix),
        "skipped_calls": dict(step1),
        "skipped_usd_by_stage": {k: round(v, 4) for k, v in step1_usd.items()},
        "skipped_usd": round(step1_total, 4),
        "shadow_cohort_rate": SHADOW_RATE,
        "shadow_cohort_usd": round(shadow_cost, 4),
        "net_saving_usd": round(step1_total - shadow_cost, 4),
        "spend_after_usd": round(after1, 4),
        "usd_per_accepted_item": round(after1 / accepted, 4),
        "audit_expected_spend_after_usd": 16.49,
    },
    "step_2_evidence_once": {
        "method": "exact character delta of every recorded chapter 2 request trace, converted with the per-stage median characters-per-token ratio from the receipts, at the input price of the stage model; judge-stage savings scaled by the calls the plan keeps",
        "input_saving_usd_by_stage": {k: round(v, 4) for k, v in input_saving.items()},
        "saving_usd": round(step2_total, 4),
        "spend_after_usd": round(after2, 4),
        "usd_per_accepted_item": round(after2 / accepted, 4),
        "audit_expected_spend_after_usd": 13.99,
        "unscaled_input_saving_usd": delta["_total_estimated_input_saving_usd"],
    },
    "step_3_finding_bank": {
        "extraction_calls_after_first_candidate": step3_calls,
        "saving_usd": round(step3_usd, 4),
        "spend_after_usd": round(after3, 4),
        "usd_per_accepted_item": round(after3 / accepted, 4),
        "audit_expected_spend_after_usd": 12.89,
        "assumption": "the first extraction of a family returns the ranked candidates the new prompt asks for, so every later extraction of that family is served from the bank",
    },
    "step_4_second_pass_guard": {
        "reask_calls_before_first_candidate_upper_bound": step4_calls_upper,
        "upper_bound_saving_usd": round(step4_usd_upper, 4),
        "reask_calls_in_separable_scope_families": step4_calls_strict,
        "booked_saving_usd": round(step4_usd_strict, 4),
        "spend_after_usd": round(after4, 4),
        "usd_per_accepted_item": round(after4 / accepted, 4),
        "audit_expected_spend_after_usd": 12.69,
        "note": "the rule fires only when every eligible finding span is excluded, which a whole-study paper never reaches; only the separable-scope families are booked",
    },
    "step_6_option_call_plan": {
        "verified_target": OPTION_VERIFIED_TARGET,
        "calls_saved_as_recorded": step6_calls["as_recorded"],
        "saving_usd_as_recorded": round(step6_usd["as_recorded"], 4),
        "calls_saved_false_flag_fixed": step6_calls["false_flag_fixed"],
        "saving_usd_false_flag_fixed": round(step6_saving, 4),
        "spend_after_usd": round(after6, 4),
        "usd_per_accepted_item": round(after6 / accepted, 4),
        "audit_expected_spend_after_usd": 12.89,
        "note": "chapter 2 proposed four options per candidate, so the stop at the export need saves nothing at that volume; it saves with the six proposals of the judge-options slice",
    },
    "total": {
        "spend_after_steps_1_to_4_and_6_usd": round(after6, 4),
        "usd_per_accepted_item_at_fixed_yield": round(after6 / accepted, 4),
        "audit_expected_range_usd": [12.0, 13.0],
    },
}
json.dump(result, open(OUT, "w"), indent=1)
print(json.dumps(result, indent=1))
