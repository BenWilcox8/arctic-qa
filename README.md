# ArcticQA

ArcticQA builds source-supported scientific questions from Arctic research papers.
It also measures whether a model abstains when none of the listed answers is correct.

This repository contains the pipeline code, its tests, its fixed contracts, and the evidence record behind the paper.
It does not contain copyrighted paper text, credentials, or live run state.

## Start here

| Your goal | Read this |
| --- | --- |
| Run the project from a clean checkout | [Reproduction guide](docs/REPRODUCTION.md) |
| Understand the research method and its limits | [Methods](docs/METHODS.md) |
| See every command-line stage | [CLI walkthrough](docs/CLI_WALKTHROUGH.md) |
| Find a module | [Package guide](src/arctic_qa/README.md) |
| Understand the tests and contracts | [Test guide](tests/README.md) |
| Find a research record | [Research index](research/README.md) |
| Find another operator document | [Documentation index](docs/README.md) |

## What the pipeline does

The pipeline follows a paper from discovery to a released benchmark item.

1. Discovery collects paper metadata from Crossref, OpenAlex, or a replay file.
2. The metadata prefilter creates a bounded review queue without downloading full text.
3. The source pass and access pass identify a readable article for each selected paper.
4. Extraction converts each stored article into ordered sections and source chunks.
5. Eligibility checks five scientific criteria and records the supporting source spans.
6. Generation selects a finding, writes a question, builds distractors, and applies validation gates.
7. Export writes accepted multiple-choice and short-answer records with provenance.
8. The abstention benchmark tests each item with the correct answer present and absent.

The strongest automated label is `machine_accepted_unverified`.
This label means that all configured gates passed.
It does not mean that a human confirmed the item.

## Reproduce the work

The fastest complete example uses synthetic input and a fake model provider.
It makes no network call and costs nothing.

```bash
git clone https://github.com/BenWilcox8/arctic-qa.git
cd arctic-qa
export ARCTIC_QA_DATA_ROOT="$HOME/arctic-qa-data"
mkdir -p "$ARCTIC_QA_DATA_ROOT"
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json smoke \
  --fixture-dir fixtures --run-id smoke-r1'
```

The [reproduction guide](docs/REPRODUCTION.md) continues from this free run.
It covers setup without Nix, corpus access, calibration replay, generation, evaluation, and the cost guard.
It marks each paid command and gives the measured cost.

The repository does not redistribute the paper corpus.
The frozen source manifest records each DOI, retrieval URL, and file hash.
A reader can run the same methods on those articles or on another corpus.

## How a generation run flows

1. Files in `config/` define the eligibility prompt, role assignments, budget, and reviewed execution gate.
2. `src/arctic_qa/streaming.py` reads the frozen access run and processes one paper family at a time.
3. `src/arctic_qa/model_broker.py` records each paid call in the shared ledger and writes an immutable receipt.
4. `src/arctic_qa/generation.py` and `src/arctic_qa/validation.py` build and assess each candidate.
5. `src/arctic_qa/db.py` stores resumable state under the configured data root.
6. `src/arctic_qa/exporting.py` writes accepted records under `$ARCTIC_QA_DATA_ROOT/arctic-qa/exports/`.

Read [Streaming dataset](docs/STREAMING_DATASET.md) for the call order and resume rules.
Read [Shared model broker](docs/SHARED_MODEL_BROKER.md) for the paid-call safety contract.

## How the abstention benchmark flows

1. `abstention-eval --action build-set` reads accepted items and freezes `items.jsonl` with a hashed `manifest.json`.
2. `src/arctic_qa/abstention_render.py` creates the exact prompt and a unique option order for every call.
3. `src/arctic_qa/abstention_plan.py` runs all configured vendors and models for each question.
4. Each vendor directory records its manifest, trial list, responses, receipts, and summary.
5. `src/arctic_qa/abstention_score.py` writes metric tables and confidence intervals.

Read [Abstention evaluation](docs/ABSTENTION_EVALUATION.md) for the free and paid procedures.

## How the cost guard flows

1. `src/arctic_qa/benchmark_guard.py` reads the benchmark journal, the shared ledger, and subscription quota reports.
2. It compares the measurements with the rules in the guard code and the evaluation plan.
3. If a rule fires, the guard writes one model to the pause file in `config/` or a run-specific control directory.
4. The evaluator reads that file before each item and keeps paused trials pending.
5. The guard writes its own events and cross-cycle memory inside its guard directory.

The guard never stops an evaluator process.
Read [Benchmark guard](docs/BENCHMARK_GUARD.md) before you operate it.

## Repository map

| Path | Contents |
| --- | --- |
| [`src/arctic_qa/`](src/arctic_qa/README.md) | The Python package, command-line interface, pipeline, evaluator, guard, and viewers. |
| [`tests/`](tests/README.md) | The executable contracts and end-to-end replay tests. |
| [`schemas/`](schemas/README.md) | JSON Schemas for source records, progress records, provider answers, and exported items. |
| [`config/`](config/README.md) | Versioned prompts, policies, prices, role maps, plans, and reviewed gates. |
| [`fixtures/`](fixtures/README.md) | Small synthetic inputs, recorded quota samples, and labeled calibration sets. |
| [`docs/`](docs/README.md) | Reproduction, operation, architecture, and method documents. |
| [`research/`](research/README.md) | Historical reports and measurement files that support the paper. |
| `flake.nix` and `flake.lock` | The pinned Nix development environment. |
| `pyproject.toml` | The Python package metadata and test configuration. |
| `CITATION.cff` | The machine-readable citation record. |
| `LICENSE` | The MIT license. |
| `.gitignore` | The exclusions for credentials, run state, caches, and local environments. |
| `AGENTS.md`, `CLAUDE.md`, and `.claude/` | Contributor instructions for coding agents that do not affect the pipeline. |

## Data and credentials

The code resolves its data root from `ARCTIC_QA_DATA_ROOT`.
It resolves its credential directory from `ARCTIC_QA_CONFIG_DIR`.
The defaults preserve the environment that produced the paper, but a new checkout can use any writable data root.

The pipeline writes only inside the `arctic-qa` namespace of that data root.
The built-in example root must be a mounted drive.
The CLI never falls back from that example root to the root disk.

See [Reproduction: environment variables](docs/REPRODUCTION.md#3-environment-variables) for the complete variable list.

## Research limits

The system never emits `CERTAINLY_TRUE` or `CERTAINLY_FALSE`.
It never converts model votes into a confidence probability.
Source content is untrusted data, so provider prompts tell models not to obey instructions inside a paper.

Read [Methods](docs/METHODS.md) for the evidence basis, label meanings, and claim limits.

## License and citation

The code is available under the [MIT license](LICENSE).
Use [CITATION.cff](CITATION.cff) when you cite this repository.
