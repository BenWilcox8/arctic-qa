# Chapter 3 count-error retry report (arctic-ch3-count-error-retry-r1)

Date: 2026-09-16.
Branch: `fm/arctic-ch3-count-error-retry-r1`, from local `main` at `6be5d26`.
Captain standing order (2026-09-16): the chapter 3 production run must keep running without stops that need a human.
Activation artifacts: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-count-error-retry-r1/`.
Predecessor activation: `/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-broker-operation-lock-wait-r1/`.

## 1. The fault

At 2026-09-16T21:11:11Z the provider answered one `countTokens` call with HTTP 503.

- Request `39fcd0dab9507099277d03530ec92614c308de17b8fa45d629d38aac9e1f1d61`.
- Phase `away_production`, stage `eligibility`, model `gemini-3.8-flash`.
- Paper `10.1038/s41467-022-33106-1`, family `family-80f56c6192973b6df870`.
- Run `chapter3-7dc6485-r3`, campaign `arctic-qa-production-campaign-003`.

The broker wrote a `count_error` receipt on that first failure, marked the request not submitted, halted the whole shared ledger with the reason `countTokens error: HTTPError` at `spent_usd` 76.302066, and the producer exited with `the broker stopped with state count_error`.

`countTokens` is free.
The broker calls it before it reserves anything, so the call reserved nothing, submitted nothing and charged nothing.
The receipt recorded `live_call_made` false, and no submitted receipt and no received receipt exist.
No money was uncertain and none moved.

The evaluation phase was not halted, and the evaluator continued.

## 2. The repair

Commit `d862342`.

**The retry.**
`execute` retries a transient count failure in place.
The transient set is HTTP 429, 500, 502, 503 and 504, a timeout, a connection fault and an answer the broker cannot read as a token count.
The retry makes `COUNT_RETRY_ATTEMPTS` (five) attempts with a jittered exponential backoff of about two minutes in total.
Every attempt is recorded in the receipt field `count_attempts`, with its start time, its end time, its error, its HTTP status and its class.
A request that needed a retry keeps that list on its submitted receipt.

**The containment.**
A transient failure that outlives the retry halts nothing.
The receipt and the ledger row record `count_failure_class` `transient`.
`broker_provider` raises `errors.CountUnavailableError`, which is a paper-level refusal at the broker seam and in `streaming._ends_the_run`.
At the eligibility seam the producer writes a `rejection_ledger` row at stage `count_tokens` with the reason code `count_tokens_unavailable` and skips to the next paper.
In the candidate path the existing candidate-fault containment holds it.

`CountUnavailableError` subclasses `ValueError`, as `BrokerOperationBusyError` does.
`providers._call_externally_metered` turns every unnamed exception into an `AmbiguousChargeError`, and a free call that charged nothing must never be recorded as an unknown charge.
`tests/test_count_error_retry.py` pins that.

**The halt that stays.**
A permanent count failure keeps today's halt.
The permanent set is HTTP 400, 401, 403 and 404, and every status and error the broker cannot prove transient.
Such a failure means the request or the credential is wrong, and it repeats until one of them changes.

**The retry round.**
A request whose free count failed transiently counts again under its own round.
The first count-error receipt stays immutable under the request key and binds the chain through `count_retry_from_sha256`; round `n` writes its receipts under the stem `<request key>.count-retry-<n>`.
`_validate_count_retry_binding` checks that chain with every other immutable event, so the retry provably replaced no paid call and settled no money.
Without it the recovery could not work: a relaunched producer re-asks the same paper under the same request key, and a terminal `count_error` row refuses that key with `the paid request key already exists`.

**The reviewed continuation.**
`authorize-count-error-continuation` was pinned to one past incident: the countTokens 404 of 2026-09-15 on the answer judge, with its exact evidence, stage and model.
It could not clear this 503.
It keeps that exact answer-judge evidence, because that review named a replacement model rather than a retry.
Every other count error is now reviewed through `shared-paid-call-count-error-continuation-evidence-v1`, which names the request, the exact error, the class, the family and the run, and says whether the free count may run again.
Only a transient failure may.

`docs/SHARED_MODEL_BROKER.md` holds the contract, in "The free token count and its errors", "Count-retry rounds" and "Reviewed count-error continuation".

## 3. The release

The count error of request `39fcd0da...` was released at 2026-09-16T21:32:34Z.

- Review `count-error-continuation-review-39fcd0da.md`, sha256 `be052fcf0a69dbff8c0fa1835035f70db05b1c02380062c52fe62dfce8d40489`.
- Evidence `count-error-continuation-evidence-39fcd0da.json`, sha256 `96efdf72959ecf32cd8434919188849ca6ed0f798463f321307532db656328f0`.
- Continuation record `39fcd0da....count-error-continuation.json`, sha256 `7f72c117c9d8c652e25d52e08ae612689e65214cf5984b00737d36593dea04ba`.
- Ledger before `080c47b66c8616aea05507e433632108b1d95efe98516e1bf082da151c031c38`, after `79eccfe967c63bb222d12abfd8ccd14c9302894c9b5644f2745c366d3356a249`.

`spent_usd` was 76.302066 before the release and 76.302066 after it.
No paid call was replayed, retried or settled, and none exists to replay.
The ledger halt is lifted and the row carries `count_failure_class` `transient`, so the next visit of that family counts the request again.

The release ran from the task worktree under the gate the request records, and took the exclusive operation lock for one moment only.
