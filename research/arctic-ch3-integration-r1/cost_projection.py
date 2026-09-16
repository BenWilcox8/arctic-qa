"""Project the chapter 2 spend under the integrated chapter 3 call plan.

Basis: the chapter 2 receipts summarised in the yield audit evidence
(``cost-summary.json``), the cost slice's per-step projection from the same
receipts (``data/arctic-ch3-cost-r1/cost-projection.json``), the integrated
gate replay, and the per-call prices of audit section 8.1. Steps 1 to 4 and 6
are the cost slice's measured figures. The later rows book what the other
five slices add or remove, each once, at the chapter 2 volume.

Usage: python cost_projection.py <audit-evidence-dir> <cost-slice-projection.json>
                                 <gate-replay.json> <out.json>
"""

from __future__ import annotations

import glob
import json
import os
import sys

EVIDENCE, COST_SLICE, GATE_REPLAY, OUT = sys.argv[1:5]
summary = json.load(open(os.path.join(EVIDENCE, "cost-summary.json")))
cost_slice = json.load(open(COST_SLICE))
gate_replay = json.load(open(GATE_REPLAY))

total0 = summary["chapter2_spent_usd"]
accepted0 = summary["accepted_items"]
calls = summary["by_stage_calls"]
usd = summary["by_stage_usd"]
per_call = {stage: usd[stage] / calls[stage] for stage in calls}
ATTEMPT_USD = 0.0436  # one question attempt: writer plus three Pro judges (audit 8.1)

# Chapter 2 verdicts a v4 judge would have had to evidence: a failing verdict
# with an undefined_* code and no phrase, or multiple_interpretations and no
# second reading. Upper bound on the re-ask count under the v4 prompt.
unevidenced = 0
option_stage_candidates = 0
for path in sorted(glob.glob(os.path.join(EVIDENCE, "families", "*.json"))):
    bundle = json.load(open(path))
    for row in bundle["candidates"]:
        candidate = row["candidate"]
        verdict = candidate.get("standalone_verification") or {}
        reasons = verdict.get("reasons") or []
        phrases = [p for p in verdict.get("unresolved_phrases") or [] if str(p).strip()]
        if verdict.get("pass") is False and (
            (any(str(r).startswith("undefined_") for r in reasons) and not phrases)
            or "multiple_interpretations" in reasons
        ):
            unevidenced += 1
        if candidate.get("option_verdicts"):
            option_stage_candidates += 1

steps = []
spend = total0


def book(step, change, delta, basis, safeguard, *, overlap=None):
    global spend
    spend = spend + delta
    steps.append(
        {
            "step": step,
            "change": change,
            "delta_usd": round(delta, 4),
            "spend_after_usd": round(spend, 4),
            "usd_per_accepted_item_at_6": round(spend / accepted0, 4),
            "basis": basis,
            "rigor_safeguard": safeguard,
            **({"overlap": overlap} if overlap else {}),
        }
    )


# Steps 1 to 4 and 6: the cost slice's measured figures (fixed yield of 6).
book(
    "0",
    "Chapter 2 as run",
    0.0,
    f"{summary['chapter2_calls']} calls, {accepted0} accepted items",
    "-",
)
s1 = cost_slice["step_1_judge_call_plan"]
book(
    "1",
    "Judge call plan: free checks first, standalone kept, no reconstruction or "
    "verification after a free-check or standalone failure, no Pro call on an "
    "unavailable slot, 5 percent shadow cohort",
    -s1["net_saving_usd"],
    f"{s1['skipped_calls']} skipped on the 139 recorded candidates; shadow cohort "
    f"USD {s1['shadow_cohort_usd']}",
    "Same codes, earlier; routing input preserved; the integration test proves "
    "the free checks equal the corrected gates on all 139 candidates.",
)
s2 = cost_slice["step_2_evidence_once"]
book(
    "2",
    "Evidence sent once, static instructions in systemInstruction, real context budget",
    -s2["saving_usd"],
    "exact character delta of every recorded request trace, per-stage token ratio",
    "Same bytes, same span ids, same hashes.",
)
s3 = cost_slice["step_3_finding_bank"]
book(
    "3",
    "Ranked finding bank serves the alternative-finding rung",
    -s3["saving_usd"],
    f"{s3['extraction_calls_after_first_candidate']} extraction calls after a family's first candidate",
    "A banked candidate passes the same admission contract as a fresh one.",
)
s4 = cost_slice["step_4_second_pass_guard"]
book(
    "4",
    "Second admission pass only with an unexcluded span; structural pre-screen in shadow",
    -s4["booked_saving_usd"],
    f"{s4['reask_calls_in_separable_scope_families']} re-asks in separable-scope families",
    "Only rejects, on evidence the freeze-time checks already use.",
)
# Step 5: eligibility recovery adds calls (audit 8.3 step 5).
rescreens = 24
reasks = 13
elig_delta = (rescreens + reasks) * per_call["eligibility"]
book(
    "5",
    "Format re-ask and geography re-screen in the streaming path; two-pass screen in shadow at USD 0",
    +elig_delta,
    f"{rescreens} re-screens and {reasks} bounded re-asks at USD {per_call['eligibility']:.4f} each "
    f"(replay of the 79 non-eligible papers); the audit booked the re-screens alone (USD 0.53)",
    "Statuses frozen on re-ask; a failed geography never returns; no replayed paper "
    "becomes eligible without a new judgment.",
)
# Step 6: option stage.
false_flag_rounds_usd = 0.34  # audit 8.2: 5 second distractor rounds, 24 calls
set_call_usd = 0.008  # one short source-blind Pro call, no source text
option_delta = -false_flag_rounds_usd + option_stage_candidates * set_call_usd
book(
    "6",
    "Option stage: admitting_interpretation before the boolean, six proposals, "
    "rank-order stop at four verified, one whole-set call, fail-fast prefilter",
    option_delta,
    f"USD {false_flag_rounds_usd} of inverted-boolean repair rounds removed (audit 8.2); "
    f"{option_stage_candidates} candidates reach the option stage and buy one whole-set "
    f"call at about USD {set_call_usd}; the rank-order stop saves nothing at four "
    "proposals and two Pro calls per clean candidate at six",
    "Floor of three, per-option hash binding, Pro verdict on every verified option, "
    "the whole-set verdict can only reject.",
)
# Step 7: routing.
repeat_guard_usd = 1 * ATTEMPT_USD
agreement_rule_usd = 0.13
slot_lookups = 15
slot_lookup_usd = slot_lookups * per_call["repair"]
routing_delta = -repeat_guard_usd - agreement_rule_usd + slot_lookup_usd
book(
    "7",
    "Lineage-wide repeat detector, no-retry guard, weak-judge agreement rule, "
    "scope defect block, one slot_lookup per paper",
    routing_delta,
    f"1 repeat widening stopped (USD {repeat_guard_usd:.4f}); 4 barred agreement "
    f"triggers (USD {agreement_rule_usd}); {slot_lookups} slot lookups at USD "
    f"{per_call['repair']:.4f} replace extractor re-asks already booked in step 3",
    "Routing never accepts; every repaired candidate re-runs the whole gate sequence.",
)
# Step 8: the standalone judge contract v4.
reask_usd = unevidenced * per_call["standalone_verification"]
repeat_loop_usd = 0.65  # audit 8.2: byte-identical repeat attempts
standalone_delta = +reask_usd - repeat_loop_usd
book(
    "8",
    "Standalone contract v4: evidence-bound codes, one re-ask per unevidenced verdict, "
    "code-only verdict fingerprint",
    standalone_delta,
    f"upper bound {unevidenced} re-asks (chapter 2 verdicts a v4 judge would have to "
    f"evidence) at USD {per_call['standalone_verification']:.4f}; USD {repeat_loop_usd} of "
    "byte-identical repeat attempts ended by the fingerprint (audit 8.2)",
    "The re-ask replaces a verdict that carried no evidence; it never passes an item.",
    overlap="the repeat-attempt saving overlaps audit step 7 in part",
)
# Step 9: gates and writer context.
pro_agreement_usd = 20 * 0.0002
retry_waste_usd = 0.96 + 0.34 + 0.48
unsourced_findings = 9
unsourced_usd = unsourced_findings * (
    per_call["question_generation"] + per_call["standalone_verification"]
)
gates_delta = +pro_agreement_usd - retry_waste_usd - unsourced_usd
book(
    "9",
    "Corrected deterministic gates, display rules, reconstruction record, Pro agreement "
    "judge; scope_evidence at freeze",
    gates_delta,
    f"Pro fallback judge on about 20 residual agreement cases (USD {pro_agreement_usd:.3f}); "
    f"retry waste on defects no rewrite could reach in families 3185c3a7, 46dc1707, "
    f"efcc5fc4, 7edb49fb and e3d2c979 (USD {retry_waste_usd:.2f}); {unsourced_findings} "
    f"scope values found nowhere die before the writer call (USD {unsourced_usd:.3f})",
    "Every gate correction is replayed on the 139 candidates: 16 freed, 0 regressed, "
    "every accepted item still passes.",
    overlap="the retry-waste saving overlaps steps 1 and 7 in part",
)

final = spend
freed = gate_replay["freed_count"]
freed_families = len(gate_replay["freed_families"])
# Acceptance scenarios: each extra accepted item adds one distractor round
# (writer plus four verified options plus the set call) at the option stage.
per_item_option_usd = (
    per_call["distractor_generation"]
    + 4 * per_call["option_verification"]
    + set_call_usd
)
scenarios = []
for items, label in (
    (6, "chapter 2 yield, fixed"),
    (12, "audit conservative low"),
    (15, "audit central"),
    (18, "audit conservative high"),
    (6 + freed_families, "chapter 2 plus one item per freed family"),
    (6 + freed, "chapter 2 plus every freed candidate"),
):
    extra = max(0, items - accepted0) * per_item_option_usd
    scenarios.append(
        {
            "accepted_items": items,
            "label": label,
            "spend_usd": round(final + extra, 4),
            "usd_per_accepted_item": round((final + extra) / items, 4),
        }
    )

result = {
    "schema": "arctic-ch3-integrated-cost-projection-v1",
    "basis": {
        "chapter2_spent_usd": total0,
        "chapter2_calls": summary["chapter2_calls"],
        "accepted_items": accepted0,
        "usd_per_call_by_stage": {k: round(v, 5) for k, v in per_call.items()},
        "note": (
            "Steps 1 to 4 and 6 are the cost slice's receipt-measured figures at a "
            "fixed yield of 6. Steps 5 and 7 to 9 book the other five slices from the "
            "audit's own per-call prices and replay counts. Overlaps are named, not "
            "netted, so the total is an upper bound on the saving by about USD 1."
        ),
    },
    "steps": steps,
    "total": {
        "spend_after_all_steps_usd": round(final, 4),
        "usd_per_accepted_item_at_fixed_yield": round(final / accepted0, 4),
        "audit_expected_after_step_7_usd": 11.89,
        "cost_slice_after_steps_1_to_4_and_6_usd": cost_slice["total"][
            "spend_after_steps_1_to_4_and_6_usd"
        ],
    },
    "yield_scenarios": scenarios,
    "target": "under USD 1.00 per accepted item at the same rigor (audit section 8.3)",
}
json.dump(result, open(OUT, "w"), indent=1)
print(json.dumps({"total": result["total"], "yield_scenarios": scenarios}, indent=1))
