# Current Arctic yield audit

## Scope and result

This audit freezes one current production cohort at `2026-09-14T08:33:02+00:00`.

The invocation is `first-production-81b1976-live-rerun-r2`.

The audited contract is generation v16, scope v4, and candidate schema 2.2.0.

The cohort contains 42 complete eligibility decisions.

Five machine-accepted records are outside the rejection review denominator.

The review denominator is 37 current decisions.

The frozen decision inventory is:

| Current state | Count |
|---|---:|
| Eligibility unresolved | 6 |
| Scientific exclusion | 3 |
| Generation rejected | 27 |
| Incomplete non-MCQ | 1 |
| Machine accepted, not in review denominator | 5 |

The independent auditors classified the 37 reviewed decisions as follows:

| Audit classification | Count | Share of reviewed decisions |
|---|---:|---:|
| Avoidable false rejection | 20 | 54.1% |
| Justified rejection | 9 | 24.3% |
| Ambiguous, needs more evidence | 4 | 10.8% |
| Operationally unresolved | 4 | 10.8% |

An avoidable false rejection does not prove that a complete MCQ is valid.

Each accepted MCQ still needs an answer match, grounded evidence, complete scope, and three independently verified distractors.

## Cohort custody

The frozen records are in `cohort.jsonl`.

`cohort.sha256` binds the JSONL bytes.

`cohort-summary.json` records the exact reviewed and accepted paper lists.

Each decision binds its eligibility job, source version, payload hashes, current candidate, historical candidates, rejection events, and model receipts.

The collector reads the canonical SQLite database in read-only mode.

It also reads the selected access manifest, eligibility jobs, and streaming model receipts.

The collector does not use the shared progress counters as the decision inventory.

The process error at 07:58 UTC was an operational stop.

It was not a scientific rejection.

Recovery completed that paper before the cohort cutoff.

Production continued after the cutoff, so later progress counts do not change this frozen audit denominator.

Production is now paused by captain instruction.

The pause receipt binds the unchanged ledger and its one outstanding submitted reservation.

## Per-paper decisions

### Auditor A

| Paper | Classification | Current evidence and decision |
|---|---|---|
| `10.1038/s43247-026-03735-1` | Justified rejection | Spans `s000001`, `s000021`, `s000022`, `s000084`, `s000454`, and `s000479` identify a Perspective that synthesizes prior work. No primary finding was established. |
| `10.1007/s00300-017-2082-7` | Avoidable false rejection | Spans `s000354` to `s000356` state that Chl-a was higher than the control after 13 incubation days. Interleaved PDF text split the required scope. |
| `10.1016/j.oceaneng.2026.125763` | Avoidable false rejection | The source makes coast distance and depth dominant factors. Course, group, and status are secondary. Reconstruction selected a displayed component under valid parent span `436e8942...`. |
| `10.5194/os-20-341-2024` | Avoidable false rejection | The source says eastward wind drives alongshore transport and a coastally confined river plume. Reconstruction selected component `1fada4b...` under parent `f5f4b137...`. |
| `10.5194/tc-20-4313-2026` | Operationally unresolved | The Arctic eligibility criteria pass. Layout fragments prevent binding “Central Arctic” and locations north of Greenland and the Canadian Arctic Archipelago. |
| `10.1038/s41467-023-39466-6` | Avoidable false rejection | The source reports an increase of 0.37 SD per decade. Answer, reconstruction, and entailment agree. Optional numeric metadata caused rejection. |
| `10.1038/s41598-025-21785-x` | Avoidable false rejection | The source reports year-round fin-whale calls southwest of Svalbard. The model selected a displayed component under parent `f61014ad...`. |
| `10.1038/srep39084` | Ambiguous, needs more evidence | The source reports 0.7 degrees Celsius north of 85 degrees north. The question uses the unresolved phrase “at this time.” |
| `10.3389/fmars.2026.1788001` | Avoidable false rejection | The source reports `m/zwa` 399.7263 in the Arctic Ocean. Answer and reconstruction agree. The numeric validator rejects the uncommon unit and bare tolerance basis. |
| `10.1038/s43247-025-02782-4` | Ambiguous, needs more evidence | The source reports a 0.42 to 2.53 nitrogen-fixation range. The context expands CAO without support in the selected source chunk. |
| `10.5194/cp-19-2157-2023` | Operationally unresolved | The answer “greater than 95%” is supported. Component span resolution stopped independent distractor verification. This is not an accepted MCQ. |
| `10.1038/s41467-019-13299-8` | Operationally unresolved | The 83% answer is supported. Three distractors are other source values, and 17% is derived. Component resolution stopped independent verification. |
| `10.1007/s00484-023-02531-2` | Operationally unresolved | Eligibility is scientifically satisfied. Table and PDF fragments prevent contiguous binding of the named Arctic sites. |
| `10.3389/fhumd.2026.1629607` | Justified rejection | Interviews took place in Iceland's Northeastern Region, below the configured Arctic cutoff. No qualifying Arctic component was shown. |
| `10.30758/0555-2648-2025-71-4-445-468` | Avoidable false rejection | The source reports 30.65 micromoles per square metre per day for sandstone sample 3A-03. Reconstruction selected a component under parent `12cb4...`. |
| `10.1038/srep34456` | Ambiguous, needs more evidence | The source says maximum BP occurred in summer. The selected chunk does not support the context expansion to bacterial production. |
| `10.14214/df.383` | Avoidable false rejection | The source identifies PAR and net ecosystem exchange as dominant low-N2O-flux drivers. Adjacent components split the method and result scope. |
| `10.1371/journal.pone.0137209` | Justified rejection | The Fairbanks site is at 64 degrees 51 minutes north. The configured Arctic boundary excludes it. |
| `10.1128/aem.00117-09` | Avoidable false rejection | The source reports an eightfold Chl-a decrease from summer to winter. Line-break hyphenation split key words and the evidence interval. |

### Auditor B

| Paper | Classification | Current evidence and decision |
|---|---|---|
| `10.1038/s41467-026-75094-6` | Avoidable false rejection | Eligibility evidence names Novaya Zemlya and the 40 to 70 degree north domains. Only scope-phrase binding failed. |
| `10.1038/s41467-020-15924-3` | Avoidable false rejection | Spans `s000206` and `s000207` report 9.7% annual Arctic warming from CO2 radiative forcing. Reconstruction selected a displayed component instead of its combined selectable span. |
| `10.5194/acp-24-7359-2024` | Avoidable false rejection | Spans `s000375` to `s000377` report 8.6% occurrence over Arctic marine regions. Finding binding failed. |
| `10.1007/s00216-007-1324-x` | Avoidable false rejection | Spans `s000309` and `s000310` report 5.5 to 6.9 nM acetone. The answer and reconstruction text match exactly. Optional numeric metadata caused rejection. |
| `10.1038/ismej.2015.239` | Justified rejection | Evidence places the study at 64 to 77 degrees south. The Arctic geography criterion correctly failed. |
| `10.1038/s41612-026-01384-x` | Avoidable false rejection | Spans `s000397` and `s000398` report a sustained significant SIC decline under SPAO. Answer and reconstruction agree. Optional numeric fields contain string `null` values. |
| `10.54254/2753-8818/2026.34179` | Justified rejection | The source explicitly describes a review synthesis. It does not establish primary Arctic study activity. |
| `10.1017/jog.2023.18` | Avoidable false rejection | Spans `s000023` to `s000026` report a 5.8% glacier-area reduction. Finding span binding failed. |
| `10.1038/sdata.2017.39` | Avoidable false rejection | Spans `s000222` and `s000223` identify SAUP records for FAO area 18 in the Arctic. Finding binding failed. |
| `10.1007/s00382-025-07623-w` | Ambiguous, needs more evidence | The selected span ends at “The observed positive” because of two-column extraction. A wider chunk suggests positive SST values, but the cited span is incomplete. |
| `10.14430/arctic79682` | Justified rejection | The question quotes “Three cellars,” which is the answer. It also supplies an unsupported MAIAT expansion. |
| `10.1038/s41597-020-00751-4` | Justified rejection | The source reports separation between north and south stations. The question leaves “samples” undefined. |
| `10.5194/bg-22-4545-2025` | Avoidable false rejection | Eligibility evidence identifies Baffin Bay and ArcticNet sampling. An unknown geography span ID caused rejection. |
| `10.1101/353060` | Justified rejection | The source supports five groups. The question gives that answer and uses paper-dependent wording. |
| `10.3389/fmars.2025.1656212` | Avoidable false rejection | Spans `s000205` to `s000207` report higher fatty-acid carbon-isotope values at ice-covered stations. Reconstruction span lookup failed. |
| `10.1038/s41558-025-02471-2` | Avoidable false rejection | Spans `s000202` and `s000203` report 15, 75, and 155 metre depths. Verified closed-set tuple distractors were rejected as compound assertions. |
| `10.1016/j.envpol.2022.120322` | Avoidable false rejection | Spans `s000532` and `s000534` report 7.84 plus or minus 3.64 micrograms per gram dry weight. Answer and reconstruction agree. Optional numeric metadata caused rejection. |
| `10.1186/s40645-023-00591-x` | Justified rejection | The answer is source-supported. The context adds WACE acronym expansions that are absent from supplied source data. |

## Actual attempt semantics

The viewer's former `attempt_count` is the count of distinct model-stage receipts.

It is not the number of distinct scientific findings.

Provider transport retries have their own attempt identity.

The production stream sets automatic generation transport retries to zero.

The audited v16 selection policy freezes one finding for each paper family.

A later run with the same policy reuses that finding.

The audited v16 stream has no automatic alternative-finding path.

It also has no automatic question-repair path.

The existing one-correction helper changes the same candidate.

It does not repeat blinded reconstruction and answer verification.

Therefore, it is not safe as a production question-repair path.

The shipped v17 contract adds a bounded progression.

It uses at most two distinct findings, one total revision, and three candidate paths for each paper family.

Each path repeats reconstruction and option verification under a deterministic attempt identity.

The stream rebuilds progression from immutable candidate, finding, rejection, validation, and model-receipt records.

It treats a persisted budget stop as terminal.

It also rejects a candidate when its stored attempt provenance differs from the requested path.

Historical v12 and v14 candidates remain in the record.

They are not current v16 decisions.

The viewer count fix separates model calls, QA candidates, and ID-backed finding attempts.

## Per-case fix coverage

This table maps every Auditor A decision to a shipped control or an explicit remaining disposition.

The mapping does not change the frozen v16 classification.

| Paper | Shipped control or remaining disposition | Regression seam |
|---|---|---|
| `10.1038/s43247-026-03735-1` | Keep the primary-finding exclusion. | Existing eligibility and streaming tests. |
| `10.1007/s00300-017-2082-7` | The selectable-span projection and bounded alternative path can avoid the split finding. The frozen candidate is not promoted. | `test_evidence_combination.py`, `test_bounded_fallback.py` |
| `10.1016/j.oceaneng.2026.125763` | The model now sees only the selectable parent span. | `test_evidence_combination.py` |
| `10.5194/os-20-341-2024` | The model now sees only the selectable parent span. | `test_evidence_combination.py` |
| `10.5194/tc-20-4313-2026` | Keep this case operationally unresolved. It has no candidate or complete selected-scope phrase. | No acceptance regression. |
| `10.1038/s41467-023-39466-6` | Exact answer agreement now takes precedence over inapplicable optional numeric metadata. | `test_answer_equivalence.py` |
| `10.1038/s41598-025-21785-x` | The model now sees only the selectable parent span. | `test_evidence_combination.py` |
| `10.1038/srep39084` | The prompt rejects paper-dependent time wording. One bounded revision can repair supported wording. The frozen case stays ambiguous. | `test_question_context.py`, `test_bounded_fallback.py` |
| `10.3389/fmars.2026.1788001` | Exact answer agreement can bypass inapplicable optional scalar metadata. Unit and source checks remain. | `test_answer_equivalence.py` |
| `10.1038/s43247-025-02782-4` | The prompt forbids unsupported acronym expansion. The frozen context stays ambiguous. | `test_question_context.py`, `test_streaming.py` |
| `10.5194/cp-19-2157-2023` | Selectable spans remove the component-ID stop. The case still needs three verified distractors. | `test_evidence_combination.py` |
| `10.1038/s41467-019-13299-8` | Selectable spans let independent verification continue. The derived 17% option still needs all gates. | `test_evidence_combination.py`, `test_distractor_validation.py` |
| `10.1007/s00484-023-02531-2` | Keep this case operationally unresolved. Empty or fragmented selected scope cannot be invented. | No acceptance regression. |
| `10.3389/fhumd.2026.1629607` | Keep the exact geography exclusion. | Existing geography tests. |
| `10.30758/0555-2648-2025-71-4-445-468` | The model now sees only the selectable parent span. | `test_evidence_combination.py` |
| `10.1038/srep34456` | Keep this case ambiguous. The prompt cannot expand BP to bacterial production without supplied source support. | Negative cases in `test_question_context.py` |
| `10.14214/df.383` | The selectable-span projection preserves the adjacent source interval. | `test_evidence_combination.py` |
| `10.1371/journal.pone.0137209` | Keep the exact geography exclusion. | Existing geography tests. |
| `10.1128/aem.00117-09` | Narrow normalization can compare complete split words. The frozen record stays rejected because its reconstruction is truncated and its columns interleave. | `test_scope_layout.py` negative and positive cases. |

This table maps every Auditor B decision.

| Paper | Shipped control or remaining disposition | Regression seam |
|---|---|---|
| `10.1038/s41467-026-75094-6` | The narrow eligibility separator fix accepts exact span IDs separated by whitespace. It does not repair unknown IDs. | `test_geography_correction.py` |
| `10.1038/s41467-020-15924-3` | The model now sees only the selectable parent span. | `test_evidence_combination.py` |
| `10.5194/acp-24-7359-2024` | The model now sees only selectable spans. | `test_evidence_combination.py` |
| `10.1007/s00216-007-1324-x` | Exact answer agreement now takes precedence over inapplicable optional numeric metadata. | `test_answer_equivalence.py` |
| `10.1038/ismej.2015.239` | Keep the Antarctic geography exclusion. | Existing geography tests. |
| `10.1038/s41612-026-01384-x` | The prompt omits inapplicable numeric metadata. Exact directional agreement remains available. | `test_answer_equivalence.py`, `test_streaming.py` |
| `10.54254/2753-8818/2026.34179` | Keep the review-synthesis exclusion. | Existing eligibility tests. |
| `10.1017/jog.2023.18` | The model now sees only selectable spans. | `test_evidence_combination.py` |
| `10.1038/sdata.2017.39` | The model now sees only selectable spans. | `test_evidence_combination.py` |
| `10.1007/s00382-025-07623-w` | Keep this case ambiguous. The cited interval is incomplete. | No acceptance regression. |
| `10.14430/arctic79682` | Keep the frozen answer-leaking rejection. The v17 prompt and bounded revision prevent or repair supported wording. | `test_question_context.py`, `test_bounded_fallback.py` |
| `10.1038/s41597-020-00751-4` | Keep the frozen undefined-samples rejection. A revision can add only supported context. | `test_question_context.py`, `test_bounded_fallback.py` |
| `10.5194/bg-22-4545-2025` | Keep the unknown geography ID fail-closed. The system must regenerate exact IDs before another decision. | Negative geography tests. |
| `10.1101/353060` | Keep the frozen answer-leaking rejection. The v17 prompt forbids paper-dependent wording. | `test_question_context.py` |
| `10.3389/fmars.2025.1656212` | The model now sees only selectable spans. | `test_evidence_combination.py` |
| `10.1038/s41558-025-02471-2` | Typed numeric closed-set tuples can contain “and” when cardinality and units match. All verifier gates remain. | `test_distractor_validation.py` |
| `10.1016/j.envpol.2022.120322` | Exact answer agreement now takes precedence over inapplicable optional numeric metadata. | `test_answer_equivalence.py` |
| `10.1186/s40645-023-00591-x` | Keep the unsupported WACE expansion rejection. The v17 prompt cannot invent an expansion. | Negative cases in `test_question_context.py` |

## Structural causes and bounded fixes

### Model-facing evidence identifiers

Combined evidence spans exposed both selectable parent IDs and nonselectable component IDs.

Models frequently chose visible component IDs.

The resolver then rejected those IDs as missing.

The integrated fix projects only selectable parent spans into model-facing `SOURCE_DATA`.

Stored combined spans retain their component provenance.

This fix covers finding, reconstruction, answer-verification, and distractor model inputs.

### Exact answers and optional numeric metadata

Exact answer text can agree while optional numeric metadata is malformed or inapplicable.

Examples include a numeric range, an uncommon unit, and string `null` values for a directional answer.

The acceptance gate must test exact normalized answer text before optional numeric equivalence.

It must retain unit, sign, negation, and scope safety.

The reconstructor prompt must tell the model to omit numeric metadata when one scalar value and unit do not apply.

### Stand-alone questions and separate context

Benchmark questions must identify each necessary system, location, population, time, method, comparison, and condition.

They must not use “this study,” “according to the study,” “at this time,” unresolved samples, figures, tables, or unidentified OTUs.

Uncommon acronym expansions belong in `question_context` only when supplied source data supports the expansion.

The question and context must not reveal the answer.

Question repair must preserve study-specific scope.

It must not convert a study result into a universal fact.

### Closed-set tuple distractors

A typed closed-set option can contain a tuple such as three depths.

The presence of “and” does not make that tuple an unsafe compound claim.

The narrow fix permits a tuple only for the typed closed-set case.

All normal contradiction, evidence, ambiguity, leakage, and verifier gates still apply.

### Narrow PDF comparison normalization

Scope checks now use a comparison-only text projection.

The projection collapses whitespace and rejoins alphabetic words split by a line-break hyphen.

It does not change source text, quotes, offsets, hashes, span IDs, locators, or provenance.

It does not join evidence spans, complete truncated words, repair interleaved columns, or change numbers, signs, units, negation, or coordinates.

Therefore, the four inspected layout cases keep their frozen outcomes.

### Bounded finding and question progression

The production default remains one published QA for each paper family.

One paper can try at most two distinct findings.

One paper can also use at most one question or context revision in total.

The total maximum is three candidate paths per paper.

The first alternative-finding allowlist contains `reconstruction_disagreement` and `insufficient_verified_distractors`.

Contract mismatches, budget stops, provider ambiguity, and other operational failures do not start another finding.

Each path needs a new deterministic identity and immutable lineage.

Each revised candidate must repeat blinded reconstruction, answer verification, and option verification.

An alternative-finding prompt can exclude the failed finding location.

It must not give the failed answer to the new reconstructor.

The system must record reason codes for each progression, cap, or budget stop.

The one-dollar family limit and global campaign ceilings remain independent hard limits.

No path can retry until an answer matches.

No path can reuse a paid request ID.

No path can delete failed history.

If a later alternative fails, an earlier valid short-answer candidate remains in the incomplete export.

It does not enter the accepted MCQ count.

## Valid remaining rejection mechanisms

Keep scientific geography exclusions when exact source evidence falls outside the configured Arctic scope.

Keep review or editorial exclusions when no primary study finding is established.

Keep question rejection for answer leakage, unresolved referents, unsupported context, or missing study scope.

Keep evidence rejection when the cited interval does not contain the claim and its required scope.

Keep MCQ incompleteness when fewer than three distractors pass independent verification.

Keep ambiguity when the cited source interval is incomplete or supports more than one answer.

Never treat a model's confidence or opinion as measured 95% validity.

## Integrated regression evidence

The combined focused suite passed after all commits were integrated.

It covered streaming, CLI integration, bounded fallback, tuple distractors, and scope-layout comparison.

It also covered the compatibility seams for attempt-bound option receipts and targeted distractor resumption.

Ruff passed for every changed Python file.

`git diff --check` passed.

These tests measure code behavior.

They do not measure scientific validity or production yield.

## Rollout boundary

Do not rewrite raw records into accepted records.

The current live selection mode is `machine_validated_preview`.

This mode requires at least three model-verified distractors before an item enters the machine preview.

A preview item is not a strict benchmark acceptance.

It is also not measured 95% validity.

The strict final export remains a separate gate with its own manifest, evidence, context, rationale, and provenance checks.

The reviewer JSONL, benchmark JSONL, and CSV must preserve that distinction.

Do not resume the paused producer from the old recovery authority.

After combined tests pass, release with a new prompt and schema version.

Use one version for every result in a reported denominator.

First run a bounded dry validation against the frozen regression cases.

Then run a small no-batch production tranche under the existing campaign ceilings.

Report accepted MCQs, incomplete MCQs, scientific exclusions, operational unresolved cases, and each rejection family separately.

Expected effects are engineering heuristics until a new versioned run measures them.

The evidence-ID fix should reduce false span-not-found exits.

The exact-answer fix should reduce false reconstruction disagreement.

The tuple fix can improve verified distractor yield for closed sets.

The bounded fallback can improve per-paper yield without producing duplicate published rows.
