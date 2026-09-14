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
New candidates use generation prompt `arctic-qa-generation-v17`.
They use scope contract `selected-evidence-literal-scope-v4` and candidate schema `2.3.0`.
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
