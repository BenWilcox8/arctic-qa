# Chapter 3 slice report: eligibility contract, prompt v8 and schema v4

Task: `arctic-ch3-eligibility-r1`.
Branch: `fm/arctic-ch3-eligibility-r1`, from local `main` at `bd2fb22`.
Date: 2026-09-15.
No paid provider call was made. No file under `/mnt/crdata` was touched. Legacy and chapter 2 data are unchanged.

## 1. Audit findings addressed

| Id | Source | State |
|---|---|---|
| 4.7 E1 | The prompt and the schema never state the missing-context rule the validator enforces. | Done. The check is non-fatal, and prompt v8 and schema v4 state the rule. |
| 4.7 E2 | The bounded geography re-screen selects zero papers, because two functions define "unresolved" differently. | Done. One shared definition, a unit test that the two agree, and the re-screen wired into the streaming path. |
| 4.7 E6 | A formatting mistake parks the paper until a later batch pass, so the bounded re-ask never ran. | Done. The re-ask runs inside the streaming pass, with a specific repair note. |
| 4.7 E8 | The reason-code vocabulary is unbounded, so no routing rule can read it. | Done. A per-criterion enum plus `other`, recorded and never decisive. |
| 4.7 C5 | Two-pass screening. | Done as a shadow measurement booked at USD 0. It is never a decision path. |
| Section 5, phase B, "Eligibility prompt v8 and schema v4" | Study-setting spans by dimension, intersection test, dimension label validated, reason-code enum. | Done. |
| Section 5, phase D, "Eligibility" | Format re-ask in the streaming path, two-pass screening in shadow only. | Done. |
| 4.7 E7 | A content floor on modeled-domain spans. | Not implemented. Section 4.10 records it as refuted. |
| Stage findings E3, E4, E5 | The writer-facing half: `_context_only_span_is_usable`, `_eligible_generation_scope`, the context bundle. | Not in this slice. Owner: `arctic-ch3-writer-context-r1`. This slice ships the dimension labels that slice consumes. |

## 2. Every new or changed rule, with its exact condition

### 2.1 The `missing_context` check is non-fatal

File: `src/arctic_qa/gemini_eligibility.py`, `_validate_response_span_contract`.

Exact condition. For one criterion row with `status == "uncertain"` and an empty `missing_context` array:

- when that criterion row produced no other validation error, the validator records `criterion_missing_context_absent:<criterion>` in the new `contract_notes` list and emits no error;
- when that criterion row already produced an error (`criterion_evidence_missing`, `evidence_invalid`, `evidence_span_duplicate`, `evidence_span_unknown`), the code is emitted as an error as before.

The validator counts the errors it holds when it enters the row and compares that count after the evidence loop. That comparison is the "otherwise complete" test.
The code is also added to `FORMAT_ERROR_CODES`, so a future bare case is re-asked once instead of ending the paper.
The value is never filled from the paper-level `known_missing_context`. Every chapter 2 response carries the same four generic gaps, and such a fill would erase the per-criterion diagnosis.

Prompt v8 states the rule after "Use uncertain when the supplied material does not support a decision.":
"When a criterion status is uncertain, write at least one value in that criterion's missing_context. Name what you could not read."
Schema v4 states the same rule as the `missing_context` description.

Tests: `tests/test_eligibility_contract_v8.py::test_an_empty_missing_context_is_recorded_and_no_longer_kills_the_paper`, `::test_an_uncertain_geography_keeps_its_decision_with_no_missing_context`, `::test_the_code_stays_fatal_when_that_criterion_record_is_broken`, `::test_the_residual_case_is_re_askable_rather_than_terminal`, `::test_v8_states_the_missing_context_rule_the_validator_enforces`.

Rigor safeguard. `missing_context` is a diagnostic field. It takes no part in `_status_mapping_v2`, which computes the decision in Python from the five criterion statuses. An empty `missing_context` cannot move a status and cannot make a paper eligible. The change turns a lost call into a recorded decision, and the recorded decision is the real one. The replay confirms this: of the 22 papers, 18 record `uncertain` and 4 record `excluded`, and none records `eligible`.

### 2.2 `geography_rescreen_keys` aligned with `_status_mapping_v2`

File: `src/arctic_qa/gemini_eligibility.py`.

One definition now serves both. `REQUIRED_CRITERIA` holds the four criteria that must be satisfied. `CORRECTION_SATISFIABLE_STATUSES` holds `satisfied` and `uncertain`. `unsatisfied_required_criteria(statuses, ignore=...)` names every criterion that still bars an eligible decision.

- `_status_mapping_v2` calls it with no exemption.
- `geography_rescreen_eligible(statuses)` calls it with `study_geography` exempt, and requires the result to be empty and `study_geography` to be `uncertain`.
- `geography_rescreen_keys` calls `geography_rescreen_eligible`.

The chapter 2 defect was that `geography_rescreen_keys` counted `correction_retraction_coverage` as blocking. That criterion is `uncertain` on 79 of 79 non-eligible papers, because the frozen corpus has no retraction metadata, so the selected set was empty for every paper in the corpus.

Unit test that the two agree: `tests/test_eligibility_contract_v8.py::test_the_rescreen_and_the_status_mapping_agree_on_every_status_grid`. It walks all 81 status assignments of the other four criteria, and for each one asserts that `geography_rescreen_eligible` is true exactly when replacing `study_geography` with `satisfied` makes `_status_mapping_v2` return `eligible`. The two cannot drift again without this test failing.

Rigor safeguard. A failed geography is a decision, not an unresolved criterion, so a correct exclusion never returns. `::test_a_failed_geography_never_returns_to_the_re_screen` pins that.

### 2.3 The geography re-screen wired into the streaming path

Files: `src/arctic_qa/streaming.py`, `config/gemini-eligibility-geography-rescreen-v2.txt`.

Exact condition. After a first screening that is valid, `_run_eligibility` runs at most one re-screen call when `geography_rescreen_eligible` is true for that paper's criterion statuses. The re-screen:

- uses re-screen prompt v2, which carries the v7 ARCTIC SCOPE SPANS and QUESTION SCOPE PHRASES blocks, updated to the v8 dimension rule, so a recovered paper enters with the same span and phrase discipline;
- carries a `repair_request` block with `kind: geography_rescreen` and `frozen_criterion_statuses`, which holds the four criteria it may not touch;
- returns a complete v4 response, validated by the same `validate_response` against the same hashes and the same span catalog.

The answer replaces the first screening only when all three hold: the response is valid, no frozen criterion status moved, and the re-screen job row is receipt-bound. Otherwise the first screening stands, and the row records `unresolved_invalid_response` or `refused_moved_frozen_status`.

Tests: `tests/test_eligibility_streaming_recovery.py::test_an_unresolved_geography_is_re_screened_once_in_the_same_pass`, `::test_a_failed_geography_is_never_re_screened`, `::test_a_second_unresolved_criterion_blocks_the_re_screen`, `::test_a_re_screen_that_moves_a_frozen_status_is_refused`, `::test_an_invalid_re_screen_answer_leaves_the_first_screening_in_place`, `::test_no_re_screen_prompt_means_no_re_screen_call`, and `tests/test_eligibility_contract_v8.py::test_the_rescreen_prompt_v2_carries_the_v8_span_and_phrase_blocks`.

Rigor safeguard. The re-screen decides one criterion and freezes the other four, and `_rescreen_moved_a_frozen_status` refuses an answer that moves any of them. It applies the same ordered geography procedure, the same actual-study-evidence rule and the same "provenance is not entailment" sentence. A paper it leaves uncertain stays out. It is bounded to one call per paper per run.

### 2.4 The format re-ask in the streaming path, with a specific note

Files: `src/arctic_qa/streaming.py`, `src/arctic_qa/gemini_eligibility.py`.

Exact condition. When a screening answer is invalid and every error is in `FORMAT_ERROR_CODES`, `_run_eligibility` sends one repair call inside the same pass, bounded by `MAXIMUM_FORMAT_ATTEMPTS` (2 attempts in total). The repair note now carries the diagnosis, not only the code:

- `unbound_phrases`: each `question_scope_phrases` value that is not in the joined finding-span text, under the binding projection;
- `finding_span_text`: the text it was compared against;
- `mislabelled_dimensions`: each `{span_id, dimension}` whose span text does not state that dimension;
- `frozen_criterion_statuses` and the instruction not to change any of them, both unchanged.

The validator produces this detail where it raises the code, under `validation.format_repair_detail`, so the note is built from the validator's own evidence.

The repair answer is refused when either holds:

- it moved a criterion status (`_repair_moved_a_status`, unchanged), recorded as `repair_changed_criterion_status`;
- a repaired `question_scope_phrases` value no longer names a station, region, stratum, population or modeled domain, recorded as `eligible_arctic_scope_phrase_not_specific`. `phrase_is_specific` refuses a bare number, a number with a percent sign or a bare unit, and the vague labels "In the Arctic", "the Arctic", "the study area", "this study", "the region", "the site", "the sites", "the station". This test runs on a repair answer only, so it cannot change what a first-pass answer decides.

A paper that spends its attempts on the shape of its answer ends as `unresolved_rescreenable`, not as a terminal `screening_error`. `_brokered_eligibility_state` keeps that state through the resume-time re-validation.

Tests: `tests/test_eligibility_streaming_recovery.py::test_a_formatting_mistake_is_re_asked_inside_the_same_pass`, `::test_the_re_ask_is_bounded_and_leaves_the_paper_re_screenable`, `::test_an_envelope_error_is_never_re_asked`, `::test_a_repair_that_moves_a_criterion_status_is_refused`, `::test_a_repaired_phrase_that_names_nothing_is_refused`, and `tests/test_eligibility_contract_v8.py::test_the_repair_note_names_the_phrase_that_failed_and_the_text_it_missed`, `::test_a_repaired_phrase_must_still_name_a_station_region_or_population`.

Rigor safeguard. A formatting mistake is a mistake about how the answer is written, never about the science. `_repair_moved_a_status` refuses any repair that moves a criterion status and is unchanged. A provider envelope error, a refusal and a malformed response stay terminal and are never re-asked.

### 2.5 Receipt binding for a re-ask and a re-screen

File: `src/arctic_qa/streaming.py`.

A re-ask and a re-screen send different bytes, so each takes its own job key, its own broker receipt and its own job row. `_eligibility_job_key` hashes the base job key, the provider identity, the model, the authority and, when present, the exact `attempt_note` the payload carries. An attempt with no note produces the same key the chapter 2 code produced, so an existing row still validates.

Each job row records `eligibility_attempt` with `kind`, `attempt` and `attempt_note`. `_validate_brokered_eligibility` rebuilds the request from that record, so a resumed run re-derives the exact payload and re-checks the receipt.

`_load_eligibility_jobs` now accepts a row written under the main prompt or under the re-screen prompt, and takes the last attempt for one candidate, in the order initial, format repair, geography re-screen. Two rows of the same kind and attempt for one candidate are still refused.

Tests: `tests/test_eligibility_streaming_recovery.py::test_each_attempt_binds_its_own_receipt_identity`, `::test_the_loader_takes_the_last_attempt_of_one_candidate`, `::test_the_loader_still_refuses_two_jobs_of_the_same_attempt`.

### 2.6 Prompt v8 and schema v4

Files: `config/gemini-eligibility-prompt-v8.txt`, `schemas/gemini-eligibility.v4.schema.json`.
Response contract version: `eligibility-response-v4`.

Changes against v7 and v3:

1. Study-setting spans by dimension. `eligible_arctic_scope.activity_span_ids` (an array of strings, `maxItems` 24) becomes `eligible_arctic_scope.activity_spans` (an array of `{span_id, dimension}` objects, `maxItems` 12). The dimension enum is `geography`, `period`, `sample`, `method`, `definition`. The prompt asks for the dates or campaign window, what was sampled and how many, the instrument or platform, and the first expansion of each acronym the result sentences use.
2. An intersection test in place of the subset test. v7 demanded that every activity span also appear in the selected `study_geography` evidence, which made the writer's study-setting spans a subset of the spans that prove latitude. v8 demands that at least one activity span appear there. The validator applies `set(activity_ids) & geography_ids` for v4 and keeps `set(activity_ids) <= geography_ids` for v3.
3. The dimension label validated against the span text. `_dimension_supported(text, dimension)` requires a marker of that dimension in the span's own text, through the binding projection: a coordinate or a place word for `geography`; a four-digit year or a month name for `period`; a sampling or measurement word for `sample`; an instrument, platform, vessel, model or reanalysis word for `method`; an acronym-with-expansion shape for `definition`. A label the text cannot support raises `eligible_arctic_scope_dimension_unsupported`, which is format-repairable, so one bounded re-ask corrects the label.
4. A per-criterion reason-code enum plus `other`. The 18-value enum is in the schema, so the provider's structured output constrains what the classifier writes. The Python validator applies a relaxed copy of the schema in which the `reason_codes` enum is dropped, and records an out-of-enum code as the contract note `reason_code_out_of_enum:<criterion>`.

Kept, and pinned by `tests/test_eligibility_contract_v8.py::test_v8_keeps_every_rule_the_audit_ordered_kept` and `::test_a_clean_v4_response_validates_and_keeps_every_dimension`: span hashing by `source_bytes_sha256`, "Span location records provenance only", the actual-study-evidence rule, the ban on a title or a citation as Arctic scope, criterion-by-criterion decisions with no model-supplied overall decision, `_repair_moved_a_status`, the ordered geography procedure and the separable-component rule.

Rigor safeguards for this group:

- A study-setting span is still a hash-bound span of the same paper from the same frozen extraction, and it is still barred from answer evidence. The dimension label is metadata about a span the classifier already selected; it admits nothing new.
- The intersection test cannot admit a paper that has no geography-bearing span, because an empty intersection still raises `eligible_arctic_scope_activity_unbound`. The Arctic custody chain is unbroken. `::test_no_geography_bearing_activity_span_still_breaks_the_custody_chain` pins this.
- The dimension test makes the stage stricter. It can only refuse a label; it never admits a span the ordered geography procedure refused.
- The reason-code enum takes no part in `_status_mapping_v2` and no part in the re-screen pool. Enforcing it in Python would turn a vocabulary slip into a lost paper, which is the defect E1 removes, so an out-of-enum code is recorded and never decisive. `::test_a_reason_code_outside_the_enum_is_measured_and_never_decides` pins this.

### 2.7 Two-pass screening as a shadow measurement at USD 0

File: `src/arctic_qa/gemini_eligibility.py`, `shadow_two_pass_measurement`.

`shadow_two_pass_measurement(text, resolved_scope)` records the deferral rate and the span quality of a short first view: the short-view length, whether the short view holds an Arctic geography marker, the number of selected activity spans, and how many of them fall inside the short view. The record carries `applied: False`, `decision_path: False` and `booked_usd: "0"`. It makes no provider call, and no caller reads it as a status. Its only two labels are `defer_to_full_text` and `short_view_covers_selected_spans`, and neither is a status.

It is recorded on every eligibility job row, in the batch path and in the streaming path.

Test: `tests/test_eligibility_contract_v8.py::test_the_two_pass_screen_is_a_shadow_measurement_booked_at_zero`.

Rigor safeguard. The audit refuted the two-pass screen as a saving, because a positive eligibility decision is load-bearing downstream: the selected activity spans feed `_require_arctic_scope_custody` and the writer context bundle, so a short view cannot decide `eligible`. The rule that the short view never decides a paper's status is enforced by construction, because the function returns a record and no caller branches on it.

## 3. The chapter 2 replay

Fixture: `fixtures/ch2-eligibility-non-eligible-v1.jsonl`, 79 rows, one for each non-eligible chapter 2 paper. It is distilled from the read-only audit evidence at `data/arctic-ch2-yield-audit-r1/evidence/eligibility/batch-*.json`, and keeps the recorded decision, the recorded validation errors and the parsed criterion records. The audit bundle itself was not modified.

Replay: `tests/test_eligibility_ch2_replay.py`. It applies the real `_status_mapping_v2`, `format_repairable` and `geography_rescreen_eligible`, so a change to those rules changes these counts and the test fails.

| Outcome over the 79 non-eligible papers | Chapter 2 as run | After this slice |
|---|---|---|
| eligible | 0 | 0 |
| uncertain (unresolved) | 8 | 26 |
| excluded | 33 | 37 |
| screening error | 38 | 16 |

Of the 16 remaining screening errors, 13 are format-repairable (9 `eligible_arctic_scope_phrase_unbound`, 4 `eligible_arctic_scope_missing`) and are now reached by the streaming re-ask. The other 3 are `criterion_evidence_missing`, which is a real contract break and stays terminal.

The corrected `geography_rescreen_keys` selects 24 of the 26 unresolved papers, against 0 in chapter 2. That matches the audit's own count in finding E2.

Expected recovery of the 38 screening errors, as measured by the replay:

- 22 (`criterion_missing_context_absent`) become recorded decisions with no extra call: 18 `uncertain` and 4 `excluded`. Of the 18, the re-screen pool then covers those whose only remaining bar is geography.
- 13 are format-repairable and get one bounded re-ask each, at about USD 0.022 per call, about USD 0.29 in total. The audit judged most of these 13 as papers that should be eligible, among them `10.1038/nature10089` (Müller Ice Cap, 79.8 N), `10.1038/s41467-018-03756-1` (Renland ice cap, 71.30 N) and `10.1038/s41467-022-29523-x` ("pan-Arctic (60-90°N)").
- 3 stay terminal.

Screening errors fall from 38 of 200 (19 percent) to 16 of 200 (8 percent) from the non-fatal rule alone, and toward 3 of 200 (1.5 percent) to the extent the bounded re-ask succeeds. The audit's phase E target is under 5 percent.

No replayed paper becomes eligible without a new model judgment. `::test_no_replayed_paper_becomes_eligible_without_a_new_judgment` pins that, and also pins that every one of the 33 recorded exclusions stays excluded.

## 4. Expected effect on acceptance and on Gemini cost per accepted item

These are expectations, not measurements. No paid call was made.

Acceptance. This slice does not admit any paper on its own. It does two things that raise the pool a later stage can accept from:

1. It returns the 22 lost calls as decisions and opens the 24-paper re-screen pool. The audit's analysts judged at least 14 of those 24 as papers that should be eligible. Of the 13 format-repairable papers, the audit judged the three named above as eligible.
2. It supplies the writer with study-setting spans labelled by dimension. The audit measured the effect of that supply: families that received a context-only span cost USD 1.39 per accepted item, families that received none cost USD 6.63. This slice removes the subset test that capped the supply. The gain is realized only when the writer-context slice lands its filter changes (stage findings E4 and E5), because chapter 2's locator filter and separable-component phrase filter deleted 36 percent and a further share of the forwarded spans.

Cost. The audit's cost plan, step 5, books this slice as +USD 0.53 on a chapter 2-sized run: about 24 re-screen calls at the run average of USD 0.0222, plus about USD 0.29 for the bounded re-asks, against USD 0.84 already spent on the 38 screening errors that bought nothing. The eligibility stage stays near USD 4.4. The two-pass measurement is booked at USD 0.

Cost per accepted item. Step 5 of the cost plan moves the projection from USD 2.12 to USD 2.20 at a fixed yield of 6 items, because it buys papers rather than saving calls. The saving arrives through yield: the audit's combined estimate is 12 to 18 accepted items from the same 200 papers, which puts the run at about USD 0.70 to 1.00 per accepted item once the other five slices land. This slice alone changes no accepted item count and should not be booked as a saving.

## 5. Test results

Command: `nix develop -c bash -c 'PYTHONPATH=src pytest tests/<files> -q'`, run in bounded foreground parts.

| Part | Result |
|---|---|
| `tests/` without `test_streaming.py`, `test_cli_integration.py`, `test_model_broker.py` | 617 passed |
| `tests/test_streaming.py` | 56 passed |
| `tests/test_cli_integration.py` and `tests/test_model_broker.py` | 178 passed |
| New: `tests/test_eligibility_contract_v8.py` | 24 passed |
| New: `tests/test_eligibility_streaming_recovery.py` | 15 passed |
| New: `tests/test_eligibility_ch2_replay.py` | 5 passed |

The whole suite is green: 851 tests, all passing. `ruff check .` passes. `ruff format --check` passes for every file this slice touched; `data/arctic-current-yield-audit-r1/freeze_cohort.py` was already unformatted on `bd2fb22` and was left alone.

## 6. Files touched outside the owned set

The owned set is `gemini_eligibility.py`, `screening.py`, the eligibility prompts and schemas, and the eligibility and re-screen calls inside `streaming.py`.

| File | Change | Why, and what integration must reconcile |
|---|---|---|
| `src/arctic_qa/streaming.py`, routing sets | Added `ELIGIBILITY_CONTRACT_REASONS`, holding `eligible_arctic_scope_dimension_unsupported` and `eligible_arctic_scope_phrase_not_specific`, each with a one-line comment. | Every new reason code needs a routing layer entry. These two codes end a screening attempt before any candidate exists, so no candidate-level rung may claim them. The routing slice owns the logic. `tests/test_eligibility_streaming_recovery.py::test_every_new_eligibility_code_has_one_routing_entry` asserts that no candidate-level set holds them. |
| `src/arctic_qa/streaming.py`, `_load_eligibility_jobs` | Accepts a row under the re-screen prompt and takes the last attempt per candidate. | Needed because one paper can now hold three rows. The duplicate guard is kept for rows of the same kind and attempt. |
| `src/arctic_qa/streaming.py`, `_validate_brokered_eligibility` | New optional `rescreen_prompt_file` parameter; rebuilds the request from the recorded `eligibility_attempt`. | Needed so a re-asked or re-screened row stays receipt-bound on resume. |
| `src/arctic_qa/streaming.py`, `run_stream`, `_write_run_manifest`, `_trusted_brokered_eligibility_decisions` | New optional `eligibility_rescreen_prompt_file` parameter, and `eligibility_rescreen_prompt_sha256` in the run manifest. | The re-screen prompt must be a declared, hashed run input. |
| `src/arctic_qa/streaming.py`, version branches | `ELIGIBILITY_RESPONSE_V2`/`V3` set literals replaced by the shared `SPAN_CONTRACT_VERSIONS` and `SCOPE_CONTRACT_VERSIONS` constants. | Mechanical. It admits v4 at every branch point. A sibling that edits one of these lines should keep the constant. |
| `src/arctic_qa/cli.py` | New `--eligibility-rescreen-prompt-file` option on `stream`, defaulting to `config/gemini-eligibility-geography-rescreen-v2.txt`, passed only when the file exists. | The CLI is the only caller of `run_stream`. |
| `docs/BENCHMARK_INPUT_CONTRACT.md` | New section "Dimension-labelled study-setting spans" with the forwarded record shape. | Required by the brief, so the writer-context slice and this slice agree on the shape. |
| `fixtures/ch2-eligibility-non-eligible-v1.jsonl` | New, 79 rows distilled from the read-only audit evidence. | The replay fixture. |
| `AGENTS.md` | One line: a run directory can hold three eligibility job rows for one paper, and `_load_eligibility_jobs` takes the last attempt. | A sharp edge a future session would otherwise meet in the run directory. Low-conflict, one bullet. |

`src/arctic_qa/screening.py` is in the owned set and needed no change. It holds the legacy deterministic geography screen, which is not part of the Gemini eligibility contract and is not named by audit 4.7.

## 7. Contract versions

| Version | Before | After |
|---|---|---|
| Eligibility prompt | `config/gemini-eligibility-prompt-v7.txt` | `config/gemini-eligibility-prompt-v8.txt` |
| Eligibility response schema | `schemas/gemini-eligibility.v3.schema.json` (`eligibility-response-v3`) | `schemas/gemini-eligibility.v4.schema.json` (`eligibility-response-v4`) |
| Geography re-screen prompt | `config/gemini-eligibility-geography-rescreen-v1.txt` | `config/gemini-eligibility-geography-rescreen-v2.txt` |

No sibling's contract version was bumped. `config/gemini-eligibility-v1.json` is unchanged, so no chained ledger price transition is needed; the price config carries no prompt or schema reference.
Every earlier contract keeps its reader: v1, v2 and v3 responses validate exactly as they did at `bd2fb22`.

## 8. Deferred items, with owner

| Item | Owner | Why |
|---|---|---|
| Stage findings E4 and E5: `_context_only_span_is_usable` locator redaction, and the separable-component phrase filter moved to a dimension test in `_eligible_generation_scope`. | `arctic-ch3-writer-context-r1` | Those functions live in `generation.py` and belong to the writer-context slice. E5's fix reads the `dimension` key this slice now writes. Until E4 and E5 land, the extra study-setting spans this slice supplies are still deleted before the writer sees them, so the yield gain is not realized. |
| Grouping the rendered context spans by dimension in `_context_only_source`. | `arctic-ch3-writer-context-r1` | Same file, same slice. The forwarded record shape is documented in `docs/BENCHMARK_INPUT_CONTRACT.md`. |
| Routing behaviour for `eligible_arctic_scope_dimension_unsupported` and `eligible_arctic_scope_phrase_not_specific` beyond the registration. | `arctic-ch3-routing-r1` | The routing slice owns the logic. Neither code can reach a candidate-level rung today. |
| A live calibration of prompt v8 and re-screen prompt v2 against must_fail controls. | The captain, then the integration crew | No paid provider call was permitted in this slice. The audit's rule is that no judge prompt ships without a live calibration run; the eligibility classifier is not a judge, but the re-screen changes what enters the corpus, so a small measured run before the next production run is the safer order. |
| Measuring the two-pass deferral rate and span quality over one batch. | Phase E measurement | The shadow record is written on every job row and is booked at USD 0. The audit defers the decision until the numbers exist. |
| Re-running the phase E measures (candidates with zero context spans, families with no date, screening errors, wrongly withheld papers). | `arctic-ch3-integration-r1`, then a captain-approved run | This slice moves the screening-error measure only. The others need the writer-context slice and a real run. |
