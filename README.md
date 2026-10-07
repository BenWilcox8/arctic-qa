# Arctic Questions, Missing Answers

This repository holds the code, data and analysis behind the paper "Arctic Questions, Missing Answers: A Dataset and Benchmark for LLM Abstention in Arctic Science".
**ArcticQA** is a dataset of 194 multiple-choice questions built from primary Arctic research papers.
Each question has a gold answer and four distractors, and separate automated checks test them against a verbatim passage of the source paper.
**ArcticAbstain** is a paired benchmark on those questions.
It shows each question twice: once with the correct answer among the options, and once with the correct answer replaced by a distractor.
Both versions also offer the option "I abstain from answering".
A comparison of the two conditions shows whether a model abstains because no valid answer is available, or only abstains at a fixed rate.

## Status

- **Machine-accepted, not expert-verified.** Every item carries the label `machine_accepted_unverified`. All automated gates passed, but no domain expert reviewed any item, and the residual error rate is not measured.
- **Released under open licenses.** The dataset in `data/arcticqa-v1/` is released under CC BY 4.0, and the code under the MIT License. See [License](#license).
- **Frozen.** The data comes from the evaluation snapshot of 2026-09-17T18:02:42Z and does not change.
- **Source passages withheld.** The repository holds no text of the source papers. Each item names its source paper by DOI. See [the data README](data/arcticqa-v1/README.md#what-is-withheld).

## Headline result

Eight models answered each of the 194 questions in both conditions, three times each, at high reasoning effort.
This gives 9,312 recorded responses, of which 9,205 are valid.

| Model | Abstention, answer present | Abstention, answer absent | Shift (pp) [95% CI] | Holm p |
| --- | ---: | ---: | ---: | ---: |
| Gemini 3.8 Flash | 6.4% | 10.3% | +3.9 [0.6, 7.4] | 0.187 |
| Gemini 3.7 Flash | 6.0% | 8.6% | +2.5 [-0.1, 5.4] | 0.198 |
| Claude Fable 5.1 | 36.1% | 46.9% | +10.8 [5.7, 16.1] | 0.0008 |
| Claude Opus 5 | 41.3% | 47.5% | +6.2 [0.9, 11.6] | 0.187 |
| Claude Sonnet 5 | 48.5% | 51.0% | +2.5 [-1.7, 6.9] | 0.331 |
| ChatGPT Astra | 63.0% | 74.1% | +11.0 [5.7, 16.6] | 0.0010 |
| ChatGPT 5.6 Sol | 0.2% | 2.1% | +1.9 [0.3, 4.0] | 0.198 |
| ChatGPT 5.6 Terra | 0.0% | 1.5% | +1.5 [0.3, 3.3] | 0.198 |

- Abstention with the correct answer present ranges from 0.0% to 63.0%. The baseline differs much more between models than the response to answer removal does.
- The abstention point estimate rises for every model when the correct answer is removed. Averaged over the eight models, the mean per-question shift is +5.05 percentage points.
- After Holm correction over the eight model tests, only Claude Fable 5.1 and ChatGPT Astra show a significant shift (adjusted p < 0.05).

The table gives exact values, rounded. The paper prints each shift as the difference of the two rounded rates, so it shows +2.6 for Gemini 3.7 Flash and +11.1 for ChatGPT Astra. It also prints 0.0011 for the Holm p of ChatGPT Astra, whose exact value is 0.00105.
[`data/arcticqa-v1/results/TABLES.md`](data/arcticqa-v1/results/TABLES.md) holds both paper tables with the p-values, and the data README explains each [difference in presentation](data/arcticqa-v1/README.md#how-the-numbers-relate-to-the-paper).

## Get the data

Clone the repository, or download single files from the `data/arcticqa-v1/` folder:

```bash
git clone https://github.com/BenWilcox8/arctic-qa.git
cd arctic-qa
```

Each JSONL file has one JSON record per line, so plain Python reads it with no extra package:

```python
import json
items = [json.loads(line) for line in open("data/arcticqa-v1/items.jsonl", encoding="utf-8")]
responses = [json.loads(line) for line in open("data/arcticqa-v1/responses.jsonl", encoding="utf-8")]
```

## The data

Everything the paper analyses is in [`data/arcticqa-v1/`](data/arcticqa-v1/README.md), as plain JSONL and CSV files.

| File | Rows | Contents |
| --- | ---: | --- |
| [`items.jsonl`](data/arcticqa-v1/items.jsonl) | 194 | Question, context, gold answer, four ranked distractors, source paper DOI, and how each distractor was verified. |
| [`conditions.jsonl`](data/arcticqa-v1/conditions.jsonl) | 388 | The answer-present and answer-absent option set of each question, with the correct action. |
| [`responses.jsonl`](data/arcticqa-v1/responses.jsonl) | 9,312 | Every response: model, condition, trial, the exact prompt and option letters shown, the raw text, the chosen option, the outcome class (N0 to N5) and validity. |
| [`responses.csv`](data/arcticqa-v1/responses.csv) | 9,312 | A flat copy of the main response fields. |
| [`excluded-items.json`](data/arcticqa-v1/excluded-items.json) | 1 | The 195th accepted question, which is outside the analysis because 8 of its 48 responses were never collected. |
| [`results/`](data/arcticqa-v1/results/) | | The paper tables, per-model rates, intervals, p-values and metrics. |

The [data README](data/arcticqa-v1/README.md) gives the full schema, the outcome classes, the 107 invalid responses and their causes, and what is withheld.

## Reproduce the paper tables

One command recomputes every number of the two paper tables from `responses.jsonl`:

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa.paper_tables'
```

It needs no network and no credential, and it takes about 15 seconds on an idle CPU.
It writes `data/arcticqa-v1/results/`; add `--check` to compare with the committed files instead.
`tests/test_paper_release.py` checks the counts, re-renders all 9,312 prompts from the items, and asserts every printed number of the paper tables.

Without Nix, any Python 3.11 or later works: `PYTHONPATH=src python3 -m arctic_qa.paper_tables`.
The package has no third-party dependency.

The paper's figure plots the two abstention rates of each model, which are the `present_abstention` and `absent_abstention` columns of `data/arcticqa-v1/results/condition-shifts.csv`.

## The code that matters most

### Dataset construction (paper section 3)

| Stage | Main code | What it does |
| --- | --- | --- |
| Corpus and access | `discovery.py`, `metadata_prefilter.py`, `source_pass.py`, `extraction.py` | Collects metadata, filters it, gets readable full text, and splits it into sections and chunks. |
| Eligibility | `gemini_eligibility.py`, [`config/arctic-eligibility-policy-v3.json`](config/arctic-eligibility-policy-v3.json) | Screens each paper against five criteria, among them primary research and the 66.56 degrees north geography rule. |
| Finding and question | `generation.py` | Selects one finding and its verbatim evidence, then writes a standalone question and its context. |
| QA validation | `generation.py`, `validation.py` | A blind reconstruction of the answer, an answer verdict against the source, and rule-based gates. |
| Distractors | `generation.py`, `validation.py`, `distractor_order.py` | Proposes candidates, checks each one for contradiction in the question's scope, and fixes a random order. |
| Orchestration | `streaming.py`, `model_broker.py`, `db.py` | Runs one paper family at a time and records every paid call with an immutable receipt. |

The writer model was Gemini 3.8 Flash, and the answer and option judges were Gemini 3.1 Pro Preview.
Of the 776 distractors of the 194 items, 455 have a rule-based confirmation and 321 rest on the judge's verdict alone.
185 of the 194 source papers have a Semantic Scholar discovery record.
The metadata prefilter kept 16,339 papers, of which 4,420 had full text and formed the frozen corpus ([JEV_PRESCREEN.md](docs/JEV_PRESCREEN.md)).
The run screened a ranked part of that corpus, so the 194 items are not a random sample of Arctic research.

### Abstention evaluation (paper section 4)

| Step | Main code |
| --- | --- |
| Freeze the evaluation items | `abstention_set.py` |
| Render the exact prompt, the two conditions and the per-call option order | [`abstention_render.py`](src/arctic_qa/abstention_render.py) |
| Call the models: Gemini API, Claude Code CLI, Codex CLI | `abstention_providers.py`, `abstention_subscription.py` |
| Run all eight models per question | `abstention_plan.py`, `abstention_watch.py` |
| Map each letter to N0 to N5 and compute the metrics | [`abstention_score.py`](src/arctic_qa/abstention_score.py) |

### Analysis and release (paper section 5)

| File | What it does |
| --- | --- |
| [`src/arctic_qa/paper_tables.py`](src/arctic_qa/paper_tables.py) | Rates, shifts, bootstrap intervals, sign-flip tests, Holm correction and metrics, from the released responses. |
| [`src/arctic_qa/paper_release.py`](src/arctic_qa/paper_release.py) | Builds `data/arcticqa-v1/` from the frozen evaluation snapshot and re-renders each call to prove it. |
| [`tests/test_paper_release.py`](tests/test_paper_release.py) | Holds the counts, the rendering and the paper numbers. |

## Limitations

- ArcticAbstain is a geographically bounded case study. Its coverage follows the discovery queries, full-text availability and a model-guided processing order. It does not represent all Arctic research or other fields.
- The items come from model-generated content and automated validation, with one model vendor for both writing and judging. No sample was reviewed by a domain expert.
- 15 of the 194 questions are fully or partly in Russian (Cyrillic script).
- The evaluation covers 194 questions, eight models, one prompt, one abstention option and high reasoning effort only.
- The paired multiple-choice task measures rejection of an invalid option set, not spontaneous abstention in open scientific work.
- The results do not establish training exposure, explain model policies, or show generalization outside this setting.

[Methods](docs/METHODS.md) gives the research basis of each design choice and the limits of each claim.

## Everything else

### Start here

| Your goal | Read this |
| --- | --- |
| Use the dataset and responses | [Data README](data/arcticqa-v1/README.md) |
| Run the pipeline from a clean checkout | [Reproduction guide](docs/REPRODUCTION.md) |
| Understand the research method and its limits | [Methods](docs/METHODS.md) |
| See every command-line stage | [CLI walkthrough](docs/CLI_WALKTHROUGH.md) |
| Operate the abstention evaluation | [Abstention evaluation](docs/ABSTENTION_EVALUATION.md) |
| Find a module | [Package guide](src/arctic_qa/README.md) |
| Understand the tests and contracts | [Test guide](tests/README.md) |
| Find a research record | [Research index](research/README.md) |
| Find another operator document | [Documentation index](docs/README.md) |

### A free end-to-end run

The fastest complete pipeline example uses synthetic input and a fake model provider.
It makes no network call and costs nothing.

```bash
export ARCTIC_QA_DATA_ROOT="$HOME/arctic-qa-data"
mkdir -p "$ARCTIC_QA_DATA_ROOT"
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --json smoke \
  --fixture-dir fixtures --run-id smoke-r1'
```

The [reproduction guide](docs/REPRODUCTION.md) continues from this free run.
It covers setup without Nix, corpus access, calibration replay, generation, evaluation and the cost guard.
It marks each paid command and gives the measured cost.
The repository does not redistribute the paper corpus.

### How a generation run flows

1. Files in `config/` define the eligibility prompt, role assignments, budget and reviewed execution gate.
2. `src/arctic_qa/streaming.py` reads the frozen access run and processes one paper family at a time.
3. `src/arctic_qa/model_broker.py` records each paid call in the shared ledger and writes an immutable receipt.
4. `src/arctic_qa/generation.py` and `src/arctic_qa/validation.py` build and assess each candidate.
5. `src/arctic_qa/db.py` stores resumable state under the configured data root.
6. `src/arctic_qa/exporting.py` writes accepted records under `$ARCTIC_QA_DATA_ROOT/arctic-qa/exports/`.

Read [Streaming dataset](docs/STREAMING_DATASET.md) for the call order and resume rules.
Read [Shared model broker](docs/SHARED_MODEL_BROKER.md) for the paid-call safety contract.

### How the abstention benchmark flows

1. `abstention-eval --action build-set` reads accepted items and freezes `items.jsonl` with a hashed `manifest.json`.
2. `src/arctic_qa/abstention_render.py` creates the exact prompt and a unique option order for every call.
3. `src/arctic_qa/abstention_plan.py` runs all configured vendors and models for each question.
4. Each vendor directory records its manifest, trial list, responses, receipts and summary.
5. `src/arctic_qa/abstention_score.py` writes metric tables and confidence intervals.

Read [Abstention evaluation](docs/ABSTENTION_EVALUATION.md) for the free and paid procedures.
Read [Benchmark guard](docs/BENCHMARK_GUARD.md) for the cost and quota guard that ran beside the evaluator.

### Repository map

| Path | Contents |
| --- | --- |
| [`data/`](data/arcticqa-v1/README.md) | The released items, conditions, responses and result tables (CC BY 4.0). |
| [`src/arctic_qa/`](src/arctic_qa/README.md) | The Python package, command-line interface, pipeline, evaluator, analysis, guard and viewers. |
| [`tests/`](tests/README.md) | The executable contracts and end-to-end replay tests. |
| [`schemas/`](schemas/README.md) | JSON Schemas for source records, progress records, provider answers and exported items. |
| [`config/`](config/README.md) | Versioned prompts, policies, prices, role maps, plans and reviewed gates. |
| [`fixtures/`](fixtures/README.md) | Small synthetic inputs, recorded quota samples and labeled calibration sets. |
| [`docs/`](docs/README.md) | Reproduction, operation, architecture and method documents. |
| [`research/`](research/README.md) | Historical reports and measurement files that support the paper. |
| `flake.nix` and `flake.lock` | The pinned Nix development environment. |
| `pyproject.toml` | The Python package metadata and test configuration. |
| `CITATION.cff` | The machine-readable citation record. |
| `LICENSE` | The MIT License of the code. See [License](#license). |
| `AGENTS.md`, `CLAUDE.md` | Contributor instructions for coding agents. They do not affect the pipeline. |

### Data root and credentials

The pipeline resolves its data root from `ARCTIC_QA_DATA_ROOT` and its credential directory from `ARCTIC_QA_CONFIG_DIR`.
The defaults keep the environment that produced the paper, but a new checkout can use any writable data root.
The pipeline writes only inside the `arctic-qa` namespace of that data root.
The built-in example root must be a mounted drive, and the CLI never falls back from it to the root disk.
See [Reproduction: environment variables](docs/REPRODUCTION.md#3-environment-variables) for the complete list.
The released data in `data/arcticqa-v1/` is not run state, and the pipeline never writes to it.

### Research limits of the pipeline

The system never emits `CERTAINLY_TRUE` or `CERTAINLY_FALSE`.
It never converts model votes into a confidence probability.
Source content is untrusted data, so provider prompts tell models not to obey instructions inside a paper.

## What the repository does not include

The paper mentions these records, and the repository does not hold them:

| Not included | Reason |
| --- | --- |
| The verbatim source passages (answer evidence and distractor contradictions) | The authors withhold all text of the source papers. Each item gives the DOI of its paper instead. |
| The full texts and PDFs of the source papers | They are copyrighted by their publishers. |
| The per-candidate construction records: rejected candidates, judge verdicts and rationales, validation events | They quote the source passages. |
| The provider receipts and the paid-call ledger | Each receipt holds the full request, with source text. The responses file keeps the model output of every evaluation call. |
| The discovery and metadata-filter records (84,829 and 16,339 papers in the paper) | They were not prepared for release. The code that made them is here. |
| The paper's LaTeX source and figure files | The authors distribute the paper separately. The figure's data is in `results/condition-shifts.csv`. |

The prompts, the role and model configuration, the eligibility policy and all pipeline code are in the repository: `config/`, `src/arctic_qa/generation.py` and `data/arcticqa-v1/prompt.json`.

## Citation

Please cite the paper:

```bibtex
@misc{wilcox2026arctic,
  title  = {Arctic Questions, Missing Answers: A Dataset and Benchmark for {LLM} Abstention in Arctic Science},
  author = {Wilcox, Benjamin and Gao, Dawei and Kathiravelu, Pradeeban and Causey, Douglas and Sha, Kewei and Feng, Yunhe},
  year   = {2026},
  note   = {arXiv identifier to be added}
}
```

[CITATION.cff](CITATION.cff) holds the same record in machine-readable form.

## License

- **Code:** the MIT License, in [`LICENSE`](LICENSE). It covers everything outside `data/arcticqa-v1/`.
- **Dataset:** the Creative Commons Attribution 4.0 International License (CC BY 4.0), in [`data/arcticqa-v1/LICENSE`](data/arcticqa-v1/LICENSE). It covers every file in `data/arcticqa-v1/`. The legal code is at https://creativecommons.org/licenses/by/4.0/legalcode.

The source papers keep their own copyright. The dataset names each one by DOI and holds no text from it.
