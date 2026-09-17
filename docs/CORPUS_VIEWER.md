# Corpus stage viewer

The corpus stage viewer shows the selected discovery run and its latest screening overlay.
It is read-only.
It does not start, stop, or advance corpus processing.

The commands of this document write path variables such as `$ARCTIC_QA_DATA_ROOT`.
The "Environment variables" section of `docs/REPRODUCTION.md` gives their values.

The viewer builds a disposable SQLite query cache in the runtime directory.
The immutable discovery ledger and the explicit screening overlay remain authoritative.
The viewer rebuilds the cache only when an input file changes.

## Start the viewer

Run the viewer with explicit artifact and runtime paths:

```bash
PYTHONPATH=src python -m arctic_qa corpus-view \
  --corpus-root $ARCTIC_QA_DATA_ROOT/arctic-qa/corpus-search-r1 \
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
  --live-dataset-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/live-publication \
  --project-overview-file /private/status/project-progress-overview-v1.json \
  --research-timeline-file /private/status/research-fleet-timeline-v1.json \
  --benchmark-journal-dir /private/arctic-qa-data/abstention-eval/streaming-r2 \
  --benchmark-guard-state-file /private/arctic-qa-data/abstention-eval/guard-r1/guard-state.json \
  --host 127.0.0.1 \
  --port 8787 \
  --process-stale-after-seconds 300
```

Stop the foreground process with `Ctrl-C`.

## How the viewer answers a request

One background thread builds the four routes the page polls.
The routes are `/api/state`, `/api/live-benchmark`, `/api/live-papers` and the unfiltered `/api/pipeline-trace`.
A request copies out the bytes of the last cycle and answers in milliseconds.
`--refresh-interval-seconds` sets the cycle, 15 seconds by default, which is the cadence the page polls at.
The header `X-Snapshot-Age-Seconds` gives the age of the bytes that were served.
Each payload keeps its own `generated_at_utc`, which is the moment the reading was taken and not the moment it was served.
`/healthz` reads the same snapshot.

A failed cycle keeps the last good bytes and puts the failure in `X-Snapshot-Refresh-Error`.
The failure also goes to the standard output of the process.

A fixed pool of request threads serves every request.
`--worker-threads` sets its size, 8 by default.
The pool is fixed for memory, not only for load: a new thread gets a malloc arena of its own from the C library, about 64 MB, and those pages never go back.
A thread for each request walked the viewer to the arena limit and 3.8 GB in six hours on 2026-09-17, and it then accepted connections and returned empty responses.

A backlog longer than the pool gets HTTP 503 with a message.
Do not make an over-full backlog drop the connection instead: an accepted connection that answers nothing reads as a dead site, which is the failure this rule exists to prevent.

Do not move the work of a polled route back into its request handler.
Each of these routes reads a record of tens or hundreds of megabytes: the shared ledger, the query index, the state database.
`/api/state` answered in 6.4 seconds that way, so a page that polls every 15 seconds queued its own requests.

The server exposes only `/`, the named APIs, `/healthz`, and fixed download routes.
It does not expose source files, PDFs, credentials, directories, or arbitrary paths.

The live dataset browser uses `/api/live-dataset`.
It shows joined benchmark and reviewer rows from the hash-checked current snapshot.
Search covers questions, context, options, answers, DOI values, and titles.
The API permits page sizes of 10, 25, 50, and 100.

The fixed live downloads are `/downloads/live-dataset/benchmark` and `/downloads/live-dataset/reviewer`.
No route accepts a file path.

## Live benchmarking

The section "Live benchmarking" shows the streaming abstention evaluation.
It uses `/api/live-benchmark`.
The route builds the whole payload from the files and makes no paid call.
The background refresher builds it, as it builds every polled route.

### Every count is a count of questions

The evaluator appends one journal row for each pass over a question, and it passes again whenever a paused arm owes that question trials.
So the journal always holds more rows than questions.
Every count of this section reads the last row of each question, which carries that question's whole record.
The page states the source under the counts and reports the rows beside the questions.
A reader that counted rows told the page 286 questions on 2026-09-17 where the journal held 160.

A question is complete when its recorded trials reach its planned trials.
It is not complete because its row carries the `complete` flag: a row written while an arm was paused carries that flag and still holds part of its plan.
The flag stays readable as `row_complete_flag`.

`coverage` in the payload holds the counts:
questions with a response, questions with every response, questions part answered, questions stranded, questions held by a pause, questions revisited, questions reopened, the trials recorded against the trials planned, and the journal rows beside the questions.
A stranded question is one whose last row holds fewer responses than its plan and which no pause names.
Nothing takes such a question up on its own, because the evaluation policy forbids a retry of a trial that stopped.
Reopening one is an operator action.

Each arm carries `questions_with_a_response` and `questions_with_every_response` of its own.
An arm covers a question only when it recorded its whole share of that question's plan.
The share is the planned trials over the models of the plan, and the model count comes from the widest row of the journal.
It never comes from one row's own model count: a row names only the models that recorded something, so that put the share at 24 where it is 6.

Two options select its inputs.
`--benchmark-journal-dir` points at the work directory of the streaming evaluator.
The route reads `cost-journal.jsonl` and `watch-state.json` from it.
`--benchmark-guard-state-file` points at `guard-state.json` of the cost and quota guard.

The section shows the per-model table with the N1 to N5 counts and the seven paper metrics, the per-question cost rows, the Gemini spend against the USD 200 allocation with its extrapolation, the four guarded quota windows, the guard rules, and the evaluator watch state.
The "Evaluator state" block says whether the evaluator is polling, how many questions are in flight, how many question workers it runs, when its watch state last moved, and which arms it runs.
Without the guard state file the section still shows the journal, and marks the budget readings as not available.

`docs/BENCHMARK_GUARD.md` describes the guard, its rules, and how to operate it.

## Papers in analysis now

The section "Papers in analysis now" stands above the per-paper pipeline inspector.
It uses `/api/live-papers`.
The table refreshes every 15 seconds without a page reload, together with the rest of the page.
The route reads records only.
It never writes to the shared ledger, the state database, or a completion label.
It never touches the producer, a launcher, or the evaluator.

When no producer runs, the section says so above the table rather than showing an empty one.
It names the moment generation stopped, which is `producer.stopped_at_utc` of the payload, and the last message the producer wrote.
The producer writes its progress record for each finished paper, so the last record of a stopped run is the moment it stopped.

The upper table holds the papers the producer analyzes at this moment.
A paper is in that table when three conditions are true.
Its paid calls carry the invocation run id of the current producer.
One of those calls was submitted or completed inside the active window of 420 seconds.
The paper has no completion label of that run and no final state in the progress record.

Each row gives the paper title and identity.
It also gives the stage of the most recent call, the number of calls, and the money this run spent on them.
The last column gives the time the paper has been in analysis.
That time starts at the first call of the current burst of work.
A relaunched producer replays the receipts of a paper it visited before.
More than one window separates such a replay from live work, so the replayed calls stay out of the count.

The "Thread or slot" column stays empty.
The producer runs one thread per paper, but it writes no thread name and no slot number into any record.
The column takes a value when a producer record carries one.

The lower table holds the last 10 papers of the run that reached a final outcome, newest first.
An accepted paper shows its accepted question count.
A rejected paper shows its final reason code.
A screened-out paper, an unresolved paper and a paper that reached the per-paper cost cap show the same reason field.
The outcome comes from the completion label of the run when the paper has one.
It comes from the progress record when the paper has none.

The finish time of a paper is the time of its last paid call, not the time of its label.
A batch catch-up labels every finished paper of one run in one transaction.
Such a batch gives every label the same minute, and that minute holds no order.
A paper this run replayed and never called keeps the label time, because the label is its only timestamp.

The section names the producer state when no producer is running.
It shows the message of the progress record instead of an empty table.
A progress record that says running but is older than `--process-stale-after-seconds` is not a running producer.

One paid call of the run outranks that staleness rule.
The producer writes the progress record when it finishes a paper.
A run that skips its labelled papers and then works one slow paper leaves that record quiet.
A call of the run inside the window proves the producer is up.
Such a call carries the run id, so an evaluation call can never be read as producer work.
A record that reports an error or a stop is never overruled.
A call was in flight when the producer died, and the producer did die.

The route does not rebuild the query index.
A live producer writes its eligibility run directory on every paper, so the index fingerprint moves on every poll.
A rebuild of that index took about 50 seconds on 2026-09-16, and this section polls every 15 seconds.
The titles the section reads come from the immutable discovery ledger, which no overlay touches.
Keep every new poll of this page off `refresh` unless it needs an overlay.

Three inputs select the section.
`--shared-ledger-file` gives the paid calls.
`--streaming-progress-file` gives the producer state and the recent papers.
`--pipeline-db-file` gives the titles, the accepted question counts and the completion labels.
Without the ledger and the progress record the section says that no live paper can be named.

## Per-paper pipeline inspector

The optional pipeline trace adapter reads retained artifacts from explicit roots.
It does not make model requests or change pipeline state.

Use `--pipeline-namespace` to enable the inspector.
Use `--pipeline-db-file` and `--pipeline-receipts-dir` to select the database and retained model receipts.
Repeat `--pipeline-eligibility-root` for each selected eligibility receipt root.
The configured shared ledger supplies the accounting records.

Each cache of the adapter is filled by one thread at a time, and each is kept until one of its inputs moves.
The per-family records cost the whole state database to build, the receipts cost the whole receipts directory to read.
Two threads that missed together each built the records in full, which took a paper-detail request and the background refresher past 90 seconds where one build takes 25.

A receipt is immutable once written, so the adapter keeps the parsed event for each receipt and reads again only what the directory listing says moved.
Re-deriving all of them because one arrived cost 77 seconds against the 48,938 receipts of 2026-09-17.
The shared ledger has two live writers, so a receipt arrives every few seconds and almost every request paid it.
Keep this rule when you change the adapter; it is the rule the broker keeps for the same directory, in "The immutable-event proof, and what one read of it costs" of `docs/SHARED_MODEL_BROKER.md`.

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
The readable value uses `America/Chicago` and shows CST or CDT for the event date.
Its tooltip and data attribute contain the exact retained UTC timestamp.

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
