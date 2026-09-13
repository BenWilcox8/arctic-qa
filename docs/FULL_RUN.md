# Full-run plan

This tool writes a future stream plan.
It does not start a stream, activate a gate, or send a provider request.
It does not copy or hash original source files.

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
  --data-root /mnt/crdata/research-abstention \
  --access-run-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/trial-inputs/full-manifest-continuation-4420-minus41-r1 \
  --eligibility-run-dir /mnt/crdata/research-abstention/arctic-qa/ELIGIBILITY_RUN \
  --run-id FUTURE_SCIENTIFIC_RUN_ID \
  --campaign-id FUTURE_CAMPAIGN_ID \
  --credential-file /PRIVATE/DIRECTORY/gemini.key \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts \
  --streaming-budget-policy-file config/streaming-dataset-budget-policy-v1.json \
  --price-config-file config/gemini-eligibility-v1.json \
  --execution-gate-file config/streaming-live-execution-gate-v1.json \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v3.txt \
  --eligibility-schema-file schemas/gemini-eligibility.v1.schema.json \
  --eligibility-policy-file /mnt/crdata/research-abstention/arctic-qa/ELIGIBILITY_POLICY.json \
  --planning-cumulative-budget-usd 20
```

The `run-id` and `campaign-id` must differ from the article-access run ID.
The output JSON records both identities and the exact future stream argv array.
It is not a shell command.

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
  --export-manifest /mnt/crdata/research-abstention/arctic-qa/exports/EXPORT_ID/manifest.json \
  --ledger /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --status /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.status.json \
  --json-out /PRIVATE/REPORT/quality-summary.json \
  --markdown-out /PRIVATE/REPORT/quality-summary.md
```

The quality report reads the selected export and does not change it.
An empty accepted export is valid input.
It reports zero accepted counts and unavailable rates when the records do not support a rate.
It does not turn test results or machine acceptance into scientific accuracy.

## Frozen 4,420-record draft

The current offline draft is at this path:

`/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/full-run-plans/proposed-full-scientific-run-4420-r1.json`

It binds the `full-text-ready-4420-seed20260912-r1` source freeze.
It has a distinct proposed run ID and campaign ID.
It keeps all 4,420 records in frozen order.
It records the USD 20 planning cap and current shared ledger values.
It does not authorize funding or provider calls.

Create the same draft with this command:

```bash
PYTHONPATH=src python -m arctic_qa.full_run_plan \
  --json-out /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/full-run-plans/proposed-full-scientific-run-4420-r1.json \
  --data-root /mnt/crdata/research-abstention \
  --access-run-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/trial-inputs/full-manifest-continuation-4420-minus41-r1 \
  --eligibility-run-dir /mnt/crdata/research-abstention/arctic-qa/gemini-eligibility-r1/proposed-full-scientific-run-4420-r1 \
  --run-id proposed-full-scientific-run-4420-r1 \
  --campaign-id proposed-full-scientific-campaign-r1 \
  --credential-file /home/ben/.config/arctic-qa/gemini-api-key \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts \
  --streaming-budget-policy-file /home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-qa-build-r1/proposed-streaming-dataset-budget-policy-v6.json \
  --price-config-file config/gemini-eligibility-v1.json \
  --execution-gate-file /home/ben/.config/arctic-qa/gate-021734f-usd20-r1.json \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v4.txt \
  --eligibility-schema-file schemas/gemini-eligibility.v2.schema.json \
  --eligibility-policy-file /mnt/crdata/research-abstention/arctic-qa/corpus-search-r1/protocol/protocol-v2.json \
  --planning-cumulative-budget-usd 20 \
  --frozen-source-manifest-file /mnt/crdata/research-abstention/arctic-qa/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1/full-text-ready-manifest.jsonl \
  --frozen-manifest-descriptor-file /mnt/crdata/research-abstention/arctic-qa/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1/manifest-descriptor.json
```

The current accepted trial export is `export-61975ae865dbb1f2333e`.
It has one short-answer item and two MCQ items.
Use it as trial evidence only.
Its release label remains `machine_accepted_unverified`.

The builder must materialize the frozen JSONL source list as a supported access run.
Then the planner can write a supported stream argv array.
The only deferred captain choices are the final size or selection and the production allocation.
