# Publication export

The publication exporter writes reviewer, benchmark, and scoring companion files.
The selected export manifest is the source of the final MCQ rows.
The exporter copies each selected option text and label without reconstruction.

Run the exporter with a selected export manifest.

```sh
PYTHONPATH=src:$PYTHONPATH python3.13 -m arctic_qa.publication_export \
  --export-manifest /path/to/exports/export-id/manifest.json \
  --state-db /path/to/state.sqlite3 \
  --output-dir /path/to/publication-package
```

The state database is optional.
When present, it adds paper identity, validation records, retained rationales, and model trace metadata.
It never selects rows or changes options.

The reviewer files contain labels, reference answers, validation records, short evidence excerpts, locators, and retained rationales.
An answer-absent reviewer row retains the paired question reference answer and evidence when state data retains them.
The reviewer CSV files repeat DOI, title, question, reference answer, options, and rationales in readable columns.
The reviewer files keep safe reconstruction and answer-verification result objects in `stage_results`.
The benchmark files contain questions and option text only.
The scoring files contain the correct option ID or a null value for an answer-absent variant.

The package excludes full papers, rendered requests, costs, run IDs, timestamps, and release status values.
It does not treat automated validation or model rationales as independent scientific review.

Use `--prompt-template` only for a known historical template.
The exporter copies that template into `historical-prompt-bundle` and records its SHA-256 hash.
Use `--historical-renderer` for an exact historical renderer source file.
It does not label an unspecified or future template as historical.

## Live accepted snapshot

The live exporter reads the state database without changing it.
It selects accepted families with the exact contracts in `config/live-dataset-current-contract-v1.json`.
Thus, accepted rows from older contracts remain historical and do not enter the current dataset.

The live exporter is a machine-validated preview for captain review.
It requires a payload-bound final validation event and three accepted model-verified distractors.
It preserves each distractor uncertainty and determinism result in the reviewer rows.
It does not label a row as deterministic or human-reviewed.
The final publication and CSV exports still require three deterministic accepted distractors.
It writes one answer-present row for each paper family.
If one family has multiple current candidates, the exporter selects the newest candidate state.

Each content change creates an immutable directory under `snapshots/`.
The exporter updates `current.json` atomically after both JSONL files and their manifest are durable.
A restart with unchanged data selects the existing snapshot and does not add duplicate rows.

Run one refresh with the current contract selection:

```sh
PYTHONPATH=src python -m arctic_qa.publication_export \
  --state-db /mnt/crdata/research-abstention/arctic-qa/state.sqlite3 \
  --output-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/live-publication \
  --live-selection-file config/live-dataset-current-contract-v1.json \
  --live-prompt-file src/arctic_qa/generation.py \
  --live-prompt-file src/arctic_qa/validation.py
```

Add `--poll-seconds 15` to operate the bounded refresh service.
The interval must be from 1 through 300 seconds.
The service does not start model work or change the state database.

The prompt files are immutable, hash-named files under `prompts/`.
Dataset rows do not repeat prompt text.
