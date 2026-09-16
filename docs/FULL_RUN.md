# Full-run plan

This tool writes a future stream plan.
It does not start a stream, activate a gate, or send a provider request.
It does not copy or hash original source files.

The commands of this document write path variables such as `$ARCTIC_QA_DATA_ROOT`.
The "Environment variables" section of `docs/REPRODUCTION.md` gives their values.

The plan reads the frozen article-access manifest.
The ordered source list uses the manifest order.
The plan includes a source hash when the manifest has one.
An item without a full text hash stays in the plan with a null hash.

## Create a plan

Run this command in the repository root.
Set each uppercase path before you run the command.

```bash
PYTHONPATH=src python -m arctic_qa.full_run_plan \
  --json-out /PRIVATE/PLAN/full-run-plan.json \
  --data-root $ARCTIC_QA_DATA_ROOT \
  --access-run-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/trial-inputs/full-manifest-continuation-4420-minus41-r1 \
  --eligibility-run-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/ELIGIBILITY_RUN \
  --run-id FUTURE_SCIENTIFIC_RUN_ID \
  --campaign-id FUTURE_CAMPAIGN_ID \
  --credential-file /PRIVATE/DIRECTORY/gemini.key \
  --shared-ledger-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/model-receipts \
  --streaming-budget-policy-file config/streaming-dataset-budget-policy-v1.json \
  --price-config-file config/gemini-eligibility-v1.json \
  --execution-gate-file config/streaming-live-execution-gate-v1.json \
  --ledger-config-transition-file /PRIVATE/DIRECTORY/reviewed-transition.json \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v3.txt \
  --eligibility-schema-file schemas/gemini-eligibility.v1.schema.json \
  --eligibility-policy-file $ARCTIC_QA_DATA_ROOT/arctic-qa/ELIGIBILITY_POLICY.json \
  --planning-cumulative-budget-usd 20
```

The `run-id` and `campaign-id` must differ from the article-access run ID.
The output JSON records both identities and the exact future stream argv array.
It is not a shell command.
When a new broker policy is not active yet, the reviewed transition file is required.
The plan includes that file and its hash in the configuration identity and stream argv.

By default, the plan includes every source in the frozen manifest order.
It does not select only ready, eligible, or accepted sources.
Do not infer that the full manifest is funded.

For a smaller explicit prefix, add both options below.
The seed is recorded for review even though the prefix preserves manifest order.

```bash
--max-papers 20 --selection-seed approved-prefix-r1
```

The tool rejects a smaller prefix without a seed.
It has no hidden 800-paper cap.

## Read the plan before a live run

Read the `activation`, `budget_and_ledger`, and `generation_configuration` sections.
The plan reports the existing ledger spend and reservation values.
It reports the remaining amount to the stated planning cap.
That value is planning information, not a broker authorization.

The current checked-in policy has a different away-session ceiling.
The plan cannot constrain that policy.
Before a paid command, the builder must provide a reviewed gate and broker policy that enforce the approved cap.
The credential path is recorded only as a path.
Do not put a credential value in the plan or a command.

The plan is ready for the supported stream interface after that approval.
Run the `stream_command_argv` values with `PYTHONPATH=src` in the repository root.
Do not run the command while the execution gate remains disabled.

## Export and quality report handoff

Run the quality report after the stream writes an export manifest.

The selected manifest must have file paths that resolve from its directory.
The current builder must normalize namespace-relative file paths before this handoff.

```bash
PYTHONPATH=src python -m arctic_qa.quality_summary \
  --export-manifest $ARCTIC_QA_DATA_ROOT/arctic-qa/exports/EXPORT_ID/manifest.json \
  --ledger $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --status $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.status.json \
  --json-out /PRIVATE/REPORT/quality-summary.json \
  --markdown-out /PRIVATE/REPORT/quality-summary.md
```

The quality report reads the selected export and does not change it.
An empty accepted export is valid input.
It reports zero accepted counts and unavailable rates when the records do not support a rate.
It does not turn test results or machine acceptance into scientific accuracy.

## Frozen 4,420-record draft

The current offline draft is at this path:

`$ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/full-run-plans/proposed-full-scientific-run-4420-r1.json`

It binds the `full-text-ready-4420-seed20260912-r1` source freeze.
It has a distinct proposed run ID and campaign ID.
It keeps all 4,420 records in frozen order.
It records the USD 20 planning cap and current shared ledger values.
It does not authorize funding or provider calls.

## Materialize the supported access run

Run this command from the repository root.
The command creates a separate access run for all 4,420 frozen records.
It also replaces the planning draft with a plan that has a concrete stream argv array.

```bash
PYTHONPATH=src python -m arctic_qa.full_run_plan \
  --json-out $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/full-run-plans/proposed-full-scientific-run-4420-r1.json \
  --data-root $ARCTIC_QA_DATA_ROOT \
  --access-run-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/full-run-inputs/full-scientific-access-4420-r1 \
  --eligibility-run-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/gemini-eligibility-r1/proposed-full-scientific-run-4420-r1 \
  --run-id proposed-full-scientific-run-4420-r1 \
  --campaign-id proposed-full-scientific-campaign-r1 \
  --credential-file $ARCTIC_QA_CONFIG_DIR/gemini-api-key \
  --shared-ledger-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/model-receipts \
  --streaming-budget-policy-file "$REVIEWED_BUDGET_POLICY" \
  --price-config-file config/gemini-eligibility-v1.json \
  --execution-gate-file $ARCTIC_QA_CONFIG_DIR/gate-021734f-usd20-r1.json \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v4.txt \
  --eligibility-schema-file schemas/gemini-eligibility.v2.schema.json \
  --eligibility-policy-file $ARCTIC_QA_DATA_ROOT/arctic-qa/corpus-search-r1/protocol/protocol-v2.json \
  --planning-cumulative-budget-usd 20 \
  --frozen-source-manifest-file $ARCTIC_QA_DATA_ROOT/arctic-qa/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1/full-text-ready-manifest.jsonl \
  --frozen-manifest-descriptor-file $ARCTIC_QA_DATA_ROOT/arctic-qa/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1/manifest-descriptor.json \
  --materialized-access-run-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/full-run-inputs/full-scientific-access-4420-r1 \
  --phase away_production
```

`$REVIEWED_BUDGET_POLICY` is the reviewed streaming budget policy of the run.
It is written outside the repository, because a run reviews and freezes it separately.

The materializer writes the access manifest, item receipts, progress, and completion receipt.
It does not copy, download, or hash a source file or extraction file.
Each item refers to the original immutable path and its recorded SHA-256 value.
An exact repeated command is idempotent.
Changed data at an existing materialization path causes an error.

Make sure that the plan has `stream_command_status` set to `supported_not_activated`.
Make sure that `input.target_total` and `selection.planned_source_count` are both 4,420.
Make sure that the materialized run contains 4,420 item receipts.

CAUTION: Do not run the stream argv array with the current gate.
The current gate does not authorize the proposed production identities or allocation.

The captain must select the final source count and production budget.
Then create a reviewed production gate for the exact plan identities and access artifact.
Do not change the frozen order or recorded source hashes.

The current accepted trial export is `export-61975ae865dbb1f2333e`.
It has one short-answer item and two MCQ items.
Use it as trial evidence only.
Its release label remains `machine_accepted_unverified`.

## Create the quality-first 800-record input

This command writes a ranked subset from the frozen 4,420-record manifest.
It does not call a provider, alter the freeze, or hash the original source files.

```bash
PYTHONPATH=src python -m arctic_qa.quality_order \
  --source-manifest $ARCTIC_QA_DATA_ROOT/arctic-qa/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1/full-text-ready-manifest.jsonl \
  --descriptor $ARCTIC_QA_DATA_ROOT/arctic-qa/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1/manifest-descriptor.json \
  --output-dir /PRIVATE/QUALITY_ORDER \
  --seed approved-quality-order-seed-r1 \
  --limit 800
```

The tool records every source in `quality-order.json`.
Each record has its original position, identity hashes, quality band, reason list, and seeded tie-break key.
The tool reads at most 16,000 characters from each local extraction.
It uses frozen metadata and this local extraction structure only.
It excludes QA outcomes, acceptance labels, author prestige, citation counts, and model familiarity.

The selected JSONL file keeps the ranked order and source receipts.
The matching descriptor and `materialized-top-800` directory work with `full_run_plan` as the access input.
The materializer writes zero paid calls.

The quality bands prefer readable text with Methods, Results, and evidence markers.
Within a quality band, the order gives priority to the existing frozen Arctic or marine cue.
The fixed seed then orders records with equal quality and cue state.
This ranking does not establish scientific eligibility or correctness.
