# Benchmark input contract

Each model-facing benchmark item contains `question` and `question_context`.
An empty `question_context` means that the question is self-contained.
An external evaluator must provide both fields to the evaluated model.
The evaluator must keep the fields separate in the rendered input.

The question contains the complete task.
The context contains only information that is necessary to understand that task.
Context can define an unfamiliar acronym, identify a referent, or distinguish a study group or measurement.
Context does not contain a second task.

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
