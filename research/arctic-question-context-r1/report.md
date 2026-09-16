# Question context implementation report

## Result

The branch adds a top-level `question_context` string next to `question`.
New generation calls always write the field.
Readers and exporters use an empty string when an older candidate does not have the field.
No process changes an older receipt or creates context for an older candidate.

## Generation and validation

Generation prompt version 14 defines the context rules.
The question writer supplies only context that is necessary to understand the question.
The prompt permits an acronym definition, a referent identity, or a study-group or measurement distinction.
It prohibits answers, results, conclusions, answer-bearing numbers, answer-choice eliminators, and paper summaries.
It also prohibits an acronym expansion when that expansion is the answer.

The existing answer-verification call now returns three context verdicts.
These verdicts cover necessity, source support, and answer leakage.
No new provider call is present.
Deterministic validation also rejects a direct answer string or answer-bearing number in the context.
This detection is a safety layer and does not guarantee perfect leakage detection.

The blinded reconstruction input contains `QUESTION` and `QUESTION_CONTEXT` as separate sections.
The input does not contain the reference answer, answer evidence, rationale, or reviewer data.
Distractor generation and each option check also receive both fields.
The QA hash and item identity include the context.
Thus, a context change invalidates existing option bindings.

## Serialization and documentation

The short-answer, answer-present MCQ, and answer-absent MCQ JSONL exports contain `question_context`.
Legacy candidates export an empty string.
The benchmark input contract requires external evaluators to provide both fields.
It also lists the data that a blinded input must exclude.
The methods and streaming documents describe the version 14 method.

## Checks

The focused question-context tests pass.
The generation smoke path and both generation arms pass.
The QA-gate regression tests pass.
The full suite completed with one stale prompt-wording assertion from the version 13 base.
The assertion now matches the existing atomic-answer prompt, and that test passes in isolation.
`git diff --check` passes.

## Integration

Cherry-pick this branch after the geography branch resolves any overlap in generation fixtures or prompt-version assertions.
Keep `arctic-qa-generation-v14` as the final prompt version.
Keep the top-level field name and the empty-string legacy default.

The publication exporter from commit `c7c29c3` is not on this branch.
Apply this narrow follow-up in `src/arctic_qa/publication_export.py`:

1. Add `question_context` after `question` in `_row` and `_manifest_row`.
2. Read the value from the exported item first, then the matching candidate, then use `""`.
3. Compare both `question` and `question_context` in `_matching_candidate`.
4. Add `question_context` after `question` in both benchmark-row builders.
5. Add `question_context` after `question` in both benchmark CSV field lists.
6. Add the same field to reviewer JSONL and reviewer CSV records.
7. Test that benchmark JSONL and CSV contain the context but exclude answers, evidence, rationales, labels, and selection reasons.
8. Test that an older candidate and MCQ without the field export `question_context` as `""`.

The separate viewer worker owns website presentation.
Its UI must show Context beside Question and retain the raw JSON view.
