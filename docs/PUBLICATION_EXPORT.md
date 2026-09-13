# Publication export

The publication exporter creates linked reviewer, benchmark, and scoring files.
It reads the selected state database in read-only mode.

```sh
PYTHONPATH=src python -m arctic_qa.publication_export \
  --state-db /path/to/arctic-qa/state.sqlite3 \
  --run-id trial-r1 \
  --output-dir /path/to/publication-package
```

The package contains JSONL and CSV files for each projection.
The reviewer files contain the question, options, evidence excerpts, validation records, selection data, and attributable receipt responses.
The benchmark files contain only the question and option text.
The scoring files contain the correct option ID and release status.

Each package has a manifest with counts and SHA-256 hashes.
The exporter writes one reviewer row for each accepted QA record with three accepted deterministic distractors.

The files contain no full papers, extraction blobs, or submitted prompts.
The reviewer files are not model-facing benchmark inputs.
The exporter retains missing rationale fields as null or false availability values.
It does not create historical model explanations.
