# Arctic QA chapter 3: routing and persistence slice

Task: `arctic-ch3-routing-r1`.
Branch: `fm/arctic-ch3-routing-r1`, from local `main` at `bd2fb22`.
Date: 2026-09-15.
Source: the chapter 2 yield audit at `data/arctic-ch2-yield-audit-r1/report.md`, sections 4.6 and 4.9, section 5 phase A rows "Routing" and "Persistence", and section 5 phase D row "Routing".
No paid provider call was made.
No file under `/mnt/crdata` and no chapter 2 data was changed.

## 1. Audit findings addressed

| Audit id | Defect | Change |
|---|---|---|
| 4.6 a / R2 a | A scope rejection gave the writer a bare code and an empty `unresolved_phrases`. | `scope_defect_records` computes a structured block. `SCOPE_DEFECT_INSTRUCTIONS` and the `scope_display_repair` rung act on the `display_verbatim` branch. |
| 4.6 a / R2 a | The `not_in_evidence` case cannot go to the writer, so family a2bb181f never dropped "northern Sweden". | The `frozen_scope_rebind` rung, a fourth correction component, re-grounds or removes the frozen scope value. |
| 4.6 b / R2 b | 45 of 139 candidates were rejected with no diagnostic. 29 repairs fired off them for USD 1.26 and 0 items. | The no-retry guard returns `gate_contradiction_unroutable` or `empty_diagnostic_unroutable` and flags the family for gate review. |
| 4.6 c / R1 | The slot guard read the eligibility spans, not the text the writer sees, and the `sample` pattern matched in 0 of 7 families. | `_slot_evidence_pool` reads SOURCE_DATA plus CONTEXT_ONLY_SOURCE plus the activity spans. The patterns are wider. One `slot_lookup` call runs behind the answer-leak filter. |
| 4.6 d / R3 | `source_bound_numeric_rule_missing` reached its rung in 1 of 13 cases. | The numeric repair is orthogonal. It is stripped before layer selection and rides on the attempt as `repair_numeric_rule`. |
| 4.6 d / R3 | Three scope codes sat in the contract catch-all and in no rung set, so family 3480407b ended after one attempt. | Every member of `SCOPE_FAMILY_REASONS` sits in the context layer and in one of the two scope rungs. |
| 4.6 e | The repeat detector reset on a finding switch. 72 of 139 candidates re-failed on a defect their family had seen. | `_repeat_depth` spans the whole family lineage. `option_repair` stays exempt. The replay in section 4 set the second exemption. |
| 4.6 e | 26 generation calls produced no row of any kind. | A call record row opens before every generation call and settles with the outcome. |
| 4.6 f / R6 | The weakest judge in the roster could buy a whole repair cycle. 2 of its 4 "no" verdicts were wrong. | `reconstruction_disagreement` triggers a repair only from the deterministic comparator or from a judge the role contract ranks at the Pro tier. |
| 4.9 C8 | Two families lost every completed call to an ambiguous charge. One family's attempt vanished with no terminal record. | The in-flight candidate is persisted before the charge-uncertain call. It stays `incomplete_infra` until the outcome is known. |
| 4.9 C8 | The writer was the last stage still cut off at the fixed 120 second transport timeout. | Price config revision v8 registers `call_timeout_seconds: 300` for the `question_generation` stage. |

Audit 4.10 lists the refuted proposals.
None of them is implemented.
The `not_in_evidence` writer instruction (RT-2) is absent in particular.
`SCOPE_DEFECT_INSTRUCTIONS` carries the `display_verbatim` branch alone, and one test asserts this.

## 2. The exact condition of every new or changed rule

### 2.1 The scope defect block

`validation.py::scope_defect_records(candidate)` returns one record per scope field, in sorted field order.
For each non-empty string value in `answer.scope`:

- The value binds when `_scope_phrase_in_text` finds it in the frozen `evidence_quote`, in an `evidence_components` text, or in a forwarded context-only span.
- When it does not bind, or when `answer_verification.scope_value_contradicted_by_source` is true and `contradicted_scope_field` names this field, the demand is `not_in_evidence`.
- When it binds, the field is a displayed dimension (`geography`, `period`, `population`) and `scope_phrase_is_displayed` is false, the demand is `display_verbatim`.
- Otherwise there is no defect.

Each record carries `field`, `frozen_value`, `demand`, `evidence_quote_span`, `displayed_text` and `verifier_note`.
`evidence_quote_span` is the hash-bound text the value was checked against.
`displayed_text` is the text the reader sees today.
`verifier_note` is the verifier's own string, for the field it named.
The block is persisted on the candidate as `scope_defect`.
The revision payload forwards only the `display_verbatim` records.

### 2.2 The two scope rungs

`_repair_kind` routes a scope-family trigger by the demand, never by the code alone:

- Any `display_verbatim` demand at repeat depth 0 gives `scope_display_repair`. The writer places the frozen value verbatim and changes nothing else.
- Only `not_in_evidence` demands at repeat depth 0 gives `frozen_scope_rebind`. At depth 1 or more the rung returns None, because a question rewrite may not change the frozen scope.
- An empty block falls through to the ordinary ladder, exactly as before.

`_rebound_scope_answer` asks the correction role for a replacement `scope` object.
It then refuses the replacement unless all of these tests pass:

1. The key set is a subset of the parent scope's keys. A rebind never invents a dimension.
2. Every kept value occurs verbatim in the frozen evidence quote or in a forwarded interpretation text.
3. At least one defective field changed. An unchanged answer is `frozen_scope_rebind_unchanged`.
4. The rebound answer passes `scope_is_evidence_bound`, `finding_admission_reason` and `_require_arctic_scope_custody`.

The rebound record carries its own derived `rebound_finding_id` in provenance.
Freeze-time admission runs again on it.

### 2.3 The no-retry guard

`_unroutable_outcome(reason_codes, evidence)` returns a code only when all four conditions hold:

1. The primary failure layer is `context`, `evidence` or `contract`. An option repair, a finding-layer code and a leakage code are never stopped.
2. Every primary reason code is in `DIAGNOSTIC_BEARING_REASONS`: the seven answer-scope codes and the twelve source-blind referent codes.
3. `unresolved_phrases`, `missing_detail_types`, `scope_defect`, `question_context_missing_detail` and `residual_error` are all empty.
4. The outcome is `gate_contradiction_unroutable` when every judge record that exists reports clean, and `empty_diagnostic_unroutable` when a record names a defect.

A self-describing code such as `question_context_missing` names its own fix and is never stopped.
`reconstruction_scope_not_source_bound` and `question_qualifier_not_evidence_bound` are out of the set too.
Their diagnostic lives on a record this router does not compute, and the replay showed that both reach a context widening that accepted an item.
An absent judge record is not evidence of a contradiction, as audit 4.6 b requires.

A stop writes a `generation_routing` rejection row with `gate_review_required: true`.
That row creates no path, and the stage is new, so `_generation_paths` never reads it.
A stop does not change the paper's disposition when the family already holds an `incomplete_non_mcq` candidate.
That candidate is an accepted question whose distractors failed, which is a product output, so it keeps its own disposition.
Otherwise the disposition is `generation_rejected` and the outcome code is appended to the reason codes.

### 2.4 The orthogonal numeric repair

`source_bound_numeric_rule_missing` is removed from the reason codes before layer selection when any other code remains.
`repair_numeric_rule` is set on the returned attempt instead.
`generate_candidate` then runs `_repaired_numeric_rule_answer` inside the same attempt as the question repair.
The code alone still routes to the `answer_rule_repair` rung, once.

### 2.5 The lineage-wide repeat detector

`_repeat_depth(paths, family)` counts every attempt in the family lineage whose collapsed trigger family matches.
It ignores an attempt on an exempt rung.
Two exemptions exist:

- `option_repair`, which reuses the parent's verified question and went 5 for 6 in chapter 2. It is not counted.
- `context_widened_revision`, which the replay showed the counter would otherwise suppress. At depth 2 or more the rung still runs once per finding, never twice on the same finding.

### 2.6 The slot pool and the `slot_lookup` call

`_slot_evidence_pool` builds the pool from the failed candidate's `evidence_quote`, its `evidence_components`, its forwarded context-only spans and the eligibility activity spans.
The `period` pattern now accepts a season, a field season, a cruise, an expedition and a campaign.
The `sample` pattern accepts `n = N`, a digit or an English number word with any plural noun of three letters or more, and a named cohort phrase.
A short stop list keeps words such as "was" and "less" from counting as a plural noun.

When the pool still cannot meet a demand, `_progress_generation` makes at most one `slot_lookup` call per paper.
The returned sentence is refused unless it is verbatim in the pool.
It is refused again when `interpretation_spans_contain_answer` finds the frozen answer in it.
A surviving sentence rides on the question repair as `slot_lookup`.
A null result routes `slot_evidence_unavailable`, as before.

### 2.7 The agreement trigger rule

`_agreement_verdict_is_authoritative` returns true for the deterministic comparator and for a judge whose model the role contract ranks at 4 or more.
It returns false only when the roster ranks the judge below that tier.
A record the roster cannot rank is left alone, so the rule removes a known-weak verdict and invents no new reason to stop.

### 2.8 Persistence

`_open_generation_call_record` writes a candidate row with status `incomplete_infra` before every generation call.
Its item id is `stable_id("generation-call", contract, run_id, family_id, source_version_id, attempt_id)`.
The same attempt on the same frozen source text therefore always resolves to the same row.
The row carries the request identity.

The row settles to `generation_settled` when a candidate is persisted.
It settles to `generation_incomplete` on a typed rejection or a budget stop.
It stays `incomplete_infra` when the process or the provider fails, which is the C8 diagnosis state.
The operational-unresolved branch counts the family's open records.

The three call-record statuses never enter path reconstruction, acceptance, or the run counts.
One predicate, `BENCHMARK_CANDIDATE_PREDICATE`, keeps them out of every benchmark-item query.

## 3. The rigor safeguard for each change

Routing decides which repair runs.
It never decides whether an item is accepted.
Every repaired candidate re-runs the whole gate sequence and consumes one of the six bounded paths.
`tests/test_ch3_routing.py` asserts that the two new terminal codes belong to no rung set.

| Change | Why it cannot admit a paper-dependent or unsupported item |
|---|---|
| The scope defect block | It echoes the frozen finding's own scope strings and the hash-bound text they were checked against. It states no new claim and adds no context. |
| The `scope_display_repair` rung | It asks the writer to display a value the evidence already states. A displayed value is more self-contained, not less. The gate that emitted the code runs again. |
| The `frozen_scope_rebind` rung | It only removes an unsupported qualifier or replaces it with one the frozen span states verbatim. It cannot add a dimension. It re-runs freeze-time admission and the Arctic scope custody check. Family a5bcbcf9's hallucinated period "February 2009" is the class it removes, so rigor rises. |
| The no-retry guard | It only stops spending. It has no branch that accepts. |
| The orthogonal numeric repair | It regenerates answer metadata the reader never sees and re-runs `numeric_rule_is_source_bound` against the unchanged frozen span. It fails closed. |
| The lineage repeat detector | It only stops spending. |
| The slot pool and `slot_lookup` | The value must be a verbatim sentence from hash-bound text that passed the answer-leak filter. The `context_gap` and `unavailable_in_source` escape is unchanged, so the writer still cannot invent filler. A question that still needs the paper still fails the unchanged source-blind gate. |
| The agreement trigger rule | It replaces the roster's weakest judge on a single-verdict gate with a deterministic check or a stronger judge. It makes a repair harder to trigger, not easier. |
| Persistence | A persisted candidate still runs every gate. A call record is not a benchmark item and cannot be exported or accepted. |
| The writer timeout | It changes transport only. No prompt, no model and no threshold moves. |

Everything in the "Keep" column of audit section 5 for this stage is preserved.
That is priority-layer routing, `option_repair` reuse of the verified question, revisions that reuse the frozen finding, the six-path bound, `revision_unchanged_payload`, `ATTEMPT_HISTORY`, the `context_gap` escape, the per-stage Pro timeouts of 300 s, and the invariant that routing never accepts.
The `scope_defect` block is deterministic structured data, not judge prose, so it does not breach the `ATTEMPT_HISTORY` rule.

## 4. The replay against the 139 recorded chapter 2 candidates

Script: `data/arctic-ch3-routing-r1/replay_chapter2_routing.py`.
Report: `data/arctic-ch3-routing-r1/replay-chapter2-routing.json`.
Evidence: `data/arctic-ch2-yield-audit-r1/evidence/families/*.json`, read only.

The replay rebuilds each family lineage from the recorded candidates.
It then asks the new router what it would have done at every recorded repair.
It covers 56 families, 139 candidates and 80 recorded repairs.

| Measurement | Value |
|---|---|
| Repairs the new router would have stopped | 1 of 80 |
| Accepted items suppressed | 0 |
| Accepted items suppressed by the repeat detector | 0 |
| Rejected candidates examined | 133 |
| Candidates that now carry a structured scope defect | 62 (34 `display_verbatim`, 32 `not_in_evidence`) |
| Candidates where the numeric repair now rides along | 12 |
| `reconstruction_disagreement` triggers now barred | 4 |
| Repairs routed to a different rung | 24 |

The exact list of attempts the new routing would have stopped is one attempt:

| Family | Item | Recorded rung | Trigger | Parent codes | Cause |
|---|---|---|---|---|---|
| `family-a2bb181fa382d479c5c5` | `aqa-b8d0cf201a521d1aa2c4` | `context_widened_revision` | `scope_value_not_source_supported` | `answer_scope_not_source_bound`, `scope_qualifier_not_displayed`, `scope_value_not_source_supported` | the lineage repeat detector |

This is the second widening on a finding the family had already widened.
Family a2bb181f is the audit's own example of pure waste: six attempts, USD 0.473, zero accepted items, all on one unbindable scope field.
The first widening on every finding still runs.

Audit 4.6 e requires the rung to be exempted if the counter suppresses one.
The exemption is bounded to one widening per finding, which is why one repeat widening still stops.
Before that exemption the counter suppressed two widenings.
Before the guard was narrowed it also suppressed the run's one accepted `context_widened_revision` item, in family e02e286a.
The replay caught that, and `DIAGNOSTIC_BEARING_REASONS` was narrowed to the codes whose diagnostic this router actually computes.
Nothing may widen that set without a new replay.

The guard stops nothing on the recorded data, and that is the intended result.
29 rejected candidates carry only diagnostic-bearing codes, which is the set the guard can reach.
All 29 now carry a diagnostic, because the scope defect block computes one from the frozen answer record.
The block therefore removes the cause of a blind repair instead of stopping the repair.
The guard stays as the safety net for a rejection that carries no diagnostic of any kind.
`tests/test_ch3_routing.py` proves both of its branches on constructed rejections.

## 5. Expected effect on acceptance and on cost per accepted item

These are projections from the replay counts and from the audit's own per-call prices.
No paid call was made, so nothing here is measured from new receipts.

Acceptance:

- 62 of 133 rejected candidates now hand the repair a named field, a frozen string and the span it failed on, instead of a bare code. Audit 4.6 a expects this to convert the largest zero-yield families into an accepted item or one correctly aimed attempt.
- 12 candidates keep their question repair and get the numeric metadata fix in the same attempt, instead of a rewrite the code cannot repair. Families 7edb49fb and b1a8778e are the recovered class.
- Family 3480407b's class of kill is gone, because every scope code now reaches a rung.
- The `frozen_scope_rebind` rung deletes an unsupported qualifier. It can also end an attempt that no rung could have saved, so its yield is not a promise.
- The slot pool and the `slot_lookup` call stop abandoning a finding whose slot sits in the text the writer already had. Audit 4.6 c expects zero extra items until the standalone corrections land.

Gemini cost, at chapter 2's volume of 139 candidates and 6 accepted items:

| Change | Effect | Basis |
|---|---|---|
| Aimed scope repairs in place of blind ones | USD 0 saved directly. The audit books USD 1.26 of blind repairs, and this change spends that money on a repair that names its defect instead of stopping it. The saving arrives as acceptance, not as a lower call count. | 62 of 133 rejected candidates now carry a scope defect |
| Orthogonal numeric repair | USD 0, a swap, as audit 4.10 states | 12 candidates change from a USD 0.0436 rewrite to a USD 0.0043 repair, and the metadata call now runs beside every earned repair |
| Lineage repeat detector | about USD 0.04, about USD 0.007 per accepted item | 1 repair at USD 0.0436 |
| Agreement trigger rule | about USD 0.13, about USD 0.02 per accepted item | 4 barred triggers, of which 3 bought a cycle in chapter 2 |
| Slot pool and `slot_lookup` | audit 4.6 c books USD 1.2 to 1.6, about USD 0.20 to 0.27 per accepted item. The extractor half is booked once in audit 4.5. | 15 slot repairs. Two extractor calls are replaced by one USD 0.004 call. |
| Call records and persistence | about USD 0.07 protected, no saving | family aeed4bfe |
| Writer timeout | no direct saving. It stops a completed call becoming an unknown charge. | audit 4.9 C8 |

The direct saving this slice books is small: about USD 0.17 in chapter 2 terms, or about USD 0.03 of the USD 3.33 per accepted item.
The audit projected more from the guard.
The reason it is less is that the scope defect block removes the cause of a blind repair rather than stopping the repair.
That moves the money from "stopped" to "aimed", which is the better outcome for acceptance.
The large cost movements in audit section 8 sit in the cost slice and in the finding bank, not here.
The measurable claim for this slice is narrower: no repair is now fired with nothing to act on, no rung is now unreachable, and no weak judge can buy a cycle.

## 6. Test results

Command: `nix develop -c bash -c 'PYTHONPATH=src pytest tests/<files> -q'`, run in bounded foreground parts.

| Part | Result |
|---|---|
| `tests/` without `test_streaming.py`, `test_cli_integration.py`, `test_model_broker.py` | 629 passed |
| `tests/test_streaming.py` | 56 passed |
| `tests/test_cli_integration.py` and `tests/test_model_broker.py` | 178 passed |
| `ruff check src tests` | all checks passed |
| `ruff format --check src tests` | all files already formatted |

New tests: `tests/test_ch3_routing.py`, 54 tests, one per rung, guard and persistence path.
Each one names the audit finding and the chapter 2 family it closes.
Two end-to-end persistence tests were added to `tests/test_bounded_fallback.py`.
The routing tests in `tests/test_streaming.py` and `tests/test_bounded_fallback.py` stay green.

Existing tests changed only where they pinned behaviour the audit asked to change:

| Test | Change |
|---|---|
| `test_progress_generation_continues_with_a_fresh_path_after_rejection` | The candidate count is scoped to benchmark items, and the settled call records are asserted. |
| `test_r14_predecessor_attempts_consume_successor_retry_slots` | The historical byte comparison is scoped to benchmark items. |
| `test_stage_call_timeout_comes_from_the_price_config`, `test_pro_judge_stages_carry_a_pinned_longer_call_timeout` | The writer stage is now 300 s. A stage that still keeps the 120 s default is asserted in its place. |
| `test_price_config_v7_pins_the_judge_model_and_its_price`, `test_config_requires_low_thinking_for_bounded_structured_output`, `test_stable_identifiers_stay_bound_to_the_new_routing_contract`, `test_generation_attempt_contract_rejects_unbounded_paths` | The two contract versions this slice owns. |
| `test_staged_batch_pipeline_uses_real_prompts_and_exports_accepted_output` | One candidate query is scoped to benchmark items. |

## 7. Contract versions

| Version | Before | After | Owner |
|---|---|---|---|
| `ROUTING_CONTRACT_VERSION`, aliased as `GENERATION_ATTEMPT_CONTRACT_VERSION` | `bounded-failure-routing-v4` | `bounded-failure-routing-v5` | this slice |
| `SCOPE_DEFECT_CONTRACT_VERSION` | none | `scope-defect-v1` | this slice, new |
| `GENERATION_CALL_RECORD_CONTRACT_VERSION` | none | `generation-call-record-v1` | this slice, new |
| `SLOT_LOOKUP_CONTRACT_VERSION` | none | `routing-slot-lookup-v1` | this slice, new |
| Price config `config_id` | `arctic-gemini-eligibility-r1-config-v7` | `arctic-gemini-eligibility-r1-config-v8` | this slice bumped it |
| `config/roles.v1.json` `schema_version` | `1.2.0` | `1.3.0` | this slice, for the new `slot_lookup` role |

Price config coordination.
This slice bumped the price config revision to v8, for the writer stage timeout that audit 4.9 C8 requires.
The cost slice must not bump it again.
It should add its payload and role changes to the same v8 revision.
If it needs its own meaning change, it takes v9 and folds the `question_generation` stage entry forward.
The v8 entry registers `call_timeout_seconds` and nothing else, so the stage keeps the verified writer model and its prices.
`_validate_writer_timeout_config` pins that shape.

`GENERATION_ATTEMPT_CONTRACT_VERSION` is not separate from `ROUTING_CONTRACT_VERSION`, because `generation.py` aliases it.
Two fields were added to the attempt record: `repair_numeric_rule`, a boolean, and `slot_lookup`, a record or null.
That is why the version moved.
The attempt id is unchanged in its inputs, so lineage identity holds.
No sibling's contract version was bumped.

## 8. New reason codes

Routing outcomes this slice adds.
Each one is registered in `streaming.py` with a one-line comment and a routing layer entry:

| Code | Layer | Meaning |
|---|---|---|
| `gate_contradiction_unroutable` | contract, terminal | Every judge record present reports clean while a deterministic code killed the candidate. The family is flagged for gate review. |
| `empty_diagnostic_unroutable` | contract, terminal | A judge names a defect and no diagnostic names the words it objects to. |

Rejection codes the new rungs can raise, from `generation.py`:

| Code | Raised by |
|---|---|
| `frozen_scope_rebind_not_applicable` | no unsupported frozen scope value exists |
| `frozen_scope_rebind_invalid` | the rebind invented a dimension, returned non-text, or returned a value the evidence does not state |
| `frozen_scope_rebind_unchanged` | the rebind returned the same unsupported values |

Neither terminal outcome is in any rung set, and each new rejection code fails the attempt closed.

Sibling codes registered.
`arctic-ch3-eligibility-r1`, landed at `a362bea`, announced `eligible_arctic_scope_dimension_unsupported` and `eligible_arctic_scope_phrase_not_specific`.
Both now have an explicit routing layer entry in `_failure_layer` with a one-line comment.
`tests/test_ch3_routing.py::test_a_registered_eligibility_code_claims_no_candidate_rung` asserts that no candidate-level rung set holds them.
That slice also defines `ELIGIBILITY_CONTRACT_REASONS` in `streaming.py`.
This slice did not duplicate it, so integration keeps one definition.
The other four siblings were still at `bd2fb22` when this slice finished, so no other code was available to register.
Integration reconciles the final set.

## 9. Files touched outside the owned set

Owned: `streaming.py` routing and persistence, `ROUTING_CONTRACT_VERSION`, and their tests.

| File | Change | Why |
|---|---|---|
| `src/arctic_qa/validation.py` | New `SCOPE_DEFECT_CONTRACT_VERSION`, `SCOPE_DEFECT_DEMANDS`, `scope_defect_records` and `_scope_defect_evidence_texts`. `ROUTING_CONTRACT_VERSION` bumped. | The contract version lives here, and the scope defect must be computed by the same containment rule that emits the code. The additions are new functions. Nothing existing was changed except the version literal. |
| `src/arctic_qa/generation.py` | New attempt kinds `scope_display_repair` and `frozen_scope_rebind`. Two new attempt contract fields. `SCOPE_DEFECT_INSTRUCTIONS` and `SLOT_EVIDENCE_INSTRUCTIONS`. `scope_defect` and `slot_evidence` in the revision payload. `_rebound_scope_answer`. `slot_lookup_quote` and its role schema. `scope` added to the correction components. The numeric repair driven by `repair_numeric_rule`. `scope_defect` and `rebound_finding_id` on the candidate record. | The rungs must execute somewhere. The writer-context slice owns the writer prompt, so the two new instruction blocks are appended only for a repair that carries the matching payload. A primary attempt's prompt is unchanged. |
| `src/arctic_qa/gemini_eligibility.py` | Config revision v8 approved. New `WRITER_TIMEOUT_STAGE`, `WRITER_CALL_TIMEOUT_SECONDS` and `_validate_writer_timeout_config`. | The writer timeout is a price-config fact. Coordinate with the cost slice as recorded in section 7. |
| `src/arctic_qa/model_roles.py` | `slot_lookup` added to `AUTHOR_ROLES`. | The lookup is a writer-side retrieval and judges nothing, so it stays on the author side of the role split. |
| `src/arctic_qa/broker_provider.py` | `ROLE_STAGES["slot_lookup"] = "repair"`. | The lookup runs in the cheap flash repair stage. |
| `config/gemini-eligibility-v1.json` | `config_id` v8. A `question_generation` stage entry with `call_timeout_seconds: 300`. | Audit 4.9 C8. |
| `config/roles.v1.json` | `slot_lookup` added to all three profiles on the writer-tier model. `schema_version` 1.3.0. | Required by the new role. |
| `AGENTS.md` | One line: a `candidates` row can be a call record, and a benchmark-item query needs `BENCHMARK_CANDIDATE_PREDICATE`. | A sharp edge every future session that queries candidates needs. |
| `tests/test_ambiguous_continuation.py`, `tests/test_audit_core_fixes.py`, `tests/test_chapter2_integration.py`, `tests/test_gemini_batch.py`, `tests/test_gemini_eligibility.py`, `tests/test_question_context.py`, `tests/test_streaming.py` | The assertions listed in section 6. | Each pins a behaviour that one of the two version bumps or the call records changed. |

One function was removed: `streaming.py::_source_slot_evidence`.
`_slot_evidence_pool` replaces it.
Keeping it would leave a helper that reads the activity spans alone, which audit 4.6 c forbids.

## 10. Deferred items

| Item | Owner | Note |
|---|---|---|
| The provider-side receipt lookup on a `request_key` retry | the broker owner, a later slice | Audit 4.9 C8 asks for it. This slice delivers the streaming half: an idempotent call record keyed on the attempt and the frozen source text, and a test that pins `broker_request_key` as idempotent for one exact request. The broker's ambiguous-charge handling was not weakened, and it still keeps the full reservation. |
| A second frozen findings row for a rebound answer | integration | The audit asks the rebind to produce a new finding id and to re-run freeze-time admission. It does both: the derived id is recorded in provenance and admission runs again. A second row is not inserted, because the `findings` table is unique per run, family and policy version, and a second row would break bounded path reconstruction. `answer_rule_repair` already sets this precedent. |
| The bounded comparator widening for `reconstruction_disagreement` | `arctic-ch3-gates-r1` | This slice owns the routing rule only. The comparator must reach unit-normalised and value-plus-head-noun equality, so that "24 species" against "24" never reaches a model at all. |
| A Pro escalation for the residue the widened comparator cannot settle | a later slice | Audit 4.6 f asks for the residue to be measured first. |
| A second `slot_lookup` after a resumed run | integration | The per-paper bound is an in-process counter. A crash in the middle of one family can allow one more lookup on resume, at USD 0.004. |
| The scope defect block's dependence on correct gate demands | `arctic-ch3-gates-r1` | Audit 4.6 a says to ship the block with the corrected scope rules, so the demand it reports is right. The block reads the same containment rule the gate uses, so it follows that slice automatically. |
| Reason codes from the four siblings that had not landed | `arctic-ch3-integration-r1` | See section 8. |
