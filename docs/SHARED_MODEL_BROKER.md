# Shared model broker

The streaming pipeline must send every paid model request through one broker.

Use `SharedGeminiBroker` from `arctic_qa.model_broker`.

Use one central ledger for all runs and stages:

`/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json`

Keep provider receipts in this private directory:

`/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts/`

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

The ledger must have no halt, inflight request, reservation, or ambiguous charge when the broker first applies the transition.

It also validates the private gate and the exact independent review record.

The broker writes one immutable `config-transition-*.json` event in the receipt directory.

It does not change the existing ledger, identity record, request receipts, spend, or submission counts during this operation.

Later starts can use the immutable event without the original transition file.

Before the first transitioned request, each restart validates the embedded ledger snapshot, gate, and review record again.

Each transitioned request binds its ledger record and immutable receipts to the exact transition event hash.

Later restarts require this binding before they accept a historical transition.

Before each request, the broker validates the authorized gate and review bindings again.

It validates the gate hash, commit, review path, review hash, and review file content.

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

Valid phases are `live_test` and `away_production`.

Valid stages are defined in `arctic_qa.model_broker.STAGES`.

The broker checks the offline-review execution gate before it reads a credential.

The broker binds each family to one paper ID and one source-version ID.

Use a stable source-version ID from the versioned full-text artifact.

Do not derive this ID from a run name or a display title.

The broker permits only text inputs and structured JSON output.

The approved Gemini configuration requires low thinking.

The broker does not increase the fixed output cap to make room for thinking.

It rejects tools, provider storage, fallback models, and requests above the output limit.

It counts tokens before it reserves a paid submission.

It reserves the worst-case request cost before the generation request.

It records actual input, output, and thinking tokens by stage.

It also records spend and reservations by stage and paper.

An unknown provider outcome reserves the full amount and stops the broker.

Do not retry an ambiguous request.

The provider can omit `thoughtsTokenCount` when its value is zero.

The broker uses zero only when the other three token counts are nonnegative integers.

The total must equal the sum of the prompt and candidate counts.

All other missing or inconsistent usage values cause an ambiguous charge.

The broker writes an immutable response event before it settles a successful request.

After a crash, it uses that event to complete the ledger and final receipt.

If no response event exists, it treats the interrupted request as an ambiguous charge.

## Usage reconciliation

Use reconciliation only for a saved response with the exact omitted-zero pattern.

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
  --shared-ledger-file /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  --model-receipts-dir /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts \
  --credential-file /PRIVATE/DIRECTORY/gemini.key \
  --prior-construction-spend-usd KNOWN_VALUE
```

The command accepts `--ledger-config-transition-file` when the first configuration transition still needs application.

Use `status()` for the live page and scheduler state.

Use `record_accepted()` only after the integrated validator accepts one base question.

This operation enforces one accepted item for each paper family.

The broker does not validate scientific output.

The integrated pipeline owns response schemas and scientific validation.
