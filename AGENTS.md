# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Read `docs/CORPUS_VIEWER.md` before you operate the read-only corpus-stage monitor.
- Read `docs/PUBLICATION_EXPORT.md` before you operate the live publication snapshot service.
- Read `docs/SOURCE_SCREENING_PASS.md` before you operate a bounded source pass.
- Read `docs/STANDALONE_CALIBRATION.md` before you change `STANDALONE_SYSTEM` or record a standalone calibration cassette. Recording is a paid call; replay is free.
- Run the tests as `nix develop -c bash -c 'PYTHONPATH=src pytest tests/ -q'`. The package is not installed in the devshell, so pytest cannot import `arctic_qa` without `PYTHONPATH=src`.
- A whole-suite run takes several minutes. `tests/test_model_broker.py` and `tests/test_streaming.py` hold real rate-limit sleeps.
- Read `docs/CHAPTER2_CORPUS.md` before you extract, freeze, or read the chapter 2 corpus.
- `config/gemini-eligibility-v1.json` is bound into every paid receipt through `price_config_sha256`. Any edit to it needs a chained ledger price transition before a live run can resume, and a new `config_id` revision rather than a changed meaning for an old one.
- No change to `STANDALONE_SYSTEM` ships without a live calibration run on the judge model (paid, captain-approved). The fixture rows live in `fixtures/standalone-calibration-*.jsonl`; the header of each file states its slice and release rule.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
