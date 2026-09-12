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

## Reviewed price configuration transition

The immutable identity record keeps the hash of the initial price configuration.

Do not replace this record or reset the ledger after a reviewed configuration change.

If the active configuration hash differs, provide `config_transition_file` when you construct the broker.

The CLI option is `--ledger-config-transition-file`.

The transition file must use the `shared-paid-call-config-transition-v1` schema.

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

A replaced event or an additional matching event causes an integrity halt.

The status record reports the initial hash, the active hash, and the transition event hash.

Each new request receipt records the active price configuration hash and its transition event hash.

An absent, altered, or unreviewed transition stops the broker before it reads the credential.

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

The broker writes an immutable response event before it settles a successful request.

After a crash, it uses that event to complete the ledger and final receipt.

If no response event exists, it treats the interrupted request as an ambiguous charge.

Use `status()` for the live page and scheduler state.

Use `record_accepted()` only after the integrated validator accepts one base question.

This operation enforces one accepted item for each paper family.

The broker does not validate scientific output.

The integrated pipeline owns response schemas and scientific validation.
