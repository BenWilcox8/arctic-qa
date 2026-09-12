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
  --host 127.0.0.1 \
  --port 8787 \
  --process-stale-after-seconds 300
```

Stop the foreground process with `Ctrl-C`.

The server exposes only `/`, `/api/state`, `/api/candidates`, and `/healthz`.
It does not expose source files, PDFs, credentials, directories, or arbitrary paths.

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

## Stage completion

Metadata discovery is complete when every frozen query has a terminal no-token receipt.
The immutable deduplicated ledger must also exist.

The eligible corpus is complete after every discovery record has one documented disposition.
The dispositions are accepted, excluded, or unresolved under a declared stop rule.
A final manifest must hash the protocol, discovery ledger, screening ledger, and accepted corpus.

Missing access remains an unresolved access state.
It does not become a scientific exclusion.
