# Phase 4 report: chapter 2 corpus and eligibility

Task: `arctic-ch2-corpus-r1`.
Branch: `fm/arctic-ch2-corpus-r1`, from `a53b10690e2f599928f210b90d95e0cc50f54623` on `fm/arctic-audit-priorities-r1`.
Date: 2026-09-15.

## Files touched outside the owned set

The owned set is extraction, source pass, screening, prefilter, the eligibility prompt and schema version, and the chapter 2 data root.

| File | Change | Why |
| --- | --- | --- |
| `src/arctic_qa/cli.py` | Added the `chapter2-corpus` command, two `gemini-eligibility` actions, and `--prior-run-dir`. | A new owned module needs an entry point. No other command changed. |
| `tests/test_project_progress_viewer.py` | Corrected two stale expectations. | The test was already red on the base commit. See "The red test on the base commit". |
| `src/arctic_qa/corpus_viewer.py`, `src/arctic_qa/corpus_viewer.html` | Added `unresolved_rescreenable` to the accepted overlay states, the filter list, and the display order. | The viewer refuses an overlay row whose status it does not know, so the new non-terminal state would have stopped the read-only monitor. Three lines, no logic changed. |

`generation.py`, `validation.py` and `streaming.py` are unchanged.
The chapter 2 access run directory uses the existing `article-access-manifest-v1` and `article-access-item-v1` schemas, so `--access-run-dir` needed no code change at all.

## Defects addressed, by audit finding

| Finding | Defect | Change | Regression test |
| --- | --- | --- | --- |
| 4.3, E3(a) | Two-column pages interleave line by line. A column gutter was in 81 of 95 evidence quotes. | `pdf_layout.py` reads word geometry from `pdftotext -bbox-layout` and rebuilds the text with a recursive XY cut over the lines of a page. | `test_two_column_page_is_read_one_column_at_a_time` |
| 4.3, E3(a) | A word cut by a line wrap stayed broken, so `Arctic per-` reached a question. | Lines join inside a paragraph and the wrap hyphen is removed. A line that ends in a wrap hyphen never ends a paragraph. | `test_a_word_split_by_a_line_wrap_is_joined` |
| 4.3, E3(a) | Ligatures and soft hyphens blocked every phrase match. `Bafﬁn Island` was a frozen scope phrase. | `normalize_presentation` folds NFKC, removes the soft hyphen, and collapses runs of spaces. | `test_ligatures_and_soft_hyphens_are_folded` |
| 4.3, E3(b) | `gemini_eligibility.py` joined spans that already end in a newline with another newline, so no phrase that crossed a line could bind. | The join is now `""`. | `test_a_whole_word_phrase_binds_after_normalization` |
| 4.3, E3(c) | The phrase test was byte exact against a line fragment. | The phrase and the span text are compared through one normalization. The stored span bytes never change. | `test_binding_folds_only_presentation_damage`, `test_a_phrase_broken_by_a_line_wrap_is_still_rejected` |
| 4.3, E3(d) | One formatting mistake wrote a terminal `screening_error` row and stopped the batch. The paper was never screened again. | A scope-shape error writes an `unresolved` row, not a `jobs` row, and does not stop the batch. Paid attempts are bounded at two. | `test_an_unresolved_paper_is_re_screenable_until_its_attempts_run_out`, `test_a_formatting_mistake_is_repairable_and_an_envelope_error_is_not` |
| 4.3, E3(d) | There was no repair call anywhere in the module. | The second attempt carries a repair note with the failing codes and every frozen criterion status. A repair that moves a status is refused. | `test_the_bounded_repair_freezes_every_criterion_status` |
| 4.7, E4 | The prompt never restated the policy failure rule, so the classifier used `failed` for "I found no evidence". | Prompt v7 states an ordered geography procedure with the failure rule and "Absence of evidence is never failed". | `test_v7_states_the_ordered_geography_procedure` |
| 4.7, E4 | `uncertain` is terminal, so the geography procedure alone gains nothing. | A bounded geography re-screen selects only the papers whose one unsatisfied criterion is `study_geography`. | `test_the_geography_rescreen_takes_only_the_geography_unresolved_papers` |
| 4.7 | The separable-component rule was not stated for results on both sides of the boundary. | Prompt v7 states it. | `test_v7_states_the_separable_component_rule` |
| E5 | A scope window that ended mid-clause truncated the writer's source and then rejected the item for the truncation. | Prompt v7 asks for sentence-complete result spans. Chunks start and end on a sentence. | `test_v7_asks_for_sentence_complete_result_spans`, `test_chunks_start_and_end_on_a_sentence` |

Two defects of the new code were found while building it and are pinned by their own tests.

| Defect | Found by | Change | Regression test |
| --- | --- | --- | --- |
| A heading block became the name of its section and left the document. A paper with many short lines lost up to a quarter of its words, and a section that held only a heading disappeared. | Comparing word counts against the chapter 1 extraction: median retention 0.943, with 41 of 250 papers below 0.8. | The heading block opens its section and stays among its blocks. Median retention is now 0.982, with 1 of 100 papers below 0.8. | `test_no_block_is_lost_between_the_pages_and_the_sections` |
| Reading order was decided on poppler's block grouping, and poppler merges two narrow columns into one block. | The two-column fixture. | The recursive XY cut runs over the lines of a page, and it looks for a column gutter before a horizontal band. | `test_two_column_page_is_read_one_column_at_a_time` |

## Files changed

New:

- `src/arctic_qa/pdf_layout.py` - reading-order PDF extraction over poppler word geometry.
- `src/arctic_qa/text_structure.py` - sections, headings, running-head removal, the sentence splitter.
- `src/arctic_qa/chapter2_corpus.py` - the chapter 2 root, the re-extraction pass, and the re-freeze.
- `src/arctic_qa/extraction_quality.py` - the five extraction measures and the comparison report.
- `config/gemini-eligibility-prompt-v7.txt`
- `config/gemini-eligibility-geography-rescreen-v1.txt`
- `docs/CHAPTER2_CORPUS.md`
- `tests/pdf_fixture.py`, `tests/test_chapter2_corpus.py`, `tests/test_eligibility_geography_v7.py`

Changed:

- `src/arctic_qa/extraction.py` - the new PDF path, section and chunk records with page and heading locators, sentence-complete chunking, the chapter 2 corpus root, and reuse of a frozen chapter 2 parse.
- `src/arctic_qa/gemini_eligibility.py` - the binding normalization, the non-terminal unresolved record, the bounded repair note, and the geography re-screen selector and action.
- `src/arctic_qa/cli.py`, `src/arctic_qa/corpus_viewer.py`, `src/arctic_qa/corpus_viewer.html`, `AGENTS.md`, `docs/METHODS.md`, `docs/SOURCE_SCREENING_PASS.md`, `docs/STREAMING_DATASET.md`.

`source_pass.py`, `screening.py` and `metadata_prefilter.py` are unchanged.

- The source pass keeps `pdftotext -layout`. Its job is identity checking on the retrieved bytes, and its output is chapter 1 history. `docs/SOURCE_SCREENING_PASS.md` now records that difference.
- `screening.py` reads its chunks through `extraction.load_chunks`, so it reads the chapter 2 chunks with no change.
- `metadata_prefilter.py` reads metadata only and never touches extracted text.

## New version strings

| Version | Value |
| --- | --- |
| Parser version | `2.0.0` |
| Parser key | `parser-v2` |
| Extractor name | `pdftotext-bbox-layout-reading-order` |
| Extractor version | `1.0.0` |
| Structure version | `1.0.0` |
| Eligibility prompt | `gemini-eligibility-prompt-v7` |
| Geography re-screen prompt | `gemini-eligibility-geography-rescreen-v1` |
| Chapter 2 corpus source version | `chapter2-corpus-v1` |
| Chapter 2 root schema | `arctic-qa-chapter2-corpus-root-v1` |
| Chapter 2 parse index schema | `arctic-qa-chapter2-parse-index-v1` |
| Chapter 2 manifest schema | `arctic-qa-chapter2-corpus-manifest-v1` |
| Chapter 2 manifest descriptor schema | `arctic-qa-chapter2-corpus-manifest-descriptor-v1` |
| Chapter 2 freeze receipt schema | `arctic-qa-chapter2-corpus-freeze-receipt-v1` |
| Chapter 2 re-extraction progress schema | `arctic-qa-chapter2-reextraction-progress-v1` |
| Extraction quality schema | `arctic-qa-extraction-quality-v1` |

No response schema changed.
The eligibility response contract stays `eligibility-response-v3`, and `schemas/gemini-eligibility.v3.schema.json` is untouched.

## New and changed prompt text

### `config/gemini-eligibility-prompt-v7.txt`

v7 keeps every line of v6 that the audit named as correct.
It adds three blocks and removes the byte-exact phrase demand.

The geography block, which replaces the two geography lines of v6:

```
GEOGRAPHY DECISION PROCEDURE
Decide study_geography with these rules in order. Stop at the first rule that applies.
1. Set satisfied when a span states an observation, sample, field site, station,
   cruise, modeled domain, or reported result at or north of 66.56 degrees north.
   A modeled, simulated, reanalysis, or remote-sensing domain is study activity.
2. Set satisfied when a span places that activity or result in a bounded region
   from the supplied wholly_north list.
3. Set satisfied when a span names a region from the supplied
   crosses_boundary_requires_northern_evidence list and another span places the
   activity, stratum, or result north of the boundary.
4. Set failed only when the spans place all study activity and all reported
   results outside the boundary, or show that the Arctic reference is incidental.
5. Set uncertain in every other case, including when you cannot locate the
   activity evidence. Absence of evidence is uncertain. Absence of evidence is
   never failed.
The frozen policy states the same rule: fail geography only when all actual study
activity is outside the boundary or the Arctic reference is incidental.
Use eligible_arctic_scope only for actual study activity and reported findings.
Do not use a title, affiliation, background statement, or citation as Arctic scope.
Do not use a title as proof of the criterion either. A title can tell you where to
look for the activity evidence. It cannot stand in for that evidence.
```

The span block adds sentence completion and the separable-component rule:

```
Select finding spans that are complete sentences. Extend the selection forward
until the last selected span ends a sentence. Do not end a selection on a partial
clause.
Select the spans that state the paper's reported results. Do not build the scope
from the abstract opening alone, and do not build it from a figure or table
caption alone.
When the spans show results both inside and outside the boundary, set
separable_arctic_component, never whole_study.
```

The phrase block replaces the four scope-phrase lines of v6:

```
QUESTION SCOPE PHRASES
Give question_scope_phrases only for separable_arctic_component.
Give an empty list for whole_study.
Each phrase must name the eligible station, region, stratum, population, or
modeled domain. Do not use a numeric result value, a percentage, a bare unit, or a
vague label such as "In the Arctic" as a phrase.
Copy each phrase from the selected finding spans. When a line break splits a word
with a hyphen, write the whole word. When a phrase continues across two spans,
write it as one continuous phrase.
Each phrase is checked after whitespace is collapsed, so do not repeat the line
breaks or the spacing of the span.
Do not use a phrase from a title, activity span, or unselected result span.
These phrases must identify the eligible station, region, stratum, or result in
every downstream question.
```

The removed v6 line is:

```
For a separable component, every question_scope_phrases value must be an exact
substring of the selected finding_span_ids text.
```

### `config/gemini-eligibility-geography-rescreen-v1.txt`

A second prompt for the bounded geography re-screen.
It repeats the ordered procedure, decides `study_geography` alone, and keeps every other criterion at its first-screening status.
It also repeats the actual-study-evidence rule, the provenance rule, and the separable-component rule.

## The chapter 2 corpus

Root: `/mnt/crdata/research-abstention/arctic-qa/chapter2/`.
No chapter 1 directory was written, moved, or removed.
No row of `state.sqlite3` was changed. Chapter 2 artifacts insert new rows, because the object digest is part of the artifact identity.

`docs/CHAPTER2_CORPUS.md` records the layout, the three stages, and the commands.

A run reads the chapter 2 corpus without any change to `streaming.py`.
`extract_source` resolves its corpus root through the `corpus-root.json` marker.
When the root holds a verified parse of the same bytes, it returns that frozen parse and pays no extraction cost.
The parse is faithful because the chapter 2 build derives the same source identifier as the streaming bridge.

### No new dependency

The brief allows a new dependency for a layout-aware extractor.
None was added.
`poppler-utils` is already in the nix devshell, and its `pdftotext -bbox-layout` mode publishes the bounding box of every word, line and block.
That geometry is all the reading-order cut needs, so `flake.nix` and `pyproject.toml` are unchanged and the project keeps its empty dependency list.

### The database

The chapter 2 build writes no database row at all.
`extract_source` inserts an artifact row for a parse and a chunk object, and the object digest is part of the artifact identity, so a chapter 2 object always inserts a new row.
No row of a chapter 1 run is read for update, and no `UPDATE` statement runs.
The chapter 2 source version `chapter2-corpus-v1` is recorded in the chapter 2 manifest and in the chapter 2 access run manifest.

### Stored originals

All 4,420 usable full texts are PDFs.
`METHODS.md` prefers JATS, then HTML, then PDF, but no JATS or HTML original is stored for a paper of the frozen corpus.
The XML and HTML paths of `extract_document` keep working and now apply the same presentation normalization.

## Extraction quality evidence

The measurement script is `src/arctic_qa/extraction_quality.py`.
The command is `chapter2-corpus --action quality`.
The stored report is `/mnt/crdata/research-abstention/arctic-qa/chapter2/extraction-quality-r1.json`.

The sample is 50 two-column papers, taken in the chapter 1 access order.
A paper enters the sample when its chapter 2 parse records at least one multi-column page.
The 50 papers hold 882 pages, of which 537 are multi-column.
Both corpora are measured on the same 50 papers.

### The extracted text

| Measure | Chapter 1 | Chapter 2 |
| --- | ---: | ---: |
| Text lines | 39,491 | 26,959 |
| Lines that glue two columns | 13,512 | **0** |
| Gutter rate | 0.356 | **0.000** |
| Documents with any gutter line | 50 of 50 | **0 of 50** |
| Mid-word line breaks | 2,056 | 84 |
| Mid-word breaks for each 1,000 characters | 0.433 | **0.021** |
| Ligatures and soft hyphens | 774 | **0** |
| Ligatures and soft hyphens for each 1,000 characters | 0.194 | **0.000** |

The audit found a column gutter in 81 of 95 evidence quotes.
On this sample the gutter is gone: not one of the 26,959 chapter 2 lines joins two columns, and every one of the 50 papers had at least one such line before.
The 84 remaining mid-word breaks are hyphens at the end of a paragraph, where the next paragraph starts with a lowercase word. The extractor does not join across a paragraph boundary.

### The chunks

The chapter 1 chunker cut a fixed window inside one page section.
`extraction_quality.legacy_chunks` repeats that algorithm on the chapter 1 text, so the two corpora are compared chunk for chunk.

| Measure | Chapter 1 chunks | Chapter 2 chunks |
| --- | ---: | ---: |
| Chunks | 1,257 | 4,915 |
| Chunks that carry a column gutter | 983 | **0** |
| Gutter chunk rate | 0.798 | **0.000** |
| Chunks that end on a sentence | 266 | 1,484 |
| Sentence-complete rate | 0.209 | **0.402** |

Four of five chapter 1 chunks carried a gutter. No chapter 2 chunk does.

The sentence-complete rate is a lower bound, not a defect rate.
The measure asks for terminal punctuation, and a chunk that holds a heading, an equation, a table row, or a reference entry has none.
Chapter 2 has more chunks because a section is now delimited by a heading instead of by a page.

### Text conservation

The reading-order extractor must not lose the words of the paper.

| Measure | Value |
| --- | ---: |
| Median chapter 2 words for each chapter 1 word | 0.971 |
| Lowest value in the sample | 0.944 |
| Documents below 0.9 | 0 of 50 |

The missing 3 percent is the repeated running head, which the extractor removes on purpose.
This measure is what found the lost-heading defect described above, so it is now part of the report.

### The corpus

| Count | Value |
| --- | ---: |
| Usable full texts re-extracted | 4,420 |
| Extraction failures | 0 |
| Chapter 2 corpus size on disk | 1.5 GB |

## Rigor safeguard for every change

**Reading-order extraction.** The extractor changes only how the stored bytes are read into text. Every downstream gate is unchanged. A question can only become easier to support, never easier to accept: the standalone gate, the reconstructor, the answer verifier and every deterministic contract still run without change. Removing a column gutter from a quote does not make a paper-dependent question pass any of them.

**Sentence-complete chunks.** Completing a sentence adds no text from outside the passage the extractor already selected. It removes the case where the pipeline hands its own verifier a truncated source and then rejects the item for that truncation.

**Presentation normalization in binding.** The comparison is normalized; the stored span bytes are not. A phrase must still be present in the selected finding spans. `test_a_phrase_broken_by_a_line_wrap_is_still_rejected` pins that a phrase which is not in the span still fails. Barring a percentage, a bare unit, and a vague label makes separable-component custody stricter than v6.

**The bounded repair.** The repair carries every criterion status forward and is refused when the second response moves any status, so it can never change a scientific judgment. It corrects the shape of an answer only. Paid attempts are bounded at two.

**The non-terminal unresolved record.** A paper left unresolved is out of the corpus. Nothing enters the funnel through this change. It only stops one formatting mistake from ending a paper forever and from halting the batch.

**The geography procedure.** Rules 1 to 3 restate the frozen policy and add no new inclusion ground. Rule 4 still needs positive evidence that all activity is outside the boundary, so `failed` becomes harder to reach, not easier. Rule 5 routes "I cannot find it" to `uncertain`, which keeps the paper out unless the bounded re-screen finds a real activity span. The line that a title cannot stand in for activity evidence is new and is a restriction.

**The geography re-screen.** It selects only a paper whose decision was `uncertain` and whose one unsatisfied criterion is `study_geography`. A paper with `failed` geography is never selected, so the 36 correct exclusions cannot return. A paper that is still uncertain after the re-screen stays out.

**Why no eligibility change can admit a paper-dependent item.** Eligibility decides which papers reach the writer. Every question from a newly admitted paper still passes the standalone gate, the reconstructor, the answer verifier and every deterministic gate, unchanged by this slice.

## Cross-crew contract points

1. `resolved_eligible_arctic_scope.question_scope_phrases` now holds the normalized phrase. The raw model output is kept beside it in `question_scope_phrases_source`. The audit specifies the normalized form so that downstream consumers get whole words. `generation.py::_require_arctic_scope_custody` still tests `phrase in quote` against the raw quote. A phrase that PDF extraction had broken will now fail that test instead of forcing a broken byte sequence into a question. The audit's E6 fix in phase 1 or phase 2 replaces that test with a displayed-task test. This slice and that fix belong in the same landing.

2. The chapter 2 access run directory is a drop-in `--access-run-dir`. Its `run_id` and `selection_keys_sha256` differ from chapter 1, so a broker binding made against a chapter 1 directory does not carry over.

3. The doubled-newline join in `_validate_response_span_contract` is fixed here, because the normalized phrase comparison sits on the same lines. The phase 0 crew owns the same fix. If both land, keep one.

## Tests

All test runs used the project nix devshell: `nix develop -c bash -c 'PYTHONPATH=src pytest ...'`.

| Run | Files | Result |
| --- | --- | --- |
| Whole suite | `tests/` | **580 passed**, 0 failed, 698 seconds |
| Focused set | `test_streaming.py`, `test_gemini_eligibility.py`, `test_question_context.py`, `test_eligibility_span_contract.py`, `test_chapter2_corpus.py`, `test_eligibility_geography_v7.py`, `test_geography_correction.py`, `test_corpus_viewer.py`, `test_project_progress_viewer.py`, `test_source_pass.py`, `test_metadata_prefilter.py`, `test_pipeline_trace.py`, `test_cli_integration.py` | **324 passed**, 0 failed, 584 seconds |
| Lint | `ruff check src/ tests/` | clean |
| Format | `ruff format --check` on every file of this slice | clean |

The brief names `test_streaming.py`, `test_gemini_eligibility.py`, `test_question_context.py` and `test_eligibility_span_contract.py`.
All four ran in both runs and all passed.
No streaming test stalled in a broker rate-limit sleep, so no run needed a bound.

New test files:

- `tests/test_chapter2_corpus.py`, 18 tests. Extraction, chunking, locators, the corpus root, frozen-parse reuse, the freeze, the access run contract, legacy isolation, and the quality measures.
- `tests/test_eligibility_geography_v7.py`, 16 tests. Prompt v7, the re-screen prompt, phrase binding, the bounded repair, the unresolved record, the re-screen selector, and the viewer state.
- `tests/pdf_fixture.py`. Builds a real two-column PDF, so the column tests run against page geometry and not against a string.

## The red test on the base commit

`tests/test_project_progress_viewer.py::test_project_overview_uses_streaming_counts_as_separate_live_metrics` was already failing on `a53b106`, before any change of this slice.
Commit `e1fd380` ("Scope corpus viewer to latest invocation") changed `corpus_viewer.py` to report `accepted_qa_scope` as `current_incremental_invocation` and to count `accepted_qa` from the observed counts.
It did not update this test.
This slice corrected the two stale expectations and changed no code of the viewer.

## Deferred

1. **The validator check for sentence-complete finding spans.** The audit's E5 proposes a new error code, `eligible_arctic_scope_finding_spans_not_sentence_complete`. The prompt half is shipped. The validator half is not, because a chapter 2 span is a whole paragraph by construction, so the code would rarely fire and would add a reason code that the routing sets of a sibling slice do not know. Add it with the routing change, not before.

2. **The paid geography re-screen.** The path, the prompt, the selector and the command are shipped. No paid call was made, as the brief instructs. The integration crew runs it inside the chapter 2 budget with `--action geography-rescreen --prior-run-dir <the first run>`.

3. **The chapter 2 eligibility re-screen of the whole corpus.** Shipping prompt v7 changes the job key of every paper, so the whole chapter 2 corpus re-screens with no manual requeue. That is a paid step for the integration crew.
