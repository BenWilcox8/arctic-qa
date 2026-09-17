# Jev prescreen: ranking the retained papers before the pipeline opens them

Task `arctic-jev-prescreen-r1`, 2026-09-17.
Branch `fm/arctic-jev-prescreen-r1`.
Work directory `/mnt/crdata/research-abstention/arctic-qa/jev-prescreen-r1/`.

**Conclusion in one line: the ranking predicts acceptance offline (AUC 0.693, p=0.003), the whole corpus is ranked for USD 3.39, and the live pipeline is consuming that order, but the production effect is not yet proven: 50 papers give a 1.6 times lift at p=0.28.**

`docs/JEV_PRESCREEN.md` is the contract for the module and the commands.
This report is the record of what was measured.

## 1. What was built

`src/arctic_qa/jev_prescreen.py` asks TypeSafe Jev one batched question set over each article's own full text and turns the answers into one ranking probability.
Eight questions in one call: four gates and four quality terms, each tied to a rejection reason that run `chapter3-7dc6485-r3` actually recorded.
`streaming.PaperPicker` is the producer's pick-up, which reads the ranking as it changes.

The ranking reads the article text alone.
The pipeline's outcome labels only measure it, through `--action calibrate`.
`quality_order.py` states that convention as `excluded_inputs` and the prescreen keeps it.

## 2. The corpus

| Set | Papers |
| --- | --- |
| Retained by the metadata pre-filter (`retained_article_type`) | 16,339 |
| Of those, with extracted full text on disk | 4,420 |
| Without full text, needing an `article-access-r1` fetch | 11,919 |

The 4,420 are the frozen corpus `full-text-ready-4420-seed20260912-r1`.
The captain's "about 16,000" is the retained set; only 4,420 of it could be scanned, because a prescreen cannot read a paper that is not on disk.

## 3. The calibration, before any corpus-wide spend

318 papers of run `chapter3-7dc6485-r3` carried a label when the sample was drawn.
291 were scored; the other 27 failed on size and are the subject of section 5.

| Measure | Value |
| --- | --- |
| AUC | 0.693, 95% CI [0.564, 0.822], p = 0.003 |
| Top decile acceptance | 5 of 29 = 17.2% |
| The rest | 16 of 262 = 6.1% |
| Lift | 2.82 times |
| Base rate | 7.2% |
| Cost | USD 0.2406 |

The bar set in advance was an AUC of at least 0.65 with a top decile at least twice the rest.
Both were met.

The 27 unscored papers do not bias this: their acceptance rate is 2 of 27 = 7.4%, against 7.2% for the scored ones, so the set that fell out is neither enriched nor depleted in accepted papers.

## 4. The full scan

| Measure | Value |
| --- | --- |
| Papers scanned | 4,420 of 4,420 |
| Failed | 0 |
| Faulted | 0 |
| Recovered by the shrink retry | 189 |
| Cost | USD 3.39 against a USD 10 ceiling |
| Ranked | 4,420, 0 unreadable |
| Model that answered | `jev-1.13.0` for all 4,420 |

State selection: 3,627 whole, 541 head and tail, 133 with the reference list cut, 119 with both.

The cost is close to the USD 4.20 the corrected estimator projected and to the USD 3.68 the first estimator projected.
Measured usage came in slightly under the estimate, so the projection is conservative.

## 5. Two defects only a paid call could find

Both were found by the calibration run and fixed before the corpus-wide spend.
This is the argument for calibrating first.

**The provider's limit is on tokens, is unpublished, and is not an HTTP 422.**
TypeSafe refuses an oversized request with HTTP 400 and the body `{"detail":{"error_type":"max_tokens_exceeded"}}`.
The module expected 422, so its shrink retry never fired and 27 of 318 papers simply failed.
The largest accepted request carried 32,350 input tokens and every refusal lay above it, so the limit is taken as 32,768.

**Characters are not tokens for this corpus.**
Measured over the 116 calls of the calibration that carry a real usage record, one token buys 5.38 characters of Latin text, 3.88 at the median and 1.51 at worst.
Every worst-ratio paper is a Cyrillic journal.
The flat four characters per token was safe for Latin text and wrong by 2.6 times for Cyrillic, which both under-projected the cost of the Russian journals and pushed their state over the limit.

The estimate now counts the two scripts apart, the budget is in tokens, and the shrink retry halves it up to three times.
The full scan then recovered 189 oversized papers and lost none.
On the original code those 189, disproportionately Russian Arctic research, would have been silently missing from the order.

## 6. The live order

The scan writes `live-ranking.json` atomically every 25 papers, so a scored paper reaches the pipeline without waiting for the rest of the corpus.
A resumed scan seeds that file from the responses it already bought.

The producer was stopped at a settled boundary and relaunched onto that order.

| Event | Time (UTC) |
| --- | --- |
| Settled boundary reached, in-flight 0 for this run | 04:07:26 |
| Producer stopped and verified gone | 04:07:26 |
| Relaunched, same run id, no policy transition | 04:07:39 |
| First Jev-ordered paper picked | 04:08:55 |

The first paper was `10.5194/tc-20-3217-2026`, Jev rank 1, score 0.931866.
The next picks were ranks 3, 2, 4 and 6: four workers pull at once, so ranks interleave.
The stop used the concurrency task's own algorithm, which waits for a moment with none of this run's calls on the wire, so no paid call was killed and no orphan was left.
The evaluator stayed up throughout.

## 7. The production result, and why it is not yet proof

After 50 Jev-ordered papers:

| Set | Accepted | Rate |
| --- | --- | --- |
| Jev-ordered, after the relaunch | 6 of 50 | 12.0% |
| The run's own rate before the relaunch | 27 of 359 | 7.5% |

Lift 1.60 times.
z = 1.09, two-sided p = 0.28.
The Wilson 95% interval for the Jev-ordered rate is [0.056, 0.238] and contains the base rate.

**This is not a significant result, and it should not be reported as one.**
The lift also decayed as the sample grew, which is what regression to the mean looks like:

| Papers | Accepted | Lift over base |
| --- | --- | --- |
| 17 | 4 | 3.1 times |
| 30 | 5 | 2.2 times |
| 43 | 6 | 1.9 times |
| 50 | 6 | 1.6 times |

If the 12.0% rate holds, the comparison reaches p = 0.06 at 200 papers and p = 0.02 at 400.
The order is already bought and the producer keeps consuming it at no further cost, so the measurement continues for free.

Two further limits on this comparison.
The base rate comes from papers analysed before the relaunch under the frozen order, so this is not a randomised control: corpus composition and pipeline code also differ across that boundary.
And the offline calibration's labels come from one run on a corpus already ordered by an Arctic cue, so it is evidence about papers like these, not about the 11,919 with no text.

## 8. What is still unknown

- The real price. TypeSafe publishes no price page. The constant `0.042` per million input tokens is a cookbook figure the documentation itself disclaims, and it is attributed to `jev-1.12` while `jev-1.13.0` answered every call here. Treat every USD figure in this report as an estimate at that rate.
- The exact token limit. 32,768 is inferred from the largest accepted request and the refusals above it, not documented.
- The rate limit. None is published. The scan ran 4 workers without a throttle.
- Whether the production lift is real. Section 7.

## 9. Files

| Path | What it holds |
| --- | --- |
| `<work>/manifest-v2/prescreen-manifest.jsonl` | The input manifest over all 16,339 retained papers |
| `<work>/full-screen/responses/` | One recorded response per scanned paper |
| `<work>/full-screen/live-ranking.json` | The live order the producer reads |
| `<work>/full-screen/ledger/` | The prescreen's own call ledger |
| `<work>/full-rank/jev-ranking.json` | The full ranking with every component probability |
| `<work>/full-rank/ranked-order.jsonl` | The frozen 4,420-row order |
| `<work>/labels/` | The calibration labels, read from the state database read-only |
| `<work>/calibration/jev-calibration.json` | The calibration report of section 3 |

`<work>` is `/mnt/crdata/research-abstention/arctic-qa/jev-prescreen-r1/`.

Every ranking is reproducible from the manifest and the recorded responses: `--action rank` recomputes it and makes no call.
