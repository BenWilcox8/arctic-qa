# ArcticQA

ArcticQA builds source-supported scientific questions from Arctic research papers, and measures how often a model abstains when no listed option is correct.

The pipeline reads a frozen corpus of full-text papers.
For each paper it screens the scientific eligibility, selects one finding, writes a question, builds distractors, and runs a chain of deterministic and model gates.
A question that passes every gate becomes a multiple-choice item with the label `machine_accepted_unverified`.
The benchmark then asks eight models each item, in a form where the correct option is present and in a form where it is absent.

Every paid model call goes through one shared ledger with a per-call receipt, so each number in the paper has a record.

This repository is the code and the evidence record behind the paper.
Read [docs/REPRODUCTION.md](docs/REPRODUCTION.md) to run it from a clean checkout.

## The repository

| Path | What it holds |
| --- | --- |
| `src/arctic_qa/` | The package: the pipeline, the evaluator, the guards and the viewers. |
| `tests/` | The test suite. It is also the executable specification of every contract. |
| `schemas/` | The JSON schemas of the provider responses and the exported records. |
| `config/` | The frozen prompts, policies, price tables and model registries. Receipts bind these files by hash. |
| `fixtures/` | Small synthetic inputs and calibration sets. No real source text. |
| `docs/` | The operator documentation. [docs/README.md](docs/README.md) is its index. |
| `research/` | The evidence record: one report for each task that built or corrected the pipeline. [research/README.md](research/README.md) is its index. |
| `flake.nix` | The pinned development shell. |

The pipeline never writes to the repository.
It writes every run under the data root, which `ARCTIC_QA_DATA_ROOT` names.

## The package

Every file named below is under `src/arctic_qa/`.
The modules group into seven areas.

**The corpus.**
`discovery.py` finds candidate papers.
`metadata_prefilter.py` and `source_pass.py` reduce that set before any full text is read.
`access_readiness.py` records which papers have a readable full text.
`extraction.py`, `pdf_layout.py` and `text_structure.py` turn a stored PDF or HTML file into sections and chunks.
`chapter2_corpus.py` freezes a corpus for one chapter.

**Eligibility.**
`screening.py` applies the deterministic geography rules.
`gemini_eligibility.py` asks the model the five scientific criteria.
`geography_correction.py` applies a reviewed geography overlay.

**Generation.**
`generation.py` holds the writer, the finding bank, the judge prompts and the option stage.
`context_projection.py`, `quality_order.py` and `distractor_order.py` decide what the writer sees and in which order the options appear.
`validation.py` holds the deterministic gates and the composed decision.

**Paid calls.**
`model_broker.py` is the shared ledger: budgets, gates, concurrency, receipts and settlement.
`broker_provider.py` and `providers.py` are the transports.
`model_roles.py` maps a stage to a model.
`gemini_batch.py` runs the batch path.

**The run.**
`streaming.py` is the producer: one paper at a time, from eligibility to export.
`db.py` holds the resumable state, `storage.py` stores the original objects, and `manifests.py` writes the content-addressed source manifests.
`exporting.py` and `publication_export.py` write the released files.
`full_run_plan.py` writes a plan for a future run without starting one.
`rerun_selection.py` chooses which papers a release repeats.

**The benchmark and the guards.**
`abstention_set.py` freezes an evaluation set, `abstention_plan.py` and `abstention_run.py` run it, and `abstention_score.py` scores it.
`abstention_providers.py` and `abstention_subscription.py` call the Gemini API, the Claude Code binary and the Codex binary.
`abstention_watch.py` is the streaming evaluator, `abstention_render.py` builds the exact model-facing text, and `abstention_cost.py` keeps the cost journal.
`benchmark_guard.py` keeps the benchmark inside its budget and its subscription quotas.
`corpus_viewer.py` is the read-only monitor, and `pipeline_trace.py` builds the per-request traces it shows.

**Entry points and support.**
`cli.py` and `abstention_cli.py` build the command-line interface, and `__main__.py` starts it.
`paths.py` resolves the data root and the credential directory.
`errors.py` holds the public error codes, and `util.py` holds the hashing and the atomic writes.
`standalone_calibration.py`, `chapter2_replay.py`, `quality_summary.py` and `extraction_quality.py` are the offline measurement harnesses.

## Quick start

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --version'
export ARCTIC_QA_DATA_ROOT="$PWD/../arctic-qa-data" && mkdir -p "$ARCTIC_QA_DATA_ROOT"
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json doctor'
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json smoke --fixture-dir fixtures --run-id smoke-r1'
```

The last command runs the whole pipeline on a synthetic fixture with a fake provider.
It makes no network call and costs nothing.
[docs/REPRODUCTION.md](docs/REPRODUCTION.md) continues from there.

## What the labels mean

The strongest automated label is `machine_accepted_unverified`.
It means that every deterministic gate and every model gate passed.
It does not mean that a human confirmed the item.

The system never emits `CERTAINLY_TRUE` or `CERTAINLY_FALSE`.
It never turns model votes into a confidence probability.

Read [docs/METHODS.md](docs/METHODS.md) for the evidence basis and the limits.

## Safety boundary

The pipeline treats source content as untrusted data.
Every provider prompt tells the model not to obey instructions inside a source and not to call a tool.

The CLI writes only under the `arctic-qa` namespace of the data root.
The built-in example root must be a mounted drive, and the CLI never falls back to the root disk.
[docs/CLI_WALKTHROUGH.md](docs/CLI_WALKTHROUGH.md) states the full rule.

## License and citation

The code is MIT licensed. See [LICENSE](LICENSE).
[CITATION.cff](CITATION.cff) holds the citation record.
