# Fixtures

This directory contains small inputs for tests, free demonstrations, and offline replays.
No file in this directory contains a real research article or a credential.

## Pipeline demonstration files

| File | Purpose |
| --- | --- |
| `public-source-metadata.json` | Synthetic metadata for the free `smoke` pipeline. |
| `public-source.html` | Synthetic article text for the free `smoke` pipeline. |
| `fake-author.jsonl` | Recorded writer answers for the fake provider. |
| `fake-verifier.jsonl` | Recorded judge answers for the fake provider. |

The `smoke` command reads these files through `--fixture-dir fixtures`.
It writes all results under the configured data root, not into this directory.

## Eligibility regression files

| File | Purpose |
| --- | --- |
| `eligibility-pdf-indentation.txt` | Extracted text with indentation that once affected eligibility parsing. |
| `eligibility-repeated-footer.txt` | Extracted text with a repeated page footer. |
| `eligibility-rescreen-recorded-r2.json` | A recorded geography re-screen response. |
| `ch2-eligibility-non-eligible-v1.jsonl` | Chapter 2 non-eligible examples for replay tests. |

## Calibration sets

| File | Purpose |
| --- | --- |
| `standalone-calibration-v1.jsonl` | The first labeled set for the source-blind standalone gate. |
| `standalone-calibration-v2.jsonl` | The revised labeled set for that gate. |
| `standalone-calibration-ch3-judge-slice-v1.jsonl` | The chapter 3 judge slice and its release labels. |
| `r14-audit-priorities-r1.json` | Priority records from the r14 gate audit. |
| `r15-audit-priorities-r1.json` | Priority records from the r15 gate audit. |

The labeled sets define expected outcomes.
Recorded provider answers live in calibration cassettes under `research/`.

## Cost-guard quota samples

| File | Purpose |
| --- | --- |
| `quota-axi-2026-09-16T10-41Z.json` | A complete recorded quota report for a free guard cycle. |
| `quota-axi-claude-session-low.json` | A Claude session window near its guard threshold. |
| `quota-axi-codex-exhaustion-clear.json` | A Codex report after an exhaustion condition clears. |
| `quota-axi-codex-weekly-low.json` | A Codex weekly window near its guard threshold. |
| `quota-axi-fable-weekly-low.json` | A Fable weekly window near its guard threshold. |

The guard reads these files only when a command names `--recorded-quota-file`.
The files let tests exercise pause rules without reading a live subscription account.

## Maintenance rule

Keep a fixture small and safe to publish.
If a regression needs source text, replace that text with the smallest synthetic example that preserves the fault.
