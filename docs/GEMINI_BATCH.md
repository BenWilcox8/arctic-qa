# Gemini Batch continuation

This module continues the scientific pipeline with the Gemini Batch API.
It reuses the current prompts, schemas, deterministic validators, database, accepted export, and publication exporter.
Preparation makes no network call and does not change the shared paid-call ledger.

Real batch submission is not active.
The captain must authorize one prepared round before an operator submits it.

## Price and model evidence

The configured model is `gemini-3.8-flash`.
Google lists Batch API support on the [model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash).
Google lists the Batch API request format on the [Batch API page](https://ai.google.dev/gemini-api/docs/batch-api).

The [official pricing page](https://ai.google.dev/gemini-api/docs/pricing) gives these rates through December 31, 2026:

- Input tokens cost USD 0.375 per million tokens.
- Output and thinking tokens cost USD 1.875 per million tokens.

The module accepts no tools, caching, media output, or ancillary service charge.
It does not assume that ancillary charges use the batch discount.
It rejects a model other than the configured model.

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

## Prepare the first round

Run this command from the repository root.
This example uses the frozen continuation manifest.
It contains 4,379 papers in source order after the completed 41-paper campaign prefix.
It does not select only successful trials.

```bash
nix develop --offline -c bash -lc '
PYTHONPATH=src python -m arctic_qa.gemini_batch prepare \
  --state-dir "$BATCH_STATE" \
  --db /mnt/crdata/research-abstention/arctic-qa/state.sqlite3 \
  --namespace /mnt/crdata/research-abstention/arctic-qa \
  --run-id FUTURE_BATCH_INVOCATION_ID \
  --campaign-id FUTURE_SCIENTIFIC_CAMPAIGN_ID \
  --access-run-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/trial-inputs/full-manifest-continuation-4420-minus41-r1 \
  --eligibility-run-dir /mnt/crdata/research-abstention/arctic-qa/gemini-eligibility-r1/FUTURE_SCIENTIFIC_CAMPAIGN_ID \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v4.txt \
  --eligibility-schema-file schemas/gemini-eligibility.v2.schema.json \
  --eligibility-policy-file /mnt/crdata/research-abstention/arctic-qa/corpus-search-r1/protocol/protocol-v2.json \
  --price-config-file config/gemini-eligibility-v1.json \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --overall-ceiling-usd 250
'
```

Use `--max-papers NUMBER` only for an explicit smaller prefix.
The manifest records this count and preserves the frozen order.
The command has no hidden 800-paper limit.

Read the returned `manifest_path`.
Review its ordered identities, stage counts, request count, request-file hash, and reserved cost.
The request file remains private because it contains source text and model instructions.

Dependent stages require separate rounds.
One batch job has a target turnaround of 24 hours.
Therefore, the complete pipeline has no one-day completion promise.

### Recorded real-data preview

An offline preview used the frozen continuation manifest and `--max-papers 1`.
It wrote only to `.pytest_cache/production-batch-preview-v2` in this worktree.
It did not change the production database, source files, or shared ledger.

The preview prepared paper `10.37482/issn2221-2698.2025.59.44` at position 1.
The source hash is `eaf5987a8c2fef3f24549b26aba42fcb1543b596fa73c302083af25b801a508e`.
The eligibility round has these values:

- Round ID: `batch-round-bfea03e79652026e3e16a2cb391c138e`
- Request count: `1`
- Reserved cost: USD `0.0489105`
- Shared ledger use at preparation: USD `12.723581`
- Projected cumulative use: USD `12.7724915`
- Overall construction ceiling: USD `250`
- Live call made: `false`
- Submission enabled: `false`

The round manifest is in `.pytest_cache/production-batch-preview-v2/batch-state/rounds/batch-round-bfea03e79652026e3e16a2cb391c138e/manifest.json`.
This one-paper prefix is a launch check, not a funded full-run estimate.
Prepare the complete 4,379-paper round without `--max-papers` before you request authorization.

## Authorize and submit one round

Wait until the current synchronous campaign is settled.
Make sure that its ledger has no reservation, ambiguous charge, or in-flight request.

Create one authorization JSON file with this shape:

```json
{
  "schema": "arctic-gemini-batch-submit-authorization-v1",
  "round_manifest_sha256": "SHA256_OF_ROUND_MANIFEST",
  "shared_ledger_sha256": "CURRENT_SHARED_LEDGER_SHA256",
  "maximum_reserved_cost_usd": "EXACT_MANIFEST_RESERVED_COST",
  "authorized_by": "CAPTAIN_APPROVAL_REFERENCE"
}
```

Run this command only after the captain supplies that authorization:

```bash
nix develop --offline -c bash -lc '
PYTHONPATH=src python -m arctic_qa.gemini_batch submit \
  --state-dir "$BATCH_STATE" \
  --manifest "$BATCH_STATE/rounds/ROUND_ID/manifest.json" \
  --authorization /PRIVATE/ROUND_ID.authorization.json \
  --credential-file /home/ben/.config/arctic-qa/gemini-api-key \
  --price-config-file config/gemini-eligibility-v1.json \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --overall-ceiling-usd 250
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
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json
```

## Get and ingest results

Run the status command after Google creates the job.
The command saves the exact status response.
It downloads the result file only after a successful terminal state supplies a file name.

```bash
PYTHONPATH=src python -m arctic_qa.gemini_batch status \
  --state-dir "$BATCH_STATE" \
  --round-id ROUND_ID \
  --credential-file /home/ben/.config/arctic-qa/gemini-api-key \
  --price-config-file config/gemini-eligibility-v1.json \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json
```

Then ingest the downloaded JSONL file:

```bash
PYTHONPATH=src python -m arctic_qa.gemini_batch ingest \
  --state-dir "$BATCH_STATE" \
  --round-id ROUND_ID \
  --results-file "$BATCH_STATE/raw/ROUND_ID.download.jsonl" \
  --price-config-file config/gemini-eligibility-v1.json \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json
```

Ingestion maps out-of-order results by request key.
Repeated ingestion of the same exact response does not add cost.
Unexpected or duplicate keys stop ingestion.
Missing results and provider errors retain their conservative liability.
Cancellation does not release a reservation without final settlement evidence.

Run `resume` with the same arguments as `prepare`.
The pipeline validates actual upstream results before it prepares the next eligible stage.
The continuation manifest excludes the completed 41-paper campaign prefix.
An exact request key that exists in the shared ledger causes preparation to stop.

## Export accepted data

The export operation uses the same accepted-export function as synchronous mode.
It can also create the reviewer, benchmark, scoring, CSV, and prompt companion package.

```bash
PYTHONPATH=src python -m arctic_qa.gemini_batch export \
  --db /mnt/crdata/research-abstention/arctic-qa/state.sqlite3 \
  --namespace /mnt/crdata/research-abstention/arctic-qa \
  --campaign-id FUTURE_SCIENTIFIC_CAMPAIGN_ID \
  --publication-output-dir /PRIVATE/PUBLICATION_PACKAGE \
  --prompt-template config/gemini-eligibility-prompt-v4.txt
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
