# Standalone calibration harness

This document tells you how to operate the calibration harness of the source-blind standalone gate.
Read it before you change `STANDALONE_SYSTEM` in `src/arctic_qa/generation.py` and before you record a cassette.

## What the harness gates

The standalone gate is the composed decision in `validation.standalone_gate_decision`: the deterministic fail-list screen unioned with the typed codes of the source-blind judge.
The calibration set `fixtures/standalone-calibration-v2.jsonl` holds labelled displayed tasks.
The release rule is stated in the set header: no `must_fail` row may pass the composed decision, and at least 80 percent of the `must_pass` rows must pass it.

The chapter 2 yield audit (stage `standalone_gate`, finding F6) found that the v1 set gated nothing behavioural: the tests fed the composed decision hard-coded judge codes, so the judge prompt never ran.
The harness in `src/arctic_qa/standalone_calibration.py` closes that gap with a recorded cassette.

## Rows and labels

Every row carries two labelers as fields under `labels.labeler_1` and `labels.labeler_2`.
The gating `label` is their agreement.
A row whose labelers disagree carries `label: disputed`.
A disputed row stays in the file for the audit trail and never gates: it is not recorded in a cassette and it is not scored.

Rows carry a `slice`.
The `core` slice holds the r15 audit rows.
The `held_out` slice holds chapter 2 candidates that the yield audit judged.
Do not use a `held_out` row to iterate the prompt text.
The held-out slice measures a prompt revision after the revision is written.

The set keeps the typed reason codes in `model_reasons`.
The tests in `tests/test_standalone_calibration.py` use them to exercise the deterministic half offline.
Neither labeler of v2 is a human; the header's `blocking_note` records that limit.

## Record a cassette

Recording calls the configured `standalone_verifier` model once per gating row.
It is a paid operation.
Do not record from a test and do not record without an authorized spend.

```
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --data-root <root> --json \
  calibrate-standalone --mode record --provider broker \
  --cassette data/<task>/standalone-calibration-v2.cassette.jsonl \
  --run-id <run-id> --phase calibration \
  --streaming-budget-policy-file <policy> --price-config-file config/gemini-eligibility-v1.json \
  --execution-gate-file <gate> --shared-ledger-file <ledger> --model-receipts-dir <receipts> \
  --credential-file <credential> --prior-construction-spend-usd <usd>'
```

The broker binds every request to the paper identity `standalone-calibration` and to the row's family id, so each call leaves a receipt.
The cassette is written only after every call succeeded.
Its header binds the SHA-256 of `STANDALONE_SYSTEM`, the calibration set version, the requested model and the call parameters.
The command prints the recording summary and the release-rule report, and exits with code 1 when the rule fails.

For an offline check of the record path, pass `--provider fake --provider-script <events.jsonl>`.
The fake provider returns one scripted `standalone_verifier` response per gating row, in file order.

## Replay a cassette

```
nix develop -c bash -c 'PYTHONPATH=src python -m arctic_qa --data-root <root> --json \
  calibrate-standalone --mode replay --cassette <cassette>'
```

Replay makes no call.
It refuses a cassette whose header hash differs from the current `STANDALONE_SYSTEM`, so a prompt change always forces a fresh recording.
It refuses a cassette that lacks a gating row or whose stored prompt differs from the row.
The report lists every scored row with the judge's codes, the composed decision and whether the row passed, then the must-pass rate, the must-fail violations and `passed`.

## Change the prompt or the set

1. Edit the prompt or add rows.
2. Bump `STANDALONE_CALIBRATION_SET_VERSION` in `src/arctic_qa/validation.py` when the row set changes, and write the same value into the set header.
3. Run the offline tests: `nix develop -c bash -c 'PYTHONPATH=src pytest tests/test_standalone_calibration.py -q'`.
4. Ask for the calibration spend, record a fresh cassette, replay it, and keep the cassette and the report with the task that changed the prompt.
