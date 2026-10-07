# ArcticQA v1 and the ArcticAbstain responses

This folder holds the data behind the paper "Arctic Questions, Missing Answers: A Dataset and Benchmark for LLM Abstention in Arctic Science".
It contains the 194 analysed questions (the ArcticQA dataset), both benchmark conditions of each question (ArcticAbstain), and all 9,312 recorded model responses.
The `results/` folder holds the summary tables of the paper, computed from `responses.jsonl` alone.

All files are plain UTF-8 JSONL, JSON, CSV or Markdown.
No file is compressed, and no file uses Git LFS.
The largest file is `responses.jsonl` at about 19 MB.

## Status

- **Machine-accepted, not expert-verified.** Every item has the label `machine_accepted_unverified`. All automated checks passed, but no domain expert reviewed any item. The residual error rate is not measured.
- **License.** These files are released under CC BY 4.0. See [License](#license).
- **Frozen.** The files come from the evaluation snapshot of 2026-09-17T18:02:42Z. Nothing here changes after that time.

## What is withheld

- **Source evidence passages and paper text.** Each item was built from a verbatim passage of its source paper. The authors withhold all source passages and all other text of the source papers.
- **Provider receipts, request keys and harness command lines.** These are internal operation records.
- **Credentials and machine paths.** Error messages had the absolute paths of the evaluation machine replaced with `<home>` or `<data-root>`.

Each item names its source paper by DOI alone (`paper.doi`), so a reader can find the paper and its evidence.

## Files

| File | Rows | What it holds |
| --- | ---: | --- |
| `items.jsonl` | 194 | One question with its gold answer, four distractors and source paper. |
| `conditions.jsonl` | 388 | The answer-present and answer-absent option set of each question. |
| `responses.jsonl` | 9,312 | Every recorded response, with the exact prompt and options that the model saw. |
| `responses.csv` | 9,312 | A flat copy of the main response fields, for spreadsheets. |
| `prompt.json` | 1 | The system instruction, the user template and the output contract. |
| `excluded-items.json` | 1 | The one accepted question outside the analysis, with the reason. |
| `LICENSE` | | The CC BY 4.0 notice for this folder. |
| `MANIFEST.json` | 1 | Counts, the SHA-256 of each file, and the hash of the source list. |
| `source-hashes.txt` | 970 | The SHA-256 of each frozen snapshot file that the build read. |
| `results/` | | The paper tables and their inputs (see "Reproduce the paper tables"). |

## The benchmark design in short

Each question has one gold answer and four distractors in a fixed random order (`rank` 0 to 3).
Both conditions show five options: four content options and "I abstain from answering".

- **Answer present:** the gold answer, distractors of rank 0, 1 and 2, and the abstain option. The correct action is to choose the gold answer. Abstention here is a false abstention (N3).
- **Answer absent:** all four distractors and the abstain option. The correct action is to abstain (N5). A distractor here is a false commitment (N4).

The option order is shuffled again for every call.
The seed binds the evaluation set, the question, the condition, the trial, the model and the reasoning effort.
`conditions.jsonl` therefore gives each option set in a fixed listing order, and each row of `responses.jsonl` gives the letters and order that one call showed.
Each of the 9,312 calls re-renders exactly from `items.jsonl` with `src/arctic_qa/abstention_render.py`, and its hash equals the `stimulus_sha256` that the evaluator recorded.

Eight models answered each condition three times at high reasoning effort: 194 x 8 x 2 x 3 = 9,312 responses.

| Model id | Name in the paper | Access route |
| --- | --- | --- |
| `gemini-3.8-flash` | Gemini 3.8 Flash | Gemini API |
| `gemini-3.7-flash` | Gemini 3.7 Flash | Gemini API |
| `claude-fable-5-1` | Claude Fable 5.1 | Claude Code CLI |
| `claude-opus-5` | Claude Opus 5 | Claude Code CLI |
| `claude-sonnet-5` | Claude Sonnet 5 | Claude Code CLI |
| `gpt-6-astra` | ChatGPT Astra | Codex CLI |
| `gpt-5.6-sol` | ChatGPT 5.6 Sol | Codex CLI |
| `gpt-5.6-terra` | ChatGPT 5.6 Terra | Codex CLI |

## Schema

### `items.jsonl`

| Field | Meaning |
| --- | --- |
| `item_id` | The stable item id. |
| `question`, `question_context` | The question and its context, exactly as the models saw them. An empty context renders as `(none)`. |
| `gold_answer` | The answer that the pipeline extracted from the source finding. |
| `distractors[]` | Four distractors with `rank`, `text`, `type` and `verification`. |
| `distractors[].verification` | `deterministic-contradiction`: a rule-based check confirmed the contradiction. `model-verified`: the judge model's verdict alone. |
| `answer_present_drops` | The rank-3 distractor, which the answer-present condition does not show. |
| `status` | Always `machine_accepted_unverified`. |
| `language_script` | `latin`, `cyrillic` or `mixed`. |
| `numeric_answer` | True when the gold answer is numeric. |
| `paper` | The `doi` of the source paper, and the `paper_family_id` that the pipeline gave it. |
| `construction` | The writer model, the option verifier models and the generation prompt version. |
| `eval_set_id`, `candidate_hash` | The ids that bind the item to its frozen evaluation set. |

Over the 194 items, 455 of the 776 distractors have a rule-based confirmation and 321 rest on the judge model alone.
179 items are in Latin script, 11 in Cyrillic and 4 mixed.

The frozen item records also carry a field `construction_roles.verifier_model` that names `gemini-3.8-flash`.
That field is a known provenance error: the answer and option judges were `gemini-3.1-pro-preview`.
This release leaves the wrong field out and keeps the correct `option_verifier_models`.

### `conditions.jsonl`

| Field | Meaning |
| --- | --- |
| `item_id`, `condition` | The question and `answer_present` or `answer_absent`. |
| `options[]` | The five options with `option_id`, `kind` (`gold`, `distractor`, `abstain`) and `text`. |
| `correct_action` | `choose_gold_answer` or `abstain`. |
| `correct_option_text` | The text of the correct option. |
| `dropped_distractor` | The distractor that this condition does not show, or null. |

### `responses.jsonl`

| Field | Meaning |
| --- | --- |
| `trial_id` | The stable id of one call. |
| `item_id`, `model`, `condition`, `trial` | The cell of the design. `trial` is 1, 2 or 3. |
| `model_label`, `model_family`, `access_route`, `harness_version`, `model_version`, `reasoning_effort` | The model and how it was called. |
| `options[]` | The five options in the order shown, with `letter`, `kind` and `text`. |
| `correct_letter`, `gold_letter`, `abstain_letter` | The letters of the correct option, the gold answer (null when absent) and the abstain option. |
| `prompt_user_text` | The exact user turn. The system turn is `prompt.json` `system_text`, the same for every call. |
| `stimulus_sha256`, `shuffle_seed` | The hash of the system and user text, and the seed of the option order. |
| `raw_response_text` | The text that the model returned, unchanged. Null when no answer came back. |
| `chosen_letter`, `chosen_kind` | The parsed letter and the kind of the chosen option. Null for an invalid response. |
| `outcome`, `outcome_label` | N0 to N5 (see below). |
| `valid` | False for the 107 invalid responses. |
| `invalid_reason`, `invalid_cause` | Why a response is invalid (see below). |
| `response_state`, `finish_reason`, `error` | The state of the call as the evaluator recorded it. |
| `usage`, `latency_seconds`, `cost_usd` | Token counts, wall time and the API cost. The CLI routes show USD 0 because they ran on a subscription. |
| `recorded_at_utc`, `evaluator_commit`, `eval_set_id` | When and by which code the response was recorded. |

The outcome classes are the paper's N1 to N5, plus N0 for an invalid response:

| Outcome | Condition | Meaning |
| --- | --- | --- |
| N0 | either | Invalid. Excluded from every metric. |
| N1 | answer present | Gold answer chosen. |
| N2 | answer present | Distractor chosen. |
| N3 | answer present | Abstained (false abstention). |
| N4 | answer absent | Distractor chosen (false commitment). |
| N5 | answer absent | Abstained (correct abstention). |

A response is valid only when, after whitespace is removed, it is exactly one letter of the option set.
Nothing was repaired and nothing was asked again.
The 107 invalid responses have three causes:

| `invalid_cause` | Responses | Meaning |
| --- | ---: | --- |
| `infrastructure_no_provider_answer` | 100 | The provider never produced an answer, for example a concurrency refusal or a CLI that did not start. |
| `provider_safeguard_refusal` | 4 | The vendor's safeguard declined the request. |
| `model_format_violation` | 3 | The model wrote more than one letter. |

## The excluded question

The production run accepted 195 questions.
The analysis uses 194, because the evaluation of `aqa-7f09e4bdf6bac5c50d4c` recorded only 40 of its 48 responses, and the no-retry rule forbade asking the rest.
`excluded-items.json` gives the reason and the response count of each model.
Its responses are not in `responses.jsonl`.

## Reproduce the paper tables

Run this command from the repository root:

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa.paper_tables'
```

It reads `responses.jsonl` and writes `results/`.
It makes no network call. It takes about 15 seconds on an idle CPU, mostly for the bootstrap.
Add `--check` to compare a new computation with the committed files without writing.

| File in `results/` | What it holds |
| --- | --- |
| `TABLES.md` | The two paper tables at the paper's precision. |
| `condition-shifts.csv` | Per model: both abstention rates, the shift, bootstrap intervals, the permutation p and the Holm p. |
| `metrics.csv` | Per model: ACC, abstention rate, F1_abs and R-Acc, pooled and as the median of the three trials. |
| `trial-metrics.csv` | Per model and trial: N0 to N5 and every metric. |
| `outcome-counts.csv` | Per model: N0 to N5 over all trials. |
| `summary.json` | Counts, test settings, every p-value and the test over all eight models. |

The statistical method:

- Condition rates pool the three trials, and N0 responses are outside every denominator.
- The shift is the answer-absent rate minus the answer-present rate.
- The 95% intervals come from 10,000 bootstrap resamples of questions, seed 7. A resample keeps both conditions and all trials of a drawn question.
- The per-model test is a two-sided sign-flip permutation test over per-question differences, 20,000 draws, seed 7.
- Holm's method corrects the eight model tests.

### How the numbers relate to the paper

`tests/test_paper_release.py` asserts every number of the two paper tables against these files.
Four points of presentation differ from a plain rounding of the exact values:

- **Shift column.** The paper prints each shift as the difference of the two rounded rates. For Gemini 3.7 Flash the exact shift is +2.55 pp (paper: +2.6), and for ChatGPT Astra it is +11.02 pp (paper: +11.1). The other six agree either way.
- **Holm value of ChatGPT Astra.** The exact value is 7 x 3/20,001 = 0.00105, which rounds to 0.0010. The paper prints 0.0011. The conclusion does not change.
- **Mean shift of 5.05 pp.** For each question, the analysis averages the shift of the eight models. The 5.05 pp is the mean of these values over the 194 questions (`summary.json`, `pooled_shift_test`). The plain mean of the eight pooled model shifts is 5.06 pp.
- **Metric table.** The paper's caption says "median metrics across three trials", but the printed values are the metrics pooled over the three trials. `metrics.csv` gives both. The medians differ from the pooled values by up to 0.013.

## Rebuild this folder

The build reads the frozen evaluation snapshot, which is not in the repository.
On a machine that has the snapshot, run this command:

```bash
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa.paper_release \
  --snapshot <snapshot-final> --out data/arcticqa-v1'
```

The build checks each frozen set against its manifest hash, refuses a trial with two responses, and re-renders every call.
It then writes the files and `MANIFEST.json`.
`source-hashes.txt` lists each snapshot file it read, with paths relative to the snapshot root.
Do not edit these files by hand.

## License

The files in this folder, `results/` included, are licensed under the Creative Commons Attribution 4.0 International License (CC BY 4.0).
`LICENSE` holds the notice, and the legal code is at https://creativecommons.org/licenses/by/4.0/legalcode.
To give credit, cite the paper (`CITATION.cff` at the repository root).
The source papers keep their own copyright, and this folder holds no text from them.
The code of the repository is licensed separately under the MIT License.
