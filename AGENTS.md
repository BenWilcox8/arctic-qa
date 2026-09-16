# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Read `docs/CORPUS_VIEWER.md` before you operate the read-only corpus-stage monitor.
- Read `docs/PUBLICATION_EXPORT.md` before you operate the live publication snapshot service.
- Read `docs/SOURCE_SCREENING_PASS.md` before you operate a bounded source pass.
- Read `docs/ABSTENTION_EVALUATION.md` before you build, dry-run, or run the abstention evaluation. A paid run needs a reviewed private evaluation gate and the construction files that the production ledger already binds; the dry run needs neither.
- A subscription evaluation run (Claude Code, Codex) needs the harness login, a reviewed gate with `--provider`, and a subscription ledger directory. Do not change the Codex config in `abstention_subscription.py` without a captured request: `tools.web_search = false` does not remove web search, only `web_search = "disabled"` does.
- Read `docs/STANDALONE_CALIBRATION.md` before you change `STANDALONE_SYSTEM` or record a standalone calibration cassette. Recording is a paid call; replay is free.
- Run the tests as `nix develop -c bash -c 'PYTHONPATH=src pytest tests/ -q'`. The package is not installed in the devshell, so pytest cannot import `arctic_qa` without `PYTHONPATH=src`.
- A whole-suite run takes several minutes. `tests/test_model_broker.py` and `tests/test_streaming.py` hold real rate-limit sleeps.
- Read `docs/CHAPTER2_CORPUS.md` before you extract, freeze, or read the chapter 2 corpus.
- Read the "Chapter 3 call plan" section of `docs/STREAMING_DATASET.md` before you change the order of the paid calls in `generate_candidate`, the finding bank, or the option call plan.
- `config/gemini-eligibility-v1.json` is bound into every paid receipt through `price_config_sha256`. Any edit to it needs a chained ledger price transition before a live run can resume, and a new `config_id` revision rather than a changed meaning for an old one.
- No change to `STANDALONE_SYSTEM` ships without a live calibration run on the judge model (paid, captain-approved). The fixture rows live in `fixtures/standalone-calibration-*.jsonl`; the header of each file states its slice and release rule.
- One paper can hold three eligibility job rows in a run directory: the first screening, one bounded format re-ask, and one bounded geography re-screen. Each row binds its own broker receipt. `streaming.py::_load_eligibility_jobs` takes the last attempt, and `_attempt_order` defines that order.
- Not every row in the `candidates` table is a benchmark item. A row whose status is in `streaming.INCOMPLETE_CANDIDATE_STATUSES` records one generation call. Query benchmark items with `streaming.BENCHMARK_CANDIDATE_PREDICATE`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
