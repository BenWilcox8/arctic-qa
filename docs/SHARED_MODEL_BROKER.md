# Shared model broker

The streaming pipeline must send every paid model request through one broker.

Use `SharedGeminiBroker` from `arctic_qa.model_broker`.

Use one central ledger for all runs and stages:

`/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json`

Keep provider receipts in this private directory:

`/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/model-receipts/`

The ledger schema is `shared-paid-call-ledger-v1`.

Do not create a new ledger for a new process, run, phase, or stage.

The adjacent immutable identity record makes a missing ledger fail closed.

## Invocation

Construct the broker with the policy, price, gate, central ledger, and receipt paths.

Pass the private credential path and the known prior construction spend.

The constructor creates the first ledger and its immutable identity record.

Later constructors require both files and require the same frozen inputs.

Create the request key from the exact request identity.

Then call the broker one time.

```python
from arctic_qa.model_broker import SharedGeminiBroker, broker_request_key

request_key = broker_request_key(
    model="gemini-3.8-flash",
    run_id=run_id,
    stage="question_generation",
    paper_id=paper_id,
    family_id=family_id,
    payload=payload,
)

receipt = broker.execute(
    phase="live_test",
    run_id=run_id,
    stage="question_generation",
    paper_id=paper_id,
    family_id=family_id,
    request_key=request_key,
    payload=payload,
)
```

Valid phases are `live_test` and `away_production`.

Valid stages are defined in `arctic_qa.model_broker.STAGES`.

The broker checks the offline-review execution gate before it reads a credential.

The broker permits only text inputs and structured JSON output.

It rejects tools, provider storage, fallback models, and requests above the output limit.

It counts tokens before it reserves a paid submission.

It reserves the worst-case request cost before the generation request.

It records actual input, output, and thinking tokens by stage.

It also records spend and reservations by stage and paper.

An unknown provider outcome reserves the full amount and stops the broker.

Do not retry an ambiguous request.

Use `status()` for the live page and scheduler state.

Use `record_accepted()` only after the integrated validator accepts one base question.

This operation enforces one accepted item for each paper family.

The broker does not validate scientific output.

The integrated pipeline owns response schemas and scientific validation.
