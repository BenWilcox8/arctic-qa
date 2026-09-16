# Abstention evaluation harness: build report

Task: `arctic-abstention-eval-build-r1`.
Branch: `fm/arctic-abstention-eval-build-r1` from `main` at `d2f84e7`.
Date: 2026-09-16 UTC.

## 1. What was built

The harness measures how often a model abstains when no listed option is correct.
It follows the locked decisions of the captain (2026-09-16) and the design report `arctic-abstention-eval-design-r1`, section 8.
The document `docs/ABSTENTION_EVALUATION.md` holds the design, the run procedure, and the decisions with the captain's words.

| Part | Module | Note |
| --- | --- | --- |
| Distractor order | `src/arctic_qa/distractor_order.py` | Contract `distractor-order-v1`. A seeded pseudo-random permutation with its recorded seed. Generation writes it into `candidate["distractor_order"]`. The exporter and the live snapshot apply it. An older candidate gets a deterministic assigned order keyed on the item id, recorded in the set manifest. |
| Evaluation set builder | `src/arctic_qa/abstention_set.py` | Populations `current`, `production`, `list`. Reads the state database read-only. Keeps the first k accepted distractors of the order. Excludes and lists items with fewer than k. Freezes `items.jsonl` and `manifest.json` with hashes. One item per paper family. |
| Renderer | `src/arctic_qa/abstention_render.py` | Prompt `abstention-eval-prompt-v1`. Both conditions show k+1 options. Gold-present drops the last ordered distractor. All options, the abstention option included, are shuffled with a recorded seed per set, item, condition, and repeat. Strict single-letter parser. N0 to N5 mapping. |
| Providers | `src/arctic_qa/abstention_providers.py` | `EvaluationProvider` interface. `GeminiBrokerEvaluationProvider` sends one isolated broker call per trial with enum-constrained output. `ScriptedTransport` and `ScriptedEvaluationProvider` answer offline. `list-models` enumeration. |
| Runner | `src/arctic_qa/abstention_run.py` | One call per item, condition, model, arm, and repeat. Appends `responses.jsonl`. Resumes from that file and from receipts. Stops on the first budget stop or ambiguous charge. The dry run builds a private ledger and runs the broker path with the scripted transport. |
| Scorer | `src/arctic_qa/abstention_score.py` | N0 to N5 tallies, the seven metrics, the narrow recall, the ideal pair rate, the random baseline, the paired item bootstrap, the contamination table, and the strata table. CSV and LaTeX output. |
| Broker | `src/arctic_qa/model_broker.py` | Phase `benchmark_evaluation`, stage family `evaluation_answer:<model>`, evaluation policy, price config and gate, run binding, phase split of every total. |
| CLI | `src/arctic_qa/abstention_cli.py` | `abstention-eval --action build-set, render, dry-run, run, canary, score, list-models, gate-template`. |

## 2. Contract versions added

| Contract | Value |
| --- | --- |
| Distractor order | `distractor-order-v1`, field `distractor_order` on every new candidate |
| Evaluation set | `abstention-eval-set-v1` |
| Prompt | `abstention-eval-prompt-v1`, abstention option `I abstain from answering` |
| Run manifest | `abstention-eval-run-v1` |
| Scores | `abstention-eval-scores-v1` |
| Ledger phase | `benchmark_evaluation` |
| Ledger stage family | `evaluation_answer:<model>` |
| Evaluation policy | `benchmark-evaluation-policy-v1`, file `config/benchmark-evaluation-policy-v1.json`, ceiling USD 5.00 |
| Evaluation prices | `benchmark-evaluation-price-config-v1`, file `config/benchmark-evaluation-prices-v1.json` |
| Evaluation gate | `benchmark-evaluation-execution-gate-v1`, disabled template `config/benchmark-evaluation-execution-gate-v1.json` |
| Population contract | `config/abstention-eval-chapter2-contract-v1.json` (schema 2.7.0, prompt v22) |

The construction price config `config/gemini-eligibility-v1.json` is unchanged.
No price transition is needed on the production ledger.
The generation gates and prompts are unchanged except for the new `distractor_order` field.

## 3. Metering design

Evaluation calls share the one ledger and the lifetime ceiling.
They never mix with construction accounting:

- The status record reports evaluation spend in `usage.benchmark_evaluation_usd` and in a new `evaluation` block.
- `dataset_construction_usd`, `away_session_usd`, the checkpoint, and the away submission count exclude evaluation requests.
- `project_lifetime_usd` includes both phases, and the lifetime ceiling applies to both.
- Each evaluated item is its own ledger paper family (`evaluation-item:<item_id>`), so construction family rows and the per-paper cap never see evaluation spend.
- The evaluation policy holds the ceiling, the request cap, the per-item repeat limit, the pace, and the retry ban.
- The gate binds the set manifest hash, the prompt hash, the abstention text, the models, the arms, the decoding record, the repeat limit, one run id, the policy hash, and the price config hash.
- The request key binds the run id and the trial id, so repeats of one identical stimulus never collide.

The production ledger (14757 receipts, 13 transitions) validates with the new code.
Its status shows `integrity_valid: true`, `benchmark_evaluation_usd: 0`, and every construction total unchanged.

## 4. Model enumeration

`list-models` reached 58 models with the configured key on 2026-09-16 (free `models.list` call).
The text models that support `generateContent`:

| Model | Pro | Priced here | Thinking control | Note |
| --- | --- | --- | --- | --- |
| gemini-3.1-pro-preview | yes | yes (2.00 / 12.00) | `thinkingLevel` low, medium, high | Preview. Judged every chapter 2 item and every option. |
| gemini-2.5-pro | yes | no | `thinkingBudget` (older API) | Stable. No construction role. Needs a budget-based price entry before it can run. |
| gemini-pro-latest | yes | no | alias | Not pinnable to one revision. Not usable for a paper claim. |
| gemini-3.1-pro-preview-customtools | yes | no | tools variant | Not an evaluation target. |
| gemini-3.8-flash | no | yes (0.75 / 3.75) | `thinkingLevel` | Wrote every production item. |
| gemini-3.5-flash | no | yes (1.50 / 9.00) | `thinkingLevel` | No construction role. |
| gemini-3.1-flash-lite | no | yes (0.25 / 1.50) | `thinkingLevel` | Answer-agreement judge for some items. |
| gemini-3.5-flash-lite | no | yes (0.30 / 2.50) | `thinkingLevel`, supports minimal | No construction role. |
| gemini-3.6-flash, gemini-3.7-flash, gemini-3-flash-preview, gemini-2.5-flash, gemini-2.5-flash-lite, gemini-3.1-flash-lite-preview | no | no | mixed | Reachable, unpriced. |

The other Pro names are image, TTS, music, or deep-research models and are not evaluation targets.

Recommendation for a useful, cost-bounded paper claim:

1. Run `gemini-3.1-pro-preview` first. It is the only Pro variant with `thinkingLevel` presets and a verified price entry. It is also the construction judge, so its result carries the target-adaptive retention risk. The contamination table reports that.
2. Add `gemini-2.5-pro` as the second Pro variant. It has no construction role and is a stable release, so it is the cleanest Google control for the family effect. It needs a `thinkingBudget` price entry and one small provider extension (deferred item 2).
3. Keep the non-Pro models priced but off until the captain has cost data. The design report's cost estimate stands: about USD 0.03 per Pro call at the high preset, and the canary below measures the medium preset.

## 5. Dry run

The dry run built a private ledger and ran the whole path with the scripted transport:

| Item | Value |
| --- | --- |
| Set | `abstention-eval-set-4d3202cf27b30859` (5 current-contract items, k = 4) |
| Plan | 2 models x 2 arms x 2 conditions x 2 repeats x 5 items = 80 calls |
| Wall time | 11 s |
| Ledger | `benchmark_evaluation_usd 0.047840`, `dataset_construction_usd 0`, `project_lifetime_usd 0.047840` |
| Scores | random baseline ACC 0.200, SSR 0.300; all eight table files written |

## 6. Canary

Authorization: firstmate inbox message 001 (2026-09-16T04:37:01Z), the captain's price-gauge authorization.
Run id `abstention-canary-r1`, commit `9717864`, gate and review record under `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/private/`.
Run directory: `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/runs/canary-r1/` (`responses.jsonl`, `run-summary.json`, `scores/`).
Plan: the 5 current-contract items, both conditions, `gemini-3.1-pro-preview`, preset `medium`, temperature 2.0, one repeat, enum output, ceiling USD 5.00, on the production ledger.

| Measure | Value |
| --- | --- |
| Calls | 10 of 10 planned, all completed, no ambiguous charge, no budget stop |
| Total cost | USD 0.060676 |
| Cost per call | USD 0.006068 (min 0.002646, max 0.009290) |
| Prompt tokens per call | 216.2 |
| Thinking tokens per call | 468.6 (min 186, max 739) |
| Output tokens per call | 1.0 |
| Latency per call | mean 14.5 s, median 14.2 s, max 17.6 s |
| Invalid rate (N0) | 0 of 10 |
| Wall time | 2 min 39 s at the 10-per-minute pace |
| Ledger after the run | `benchmark_evaluation_usd 0.060676`, `dataset_construction_usd 54.103785` (unchanged), `project_lifetime_usd 54.164461`, evaluation remaining USD 4.939324 |

Outcomes: N1 3, N2 1, N3 1, N4 0, N5 5.
Point estimates on 5 items (not a paper result): ACC 0.80, Precision_abs 0.83, Recall_abs 0.83, F1_abs 0.83, Abstention Rate 0.60, R-Acc 0.75, SSR 0.90.
The model abstained in all five gold-absent trials and answered the gold option in three of five gold-present trials.

Price projection from the canary at the medium preset: about USD 0.006 per call, so USD 0.012 per item for both conditions and one repeat.
At that rate USD 5.00 covers about 400 item-condition pairs, and 500 items x 2 conditions x 4 repeats cost about USD 24 per Pro model per arm.
The high preset will think for longer; measure it with one more canary before the large run.

## 7. Tests

New test modules: `tests/test_abstention_render.py`, `tests/test_abstention_broker.py`, `tests/test_abstention_run.py` (22 tests).
They cover the stable order, the equal option count, the drop rule, the shuffle, the strict parser, the N0 filing, the ledger phase separation, the ceiling, the reserve, the repeat limit, the gate binding, the payload rules, the dry run through the broker, the resume, the scoring formulas against both random baselines, the CLI, and the offline model listing.

Suite runs (`nix develop -c bash -c 'PYTHONPATH=src pytest ... -q'`), all green:

| Part | Result |
| --- | --- |
| New abstention modules | 22 passed |
| `test_broker_provider`, `test_publication_export`, `test_cli_integration` | 110 passed |
| `test_model_broker` | 86 passed |
| every other module except `test_streaming` | 945 passed |
| `test_streaming` | 56 passed (after the pacing signature fix) |

One regression appeared and was fixed: `test_streaming` patches `_pace` with a one-argument function, so the phase is now passed through an attribute instead of a parameter.

## 8. Deferred items

1. The `thinkingBudget` model family (`gemini-2.5-pro`) needs a budget-based price entry type and a provider branch.
2. A second, non-Google control model needs a new credential and a provider implementation.
3. The exporter keeps validation order for candidates without an order field. The set builder assigns the order for those.
4. The paired item bootstrap does not yet bootstrap the difference between two models.
5. The rate window of the ledger is shared between phases.
