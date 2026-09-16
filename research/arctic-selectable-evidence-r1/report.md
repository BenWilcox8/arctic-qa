# Selectable Evidence Handoff

The model-facing `SOURCE_DATA` payload now exposes only each span's top-level `span_id`.

The payload keeps the exact span text, source chunk, offsets, and text hash.

The resolver still receives the full span map, so resolved combined spans retain their component IDs, locators, hashes, and eligibility provenance.

The finding context and role context use the same projection.

The regression covers adjacent combined spans, hidden component IDs, exact locator round-trip, retained component provenance, unknown IDs, and cross-source IDs.

Validation passed with `nix develop -c env PYTHONPATH=/home/ben/.treehouse/arctic-qa-e841b2/18/arctic-qa/src pytest tests/test_evidence_combination.py -q`.

Validation passed with the three focused streaming tests in `tests/test_streaming.py`.

Ruff and `git diff --check` passed for the changed source and test files.

The implementation commit is `57dcd7184be365bf796590a92c78b5043829da55`.

No prompt version, scientific constant, numeric validation rule, or canonical database record changed.
