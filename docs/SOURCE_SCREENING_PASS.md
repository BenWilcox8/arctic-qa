# Bounded source-screening pass

The source pass processes one fixed candidate set.
It does not discover papers or generate questions.
Read `config/source-screening-policy-v1.json` before you run it.

## Records

The `prepare` action writes an immutable `run-manifest.json` file.
This file records all selected keys, input hashes, limits, and the producer commit.
The command refuses to resume if an input changed.

Each network attempt has an immutable receipt in `attempts/`.
Each selected candidate has one immutable receipt in `items/` after processing.
Originals and extracted text are private run files.
Do not serve the run directory over HTTP.

The `progress.json` file shows a live observation.
It is not completion proof.
The `decide` action writes a versioned overlay and a durable run receipt.
Hash-checked pointer files identify the current revision.

## Run sequence

Use the same explicit paths and producer commit for all actions:

```bash
PYTHONPATH=src python -m arctic_qa --json source-pass \
  --action prepare \
  --queue-file /path/to/review-queue.ndjson \
  --candidates-file /path/to/deduplicated-candidates.json \
  --protocol-file /path/to/protocol-v2.json \
  --prior-screening-file /path/to/initial-screening-ledger-r3.json \
  --policy-file config/source-screening-policy-v1.json \
  --output-dir /private/source-screening/run-RUN_ID \
  --run-id RUN_ID \
  --code-commit PRODUCER_COMMIT \
  --viewer-progress-file /private/runtime/corpus-progress-v1.json
```

Run `smoke` with the same options after you inspect the manifest.
The smoke action stops after the fixed smoke size.
Inspect its receipts and viewer state.
Then run `continue` with the same options.
This action processes the rest of the fixed selection.

The runner uses only the discovery record's open-access URL.
A missing URL remains unattempted and source-unreviewed.
A failed request remains pending.
Neither state is a scientific exclusion.

## Source decisions

The decision proposal has schema `source-decision-proposals-v1`.
Each decision must name a decision author and one semantic review method.
Use `codex-native-semantic-source-review-v1` for native Codex review.
Use `human-semantic-source-review-v1` for direct human review.

Deterministic code checks hashes, source versions, quote offsets, and criteria fields.
These checks do not perform semantic screening.
Do not describe the source pass as LLM-free when Codex made the scientific decision.

An explicit decision requires verified full text and exact source passages.
Its source hash and extracted-text hash must match the stored receipt.
Record geography separately from overall scientific eligibility.
Record unknown correction or retraction coverage as a limitation.

Run the final action with the normal arguments and this additional option:

```bash
  --action decide --decisions-file /private/decision-proposals-v1.json
```

The command retains unreviewed and pending records in the final overlay.
It does not turn access failure into exclusion.
