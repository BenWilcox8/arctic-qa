# Metadata prefilter

The metadata prefilter processes an existing discovery ledger.
It does not search for papers, download sources, or decide scientific eligibility.

The frozen policy is `config/metadata-prefilter-policy-v1.json`.
It creates one auditable disposition for every discovery record.
It also assigns every record to a deterministic retrieval or review queue.

Run the command with explicit input and output paths:

```bash
PYTHONPATH=src python -m arctic_qa metadata-prefilter \
  --candidates-file /path/to/deduplicated-candidates.json \
  --screening-file /path/to/initial-screening-ledger-r3.json \
  --protocol-file /path/to/protocol-v2.json \
  --policy-file config/metadata-prefilter-policy-v1.json \
  --output-dir /path/to/metadata-prefilter-r1/run-RUN_ID \
  --run-id RUN_ID \
  --code-commit GIT_COMMIT \
  --viewer-progress-file /private/runtime/corpus-progress-v1.json
```

Use the same command to resume an incomplete run.
The command refuses changed inputs, policy, batch size, run identity, or producing commit.
It returns the saved receipt without changing a completed run.

Each completed batch is an immutable NDJSON file.
The atomic progress file records completed batch hashes and resume history.
The final receipt reconciles all record, disposition, and queue counts.
The final review queue orders queue rank first and discovery sequence second.

Metadata dispositions remain separate from source eligibility.
A nonresearch type is a metadata review flag, not a source-screening exclusion.
An article type is also provisional until source review.
Title terms are review flags only.
