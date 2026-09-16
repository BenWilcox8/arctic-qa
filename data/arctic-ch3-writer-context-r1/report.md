# Chapter 3 writer-context slice - report

Task: `arctic-ch3-writer-context-r1`.
Branch: `fm/arctic-ch3-writer-context-r1`, based on local `main` at `bd2fb22`.
Plan: `data/arctic-chapter3/plan.md`, the row `arctic-ch3-writer-context-r1`.
Source: the chapter 2 yield audit, section 4.1, section 5 phase A row "Context bundle", and phase B rows "Separable filter", "Extractor prompt and freeze", "Definition retrieval", and "Writer prompt".

No paid provider call was made.
No file under `/mnt/crdata` changed.
No legacy or chapter 2 data changed.

## 1. Audit findings addressed

| Finding | Audit id | Change |
| --- | --- | --- |
| The locator filter deletes the study-setting span | 4.1 (a), E4, FS-1, W1 | Redaction on the display projection, complete-sentence rule, leak test on the displayed text |
| The binding rule never reads the context bundle | 4.1 (d) second half, W2, DG-5 | Context texts join the binding pool for a qualifier in `question_context` |
| The separable phrase filter deletes setting spans | 4.1 (c), E5 | Phrase test kept for geography and sample spans, other dimensions exempt, verifier instruction widened |
| No record says where a scope value came from | 4.1 (d), FS-2 | `scope_evidence` per scope value, `finding_scope_value_unsourced` at freeze, cited spans forwarded on every attempt |
| A context-only qualifier is placed by taste | 4.1 (e), W2 | Deterministic placement rule in the writer prompt |
| No stage retrieves a definition | 4.1 (f), W7 | Free gloss-sentence scan, forwarded as a `definition` span |
| The slot checklist is a presence test | 4.1 (g), W3 | Resolvability test, `resolver_text`, shadow record |
| The writer is told it can omit context | 4.1 (g), W5 | One supported setting sentence asked for, no deterministic empty-context reject |
| The writer never sees the display contract | W4 part 2 | Verbatim scope rule stated to the writer, no frozen-value rewrite |
| The uncertainty notation is not stated | 4.3 (d), DG-4 prompt half | Uncertainty notation for `tolerance_basis` in the extractor prompt |

## 2. Contract versions

| Constant | Before | After |
| --- | --- | --- |
| `GENERATION_PROMPT_VERSION` | `arctic-qa-generation-v22` | `arctic-qa-generation-v23` |
| `CANDIDATE_SCHEMA_VERSION` | `2.7.0` | `2.8.0` |
| `CONTEXT_ONLY_EVIDENCE_CONTRACT_VERSION` | `question-context-evidence-v1` | `question-context-redacted-evidence-v2` |
| `REFERENT_SLOT_CONTRACT_VERSION` | `referent-slot-checklist-v1` | `referent-slot-resolvability-v2` |
| `FINDING_ADMISSION_CONTRACT_VERSION` | `freeze-time-finding-admission-v1` | `freeze-time-finding-admission-v2` |

The `2.7.0` row of `CANDIDATE_CONTRACTS` now holds the four predecessor literals, so every stored chapter 2 candidate keeps its historical contract.
The new `2.8.0` row tracks the four owned constants.
Every other entry of the `2.8.0` row is a literal pinned at `bd2fb22`, with a comment that names the sibling slice that owns it.
A test asserts that the two rows differ only on the four owned keys.
The integration crew re-pins each literal to the version its slice lands.

`context_only_spans_resolve` accepts the v1 and the v2 block, so the replay of the 139 chapter 2 candidates validates under their own contract.

## 3. The rules, with their exact conditions

### 3.1 Context bundle (audit 4.1 a)

New module `src/arctic_qa/context_projection.py`.
Both `generation.py` and `validation.py` import it, so the validator re-derives the same projection.

`redact_locators(text)` removes only these pointers:

- a bracket citation `[8]`, `[3, 4]`, `[12-15]`;
- a parenthetical that carries a locator word (fig, figure, figs, tab, table, eq, suppl, sect, section, panel, appendix) with a number;
- a parenthetical that carries an author-year pair, such as `(Verlinde et al., 2016)` or `(Delanoe and Hogan, 2008, 2010; Ceccaldi et al., 2013)`.

Any other parenthetical stays, for example `(71.323 N, 156.615 W)` or `(number: MR18-05C)`.
After the removal, an empty `()` or `[]`, a space before punctuation, and a run of spaces are collapsed.

`context_only_display_text(text)` returns the projection, or `None` when the span is unusable.
A span is unusable when one of these is true:

- the raw bytes carry a run of eight or more spaces (a two-column join);
- the redacted text still matches `RESIDUAL_LOCATOR_PATTERN`, the locator half of the chapter 2 filter without the citation alternatives, so "Table 2 lists the twelve stations" still dies;
- the redacted text is shorter than 16 characters;
- the redacted text does not end in terminal punctuation;
- the redacted text carries no finite verb from a wide verb list.

`_context_only_span_is_usable` is now `context_only_display_text(text) is not None`.
`MAX_CONTEXT_ONLY_SPANS` stays at 12.

Each forwarded span record carries `text` and `text_sha256` on the raw chunk bytes, and `display_text` on the projection.
The model-facing span in `CONTEXT_ONLY_SOURCE` shows `text` as the projection and names the raw bytes through `source_text_sha256`.
The model never sees the raw bytes of a context-only span.
The provenance record under `context_only_source` stores both texts.
`validate_candidate` fails with `interpretation_span_not_located` when `display_text` differs from the projection re-derived from the stored bytes.

The answer-leak test runs on the raw bytes and on the projection, in `_forwarded_context_only_spans`, in `_qa_gate_reasons`, and in `validate_candidate`.

The binding pool: `question_qualifier_binding_reason` takes `question_context` and `context_only_texts`.
A recorded scope value that the question stem displays must be in the role evidence, unchanged.
A recorded scope value that `question_context` displays must be in the role evidence or in a forwarded context-only text.
`_qa_gate_reasons` passes the raw texts of the forwarded spans.

### 3.2 Separable filter (audit 4.1 c)

`_eligible_generation_scope` reads an optional `dimension` label on each activity record.
A label is one of geography, period, sample, method, definition.
The phrase test applies when the label is geography or sample.
The phrase test also applies when the record has no label and the displayed text names a place: a coordinate, or a capitalised word after the first word of a sentence that is not a month, weekday, or season name.
A period, method, or definition span is exempt.
The eligibility slice ships the labels; until then the interim place test decides.

The verifier instruction and the schema description of `interpretation_scope_applies_to_finding` now name a place, a period, a population, a sample, or a method, and ask the verifier to test population, sample, and method first.
`_require_arctic_scope_custody` and `scope_qualifier_not_displayed` are unchanged.

### 3.3 Extractor prompt and freeze (audit 4.1 d)

The extractor schema requires `scope_evidence`, an array of `{dimension, span_id, quote}`.
The prompt adds the audit's three sentences and restricts a citation to a supplied `SOURCE_DATA` span or a supplied `CONTEXT_ONLY_SOURCE` span.
The extractor receives exactly the methods and results spans the pipeline forwarded, so "methods and results spans" is implemented as "a supplied span".

`_scope_evidence_reason` runs in `_admit_ranked_finding` before the other admission checks.
It returns `finding_scope_value_unsourced` when any of these is true for a non-null scope value:

- no entry names its dimension;
- the entry's `span_id` is not a supplied finding span and not a forwarded context-only span;
- the quote is not inside that span, through the shared line-wrap projection (raw text or display text for a context-only span);
- the scope value is not inside the quote.

An entry for a null dimension is dropped.
On success the frozen answer keeps `scope_evidence`, and `scope_context_span_ids` lists every cited context-only span.
Those ids also join `interpretation_span_ids`.
`_finding_interpretation_spans` puts the cited spans first in the bundle on every attempt on that finding, and every span still passes `_forwarded_context_only_spans`.
The code is in `FINDING_ADMISSION_REASK_REASONS`, so the free re-ask runs once, and in the streaming alternative-finding sets.

The exact-span scope values, the prose-sentence rule, and the ban on a title or caption stay.

### 3.4 Definition retrieval (audit 4.1 f)

`_definition_context_spans` runs after the finding is frozen and before the writer call.
The flagged tokens are `validation.unresolved_acronym_tokens` over the finding quote, the required phrases, and the scope values, kept when a token has two capitals or a digit.
For each token, in chunk order, the scan finds the first `expansion (TOKEN)` or `TOKEN (expansion)` whose expansion plausibly glosses the token.
The strict test compares the initials of the last words, after a hyphen or slash split and a closed glue-word skip.
The fallback accepts an expansion whose first word starts with the token's first letter, so `shortwave radiation (SW)` is retrieved.
An abbreviated binomial such as `S. polaris` retrieves the first sentence that spells out the genus.
The whole sentence is forwarded, at most six per finding, as a hash-bound span with `span_role` and `dimension` set to `definition`.
It passes the same projection, the same complete-sentence rule, and the same answer-leak filter.
A sentence that overlaps the finding's own evidence is not forwarded.
The scan costs no model call.

The tokenizer belongs to `arctic-ch3-gates-r1`.
The scan reads it through the new public name `unresolved_acronym_tokens`, so the tokenizer fix reaches retrieval without a second change.

### 3.5 Writer prompt (audit 4.1 e and g)

`REFERENT_SLOT_DEFINITION` carries the audit's resolvability wording, including the four examples, and keeps the sentence the fixtures require.
`REFERENT_SLOTS_SCHEMA` requires `resolver_text` on each slot.
`REFERENT_SLOT_RECORD_INSTRUCTIONS` tells the writer what `resolver_text` is and keeps the diagnostic sentence.
Provenance records `referent_slot_resolvability` in shadow mode: a slot marked as stated is listed when `resolver_text` is empty, equals `displayed_text`, or is not in the displayed fields.
No gate reads it.

`QUESTION_CONTEXT_INSTRUCTIONS` opens with the audit's setting-sentence text, conditional on the source stating the place, period, or sample.
An empty context stays legal.
`QUESTION_CONTEXT_INSTRUCTIONS` and `REQUIRED_PHRASE_COVERAGE_INSTRUCTIONS` carry the placement rule: a qualifier from `CONTEXT_ONLY_SOURCE` goes in `question_context` and never in the stem.
The old "when the question reads better without it" sentence is gone.

`VERBATIM_SCOPE_DISPLAY_INSTRUCTIONS` states the verbatim scope rule to the writer and the direct-joint arm.
`SCOPE_ROLE_SEMANTICS_INSTRUCTIONS` is unchanged, because its version constant belongs to a sibling.

The extractor prompt tells the writer to set `tolerance_basis` to the exact uncertainty text, including the parentheses, and not to add a unit the source does not repeat.
Every anti-leakage rule, "Do not invent a slot value that neither source states", the study-identity ban, the study-local examples, `ANSWER_FORMAT_INSTRUCTIONS`, and `CLOSED_SET_INSTRUCTIONS` are unchanged, and a test asserts each one.

### 3.6 Not implemented (audit 4.10)

- No deterministic empty-context reject.
- No `MAX_CONTEXT_ONLY_SPANS` raise.
- No frozen-value rewrite.
- No bare-season period fail, and no W6 prompt text.

## 4. Rigor safeguard per change

Every forwarded span is hash-bound text of the same paper, rendered `selectable_for_answer_evidence: false`.
The standalone judge still reads only the question and the question context.

| Change | Why it cannot admit a paper-dependent or unsupported item |
| --- | --- |
| Redaction | Only characters are removed. A deleted `(Figure 1)` or `[8]` cannot add a claim, a place, a period, or an answer. The validator re-derives the projection from the stored bytes, so a tampered display fails custody. |
| Residual locator kept | A sentence whose meaning depends on an unseen figure or table is still never displayed. |
| Leak test on display | The test now covers what the reader sees, so it can only drop more spans. |
| Context texts in the binding pool | The stem pool is unchanged. The context pool adds only hash-verified same-paper text. A value in no span is still rejected. `interpretation_scope_applies_to_finding` still tests that the qualifier applies to this finding. |
| Separable exemption | A geography or sample span from another region still needs a scope phrase. `_require_arctic_scope_custody` and the display gate are unchanged. |
| `scope_evidence` | A new rejection. A scope value must now be verbatim in a named, supplied, hash-bound span, so an invented qualifier such as "Eurasian Basin" dies before any writer call. |
| Cited spans forwarded | They still pass the answer-leak filter and the column-interleave test. |
| Definition retrieval | Verbatim, hash-bound, same-paper text that defines a term. The leak filter removes a gloss sentence that states the answer. The gate still tests the expansion. |
| Resolvability test and `resolver_text` | Prompt and record only. The record never adds or removes a gate reason. |
| Placement rule and setting sentence | They add source-supported setting text only. Both leakage checks and `question_context_not_source_supported` still reject. |
| Verbatim scope rule | It states an existing gate, so the writer can obey it. The gate is unchanged. |
| Uncertainty notation | Prompt text only. The numeric gate is unchanged in this slice. |

## 5. Expected effect

Yield, from the audit's measurements on the 56 families:

- Redaction alone raises forwarded spans from 63 to 99, cuts zero-context families from 23 to 9, and cuts no-date families from 42 to 31.
- The separable exemption recovers up to 10 more spans and 3 more families from the zero-context group.
- The context-text binding pool clears the 19 `question_qualifier_not_evidence_bound` kills whose qualifier sat in a forwarded span.
- `scope_evidence` stops the repeat-failure lineages that 44 unsourced scope values fed (87 scope-binding rejections), and rejects the 9 values found nowhere before the first writer call.
- Definition retrieval converts a share of the 52 `question_context_referent_unresolved` kills.

The audit's estimate for section 4.1 as a whole is +1 to +3 accepted items on the same spend, and the removal of the scope-binding rejection class.
The starved cohort spent USD 6.39 per accepted item against USD 1.39 for the fed cohort.
Moving the starved cohort toward the fed rate is worth about USD 1.0 per accepted item at the chapter 2 spend.

Cost: no change in this slice adds a model call.
`scope_evidence` adds a few dozen output tokens per extraction call.
A definition span adds one sentence to the writer, reconstructor, and verifier prompts.
`finding_scope_value_unsourced` removes one writer call and three Pro judge calls, about USD 0.04 to 0.07, for every finding it stops.
The Pro short-circuit on `unavailable_in_source` or an empty `resolver_text` belongs to the cost slice; the shadow record gives it its input.

## 6. Tests

New file `tests/test_writer_context_chapter3.py`: 48 tests, one or more per rule above, each with the safeguard half.
Updated: `tests/test_writer_context_bundle.py` (the redacted citation case moved from "unusable" to "kept", the separable fixture names a place, the admission fixtures cite their span, the contract-version test now covers the 2.7.0 row), `tests/test_chapter2_integration.py`, `tests/test_cli_integration.py`, and `tests/test_streaming.py` (prompt-version and schema literals).
Fixtures: `fixtures/fake-author.jsonl` (extractor `scope_evidence`, writer `resolver_text`, the new prompt markers) and `fixtures/fake-verifier.jsonl` (the widened applicability markers).

Whole suite, run in bounded foreground parts with `nix develop -c bash -c 'PYTHONPATH=src pytest <files> -q'`:

| Part | Result |
| --- | --- |
| `tests/` without the three slow files | green |
| `tests/test_streaming.py` | green |
| `tests/test_cli_integration.py` | green |
| `tests/test_model_broker.py` | green |

## 7. Files touched outside the owned set

- `src/arctic_qa/validation.py`: the version constants and the `2.7.0` and `2.8.0` rows of `CANDIDATE_CONTRACTS` (owned versions, shared file); `question_qualifier_binding_reason` gained `question_context` and `context_only_texts`; `context_only_spans_resolve` accepts the v1 block and checks `display_text` under v2; `validate_candidate` runs the leak test on `display_text`; the three schema-version sets include `2.8.0`; the public `unresolved_acronym_tokens`; the import of `context_projection`.
- `src/arctic_qa/streaming.py`: `finding_scope_value_unsourced` in `ALTERNATIVE_FINDING_REASONS` and `IMMEDIATE_ALTERNATIVE_FINDING_REASONS`, with a one-line comment each.
- `src/arctic_qa/providers.py`: the fake provider hydrates `{{span_id}}` inside `scope_evidence` of a fixture answer.
- `src/arctic_qa/context_projection.py`: new module, the context code of this slice.
- `tests/test_streaming.py`, `tests/test_cli_integration.py`, `tests/test_chapter2_integration.py`: version literals only.
- `docs/BENCHMARK_INPUT_CONTRACT.md` (new section "Chapter 3 writer context") and `docs/STREAMING_DATASET.md` (one paragraph).

## 8. Deferred items

| Item | Owner |
| --- | --- |
| Eligibility schema v4 dimension labels on `activity_spans`. This slice reads `dimension` when present and applies the interim place test otherwise. | `arctic-ch3-eligibility-r1` |
| The tokenizer fix (hyphen and slash split, copulas, allowlist). Retrieval reads `unresolved_acronym_tokens`. | `arctic-ch3-gates-r1` |
| The numeric gate half of DG-4 (bounded unit inheritance). Until it lands, a `tolerance_basis` such as `(sd = 0.3)` written per the new prompt fails the v3 numeric gate. Land both in one release. | `arctic-ch3-gates-r1`, integration |
| Pro short-circuit on `unavailable_in_source` or an unresolved `resolver_text`. Input: `provenance.referent_slot_resolvability`. | `arctic-ch3-cost-r1` |
| Turning the resolvability shadow into a rejection after one measured run. | Phase E, captain |
| Re-pinning the sibling literals in the `2.8.0` row. | `arctic-ch3-integration-r1` |
| The live export contract `config/live-dataset-current-contract-v1.json` still selects `2.7.0` and v22. It moves only with a captain instruction after a chapter 3 run has items. | Captain |
| Per-sentence residual locator test. The audit's amended rule tests the whole span, so a span with one figure-pointing sentence beside a study-site sentence still dies. | Phase E measurement |
| A `scope_evidence` re-check inside `validate_candidate` for stored candidates. The freeze-time check is the gate in this release. | Integration, if wanted |
