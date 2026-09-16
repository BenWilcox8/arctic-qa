# Configuration

This directory contains versioned inputs that define a run.
The files cover prompts, model roles, budgets, prices, evaluation plans, and reviewed execution gates.

Most files are immutable contracts.
A receipt stores their hashes, so an edit can invalidate a reviewed run.
Create a new version when a contract changes.

Historical files can contain paths from the machine that produced the paper.
Treat those paths as evidence, not as instructions for a new checkout.
Use the environment variables and command options in [the reproduction guide](../docs/REPRODUCTION.md).

## Dataset construction

| File | Purpose |
| --- | --- |
| `arctic-eligibility-policy-v3.json` | The five scientific eligibility criteria and their decision rules. |
| `article-access-policy-v1.json` | The rules for locating and accepting readable article text. |
| `metadata-prefilter-policy-v1.json` | The conservative metadata-only filter before source access. |
| `source-screening-policy-v1.json` | The bounded source-screening policy and selection limits. |
| `gemini-eligibility-v1.json` | Model, price, token, timeout, and safety values for eligibility calls. |
| `roles.v1.json` | The allowed model profiles and the mapping from a pipeline role to a profile. |
| `streaming-dataset-budget-policy-v1.json` | Construction ceilings, submission limits, and per-paper cost limits. |
| `streaming-live-execution-gate-v1.json` | The reviewed authorization for one specific live construction run. |
| `live-dataset-current-contract-v1.json` | The schema and prompt versions that define the current accepted population. |

## Eligibility prompts

| File | Purpose |
| --- | --- |
| `gemini-eligibility-prompt-v1.txt` | The first eligibility prompt. |
| `gemini-eligibility-prompt-v2.txt` | The second eligibility prompt. |
| `gemini-eligibility-prompt-v3.txt` | The third eligibility prompt. |
| `gemini-eligibility-prompt-v4.txt` | The fourth eligibility prompt. |
| `gemini-eligibility-prompt-v5.txt` | The fifth eligibility prompt. |
| `gemini-eligibility-prompt-v6.txt` | The sixth eligibility prompt. |
| `gemini-eligibility-prompt-v7.txt` | The seventh eligibility prompt. |
| `gemini-eligibility-prompt-v8.txt` | The current eligibility prompt for chapter 3. |
| `gemini-eligibility-geography-rescreen-v1.txt` | The first bounded prompt for a geography-only re-screen. |
| `gemini-eligibility-geography-rescreen-v2.txt` | The current geography-only re-screen prompt. |

Older prompts remain because run manifests and receipts bind their exact bytes.
`gemini_eligibility.py` selects the prompt that its run contract names.

## Abstention benchmark

| File | Purpose |
| --- | --- |
| `abstention-eval-chapter2-contract-v1.json` | The chapter 2 population contract for building a frozen evaluation set. |
| `benchmark-evaluation-plan-high-v1.json` | Eight models, two conditions, and three repeats for 48 trials per item. |
| `benchmark-evaluation-policy-v1.json` | The first bounded canary policy. |
| `benchmark-evaluation-policy-v2.json` | The concurrent vendor policy with the same canary ceiling. |
| `benchmark-evaluation-policy-v3.json` | The reviewed USD 200 Gemini evaluation ceiling. |
| `benchmark-evaluation-prices-v1.json` | Billable Gemini model prices for the shared broker. |
| `benchmark-evaluation-list-prices-v1.json` | Informational API-price equivalents for subscription calls. |
| `benchmark-evaluation-subscription-models-v1.json` | Harness binaries, model identifiers, and allowed reasoning presets. |
| `benchmark-evaluation-execution-gate-v1.json` | The reviewed authorization for one specific evaluation run. |
| `benchmark-evaluation-model-pause-v1.json` | The control file that keeps selected model trials pending. |

## How these files control a paid call

1. The command selects a policy, a price file, and a run identifier.
2. A reviewed gate binds those values, the code commit, and the input files by hash.
3. The shared broker validates all bindings before it reserves money.
4. The provider receives the request only after the broker admits it.
5. The broker stores the same file hashes in the immutable receipt.

The evaluation gate uses the subscription-model registry in its price-config hash slot.
Another machine can pass its own registry or binary path without changing the historical file.

## Pause-file exception

The model pause file is a live control interface, not an immutable research contract.
The evaluator reads it before every item.
A paused trial remains pending and receives no invalid score.

Read [Abstention evaluation](../docs/ABSTENTION_EVALUATION.md) before you edit this file.
Read [Benchmark guard](../docs/BENCHMARK_GUARD.md) before a guard writes it.
