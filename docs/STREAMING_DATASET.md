# Streaming dataset scheduler

The streaming scheduler processes one full-text paper at a time.
It runs scientific eligibility for each newly ready paper.
It immediately continues an eligible paper through QA, distractors, validation, and export.
It does not require a separate eligibility batch or manual stage relaunch.
It uses the answer-first method for the current commission.
It retains the direct-joint code without spending on that arm.

Automated acceptance does not establish scientific truth.
The strongest release label is `machine_accepted_unverified`.

## Combined source evidence

Generation prompt v15 uses the `finding-evidence-span-v3` contract.
The scheduler combines eligible source intervals only when they overlap or have a whitespace-only gap of at most 32 characters.
One combined excerpt can contain at most four component spans and 3,200 characters.
The excerpt copies the complete bounded source interval, including all intervening text.
The scheduler does not merge intervals from different chunks, sources, source versions, or eligibility scopes.
Nonadjacent intervals remain separate decision evidence.

Each combined record keeps the merged locator and SHA-256 value.
It also keeps the ordered component span IDs, locators, SHA-256 values, and eligibility locators.
Each model role keeps its original evidence and rationale.
The blinded reconstruction prompt still excludes the frozen answer.

Answer agreement first uses the existing deterministic matcher.
A deterministic match is authoritative and does not make a judge request.
Only a deterministic mismatch calls the Gemini answer judge.
The judge receives the question, required context, proposed answer, and reconstructed answer.
It does not receive full papers or passage lists.
A `yes` result records `lower_confidence_llm_equivalent` for agreement provenance only.
A `no` result rejects the candidate for reconstruction disagreement.
A missing or malformed result is unresolved.

Answer agreement does not replace evidence validation.
Each role's evidence must resolve to the frozen source and support its stated scope.
The verifier must still accept entailment, relation, scope, ambiguity, and alternative-answer checks.
These checks reduce accidental agreement and nearby-scope errors, but they do not measure scientific validity.

## Offline command

Use fake scripts for an offline integration run:

`--access-run-dir` accepts a chapter 1 article-access run directory.
It also accepts the chapter 2 directory that the corpus freeze writes.
Both use the same `article-access-manifest-v1` and `article-access-item-v1` schemas.
Read [the chapter 2 corpus document](CHAPTER2_CORPUS.md) before you use the chapter 2 directory.

```bash
PYTHONPATH=src python -m arctic_qa --json stream \
  --phase offline \
  --run-id offline-stream-r1 \
  --campaign-id streaming-commission-r1 \
  --access-run-dir /path/to/article-access-run \
  --eligibility-run-dir /path/to/eligibility-run \
  --eligibility-policy-file /path/to/frozen-eligibility-policy.json \
  --author-script fixtures/fake-author.jsonl \
  --verifier-script fixtures/fake-verifier.jsonl \
  --max-papers 1
```

The default progress file is:

`DATA_ROOT/arctic-qa/streaming-dataset-r1/progress.json`

It uses the `streaming-dataset-progress-v1` schema.
The file contains separate scientific eligibility and QA counts.
`full_text_ready` counts all ready records in the selected access artifact.
`eligibility_completed` counts records processed in the current invocation.
`eligible` counts deterministic eligible results.
`excluded` counts deterministic scientific exclusions.
`unresolved` counts records without a valid eligibility decision.
`generation_rejected` counts downstream QA candidates that failed validation.
`accepted_qa` counts accepted base questions.
An unresolved record never increments `excluded`.
It keeps at most 100 recent paper records.
For a brokered run, it also contains `broker_status_sha256` and `budget_policy_sha256`.
The broker refreshes those custody hashes after each durable ledger change.
After export, `dataset_metadata_sha256` binds the progress record to the export manifest.
Each export ID binds the SHA-256 value of every emitted JSONL file.
Thus, changed rejection content creates a new immutable export directory.
The progress and export manifest use the campaign ID as their shared `run_id`.
The progress record keeps the command run ID separately as `invocation_run_id`.
It also binds `run_manifest_sha256` to an immutable invocation manifest.
That manifest freezes the access selection, completion receipt, eligibility inputs, providers, models, phase, campaign, generation arm, and export seed.
Changing those inputs under the same command run ID fails before a model call.

Article-access item records can omit catalog fields that remain in the ordered selection.
The scheduler carries the selected authors and year into the source record.
It records a missing discipline as `unclassified` and does not guess a subject from the title or venue.
It skips items that are not `full_text_ready` and continues to the next ready item in the frozen order.
It also continues after a brokered response fails deterministic evidence validation.
That paper receives `eligibility_unresolved` with the exact validation errors.
The scheduler makes no downstream QA call for that paper.
It does not normalize or accept unmatched evidence.
Request identity, receipt, transport, accounting, and integrity failures still stop the run.

## Live command boundary

Live mode is disabled in the checked-in execution gate.
The exact integrated commit needs an independent review pass before the gate can change.
After an explicit pass of the complete integrated revision, current supervisor authorization permits a private gate and a tiny canary.
No new captain permission is required at that point.

After that review gate passes, use one of these phases:

- `live_test`
- `away_production`

Both phases must use the same central ledger and receipt directory:

```text
/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json
/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts/
```

Supply the credential through a private file with mode `0600`.
Its parent directory must not grant group or other access.
Do not put the key in a command, log, repository file, vault note, or export.

The live command shape is:

```bash
PYTHONPATH=src python -m arctic_qa --json stream \
  --phase live_test \
  --run-id live-test-r1 \
  --campaign-id streaming-commission-r1 \
  --access-run-dir /mnt/crdata/research-abstention/arctic-qa/ARTICLE_ACCESS_RUN \
  --eligibility-run-dir /mnt/crdata/research-abstention/arctic-qa/ELIGIBILITY_RUN \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v3.txt \
  --eligibility-policy-file /mnt/crdata/research-abstention/arctic-qa/ELIGIBILITY_POLICY.json \
  --credential-file /PRIVATE/DIRECTORY/gemini.key \
  --prior-construction-spend-usd KNOWN_VALUE \
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts \
  --ledger-config-transition-file /PRIVATE/DIRECTORY/config-transition.json \
  --progress-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/progress.json \
  --max-papers 20
```

Use `--role-profile` to bind the run to one profile in `config/roles.v1.json`.
Each judge role must then use the model that profile configures.
A run in the `away_production` phase must give this option.

Use `--ledger-config-transition-file` only when an existing ledger has a reviewed configuration change.

The transition must bind the current ledger, identity record, private gate, exact code revision, and independent review record.

The broker applies the transition without resetting prior spend or submission counts.

Schema v1 authorizes a reviewed price configuration change.

Schema v2 can authorize the exact live-test paper limit change from 20 to 40.

It can also authorize one later transition with these two changes:

- `live_test_maximum_papers` from 40 to 41
- `live_test_maximum_generation_submissions` from 100 to 101

It can authorize one final transition from the 41/101 policy:

- `live_test_maximum_papers` from 41 to `null`
- `live_test_maximum_generation_submissions` from 101 to `null`

This final transition removes the live-test count stops.

It keeps the USD 5 cumulative trial ceiling and all other broker controls.

The broker reports `null` for the two remaining live-test count values.

The later transition must authorize both fields separately in its change set.

The broker rejects partial, mismatched, or additional changes.

Every v2 transition keeps the cumulative USD 5 live-test ceiling.

The command must still set `--max-papers` to the intended fixed-order input bound.
The immutable input manifest's `target_total` is the maximum accepted value.
There is no separate fixed 500-paper CLI ceiling.

A new live continuation gate can set `continuation_input_binding_version` to
`stream-input-binding-v1`. The gate must then bind the access directory, access
run ID, manifest and completion-receipt hashes, frozen-manifest hash, remaining
order hash, and family count. The stream validates this binding before work.
The broker validates it again before every paid request. A substituted or
changed input stops before provider transport. Historical gates without this
version marker keep their existing behavior.

The checked-in default policy remains at 20 papers and 100 submissions.

Each expanded policy requires an accepted transition for an existing ledger.

The first transitioned request binds its ledger record and receipts to the exact transition event.

Each restart rejects an unreviewed, replaced, additional, or incorrectly bound event before provider submission.

See [the shared model broker guide](SHARED_MODEL_BROKER.md) for the required fields.

The broker checks the gate before it reads the credential.
It has no automatic retry, model fallback, or budget reset.
An unknown charge stops all later calls.

## Successful-path call budget

The current scheduler makes ten calls for a deterministic match on one accepted paper.
Eligibility is the first call in the same command.
Nine QA and distractor calls immediately follow an eligible decision.
An accepted judge fallback adds one call.

| Stage | Calls | Input boundary | Output boundary |
| --- | ---: | ---: | ---: |
| Scientific eligibility, upstream | 1 | Counted before submission, at most 1,048,576 tokens | At most 8,192 tokens, including thinking |
| Finding and answer extraction | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Question generation | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Blinded reconstruction | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Answer verification | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Answer-agreement judge fallback | 0 or 1 | Compact question, required context, and two answers | At most 4 tokens, with thinking disabled |
| Distractor generation | 1 | Counted before submission, at most 1,048,576 tokens | At most 2,048 tokens, including thinking |
| Exact-option verification | 4 | Each call is counted before submission, at most 1,048,576 tokens | Each call is at most 2,048 tokens, including thinking |
| Downstream scheduler total | 9 or 10 | Each input is counted separately | 18,432 or 18,436 maximum requested output tokens across calls |
| Full new-paper total | 10 or 11 | Each input is counted separately | 26,624 or 26,628 maximum requested output tokens across calls |

The broker reserves each request from its exact counted input and configured output cap.
The base model uses low thinking.
The answer-agreement judge uses minimal thinking.
The fixed output limits still include both candidate and thinking tokens.
See [the Gemini structured-output budget correction](GEMINI_STRUCTURED_OUTPUT_BUDGET.md).
The verified price record uses USD 0.75 per million input tokens.
It uses USD 3.75 per million output and thinking tokens.
The judge uses `gemini-3.1-flash-lite` at USD 0.25 per million input tokens.
Its output costs USD 1.50 per million tokens.
Each request must reserve no more than USD 0.25.
Each paper must use no more than USD 1.00.

The successful synthetic broker test reports 100 input, 10 candidate, and 5 thinking tokens for each call.
Its ten-call totals are 1,000 input, 100 candidate, and 50 thinking tokens.
Its fixture-priced ledger spend is USD 0.001320.
The nine downstream calls account for USD 0.001188 of that fixture total.
These values test accounting only.
They are not production measurements or yield estimates.

An earlier planning example used seven calls.
That estimate does not match the implemented path with four separate exact-option checks.
The implementation keeps all four checks and reports nine downstream calls.

## Resume and binding

The campaign ID freezes one finding for each paper family across live-test and production invocations.
Changing an invocation run name cannot authorize another paid request for the same model, stage, paper, family, and payload.

The local call journal binds each result to the provider, model, paper, family, immutable source SHA-256, schema, parameters, prompt, and candidate entity.
The central broker independently binds the model, stage, paper, family, immutable source SHA-256, and exact request payload.
The broker rejects a paper family that changes its source version.
It also rejects one source version assigned to two paper families.
An eligibility job resumes without another call only when its exact request and response resolve to a completed receipt in the shared ledger.
Standalone or fabricated eligibility jobs cannot skip the brokered eligibility call.
Each resume reruns deterministic eligibility validation against the frozen full extraction and the broker receipt response.
Saved job state and validation fields have no acceptance authority.
An invalid evidence result stays uncertain and nonaccepted.
The result remains resumable without another request and does not block the next frozen paper.
Completed receipts are reused only after the broker validates that each immutable event remains present in its ledger.
Ambiguous receipts stop the scheduler.
A refusal that names the per-paper cost cap does not stop the run.
The producer records the family at stage `paper_cost_cap` with the reason code `paper_cost_cap_reached` and continues with the next paper.
The refused receipt is immutable and is not resumable, so a relaunch replays it at no cost and never re-tries the capped family.
See [the shared model broker guide](SHARED_MODEL_BROKER.md), section "The per-paper cost cap".
Do not replay an ambiguous request.
The `reconcile-usage` command can settle one saved omitted-zero usage response after an independent review and supervisor release.
It preserves all original receipts and writes a new immutable reconciliation receipt.
After settlement, the provider adapter can resume the same eligibility job from an authenticated in-memory receipt view.
The view binds the reconciliation event, both original receipt hashes, settled usage, and settled cost.
The local ambiguous call entry becomes complete only after the saved JSON passes schema validation.
This resume path does not call token counting or generation, including after a process restart.
The saved NDVI response binds prompt version 1.
Its continuation command must specify `--eligibility-prompt-file config/gemini-eligibility-prompt-v1.txt`.
The current CLI defaults use prompt version 3 and response schema v1.
Version 3 requires exact whitespace and Unicode preservation.
It also requires each quote to occur once in its cited block.
Repeated text must include adjacent exact text until the quote is unique.
Prompt versions 1, 2, and 3 and response schema v1 remain immutable for saved requests.

The candidate contract for new requests uses prompt version 4 and response schema v2.
Select it explicitly only after the focused review and release gate pass:

```bash
PYTHONPATH=src python -m arctic_qa --json stream \
  OTHER_REQUIRED_ARGUMENTS \
  --eligibility-prompt-file config/gemini-eligibility-prompt-v4.txt \
  --eligibility-schema-file schemas/gemini-eligibility.v2.schema.json
```

Version 4 renders each complete source text once with short scoped span IDs.
The internal immutable manifest binds each ID to the extraction hash, block hash, span hash, and UTF-8 byte range.
The response selects one or more IDs for each criterion.
The validator rejects unknown IDs, changed bindings, out-of-range bindings, and repeated IDs within one criterion.
One verified span can support separate criteria.
Cross-criterion reuse does not change the span identity, hash, location, or scientific acceptance rules.
The validator copies the exact source bytes into the resolved evidence record.
It does not accept a generated or normalized quote.
Multiple IDs support findings that PDF extraction separates with text from another column.
The response does not contain an overall decision.
The program derives the decision from the five criterion statuses with mapping `eligibility-criterion-status-map-v1`.
This mapping does not change the scientific criteria.
Span location proves source provenance, but it does not prove scientific entailment.
The existing evidence and scope gates still decide whether the selected text supports the status.

Generation prompt version 5 uses immutable source spans for all evidence-bearing generation and verification roles.
The program splits extracted lines at PDF column gaps of at least three spaces.
It binds each span ID to one chunk, exact character offsets, and the SHA-256 value of the span text.
The extractor selects one span ID and cannot supply replacement quote text or offsets.
The program copies the selected span into the answer evidence record.
The candidate retains the span ID, contract version, and text hash.
An unknown span ID rejects the candidate with `finding_evidence_span_not_found`.
This change prevents a model from joining visually adjacent text across PDF columns.
It does not prove that the selected span entails the answer.
The reconstructor, answer verifier, distractor writer, and option verifier also select source span IDs.
The program copies stored text and offsets into each downstream record.
Each role gets a distinct rejection code for an unknown span ID.

Generation prompt version 12 keeps model justifications in separate rationale fields.
These fields preserve the selection and verification records without adding rationale text to the answer.
Generation prompt version 13 requires one concise answer for one focused question.
Finding policy version 5 applies this answer selection rule to new findings.
It keeps necessary units, entities, relations, and qualifiers in the answer.
It permits multiple values only when the question requests all of them.
The question writer must request exactly the content of `answer.text`.
Version 13 does not change the blind reconstruction input or the acceptance gates.
Generation prompt version 14 adds a separate `question_context` string.
The writer leaves this field empty when the question is self-contained.
Otherwise, the field contains only source-supported information that is necessary to understand the question.
The verifier checks the context in the existing answer-verification call.
The checks cover necessity, source support, and answer leakage.
The blinded reconstruction call receives the question and the question context.
It does not receive the reference answer, answer evidence, rationale, paper-selection data, or reviewer data.
All later option checks bind to the question and the question context.
Existing records and receipts keep their original prompt versions and decisions.
The pipeline does not add context to an existing record without a new versioned process.
Generation prompt version 22 gives every role a two-part evidence bundle.
`SOURCE_DATA` holds the selectable finding spans. `CONTEXT_ONLY_SOURCE` holds the hashed study-setting spans of the same paper.
A context-only span supports a `question_context` statement only. No role can select one as answer evidence, as a scope value, or as a required question phrase.
Version 22 replaces the empty-context default with a checklist of ten referent slots.
The writer sets `question_context` to an empty string only when the question alone fixes every applicable slot.
Candidate schema 2.7.0 records the forwarded context-only spans and the writer's `referent_slots` diagnostic.
Generation prompt version 23 and candidate schema 2.8.0 show each model a locator-redacted projection of every context-only span.
The extractor cites a supplied span for every non-null scope value in `scope_evidence`.
The writer records `resolver_text` for every stated referent slot.
See [the benchmark input contract](BENCHMARK_INPUT_CONTRACT.md) for the chapter 3 writer-context rules and for external evaluation custody.
See [the shared model broker guide](SHARED_MODEL_BROKER.md) for the exact command and rules.
An immutable-event failure republishes broker status with `halted` set to `true` and `integrity_valid` set to `false`.
The broker observer updates streaming progress to bind that halted status.

The scheduler records one accepted base question for each family only after at least three distractors pass all checks.
MCQ variants do not increase that count.
It preserves a valid short answer with fewer than three accepted distractors as `incomplete_non_mcq`.
That record uses a separate incomplete short-answer export.
It does not increase the 500-item target or headline `accepted_qa` count.
It exports an answer-present MCQ only with three accepted distractors.
It exports an absent-answer form only with four accepted distractors and the `invalid_option_set` label.

## Chapter 2 launch contract

Chapter 2 (captain order of 2026-09-15) runs campaign `arctic-qa-production-campaign-002` on the column-aware chapter 2 corpus.
The streaming input is the gate-bindable access run that `chapter2-corpus --action stream-input` writes under the chapter 2 root.
It carries the frozen manifest hash, the frozen order hash, and the run manifest hash that the execution gate binds.
The run names the role profile `gemini_separated`, so the writer is `gemini-3.8-flash` and every judge is a different model.
The broker price config revision `arctic-gemini-eligibility-r1-config-v6` registers the judge stage models with verified pricing.
The budget is one chained ledger transition: the price config change first, then the policy ceiling from USD 61.614496 to USD 108.994972, which is the USD 33.994972 spent before chapter 2 plus the USD 75.00 chapter 2 allocation.
The run halts at exhaustion, with no replay, no retry, and no budget reset.
The live export selects schema `2.7.0` and prompt `arctic-qa-generation-v22`, so the dataset page shows chapter 2 items only.

## Chapter 3 call plan

The chapter 2 yield audit (sections 4.4, 4.5 and 4.9) reordered the paid calls of one question attempt.
The gates, the reason codes and the routing inputs did not change.
Only the timing of the calls changed.

The judge call plan (`judge-call-plan-v1`, recorded in `provenance.judge_call_plan`):

1. The free checks run right after the writer, with the same names and inputs as at the QA gate.
2. The standalone call is made for every candidate, because routing reads its codes.
3. When a free check or the standalone gate fails, the reconstructor and the answer verifier are not called.
   The candidate is persisted as `qa_gate_failed` with `reconstruction` and `answer_verification` set to `null`.
   Its reason list holds the standalone codes and the free codes only, so routing reads the same input as before.
4. When the writer's own `referent_slots` record marks a slot `unavailable_in_source`, no judge is called.
   The reason list starts with one `writer_slot_unavailable_<slot>` code per such slot.
5. A seeded random cohort of 5 percent (`judge-short-circuit-shadow-cohort-v1`) runs every call anyway.
   The cohort member keeps the short-circuit reason list for routing and records the full-suite list in `shadow_gate_reasons`.
   The draw is a hash of the policy version, the campaign and the candidate entity, so a replay selects the same members.

Every role receives the evidence spans once.
The chunk text no longer rides beside the tiled spans of the same text.
The static instructions of the extractor, the reconstructor, the answer verifier and the option verifier ride in the system instruction, as the standalone judge's already did.
The prompt hash binds the system instruction, so a receipt still matches its exact request.
The finding context has a measured budget, `MAX_FINDING_CONTEXT_CHARS`.
A paper above it is rejected with `finding_context_over_budget` before any paid call.

The finding bank (`ranked-finding-bank-v1`, table `finding_bank`):

- Every ranked candidate the extractor returns is persisted with its admission result and its span ids.
- The bank is keyed to the extractor prompt, the admission contract, the span contract and the Arctic scope custody state.
- An attempt without a frozen finding is served from the bank first.
  A banked candidate passes the same admission path as a fresh one.
  The extractor is called again only when the bank holds no servable candidate.
- The extractor also returns `answer_basis_class` and `source_blind_answer_basis` per candidate.
  Both fields stay outside the frozen answer and outside every judge payload.
  A `study_internal_index` candidate is ranked last.
  When only such candidates remain, the family records `no_admissible_finding`.
- The free admission re-ask is spent only when an unexcluded eligible span remains.
- The structural pre-screen (`structural-finding-prescreen-shadow-v1`, table `finding_prescreen_shadow`) records one shadow verdict per paper and never blocks a call.

Options are verified in the writer's rank order.
Verification stops once four distractors are verified: the floor of three for the present MCQ plus the fourth that the absent-answer form needs.
The remaining proposals are recorded as a reserve in `provenance.option_verification_call_plan`.
The validator's model-free option checks run before any paid option call.

A production run never selects the `cost_aware` role profile.
The `data/arctic-ch3-cost-r1/` directory holds the receipts-based measurements behind these rules.

## Viewer command

The read-only viewer accepts the shared ledger, progress, budget policy, and export metadata files.
It never initiates a model call.

See `docs/CORPUS_VIEWER.md` for its full command.
