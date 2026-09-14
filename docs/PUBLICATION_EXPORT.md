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
The benchmark files contain questions and option text only.
The scoring files contain the correct option ID or a null value for an answer-absent variant.

The package excludes full papers, rendered requests, costs, run IDs, timestamps, and release status values.
It does not treat automated validation or model rationales as independent scientific review.

Use `--prompt-template` only for a known historical template.
The exporter copies that template into `historical-prompt-bundle` and records its SHA-256 hash.
Use `--historical-renderer` for an exact historical renderer source file.
It does not label an unspecified or future template as historical.
