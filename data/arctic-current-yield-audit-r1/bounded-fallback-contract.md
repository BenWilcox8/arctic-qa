# Bounded fallback persisted-state contract

Contract version: `bounded-paper-progression-v1`.

## Attempt paths

Each paper family can have at most two distinct finding indexes.

Each paper family can have at most one question or context revision in total.

Each paper family can have at most three candidate paths.

The deterministic path key is `(campaign_id, paper_family_id, finding_attempt_index, question_revision_index)`.

`finding_attempt_index` is 1 or 2.

`question_revision_index` is 0 or 1.

A revision keeps its parent's finding index.

Only one path for the family can have `question_revision_index=1`.

## Lineage fields

Persist this object as `provenance.generation_attempt` in each candidate.

Persist the same object as `detail.generation_attempt` in each generation rejection.

```json
{
  "contract_version": "bounded-paper-progression-v1",
  "attempt_id": "stable deterministic ID",
  "attempt_kind": "primary|question_revision|alternative_finding",
  "finding_attempt_index": 1,
  "question_revision_index": 0,
  "parent_attempt_id": null,
  "parent_item_id": null,
  "trigger_reason_code": null,
  "finding_policy_version": "exact policy used",
  "excluded_finding_span_ids": []
}
```

`attempt_id` is `stable_id("generation-attempt", campaign_id, paper_family_id, finding_attempt_index, question_revision_index, contract_version)`.

`parent_attempt_id` is null only for the primary path.

`parent_item_id` is present only when the parent produced a candidate row.

`trigger_reason_code` is null only for the primary path.

An alternative finding records only prior selectable span IDs in `excluded_finding_span_ids`.

It must not record or send the failed answer as reconstruction input.

## Finding identity

Use one immutable finding row for each finding index.

Derive its selection policy as `base_policy + ":finding-" + index`.

The second extractor request must have a new entity and request identity.

Its prompt can exclude the first finding's selectable source span IDs.

The second finding must have a different `finding_id` and different selectable evidence interval.

A question revision reuses the same immutable finding row.

It must not update or replace the parent candidate.

## Stage checkpoints

Use the attempt ID in the entity identity for every model stage.

The expected ordered checkpoints are:

1. `finding_answer_extraction`, except for a question revision that reuses its parent finding.
2. `question_generation`.
3. `blinded_reconstruction`.
4. `answer_verification`.
5. `distractor_generation` when QA gates pass.
6. One `option_verification` for each proposed option.
7. One payload-bound `validation_event`.

Model receipt state is the stage checkpoint.

The candidate row and payload-bound validation event are the completed candidate checkpoint.

A generation rejection is the terminal checkpoint when no candidate row exists.

Do not mutate the parent candidate or delete any receipt, finding, rejection, or validation event.

## Revision behavior

A question revision receives the parent question, parent context, and one allowlisted failure reason.

It produces a new question and context for the same frozen finding.

It must repeat blinded reconstruction, answer verification, distractor generation, and option verification.

Do not use `apply_one_correction` for this path.

Do not expose the proposed answer to the blinded reconstructor.

## Transition rules

Use explicit allowlists for `question_revision` and `alternative_finding` transitions.

The first shipped alternative-finding allowlist contains `reconstruction_disagreement` and `insufficient_verified_distractors`.

All other failure reasons are terminal until a later reviewed contract adds them.

Do not progress from a scientific exclusion, accepted MCQ, budget stop, provider ambiguity, or terminal operational error.

Prefer one question revision when the failure is limited to stand-alone wording or supported context.

Prefer a second finding when the first finding is intrinsically unsuitable or cannot produce three verified distractors.

After one revision is used, no other finding can receive a revision.

After two finding indexes are used, no other finding can be selected.

The one-dollar family cap and every global cap apply before each new model request.

A cap stop records its existing budget reason and does not consume a new attempt path.

## Resume and idempotency

On resume, reconstruct family progression from current-contract candidate provenance and generation-rejection details.

Ignore old prompt, schema, and attempt-contract versions for current progression.

Sort by `(finding_attempt_index, question_revision_index, attempt_id)`.

Reject duplicate path keys or inconsistent parents as corrupted state.

If a path has a payload-bound validation event, use that terminal result without a model call.

If a path has a terminal generation rejection, apply the transition allowlist once.

If a path is partial, rerun the same deterministic attempt ID.

The broker must resume the same deterministic stage request from its retained receipt state.

Never create a new paid request ID for the same stage checkpoint.

Never blindly replay a submitted or ambiguous request.

Stop after acceptance or after the family reaches its finding, revision, path, or budget cap.

Only one accepted published QA remains the family default.

If every alternative fails, preserve an earlier `incomplete_non_mcq` candidate in the incomplete export.

Do not count that candidate as an accepted MCQ.
