# Arctic scope layout report

This change adds a comparison-only projection for selected scope text.

The projection collapses safe PDF whitespace and removes a hyphen only when a line break separates two alphabetic characters.

It does not change evidence quotes, source span IDs, offsets, hashes, locators, or provenance.

It does not join separate spans, complete truncated words, alter numbers or units, change negation, or change coordinates.

## Frozen record audit

| Cohort index | DOI | Saved result | Narrow disposition |
| ---: | --- | --- | --- |
| 7 | `10.5194/tc-20-4313-2026` | `eligibility_unresolved` / `eligible_arctic_scope_phrase_unbound` | Unresolved. The phrase is split across finding spans. Cross-span context remains unsupported. |
| 17 | `10.1007/s00484-023-02531-2` | `eligibility_unresolved` / `eligible_arctic_scope_phrase_unbound` | Unresolved. `Mys Vanka-` is terminal and cannot become `Mys Vankarem`. |
| 20 | `10.1038/srep34456` | `generation_rejected` / `reconstruction_evidence_span_not_found` | Unresolved. BP expansion and reconstruction context are not supplied by this matcher fix. |
| 23 | `10.1128/aem.00117-09` | `generation_rejected` / `reconstruction_disagreement` | Still rejected. The saved answer quote has interleaved column text, and reconstruction comparison ends at `win-`. |

The new regression proves that `photosyn-\nthetic` binds to `photosynthetic` when the selected evidence contains the complete phrase.

The saved payload check read only the four frozen records and preserved their recorded outcomes.

## Validation

Focused tests passed with 15 tests.

The tests cover line-break hyphenation, safe whitespace, numbers, signs, ranges, units, negation, coordinates, incomplete words, invalid IDs and hashes, and cross-source evidence.

Ruff passed for the changed Python files.

`git diff --check` passed.
