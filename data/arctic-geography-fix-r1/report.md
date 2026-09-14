# Geography correction implementation report

## Current checkpoint

The correction is implemented locally. No focused check is failing.
The next step is to commit this work, then integrate the reviewed question-context
commit `207d8b5453dcebd2a4907238e2432341ef2ba9a1`. The final integration must retain
generation prompt version 14 and pass the combined focused tests.

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

Commands used during the final pass will be recorded below after integration.

## Reviewed restart path

Do not run this before the supervisor releases the corrected gate and verifies
the external ledger state. The supervisor must first create a new immutable
execution gate that binds the v3 policy, v5 prompt, v3 schema, prompt version 14,
the reviewed code commit, the correction overlay, and the existing campaign
budget. Then run the recovery command with the exact reviewed evidence paths.
After the recovery check, run the existing production command with the new
eligibility paths and the existing ledger, ranked input, campaign ID, and budget
baseline. Do not create a ledger or submit a batch.
