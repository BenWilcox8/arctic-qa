# Documentation index

This directory holds the operator documentation.
The top-level [README.md](../README.md) holds the map of the repository.
The [research record](../research/README.md) holds the evidence behind the numbers of the paper.

## Start here

| Document | What it gives you |
| --- | --- |
| [REPRODUCTION.md](REPRODUCTION.md) | The path from a clean checkout to a free end-to-end run, and then to each paid stage. |
| [CLI_WALKTHROUGH.md](CLI_WALKTHROUGH.md) | Every command-line stage in order, with its options and its safety boundary. |
| [METHODS.md](METHODS.md) | The research basis of each method choice, and the limits of the claims. |

## Build the corpus

| Document | What it gives you |
| --- | --- |
| [METADATA_PREFILTER.md](METADATA_PREFILTER.md) | The metadata-only reduction of a discovery ledger. No download, no eligibility decision. |
| [SOURCE_SCREENING_PASS.md](SOURCE_SCREENING_PASS.md) | The bounded screening pass over one fixed candidate set. |
| [CHAPTER2_CORPUS.md](CHAPTER2_CORPUS.md) | The column-aware re-extraction and the freeze of the chapter 2 corpus. |

## Run the pipeline

| Document | What it gives you |
| --- | --- |
| [STREAMING_DATASET.md](STREAMING_DATASET.md) | The producer: one paper at a time, the call plan, the budgets and the resume rules. |
| [SHARED_MODEL_BROKER.md](SHARED_MODEL_BROKER.md) | The shared paid-call ledger: gates, receipts, settlement and the operation lock. |
| [FULL_RUN.md](FULL_RUN.md) | The plan of a future full run. It starts nothing and calls nothing. |
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
