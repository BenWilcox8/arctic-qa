# Abstention evaluation

This document describes the abstention evaluation harness of ArcticQA.
The harness measures how often a model abstains when no listed option is correct.
It reuses the metrics of the previous abstention paper.
It does not build a separate abstention dataset.
Abstention is part of the evaluation of every item.

## Decisions

The captain locked these decisions on 2026-09-16.
The decision record is `data/arctic-abstention-eval/decisions.md` in the firstmate data folder.
The exact words of the captain are quoted here.

1. Distractor order.
   "Order the existing distractors at random but make sure that they're still ordered. And then drop the last one at test time."
   Every item carries one fixed random distractor order with its seed.
   The gold-present condition drops the LAST distractor of that order.
   There is no confidence measure and no rotation.
   "Let's do no rotation. It's fine, because it's just random."
2. Conditions.
   Each item runs as a pair: gold-present and gold-absent.
   "yes, as a pair, but in different sessions".
   Both conditions show the same number of options.
3. Option order.
   "Order should be shuffled randomly."
   All displayed options, the abstention option included, are shuffled with a recorded seed.
4. Abstention wording.
   "wording should be "I abstain from answering"".
5. Output contract.
   "it should be single letter scoring with very clear prompting".
   "Scrap the reask. If the model doesn't output a single character, then just file that away for later revision".
   A response that is not exactly one option letter is invalid (N0).
   The raw response is stored, the trial is excluded from the metrics, and the N0 rate is reported per model.
6. Decoding.
   "Lets use the max temperature, and the reasoning will vary based on the official presets."
   "Lets start with medium reasoning to get a gauge on price".
   The temperature is the API maximum for the model and is recorded per run.
   The thinking level is an official preset and each preset is a separate arm.
   Repeats are a run parameter.
7. Scope.
   "build and test the infrastructure for a large run, but I will actually conduct the large scale run once the entire dataset is built".
8. Providers and models.
   "my budget is specifically in Google API credits, so I can only use Google AI models for the evaluation. But leave it open".
   "Let's only use pro models right now, but enumerate all of the different variants that I have access to".
9. Framing.
   "Let's focus on abstention specifically, so stick with what I previously had told you rather than saying no answer is scientifically defensible".

## Design

### Items and distractor order

A benchmark item is one accepted candidate with its gold answer and its accepted distractors.
The module `src/arctic_qa/distractor_order.py` defines the order contract `distractor-order-v1`.
The order is a seeded pseudo-random permutation of the distractor texts.
Generation records the order in the candidate field `distractor_order` with its seed.
The exporter and the live snapshot show the accepted distractors in that order.
An older candidate has no order field.
The evaluation set builder assigns the order with a seed keyed on the item id.
The manifest lists which items received an assigned order.

### Evaluation set

The module `src/arctic_qa/abstention_set.py` freezes one evaluation set.
It reads the state database read-only.
It selects items by population: `current`, `production`, or `list`.
The `current` population uses a contract file with the candidate schema, the generation prompt, and the scope contract.
The `production` population uses every accepted candidate whose run id starts with the production campaign prefix.
One item per paper family enters the set.
The builder keeps the first k accepted distractors of the order.
An item with fewer than k accepted distractors is excluded and listed with its reason.
The set directory holds `items.jsonl` and `manifest.json` with the item hashes.

### Conditions and rendering

The module `src/arctic_qa/abstention_render.py` renders one trial.
A trial is one item, one condition, one model, one thinking arm, and one repeat.
The gold-present condition shows the gold answer, the first k-1 distractors, and the abstention option.
The gold-absent condition shows all k distractors and the abstention option.
Both conditions show k+1 options.
The option order is a seeded permutation.
The seed depends on the set, the item, the condition, and the repeat.
Every model sees the same stimulus for the same item, condition, and repeat.
The system instruction asks for exactly one uppercase letter.
The user content shows `QUESTION`, `QUESTION_CONTEXT`, and `OPTIONS` as separate blocks, as `docs/BENCHMARK_INPUT_CONTRACT.md` requires.
The prompt version is `abstention-eval-prompt-v1`.
The prompt hash binds the templates and the abstention text into the gate.

### Output contract

The Gemini provider asks for enum-constrained output with `text/x.enum`.
The enum holds the option letters of the trial.
The parser accepts only one uppercase letter from that set.
A different text, an empty response, or a finish reason other than `STOP` is invalid.
An invalid response is recorded as N0 with the raw text and the reason.
There is no re-ask.

### Isolation

One provider call answers one trial.
Each call is its own session with no history, no tools, and no provider storage.
The broker stores every request and response as an immutable receipt.
The request key binds the model, the item identity, the exact payload, the run id, and the trial id.
Two repeats of one identical stimulus never collide.

### Metering

Evaluation calls go through the shared paid-call ledger.
They run under the phase `benchmark_evaluation` and the stage family `evaluation_answer:<model>`.
The phase has its own budget policy, price config, and execution gate:

- `config/benchmark-evaluation-policy-v1.json` holds the evaluation ceiling and the operational controls.
- `config/benchmark-evaluation-prices-v1.json` holds one price entry, the thinking presets, and the pinned temperature of each evaluated model.
- `config/benchmark-evaluation-execution-gate-v1.json` is the disabled gate template.

The broker reports evaluation spend in `usage.benchmark_evaluation_usd` and in the `evaluation` block of the status record.
Evaluation spend counts toward the project lifetime ceiling.
It never counts toward the construction totals, the away ceiling, the checkpoint, or the per-paper cap.
Each evaluated item is its own ledger paper family.
A per-item repeat limit replaces the per-paper construction cap.
The construction price config and its transition chain stay unchanged.
The evaluation policy and price config are bound into the gate by hash.
A changed ceiling needs a new policy file and a new reviewed gate.

The gate binds the evaluation set manifest hash, the prompt version and hash, the abstention text, the model list, the arms, the decoding record, the repeat limit, and one run id.
The broker validates the gate and the binding before it reads the credential and before every request.
A changed manifest, prompt, model list, arm, decoding record, or run id stops before `countTokens`.

### Providers

The module `src/arctic_qa/abstention_providers.py` defines the provider interface.
The Google Gemini implementation sends every call through the shared broker.
The scripted transport answers by model and payload hash with no paid call.
Another provider implements the same interface and registers its price entries.

### Scoring

The module `src/arctic_qa/abstention_score.py` scores one run.
It tallies N0 to N5 per model, condition, and arm.
It computes ACC, Precision_abs, Recall_abs, F1_abs, Abstention Rate, R-Acc, and SSR.
It also reports the narrow recall `N5 / (N4 + N5)` and the ideal pair rate.
The random baseline is a uniform guess over k+1 options in both conditions.
For k = 4 the baseline is ACC 0.200, Precision 0.500, Recall 0.125, F1 0.200, Abstention 0.200, R-Acc 0.125, SSR 0.300.
The test suite also reproduces the baseline of the previous paper (five and four options).
Confidence intervals come from a paired item bootstrap with a fixed seed.
The scorer writes `scores.json`, `main-table.csv`, `main-table.tex`, `condition-table.csv`, `letters.csv`, `contamination.csv`, `contamination.tex`, and `strata.csv`.
The contamination table shows the construction role of each evaluated model over the evaluated items.

## Procedure

Run every command from the repository root inside `nix develop`.
Prefix each command with `PYTHONPATH=src python -m arctic_qa --json abstention-eval`.

1. Build the evaluation set.

   ```bash
   --action build-set --state-db /mnt/crdata/research-abstention/arctic-qa/state.sqlite3 \
     --output-dir <sets-dir> --population current \
     --contract-file config/abstention-eval-chapter2-contract-v1.json
   ```

   Read `manifest.json`. Make sure that the item count and the exclusions are correct.
2. Render the trials and read one prompt.

   ```bash
   --action render --eval-set-dir <set-dir> --models gemini-3.1-pro-preview --arms medium --repeats 1
   ```

3. Run the dry run and its scores.

   ```bash
   --action dry-run --eval-set-dir <set-dir> --run-dir <dry-dir> --run-id <id> \
     --models gemini-3.1-pro-preview --arms medium --repeats 1 --scripted-policy random
   ```

   The dry run builds a private ledger in `<dry-dir>/ledger` and makes no paid call.
4. Write the gate template.

   ```bash
   --action gate-template --eval-set-dir <set-dir> --run-id <id> --models <models> --arms <arms> \
     --repeats <n> --review-record <review-file> --output-file <private-gate>
   ```

   An independent reviewer sets `independent_review_verdict` to `pass` and `evaluation_enabled` to `true`.
   The gate binds the code commit of the reviewed revision.
5. Run the paid evaluation.

   ```bash
   --action run --eval-set-dir <set-dir> --run-dir <run-dir> --run-id <id> \
     --models <models> --arms <arms> --repeats <n> \
     --evaluation-gate-file <private-gate> \
     --shared-ledger-file <ledger> --model-receipts-dir <receipts> \
     --streaming-budget-policy-file <active-construction-policy> \
     --price-config-file <active-construction-price-config> \
     --execution-gate-file <active-construction-gate> \
     --ledger-config-transition-file <active-transition> \
     --credential-file <key>
   ```

   The construction files must be the files that the production ledger already binds.
   The run appends one row per trial to `responses.jsonl`.
   A rerun resumes from that file and from the receipts.
   A budget stop or an ambiguous charge ends the run.
6. Score the run.

   ```bash
   --action score --run-dir <run-dir> --bootstrap 2000 --bootstrap-seed 7
   ```

### Canary

The action `canary` runs the current-contract items on one Pro variant.
It pins the `medium` preset and one repeat.
It refuses a model that is not a Pro variant.
It refuses an evaluation policy with a ceiling above USD 5.00.
It reports the cost per call, the tokens with thinking, the latency, and the invalid rate.
The canary runs only after the captain's price-gauge authorization.

### Model enumeration

The action `list-models` calls the free `models.list` endpoint.
It marks the Pro variants and joins each model with its local price entry and presets.
Pass `--models-file` with a saved response to run it offline.

## Limits

- The evaluation policy raises the per-minute pace only in the dry run. A paid run keeps the pace of the policy file.
- The rate window of the ledger is shared between phases. A construction run and an evaluation run pace each other.
- Only Gemini 3 models with `thinkingLevel` presets have price entries. A `thinkingBudget` model such as `gemini-2.5-pro` needs a new entry type before it can run.
- Every current item was written by `gemini-3.8-flash` and judged by `gemini-3.1-pro-preview`. The contamination table makes this visible. It does not remove the effect.
