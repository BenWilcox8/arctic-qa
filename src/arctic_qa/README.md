# Python package

This directory contains the `arctic_qa` package.
The package has no third-party Python dependency.
External programs provide PDF text extraction and the optional subscription-model transports.

The main command is `python -m arctic_qa` or the installed `arctic-qa` script.
Run `python -m arctic_qa --help` to see the command groups.
Read [the CLI walkthrough](../../docs/CLI_WALKTHROUGH.md) before you operate a live stage.

## Main data flow

1. Discovery stores source metadata in SQLite and writes content-addressed manifests.
2. Screening and access stages write bounded run directories with manifests, progress, receipts, and overlays.
3. Extraction stores ordered sections and chunks from each original article.
4. Streaming joins eligibility, generation, validation, routing, and export for one paper family at a time.
5. The broker writes an immutable receipt for every paid request.
6. Export and publication tools turn accepted database rows into review and release files.
7. Abstention modules freeze an evaluation set, run model trials, and write scores.

Runtime data lives under the namespace that `paths.py` resolves.
Package modules do not write research records into this checkout.

## Entry points and shared support

### `__init__.py`

This module identifies the package and exposes `__version__`.
It reads and writes no external data.

### `__main__.py`

This module is the `python -m arctic_qa` entry point.
It calls `cli.main()` and returns that command status without direct file access.

### `cli.py`

This module builds the main command-line parser with `parser()` and dispatches commands through `main()`.
Its handlers call the pipeline modules, then print human-readable or JSON results.

### `paths.py`

This module resolves `ARCTIC_QA_DATA_ROOT`, `ARCTIC_QA_CONFIG_DIR`, and the default credential file.
`DataPaths` creates named paths under the `arctic-qa` namespace and checks the built-in mount rule.

### `errors.py`

This module defines the public error classes and run-stop marker helpers.
Other modules use these classes to separate paper-level faults from conditions that stop a run.

### `util.py`

This module provides stable identifiers, SHA-256 hashes, canonical JSON, redaction, normalization, and atomic file writes.
Its write helpers create a complete file or leave the previous file unchanged.

## Corpus discovery, access, and extraction

### `discovery.py`

This module queries Crossref and OpenAlex or replays recorded discovery pages.
`ingest_pages()` normalizes metadata into the state database, and `manual_record()` builds one local source record.

### `metadata_prefilter.py`

This module applies a fixed metadata-only policy to an existing discovery ledger.
`run_metadata_prefilter()` writes a manifest, batched decisions, a review queue, progress, and hashes in its run directory.

### `source_pass.py`

This module operates one bounded screening pass over the fixed metadata queue.
`prepare_source_pass()` freezes inputs, `run_source_pass()` gathers public evidence, and `apply_source_decisions()` writes the reviewed overlay.

### `access_readiness.py`

This module checks whether selected sources have usable public full text.
It reads the screening run and access policy, then writes per-source artifacts, receipts, progress, and an access overlay.

### `storage.py`

This module retrieves or stores an original source object after URL and size checks.
`fetch_source()` and `store_original()` write content-addressed files and update the matching database source record.

### `extraction.py`

This module extracts PDF, XML, or HTML originals into ordered sections and chunks.
`extract_source()` reads a stored object, writes extraction files, and registers their hashes and rows in SQLite.

### `pdf_layout.py`

This module reads `pdftotext -bbox-layout` output and reconstructs page reading order.
`extract_layout()` runs the external extractor, while `extract_blocks()` converts its XML into in-memory text blocks.

### `text_structure.py`

This module removes repeated page heads and divides ordered blocks into sections and sentence-complete chunks.
Its functions are deterministic and write no files.

### `screening.py`

This module applies the deterministic Arctic geography screen.
`decide()` reads evidence in memory, and `screen_source()` reads chunks and writes the resulting decision to SQLite.

### `chapter2_corpus.py`

This module rebuilds and freezes the chapter 2 corpus in a content-addressed order.
Its entry points read stored originals and write extraction records, a freeze receipt, manifests, and stream input files.

### `chapter2_replay.py`

This module replays chapter 2 candidate bundles against the current deterministic gates.
`replay_candidate()` and `replay_chapter2_gates()` read recorded bundles and return comparison records without paid calls.

### `extraction_quality.py`

This module measures text and chunk quality for old and current extraction formats.
`quality_report()` reads extracted rows and returns counts, medians, and warning summaries without changing the corpus.

### `manifests.py`

This module writes the source manifest for one database snapshot.
`write_source_manifest()` serializes ordered source records and their content hashes to JSON Lines.

## Eligibility, generation, and validation

### `gemini_eligibility.py`

This module owns the five-criterion Gemini eligibility contract and its bounded repair and geography re-screen paths.
`run_gemini_eligibility()` reads extracted spans and configuration, then writes jobs, receipts, budgets, progress, and decisions in its run directory.

### `generation.py`

This module owns finding selection, question writing, judge calls, distractor construction, and option repair.
`generate_candidate()` reads eligible source spans and writes candidate, call, finding-bank, distractor, and gate records through SQLite and the provider.

### `validation.py`

This module contains deterministic source, context, numeric, standalone, and option-set gates.
`validate_candidate()` reads the candidate and source evidence, then writes a `ValidationResult` and event records through the caller.

### `context_projection.py`

This module removes source locators and keeps only model-visible context text.
Its pure functions return projected strings and write nothing.

### `distractor_order.py`

This module derives and validates the stable random order of accepted distractors.
Its functions read candidate values in memory and return order records without file access.

### `quality_order.py`

This module assigns a deterministic review order from recorded quality features.
`produce_quality_order()` reads an input JSON file and writes a ranked JSON result.

### `geography_correction.py`

This module converts a reviewed geography correction report into a signed overlay shape.
`write_geography_correction_overlay()` reads the report and writes the overlay JSON file.

### `rerun_selection.py`

This module creates an immutable rerun-first selection for a production release.
`build_rerun_selection()` reads candidate and receipt history, then writes selection, manifest, and frozen access-run files.

## Paid-call control and model transports

### `model_roles.py`

This module loads the role contract and resolves each pipeline role to a model profile.
It validates phase restrictions and role separation without making provider calls.

### `providers.py`

This module defines the common construction-provider interface and fake, replay, Claude, and Gemini implementations.
`call_provider()` records call state in SQLite, while the live providers exchange JSON with their configured transport.

### `model_broker.py`

This module is the shared paid-call ledger and admission controller for Gemini requests.
`SharedGeminiBroker` validates gates and budgets, reserves a request, writes its receipt, settles usage, and recovers orphaned reservations.

### `broker_provider.py`

This module adapts `SharedGeminiBroker` to the construction-provider interface.
`BrokerProvider` sends a validated payload through the broker and returns the stored provider result to generation code.

### `gemini_batch.py`

This module runs the bounded Gemini batch path against the shared ledger.
Its commands prepare batches, submit requests, poll status, ingest results, and store batch progress and receipts.

### `pipeline_trace.py`

This module builds a readable trace that joins candidate events with paid-call receipts.
`record_model_request_trace()` and `PipelineTraceStore` write trace records for the viewer and audits.

## Streaming run, persistence, and export

### `db.py`

This module owns the SQLite schema, migrations, transactions, and typed query helpers.
`Database` reads and writes the resumable state file under the configured data root.

### `streaming.py`

This module is the chapter 3 producer and its main entry point is `run_stream()`.
It replays completed work, evaluates eligibility, generates candidates, contains paper-level faults, updates progress, and exports accepted items.

### `exporting.py`

This module exports one accepted run from SQLite.
`export_run()` writes multiple-choice, gold-absent, and short-answer JSON Lines files with source evidence and option order.

### `publication_export.py`

This module builds the read-only publication package and refreshes its live snapshot.
It reads accepted candidates, receipts, and validation events, then writes public manifests, reviewer tables, prompts, and trace files.

### `quality_summary.py`

This module summarizes a publication export with source coverage, quality checks, configuration, and spend.
`build_summary()` returns structured data, while `main()` writes or prints the JSON and Markdown forms.

### `full_run_plan.py`

This module builds a no-call plan for a future production run.
It reads policies, the access run, and ledger totals, then writes a frozen manifest draft and plan files when requested.

## Abstention evaluation

### `abstention_cli.py`

This module adds the `abstention-eval` command and dispatches its build, render, run, watch, score, gate, and status actions.
It reads command inputs and delegates all file writes and provider calls to the evaluation modules.

### `abstention_set.py`

This module freezes accepted candidates into one evaluation population.
`build_eval_set()` reads SQLite, and `write_eval_set()` writes `items.jsonl` with a hashed `manifest.json`.

### `abstention_render.py`

This module creates the exact prompt, displayed options, seeded order, and letter map for one trial.
`render_trial()`, `parse_letter()`, and `classify()` are pure functions with no external I/O.

### `abstention_providers.py`

This module defines the evaluation-provider interface, the Gemini broker adapter, and the scripted free transport.
It builds request payloads, reads broker receipts, and can query the Gemini model list.

### `abstention_subscription.py`

This module calls Claude Code or Codex in a new isolated subprocess for each evaluation trial.
It validates the subscription gate and registry, then writes immutable request receipts and ledger rows with reported token usage.

### `abstention_run.py`

This module drives one serial evaluation run and resumes it from existing responses and receipts.
It writes the run manifest, trials, responses, summary, private dry-run ledger, and optional gate template.

### `abstention_plan.py`

This module runs every vendor in an evaluation plan concurrently for one item set.
`run_plan()` writes a plan manifest and summary plus a complete serial-run directory for each vendor.

### `abstention_watch.py`

This module watches a production database and evaluates each newly accepted item.
`watch()` reads the authorization and pause controls, writes vendor results and cost rows, and resumes eligible vendors after released charge ambiguity.

### `abstention_score.py`

This module computes the N0 to N5 counts, abstention metrics, random baseline, and item-level bootstrap intervals.
`score_run()` reads response rows and writes JSON, CSV, and LaTeX score artifacts.

### `abstention_cost.py`

This module joins construction cost, Gemini evaluation cost, and subscription token usage for each question.
`CostJournal` writes append-only rows, and `summarize_journal()` returns cumulative cost and model metrics.

## Paper data and analysis

### `paper_release.py`

This module builds `data/arcticqa-v1/` from the frozen evaluation snapshot.
`build()` reads the frozen sets, responses and state database, re-renders every call, and writes the items, conditions, responses and manifest.

### `paper_tables.py`

This module computes the tables of the paper from `data/arcticqa-v1/responses.jsonl` alone.
`compute()` returns the rates, shifts, bootstrap intervals, sign-flip tests, Holm values and metrics, and `main()` writes them under `results/`.

## Monitoring and calibration

### `benchmark_guard.py`

This module evaluates benchmark cost and subscription-quota rules without stopping a process.
`BenchmarkGuard` reads journals, ledgers, quota reports, and memory, then writes guard events, memory, and the model pause file.

### `corpus_viewer.py`

This module serves the read-only corpus and benchmark monitor.
`serve_corpus_viewer()` reads run artifacts and `corpus_viewer.html`, then exposes a local HTTP interface without changing run data.

### `standalone_calibration.py`

This module records or replays the source-blind judge against labeled calibration rows.
Recording writes a paid cassette, while `evaluate_cassette()` reads a cassette and reports whether the release rule passes.

## Non-Python package files

| Path | Purpose |
| --- | --- |
| `corpus_viewer.html` | The browser interface that `corpus_viewer.py` serves. |
| [`data/`](data/README.md) | Static geography data that ships with the package. |

## Where outputs go

`paths.DataPaths` keeps state, originals, manifests, extractions, runs, and exports under one namespace.
The default namespace is `$ARCTIC_QA_DATA_ROOT/arctic-qa/`.
Individual bounded runs also write manifests and receipts inside the output directory supplied to their command.
