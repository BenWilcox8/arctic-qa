# Corpus stage viewer

The corpus stage viewer shows the selected discovery run and its latest screening overlay.
It is read-only.
It does not start, stop, or advance corpus processing.

The viewer builds a disposable SQLite query cache in the runtime directory.
The immutable discovery ledger and the explicit screening overlay remain authoritative.
The viewer rebuilds the cache only when an input file changes.

## Start the viewer

Run the viewer with explicit artifact and runtime paths:

```bash
PYTHONPATH=src python -m arctic_qa corpus-view \
  --corpus-root /mnt/crdata/research-abstention/arctic-qa/corpus-search-r1 \
  --run-id 20260911T232247Z \
  --runtime-dir /private/runtime/path \
  --progress-file /private/runtime/path/corpus-progress-v1.json \
  --zotero-receipts-dir /private/zotero/receipts \
  --metadata-run-dir /private/metadata-prefilter/run-RUN_ID \
  --source-run-dir /private/source-screening/run-RUN_ID \
  --pipeline-namespace /private/arctic-qa-data \
  --pipeline-db-file /private/arctic-qa-data/state.sqlite3 \
  --pipeline-receipts-dir /private/arctic-qa-data/streaming-dataset-r1/model-receipts \
  --pipeline-eligibility-root /private/arctic-qa-data/gemini-eligibility-r1/run-RUN_ID \
  --live-dataset-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/live-publication \
  --project-overview-file /private/status/project-progress-overview-v1.json \
  --research-timeline-file /private/status/research-fleet-timeline-v1.json \
  --host 127.0.0.1 \
  --port 8787 \
  --process-stale-after-seconds 300
```

Stop the foreground process with `Ctrl-C`.

The server exposes only `/`, the named APIs, `/healthz`, and fixed download routes.
It does not expose source files, PDFs, credentials, directories, or arbitrary paths.

The live dataset browser uses `/api/live-dataset`.
It shows joined benchmark and reviewer rows from the hash-checked current snapshot.
Search covers questions, context, options, answers, DOI values, and titles.
The API permits page sizes of 10, 25, 50, and 100.

The fixed live downloads are `/downloads/live-dataset/benchmark` and `/downloads/live-dataset/reviewer`.
No route accepts a file path.

## Per-paper pipeline inspector

The optional pipeline trace adapter reads retained artifacts from explicit roots.
It does not make model requests or change pipeline state.

Use `--pipeline-namespace` to enable the inspector.
Use `--pipeline-db-file` and `--pipeline-receipts-dir` to select the database and retained model receipts.
Repeat `--pipeline-eligibility-root` for each selected eligibility receipt root.
The configured shared ledger supplies the accounting records.

The paper list is bounded to 100 rows per request.
Filters can select a title, DOI, paper ID, run, final state, or current stage.
The server uses opaque paper and stage keys.
It does not accept file paths in these API requests.

Select a paper to load its run history and small structured records.
Select one stage to load its retained request context, model response, parsed result, checks, timing, cost, and provenance.
The adapter labels unavailable and unretained payloads explicitly.
It does not present reconstructed context as a verbatim submitted request.

The retained-paper list shows when each paper entered its current state.
This value comes from the matching candidate, eligibility, or request-state transition.
The viewer shows unknown when no matching event time exists.
The readable value uses UTC, and its tooltip contains the exact retained timestamp.

The private inspector can show retained source context that was supplied to a model.
It removes credentials, authorization headers, provider thought signatures, and private paths.
The page inserts all source and model text through text-only DOM operations.
It never interprets retained text as HTML.
The readable view removes extraction indentation and joins hard-wrapped prose for display.
The raw JSON controls keep the exact retained text.
The readable view combines evidence spans only when their source locator matches and their recorded offsets are adjacent.

The selected paper and stage stay in the page URL.
Open stage panels stay in browser session storage.
The 15-second refresh checks a bounded list and preserves the current selection.
The refresh does not start work and does not imply an active run.

For a live deployment, configure the producer's canonical database, progress record, ledger, and receipt directories directly.
Do not use a manually copied progress snapshot as the continuing data source.
Keep curated historical timeline records separate from these live producer inputs.

For the integrated streaming view, also pass these optional files:

- `--shared-ledger-file` selects the one `shared-paid-call-ledger-v1` ledger.
- `--streaming-budget-policy-file` selects the frozen budget grant.
- `--streaming-progress-file` selects a small `streaming-dataset-progress-v1` record.
- `--dataset-metadata-file` selects a validated export manifest for download.
- `--production-plan-file` selects the exact plan for the current production campaign.
- `--publication-package-dir` selects one manifest-backed trial publication package.
- `--project-overview-file` selects the maintained editorial project status.

The streaming progress record can contain at most 100 recent paper rows.

The integrated view shows `excluded` and `unresolved` as separate counts.
`excluded` means that deterministic eligibility validation accepted an exclusion.
`unresolved` means that no valid eligibility decision exists.
The view shows downstream QA rejection as `generation_rejected`.
For an old combined count, the view separates states only when all rows are present.
Otherwise, it shows `legacy_rejected_or_unresolved` and does not guess the split.

Each row can show its paper ID, title, current stage, final state, and final reason.

Progress counts describe one incremental invocation.
They can reset when the same run resumes.
The shared ledger supplies cumulative accepted-QA, paper, submission, and cost totals.

The broker writes an adjacent `shared-gemini-broker-status-v2` record.

The page reads model costs and tokens only from this invariant-checked record.

The page verifies the ledger hash and budget-policy hash before it shows this state.

The progress record must contain `broker_status_sha256` and `budget_policy_sha256` when a ledger exists.

If dataset metadata exists, progress must also contain `dataset_metadata_sha256`.

The dataset metadata run ID must equal the streaming progress run ID.

The page shows each money limit, count limit, used value, and remaining value.

It also shows per-paper remaining cost in the paper table.

An absent or inconsistent custody record appears as an error.

It never serves provider receipts, source text, PDFs, or credentials.

The production plan adds the campaign ID, invocation run ID, phase, and budget scopes.
The viewer labels the current invocation state from the canonical progress record.
It does not treat a planning record as proof that a campaign started.

The optional publication package is a trial example, not a production result.
Its manifest must describe one question with two variants.
The viewer verifies every exposed file hash before it shows download links.
It exposes only the manifest, six data files, and two historical renderer companions.
It does not expose source custody copies or other package paths.

## Project progress overview

The top of the page contains two compact diagrams.
One diagram shows scientific dataset stages.
The other diagram shows supporting engineering stages.
The diagrams use text labels with green, yellow, and red status colors.
Select a node to open its explanation and next action.

The configured overview file must use `project-progress-overview-v1`.
It contains an `updated_at_utc` timestamp and two stage arrays.
Each stage has an ID, label, status, explanation, and next action.
The supported status values are `completed`, `in_progress`, and `not_finished`.
The file can also contain a summary, a scientific and engineering distinction, and short notes.

The pipeline owner must replace this file atomically after a reviewed status change.
The viewer never writes the file.
It does not expose the configured path.
It rejects files larger than 128 KiB.
It rejects malformed values and displays no inferred stage state.
It marks an old valid record stale with the normal artifact freshness threshold.

The editorial stage record does not contain live counters.
The viewer derives those counters from the existing discovery, access, streaming, and broker records.
This separation prevents an editorial update from overriding accounting or pipeline evidence.

## Research fleet timeline

The optional timeline file uses `research-fleet-timeline-v1`.
It contains a curated chronological history from timestamped operational evidence.
The page can filter entries by agent or crew and pipeline stage.

Each entry identifies its event kind, status, artifact state, version, and evidence reference.
The event kinds separate code changes, reviews, tests, live execution, outcomes, blockers, restarts, and monitoring.
The artifact state keeps committed or deployed work separate from work in progress.

The timeline owner must keep entries in chronological order.
The owner must label date precision and attribution limits in the entry text.
The coverage note must identify gaps in the bounded evidence search.
The viewer does not infer missing events or completion from the timeline.

The viewer accepts only HTTP or HTTPS evidence links.
It does not serve private reports, gate files, credentials, receipts, or arbitrary paths.
It rejects malformed files and files larger than 256 KiB.

## Progress record

The optional progress file uses `schemas/corpus-progress.v1.schema.json`.
The pipeline owner must replace this file atomically after a real state change.
The viewer never writes the progress file.

The record states are `not_running`, `running`, `paused`, `error`, and `completed`.
The record timestamp shows when the pipeline owner observed the state.
The viewer reports stale telemetry as unknown process state.
The viewer also keeps absent telemetry separate from an observed `not_running` state.
The process liveness window is 300 seconds by default.
It is independent of the longer historical artifact freshness window.

The optional metadata run directory contains producer progress, dispositions, and a final receipt.
The viewer uses the completed receipt as the authority for completion.
It keeps metadata disposition separate from source eligibility.

The optional source run directory contains live progress and versioned completion records.
The viewer follows hash-checked pointers to the current source overlay and receipt.
It refreshes its cache when a pointer or progress record changes.
It does not expose stored source files or extracted text.
It reports full-text retrieval and semantic review as separate counts.
Prepared, paused, and completed states clearly report that no process is running.

## Stage completion

Metadata discovery is complete when every frozen query has a terminal no-token receipt.
The immutable deduplicated ledger must also exist.

The eligible corpus is complete after every discovery record has one documented disposition.
The dispositions are accepted, excluded, or unresolved under a declared stop rule.
A final manifest must hash the protocol, discovery ledger, screening ledger, and accepted corpus.

Missing access remains an unresolved access state.
It does not become a scientific exclusion.
