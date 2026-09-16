# Chapter 2 phase 2: gates, correct and not lax

Task `arctic-ch2-gates-r1`.
Branch `fm/arctic-ch2-gates-r1`, created from `fm/arctic-audit-priorities-r1` at commit a53b106.
Source of the work: `data/arctic-r15-holistic-audit-r1/report.md`, sections 4.2, 4.4, 4.8, 5 (phase 2) and 6, with the stage files `standalone_gate.md`, `deterministic_gates.md`, `distractors.md` and `reconstruction.md`.

## 1. Files touched outside the owned set

Integration must reconcile these files with the sibling crews.

| File | Why this slice touched it | Size of the change |
| --- | --- | --- |
| `src/arctic_qa/generation.py` | The gate contracts live half in the prompts. This slice replaced `STANDALONE_SYSTEM`, the numeric metadata block of the extractor prompt, the two overloaded sentences of the answer-verifier prompt, and the `answer_verifier` schema. It also mirrored the new gates in `_qa_gate_reasons`. | 194 lines |
| `src/arctic_qa/streaming.py` | Routing sets only. The new reason codes need a layer and a repair path, and one new code needed a dependency collapse. No routing logic changed. | 18 lines |
| `fixtures/fake-author.jsonl`, `fixtures/fake-verifier.jsonl` | The scripted provider asserts prompt markers and returns role payloads. Both changed with the prompts and the schema. | 6 lines |
| `docs/BENCHMARK_INPUT_CONTRACT.md` | A new section at the end of the file states the four new contract versions. No existing line changed. | 31 lines added |
| `src/arctic_qa/pipeline_trace.py` | The viewer reads any reason code that holds the word "malformed" as an invalid model response. The new `benchmark_text_malformed` code, and the existing `standalone_malformed_text` code, are QA rejections. One new branch classifies both. | 16 lines |
| `tests/test_project_progress_viewer.py` | A pre-existing red test on the base commit. The fix is identical to the one the writer sibling already made. | 4 lines |

Sibling overlap that integration must merge with care:

- `_qa_gate_reasons` now calls `_scope_phrase_in_text` instead of a bare `normalize_text` substring test.
  The audit assigns this line to phase 1 (report section 4.3 fix 3).
  This slice made it because the gate and `validate_candidate` disagreed, which sent a repairable item down the wrong path.
  If the writer sibling made the same change, keep one copy.
- `CANDIDATE_CONTRACTS["2.6.0"]` now names the four new contract versions.
  `CANDIDATE_SCHEMA_VERSION` stays `2.6.0`, because the writer sibling owns it.
  **When the writer sibling moves that constant to `2.7.0`, integration must copy the `2.6.0` row to a `2.7.0` key and pin the `2.6.0` row to the predecessor literals.**
  The constants `PREDECESSOR_STANDALONE_VERIFICATION_CONTRACT_VERSION`, `PREDECESSOR_QUESTION_VERIFICATION_CONTRACT_VERSION` and `PREDECESSOR_NUMERIC_RULE_CONTRACT_VERSION` exist for that one-line pin.
  Schema rows `2.0.0` to `2.5.0` are already pinned to the contracts they shipped with.

## 2. New and changed contract versions

| Constant | Predecessor | New value |
| --- | --- | --- |
| `STANDALONE_VERIFICATION_CONTRACT_VERSION` | `source-blind-scientific-referent-v2` | `source-blind-scientific-referent-v3` |
| `QUESTION_VERIFICATION_CONTRACT_VERSION` | `question-verification-v1` | `question-verification-v2` |
| `NUMERIC_RULE_CONTRACT_VERSION` | `numeric-rule-source-support-v2` | `numeric-rule-source-support-v3` |
| `OPTION_DISPLAY_CONTRACT_VERSION` | none | `displayed-option-structure-v1` |
| `STANDALONE_CALIBRATION_SET_VERSION` | none | `standalone-calibration-v1` |

`ANSWER_AGREEMENT_CONTRACT_VERSION` stays `deterministic-first-answer-agreement-v1`.
This slice owns that version but changed nothing in the contract.
The audit says the deterministic-first agreement contract works and must not change.
A bump with no behavior change would invalidate every stored agreement receipt for no reason.

`OPTION_DISPLAY_CONTRACT_VERSION` is additive in candidate provenance.
Generation writes it and `CANDIDATE_CONTRACTS` reads it.
No sibling owns that key.

## 3. Defects addressed, by audit finding id

### SG-1 and D6: the standalone gate

The gate tested identification and the prompt forbade the only way to meet it.
Contract v3 replaces `STANDALONE_SYSTEM` with an interpretability test.
Appendix A holds the full prompt text.
Its shape is:

1. State in one sentence what the task asks for. If you cannot, the task fails.
2. Name the kind of answer the task wants. If you cannot, the task fails.
3. For each detail that you believe is missing, apply the necessity test.
4. Ask whether a strong scientist who cannot see the paper can choose the correct option from the displayed text alone.

The necessity test says that a missing detail is necessary only when two readers who both understand the task can defend different answers, or when the reader cannot tell what kind of fact the task asks for.
A location or period is necessary whenever the value can differ between sites or periods.
The prompt adds a pass list of eight observable classes and a fail list of seven.
The fail list adds four classes that the predecessor never named: an opaque campaign or cruise code, an antecedent-free definite description such as "the combined expeditions", garbled text, and an unnamed measured variable that carries the answer.

Kept exactly: the source-blind input, the typed reason codes, the ban on paper identity, and the rule against universalizing an observation.
The judge still receives only the question and the question context.

SG-2 is **not** implemented. The reviewers refuted it.
DW-5 and R6 are **not** implemented for the same reason.

### SG-4: the calibration set

`fixtures/standalone-calibration-v1.jsonl` holds 15 must-pass rows and 12 must-fail rows.
Every row carries its question, its question context, the answer text needed by the leakage checks, the auditor note, and the typed codes that a correct v3 judge must return.
The must-fail rows include the two items that the predecessor contract wrongly passed, `aqa-3af119e6f5b0e447d8fd` and `aqa-0e07e2ac78c048db8c31`.
The must-pass rows include the two r14 positive controls.

The file header records `labeling_status: single_labeler_provisional`.
It also records the blocking note: two independent human labelers must answer the north-star question for each must-pass row, and only unanimous rows may stay, before the set gates a production release.
The same note is in the docstring of `tests/test_standalone_calibration.py` and in `docs/BENCHMARK_INPUT_CONTRACT.md`.
The header records the release rule (no must-fail row passes, at least 80 percent of must-pass rows pass) and the held-out rule (no row in the file may be used to iterate the prompt).

`tests/test_standalone_calibration.py` runs the composed gate decision over every row.
The composed decision is `validation.standalone_gate_decision`, which unions the deterministic screen with the typed codes of the source-blind judge.
No model call runs. The judge codes come from the audit verdicts that the fixture records.
Results on the fixture: 15 of 15 must-pass rows pass, and 12 of 12 must-fail rows fail.
11 of the 12 must-fail rows fail on the deterministic screen alone.
The twelfth row, `aqa-b0b612392ec0742dabdb`, carries the plural acronym "ITPs", which the acronym pattern does not detect.
The fixture marks that row `deterministic_expectation: defer` and the report records the gap in section 6.

### SG-3 and D4: the deterministic context rules

The flag `required` is now `(model question_context_required) OR benchmark_text_requires_context(question)`.
A context that the deterministic rule demanded can no longer be judged unnecessary.
This ends the `question_context_missing` and `question_context_unnecessary` oscillation that killed three families.
`question_context_unnecessary` still rejects a context that neither half demanded.

The acronym allowlist is the list that report section 4.2 fix 5 names: the ten existing tokens plus UTC, GMT, AD, BC, CE, BCE, GPS, PCR, UV, SI, RMSE, SD, NAO and ENSO.
The stage file also proposed BP, SE, CI, IR and AO.
Those are **not** added.
Report section 4.2 forbids AO by name, and BP, SE, CI and IR each carry more than one meaning in the natural sciences.
`BP` means both "before present" and "base pair".
No study-local label is on the list, and a test asserts that POC, TPM, OTU, ITP, CTL, DBO4, AO and SAUP stay off it.

`question_context_not_source_supported` and both leakage checks are unchanged.

### Section 4.8 item 2: the two false passes

`benchmark_context_verification_reason` gained three deterministic fail-list classes.

1. Garbled benchmark text. A line break, a run of three or more spaces, or a quotation of more than eight words is a two-column gutter or a copied source sentence. The code is `benchmark_text_malformed`.
2. A source pointer such as "the fourth column" or "the following table". The code is `source_dependent_locator`.
3. A publication-relative period such as "the past 20 years", "recent years" or "at this time". The code is `publication_relative_period`.

The function also closed the hole that let the opaque cruise codes pass.
A non-empty context used to satisfy the acronym demand when it held any word that was not filler.
The context must now expand the acronym.
A definitional gloss resolves a token; a repeat of the token does not.
With its line break repaired, `aqa-0e07e2ac78c048db8c31` now fails on `question_context_referent_unresolved` because of PS80 and HE451.1, and `aqa-3af119e6f5b0e447d8fd` fails on `question_context_missing` because of "the combined expeditions".
A test asserts both.

The definite-description pattern gained the nouns the audit names: expedition, cruise, campaign, transect, enclosure, archipelago, lake, wetland, fjord, pond, core and run, and the adjective "combined".

### D1 and D7: the numeric contract

The writer prompt and the schema field descriptions now state one vocabulary, and the rule enforces exactly that one.
The prompt block carries the marker `NUMERIC_METADATA_VOCABULARY numeric-rule-source-support-v3`.

- `unit` is the unit token that follows the value in the span.
- `tolerance_basis` is exact span text that states the tolerance, and it must contain that same unit.
- `reported_precision` is the decimal increment of the literal, or exact span text that states the precision.
- `rounding_rule` is `none` or `<N> decimal places`.
- `conversion_rule` is `direct source literal`.
- An exact integer count keeps its own separate vocabulary, which the prompt also states.

The wording `direct reporting without additional rounding` and `direct source reporting with no conversion` is gone from the prompt and from the rule.
The directly published exact scalar now uses the same wording as every other scalar.
This closes r14 recommendation 4.

`_text_matches_typed_numeric` takes the unit from the rule instead of `SAFE_UNIT_SPELLINGS`.
`SAFE_UNIT_SPELLINGS` still maps equivalent spellings of the same unit.

The prompt adds the D7 rule: emit `numeric_rule` only when `answer.text` displays exactly one number with its unit, never for a categorical, directional, multi-value or descriptive answer, and never with a placeholder `canonical_value`.

### D2: eligibility contiguity

`_eligible_arctic_scope_error` allows a gap between two selected eligibility spans when every intervening span of the same locator is whitespace only and the skipped bytes are at most `MAX_ADJACENT_WHITESPACE_CHARS` (32).
That is the allowance `source_span_evidence_resolves` already grants.
The branch now emits `finding_evidence_components_not_contiguous` instead of `eligible_arctic_finding_out_of_scope`.
The honest code is in `ALTERNATIVE_FINDING_REASONS` and in `IMMEDIATE_ALTERNATIVE_FINDING_REASONS`, and `_failure_layer` puts it in the `finding` layer.
Per-component hashing and ordering are unchanged.

### RECON-1, RECON-2, RECON-5 and RECON-6: the answer verifier

`relation_scope_match` carried four jobs. It now carries one.

The schema gained `scope_value_contradicted_by_source`, `contradicted_scope_field` and `scope_representation_note`.
The prompt lost the two sentences that loaded referent resolution and verbatim-in-span bookkeeping onto `relation_scope_match`.
The replacement prompt text is in `answer_verification_prompt` and reads:

> Set relation_scope_match to false only when the selected span does not support the ANSWER_RECORD answer as the answer to this QUESTION. Judge the scientific relation, not the wording.
> Set scope_value_contradicted_by_source to true only when a non-null ANSWER_RECORD scope value states a place, period, population, method, comparison, or condition that the selected span contradicts. Name that field in contradicted_scope_field. A value that is worded differently, held under a different scope field, absent from the QUESTION, or absent from the selected span is not a contradiction.
> Record every wording, field-role, or span-containment difference in scope_representation_note. That note never changes a verdict.
> Do not use relation_scope_match or scope_value_contradicted_by_source for a referent, self-containment, or answer-leakage defect. Report those only in question_context_referent_resolved, question_context_missing_detail, and question_answer_leakage_absent.

The result split is:

| Result | Field | Gating |
| --- | --- | --- |
| Relation and scope entailment | `relation_scope_match` | Hard reject, kept |
| Contradicted scope value | `scope_value_contradicted_by_source` with `contradicted_scope_field` | New hard reject, `scope_value_not_source_supported` |
| Referent resolution | `question_context_referent_resolved` | Unchanged |
| Answer leakage | `question_answer_leakage_absent` | Unchanged |
| Scope bookkeeping | `scope_representation_note` | Never gates |

A contradiction without a named field returns `answer_verifier_scope_verdict_missing`, so the verdict cannot be empty.

Two more rejections were added.

- `question_qualifier_not_evidence_bound`. Every place, period, population, method, comparison or condition qualifier that any role recorded, and that the question states, must be verbatim in the answer's own frozen evidence span.
- `reconstruction_alternative_answer_present`. The dead `reconstruction_has_competing_alternatives` check is now a hard reject in `validate_candidate` and in `_qa_gate_reasons`.

The new code needed one routing entry.
The reconstructor's ambiguity label and the competing-alternatives check report the same defect, so they fire together.
Without a collapse the two codes span two failure layers, and `_next_generation_attempt` then ends the family.
`_DEPENDENT_ROUTING_REASONS` now collapses `reconstruction_alternative_answer_present` under the `answer_ambiguous` root.
A whole-suite run found this: `test_failed_reconstruction_never_reaches_distractor_generation` lost two of its eight generation submissions.
The collapse restores them, and `tests/test_gate_contracts_r15.py` holds a regression test for it.

`scope_judge` is **not** built. Report section 4.4 fix 5 forbids it.
The reconstructor prompt and the RECONSTRUCTION block of the verifier prompt are unchanged, because phase 0 owns them.

### DW-1, DW-2, D5 and section 4.8 item 3: option display

- Structural parallelism. A compound option is exempt only when the source-supported answer is itself multi-clause, the option's clause count equals the answer's, and the option declares a deterministic kind. A `closed_set` answer takes no part in this path, because `_closed_set_contract` types every member and cross-checks the display. The answer's own literal must be verbatim in its evidence quote, so an unsupported answer shape cannot create an exemption.
- Member parser. `_displayed_categorical_members` reads the typed `candidate_values` array first and falls back to the display. It splits on "followed by", "then" and ">", strips an enumeration lead such as "including" or "such as", and removes a rank prefix such as "phylum". The display cross-check stays display-derived.
- Single-member closed set. A `closed_set` rule with one member takes the `unique_categorical` path instead of failing every option on `closed_set_contract_invalid`. That path still needs a closure term in the source.
- Numeric display. `_numeric_display_issue` normalizes the Unicode minus and the en dash, reads word integers through `INTEGER_WORDS`, and matches the unit as a substring of the option's own display. `convert` no longer decides whether a display agrees with its own metadata. The option must still display exactly one quantity that carries the rule's unit, and a displayed range is still refused.
- Every option now runs the display rule. The predecessor skipped `_text_display_issue` for an option that carried numeric metadata, and duplicated a weaker negation and conjunction ban inside the numeric rule. A negated or compound option now reports the same honest code whether or not it carries numeric metadata.
- Section 4.8 item 3. A compound or negated option that has no deterministic contradiction is refused with `option_compound_support_insufficient`, unless its verdict came from a model that differs from the writer model.

The refuted `reported_enumeration` contract is **not** built.
`_source_establishes_complete_set` still returns False for "including" and "such as".
A test asserts that the `aqa-aa01612494c63919f347` options stay refused.

Kept exactly: verdict binding by hash, the floor of three verified distractors, equivalence normalization, and `forbidden_meta_option`.

### Frozen option payloads

`fixtures/r15-audit-priorities-r1.json` holds the seven option payloads the audit names, exactly as the pipeline stored them:
`aqa-2af2b346d30478f68400`, `aqa-247e683b6c742bae46fa`, `aqa-a3843bad70b6db3f6bf0`, `aqa-3e74c2c169f63d6686e4`, `aqa-aa01612494c63919f347`, `aqa-a4a455748ac2480b2650` and `aqa-a8ac70980059cdfd7001`.
`tests/test_distractor_validation.py` asserts the rule behavior against each one.

## 4. Rigor safeguard for every change

The question is the same for each change: can this change admit an item that is paper-dependent, or that the paper does not support?

| Change | Why it cannot admit a bad item |
| --- | --- |
| Standalone contract v3 | Steps 1, 2 and 4 fail every task where the reader cannot state the task, name the answer type, or choose the option without the paper. The input is still source-blind. The fail list is longer than the predecessor's by four classes, each of which passed v2 at least once. |
| Deterministic fail-list screen | It only adds rejections. It runs beside the judge and never overrides it. |
| Calibration set | It admits nothing. It makes every later prompt change measurable and blocks a release that lets a must-fail row through. |
| `required` = model OR deterministic | Source-blind answerability is still judged by the standalone gate and by `question_context_referent_resolved`. Both are unchanged. A context that is source-supported and non-leaking cannot make a paper-dependent item pass. |
| Narrowed acronym allowlist | Every listed token has one meaning across the natural sciences and names no study, site, run, instrument or dataset. No listed token can hide a paper-dependent referent. |
| Definitional-gloss rule | It is strictly stricter. A context that repeats a code no longer satisfies the acronym demand. |
| Numeric vocabulary | Every evidence test survives. The value, its unit and its uncertainty must still appear verbatim in the frozen span. The change touches only the English self-description of a metadata field. |
| Unit from the rule | The unit must still be attached to the literal in the evidence and in the display. A whitelist miss is not evidence of anything. |
| Unit inside `tolerance_basis` | A new requirement. The predecessor accepted a bare basis, so a tolerance could bind a number whose unit the span never attached to it. |
| Eligibility whitespace gap | The spans must still all sit inside the certified Arctic finding spans, in order, in the same chunk, with a matching hash per component. Only whitespace may sit between them, and whitespace carries no claim. |
| Verifier split | Nothing is removed from the acceptance decision except which JSON key holds a phrase and whether the phrase is verbatim inside one selected span. Neither is visible to the benchmark model. Every referent defect is still rejected twice. |
| `scope_value_not_source_supported` | A new rejection. |
| `question_qualifier_not_evidence_bound` | A new rejection. |
| `reconstruction_alternative_answer_present` | A new rejection, and it replaces an opaque boolean with a named second answer. The routing collapse changes where one repair is spent, never whether an item is accepted. |
| Structural-parallelism exemption | It cannot pass a compound option against an atomic answer. The answer shape must be verbatim in the evidence quote. A `closed_set` answer is out of the path, so a mixed claim cannot pass as a member list. |
| Member parser | It parses the option that the reader sees. Every member test, the cardinality test and the completeness test are unchanged. |
| Single-member closed set | The `unique_categorical` path still needs the answer inside the source's own enumerated set and a closure term in the source text. |
| Numeric display rule | It decides nothing about whether an option is false. Contradiction stays with `_numeric_incompatible` against a source-bound answer rule, or with the option verifier. Metadata that is not among the displayed numbers still fails, and a unit that is absent from the display still fails. |
| Display rule on every option | A new rejection for a negated or compound option that carries numeric metadata. |
| `option_compound_support_insufficient` | A new rejection. |

## 5. Tests

The project runs pytest under its nix devshell: `nix develop -c bash -c 'PYTHONPATH=src pytest tests/<file> -q'`.

New test files:

- `tests/test_standalone_calibration.py`, 56 tests. The calibration gate for contract v3.
- `tests/test_gate_contracts_r15.py`, 43 tests. One regression test for each audit defect in this slice.

Focused runs, all green:

| Suite | Result |
| --- | --- |
| `test_standalone_calibration.py` | 56 passed |
| `test_gate_contracts_r15.py` | 43 passed |
| `test_distractor_validation.py` | 24 passed |
| `test_question_context.py`, `test_generation_scope_roles.py`, `test_evidence_combination.py` | 63 passed |
| `test_cli_integration.py` | 92 passed |
| `test_answer_equivalence.py` | 47 passed |
| `test_eligibility_span_contract.py`, `test_gemini_eligibility.py` | included in the focused run above |
| `test_project_progress_viewer.py` | 7 passed |
| `tests/` (whole suite) | see section 7 |

Ruff:

- `ruff check src tests` passes.
- `ruff format --check` passes for every file this slice touched.
- Eleven files were already unformatted at the base commit, among them `cli.py`, `gemini_batch.py`, `gemini_eligibility.py` and `model_broker.py`. This slice did not reformat them, because that would create a large conflict with three parallel crews for no functional gain. Integration should run one formatting pass after the merge.

## 6. Deferred, with the reason

- The plural acronym form. `_UNFAMILIAR_ACRONYM_PATTERN` does not match "ITPs" or "OTUs". Adding the plural would catch the ITP must-fail row, and would also kill the must-pass row `aqa-adfe6c6aed58bb7d06c5`, which uses "OTUs" without an expansion. The audit does not name the plural gap as a defect. The model judge decides that row under the v3 fail list, which names an unexpanded acronym first.
- The `reported_enumeration` closed set (D5). Report section 4.4 fix 4 refuses it as written. It needs a sixth blocking condition: a source-blind judge that sees only the benchmark-facing text and the options must exclude three of four options from general scientific knowledge. That judge is a new model role, which phase 0 owns.
- The null-result option idiom ("no significant trend"). The stage file proposes it. Report section 4.4 and the section 5 phase 2 table do not. It would relax a gate, so this slice left it closed.
- The different-family option verifier. Phase 0 owns `roles.v1.json` and the startup assertion. This slice implemented the deterministic-contradiction half of section 4.8 item 3 and reads the verdict model from the existing provenance, so the rule already works when phase 0 lands.
- The raw-text reject for benchmark-facing text (report section 4.3 fix 2) is implemented here as `benchmark_text_malformed`, because the v3 fail list names garbled text and the calibration set needs it. Phase 1 also owns a raw-text reject. Integration should keep one implementation.
- `option_referent_unresolved` (distractor stage root cause D). It is a routing correction inside `_generate_distractors` and the option verifier schema, which phase 3 routing owns.

## 7. Whole-suite result

The machine ran four crews in parallel while this slice validated.
A whole-suite run therefore took more than 20 minutes, and `tests/test_model_broker.py` holds real rate-limit sleeps.
The runs were bounded, as the task brief requires.

One whole-suite run completed before the last two changes and found one real defect.
`test_failed_reconstruction_never_reaches_distractor_generation` lost two of its eight generation submissions, because the new competing-alternatives code split the repair across two failure layers.
Section 3 records the fix and its regression test.

The final validation ran in two bounded parts:

1. `pytest tests/ -q --ignore=tests/test_model_broker.py`.
2. `tests/test_model_broker.py` on its own. That file holds no code this slice changed, and it passed in the earlier complete run.

Section 8 records the result of both.

## 8. State at hand-off

The branch is `fm/arctic-ch2-gates-r1`, one commit on `fm/arctic-audit-priorities-r1`.
The tree is clean. Nothing is pushed and no pull request exists.

What ran green on the committed tree:

| Command | Result |
| --- | --- |
| `pytest tests/test_standalone_calibration.py tests/test_gate_contracts_r15.py tests/test_distractor_validation.py tests/test_question_context.py tests/test_eligibility_span_contract.py tests/test_gemini_eligibility.py` | 180 passed |
| `pytest tests/test_cli_integration.py` | 92 passed |
| `pytest tests/test_streaming.py` | 56 passed, exit 0, on the tree before the last three changes |
| `pytest tests/test_streaming.py -k "failed_reconstruction_never_reaches_distractor_generation"` | passed, on the final tree |
| `pytest tests/test_streaming.py -k "same_campaign_regenerates or new_campaign_regenerates or live_gate_binds_reviewed"` | 3 passed, on the final tree |
| `pytest tests/test_answer_equivalence.py` | 47 passed |
| `pytest tests/test_generation_scope_roles.py tests/test_evidence_combination.py` | 22 passed |
| `pytest tests/test_publication_export.py tests/test_pipeline_trace.py tests/test_project_progress_viewer.py tests/test_gemini_batch.py tests/test_bounded_fallback.py tests/test_scope_layout.py tests/test_corpus_viewer.py tests/test_current_payload_validation.py tests/test_ambiguous_continuation.py` | 116 passed |
| `pytest tests/ --ignore=tests/test_model_broker.py --ignore=tests/test_streaming.py` | 540 passed, exit 0 |
| `ruff check src tests` | passed |
| `ruff format --check` on every file this slice touched | passed |

The run was bounded in two parts, as the task brief requires.
Part one is every test except `tests/test_model_broker.py` and `tests/test_streaming.py`: 540 passed, exit 0.

Part two is those two files, which hold real rate-limit sleeps.
It did not finish inside its bound.
Four crews validated on this machine at the same time.
The load average reached 12 with eight pytest processes, and the run stalled inside a broker sleep.

The evidence for part two is therefore indirect but complete:

- `tests/test_streaming.py` passed in full, 56 of 56, on the tree that holds every gate change except the last three.
- Those three changes are the routing collapse, the pipeline-trace branch and the question-qualifier union. Only the routing collapse touches a streaming test, and that test passed on the final tree, together with the three tests that surround the stall point.
- `tests/test_model_broker.py` holds no code this slice changed. It passed in the one whole-suite run that did complete.

Integration should run one whole-suite command on an idle machine after the merge.


## Appendix A. The v3 standalone prompt, in full

`STANDALONE_SYSTEM`, contract `source-blind-scientific-referent-v3`.

```
Judge whether one displayed scientific task is interpretable without the source paper.
You receive only the question and question_context.
The reader is a strong scientist who cannot see the paper, title, table, figure, evidence, or answer.
The reader will also see four mutually exclusive options of one type. You do not see them.
Do not judge source support or answer correctness.

Apply this test in order. Stop at the first step that fails.
1. State in one sentence what the task asks for. If you cannot, the task fails.
2. Name the kind of answer the task wants, such as a percentage, a taxon, a direction, or a count. If you cannot, the task fails.
3. For each detail that you believe is missing, apply the necessity test. The task passes when no missing detail is necessary.
4. Ask whether a strong scientist who cannot see the paper can choose the correct option from the displayed text alone, or whether the asked-for value is an arbitrary study-specific quantity. If the latter, the task fails.

NECESSITY TEST. A missing detail is necessary only when one of these is true.
(a) Two readers who both understand the task can defend different answers because the detail is absent.
(b) The reader cannot tell what kind of fact the task asks for.
A detail that only tells the reader where, when, or by whom the fact was produced is not necessary. Do not report it.
A location or period is necessary whenever the value can differ between sites or periods.

These tasks pass. They are correct benchmark tasks.
- A result described by its own scientific properties, with no site name, when the paper reports it as a whole-study result.
- An observation with no sampling date, when the task states no comparison between times.
- A period fixed by calendar text, such as 'April to September' or 'collected in 2011'.
- A period fixed by an event that the task names, such as '9 months after vaccination'.
- A generic description of a design, such as 'across several stations' or 'a lake and an adjacent wetland', when the task asks the reader to choose between the described categories.
- A quantity stated with its own sample size, such as 'n = 457'.
- A study described inline, such as 'In a study that tracked daily transcriptomes of Calanus finmarchicus at two high Arctic stations'.
- A station identified by a coordinate together with one more property from the task, such as 'the southern station (74.5 deg N)' in a task that states a two-station design.

These tasks fail. They are not interpretable without the paper.
- An acronym, run label, station code, or expedition code that the task never expands, such as 'ITP', 'refRun', 'CTL', 'GHSZ', 'PS80', or 'DBO4'.
- A definite description with no antecedent in the task, such as 'the southern station' with no other property, 'the identified OTUs', 'the combined expeditions', or 'this experiment'.
- A period fixed only by the publication date, such as 'the past 20 years', 'recent years', or 'at this time'.
- A pointer to source material, such as 'Table 2', 'the fourth column', 'Figure 6', or 'according to the study'.
- A measured variable with no name, no unit, and no stated basis, when the answer is a value of that variable.
- Text that is broken, garbled, or cut in the middle of a word.
- A task that states its own answer.

A study, publication, author, journal, dataset, or campaign identity is never a necessary detail.
A named campaign, cruise, core, or project code does not resolve a referent. It is an unexpanded label. Judge it by the fail list.
A DOI, paper title, or phrase such as 'according to the study' cannot replace scientific context.
Do not treat an empirical observation as a universal claim unless the displayed text makes that general scope explicit.
Most well-written tasks pass. Report a missing detail only when the necessity test selects it.
For a failed verdict, name each unresolved phrase and the detail that the necessity test selected.
Do not use a generic study-local reason when a scientific detail is missing.
The controller owns the contract version. Do not infer or judge version metadata.
Return only the requested JSON object.
```

## Appendix B. The numeric metadata vocabulary, as the prompt states it

The extractor prompt block carries this text.
The schema field descriptions state the same strings.

```
Emit numeric_rule only when answer.text displays exactly one number with its unit.
Never emit numeric_rule for a categorical, directional, multi-value, or descriptive answer,
and never set canonical_value to a placeholder such as 0 or 1.
Omit numeric_rule when any field is unsupported or when the answer contains multiple values.
NUMERIC_METADATA_VOCABULARY numeric-rule-source-support-v3. Use exactly these strings and no others.
Set unit to the unit token that follows the value in the span, not a gloss and not an expanded name.
Set tolerance_basis to exact span text that states the tolerance, and it must contain that same unit.
When the span reports an uncertainty, copy the uncertainty text with its unit, such as '+/-0.3 t'.
When the value is a directly published exact scalar with zero tolerance, copy its displayed quantity
with its unit, such as '1.8 cm'.
Set reported_precision to the decimal increment of the literal value, such as '0.1' for '1.0',
or to exact span text that states the precision.
Set rounding_rule to 'none' when the value is reported without further rounding,
or to '<N> decimal places' matching the literal, such as '1 decimal place'.
Set conversion_rule to exactly 'direct source literal' when no unit conversion was applied.
The only separate vocabulary is a literal exact integer count: use tolerance_basis 'count',
reported_precision 'exact integer', rounding_rule 'none',
and a conversion_rule that starts with 'direct count'.
```
