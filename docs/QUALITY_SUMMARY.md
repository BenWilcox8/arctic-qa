# Quality summary

The quality summary reads a selected export manifest.
It does not change the export, ledger, status file, database, or receipts.

Run the module with two explicit output paths.

```sh
PYTHONPATH=src python -m arctic_qa.quality_summary \
  --export-manifest /path/to/exports/EXPORT_ID/manifest.json \
  --source-root /path/to/arctic-qa \
  --ledger /path/to/shared-paid-call-ledger.json \
  --status /path/to/shared-paid-call-ledger.status.json \
  --json-out /path/to/quality-summary.json \
  --markdown-out /path/to/quality-summary.md
```

The `--ledger` and `--status` inputs are optional.
The `--source-root` value is required for relative export file paths.
Set it to the Arctic QA data namespace that contains the `exports` directory.
Use `--ledger` for known spend, reservations, and ambiguous charges.
Use `--status` for provider configuration and a status budget.

The JSON file gives compact counts, coverage, rejections, spend, and source identity.
The Markdown file gives the same result in a readable form.
The module writes only `--json-out` and `--markdown-out`.

An export with zero accepted items is a valid input.
The report shows zero item counts and makes unavailable rates explicit.

## Validity interpretation

The target validity is 95 percent.
This target is not a measured accuracy result.
An observed automated pass rate or agreement rate measures the selected automated process only.
It does not establish scientific accuracy.

Scientific accuracy stays unknown until an independently established reference sample exists.
The required evidence is a documented sample, blind judgments against a rubric, and an uncertainty interval.
Shared model families, prompts, training data, and source-selection errors can create correlated blind spots.

If the input has `test_only`, `fixture`, `test_mode`, or `synthetic` set to `true`, the report labels it as a fixture.
A fixture is never live research evidence.

## Later bounded independent automated recheck

Freeze the export manifest and record its hash.
Draw a seeded random sample of accepted base questions.
Record the seed and sample size before you start.
Use a different model configuration from construction.
Hide construction answers and prior automated results.
Compare the result with frozen source evidence and a prewritten rubric.
Report the agreement proportion and a 95 percent Wilson interval.
Label the result as automated agreement, not scientific accuracy.

Do not make paid calls from this module or this procedure.
