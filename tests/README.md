# Tests

This directory is the executable specification of ArcticQA.
The tests cover pure rules, stored-record contracts, command-line flows, free replays, and paid-call safety boundaries.

All normal tests are free.
Provider and network tests use fakes, recorded responses, or local servers.
Some tests use real rate-limit delays, so the complete suite takes about 20 minutes.

## Run the suite

Run the complete suite from the repository root.

```bash
nix develop -c bash -c 'PYTHONPATH=src pytest tests/ -q'
```

Run the linter and format check separately.

```bash
nix develop -c bash -c 'ruff check . && ruff format --check src tests'
```

The package is not installed inside the Nix shell.
Keep `PYTHONPATH=src` in each pytest command.

Tests that need a private evidence bundle or the real corpus skip when its environment variable is absent.
The [reproduction guide](../docs/REPRODUCTION.md#3-environment-variables) lists those variables.

## Test helpers

| File | Purpose |
| --- | --- |
| `cost_plan_harness.py` | A subprocess harness that records evaluation-plan concurrency and cost behavior. |
| `pdf_fixture.py` | Helpers that build small PDF and layout fixtures for extraction tests. |

## Abstention evaluation tests

| File | Contract under test |
| --- | --- |
| `test_abstention_broker.py` | Gemini evaluation admission, receipts, phase accounting, and stop conditions. |
| `test_abstention_plan.py` | Multi-vendor plans, concurrency, pauses, summaries, and resume behavior. |
| `test_abstention_render.py` | Prompt text, condition construction, seeded option order, parsing, and N0 to N5 classification. |
| `test_abstention_run.py` | The free end-to-end dry run, manifests, receipts, resume behavior, and score inputs. |
| `test_abstention_subscription.py` | Isolated Claude Code and Codex subprocesses, output parsing, registries, gates, and usage ledgers. |
| `test_abstention_watch.py` | Streaming evaluation, pending items, ceiling checks, pauses, vendor resume, and cost rows. |
| `test_ambiguous_continuation.py` | Reviewed continuation after an ambiguous charge, including the stable error message. |
| `test_live_benchmark_viewer.py` | The viewer projection of live evaluation state, costs, models, pauses, and outcomes. |

## Corpus, access, and extraction tests

| File | Contract under test |
| --- | --- |
| `test_access_readiness.py` | Bounded article access, safe URLs, redirects, receipts, resume behavior, and overlays. |
| `test_chapter2_corpus.py` | Chapter 2 re-extraction, content-addressed freeze, receipts, and streaming input. |
| `test_chapter2_integration.py` | The joined chapter 2 corpus, generation, validation, and export path. |
| `test_eligibility_ch2_replay.py` | Free replay of recorded chapter 2 eligibility decisions. |
| `test_metadata_prefilter.py` | Fixed metadata classification, batching, progress, review queues, and resume checks. |
| `test_source_pass.py` | Bounded source screening, public-source parsing, receipts, overlays, and reviewed decisions. |
| `test_writer_context_bundle.py` | The source bundle that the writer receives. |
| `test_writer_context_chapter3.py` | The chapter 3 writer context and its contract versions. |

## Eligibility tests

| File | Contract under test |
| --- | --- |
| `test_eligibility_contract_v8.py` | Prompt v8, schema v4, five criteria, and current status mapping. |
| `test_eligibility_geography_v7.py` | Geography evidence and bounded re-screen behavior. |
| `test_eligibility_request_constraints.py` | The restricted JSON Schema keyword set accepted by the live provider. |
| `test_eligibility_span_contract.py` | Span manifests, identifiers, evidence binding, and response validation. |
| `test_eligibility_span_filter.py` | Activity-span filtering and unverified non-Latin spans. |
| `test_eligibility_streaming_recovery.py` | Job ordering, repair attempts, recovery, and the final eligibility decision. |
| `test_gemini_eligibility.py` | Budgets, timeouts, provider responses, validation, persistence, and run manifests. |
| `test_geography_correction.py` | Reviewed correction overlays and their input bindings. |

## Generation and validation tests

| File | Contract under test |
| --- | --- |
| `test_answer_equivalence.py` | Numeric, categorical, directional, and text answer equivalence. |
| `test_audit_core_fixes.py` | Gate corrections that came from the chapter 2 audits. |
| `test_bounded_fallback.py` | The bounded alternative-finding and repair paths. |
| `test_ch3_judge_options.py` | The source-blind judge, satisfiability checks, option verification, and repair. |
| `test_ch3_routing.py` | Chapter 3 failure routing and the recorded chapter 2 replay. |
| `test_current_payload_validation.py` | Current candidate payloads against their schema and generation contract. |
| `test_distractor_validation.py` | Individual distractor rules, set rules, receipts, and option equivalence. |
| `test_evidence_combination.py` | Valid and invalid combinations of source evidence spans. |
| `test_finding_bank.py` | Ranked findings, bank persistence, admission, exclusion, and reuse. |
| `test_gate_contracts_r15.py` | Versioned r15 gate constants and response contracts. |
| `test_gate_corrections_ch3.py` | Chapter 3 corrections for standalone, context, reconstruction, and options. |
| `test_generation_scope_roles.py` | Scope, answer, and context roles in the generation payload. |
| `test_question_context.py` | The top-level question context and its source evidence. |
| `test_scope_layout.py` | Display-only scope projection and comparison layout. |
| `test_standalone_calibration.py` | Calibration-set loading, cassette binding, replay, and release rules. |
| `test_standalone_calibration_ch3_slice.py` | The labeled chapter 3 judge slice against the current gate. |

## Streaming and broker tests

| File | Contract under test |
| --- | --- |
| `test_broker_operation_lock_wait.py` | Wait behavior for ordinary calls and immediate refusal for reviewed operations. |
| `test_broker_provider.py` | The adapter from generation calls to shared-broker requests and results. |
| `test_ch3_candidate_fault_containment.py` | Paper-family containment for generation, routing, validation, and persistence faults. |
| `test_ch3_integration.py` | The free chapter 3 integration path across all pipeline slices. |
| `test_chapter3_production_run.py` | Registered policy transitions, live-run bindings, and production resume rules. |
| `test_count_error_retry.py` | Retry, receipt chaining, and paper-level containment for a transient free token-count failure. |
| `test_cost_call_plan.py` | Per-paper call counts, cost projection, and bounded option calls. |
| `test_http_rejection_settlement.py` | Settlement of provider HTTP rejections before generation. |
| `test_model_broker.py` | Budget, gate, receipt, reservation, recovery, concurrency, and settlement invariants. |
| `test_model_roles.py` | Role contracts, profile restrictions, and separation rules. |
| `test_phase_scoped_slots.py` | Separate construction and evaluation concurrency with a shared minute window. |
| `test_streaming.py` | Producer resume, eligibility, generation, routing, export, limits, and stop behavior. |

## Commands, persistence, and export tests

| File | Contract under test |
| --- | --- |
| `test_cli_integration.py` | Command parsing and the free synthetic end-to-end `smoke` run. |
| `test_db_migrate_idempotent.py` | Repeatable SQLite schema migration. |
| `test_full_run_plan.py` | Read-only planning, frozen input construction, and projected limits. |
| `test_gemini_batch.py` | Batch preparation, submission, polling, result ingestion, and shared-ledger safety. |
| `test_pipeline_trace.py` | Joins between candidates, events, model requests, and receipts. |
| `test_publication_export.py` | Public package contents, validation projection, reviewer rows, and live snapshots. |
| `test_quality_order.py` | Stable ranking from recorded quality features. |
| `test_quality_summary.py` | Publication coverage, quality counts, configuration, spend, and Markdown output. |
| `test_rerun_selection.py` | Immutable rerun selection, historical calls, and frozen access input. |

## Viewer and guard tests

| File | Contract under test |
| --- | --- |
| `test_benchmark_guard.py` | Cost rules, quota rules, attribution, pause selection, memory, and reports. |
| `test_corpus_viewer.py` | Safe read-only HTTP responses and artifact projection. |
| `test_project_progress_viewer.py` | Progress views across corpus and generation stages. |
| `test_research_timeline_viewer.py` | The chronological projection of research-stage events. |

## Evidence-dependent tests

The following tests use private, external evidence when the matching environment variable exists.
They skip in a normal public checkout.

| Environment variable | Tests that use it |
| --- | --- |
| `ARCTIC_CH2_EVIDENCE_DIR` | Chapter 2 replay and integration tests. |
| `ARCTIC_REAL_CORPUS_DIR` and `ARCTIC_REAL_CORPUS_RUN` | Viewer tests against the production corpus. |
| `ARCTIC_ZOTERO_RECEIPTS_DIR` | Custody checks for the source receipts. |

No skipped evidence test is required for the free `smoke` reproduction.
