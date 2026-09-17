# Jev prescreen: rank the retained papers before the pipeline opens them

This document is the contract for `src/arctic_qa/jev_prescreen.py` and the command `python -m arctic_qa jev-prescreen`.
The prescreen asks a cheap typed-judgement model, TypeSafe Jev, a small batched question set over each article's own full text.
It turns the answers into one ranking probability per paper.
The result is an order, not a decision: the ranking removes no paper and changes no eligibility verdict.

CAUTION: The prescreen never touches the shared paid-call ledger, the producer, the evaluator or the chapter 3 state.
It keeps its own append-only call ledger with its own USD ceiling.
`docs/SHARED_MODEL_BROKER.md` governs the pipeline's money; this document governs only the prescreen's own.

## 1. Why an order is worth money

The pipeline pays a full-text eligibility call on every paper it opens.
In run `chapter3-7dc6485-r3`, eligibility and finding extraction were 72.7% of the run cost.
Of the 276 papers the run labelled, 21 produced an accepted question.
117 ended at eligibility and 132 ended at generation.

A paper that fails eligibility still costs a paid call of about 24,000 input tokens.
That call cost USD 0.0226 on average in run r3.
A Jev call over the same article sends about 19,800 input tokens at a price 18 times lower per token, which is USD 0.0008.
So the prescreen costs about one twenty-seventh of the eligibility call it defers.

## 2. The corpus this runs on

The metadata pre-filter retained 16,339 papers with the disposition `retained_article_type`.
4,420 of those have extracted full text on disk today.
Every one of the 4,420 is inside the 16,339, and they are the frozen corpus `full-text-ready-4420-seed20260912-r1`.

The other 11,919 have no text on disk.
They are the `excluded_other_access_candidates` of the freeze receipt.
A prescreen cannot read them, because there is no article to read.
`article-access-r1` is the stage that would fetch them, and that fetch is a separate task with its own cost and its own licence questions.

So the prescreen runs on the 4,420 first.
`--action build-manifest` still writes a row for all 16,339, with `has_full_text` false on the rest, so the papers that need a fetch stay visible and counted.

## 3. What Jev is asked

One `POST https://api.typesafe.ai/v1/systemone` per paper.
The `state` is the article text alone, as a plain string.
No title, no DOI and no metadata go into the state, so the score depends on the article's own words only.
All eight questions are batched into that one call.

Batching is what makes this cheap.
The document dominates the request, so eight separate calls would pay for the article eight times.
The provider's own cookbook measures 13 batched questions as 12.2 times cheaper than 13 single-question calls, with no change in the answers.

Jev returns no text.
It returns a probability for a yes/no question (`noul`), a probability per option for a `choice`, and a probability-weighted level for a `score`.
Every question below is answerable in one of those three shapes.

### The gates

A gate is a near-necessary condition.
A paper that fails one cannot produce a question at all.

| Key | Type | Weight | What it predicts | Papers lost to it in run r3 |
| --- | --- | --- | --- | --- |
| `geography_status` | choice, 3 options | 2.0 | `criterion_failed:study_geography`, `criterion_unresolved:study_geography`, `criterion_evidence_missing:study_geography` | 60 |
| `article_type` | choice, 3 options | 1.5 | `criterion_failed:published_primary_findings` | 21 |
| `reports_own_finding` | noul | 1.5 | `no_admissible_finding` | 3 |
| `extraction_complete` | noul | 1.0 | the readable-text floor | — |

`geography_status` is the largest single lever in the whole taxonomy.
Its three options copy the pipeline's own geography decision procedure in `config/gemini-eligibility-prompt-v8.txt`.
`arctic_activity_stated` is the pass case.
`outside_or_incidental` is the fail case of rule 2.
`not_stated` is the absence-of-evidence case of rule 5, which the pipeline records as `unresolved`.
An unresolved paper costs the same paid call as an excluded one and yields the same nothing, so the prescreen treats both as losses.

### The quality terms

A quality term separates the papers that pass the gates.

| Key | Type | Weight | What it predicts | Papers lost to it in run r3 |
| --- | --- | --- | --- | --- |
| `finding_not_figure_dependent` | noul | 2.0 | `finding_span_figure_defined_referent` | 36 |
| `scope_bound_with_finding` | noul | 2.0 | the scope-unsourced family, and `slot_evidence_unavailable` | 33 |
| `self_contained_claim` | score, 4 levels | 1.0 | `standalone_det_question_context_referent_unresolved` | 5 |
| `arctic_attributed_finding` | noul | 0.5 | `eligible_arctic_scope_missing_from_finding` | 13 |

`finding_not_figure_dependent` is the largest generation-stage code of the run.
The pipeline refuses a finding whose referent only a figure or a table caption defines (`src/arctic_qa/generation.py`, `finding_span_figure_defined_referent`).
`scope_bound_with_finding` covers `finding_scope_value_unsourced`, `eligible_arctic_scope_finding_unbound`, `scope_qualifier_missing`, `scope_qualifier_not_source_bound`, `answer_scope_not_source_bound` and `reconstruction_scope_not_source_bound`, which are 24 papers together, plus 9 more at `slot_evidence_unavailable`.
`self_contained_claim` is the captain's own phrase in question form.

### What the prescreen deliberately does not ask

Some rejection codes are properties of the model's output, not of the article.
A reader of the article cannot predict them, so a question about them would add cost and noise and no information.

| Code | Papers | Why it is not asked |
| --- | --- | --- |
| `eligible_arctic_scope_invalid` | 28 | The eligibility model's own JSON is malformed. A verified example passed all four criteria and still failed on a duplicate `span_id`. |
| `extractor_response_invalid` | 23 | The extractor's provider call failed. This is a reliability event of the provider. |
| `option_set_not_mutually_exclusive`, `option_set_verdict_missing` | 5 | These judge the distractors the writer model produced. |
| `alternative_finding_not_distinct`, `reconstruction_alternative_answer_present` | 7 | These depend on the generated question text. |

The 276 labelled papers of the run account for exactly:

| Group | Papers |
| --- | --- |
| Loss the gate questions name | 84 |
| Loss the quality questions name | 87 |
| Loss the prescreen does not ask about, in the table above | 63 |
| A tail of single-figure codes, model-side or layout-side | 21 |
| Accepted | 21 |
| Total | 276 |

So the prescreen's questions name 171 of the 255 losses.

`standalone_det_question_context_referent_unresolved` is the one borderline entry among the 87.
It depends on the question the writer model produced, so it is not fully predictable from the article.
It is still asked, because an article whose finding sentence leans on undefined pronouns, acronyms or local jargon is more likely to trigger it, and the question costs almost nothing inside the batch.
Its weight is 1.0, half the weight of the two questions with the largest counts behind them.

## 4. How the answers become one probability

`rank_probability` in `jev_prescreen.py` holds this arithmetic.
Every input is a probability in the range 0 to 1.
A `noul` answer is that probability already.
A `choice` answer gives the mass its distribution puts on the question's good options.
A `score` answer is the probability-weighted level divided by the top level index.

The gates combine as a weighted geometric mean:

```
gate = product over gates of p_i ** (w_i / sum of gate weights)
```

The gates multiply because they are near-necessary conditions.
A paper that is certainly not Arctic scores zero, whatever else it does well.

The quality terms combine as a weighted arithmetic mean:

```
quality = (sum over quality terms of p_i * w_i) / (sum of quality weights)
```

The quality terms average because a paper can survive a weak one.

The two factors combine as a weighted geometric mean:

```
rank_probability = gate ** 0.5 * quality ** 0.5
```

The exponent 0.5 is measured, not chosen by taste.
The gate questions name 84 papers of loss and the quality questions name 87, so the two sides weigh almost the same.
A later run re-measures it.

The result stays between 0 and 1 and rises with every input.

### A worked example

A paper answers `geography_status` with 0.5 on `arctic_activity_stated` and every other question at its best value.
The gate weights are 2.0, 1.5, 1.5 and 1.0, which is 6.0.
So `gate` is 0.5 raised to 2.0 / 6.0, which is 0.793701.
`quality` is 1.000000.
So `rank_probability` is 0.793701 raised to 0.5, which is 0.890899.
`tests/test_jev_prescreen.py::test_a_worked_example_of_the_arithmetic` holds this example.

### What the weights are, and are not

The weights are judgement, informed by the counts in the tables above.
They are not fitted.
A fitted model on 21 accepted papers would overfit, and it would be harder to defend in the paper.

Every component probability is recorded per paper.
So the captain can refit the weights on the calibration sample at any time, from the recorded responses, with no new paid call.

### The ranking never reads an outcome

`src/arctic_qa/quality_order.py` states this convention in its `excluded_inputs` field, and the prescreen keeps it.
The ranking's only input is the article text.
`jev-ranking.json` records `ranking_inputs` as `article_full_text_only` and lists the excluded inputs, which include the pipeline's acceptance labels and its rejection reason codes.

The calibration in section 8 does read those labels.
That is measurement of the ranking, never construction of it.

## 5. The state: size, chunking and truncation

TypeSafe publishes no state size limit.
The largest document in any cookbook is 53,777 characters.
So the budget is ours, and `--state-character-budget` carries it.
The default is 120,000 characters, which is about 30,000 tokens.

Measured over the 4,420 frozen papers:

| Measure | Characters | Estimated tokens |
| --- | --- | --- |
| Minimum | 2,076 | 519 |
| 10th percentile | 32,102 | 8,025 |
| Median | 77,804 | 19,451 |
| 90th percentile | 129,333 | 32,333 |
| 99th percentile | 231,057 | 57,764 |
| Maximum | 2,149,706 | 537,426 |

So the default budget carries about 87% of the corpus whole.

`select_state_text` chooses the state in three steps:

1. If the article fits the budget, send it whole. The rule is recorded as `whole`.
2. If it does not fit, cut the trailing reference list, because it holds no finding of this study. The rule is recorded as `references_trimmed`.
3. If the body alone is still over the budget, keep 60% of the budget from the head and 40% from the tail, and mark the gap with an elision line. The rule is recorded as `head_tail` or `references_trimmed_head_tail`.

The head carries the title, the abstract and the introduction.
The tail carries the discussion and the conclusions, which is where a stated main finding lives.

### Why the rule does not split by section

A section-preference rule was the first design, and the corpus refuses it.
The extracted text is a flat PDF dump with column-flow artifacts, so headings are rarely on their own line.
Of the 4,420 papers, a strict own-line heading finds `abstract` in 819 and `results` in 518.
A rule that depends on locating the results section would fail on most of the corpus and fail silently.

The reference list is the one section that is found reliably and late.
A strict own-line reference heading appears in 1,710 of the 4,420 papers.
Where it appears, the last one sits at a median 0.777 of the text, with a fifth percentile of 0.604.
So `_references_start` takes only a marker in the last half of the article, which keeps 1,678 of those 1,710 and refuses the rest as body prose.

### When the provider refuses the state

The provider answers a bad request body with HTTP 422.
Because no size limit is published, a 422 is the only way to learn that one exists.
The screen answers it once, with a state at a quarter of the budget, and records `shrunk_after_422_from_budget` in the selection.
If that call fails too, the paper alone fails and the screen continues.
If the state is already smaller than the shrink target, no second call is made, because it would buy nothing.

### Reproducibility

Every result is reproducible from the manifest and the recorded responses.
Each response record holds the request hash, the state hash, the state character count, the byte spans that were sent, the selection rule, the question-set hash, the model that answered, and both timestamps.
`--action rank` recomputes the whole ranking from those records and makes no call.

Jev is not exactly deterministic.
The provider exposes no seed and no temperature.
Its own cookbook measures a per-question standard deviation near 0.01 across repeats.
The ranking uses the probabilities as continuous values and never thresholds them, so this noise moves a paper a little in the order and never flips a decision.

## 6. The cost

The price is `0.042` USD per million input tokens and `0.00` USD per million output tokens.
TypeSafe publishes no price page.
This constant comes from its cookbooks, and the documentation itself calls those figures unverified rather than billed rates.
So every ledger row records the price, its source and the status `unverified_cookbook_constant`.

Measured with `--action estimate` against the real manifest:

| Set | Papers | Input tokens | Cost at the documented price |
| --- | --- | --- | --- |
| Calibration sample, the labelled papers of run r3 | 270 | 5,291,394 | USD 0.23 |
| Every retained paper that has full text | 4,420 | 84,570,861 | USD 3.68 |
| All 16,339 retained papers, if the rest were fetched | 16,339 | about 324,000,000 | about USD 13.61 |

The third row is an extrapolation at the same mean size per paper.
It also assumes a fetch that has not happened.

Per paper, the median cost is about USD 0.0008.
The pipeline's own eligibility call on the same paper costs about USD 0.0226.

## 7. The order the pipeline reads

The producer reads its paper order from an article-access run directory.
`full_run_plan.materialize_frozen_access_run` builds that directory from a frozen manifest, and `streaming.run_streaming_dataset` walks `selection[*].position` in order.
`quality_order.py` already uses this path to write a reordered manifest, so the mechanism exists and needs no change.

The prescreen adds the smallest possible input to it.
`--action write-order` takes the frozen manifest, its descriptor and a ranked list of candidate keys.
It writes a new frozen manifest in the ranked order, with `manifest_position` rewritten to 1..N, plus a derived descriptor.
Those two files are exactly what `materialize_frozen_access_run` already reads.
Nothing in `streaming.py` or `full_run_plan.py` changes.

Two rules protect the corpus:

- A frozen paper the ranking never saw keeps its own relative order after the ranked ones. No paper is lost.
- A ranked key that the frozen manifest does not hold is ignored. A repeated key is refused.

The current run is untouched.
The ranked manifest is a new set of files in the prescreen work directory.
A future launch points at it; nothing points at it until then.

## 8. The calibration, before anyone pays for 16,000 papers

The question the captain has to answer is whether the ranking predicts acceptance.
The run has already decided 276 papers, so the answer is measurable for USD 0.23.

`--action labels` reads `paper_completions` for one run through a read-only URI and writes the labels.
`generation_accepted` is `accepted`.
`generation_rejected`, `eligibility_excluded` and `eligibility_unresolved` are `rejected`.
`incomplete_non_mcq` and `paper_cost_cap_reached` carry no label, because the run never finished judging them.

For run `chapter3-7dc6485-r3` at the time of writing, that is 276 papers: 21 accepted, 249 rejected and 6 unlabelled.
The whole labelled set is the calibration sample, which is better than a matched draw, because it uses every label the run has.

`--action calibrate` reports:

- the AUC of the ranking against the labels, with ties counted as half;
- the acceptance rate in the top decile, against the rest, and the lift between them;
- the base rate, which is about 0.078;
- the counts, including labelled papers that carry no score.

### How to read the numbers

The base rate is about 8%, so the numbers to compare against are these.

- An AUC near 0.5 means the ranking does not separate at all. Do not pay for the full set.
- An AUC of 0.65 or more, with a top-decile acceptance rate at least twice the rest, means the reordering is worth the USD 3.68 for the 4,420.
- A top-decile rate below the base rate means the ranking is inverted, and the weights or the question wording are wrong.

### What the calibration cannot prove

The labels come from one run, on one frozen corpus, under one prompt contract.
The frozen corpus was already ordered by an Arctic cue, so the papers the run reached are not a random draw of the 16,339.
A good AUC on this sample is evidence that the ranking works on papers like these.
It is not evidence about the 11,919 papers that have no text yet.

21 accepted papers is a small positive class.
The AUC has a wide confidence interval at that size.
Report it with the counts beside it, and re-run the calibration as the live run labels more papers.

## 9. The credential

There is one environment variable and one file.
`TYPESAFE_API_KEY` wins when it is set.
Otherwise the key is read from `typesafe-api-key` inside the credential directory, which is `~/.config/arctic-qa` by default and `ARCTIC_QA_CONFIG_DIR` when that is set.

The key is never printed, never written to a receipt and never put in an error message.
No action but `screen` needs it.
`build-manifest`, `labels`, `estimate`, `rank`, `write-order` and `calibrate` all run free and offline.

## 10. The model id

`jev-latest` is the one model id the API reference documents, and it floats.
A request for it is served by a concrete version, and the response names that version.

So `--model` defaults to `jev-latest`, every receipt records the model that answered, and `jev-ranking.json` counts the distinct answered models of a screen.
More than one entry there means the ranking mixes two model versions, and a reviewer has to decide whether that matters before the order is used.
Pin a concrete version with `--model` once a live key has named one.

## 11. The prescreen ledger

`CallLedger` writes `calls.jsonl` and `ledger.json` in the ledger directory.
It is not the shared paid-call broker and never talks to it.

One row per attempt records the candidate key, the request hash, the model, both timestamps, the token counts, the cost and the state.
A failed attempt records its error and costs nothing.
`ledger.json` holds the ceiling, the spend, what is left, the call count and the price constant.

`--ceiling-usd` stops the screen before a call, never in the middle of one.
`reserve` projects the call's cost from the state's token estimate and refuses when the projection would pass the ceiling.
A stopped screen keeps everything it bought, and a second screen over the same directory replays the recorded papers free.

If the provider reports no `input_tokens`, the row is billed on the estimate and records `billed_from_estimate` true.
The prescreen has no free token count, so this is the honest fallback.

## 12. Commands

Build the manifest over the retained set.

```
python -m arctic_qa jev-prescreen --action build-manifest \
  --dispositions-file <data>/metadata-prefilter-r1/run-20260912T024210Z/metadata-dispositions.ndjson \
  --freeze-manifest <data>/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1/full-text-ready-manifest.jsonl \
  --output-dir <work>/manifest
```

Write the calibration labels of the run.

```
python -m arctic_qa jev-prescreen --action labels \
  --database-file <data>/state.sqlite3 --run-id chapter3-7dc6485-r3 \
  --output-dir <work>/labels
```

Screen the calibration sample.
This is the first paid call, and it needs the key.

```
python -m arctic_qa jev-prescreen --action screen \
  --manifest-file <work>/manifest/prescreen-manifest.jsonl \
  --only-keys-file <work>/labels/labelled-keys-chapter3-7dc6485-r3.txt \
  --ceiling-usd 1.00 --workers 4 --output-dir <work>/calibration-screen
```

Rank and calibrate.
Both are free.

```
python -m arctic_qa jev-prescreen --action rank \
  --manifest-file <work>/manifest/prescreen-manifest.jsonl \
  --responses-dir <work>/calibration-screen/responses \
  --output-dir <work>/calibration-rank

python -m arctic_qa jev-prescreen --action calibrate \
  --ranking-file <work>/calibration-rank/jev-ranking.json \
  --labels-file <work>/labels/labels-chapter3-7dc6485-r3.jsonl \
  --output-dir <work>/calibration
```

If the numbers pass the bar in section 8, screen the whole frozen corpus and write the order.

```
python -m arctic_qa jev-prescreen --action screen \
  --manifest-file <work>/manifest/prescreen-manifest.jsonl \
  --ceiling-usd 6.00 --workers 4 --output-dir <work>/full-screen

python -m arctic_qa jev-prescreen --action rank \
  --manifest-file <work>/manifest/prescreen-manifest.jsonl \
  --responses-dir <work>/full-screen/responses --output-dir <work>/full-rank

python -m arctic_qa jev-prescreen --action write-order \
  --freeze-manifest <data>/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1/full-text-ready-manifest.jsonl \
  --freeze-descriptor <data>/corpus-freeze-r1/full-text-ready-4420-seed20260912-r1/manifest-descriptor.json \
  --order-file <work>/full-rank/ranked-order.jsonl --output-dir <work>/order
```

The work directory of this task is `/mnt/crdata/research-abstention/arctic-qa/jev-prescreen-r1/`.

Concurrency is bounded by `--workers`, and the default is 4.
The provider publishes no rate limit.
Its own cookbooks say that eight workers are enough to reach a rate limit on a shared key, so raise `--workers` slowly and watch for HTTP 429.
The client retries 429 and 529 with exponential back-off, and obeys `retry-after` or `retry-after-ms` when the provider sends one.

## 13. What is still unknown

- The real price. TypeSafe publishes no price page, and the cookbook constant is disclaimed.
- The state size limit. None is published. A 422 is the only signal, and section 5 says what the screen does with one.
- The rate limit. None is published.
- The full model roster. `models.list()` needs a live key.
- Whether a 429 carries a `Retry-After` header at all. The client reads one when it is there and falls back to exponential back-off when it is not.
