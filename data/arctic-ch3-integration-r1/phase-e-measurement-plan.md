# Phase E measurement plan for the next 200 papers

Source: chapter 2 yield audit, section 5, phase E.
The plan names the six measures, the artifact each one reads, the computation, and the target.
It runs on the artifacts of one integrated chapter 3 run.
No measure needs a paid call of its own.
The run itself starts only on a new captain instruction.

## Inputs the run must keep

- The run directory of the streaming pass: the eligibility job rows (up to three per paper), the run manifest with `eligibility_rescreen_prompt_sha256`, and the broker receipts.
- The candidates table of the run database, queried with `streaming.BENCHMARK_CANDIDATE_PREDICATE` so call records stay out.
- The rejection ledger rows of the run, including the `generation_routing` rows with `gate_review_required`.
- The `finding_bank` and `finding_prescreen_shadow` tables.
- The cost summary of the run (the same shape as `cost-summary.json` of the yield audit evidence).
- The chapter 2 baseline: `cost-summary.json` and the family bundles of `data/arctic-ch2-yield-audit-r1/evidence`.

## The six measures

| Measure | Reads | Computation | Target | Chapter 2 value |
| --- | --- | --- | --- | --- |
| 1. Candidates with zero context spans | `provenance.context_only_source` of every benchmark candidate | share of candidates whose forwarded context-only span list is empty | under 10 percent | 23 of 56 families had no span (about 41 percent of families) |
| 2. Families with no date in context | the forwarded spans and `question_context` of the first candidate per family | share of families whose forwarded spans and `question_context` carry no four-digit year, month name or season plus year (the `period` dimension marker of `gemini_eligibility._dimension_supported`) | under 30 percent | 42 of 56 families (75 percent) |
| 3. Screening errors | the last eligibility job row per paper | papers whose last row is `screening_error` or `unresolved_rescreenable`, over papers screened; report the format re-asks and re-screens separately | under 5 percent | 38 of 200 (19 percent); the replay of the 79 non-eligible papers gives 16 of 200 before the re-ask, 3 of 200 after |
| 4. Wrongly withheld papers | every paper whose last decision is `uncertain` or `excluded`, with its criterion records and `question_scope_phrases` | a sample of 40 withheld papers, labelled by two readers against the ordered geography procedure of prompt v8; share the readers judge eligible | under 12 percent | the audit judged at least 14 of the 24 re-screen pool papers eligible (58 percent of the pool) |
| 5. Over-strict share of kills | every rejected benchmark candidate with its `qa_gate_reasons`, judge records, `scope_defect` block and `judge_call_plan` | a sample of 60 rejections, labelled by two readers as correct, over-strict or under-strict against the rejection code's own rule; the over-strict share | under 15 percent | 65 of 139 (47 percent): 42 deterministic-gate and 23 standalone-judge over-strict kills |
| 6. USD per accepted item | the run cost summary and the accepted item count | total spend divided by accepted items; also per stage, and with the shadow cohort's extra calls stated separately | under USD 1.00 | USD 3.33 |

## Measures the integrated runtime records for free

These measures need no reader and are reported beside the six.

- The judge call plan: `provenance.judge_call_plan.skip_reason` counts per class (`skipped_on_unavailable_slot`, `skipped_after_free_check_failure`, `skipped_after_standalone_failure`, none), the shadow cohort size, and the shadow cohort's `shadow_gate_reasons` against its persisted list. A cohort member whose full suite would have rejected a candidate the short circuit accepted is a recall failure of the plan and is reported by item.
- The standalone re-ask rate: `provenance.standalone_verification_reask` non-null over candidates, and the share of re-asks whose second verdict was evidenced.
- The option stage: `provenance.option_verification_call_plan` (proposed, prefiltered, verified, reserve), the malformed re-ask count, `option_set_verdict` failures, and `shadow_labels` of `superlative_closure_would_establish_set` (audit 4.8 D4, shadow only).
- Routing: the `generation_routing` rows with `gate_review_required` (the no-retry guard), repeat-detector stops, `slot_lookup` calls per paper, and `frozen_scope_rebind` outcomes.
- The finding bank: rows served without an extractor call, `no_admissible_finding` rows, and the structural pre-screen's shadow verdicts against the papers that froze a finding (the screen must not be promoted while it fires more on productive papers, cost slice section 3.3).
- The referent slot resolvability shadow: `provenance.referent_slot_resolvability` (writer-context slice), reported as the share of slots marked stated whose `resolver_text` is empty or absent from the displayed fields. Phase E decides whether it becomes a gate.
- The two-pass eligibility shadow: the deferral rate and span coverage recorded on every eligibility job row, booked at USD 0.

## Procedure

1. Run the 200 papers under the integrated release with the strongest profile and the `away_production` phase (the `cost_aware` profile is refused at startup).
2. Export the run cost summary and the candidate table.
3. Compute measures 1, 2, 3 and 6 and the free measures with a script that reads only the run artifacts; store it beside the run.
4. Draw the two samples for measures 4 and 5 with a fixed seed; label with two readers; keep only unanimous rows for the share.
5. Compare every measure with its chapter 2 value in one table, and name the slice whose change the difference tests.
6. A measure over its target names the stage to revisit; it never relaxes a gate.
