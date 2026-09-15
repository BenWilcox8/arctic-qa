# arctic-ch2-core-routing-r1: phase 0 and phase 3

Branch: `fm/arctic-ch2-core-routing-r1`, created from `fm/arctic-audit-priorities-r1` at commit `a53b106`.
Scope: the r15 holistic audit, section 5 phase 0 (all rows) and phase 3 (routing).
No paid provider call was made.
Nothing under `/mnt/crdata` was touched.
No legacy corpus, candidate, receipt, export, or runtime snapshot was changed.

## Files touched outside my owned set

Integration must reconcile these files with the sibling crews.

| File | Why my slice needed it | Sibling overlap |
| --- | --- | --- |
| `src/arctic_qa/generation.py` | Reconstructor and answer-verifier prompt text, the freeze-time admission gate, ATTEMPT_HISTORY, the surgical and answer-rule repair rungs, the distractor pre-filter and feedback. | `arctic-ch2-writer-context-r1` owns the writer prompt, the extractor prompt, and `CANDIDATE_SCHEMA_VERSION`. My edits do not touch those. |
| `src/arctic_qa/validation.py` | The rejection ledger detail, the refusal of schemas without a standalone contract, the empty reconstructor scope, and a public wrapper for the option display rules. | `arctic-ch2-gates-r1` owns the standalone contract, the numeric contract, and the `relation_scope_match` split. My edits do not touch those. |
| `src/arctic_qa/cli.py` | Two new `stream` arguments: `--roles-file` and `--role-profile`. | None expected. |
| `src/arctic_qa/gemini_eligibility.py` | The doubled-newline join and the comparison-side fold. | `arctic-ch2-corpus-r1` owns eligibility prompt v7 and the bounded re-screen. My edit is in `_validate_response_span_contract` only. |
| `fixtures/fake-verifier.jsonl` | Prompt-marker guards for the reconstructor and the answer verifier. | The gates crew also edits verifier prompt text, so this fixture can conflict. |
| `tests/test_streaming.py` | Two `provider_policy` assertions now ignore the new `model_roles` block. | Shared test file. |
| `tests/test_generation_scope_roles.py` | Two routing expectations that encoded the v3 multi-layer abort. | Shared test file. |
| `tests/test_cli_integration.py` | Two tests asserted that a schema without a standalone contract reaches `machine_accepted_unverified`. They now assert the refusal. | Shared test file. |
| `docs/METHODS.md`, `docs/STREAMING_DATASET.md` | The role contract and the `--role-profile` option. | Siblings may also edit these files. |
| `AGENTS.md` | One line naming the test command, because a bare `pytest` cannot import the package from the devshell. | Siblings may also edit this file. |
| `src/arctic_qa/corpus_viewer.py`, `tests/test_project_progress_viewer.py` | A pre-existing test failure at the base commit, unrelated to my slice. See "Pre-existing failure fixed". | No chapter 2 crew owns the viewer. |

Files I own and changed: `src/arctic_qa/streaming.py`, `config/roles.v1.json`, `tests/test_bounded_fallback.py`.
Files I added: `src/arctic_qa/model_roles.py`, `tests/test_model_roles.py`, `tests/test_audit_core_fixes.py`.

## Contract versions

| Constant | Value | Owner |
| --- | --- | --- |
| `validation.ROUTING_CONTRACT_VERSION` | `bounded-failure-routing-v4` (was `bounded-failure-routing-v3`) | Mine. `generation.GENERATION_ATTEMPT_CONTRACT_VERSION` and `streaming.GENERATION_ATTEMPT_CONTRACT_VERSION` now both read this one constant. |
| `model_roles.MODEL_ROLES_CONTRACT_VERSION` | `generation-model-roles-v1` (new) | Mine. |
| `validation.REJECTION_DIAGNOSTIC_CONTRACT_VERSION` | `rejection-diagnostic-detail-v1` (new) | Mine. |
| `config/roles.v1.json` `schema_version` | `1.1.0` (was `1.0.0`) | Mine. |
| `validation.ANSWER_AGREEMENT_PROMPT_VERSION` | `answer-agreement-judge-v1`, unchanged | Mine to bump, but `ANSWER_AGREEMENT_SYSTEM` did not change. The audit says to keep that system prompt as written, so a bump would misreport it. |

Two contract notes for integration.

1. The routing version moved into `validation.py` because `CANDIDATE_CONTRACTS` cannot import `generation.py`.
   `CANDIDATE_CONTRACTS["2.6.0"]["generation_attempt_contract_version"]` now reads the constant instead of the literal `"bounded-failure-routing-v3"`.
   When `arctic-ch2-writer-context-r1` adds schema `2.7.0`, move that constant reference to the new entry and pin `2.6.0` back to the literal `"bounded-failure-routing-v3"`.
2. The reconstructor and answer-verifier prompt text changed under this slice, and those prompts have no version of their own.
   They sit under `GENERATION_PROMPT_VERSION`, which `arctic-ch2-writer-context-r1` owns.
   That crew's bump to v22 makes my prompt edits contract-visible. I did not bump it.

## Defects addressed, by audit finding

### Phase 0

**R5, section 4.2 fix 4, section 4.8 item 1. No code read `roles.v1.json`.**
`src/arctic_qa/model_roles.py` reads and validates the contract.
`run_stream` loads it at stream start, before the first paper.
The contract is refused when any judge (`standalone_verifier`, `option_verifier`, `reconstructor`, `answer_verifier`, `answer_judge`) carries the writer model or the writer provider, and when `standalone_verifier` or `option_verifier` does not carry the strongest ranked judge model.
`config/roles.v1.json` gains `contract_version`, a `model_strength_rank` map, and the missing `standalone_verifier` and `answer_judge` roles.
`run_stream` gains `role_profile`. When a profile is named, every judge role's requested model must equal the configured one, and the writer must not share a model with any judge. The `away_production` phase must name a profile.
The run manifest and the run result both record the resolved roles, the effective per-role models, and whether separation was enforced.
The `same_model_roles` disclosure stays, now paired with an assertion: an enforced run that ends with one model in every role raises.

*Boundary I chose, and why.* The live broker serves every role from one Gemini model today, so a hard separation check on every phase would fail every live-test run in the suite. The gate therefore binds on the `away_production` phase, which is the chapter 2 run. `live_test` keeps the disclosure. The run crew must add per-role stage models and their pricing to the broker price config before the production run starts; that file is theirs.

**Section 4.2 fix 6 and section 4.6 fix 6. The rejection ledger detail was `{}` on every row.**
`validation.rejection_diagnostic_detail` builds a typed record from `unresolved_phrases`, `missing_detail_types`, `review_rationale`, `verification_rationale`, `residual_error`, and `question_context_missing_detail`.
`_finish` writes it into the validation event detail and into every `automated_acceptance` rejection ledger row.

**RECON-2, RECON-6, D7, section 4.4 fix 5 and section 5 phase 0.**
Three prompt changes, given in full below.
The reconstructor no longer must return a non-null scope, and `validate_candidate` now accepts an all-null reconstructor scope through `scope_is_evidence_bound(..., allow_empty=True)`.
The writer answer record and the typed numeric path keep their non-null requirement.
The answer verifier no longer receives `RECONSTRUCTION`, so `question_claim_type_disagreement` compares two independent labels.
The extractor and the reconstructor both now carry "never emit numeric_rule for a non-scalar answer".
Kept: deterministic-first agreement, the alternatives instruction, blind reconstruction, `confidence_category`.

**FS-1, FS-3, section 4.5 fixes 1 and 2. The finding froze before it was checked.**
`generation.finding_admission_reason` runs before the `INSERT INTO findings`.
It rejects a finding whose `evidence_quote`, at the recorded offsets, contains neither `answer.text` nor `deterministic_rule.source_value` (`finding_evidence_quote_excludes_finding`).
It rejects a finding whose `required_question_phrases` carry the answer (`finding_answer_phrase_in_required_question_phrases`), which used to be computed at freeze time and applied 440 lines later at the QA gate.
On a rejection the selected span ids join the excluded set and the extractor is asked once more. `FINDING_ADMISSION_PASSES` is 2. The re-ask costs one extractor call and spends no family retry path, which is what the audit asks for.
A finding frozen under an earlier contract still reports the same checks at the QA gate.

**E3, eligibility. The doubled-newline join.**
`gemini_eligibility.py` joins the selected finding spans with `""`, because each span already ends with its own newline.
`normalize_for_phrase_binding` folds NFKC, the soft hyphen, a hyphen at a line break, and runs of whitespace, on both sides of the comparison.
Deferred, and named below: the bounded re-ask instead of a terminal `screening_error`, the prompt rewrite, and normalizing the stored phrases. Those sit in the corpus crew's phase 4 row.

**Section 4.8 item 5. Stored pre-v21 payloads.**
`validate_candidate` refuses any schema whose `CANDIDATE_CONTRACTS` entry declares no `standalone_verification_contract_version`, with `unsafe_legacy_candidate_schema`.
Schemas 2.0.0 to 2.4.0 are refused. The standalone branch is no longer conditional.

### Phase 3, routing

**R1. The multi-layer guard ended the family.**
`_LAYER_PRIORITY` is `("leakage", "finding", "evidence", "context", "options", "contract")`, leakage first as the brief states.
`_primary_failure_layer` picks one layer, `_next_generation_attempt` keeps only that layer's codes, and the `return None` on more than one layer is gone.
`answer_rule_repair` is a new rung for `source_bound_numeric_rule_missing`. It keeps the frozen question and the frozen finding, runs once, regenerates only the numeric metadata through the `correction` role, and rejects the attempt when the regenerated rule does not bind verbatim to the same frozen span through the unchanged `numeric_rule_is_source_bound`.
`OPTION_REPAIR_REASONS` was not widened, as the audit directs.

**R2. Repairs repeated a demand the source could not meet.**
`_reason_family` collapses a code to the demand a repair answers (`referent_slot`, `standalone`, `question_context`, `scope`, or the literal code).
`_repeat_depth` counts earlier attempts on the same finding that answered the same family.
Depth 0 gives `question_revision`, or `surgical_correction` for a single code in `SURGICAL_CORRECTION_REASONS`. Depth 1 gives `context_widened_revision`. Depth 2 or more leaves the finding.
When a rung declines, the family takes the alternative finding instead of spending a rewrite the escalation already judged unwinnable. That also ends the `answer_rule_repair` rung after its one attempt.
`context_widened_revision` adds the paper's hash-verified `activity_spans` to `SOURCE_DATA` inside a `CONTEXT_ONLY_SOURCE` block.
`_slot_evidence_types` reads the same activity spans for a place, a calendar period, a sample-size phrase, and an acronym pattern. When the demanded slot kind is absent, routing spends no rewrite and moves to the next finding with `slot_evidence_unavailable`.
The place test is deliberately wide: a coordinate or any proper noun that is not a sentence's first word counts. A wrong "the source states a place" only spends a rewrite the pipeline would have spent anyway, while a wrong "no place" would discard a finding the source can still support.
The writer schema gains an optional `context_gap` string. A repair that sets it raises `slot_evidence_unavailable` instead of inventing filler.

**R3. The repair loop remembered one generation back.**
`_attempt_history` builds `ATTEMPT_HISTORY` from every earlier candidate on the same frozen finding: attempt kind, question, question_context, typed reason codes, unresolved phrases. No judge free text is passed.
The revision instruction now orders the writer to keep every definition, place name, period, or sample description that an earlier attempt added.

**R4. `apply_one_correction` was dead code.**
It is replaced by `correct_one_component`, which returns the replacement value only and writes nothing.
Its prompt carries `SOURCE_DATA`, the reason codes, the unresolved phrases, and the residual error, which the old prompt had none of.
The surgical rung for a question or a question_context defect runs through the ordinary `question_writer` path with `SURGICAL_CORRECTION_INSTRUCTIONS`, so the corrected candidate re-runs `validate_candidate` as an ordinary attempt and consumes a path.
`correct_one_component` is live through `answer_rule_repair`. No dead code remains.

**DW-3, section 4.6 fix 5. The distractor repair sent a nonce.**
`option_display_issue` runs the model-free display rules on each proposed option before the paid option verifier call. A failing option is dropped and recorded in provenance under `option_display_prefilter`.
`_rejected_option_feedback` gives an `option_repair` the earlier option texts with the deterministic code that rejected each one, from that provenance and from the `option_validation` ledger rows.

**RECON-R4. Downstream symptoms consumed the routing budget.**
When any `standalone_*` code other than `standalone_answer_leakage` is present, `_routing_reason_codes` drops `relation_scope_mismatch`, `answer_verifier_scope_not_source_bound`, `reconstruction_scope_not_source_bound`, `answer_ambiguous`, and `question_claim_type_disagreement`.
`source_entailment_not_verified` is never collapsed.

**Kept, as the brief requires:** dependency collapse, immediate alternative finding, `revision_unchanged_payload`, the six-path bound (`MAX_CANDIDATE_PATHS == 6`), and `option_repair` reusing the parent question.

## New and changed prompt text

**Reconstructor, replacing "At least one scope value must be non-null."**

> Leave every scope value null when the QUESTION states no qualifier that the selected span supports.

**Answer verifier.** The `RECONSTRUCTION` block is removed from the prompt input. The reading order sentence becomes:

> Read QUESTION and QUESTION_CONTEXT alone before you use SOURCE_DATA or ANSWER_RECORD.

and one sentence is added:

> Label the question claim type from QUESTION and SOURCE_DATA alone.

**Reconstructor numeric instruction.**

> Populate numeric only for one scalar value with one applicable unit. Never emit numeric_rule for a non-scalar answer: omit numeric for ranges, tuples, counts written as words, nonnumeric answers, categorical answers, multi-value answers, descriptive answers, or directional answers.

**Extractor numeric instruction, added.**

> Emit numeric_rule only when answer.text displays exactly one number with its unit. Never emit numeric_rule for a non-scalar answer: never for a categorical, directional, multi-value, range, or descriptive answer, and never with a placeholder canonical_value.

**Revision instruction, replacing the four-sentence v21 text.**

> Revise only the question and question_context. ATTEMPT_HISTORY lists every earlier attempt on this finding with its question, its question_context, and the exact reason for its rejection. Keep every element of the parent that failure_feedback did not name as a defect. Keep every definition, place name, period, or sample description that an earlier attempt added. Change only what failure_feedback and unresolved_phrases name. Define each phrase in unresolved_phrases in question_context, or remove that phrase from the question. Use the source only to add supported subject, place, time, sample, or event context. If SOURCE_DATA does not state the detail that failure_feedback demands, set context_gap to that exact detail and leave the question unchanged. Do not add answer-bearing information. Do not repeat the parent question and question_context unchanged. Do not change the frozen finding.

**Context-widened revision, appended to the revision instruction.**

> This attempt repeats an earlier rejection on this finding. SOURCE_DATA now carries every hash-bound study-setting span this paper supplies for the frozen finding. Take the missing subject, place, period, sample, or acronym expansion from that text, in the source's own words. Set context_gap when it still is not there.

**Surgical correction, appended to the revision instruction.**

> Exactly one defect is recorded. Change the smallest span of text that removes it. Keep every other word of the parent exactly as written.

**CONTEXT_ONLY_SOURCE header, new.**

> CONTEXT_ONLY_SOURCE supports question_context statements only. Never select a CONTEXT_ONLY_SOURCE span as answer evidence, as a scope value, or as a required question phrase.

**Distractor repair feedback, new.**

> Each REJECTED_OPTIONS entry names one earlier option and the deterministic rule that rejected it. Do not repeat a rejected option or repeat its defect. Give one concise positive assertion with one interpretation and one displayed quantity.

**`correct_one_component` prompt, replacing the contentless v21 body.**

> Correct only the {component} component. Change the smallest span of text that removes every listed defect. Keep every other word exactly as written. Every value, unit, and tolerance you write must occur verbatim in SOURCE_DATA. Do not add answer-bearing information.

## Rigor safeguard, per change

The standing constraint is that an accepted item must be answerable from question, question_context, and options alone, and must be supported by the paper. No change below relaxes a gate.

| Change | Why it cannot admit a paper-dependent or unsupported item |
| --- | --- |
| Model roles loaded and asserted | The judge input does not change. A separated run puts a stronger judge of a different family on the source-blind gate and on the option gate. A shared-model run cannot reach the production phase. This only removes a correlated error; it never lowers a bar. |
| Rejection ledger detail | Diagnostics only. Nothing reads the detail to decide acceptance. The typed reason-code enum is unchanged. |
| Empty reconstructor scope accepted | An all-null scope is the reconstructor reporting that the question states no qualifier the span supports. A populated scope value is still tested against the selected span, so an invented value still fails. The writer answer scope and the numeric path keep their non-null requirement. |
| `RECONSTRUCTION` removed from the verifier prompt | The verifier now labels the claim type without seeing the reconstructor's label, so the disagreement check compares two independent judgments instead of one anchored pair. The hard reject stays. This is stricter. |
| "never emit numeric_rule for a non-scalar answer" | A new prohibition. It removes rules the deterministic check would reject anyway and can add no item. |
| Freeze-time admission gate | Both checks only reject. The evidence-quote assertion is new and kills the page-header and author-byline locator class. The leak check already existed; it now fires before the finding freezes instead of after six attempts inherited it. The one re-ask costs an extractor call, spends no family retry path, and its result runs the same gate. |
| Eligibility phrase binding | Only presentation differences that PDF extraction introduces are folded: a ligature, a soft hyphen, a hyphen at a line break, and whitespace. The phrase must still be present in the selected finding spans. Nothing about latitude, the actual-study-evidence rule, separability, or the boundary rule changes. An eligibility change cannot admit a paper-dependent question, because every question from an admitted paper still passes every downstream gate. |
| Pre-v21 schemas refused | A pure new rejection. Those schemas never ran the source-blind gate. |
| Priority-layer routing | Routing decides which repair runs, never whether an item is accepted. Every repaired candidate re-runs the whole gate sequence and consumes a path. The six-path family bound does not move. |
| `answer_rule_repair` | The finding, its evidence span, the displayed answer, and the question are all frozen. Only the metadata that self-describes the same literal is regenerated, and the unchanged `numeric_rule_is_source_bound` check must pass against the same frozen span, or the attempt is rejected. |
| Repeat detector and `context_widened_revision` | The widened text is hash-verified `activity_spans` of the same paper, marked non-selectable for answer evidence, and any span that states the answer text is dropped before rendering. Every scope value is still verified against a selected span. |
| Slot-availability guard and `context_gap` | Both remove the incentive to invent filler. They redirect the repair budget and create nothing. |
| `ATTEMPT_HISTORY` | It forbids the writer from deleting a supported context sentence. It cannot add an unsupported one, because the question-verification contract still tests every context sentence for source support. Only the writer's own earlier payloads and typed codes are passed, never judge free text. |
| Surgical rung | A smaller edit reaches the gates with more of the parent intact. The corrected candidate runs `validate_candidate` as an ordinary attempt. No gate is skipped and no threshold moves. |
| Option display pre-filter | The option gate already applies exactly these rules. Running them first only avoids buying a verdict for an option the gate will reject. The floor of three verified distractors is unchanged, so dropping an option can only lower acceptance. |
| Distractor feedback | It names earlier options and their deterministic codes. Every surviving option still needs a verified contradiction. |
| Dependent-code collapse under a standalone root | Routing only. `source_entailment_not_verified` is never collapsed, so the paper-support signal always reaches the router. |

## Tests

Run under the project devshell: `nix develop -c bash -c 'PYTHONPATH=src pytest <file>'`.
`PYTHONPATH=src` is needed because the package is not installed in the devshell.

| Suite | Result |
| --- | --- |
| `tests/test_bounded_fallback.py` | 34 passed (20 before, 14 added) |
| `tests/test_model_roles.py` (new) | 9 passed |
| `tests/test_audit_core_fixes.py` (new) | 29 passed |
| `tests/test_question_context.py` | 40 passed |
| `tests/test_gemini_eligibility.py` | 14 passed |
| `tests/test_eligibility_span_contract.py` | 3 passed |
| `tests/test_current_payload_validation.py`, `tests/test_distractor_validation.py`, `tests/test_answer_equivalence.py`, `tests/test_generation_scope_roles.py`, `tests/test_evidence_combination.py` | 83 passed |
| `tests/test_streaming.py` | 56 passed |

The whole suite ran green in three bounded foreground parts, on supervisor instruction, because this eight-core machine runs four validating crews and the harness kills a long background task under memory pressure.

| Part | Result |
| --- | --- |
| `pytest tests/ --ignore=tests/test_model_broker.py --ignore=tests/test_streaming.py` | 456 passed |
| `pytest tests/test_streaming.py` | 56 passed |
| `pytest tests/test_model_broker.py` | 86 passed |

598 tests, nothing failed, nothing skipped by me.

`ruff check src/ tests/` passes.
`ruff format --check` passes on every file I changed or added.
`cli.py` and `gemini_eligibility.py` were already unformatted at the base commit, so I kept my edits inside them minimal and did not reformat those two files.

### Regression tests added, by audit finding

`tests/test_audit_core_fixes.py`

- FS-3: the evidence quote must contain the finding; a page-header quote is rejected; a deterministic `source_value` also binds.
- FS-1: a required phrase that carries the answer is rejected at freeze time; one free re-ask exists.
- E3: a cross-line phrase binds after the join fix; a phrase the spans do not state still fails; the old doubled-newline join is shown to break the same phrase.
- Section 4.2 fix 6 and 4.6 fix 6: every judge rationale reaches the detail, and the ledger row stores it.
- Section 4.8 item 5: each of schemas 2.0.0 to 2.4.0 is refused; the current schema still declares a standalone contract.
- RECON-2: an empty reconstructor scope passes only where it is allowed; an invented scope value still fails.
- DW-3: the option pre-filter names the rule that rejects an option, for text and for numeric options.
- R2: the writer schema carries `context_gap`.
- R4: the surgical correction returns a replacement, persists nothing, carries the defect and the source in its prompt, and refuses another component.
- Section 4.1: activity spans are hash-verified, a span that states the answer is dropped, and the block marks its spans unselectable.

`tests/test_model_roles.py`

- The shipped contract separates the writer from every judge in both profiles and puts the strongest model on `standalone_verifier` and `option_verifier`.
- A judge on the writer model, a weaker model on a source-blind gate, and a missing role are each refused.
- A bound profile fails when one model holds every role, and passes when the providers serve the configured roles.
- An unbound offline run discloses the shared model. A production run must name a profile.

`tests/test_bounded_fallback.py`

- R1: the `family-d95f466cad4776a758b9` shape (three codes, three gates) now earns a repair.
- R1: the layer priority order, leakage first.
- R1: a numeric contract kill routes to `answer_rule_repair`, once only.
- R2: the `family-7ad42191e4ec5c7eb2fe` shape escalates surgical, then widened, then leaves the finding.
- R2: a demand the source cannot meet moves to another finding; a demand it can meet still spends a rewrite.
- R2: slot detection reads place, period, sample, and acronym, and place detection fails open on a bare proper noun.
- RECON-R4: a standalone root collapses its symptoms and never collapses `source_entailment_not_verified`.
- A leakage root does not collapse an independent scope defect.
- The six-path bound and the option repair set do not move.

## Pre-existing failure fixed

`tests/test_project_progress_viewer.py::test_project_overview_uses_streaming_counts_as_separate_live_metrics` already failed at the base commit `a53b106`. I confirmed this against a clean tree.

Commit `eca57d1` labeled the viewer's `accepted_qa` figure `shared_ledger_cumulative` and read it from the broker record.
Commit `e1fd380` then made the pipeline trace store narrow that count to the latest invocation, relabeled it `current_incremental_invocation`, and did not update this test.
The relabel is only true when the trace store actually supplies the narrowed count. Without a trace store the viewer still shows the campaign total under the incremental label.

`corpus_viewer.py` now sets the label where the override applies, and `_project_live_metrics` reads the label instead of asserting one.
The test asserts the cumulative label and the real count for the no-trace-store case.
`tests/test_corpus_viewer.py` and `tests/test_pipeline_trace.py` still pass.

## Deferred

| Item | Why | Owner |
| --- | --- | --- |
| Per-role stage models and their pricing in the broker price config | Adding a model line needs verified pricing, and the budget is the run crew's. Until it lands, an `away_production` run with `--role-profile` cannot start, which is the intended gate. | `arctic-ch2-integration-run-r1` |
| Cross-provider streaming (a Claude writer with a Gemini judge broker) | `run_stream` still demands one shared broker, and `generate_candidate` refuses mixed budget authorities. Making the `strongest` profile runnable end to end needs both, which is beyond this slice. | integration |
| The bounded eligibility re-ask instead of a terminal `screening_error`, eligibility prompt v7, and normalizing the stored `question_scope_phrases` | Phase 4 rows. | `arctic-ch2-corpus-r1` |
| The two-part evidence bundle for every model role | Phase 1 row. My `activity_context_block` is a small, marked helper used only by `context_widened_revision`. Replace it with that crew's bundle. | `arctic-ch2-writer-context-r1` |
| `revision_regressed_context` (R3's deterministic guard) | The brief names ATTEMPT_HISTORY, not this code. The instruction to keep earlier additions is in place. | open |
| The standalone calibration set and the seven frozen option payloads (phase 0 "Tests" row) | The calibration set gates the standalone contract v3 prompt, which `arctic-ch2-gates-r1` owns, and needs two labelers. | `arctic-ch2-gates-r1` |
| `standalone_adjudication` (routing.md R5 part 2) | The audit refutes it in section 4.2 fix 4: the answer verifier holds the paper and is biased toward "resolved". The brief does not ask for it. | closed |
