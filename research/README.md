# Research record

This tree is the evidence behind the numbers of the paper.
It holds one directory for each engineering task that built or corrected the pipeline.
Every directory holds a `report.md`, and some hold the measurement files that the report cites.

These files are records, not instructions.
They keep the absolute paths, run identifiers and dates of the machine that produced the runs.
Do not read a command in a report as a command to run today.
`docs/REPRODUCTION.md` holds the commands that a reader runs.

The code of this repository is under `src/`, `tests/`, `schemas/` and `config/`.
Nothing in this tree is imported by the package, and the linter does not read it.
The one-off analysis scripts stay exactly as they ran.
Two readers still use files here.
`tests/test_ch3_routing.py` reads the recorded chapter 2 routing replay.
`docs/REPRODUCTION.md` replays a recorded calibration cassette.

## How to read one record

1. Open the directory row that matches the pipeline stage or paper result.
2. Read `report.md` first for the question, method, result, and limitations.
3. Open only the measurement files that the report cites.
4. Use commit identifiers and recorded hashes to connect the report to code and run artifacts.
5. Use `docs/REPRODUCTION.md` for current commands instead of copying a historical command.

## Common artifact types

| Name or suffix | Meaning |
| --- | --- |
| `report.md` | The main human-readable record of one task. |
| `*.json` | A measurement, replay result, manifest, receipt, or reviewed record. |
| `*.jsonl` | An ordered set of recorded requests, responses, or calibration rows. |
| `*.py` | A one-off analysis or replay script kept as it ran. |
| `*.md` beside a report | A review, cutover record, method note, or bounded contract. |

The one-off scripts are evidence, not package modules.
The project excludes this tree from Ruff so later formatting cannot change a recorded analysis.

## Chapter 2: the first production chapter

Chapter 2 produced the first corpus, the first candidate questions and the first yield audit.
The chapter 3 work corrects the faults that this audit found.

| Directory | What it records |
| --- | --- |
| `arctic-ch2-writer-context-r1/` | The generation stage: the writer context, the finding selection and the eligibility prompt. |
| `arctic-ch2-gates-r1/` | The deterministic gates of chapter 2, and the proof that they are not lax. |
| `arctic-ch2-core-routing-r1/` | The routing layer and the reason codes of chapter 2. |
| `arctic-ch2-corpus-r1/` | The frozen chapter 2 corpus, with the freeze receipt and the extraction quality summary. |
| `arctic-ch2-integration-run-r1/` | The integration of the four chapter 2 slices, and the execution gate of the run. |
| `arctic-ch2-timeout-recovery-r1/` | A provider timeout that halted the run, and the per-stage call timeout that answers it. |
| `arctic-current-yield-audit-r1/` | The yield audit of one frozen production cohort, with the cohort freeze script. |

## Chapter 3: the current pipeline

Chapter 3 is the pipeline that the paper describes.
Each slice below owns one stage of it.

| Directory | What it records |
| --- | --- |
| `arctic-ch3-writer-context-r1/` | The context bundle, the separable filter, the extractor prompt and the writer prompt. |
| `arctic-ch3-eligibility-r1/` | The eligibility contract, prompt v8 and response schema v4. |
| `arctic-ch3-gates-r1/` | The deterministic gates, the display rules, the reconstruction record and the calibration harness. |
| `arctic-ch3-judge-options-r1/` | The standalone judge, the satisfiability guard and the option stage. |
| `arctic-ch3-routing-r1/` | The routing and persistence slice, with the chapter 2 routing replay. |
| `arctic-ch3-cost-r1/` | The receipts-based cost measurements, with the projection, token-delta and shadow-replay scripts. |
| `arctic-ch3-integration-r1/` | The integration of the six slices, with the dry run, the cost projection and the replay reports. |

## Chapter 3 live runs

These reports come from the paid production runs.
Each one records a fault that stopped a run, the correction, and the live evidence.

| Directory | What it records |
| --- | --- |
| `arctic-ch3-production-run-r1/` | The first paid run, with HTTP rejection evidence and three cassettes, including the `v6` cassette used for reproduction. |
| `arctic-ch3-expansion-200-r1/` | The USD 200 expansion of the run, with the first measurement window. |
| `arctic-ch3-paper-cap-skip-r1/` | The per-paper cost cap, and the skip that keeps the producer alive. |
| `arctic-ch3-settle-not-submitted-r1/` | A double settlement between two workers of one shared ledger. |
| `arctic-ch3-candidate-fault-containment-r1/` | The rule that one faulty candidate never ends a whole run. |
| `arctic-broker-operation-lock-wait-r1/` | The operation lock of the shared ledger, and the wait that stops a starved producer. |
| `arctic-eval-authorization-r8/` | The evaluation bounds of the captain's allocation, the silent stop at a bound, and the phase of a refused request. |
| `arctic-ch3-count-error-retry-r1/` | A free token-count 503, its bounded retry, and the paper-level containment after retry exhaustion. |
| `arctic-ch3-paper-concurrency-r1/` | Several papers of one run in flight at once, the v11 request-rate policy, and the measured throughput before and after. |
| `arctic-ch3-concurrency-busy-r1/` | The exclusive operation lock as a queue under paper concurrency, the repair of the papers it refused, and the completion date of a paper. |
| `arctic-ch3-paper-completion-r1/` | The per-paper completion label, the one-time batch that labelled the finished papers, and the startup time before and after. |
| `arctic-geography-fix-r1/` | The geography correction, with the correction overlay and the successor gate drafts. |

## Abstention benchmark

The benchmark measures how often a model abstains when no listed option is correct.

| Directory | What it records |
| --- | --- |
| `arctic-abstention-eval-build-r1/` | The build of the evaluation harness. |
| `arctic-abstention-subscription-providers-r1/` | The Claude Code and Codex subscription providers, and their isolation flags. |
| `arctic-abstention-streaming-eval-r1/` | The concurrent 8-model plan, the streaming evaluator, and the omitted token-count incident in section 7. |
| `arctic-eval-503-release-r1/` | An HTTP 503 with no usage, and the reviewed release of that ambiguous charge. |
| `arctic-jev-prescreen-r1/` | The TypeSafe Jev prescreen: the calibration of the ranking against this run own outcomes, the cost of scanning the frozen corpus, the unpublished token limit the paid calls uncovered, and the first production measurement of the reordered pipeline. |
| `arctic-ledger-parallel-r1/` | The parallel bookkeeping store of the shared paid-call ledger: the measurement that attributed the per-call cost to durable writes on a rotating disk, the snapshot-plus-journal design, the live migration, the cutover to sixteen paper workers under policy v12, the adversarial audit and the state-database incident. |
| `arctic-ch3-concurrency-50-r1/` | Fifty papers in flight: the lock-hold measurement that attributed the 21-requests-a-minute cap to 2.44 s of serialised bookkeeping per paid call, the four rules that took one warm ledger read from 371 ms to 12 ms, the rollback defect the fifty-thread test found, policy v13 and the staged relaunch. |
| `arctic-eval-parallel-items-r1/` | Several questions scored at once: the measured rates per arm of the streaming evaluator at eight questions in flight, the captain's quota floors of 2026-09-17, and the extrapolation of the dataset to the 13:00 and 14:00 UTC deadlines. |
| `arctic-ch3-concurrency-75-r1/` | Seventy-five papers in flight for one night: policy v15, the tranche a rate transition names once the ceiling has moved, the measured window that kept or refused the rate, and the settled stop that ends generation at 13:45 UTC. |
| `arctic-eval-ledger-section-r1/` | The exclusive ledger section of the streaming evaluator: the receipts listing it kept exact because it said nothing about sharing the ledger, the whole-ledger start it ran once a question, the cut-over to snapshot 8477fd5 with a settled stop, and the idle Gemini arm the measurement window found. |

## Cost guard

The guard keeps the live benchmark inside the budget of the captain.

| Directory | What it records |
| --- | --- |
| `arctic-benchmark-guard-site-r1/` | The guard itself, and the live benchmarking section of the corpus viewer. |
| `arctic-benchmark-guard-codex-attribution-r1/` | A pause that flapped every five minutes, and the measured Codex share that corrects it. |

## Record and display changes

| Directory | What it records |
| --- | --- |
| `arctic-question-context-r1/` | The top-level `question_context` field of a candidate record. |
| `arctic-context-viewer-r1/` | The question context in the readable QA record. |
| `arctic-scope-layout-r1/` | The comparison-only projection of selected scope text. |
| `arctic-selectable-evidence-r1/` | The span identifiers that the model-facing payload exposes. |
