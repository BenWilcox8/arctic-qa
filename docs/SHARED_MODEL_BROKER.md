# Shared model broker

The streaming pipeline must send every paid model request through one broker.

The commands of this document write path variables such as `$ARCTIC_QA_DATA_ROOT`.
The "Environment variables" section of `docs/REPRODUCTION.md` gives their values.

Use `SharedGeminiBroker` from `arctic_qa.model_broker`.

Use one central ledger for all runs and stages:

`$ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json`

Keep provider receipts in this private directory:

`$ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/model-receipts/`

The ledger schema is `shared-paid-call-ledger-v1`.

Do not create a new ledger for a new process, run, phase, or stage.

The legacy Gemini provider and standalone Gemini `run` and `resume` actions are disabled.

They cannot send paid requests outside this ledger.

The adjacent immutable identity record makes a missing ledger fail closed.

The broker also validates ledger totals against immutable request receipts.

An integrity mismatch creates an adjacent halt record and blocks more paid work.

The broker publishes an adjacent `shared-gemini-broker-status-v2` record after each ledger change.

This record contains verified limits, use, remaining capacity, stage totals, and paper totals.

Consumers must verify its ledger and policy hashes before they show its values.

## Invocation

Construct the broker with the policy, price, gate, central ledger, and receipt paths.

Pass the private credential path and the known prior construction spend.

The constructor creates the first ledger and its immutable identity record.

Later constructors require both files and require the same frozen inputs.

## Reviewed configuration transitions

The immutable identity record keeps the hash of the initial price configuration.

Do not replace this record or reset the ledger after a reviewed configuration change.

If an active configuration hash differs, provide `config_transition_file` when you construct the broker.

The CLI option is `--ledger-config-transition-file`.

Use schema `shared-paid-call-config-transition-v1` for a price configuration change.

It must contain these fields:

- `ledger_file`
- `from_price_config_sha256`
- `to_price_config_sha256`
- `expected_ledger_sha256`
- `expected_identity_sha256`
- `execution_gate_sha256`
- `integrated_code_commit`
- `review_record`
- `review_record_sha256`
- `reason`
- `authorized_at_utc`

The broker validates the ledger and all prior receipts before it applies the transition.

The ledger must have no halt or inflight request when the broker first applies the transition.
Each remaining reservation must have validated no-replay recovery evidence.
The transition keeps the full held amount in all budget totals.
An uncovered reservation or ambiguous charge stops the transition.

Use schema `shared-paid-call-config-transition-v3` after an earlier policy transition.
Version 3 binds the active predecessor event and keeps the active policy unchanged.
It changes only the registered price and model configuration.

It also validates the private gate and the exact independent review record.

The broker writes one immutable `config-transition-*.json` event in the receipt directory.

It does not change the existing ledger, identity record, request receipts, spend, or submission counts during this operation.

Later starts can use the immutable event without the original transition file.

Before the first transitioned request, each restart validates the embedded ledger snapshot, gate, and review record again.

Evaluation requests made after the application are the one change the ledger may carry at that point; a construction request made under the previous configuration still stops the start.

Each transitioned request binds its ledger record and immutable receipts to the exact transition event hash.

Later restarts require this binding before they accept a historical transition.

Before each request, the broker validates the authorized gate and review bindings again.

It validates the gate hash, commit, review path, review hash, and review file content.

The budget-bounded null/null transition also requires a complete
`stream-input-binding-v1` gate. That gate binds the reviewed access manifest,
completion receipt, frozen order and identity, eligibility prompt, eligibility
schema, eligibility policy, invocation run ID, and campaign ID. The stream
checks the complete binding before work. The broker checks the bound files and
run identity again before each paid request. A changed or substituted approved
input stops before `countTokens`.

It does not compare the one-time ledger snapshot after the first transitioned request.

A replaced event or an additional matching event causes an integrity halt.

The status record reports the initial hash, the active hash, and the transition event hash.

Each new request receipt records the active price configuration hash and its transition event hash.

An absent, altered, or unreviewed transition stops the broker before it reads the credential.

### Exact live-test limit transitions

Schema `shared-paid-call-config-transition-v2` permits three exact policy transitions.

The first transition changes `live_test_maximum_papers` from 20 to 40.

The second transition changes these two fields together:

- `live_test_maximum_papers` from 40 to 41
- `live_test_maximum_generation_submissions` from 100 to 101

The third transition changes these two fields together:

- `live_test_maximum_papers` from 41 to `null`
- `live_test_maximum_generation_submissions` from 101 to `null`

The two `null` values remove only the live-test count limits.

The third transition does not remove a monetary or operational limit.

The USD 5 cumulative live-test ceiling remains the controlling trial limit.

The USD 1 family limit and USD 0.25 request limit remain active.

The concurrency limit, rate limit, retry ban, and fallback ban also remain active.

The status record shows `null` for both remaining live-test count values in this mode.

The second transition change set must list each authorized field separately.

The broker rejects a partial change, a different value, or an additional policy change.

Use the same `config_transition_file` constructor argument and CLI option.

The v2 file contains all v1 fields and these fields:

- `from_config_transition_sha256`
- `from_policy_file`
- `from_policy_sha256`
- `to_policy_sha256`
- `changed_policy_fields`
- `maximum_authorized_cumulative_tranche_usd`

The change set must match one of the three exact transitions.

The source policy must remain present and match `from_policy_sha256`.

The target policy must equal that source except for the authorized limit fields.

The price configuration must not change in this transition.

The cumulative live-test ceiling in this transition must be USD 5.

This ceiling includes all earlier live-test spend in the shared ledger.

If a prior transition exists, v2 must name its event hash as the predecessor.

The predecessor must be the unique event that produced the source policy and price pair.

The broker keeps the identity record, spend, requests, receipts, and family counts.

The initial 20-paper policy remains the default for a new ledger.

An expanded policy cannot create a new ledger.

The broker records the active policy hash in each new request and receipt.

The status record shows the initial and active policy hashes.

It also shows the authorized cumulative live-test ceiling.

### Registered construction ceiling transitions

Schema v2 also moves the construction ceiling, `away_session_total_ceiling_usd`.

Each allowed move is one constant in `CEILING_CHANGES` in `model_broker.py`, and `_validate_policy` lists each allowed ceiling value.

The tranche of a ceiling transition must equal the new cumulative ceiling.

A ceiling transition needs a complete `stream-input-binding-v1` gate.

A new budget needs its constant and a chain test before any transition file can apply (`tests/test_chapter3_production_run.py`).

The chapter 3 expansion change set (`CHAPTER3_EXPANSION_CHANGE`) moves four fields together: the ceiling, `away_maximum_generation_submissions`, `accepted_question_target` and `construction_review_checkpoint_usd`.

The broker refuses each of these four fields alone, and it accepts the expanded counts only under the expansion ceiling.

### The per-paper cost cap

`maximum_paper_cost_usd` bounds one paper family, never the run.

The broker refuses a request that would take the family past the cap with `PAPER_COST_CAP_REASON`.

It records the refusal as a `not_submitted` receipt and charges nothing past the cap.

The receipt is immutable, and the reason is not resumable.

Thus, a relaunch replays the same refusal at no cost and never re-tries the capped family.

The broker itself replays the refusal: `execute` returns the stored receipt before it paces, counts or resumes.

`_resume_not_submitted` refuses that receipt under every transition, and a settlement never moves its row.

The streaming producer reads that refusal as `errors.PaperCostCapError`.

It settles the in-flight call record as `generation_incomplete`, writes a `rejection_ledger` row at stage `paper_cost_cap` with the reason code `paper_cost_cap_reached`, the family's committed spend and the stage that stopped, and continues with the next paper.

Only a whole-run stop ends the producer: the allocation ceiling, the session ceiling or a halt.

To raise the cap for a family, move `maximum_paper_cost_usd` through a registered budget transition. Do not change the skip.

### Orphan recovery and concurrent settlement

Every paid call recovers interrupted requests before it starts.

The recovery reads the ledger one time, then settles each request it found.

Another worker of the same ledger can settle one of those requests inside that window.

Therefore the recovery reads the row again before it writes a receipt, and it skips a row that is no longer `submitted`.

A settlement never ends the run: a row that is not `submitted` holds no reservation to release.

Such a settlement writes `<request_key>.settle-skipped.json` beside the receipts and returns.

The note has the schema `shared-paid-call-settle-skipped-v1`, the observed state and the reason.

The note is an observation, never an accounting event: the money of the request stays in the ledger row the other worker wrote.

A settlement that ended the run this way stopped the chapter 3 producer at 13:53 UTC on 2026-09-16.

### The exclusive operation lock

One shared ledger has one exclusive operation lock.

`model_broker.hold_operation_lock` is the only way to take it.

A reviewed operation takes the lock with no wait.
Such an operation is an authorization, a settlement, a reconciliation or an exclusive batch activation.
It refuses a held lock at once with `another paid broker operation is active`.
Two reviewed operations of one ledger must never overlap, and the second one has an operator to tell.

The ordinary request path, `execute`, waits for the lock instead.
The bound is `OPERATION_LOCK_WAIT_SECONDS`, and the wait polls each `OPERATION_LOCK_WAIT_INTERVAL_SECONDS`.
A reviewed operation is short, and a request that meets one describes no fault of its own.

At the bound the request raises `errors.BrokerOperationBusyError`.
That error keeps the message of the immediate refusal and is a `ValueError`.
It reserves nothing and submits nothing.
The broker seam does not mark it a run stop, so the producer records it against one family, skips that family and continues.

Do not give a new reviewed operation the wait.
Do not make the bound-exceeded case a run stop.
The immediate refusal ended the chapter 3 producer on 2026-09-16 at 18:26 UTC while another task released the evaluation phase.

### The free token count and its errors

The broker counts the exact input tokens of each request before it reserves anything.
It does this with the provider's `countTokens` endpoint.
That endpoint is free.
A `countTokens` call reserves nothing, submits nothing and charges nothing, so it can never make the money uncertain.

A `countTokens` failure that is transient is retried in place.
The transient failures are HTTP 429, 500, 502, 503 and 504, a timeout, a connection fault and an answer the broker cannot read as a token count.
The retry makes `COUNT_RETRY_ATTEMPTS` attempts with a jittered exponential backoff of about two minutes in total.
Each attempt is recorded in the receipt field `count_attempts`, with its start time, its end time, its error, its HTTP status and its class.
A request that needed a retry keeps that list on its submitted receipt.

A transient failure that outlives the retry becomes a count error that halts nothing.
The receipt records `state` `count_error`, `count_failure_class` `transient` and `live_call_made` false.
The ledger row records the same state and class.
The broker seam raises `errors.CountUnavailableError`, which is a paper-level refusal: the producer records the family in the rejection ledger at stage `count_tokens` with the reason code `count_tokens_unavailable`, skips that paper and continues.
A later visit of the same family counts it again, under a new count-retry round.

A `countTokens` failure that is permanent halts the phase, as before.
The permanent failures are HTTP 400, 401, 403 and 404, and every status and error the broker cannot prove transient.
Such a failure means the request or the credential is wrong, and it repeats until one of them changes.
Only a review clears it.

Do not widen the transient set to a status the provider has not shown to be temporary.
A transient provider 503 halted the whole shared ledger and ended the chapter 3 producer on 2026-09-16 at 21:11 UTC.

### Count-retry rounds

A request whose free count failed transiently counts again under its own round.

The first count-error receipt stays immutable under the request key.
Round `n` writes its receipts under the stem `<request key>.count-retry-<n>`.
The ledger row records `count_retry_round` and `count_retry_from_sha256`, which is the hash of that first receipt.
The chain proves the retry replaced no paid call and settled no money.

`_open_count_retry` opens a round.

A stored count error is never replayed as a result while it may be counted again.
`broker_provider.invoke` reads the effective receipt of a request it already holds, and `model_broker.count_error_is_transient` decides it: a transient one goes back through `execute`, which opens the next round, and a permanent one is read back as before.
A receipt written before the bounded retry records no class, so the class is read from the error string and fails closed to permanent.
Replaying the reviewed count error of 2026-09-16 as a result ended the chapter 3 producer at 22:27 UTC, after its halt had been lifted.
It refuses a permanent count error with `the paid request key already exists`, as before.

### Reviewed count-error continuation

`authorize-count-error-continuation` clears one reviewed count error without replaying it.

The reviewed operation takes the exclusive operation lock, reads the review file and the evidence file, writes an immutable continuation record beside the receipts and lifts the ledger halt.
It never replays, retries or settles a paid call, because a count error has none.

The evidence file has one of two shapes.
The answer-judge evidence `arctic-answer-judge-count-error-evidence-v1` stays exact for the countTokens 404 of 2026-09-15, because that review named a replacement model rather than a retry.
Every other count error is reviewed through `shared-paid-call-count-error-continuation-evidence-v1`:

```json
{
  "schema": "shared-paid-call-count-error-continuation-evidence-v1",
  "request_key": "<the exact request key>",
  "count_error": "<the exact error string the receipt recorded>",
  "count_failure_class": "transient",
  "live_call_made": false,
  "replay_prohibited": true,
  "count_retry_authorized": true,
  "affected_family_id": "<the family of the request>",
  "authorized_run_id": "<the run of the request>"
}
```

Each field must match the ledger row and the receipt.
`count_retry_authorized` true also stamps the class `transient` on the row, so the next visit of that family counts the request again.
A permanent count error is never counted again on a review alone: the request or the credential must change first.

### Phase slots and windows

Each phase counts its own in-flight requests against its own concurrency limit.

An evaluation request in flight never takes a construction slot, and the reverse.

The ledger `inflight` counter still covers every phase, and the per-minute window stays shared.

When another request of the same phase holds the slot or the window, `execute` waits up to 90 seconds for room.

After that wait it records the refusal as a `not_submitted` receipt with the concurrency or minute reason.

A request that such a refusal stopped resumes under the next reviewed transition, like a request the live-test cap stopped.

The resume needs the refused request to carry a transition hash, and the active transition must name that hash as its predecessor.

The resumed request keeps its identity and runs under the active price and policy hashes.

Create the request key from the exact request identity.

The request identity does not use the run ID.

Thus, a new run cannot replay the same paper, stage, model, and payload.

Then call the broker one time.

```python
from arctic_qa.model_broker import SharedGeminiBroker, broker_request_key

request_key = broker_request_key(
    model="gemini-3.8-flash",
    run_id=run_id,
    stage="question_generation",
    paper_id=paper_id,
    family_id=family_id,
    source_version_id=source_version_id,
    payload=payload,
)

receipt = broker.execute(
    phase="live_test",
    run_id=run_id,
    stage="question_generation",
    paper_id=paper_id,
    family_id=family_id,
    source_version_id=source_version_id,
    request_key=request_key,
    payload=payload,
)
```

Valid construction phases are `live_test` and `away_production`.

Valid construction stages are defined in `arctic_qa.model_broker.STAGES`.

The abstention evaluation uses the phase `benchmark_evaluation` and the stage family `evaluation_answer:<model>`.
That phase has its own policy, price config, and gate, and never counts toward construction totals.
Read `docs/ABSTENTION_EVALUATION.md` before you meter an evaluation call.

An ambiguous evaluation request halts the evaluation phase only, through the ledger field `evaluation_halted`.
Construction continues under its own ceiling.

The evaluation ceiling needs its own reviewed chained transition, with schema `benchmark-evaluation-policy-transition-v1` and the registered change set of `EVALUATION_CEILING_CHANGES`.
The "The evaluation ceiling" section of `docs/ABSTENTION_EVALUATION.md` holds that contract.

The broker checks the offline-review execution gate before it reads a credential.

The broker binds each family to one paper ID and one source-version ID.

Use a stable source-version ID from the versioned full-text artifact.

Do not derive this ID from a run name or a display title.

The broker permits only text inputs and constrained structured output.
Most stages use JSON objects.
The answer-agreement fallback uses the `yes` or `no` enum.

The base Gemini model uses low thinking.
The `gemini-3.1-flash-lite` answer judge uses minimal thinking.

The broker selects the registered model and price by stage.
The answer-agreement stage keeps the same request identity, receipt, resume, and budget controls.

The broker does not increase the fixed output cap to make room for thinking.

It rejects tools, provider storage, fallback models, and requests above the output limit.

It counts tokens before it reserves a paid submission.

It reserves the worst-case request cost before the generation request.

It records actual input, output, and thinking tokens by stage.

It also records spend and reservations by stage and paper.

An unknown provider outcome reserves the full amount and stops the broker.

Do not retry an ambiguous request.

The provider can omit `thoughtsTokenCount` or `candidatesTokenCount` when its value is zero.

The broker uses zero only when the other three token counts are nonnegative integers.

The total must equal the sum of the two counts the provider reported.

The broker accepts one omitted count, never two.

All other missing or inconsistent usage values cause an ambiguous charge.

gemini-3.7-flash omitted `candidatesTokenCount` once in three calls on 2026-09-16, with a `STOP` finish reason and a one-letter answer.

The broker writes an immutable response event before it settles a successful request.

After a crash, it uses that event to complete the ledger and final receipt.

If no response event exists, it treats the interrupted request as an ambiguous charge.

## Provider rejections

The broker records the provider error body of every non-2xx answer in the ambiguous receipt, as `error_body` (at most 4000 characters) and `provider_error_status`.

A rejection before generation (HTTP 400, provider status `INVALID_ARGUMENT`) bills nothing.
The reviewed `settle-http-rejection` command settles that one ambiguous charge at zero cost.
It needs the private gate the request ran under, a review record, and an evidence file with schema `shared-paid-call-http-rejection-evidence-v1`.
When the receipt recorded the error body, the evidence names `error_body_source` `receipt`.
When the receipt predates the body capture, the evidence names `reproduction` with a record of one call of the exact same request (same `request_sha256`) that received the same rejection.
The broker writes `<request-key>.http-rejection-settlement.json`, moves the reservation out of the ambiguous funds, and lifts the halt when every other ambiguous request has its reviewed continuation.
The request key stays a settled record.
The broker never replays it, and a corrected request has a new key.

```bash
PYTHONPATH=src python -m arctic_qa --json settle-http-rejection \
  --request-key REQUEST_SHA256 \
  --expected-ledger-sha256 LEDGER_SHA256 \
  --review-file /PRIVATE/DIRECTORY/review.md \
  --evidence-file /PRIVATE/DIRECTORY/evidence.json \
  --authorized-run-id RUN_ID --operator-id OPERATOR \
  --streaming-budget-policy-file POLICY --price-config-file PRICE_CONFIG \
  --execution-gate-file /PRIVATE/DIRECTORY/gate.json \
  --shared-ledger-file LEDGER --model-receipts-dir RECEIPTS \
  --ledger-config-transition-file TRANSITION \
  --credential-file /PRIVATE/DIRECTORY/gemini.key \
  --prior-construction-spend-usd KNOWN_VALUE
```

## Ambiguous continuation

An ambiguous charge keeps its full reservation, because the provider can have billed the call.

The reviewed `authorize-ambiguous-continuation` command does not settle, retry, or replay that request.

It records the reviewed skip, so unrelated families continue, and it lifts the halt the charge set.

The command accepts three bounded cases: any 5xx answer with no received receipt, a `MAX_TOKENS` answer with unprovable usage, and a provider timeout.

It needs a review record, an evidence file, the authorized run ID, and the gate the request ran under.

The reservation stays in `ambiguous_reserved_usd` and counts against every cap.

The event file is `ambiguous-continuation-<request-key>.json` and it is immutable.

Every other outstanding ambiguous request needs its own event first.

### The phase of the release

The release lifts only the halt that covers the phase of the request.

An ambiguous construction charge halts the whole ledger, so the release clears `halted`.

An ambiguous `benchmark_evaluation` charge halts the evaluation phase alone, so the release clears `evaluation_halted`.

The construction phase keeps its own ceiling, slots, and window through an evaluation release.

An evaluation request is reviewed by its own evaluation gate, because the construction gate never allows that phase.

So the command takes `--evaluation-policy-file`, `--evaluation-price-config-file`, and `--evaluation-gate-file` for an evaluation request.

The evaluation gate names its run in `authorized_run_id`; the construction gate names it in `authorized_new_run_id`.

The derived per-item gate of the streaming evaluator is the gate of an evaluation request.

CAUTION: Release an evaluation ambiguity before the evaluator visits that item again.
A visit rewrites the derived gate file with a new `written_at_utc`, which changes its digest.
The release needs the exact gate digest the receipt recorded.

```bash
PYTHONPATH=src python -m arctic_qa --json authorize-ambiguous-continuation \
  --request-key REQUEST_SHA256 \
  --expected-ledger-sha256 LEDGER_SHA256 \
  --review-file /PRIVATE/DIRECTORY/review.md \
  --evidence-file /PRIVATE/DIRECTORY/evidence.json \
  --authorized-run-id RUN_ID --operator-id OPERATOR \
  --streaming-budget-policy-file POLICY --price-config-file PRICE_CONFIG \
  --execution-gate-file /PRIVATE/DIRECTORY/gate.json \
  --evaluation-policy-file EVALUATION_POLICY \
  --evaluation-price-config-file EVALUATION_PRICES \
  --evaluation-gate-file WORK_DIR/gates/ITEM_ID/google_gemini.json \
  --shared-ledger-file LEDGER --model-receipts-dir RECEIPTS \
  --ledger-config-transition-file TRANSITION \
  --credential-file /PRIVATE/DIRECTORY/gemini.key \
  --prior-construction-spend-usd KNOWN_VALUE
```

The command takes the exclusive broker operation lock without a wait.

A live producer holds that lock for the whole of each paid call.

So the release can need many attempts before it wins the lock.

## Usage reconciliation

Use reconciliation only for a saved response with an omitted-zero pattern: an absent `thoughtsTokenCount` or an absent `candidatesTokenCount`.

The receipt records which count the provider omitted, in `omitted_zero_usage_field`.

An evaluation request validates the evaluation gate, and a construction-only broker reads the recorded cost instead of recomputing it, because it has no evaluation price config.

The reconciliation needs a private gate for the reviewed repair commit.

It does not read the credential or call the provider.

It writes `<request-key>.usage-reconciliation.json` as a new immutable receipt.

This receipt binds the two original receipt hashes, request identity, configuration hashes, normalized usage, cost, gate, commit, and review.

The broker does not replace the submitted, received, or ambiguous receipt.

The broker updates the ledger atomically under the existing operation and ledger locks.

The update moves the exact reservation from ambiguous funds and adds the computed cost to spent funds.

The broker removes the halt only when no ambiguous or in-flight request remains.

Repeated reconciliation returns the existing result without a ledger change or provider call.

After reconciliation, `effective_receipt()` validates the ledger and every immutable event under the ledger lock.

It keeps the original ambiguous receipt unchanged.

It creates an in-memory completed view from the immutable received response and reconciled usage.

The view binds the reconciliation-event hash and both original receipt hashes.

The provider adapter uses this view only for the exact existing request key.

If the local call journal has an ambiguous entry, the adapter requires its explicit no-transport resume method.

The resume updates that journal entry only after the saved JSON passes schema validation.

A process restart does not cause another token-count or generation request.

Use this command after an independent PASS and supervisor release:

```bash
PYTHONPATH=src python -m arctic_qa --json reconcile-usage \
  --request-key REQUEST_SHA256 \
  --execution-gate-file /PRIVATE/DIRECTORY/reconciliation-gate.json \
  --shared-ledger-file $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir $ARCTIC_QA_DATA_ROOT/arctic-qa/streaming-dataset-r1/model-receipts \
  --credential-file /PRIVATE/DIRECTORY/gemini.key \
  --prior-construction-spend-usd KNOWN_VALUE
```

The command accepts `--ledger-config-transition-file` when the first configuration transition still needs application.

Use `status()` for the live page and scheduler state.

Use `record_accepted()` only after the integrated validator accepts one base question.

This operation enforces one accepted item for each paper family.

The broker does not validate scientific output.

The integrated pipeline owns response schemas and scientific validation.
