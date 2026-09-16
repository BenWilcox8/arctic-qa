# Chapter 3 slice: standalone judge, satisfiability guard, option stage

Task `arctic-ch3-judge-options-r1`.
Branch `fm/arctic-ch3-judge-options-r1`, created from local `main` at commit bd2fb22.
Source of the work: `data/arctic-ch2-yield-audit-r1/report.md`, sections 4.2 and 4.8, section 5 phase C rows "Standalone prompt", "Satisfiability guard" and "Option stage", with the stage files `standalone_gate.md`, `distractors.md` and `routing.md`.
Nothing in section 4.10 (refuted proposals) is implemented.
No paid provider call was made.
No file under `/mnt/crdata` and no chapter 2 data was touched.

## 1. Files touched outside the owned set

Integration must reconcile these files with the sibling crews.

| File | Why this slice touched it | Size |
| --- | --- | --- |
| `src/arctic_qa/validation.py` | The option verdict key sets, `validate_distractor`, the new option helpers (`option_free_rejection_reason`, `option_verdict_rejection_reason`, `option_verdict_is_malformed`, `closed_set_closure_reason`, `definite_superlative_closure`, `option_set_hash`, `option_set_verdict_reasons`), `standalone_verification_resolves` for contract v4, the `standalone_det_` namespace helpers (left by the first worker), the 2.7.0 pin and the 2.8.0 row of `CANDIDATE_CONTRACTS`, and every `{"2.4.0", ..., "2.7.0"}` schema set now includes `"2.8.0"`. | about 690 lines |
| `src/arctic_qa/streaming.py` | Routing sets only, plus the minimal guard hook: a `forwarded_slot_evidence` keyword on `_next_generation_attempt`, two pure helpers (`_failed_answer_scope`, `_forwarded_slot_evidence`), one `if` that treats an unsatisfiable demand like the existing unmet-slot path. No routing logic changed. | about 100 lines |
| `src/arctic_qa/generation.py`, `CANDIDATE_SCHEMA_VERSION` | Bumped `2.7.0` to `2.8.0`. The writer-context slice owns the bump; both slices must land the same literal. Without it, a generated candidate would carry contract v4 under a schema row pinned to v3. | 1 line |
| `src/arctic_qa/model_roles.py`, `config/roles.v1.json` | The whole-set verdict is a new role, `option_set_verifier`. It is a judge role and a strongest-judge role, and every profile assigns it the same model as `option_verifier`. | 9 and 15 lines |
| `src/arctic_qa/broker_provider.py` | `ROLE_STAGES["option_set_verifier"] = "option_verification"`. The bound price config needs no transition. | 3 lines |
| `src/arctic_qa/gemini_batch.py` | The offline capture placeholder for option requests carries the new verdict keys. | 11 lines |
| `src/arctic_qa/pipeline_trace.py` | One entry in the invalid-response stage map. | 1 line |
| `fixtures/fake-author.jsonl`, `fixtures/fake-verifier.jsonl` | Six proposals, four option verdicts with the new keys, one whole-set verdict, a v4 standalone verdict. | 13 lines |
| `fixtures/standalone-calibration-v1.jsonl` | Header only: contract v4, the north-star re-worded without the guessability clause, the blocking note names the live-run rule. The gates slice owns the rows. | 1 line |
| `tests/test_bounded_fallback.py`, `tests/test_gemini_batch.py`, `tests/test_question_context.py`, `tests/test_standalone_calibration.py`, `tests/test_streaming.py`, `tests/test_writer_context_bundle.py`, `tests/test_cli_integration.py` | Call counts (six proposals, four verdicts, one set verdict), the v4 key set, the namespaced deterministic codes, schema 2.8.0. | see `git diff --stat` |

Sibling overlap that integration must merge with care:

- `standalone_deterministic_reason` wraps `benchmark_context_verification_reason`.
  The gates slice changes the acronym matcher inside that function.
  Keep the wrapper; the namespace is the only thing this slice adds there.
- `question_context_verification_reason` (validation.py) now emits the `standalone_det_` code for the free screen.
  The answer verifier's semantic verdict still emits `question_context_referent_unresolved`.
  One code, one producer.
- `_next_generation_attempt` gains one keyword.
  The routing slice may fold `_forwarded_slot_evidence` into its slot pool built from the text the writer sees (audit 4.6 c).
  The pure function `unsatisfiable_standalone_demands` in generation.py is the contract; the caller decides which texts feed it.
- `CANDIDATE_CONTRACTS["2.8.0"]` carries `option_verification_contract_version`.
  The writer-context slice adds its own keys to the same row.

## 2. Contract versions this slice owns

| Constant | Predecessor | New value |
| --- | --- | --- |
| `STANDALONE_VERIFICATION_CONTRACT_VERSION` | `source-blind-scientific-referent-v3` | `source-blind-scientific-referent-v4` |
| `CHAPTER2_STANDALONE_VERIFICATION_CONTRACT_VERSION` | none | `source-blind-scientific-referent-v3` (pins schema 2.7.0) |
| `OPTION_VERIFICATION_CONTRACT_VERSION` | none | `option-admitting-interpretation-v1` |

Stored chapter 2 candidates (schema 2.7.0) keep validating under v3 and the legacy option key sets.
`_option_response_schema_valid` compares key sets exactly, so the option contract carries its own version as the audit required.

## 3. The prompt clause and its replacement (audit 4.2)

17 of the 18 confirmed false fails quoted this clause of the chapter 2 prompt (generation.py line 143 at commit bd2fb22):

> 4. Ask whether a strong scientist who cannot see the paper can choose the correct option from the displayed text alone, or whether the asked-for value is an arbitrary study-specific quantity. If the latter, the task fails.

Line 149 swallowed the pass list:

> A location or period is necessary whenever the value can differ between sites or periods.

Both are gone.
The replacement block is the audit's block, word for word:

> Apply this test in order. Stop at the first step that fails.
> 1. State in one sentence what the task asks for. If you cannot, the task fails.
> 2. Name the kind of answer the task wants, such as a percentage, a taxon, a direction, or a count. If you cannot, the task fails.
> 3. List every referent the task uses. A referent fails when the displayed text never says what it is. If any referent fails, the task fails.
> 4. For each detail you believe is still missing, apply the necessity test. The task passes when no missing detail is necessary.
>
> The task asks for a value that one study measured. That is the purpose of this benchmark.
> NEVER fail a task because the value is specific to one study, because the value is "arbitrary", or because the reader could not derive or guess the value without the paper. Those are true of every correct task here. Judge only whether the reader knows WHAT is asked.
>
> NECESSITY TEST. A missing detail is necessary only when one of these is true.
> (a) The displayed text names or implies more than one candidate referent, so two readers who both understand the task can defend answers about different things. Write both readings out.
> (b) The reader cannot tell what kind of fact the task asks for.
> A location or a period is necessary whenever the displayed text does not identify which single result is meant, including when it names no site and no time and the quantity is site-specific or time-specific.
> It is NOT necessary merely because the value would differ at another site or in another year, when the displayed text already identifies one result.

Line 136 now reads: "The reader will later choose between four mutually exclusive options of one type. You do not see them, so never judge whether the reader could pick the right one."

The evidence rule (`STANDALONE_EVIDENCE_RULE`) is appended and is the text quoted back on the re-ask:

> Report a reason code only with the evidence that selects it.
> For every undefined_* code, copy into unresolved_phrases the exact words of the DISPLAYED TEXT that you cannot resolve.
> The value the task asks for is never an unresolved phrase: a question never states its own answer.
> Use multiple_interpretations only when you can write out two different answers that two readers could each defend. Put both readings in competing_readings.
> Never use a reason code to record that a value is study-specific or not derivable.

Kept word for word: the seven-line fail list, "A study, publication, author, journal, dataset, or campaign identity is never a necessary detail.", "A named campaign, cruise, core, or project code does not resolve a referent.", the source-blind input ("You receive only the question and question_context."), and the judge on a different and stronger model than the writer (`model_roles.assert_role_separation`, unchanged).
`tests/test_ch3_judge_options.py::test_standalone_prompt_keeps_the_fail_list_word_for_word` pins every line.

## 4. Findings addressed, with the exact conditions of every rule

### SG-1, SG-2 (audit 4.2): the prompt

Covered in section 3.
Rule: no change beyond the block above and the line 136 sentence.

### SG-3 as amended (audit 4.2): evidence-bound codes and the one re-ask

- Schema: `competing_readings` is a required array on `standalone_verifier`.
- Rule `standalone_verdict_is_unevidenced(verdict)`: true when `pass` is false and either (a) any reason starts with `undefined_` and `unresolved_phrases` holds no non-blank string, or (b) `multiple_interpretations` is reported and `competing_readings` holds fewer than two non-blank strings.
  Every other code is out of scope, as the audit amended.
- Rule in `generate_candidate`: when the first verdict is unevidenced, the judge is called once more on the same entity id with the prompt extended by `CONTRACT_VIOLATION` and the evidence rule quoted back.
  The different prompt hash gives a new call row, so the receipt binding holds.
  The second verdict is the verdict, whatever it says.
  `provenance.standalone_verification_reask` records `{reason, first_verdict_fingerprint, reask_count: 1}`, or is null when no re-ask ran.
- Rule in `_standalone_gate_reasons` and `validation._standalone_reason_codes`: an unevidenced verdict yields exactly `["standalone_verdict_unevidenced"]` and no referent code.
- Routing: `standalone_verdict_unevidenced` is in `ALTERNATIVE_FINDING_REASONS` and `IMMEDIATE_ALTERNATIVE_FINDING_REASONS`, so the family moves to another finding and never spends a revision.
- Bound: exactly one re-ask per candidate. `tests/test_cli_integration.py` proves two judge calls when the re-ask is unevidenced again, two when it supplies evidence, and one when the first verdict is evidenced.

### F7 (audit 4.2): fingerprint and namespace

- `verdict_fingerprint` is bound on every verdict by `_bind_standalone_contract_version`, next to the contract version.
  It is the sha256 of the canonical JSON of the sorted reason codes and the sorted `missing_detail_types`, and nothing else.
  The phrases stay out, because the 15 byte-identical repeats of chapter 2 differed only in their phrases.
  `standalone_verification_resolves` recomputes it and rejects a record whose fingerprint does not match.
  The receipt matcher treats it as controller-owned, like the contract version.
  Routing reads it; the routing slice owns the lineage-wide repeat detector that stops a revision whose fingerprint did not change.
- `standalone_deterministic_reason` prefixes the free screen's codes with `standalone_det_`.
  `generate_candidate` and `question_context_verification_reason` both use it.
  The five namespaced codes are registered in the routing sets with the repair rungs of their unprefixed forms (`REPAIRABLE_QUESTION_REASONS`, `SURGICAL_CORRECTION_REASONS`, `_DEPENDENT_ROUTING_REASONS`).

### F3 as amended (audit 4.2, and 4.6 c): the satisfiability guard

- Pure function `generation.unsatisfiable_standalone_demands(reason_codes, *, answer_scope, supplied_slots)`.
  It returns the subset of `{standalone_undefined_location, standalone_undefined_period_or_event}` present in the codes for which the frozen `answer.scope` dimension (`geography`, `period`) is null or blank and the matching slot kind (`place`, `period`) is absent from `supplied_slots`.
  `supplied_slots=None` returns the empty set: unknown text never blocks.
- Registration in streaming.py: `_forwarded_slot_evidence(path)` computes the slot kinds from the candidate's forwarded context-only spans plus the frozen finding's own evidence quote, with the existing `_slot_evidence_types`, and returns None when no such text exists.
  `_next_generation_attempt` routes a non-empty result through the existing unmet-slot branch (`alternative_finding` with trigger `slot_evidence_unavailable`, or None off the first finding).
  The raw eligibility spans are not consulted for this decision.
- There is NO freeze-time rejection.
  `tests/test_ch3_judge_options.py::test_there_is_no_freeze_time_dimension_rejection` asserts that `finding_scope_dimension_unavailable` does not exist in generation.py.
  The audit's two examples still pass the guard: family c2f433d3 (`scope.period` null, no forwarded period) now leaves the finding instead of burning revisions; family abc7e504 ("a cloudy day in August") supplies a period, so a revision is still allowed.

### D1 with C6 (audit 4.8): the admitting interpretation

- Schema order on `option_verifier`: `rationale`, `admitting_interpretation`, `contradiction_established`, `option_standalone_interpretable`, `question_admits_option_as_correct`, then the span.
  Every boolean carries the audit's description.
  `true_in_different_context` and `alternative_answer_search_passed` are gone from this role.
- Prompt: `OPTION_ADMISSION_RULE` replaces the two overloaded sentences.
- Rule `option_verdict_is_malformed`: `question_admits_option_as_correct` true with a blank `admitting_interpretation`.
  In `_generate_distractors` a malformed verdict is re-asked once, on the same entity id, with `CONTRACT_VIOLATION` and the rule quoted back.
  `provenance.option_verification_reasks` records one entry per re-asked option.
- Rule `option_verdict_rejection_reason` at validation, in order: `contradiction_established` false gives `option_contradiction_unresolved`; `option_standalone_interpretable` not true gives `option_standalone_uninterpretable`; the flag true with a blank reading gives `option_admission_unexplained` (the rejection stands); the flag true with a reading gives `option_correct_under_question_interpretation`.
  Legacy key sets keep their legacy codes.
- Bound: one re-ask per option, so at most one per candidate per malformed option.
  `tests/test_cli_integration.py` proves five option calls with one repaired re-ask, and six with a second malformed answer that stands as the rejection.

### D2 as amended (audit 4.8): six proposals, rank order, one whole-set call

- `distractor_writer` schema: `minItems` 6, `maxItems` 8; the prompt says "Propose exactly six typed distractors" and asks for best-first ranking.
- Per-option verdicts stay one call, one hash-bound verdict, one receipt per option.
  They are not batched, as the audit amended.
- Rank-order stop: verification runs in the writer's order and stops when `OPTION_VERIFIED_TARGET` options carry an admitting verdict (`option_verdict_rejection_reason` is None).
  The rest are recorded in `provenance.option_verification_deferred` and are not part of `distractors`, so they never appear in the rejection ledger or the repair feedback.
- `OPTION_VERIFIED_TARGET` is 4, not 3.
  The floor of three is unchanged.
  The answer-absent MCQ export (`exporting._absent_mcq`, task type `answer_absent_mcq`) needs four verified distractors and is a tested product output.
  A stop at three would silently end that export.
  The constant is one line; see deferred item 1.
- Whole-set call: role `option_set_verifier`, system prompt `OPTION_SET_SYSTEM`, source-blind by construction (the prompt holds QUESTION, QUESTION_CONTEXT, the ordered OPTIONS with the answer marked, and the binding; no SOURCE_DATA, no ANSWER_RECORD).
  It is bound to `qa_hash` and the ordered option hashes through `set_hash = stable_id("option-set", qa_hash, *option_hashes)` and carries its own receipt on entity `option-set-verdict`.
  It returns `overlapping_option_pairs`, `options_mutually_exclusive` and `answer_choosable_from_displayed_text`, the question-level "can a reader choose" test that section 4.2 moved here, stated so that the reader is never expected to know the value.
- Validation (`option_set_verdict_reasons`, schema 2.8.0 only): the verdict must exist, bind to this source and question, name only hashes of this candidate's distractors with no duplicate, contain every accepted option's hash, hash correctly, carry provenance, and match its receipt.
  Then `options_mutually_exclusive` not true gives `option_set_not_mutually_exclusive` and `answer_choosable_from_displayed_text` not true gives `option_set_answer_not_choosable`.
  A subset of a mutually exclusive set stays mutually exclusive, so a later per-option rejection does not stale the verdict.
- Routing: both set codes are in `OPTION_REPAIR_REASONS` and `REPAIRABLE_QUESTION_REASONS`; they earn `option_repair` on the verified question.

### D3 (audit 4.8): the closed kind enum

- `deterministic.kind` is the closed enum `numeric_outside_tolerance`, `unique_categorical`, `directional_contradiction`, `scope_excluded`, `unique_entity`, `closed_set`, in the schema with a description per kind (`OPTION_DETERMINISTIC_KINDS`) and in the prompt.
- The arithmetic contradiction stays what it was in `validate_distractor`: an extra label (`deterministic-contradiction`) computed beside the Pro verdict after the verdict admits the option.
  No verdict object is ever fabricated, and the Pro call is still made for every option that reaches it.
  The enum makes the label reachable for the first time.

### D4 as amended (audit 4.8): fail fast, closure precondition, superlative closure in shadow

- Free prefilter: `option_free_rejection_reason` is the free prefix of `validate_distractor`, in the gate's order, and `_generate_distractors` runs it on every proposal with the duplicate test against the options already kept.
  It admits nothing: every surviving option still runs the full gate.
- Fail fast: when no proposal survives, `CandidateRejectedError("option_pool_empty_after_prefilter")` is raised with every option text and its code in the message, which `_record_generation_rejection` stores.
  No candidate with zero options is persisted as `incomplete_non_mcq`.
  Registered as an immediate alternative-finding reason.
- Closure precondition: `closed_set_closure_reason(answer)` returns `closed_set_closure_not_source_established` when `deterministic_rule.kind` is `closed_set`, the set has two or more members, the member type is not `quantity`, and `_source_establishes_complete_set` is false on the frozen evidence quote.
  `_generate_distractors` raises it before the writer call, so no paid option call is made.
  Registered as an immediate alternative-finding reason.
- Superlative closure: `definite_superlative_closure(source_text, source_values)` is true only for a definite, counted superlative ("the two dominant compounds", "the two most abundant") whose count equals the number of members, with every member inside that one sentence and no enumeration lead.
  It is shadow only.
  `validate_distractor` records `shadow_labels: ["superlative_closure_would_establish_set"]` on a `closed_set_contract_invalid` result when the narrowed closure would have held.
  It changes no verdict.

### D5 as amended (audit 4.8): repair guidance

- `OPTION_REPAIR_GUIDANCE` gives one instruction per code (`option_correct_under_question_interpretation`, `option_contradiction_unresolved`, `option_standalone_uninterpretable`, `option_set_not_mutually_exclusive`, `option_set_answer_not_choosable`, and the display codes).
- The writer prompt names the construction vocabulary (opposite direction, opposite timing, alternate category, alternate place, alternate magnitude).
- No construction ban was added.
  `tests/test_ch3_judge_options.py` asserts the three refuted bans are absent.

### D6 (audit 4.8): the per-option source-blind test

- Renamed to `option_standalone_interpretable`, described, and sequenced behind the six-proposal pool.
- `true_in_different_context` is removed from the required set.
- The per-option source-blind instruction text is unchanged.

## 5. Rigor safeguard per change

Every change either tightens a contract or moves nothing on the pass side.

- Standalone prompt: the pass side of the chapter 2 judge was 54 of 55 precise, and the change edits only the fail side.
  The fail list, the publication-identity ban, the source-blind input and the role separation are unchanged and pinned by tests.
  Paper support is still judged by the reconstructor, the answer verifier and the scope contract, none of which changed.
- Evidence-bound codes: a genuine referent defect always has a phrase to quote and a genuine ambiguity always has two readings.
  A verdict with neither is a contract violation.
  The re-ask asks the same judge the same question with the rule quoted; it never passes an item, it only replaces a verdict that carried no evidence.
  If the second verdict is unevidenced too, the family leaves the finding; nothing is accepted.
- Namespace and fingerprint: bookkeeping only; neither can admit an item.
- Satisfiability guard: it removes revisions, never adds acceptances.
  A demand the source cannot meet is no longer argued for three attempts.
- Admitting interpretation: no option is accepted without an explicit not-admitted verdict from the Pro model.
  A rejection now needs a stated reading, and a silent flag is re-asked once, then stands as a rejection.
  The sea-ice-concentration catch of family c0f8d9a4 becomes auditable.
- Six proposals and rank order: the floor of three is unchanged; every verified option carries its own hash-bound verdict and receipt; a deferred option is never exported.
- Whole-set call: an additional gate, source-blind, receipt-bound, on top of the per-option verdicts.
  It can only reject.
- Closed enum: a deterministic contradiction is arithmetic against a hash-bound source value, recorded beside the model verdict and never in place of it.
- Free prefilter and fail fast: the prefilter is the gate's own free prefix; it admits nothing.
- Closure precondition: an extra rejection before any paid call.
- Superlative closure: shadow label only; no verdict changes.
- Verdict binding by hash, the floor of three, the display prefilter, `_option_needs_independent_support`, and the per-option source-blind instruction are unchanged (the "Keep" column of section 5).

## 6. Expected effect on acceptance and on cost per accepted item

From the audit's own figures.

- Standalone prompt and evidence rule: the 8 families with this clause as primary cause spent USD 2.06 to 2.36 for zero items.
  Only 3 of the 18 false fails carry no other kill code, so this fix alone yields +1 to +2 items and needs 4.1 and 4.3 to deliver the rest (17 auditor-confirmed candidates released into the downstream gates).
  The evidence rule and the code-only fingerprint end the byte-identical repeat loop, about USD 0.65 direct plus the extraction calls those retries triggered.
  The judge re-ask costs about USD 0.0075 per violation and is capped at one.
- Satisfiability guard: families c2f433d3 (USD 0.272) and abc7e504 (USD 0.2677) stop spending revisions on a dimension no span supplies; the saving scales with the corpus.
- Option stage: the false-flag fix removes about USD 0.28 of `option_repair` rounds; rank-order verification saves up to two Pro calls per clean candidate against six proposals (about USD 0.03 per candidate at the chapter 2 per-call price); the whole-set call adds one short source-blind Pro call per clean candidate (well under USD 0.01, no source text in the prompt); the fail-fast and the closure precondition remove the wasted second distractor call and every downstream option call on an unservable set.
  The stage falls from USD 0.78 toward about USD 0.45 at chapter 2 volume, and the saving scales with acceptance.
  Family 6d4fb53e now fails for free at the closure precondition instead of after USD 0.36; it yields an item only when the shadow-measured superlative closure is promoted.
- Whole-run projection (audit section 6 of `standalone_gate.md`, with the other slices): roughly 10 to 11 accepted items on about USD 16 of spend, USD 1.45 to 1.60 per item against USD 3.33, before the phase D reorder that the cost slice owns.

## 7. Calibration

- `fixtures/standalone-calibration-v1.jsonl`: header re-worded (contract v4, north star without the guessability clause, the live-run rule in the blocking note).
  Rows untouched; the gates slice re-labels them.
- `fixtures/standalone-calibration-ch3-judge-slice-v1.jsonl`: an iteration slice in the same row format.
  Six `must_pass` rows are chapter 2 confirmed false fails with their chapter 2 verdicts on record (families c2f433d3, abc7e504, 3e5633fb, c512faf6).
  Three `must_fail` controls guard the deleted clause: a study-specific value with a dangling "this estimate", a task with no site for a site-specific rate, and an unexpanded HELiPOD.
  Each control names the evidence its verdict must carry.
  The rows were used to derive SG-1 to SG-3, so the header forbids them in the held-out slice.
- The free deterministic half still rejects the six must_pass rows on CMP22, "CO 2", the hyphenated "Ice-Tethered Profiler (ITP)" gloss and "(CHINARE 2012)".
  Those are audit 4.3 matcher defects owned by the gates slice.
  The rows carry `deterministic_expectation: reject_pending_gates_fix` and `tests/test_standalone_calibration_ch3_slice.py` pins the hand-off: the judge half releases every row, and the rows flip to `pass` when the matcher lands.
- No judge prompt ships without a live calibration run.
  The run is paid (about USD 0.20), needs captain approval, and is run by the integration crew through the `calibrate-standalone` harness that the gates slice builds.
  This branch makes no paid call.

## 8. Test results

Run as `nix develop -c bash -c 'PYTHONPATH=src pytest tests/<files> -q'` in four bounded foreground parts, after `ruff check` (clean) and `ruff format` (clean).

| Part | Files | Result |
| --- | --- | --- |
| 1 | `tests/` except the three below (36 files) | green |
| 2 | `tests/test_cli_integration.py` (98 tests) | green |
| 3 | `tests/test_streaming.py` | green |
| 4 | `tests/test_model_broker.py` | green |

888 tests collected in total.
New tests: 75 in `tests/test_ch3_judge_options.py` and `tests/test_standalone_calibration_ch3_slice.py`, plus 6 scripted end-to-end tests appended to `tests/test_cli_integration.py` (the two standalone re-ask bounds, the never-re-asked control, the two option re-ask bounds, the fail-fast).

## 9. Deferred items, with owner

1. `OPTION_VERIFIED_TARGET` 4 versus 3 (captain, through integration).
   The audit says three; three saves one more Pro call per clean candidate (about USD 0.016) and ends the `answer_absent_mcq` export.
   Four keeps the export.
   One constant in generation.py, one comment beside it.
2. The live calibration run of contract v4 on the judge model (integration, paid, captain-approved), through the `calibrate-standalone` harness (gates slice).
3. The acronym matcher false positives that hold the six must_pass slice rows (gates slice, audit 4.3).
4. Consumption of `verdict_fingerprint` by the lineage-wide repeat detector (routing slice, audit 4.6 e).
5. The slot pool built from the text the writer sees (routing slice, audit 4.6 c).
   `_forwarded_slot_evidence` reads the forwarded context-only spans and the finding's evidence quote; the routing slice may widen it and keep the pure guard.
6. The short circuit that skips reconstruction and verification after a standalone fail (cost slice, audit 4.4).
   This slice still buys both calls on a failed candidate, as chapter 2 did.
7. Batch capture mode (`gemini_batch.py`) prepares all six option requests in one round, because a placeholder verdict rejects every option; the streaming path stops at the target.
   Chapter 3 runs on the streaming path.
   If the batch path is used again, integration should decide whether to cap the captured requests at the target (cost slice).
8. The shadow measurement of the narrowed superlative closure over a batch before any gate use (integration, phase E).
9. Viewers and summaries that key on the string `question_context_referent_unresolved` now also see `standalone_det_question_context_referent_unresolved` for the free screen (integration to confirm in the corpus viewer; the suite is green).

## 10. Done

Branch `fm/arctic-ch3-judge-options-r1`, clean, on top of local `main` at bd2fb22.
No push, no PR, no paid call.
