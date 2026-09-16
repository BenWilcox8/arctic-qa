# Gemini Batch continuation

This module continues the scientific pipeline with the Gemini Batch API.
It reuses the current prompts, schemas, deterministic validators, database, accepted export, and publication exporter.
Preparation makes no network call and does not change the shared paid-call ledger.

The commands of this document write path variables such as `$ARCTIC_QA_DATA_ROOT`.
The "Environment variables" section of `docs/REPRODUCTION.md` gives their values.

The batch campaign has a separate USD 25 allocation.
The first USD 50 live campaign keeps its own cumulative ledger and liabilities.
The root operator must coordinate the shared ledger before the first submission.

## Price and model evidence

Most requests use `gemini-3.8-flash`.
Google lists Batch API support on the [model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash).
Google lists the Batch API request format on the [Batch API page](https://ai.google.dev/gemini-api/docs/batch-api).

Only a deterministic answer mismatch prepares an answer-judge request.
That request uses `gemini-3.1-flash-lite`.
Google lists its limits and Batch API support on the [Flash-Lite model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite).

The [official pricing page](https://ai.google.dev/gemini-api/docs/pricing) gives these rates through December 31, 2026:

- Input tokens cost USD 0.375 per million tokens.
- Output and thinking tokens cost USD 1.875 per million tokens.

Flash-Lite judge requests use these batch rates:

- Input tokens cost USD 0.125 per million tokens.
- Output tokens cost USD 0.75 per million tokens.

The [thinking documentation](https://ai.google.dev/gemini-api/docs/generate-content/thinking) defines minimal thinking for Gemini 3 models.

The module accepts no tools, caching, media output, or ancillary service charge.
It does not assume that ancillary charges use the batch discount.
Each round contains requests for one registered model.
The round manifest records that model and its exact batch price.

## Durable files

Set `BATCH_STATE` to one private directory on the mounted data volume.
Use this same directory for every batch round and process restart.

The directory contains these files:

- `state.json` contains cumulative jobs, costs, reservations, missing results, and errors.
- `requests/` contains one immutable mapping for each exact request.
- `rounds/ROUND_ID/requests.jsonl` contains the exact keyed provider input.
- `rounds/ROUND_ID/manifest.json` contains ordered paper identities and the cost preview.
- `raw/` contains exact job responses and result files.
- `receipts/` contains one settled response for each completed request key.

Each request key binds the run, stage, paper, family, source hash, model, and exact request body.
Each private mapping also records the attempt, prompt hash, configuration hash, and conservative token bounds.

## Select the ranked continuation

Wait for an exact paused or terminal progress record and settled accounting.
Use the currently approved scientific eligibility policy for the new campaign.
Then create a final continuation plan from the ranked 800-paper input.

The selector reads the campaign database, eligibility jobs, shared ledger, and progress file.
It excludes each paper that has a disposition or paid-call receipt.
It records each excluded paper, its ranked position, disposition, and receipt keys.
It does not copy source files or change production data.

```bash
nix develop --offline -c bash -lc '
PYTHONPATH=src python -m arctic_qa.gemini_batch select-continuation \
  --access-run-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/production-campaign-r1/quality-order-r1/materialized-top-800 \
  --db $ARCTIC_QA_DATA_ROOT/arctic-qa/state.sqlite3 \
  --campaign-id arctic-qa-production-campaign-001 \
  --prior-run-id first-production-6dc430d-live-rerun-r8 \
  --eligibility-run-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/gemini-eligibility-r1/first-production-6dc430d-live-rerun-r8 \
  --shared-ledger-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --production-progress-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/progress.json \
  --output-file "$BATCH_STATE/final-continuation-plan.json" \
  --require-stopped
'
```

The selector stops if the campaign is active or the ledger is not settled.
The remaining selection keeps the original ranked positions and order.
The remaining count is dynamic and cannot be more than 800.

## Prepare the first round

Use the final continuation plan from the preceding command.

```bash
nix develop --offline -c bash -lc '
PYTHONPATH=src python -m arctic_qa.gemini_batch prepare \
  --state-dir "$BATCH_STATE" \
  --db $ARCTIC_QA_DATA_ROOT/arctic-qa/state.sqlite3 \
  --namespace $ARCTIC_QA_DATA_ROOT/arctic-qa \
  --run-id FUTURE_BATCH_INVOCATION_ID \
  --campaign-id FUTURE_BATCH_CAMPAIGN_ID \
  --access-run-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/production-campaign-r1/quality-order-r1/materialized-top-800 \
  --continuation-plan "$BATCH_STATE/final-continuation-plan.json" \
  --eligibility-run-dir "$BATCH_STATE/eligibility" \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v6.txt \
  --eligibility-schema-file schemas/gemini-eligibility.v3.schema.json \
  --eligibility-policy-file $ARCTIC_QA_DATA_ROOT/arctic-qa/corpus-search-r1/protocol/protocol-v3.json \
  --price-config-file config/gemini-eligibility-v1.json \
  --shared-ledger-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --batch-allocation-usd 25
'
```

Use `--max-papers NUMBER` only for an explicit smaller prefix.
The manifest records this count and preserves the ranked order.

Read the returned `manifest_path`.
Review its ordered identities, stage counts, request count, request-file hash, and reserved cost.
The request file remains private because it contains source text and model instructions.

Dependent stages require separate rounds.
One batch job has a target turnaround of 24 hours.
Therefore, the complete pipeline has no one-day completion promise.

The batch allocation includes every prepared request in the campaign.
Each request reserves input tokens plus its maximum output token limit.
The output limit includes thinking tokens.
The shared ledger amount appears in each budget preview, but it does not reduce the separate USD 25 batch allocation.
Submission permits only reviewed no-replay recovery holds after the producer stops.
Those holds remain fully reserved and count against every cap.
Unreviewed submitted or ambiguous requests, integrity halts, and in-flight requests still stop submission.

### Archived provisional preview

The following preview is retained as historical evidence only.
It is not an active campaign input and cannot be submitted.
The new USD 25 campaign must use a fresh final continuation plan.
This archived preview predates the current live r8 invocation.
The selector marked this snapshot as provisional because synchronous production is still active.

Snapshot `r2` excludes 78 processed or touched papers and retains 722 untouched papers.
The first remaining paper is `10.1017/cft.2025.7` at ranked position 79.
The source hash is `5df97a41ecef9fe773578dd53895d90c68f032a662dc83fec984325b926fdfe2`.
The eligibility round has these values:

- Round ID: `batch-round-fac21313d558314932bc1b88422928eb`
- Request count: `1`
- Reserved cost: USD `0.08677125`
- Shared ledger use at preparation: USD `14.974336`
- Projected cumulative use: USD `15.06110725`
- Batch allocation ceiling: USD `25`
- Live call made: `false`
- Submission enabled: `false`

The private snapshot is in `streaming-dataset-r1/private/gemini-batch-continuation-r1/provisional-ranked800-snapshot-r2.json`.
The private round manifest is in `preview-r2-batch-state/rounds/batch-round-fac21313d558314932bc1b88422928eb/manifest.json`.

The preview files are on the mounted data volume.
They do not change the production database, source files, or shared ledger.
The provisional marker prevents submission.
Create a new final plan after the synchronous campaign stops.
If the geography policy changes, prepare new requests with the approved policy.
Do not activate this preview automatically.

## Authorize and submit one round

Wait until the current synchronous campaign stops.
Make sure that its ledger has zero in-flight requests and that every retained reservation or ambiguous charge has an exact reviewed no-replay recovery record.

After captain approval, create an authorization file for each exact round manifest.
The operator can create these files within the approved campaign budget, order, and model scope.

```json
{
  "schema": "arctic-gemini-batch-submit-authorization-v1",
  "round_manifest_sha256": "SHA256_OF_ROUND_MANIFEST",
  "shared_ledger_sha256": "CURRENT_SHARED_LEDGER_SHA256",
  "maximum_reserved_cost_usd": "EXACT_MANIFEST_RESERVED_COST",
  "authorized_by": "CAPTAIN_APPROVAL_REFERENCE"
}
```

Run this command only after the captain approves the campaign scope.
Stop for new approval only if the scope expands or a bound or ambiguity stops the campaign.

```bash
nix develop --offline -c bash -lc '
PYTHONPATH=src python -m arctic_qa.gemini_batch submit \
  --state-dir "$BATCH_STATE" \
  --manifest "$BATCH_STATE/rounds/ROUND_ID/manifest.json" \
  --authorization /PRIVATE/ROUND_ID.authorization.json \
  --credential-file $ARCTIC_QA_CONFIG_DIR/gemini-api-key \
  --price-config-file config/gemini-eligibility-v1.json \
  --shared-ledger-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --batch-allocation-usd 25
'
```

The submit command reserves every request before it contacts Google.
It counts all prior synchronous spend and all batch jobs in `state.json`.
It also enforces the per-request and per-paper cost limits.

The first submission creates an immutable exclusive-batch marker beside the shared ledger.
The synchronous broker enforces this marker under its existing operation lock.
Thus, a synchronous request cannot ignore active batch liability.

The marker remains until a reviewed accounting consolidation replaces batch mode.
The module has no automatic command to remove it.

If job creation has an uncertain result, do not run `submit` again.
The state keeps all reservations and records the uncertain operation.
Find the existing provider job through an independent operator comparison.
Then bind its identity without a submission:

```bash
PYTHONPATH=src python -m arctic_qa.gemini_batch reconcile \
  --state-dir "$BATCH_STATE" \
  --round-id ROUND_ID \
  --job-name batches/PROVIDER_JOB_ID \
  --provider-file-name files/UPLOADED_REQUEST_FILE \
  --price-config-file config/gemini-eligibility-v1.json \
  --shared-ledger-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json
```

## Get and ingest results

Run the status command after Google creates the job.
The command saves the exact status response.
It downloads the result file only after a successful terminal state supplies a file name.

```bash
PYTHONPATH=src python -m arctic_qa.gemini_batch status \
  --state-dir "$BATCH_STATE" \
  --round-id ROUND_ID \
  --credential-file $ARCTIC_QA_CONFIG_DIR/gemini-api-key \
  --price-config-file config/gemini-eligibility-v1.json \
  --shared-ledger-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json
```

Then ingest the downloaded JSONL file:

```bash
PYTHONPATH=src python -m arctic_qa.gemini_batch ingest \
  --state-dir "$BATCH_STATE" \
  --round-id ROUND_ID \
  --results-file "$BATCH_STATE/raw/ROUND_ID.download.jsonl" \
  --price-config-file config/gemini-eligibility-v1.json \
  --shared-ledger-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json
```

Ingestion maps out-of-order results by request key.
Repeated ingestion of the same exact response does not add cost.
Unexpected or duplicate keys stop ingestion.
Missing results and provider errors retain their conservative liability.
Cancellation does not release a reservation without final settlement evidence.

Run `resume` with the same arguments as `prepare`.
The pipeline validates actual upstream results before it prepares the next eligible stage.
The final continuation plan excludes all processed or touched ranked papers at the checkpoint.
An exact request key that exists in the shared ledger causes preparation to stop.

## Export accepted data

The export operation uses the same accepted-export function as synchronous mode.
It can also create the reviewer, benchmark, scoring, CSV, and prompt companion package.

```bash
PYTHONPATH=src python -m arctic_qa.gemini_batch export \
  --db $ARCTIC_QA_DATA_ROOT/arctic-qa/state.sqlite3 \
  --namespace $ARCTIC_QA_DATA_ROOT/arctic-qa \
  --campaign-id FUTURE_BATCH_CAMPAIGN_ID \
  --publication-output-dir /PRIVATE/PUBLICATION_PACKAGE \
  --prompt-template config/gemini-eligibility-prompt-v6.txt
```

An empty accepted export is an honest result.
Do not copy unaccepted candidates into an accepted file.
Do not report trials or machine acceptance as an unbiased scientific-validity estimate.

## Offline validation

Run the focused offline tests:

```bash
nix develop --offline -c bash -lc 'PYTHONPATH=src pytest -q tests/test_gemini_batch.py tests/test_model_broker.py tests/test_publication_export.py'
```

The staged test uses the real prompt renderers and scientific validators.
It prepares ten requests across seven dependent stages.
It ingests shuffled keyed responses and writes one accepted export.
It also covers restart accounting, missing results, provider errors, duplicate ingestion, and uncertain submission behavior.
