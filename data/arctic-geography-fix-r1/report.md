# Geography correction implementation report

## Current runtime status

This section supersedes the earlier pending-release and partial-suite text below.
The active segment is `first-production-6fbdf41-r1-geo-v3-scope-r2` in the
existing campaign `arctic-qa-production-campaign-001`, using the original
ranked-800 input, shared ledger, USD11.614496 historical baseline, USD50 new
cap, USD61.614496 cumulative cap, USD0.25 request cap, USD1 family cap, and no
fallback or automatic retries. It uses immutable v6 prompt SHA-256
`65b861845e1335656153357309478391289e9f92ea6485c3e205587f8006f96f` and
released gate SHA-256
`792b0d9467250d02d979d5b95ea7a4762bd23b583e9ba6332ad21ec35c127c0e`.

The previous v3 segment and every v2/v5 artifact remain immutable. Its first
v3 manifest refused before provider transport because it retained v2 inputs.
The v5 segment stopped after ten decisions on a whitespace-only chunk boundary;
the final code accepts only token-bounded whitespace equivalence and turns any
remaining source-scope mismatch into a paper-local rejection. The v6 sample
eliminated the prior repeated activity-ID binding defect. Remaining source-scope
phrase failures stay uncertain, and finding/reconstruction failures stay
paper-local generation rejections. No new accepted family is claimed from the
retained campaign acceptance counter.

## Current checkpoint

The correction and question-context integration are committed locally. No focused
check is failing. The final generation prompt version is 14.

## Implemented correction

The new eligibility policy is `config/arctic-eligibility-policy-v3.json`.
The matching prompt is `config/gemini-eligibility-prompt-v5.txt`.
The matching schema is `schemas/gemini-eligibility.v3.schema.json`.

The policy uses 66.56 N as the latitude threshold. It applies to land and sea.
An explicit study latitude at or north of the threshold qualifies even when the
marine name was absent from the old name list. Evidence must describe study
activity, observations, or results. It cannot be only a title, affiliation,
background statement, or citation. A named-region fallback is limited to
documented regions. A boundary-crossing region needs evidence of a northern site.

The v3 response carries an `eligible_arctic_scope`. A separable Arctic component
has explicit activity and result spans plus required question phrases. Finding
extraction receives only its eligible result spans. Validation rejects a question
or selected finding that loses this scope. Existing acceptance and validation
rules are not bypassed.

Historical v2 jobs remain immutable. The stream loader selects jobs only when
their prompt, schema, and policy hashes match the active version. A v3 review
therefore creates a distinct request identity and cannot replay an old provider
request key.

`arctic-qa geography-correction-overlay` writes an immutable 16-row correction
overlay. Each row records the old job, prompt, schema, policy, source and
extraction hashes. It also records the reviewed source locator, proposed action,
decision source, and decision time. It states that it is a correction proposal,
not a new model decision.

The captured overlay is
`data/arctic-geography-fix-r1/correction-overlay/geography-correction-overlay.ndjson`.
Its SHA-256 is
`1ecefe47cf7cbe66cf430a9fcc08f6ae089132f620ad6cf988ac578ec1563467`.
The manifest records historical policy SHA-256
`be2ace915d377f0f5448371de1556d9f713cde170f6ed54975fcffe5bd0c74bb` and
new policy SHA-256
`64bd4d99527b96c1b52e1dbc91c3b02a3c7c58610e07770ea40fa8f90f65a773`.
The 12 retention candidates and four resolution candidates must receive v3
review. The old v2 decisions remain historical records.

## Recovery implementation

`settle-pretransport-reservation` is limited to the reviewed interrupted request
`445c8935c5dc9d1c5d03fe7d4d15308fd57d0e68f8d2d21bf310875b85b5512b`.
It requires ledger hash
`1ef465bf0f539709f8293f193e956b7b275d83e259d3ab5be760a4448b6d7a13` at use time,
the reviewed identity, no final, submitted, received, or trace sidecar, and both
review and interruption evidence files. It calls neither token counting nor
generation.

The command writes its immutable zero-cost settlement event before its final
receipt. The receipt records `live_call_made: false` and has no response. It
clears only the reservation and inflight count. It retains submission count and
prior spend. Ledger validation verifies the event and the evidence hashes.

The existing price and budget transition receipt binds the old gate. A reviewed
successor gate can record the exact old gate, integrated commit, review file, and
review hash in `supersedes_config_transition_review`. The broker validates this
predecessor binding and the new review hash. It does not edit the old transition.

## Focused checks

Passed:

- v3 explicit northern marine latitude, separable Arctic scope custody,
  insufficient evidence, Antarctic, and incidental-title controls.
- A simulated interruption after durable reservation, before transport. The
  settlement made no provider call, is idempotent, and restores a zero
  reservation without a fake response.
- Existing v2 span-contract tests.
- Existing streaming regression after retaining the concise atomic-answer prompt
  wording.

Commands used:

```sh
nix develop -c env PYTHONPATH=src pytest -q tests/test_question_context.py tests/test_geography_correction.py tests/test_eligibility_span_contract.py tests/test_model_broker.py::test_pretransport_settlement_recovers_only_a_reviewed_interrupted_reservation -vv
nix develop -c env PYTHONPATH=src pytest -q tests/test_streaming.py::test_streaming_uses_one_shared_broker_for_all_ten_stages -vv
nix develop -c env PYTHONPATH=src python -m compileall -q src
git diff --check
```

## Reviewed restart path

The original v3 launch kept its immutable invocation manifest and stopped before
generation transport because that manifest was bound to v2 eligibility inputs.
The corrected continuation uses run ID `first-production-6fbdf41-r1-geo-v3` and
a separate v3 eligibility directory of the same name. It keeps the campaign ID,
ranked input, shared ledger, historical USD11.614496 baseline, USD50 new-spend
cap, and all family-level accounting. The successor gate records the original
manifest hash and no-call refusal; it does not overwrite either old artifact.

Do not run this before the supervisor releases the corrected gate and verifies
the external ledger state. The supervisor must first create a new immutable
execution gate that binds the v3 policy, v5 prompt, v3 schema, prompt version 14,
the reviewed code commit, the correction overlay, and the existing campaign
budget. Then run the recovery command with the exact reviewed evidence paths.
After the recovery check, run the existing production command with the new
eligibility paths and the existing ledger, ranked input, campaign ID, and budget
baseline. Do not create a ledger or submit a batch.

The reviewable successor gate draft is
`data/arctic-geography-fix-r1/release-draft/successor-execution-gate-v3.draft.json`.
It is deliberately disabled and binds the completed independent review. The exact recovery
and restart commands are in
`data/arctic-geography-fix-r1/release-draft/recovery-and-restart.md`.
They run from this reviewed worktree with code at
`d56f115f7a287745addf2e54ac14b3bdd24ecf80`, or an immutable runtime snapshot
of that exact commit. They do not depend on the stale primary clone.
