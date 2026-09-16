# Context viewer update

## Result

The readable QA record now shows a nonempty top-level `question_context` after the question.
It explains that the benchmark model receives this separate context with the question.
The context remains separate from source evidence and rationales.
Legacy records without the field state that no question context was retained.
Records with an empty context show no context block.

The trace API passes the field through unchanged.
The readable view uses text-only DOM operations.
The raw JSON controls, downloads, answers, reconstructions, and rejection displays remain unchanged.

## Method display

The vertical diagram now describes the prepared latitude-first correction.
It uses actual study sites at or above 66.56 degrees north, on land or sea.
Separable Arctic components remain for scoped QA.
Insufficient or inseparable evidence is unresolved.
Historical decisions remain retained and do not claim the prepared correction.

The correction is prepared pending activation.
After the supervisor confirms activation, update this wording to state the active version.

## Validation

`nix develop --command env PYTHONPATH=src pytest tests/test_corpus_viewer.py` passed with 20 tests.
The focused trace route test covers a present context and a legacy absent field.
Ruff, JavaScript parsing, and whitespace checks passed.
The shared health route returned `available`.

Browser validation did not run.
`chrome-devtools-axi` could not attach because this host has no Chrome stable executable.

## Deployment

This branch made no shared-service change.
The local-only delivery contract keeps deployment with the supervisor.
Use the recorded private runtime method after review and activation.
