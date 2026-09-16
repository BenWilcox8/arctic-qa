# Benchmark input contract

Each model-facing benchmark item contains `question` and `question_context`.
An empty `question_context` means that the question is self-contained.
An external evaluator must provide both fields to the evaluated model.
The evaluator must keep the fields separate in the rendered input.

The question and context together contain the complete task.
They identify the actual system, location, samples, period, and conditions needed for one interpretation.
They include each detail only when the source supports it.
They do not invent missing details or broaden a paper-specific observation into a general fact.
They do not use source-dependent references such as a study, authors, figures, tables, or text above.
They do not use unresolved phrases such as the samples or the identified OTUs.

The context contains only information that is necessary to understand the task.
Context can define an unfamiliar acronym, identify a referent, or distinguish a sample, location, period, condition, system, or measurement.
When needed, context expands OTUs as operational taxonomic units (OTUs).
Context gives source-supported sample and location information when it is needed for that acronym.
Context does not contain a second task, taxonomic counts, results, conclusions, answer-bearing numbers, or answer-choice eliminators.

These rules apply to benchmark-facing text only.
Exact source quotes, evidence, rationales, and reviewer locators can retain source references.
A benchmark reader can need the source to determine or verify the answer.
The reader must not need the source to identify a referent or interpret the question scope.

Blinded benchmark input must not contain these records:

- the gold or reference answer
- answer evidence or source text
- generation, selection, reconstruction, or verification rationale
- reviewer data or validation labels
- paper-selection or geography-screening reasons

The benchmark JSONL and CSV files must put `question_context` immediately after `question`.
An exporter must write an empty string when an older candidate has no `question_context` field.
It must not create context for an older candidate during export.

Question context is part of the question identity and option-verification binding.
A context change creates a different generated item and invalidates old option bindings.

## Scope roles

Generation roles use `scope-role-semantics-v2`.
New candidates use generation prompt `arctic-qa-generation-v22`.
They use scope contract `selected-evidence-literal-scope-v4` and candidate schema `2.7.0`.
Before source-aware checks, a source-blind gate reads only the question and question context.
It rejects missing definitions or answer leakage that make the displayed task ambiguous.
Schema 2.4 records the answer-agreement method and confidence category.
Deterministic agreement is authoritative and does not call a model.
Only a deterministic mismatch can use the Gemini answer judge.
An accepted judge result has the `lower_confidence_llm_equivalent` category.
Direct scalar values use `direct-source-value-v1` in candidate provenance.
This contract binds the immutable numeric rule to the answer-verifier request.
The validator keeps explicit readers for candidate schemas `2.0.0` and `2.1.0`.
Each scope field identifies an independent qualifier for a result.
Scope fields do not contain a value that the question asks the model to supply.
This rule includes seasons, percentages, entities, locations, counts, directions, and relationships.
The question and context state each independent place, period, sample or cohort, method, comparison, and condition needed to interpret the result.
Generation keeps a sample descriptor in the population field, even when the descriptor contains Arctic or another place name.
Generation uses geography only for an independent place qualifier.
Generation uses comparison for an independent comparison or condition qualifier.

## Two-part evidence bundle

Schema `2.7.0` gives every generation role two kinds of source span.
A finding span in `SOURCE_DATA` is selectable evidence for the answer.
An interpretation span in `CONTEXT_ONLY_SOURCE` is study context only.
It states the place, the period, the population, the instrument, or an acronym expansion.
The eligibility classifier already selected and hashed these spans as `activity_spans`.
Generation re-locates each one in the source chunks through the same sha256 custody path.

`CONTEXT_ONLY_SOURCE` supports `question_context` statements only.
No role can select such a span as answer evidence, as a scope value, or as a required question phrase.
Generation drops a span that holds a figure, table or citation locator, a two-column join, or the answer text.
Candidate provenance records every forwarded span under `context_only_source`.
The validator re-checks each recorded span against the chunk bytes and its hash.

A whole-study paper has no span restriction, because the classifier certified every result as Arctic.
A separable Arctic component keeps its span restriction, and receives only the setting spans of that component.

The writer fills a `referent_slots` record with one entry for each of the ten referent slots.
The record is a diagnostic. No gate reads it, and it never supplies a slot that the displayed task leaves unfixed.

## Chapter 3 writer context

Chapter 3 candidates use generation prompt `arctic-qa-generation-v23` and candidate schema `2.8.0`.
The chapter 2 yield audit found that the locator filter erased 36 percent of the study-setting spans.
Schema `2.8.0` keeps the sentence and erases only the pointer.

Contract `question-context-redacted-evidence-v2` shows each model a locator-redacted projection of a context-only span.
The redaction removes a bracket citation, a parenthetical figure or table pointer, and a parenthetical author-year citation.
The projection must be a complete sentence with a finite verb, terminal punctuation, and at least 16 characters.
A sentence that still points at a figure or a table is not displayed.
The stored span keeps the raw chunk bytes under `text` and `text_sha256`, and records the projection under `display_text`.
The validator re-derives the projection from the raw bytes and rejects a candidate when the two differ.
The answer-leak test runs on the raw bytes and on the projection.
The module `src/arctic_qa/context_projection.py` holds the projection rules, so the validator and generation share one implementation.

The separable-component phrase test applies to a geography span and to a sample span.
A period, method, or definition span is exempt.
An unlabelled span (eligibility schema v3) keeps the phrase test only when it names a place.

Contract `freeze-time-finding-admission-v2` requires one `scope_evidence` entry for every non-null scope value.
The entry names the supplied span the value was copied from and the exact quote inside that span.
A value with no entry, an entry for a span the pipeline did not supply, a quote outside that span, or a value outside the quote is rejected with `finding_scope_value_unsourced` before the finding is frozen.
A cited context-only span is recorded in `scope_context_span_ids` and leads the bundle on every attempt on that finding.

Generation adds a definition span for each acronym that the displayed text leaves opaque.
A free string scan finds the first sentence that carries `expansion (TOKEN)` or `TOKEN (expansion)`, or that spells out an abbreviated binomial.
The sentence is forwarded with `span_role` set to `definition`, through the same projection and the same answer-leak filter.

Contract `referent-slot-resolvability-v2` replaces the presence test in the writer's slot checklist with a resolvability test.
Each slot record carries `resolver_text`: the displayed words that pick out one referent.
Provenance records the shadow result under `referent_slot_resolvability`.
No gate reads it in this release.

The writer prompt places a qualifier taken from `CONTEXT_ONLY_SOURCE` in `question_context` only.
A qualifier placed in `question_context` may bind to a forwarded context-only span.
A qualifier in the question stem still binds to the role evidence alone.
The prompt states the verbatim scope rule, asks for one supported setting sentence, and keeps every anti-leakage rule of version 22.

### Dimension-labelled study-setting spans

Eligibility response contract `eligibility-response-v4` labels each activity span with the study-setting dimension that its own text states.
The model answers with `eligible_arctic_scope.activity_spans`, an array of `{span_id, dimension}` objects, at most twelve.
The dimension is one of `geography`, `period`, `sample`, `method`, or `definition`.
The validator checks each label against the span text and rejects a label the text cannot support.
It also requires at least one activity span in the selected `study_geography` evidence, as an intersection test, not a subset test.

The forwarded record is the shape that `resolved_eligible_arctic_scope.activity_spans` carries into `sources.scope_evidence_json`.
Each entry holds the keys that contract v3 already wrote, plus one new key:

```json
{
  "span_id": "s000042",
  "locator": {"source_block_id": "text-block-00001", "section_id": "extracted-text", "page_id": null},
  "start_byte": 5120,
  "end_byte": 5402,
  "quote": "An Arctic Ocean research cruise was conducted aboard the R/V Mirai from October 24 to December 3, 2018.",
  "source_bytes_sha256": "…",
  "dimension": "period"
}
```

The `dimension` key is present only for a v4 record. A v3 record carries the same entry without it.
A consumer must treat a missing `dimension` as unknown and must not infer one from the span text.
Custody is unchanged: `quote` and `source_bytes_sha256` still bind the span to the frozen extraction, and the label never becomes selectable evidence.
Downstream code groups the forwarded spans by dimension for display and applies the separable-component phrase test to a `geography` span and to a `sample` span only.

## Chapter 2 gate contracts

The r15 holistic acceptance audit replaced four gate contracts.
The version strings are `source-blind-scientific-referent-v3`, `question-verification-v2`, `numeric-rule-source-support-v3`, and `displayed-option-structure-v1`.
Candidate provenance records all four.

The source-blind gate applies an interpretability test, not an identification test.
The judge states the task, names the answer type, and applies a necessity test to each missing detail.
A missing detail is necessary only when two readers can defend different answers, or when the reader cannot tell what kind of fact the task asks for.
The judge still reads only the question and the question context.
A study, publication, author, journal, dataset, or campaign identity is never a necessary detail.
A named campaign, cruise, core, or project code does not resolve a referent.

A deterministic screen runs beside the judge.
It rejects garbled benchmark text, a source pointer, a publication-relative period, and an acronym that the displayed text never expands.
The file `fixtures/standalone-calibration-v1.jsonl` holds the labeled calibration set for this contract.
Two human labelers must agree on each must-pass row before the set gates a production release.

The numeric contract states one metadata vocabulary in the writer prompt and in the schema field descriptions.
The `tolerance_basis` field carries the unit of the rule.
The `reported_precision` field is the decimal increment of the literal, or exact span text.
The `rounding_rule` field is `none` or `<N> decimal places`.
The `conversion_rule` field is `direct source literal`.
An exact integer count keeps its own separate vocabulary.

The answer verifier reports four separate results.
The field `relation_scope_match` carries relation and scope entailment only.
The field `scope_value_contradicted_by_source` carries a contradicted scope value, with the field name in `contradicted_scope_field`.
The field `scope_representation_note` records a wording or field-role difference and never changes a verdict.
Referent resolution and answer leakage keep their own fields.
