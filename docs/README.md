# Documentation index

This directory holds the operator documentation.
The top-level [README.md](../README.md) holds the map of the repository.
The [research record](../research/README.md) holds the evidence behind the numbers of the paper.

Start with `REPRODUCTION.md` if you want to operate the project.
Start with `METHODS.md` if you want to evaluate the scientific design.
The other documents explain one stage or one safety boundary in detail.

## Reading paths

### Reproduce the complete method

1. Use [REPRODUCTION.md](REPRODUCTION.md) to prepare a clean checkout and run the free example.
2. Use [CLI_WALKTHROUGH.md](CLI_WALKTHROUGH.md) for the corpus stages and their files.
3. Use [STREAMING_DATASET.md](STREAMING_DATASET.md) for generation and resume behavior.
4. Use [ABSTENTION_EVALUATION.md](ABSTENTION_EVALUATION.md) for the benchmark.
5. Use [PUBLICATION_EXPORT.md](PUBLICATION_EXPORT.md) for the release package.

### Audit a paid model call

1. Read [SHARED_MODEL_BROKER.md](SHARED_MODEL_BROKER.md) for admission, receipts, and settlement.
2. Read [STREAMING_DATASET.md](STREAMING_DATASET.md) for a construction call.
3. Read [ABSTENTION_EVALUATION.md](ABSTENTION_EVALUATION.md) for an evaluation call.
4. Read [BENCHMARK_GUARD.md](BENCHMARK_GUARD.md) for budget and quota controls.

### Understand one published item

1. Read [BENCHMARK_INPUT_CONTRACT.md](BENCHMARK_INPUT_CONTRACT.md) for the model-facing fields.
2. Read [METHODS.md](METHODS.md) for the source-evidence and validation rules.
3. Read [PUBLICATION_EXPORT.md](PUBLICATION_EXPORT.md) for the reviewer and benchmark files.
4. Read [QUALITY_SUMMARY.md](QUALITY_SUMMARY.md) for aggregate coverage and quality results.

## Start here

| Document | What it gives you |
| --- | --- |
| [REPRODUCTION.md](REPRODUCTION.md) | The path from a clean checkout to a free end-to-end run, and then to each paid stage. |
| [CLI_WALKTHROUGH.md](CLI_WALKTHROUGH.md) | Every command-line stage in order, with its options and its safety boundary. |
| [METHODS.md](METHODS.md) | The research basis of each method choice, and the limits of the claims. |

## Build the corpus

| Document | What it gives you |
| --- | --- |
| [METADATA_PREFILTER.md](METADATA_PREFILTER.md) | The metadata-only reduction of a discovery ledger before download or eligibility decisions. |
| [SOURCE_SCREENING_PASS.md](SOURCE_SCREENING_PASS.md) | The bounded screening pass over one fixed candidate set. |
| [CHAPTER2_CORPUS.md](CHAPTER2_CORPUS.md) | The column-aware re-extraction and the freeze of the chapter 2 corpus. |

## Run the pipeline

| Document | What it gives you |
| --- | --- |
| [STREAMING_DATASET.md](STREAMING_DATASET.md) | The producer: one paper at a time, the call plan, the budgets and the resume rules. |
| [SHARED_MODEL_BROKER.md](SHARED_MODEL_BROKER.md) | The shared paid-call ledger: gates, receipts, settlement and the operation lock. |
| [FULL_RUN.md](FULL_RUN.md) | A read-only plan for a future full run that starts nothing and calls nothing. |
| [GEMINI_BATCH.md](GEMINI_BATCH.md) | The Gemini Batch path through the same prompts, schemas and validators. |
| [GEMINI_STRUCTURED_OUTPUT_BUDGET.md](GEMINI_STRUCTURED_OUTPUT_BUDGET.md) | Why a structured-output request needs a larger token budget than its answer. |
| [STANDALONE_CALIBRATION.md](STANDALONE_CALIBRATION.md) | The calibration of the source-blind judge, by recorded cassette. |

## Evaluate and release

| Document | What it gives you |
| --- | --- |
| [ABSTENTION_EVALUATION.md](ABSTENTION_EVALUATION.md) | The 8-model abstention benchmark: sets, plans, gates, providers and scores. |
| [BENCHMARK_GUARD.md](BENCHMARK_GUARD.md) | The budget and quota guard of the live benchmark, and its pause rules. |
| [BENCHMARK_INPUT_CONTRACT.md](BENCHMARK_INPUT_CONTRACT.md) | What one model-facing benchmark item contains. |
| [PUBLICATION_EXPORT.md](PUBLICATION_EXPORT.md) | The reviewer, benchmark and scoring files of a release. |
| [QUALITY_SUMMARY.md](QUALITY_SUMMARY.md) | The read-only quality report over a selected export manifest. |

## Watch a run

| Document | What it gives you |
| --- | --- |
| [CORPUS_VIEWER.md](CORPUS_VIEWER.md) | The read-only corpus and pipeline monitor, and its artifact boundary. |

## Review record

| Document | What it gives you |
| --- | --- |
| [REVIEW_FIXES.md](REVIEW_FIXES.md) | Each failed review control, its correction and the test that holds it. |

## Document boundaries

Operator documents describe current commands and contracts.
Files under `research/` are historical evidence and can contain old commands or machine-specific paths.
Run state, paper text, receipts, and credentials live outside this checkout under the configured data root.
