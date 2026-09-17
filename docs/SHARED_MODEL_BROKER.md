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

The broker publishes an adjacent `shared-gemini-broker-status-v2` record with each snapshot of the ledger: with every commit of a broker that runs one operation at a time, and every `COMPACTION_INTERVAL_SECONDS` for a concurrent one (see "Parallel bookkeeping").

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

Every section prints its wait and its hold on stderr when either passes
`OPERATION_LOCK_LOG_THRESHOLD_SECONDS`, which is one second by default and is
set for a run by `ARCTIC_QA_OPERATION_LOCK_LOG_SECONDS`. A measured window sets
it to 0, because the serialised cost of one paid call is the sum of the holds.

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

### Concurrent requests

Every request serialises its admission and then releases it before the live call.

The admission is the orphan recovery, the gate check, the pace, the count and the reservation.
One in-process admission lock holds it, so the ledger sees one admission at a time.

After the reservation the request takes its own in-flight lock, an `flock` on a per-request file, and releases the admission.
Orphan recovery skips a submitted request whose in-flight lock is held, so a live sibling is never settled twice.

The evaluation phase always works this way.
A construction run opts in with `concurrent_construction`, which the `stream` command sets when `--paper-workers` or `--option-workers` is above one.

A construction request also holds the exclusive operation lock through its admission, because a reviewed operation of the ledger must not overlap the accounting of a paid request.
A concurrent construction request releases that lock with its admission.
Without `concurrent_construction` the lock is held for the whole call, as before.

The policy still bounds the rate: `maximum_concurrent_generation_requests` and `maximum_generation_requests_per_minute`.
The two are one registered pair (`ALLOWED_REQUEST_RATES`), so a transition can never raise one of them alone.
`CHAPTER3_CONCURRENCY_CHANGE` is the registered move from 2 and 10 to 8 and 40; it moves no money and keeps the expansion tranche.
`CHAPTER3_PARALLEL_CHANGE`, `CHAPTER3_SCALE_CHANGE` and `CHAPTER3_NIGHT_CHANGE` are the moves that follow it, to 16 and 100, to 50 and 300, and to 75 and 450.
None of them moves money either, so each names the construction ceiling that is already authorized as its cumulative tranche.
That ceiling is not a constant: the USD 600 allocation moved it on 2026-09-17, so a rate transition applied before that date names the expansion tranche and one applied after it names the six-hundred tranche.
Every earlier pair stays registered, so a fall back to a lower rate needs no transition.

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

## Parallel bookkeeping

The ledger keeps every guarantee it had.
The cost of one paid call no longer grows with the history behind it.

### What it replaces

Until 2026-09-17 the ledger was one JSON file.
Each operation read the whole file, proved every row of it, changed one thing, and wrote the whole file again.
At 6,036 rows and 8.2 MB that cost about 3 seconds of serialised work for each paid call.

`research/arctic-ledger-parallel-r1/report.md` holds the measurement.
The three costs, in order, were the durable writes on a rotating data disk, the directory-wide scans of the receipts directory, and the whole-file parse with the whole-ledger proof.

The number of calls in flight settles at the call length over the admission length.
A call takes 8 seconds, so 16 calls in flight need an admission of half a second.
That is why this section exists.

The old shape is kept as history in "The one-file ledger, until 2026-09-17" below.

### The store

The store has three files beside each other.

| file | what it is |
| --- | --- |
| `shared-paid-call-ledger.json` | the compacted **snapshot** |
| `.shared-paid-call-ledger.json.journal` | the append-only **journal** |
| `.shared-paid-call-ledger.json.journal.base.json` | the **base record**, which binds the two |

The state of the ledger is the snapshot with every journal record of a higher sequence number applied, in order.

A journal record is one line:

```json
{"schema":"shared-paid-call-ledger-journal-v1","seq":412,"at":"2026-09-17T04:10:02Z","delta":{...}}
```

A delta names only what the mutation changed.
It is an **absolute assignment**, never an increment: `{"requests": {"set": {"<key>": {...}}}}` gives the new row, and `{"removed": ["<key>"]}` says a key is gone.
Applying the same record twice therefore changes nothing, and a replay from any earlier point reaches the same state.
That one property is what makes every stop safe, at any moment, in any order.

### The base record

The base record says which journal records the snapshot already holds, and binds the snapshot by its hash.

```json
{"schema":"shared-paid-call-ledger-journal-base-v1",
 "snapshot_sha256":"<hash of the snapshot>",
 "applied_seq":400,
 "supersedes":{"snapshot_sha256":"<hash of the one before>","applied_seq":250}}
```

A snapshot whose hash matches neither record was changed outside the store.
That is an integrity failure and it fails closed, because replaying the journal over a hand-edited snapshot would quietly repair a field no record names.

The base record is written **before** the snapshot it names, so a stop between the two writes leaves the snapshot it supersedes, which is bound too.

### One paid call

| step | what it costs |
| --- | --- |
| read the ledger | the records another process appended since the last read |
| prove the ledger | the rows that moved, and the size of the stage, paper and binding maps |
| commit | one appended line |
| make it durable | one `fsync`, shared with every call that appended before it |

Nothing in that list grows with the number of rows in the ledger.

### Group commit, and where the flush is

`_ledger_lock` holds the shared ledger lock for the append and releases it **before** the flush.

An appended record is visible to every reader of the journal the moment it is written.
What the flush adds is durability against power loss, and the money rule needs that before the provider call, not before the lock is released.
Outside the lock, one thread's `fsync` covers every record its peers appended.
A wave of sixteen concurrent calls pays one flush, not sixteen.

A durable write on this data disk, a USB rotating disk, costs 150 to 470 ms whatever its size.
Group commit is the whole answer to that number, and it is why the journal lives beside the ledger and not on another device: one file, one flush, one filesystem, and the same durability the ledger always had.

### The proof, and where the full pass runs

`_validate_ledger` is now three parts.

- `_validate_ledger_shape` checks the fields of the ledger.
- `_row_contribution` proves one request row and returns what it adds to the totals.
- `_compare_ledger` compares the summed contributions with what the ledger stores.

`_prove_ledger` sums every row. It is the full pass and it keeps no state, so the compactor thread runs it while the hot path runs its own.
`_validate_ledger_delta` subtracts the contribution of each row that moved, proves the row again and adds it back.
Everything `_compare_ledger` compares is compared on every read and every commit; only the summing is incremental.

### The immutable-event proof, and what one read of it costs

The proof against the receipts on disk sits on top of the money proof, and it
used to cost the whole history on every read.
Measured on the live ledger on 2026-09-17 at 8,230 request rows and 32,251
receipt files: one warm read cost 371 ms, and a paid call makes five to seven
of them.
That is what held the exclusive operation lock a mean 2.4 s per paid call and
held the run at about 21 requests a minute whatever the thread count was.

Four rules now keep a warm read at about 12 ms.

- The receipts directory is listed once. A broker that shares the ledger with
  another live writer re-lists it at most every
  `RECEIPT_LISTING_REFRESH_SECONDS`, because the directory moves on every paid
  call of every worker and the fingerprint alone made a 32,251-entry `scandir`
  part of nearly every read. A sequential broker, which is every reviewed
  operation, keeps the exact fingerprint.
- Everything derived from one listing is derived once (`_listing_derived`):
  the name filters and the request key of every paid-call receipt. Both are
  safe to read from a listing a few seconds old, because a receipt this process
  wrote belongs to a row it has already registered.
- The accepted-item proof is **not** one of them. The receipt of an accepted
  family and its ledger row move together, so a listing a few seconds old does
  not hold the receipt the row names; caching that answer against the listing
  raised a false integrity halt at 07:40:51 UTC on 2026-09-17 and stopped the
  chapter 3 producer. It is derived, against a listing taken again, whenever
  the ledger's accepted map differs from the map that was proved, which is a
  few times an hour.
- No pattern walk of the directory is on the call path. A glob for one usage
  reconciliation receipt cost 44 ms of every read that proved it.
- A row is proved again only when the store reports it moved, which is the
  same tracking the money proof of the delta already trusts. Where that report
  is absent, which is a reload of the snapshot, a reviewed repair or a full
  pass, every row is checked by its signature as before.

A full pass over every row and every receipt still runs every
`IMMUTABLE_EVENT_REVALIDATION_SECONDS`, and it re-lists the directory first.

`concurrent_requests` is the flag that says another live writer shares this
ledger, and it is a parameter of the broker, never an attribute a caller sets
afterwards, because the start of the broker reads it.
A concurrent construction run is such a writer by definition, so
`concurrent_construction` still implies it.
The streaming evaluator is the other one and sets it itself.

The evaluator did not say so until 2026-09-17.
It holds no construction request, so the flag it hung on said nothing about it,
and it kept the exact fingerprint of a directory that the producer moved on
every paid call of every one of its 75 workers.
Every ledger read of the evaluator was therefore a full listing of 52,716
entries and a re-derivation of everything read out of it: 0.25 s a read against
0.026 s with the listing kept, measured on the live ledger at 13,309 rows.
Three exclusive sections of one paid Gemini call held the operation lock about
22 s in total, which held the Gemini arm at 13.7 questions an hour.

### One process starts one ledger once

A broker start compacts a snapshot that lags its journal, so a reviewed
authorization always binds the state and never a stale file.
The start also materializes the store, proves every row of it against the
receipts, and publishes the status record.
All of that runs under the shared ledger lock.

The streaming evaluator builds one broker per question, because the derived
evaluation gate belongs to the question.
So the start ran once a question: a 60 MB materialization, a proof of every
row and an 18 MB durable write, 1.16 s measured on an idle machine, under the
lock that each of the producer's 75 workers takes for every paid call.

A broker of a ledger that this process already started, and that says it
shares the ledger, skips the compaction of the start.
The compactor thread of that first start keeps the snapshot current within
`COMPACTION_INTERVAL_SECONDS`.
A reviewed operation is its own process, so it always compacts.
The start also reads the store once instead of materializing the same two
files a second time for the identity check.

### A mutation that was never committed

`LedgerStore.rollback` puts the keys a refused reservation touched back to
their committed values, in place.
A tracked container rolls **itself** back and stays the same object.
Replacing it with a plain copy of what it held untracked it: the ledger root
does not wrap its rows, so the restored map came back an ordinary `dict`, every
later change to it went unreported, the delta proved totals it had not been
told about, and the read raised, which writes an integrity halt on the shared
ledger and stops every caller of it.
A refused reservation is what rolls back, and the concurrency slots refuse one
often once the threads outnumber them.
Found at fifty threads on 2026-09-17; `tests/test_ledger_store.py` is the guard.

For the same reason a parent keeps a reference to a tracked child and never a
copy of it: copying the whole requests map on the first touch of every commit
cost 90 ms a commit at 8,631 rows, which is more than the commit it was there
to undo.

### The compactor

One background thread per ledger per process rewrites the snapshot, every `COMPACTION_INTERVAL_SECONDS`.
The evaluator's broker per question left a thread, a whole copy of the ledger and an `atexit` compaction behind for every question it scored, so the owner of the thread is now recorded per ledger and a later broker of the same ledger starts none of its own.
It never takes the shared ledger lock, so it never delays a paid call.
It materializes the store from the snapshot and the journal, runs the **full row-by-row proof**, writes the snapshot and the base record under the compaction lock, and publishes the status record.

A broker that runs one operation at a time, which is every reviewed command and every test, writes the snapshot and the status with the commit instead (`deferred_snapshot`).
The snapshot is then exact for anything that reads the plain file.

The compaction lock, `.shared-paid-call-ledger.json.compaction.lock`, orders compactors against each other and against nothing else.
A compactor that does not get it does nothing and tries again later.

### Every reader

Every reader in this repository reads the store, never `json.load` of the ledger file:

- `ledger_store.read_ledger(path)` materializes the snapshot and the journal,
- `ledger_store.apply_journal(path, snapshot)` finishes a snapshot a reader already read under the shared lock.

The migrated readers are `live_papers.read_shared_ledger` (the website), `benchmark_guard.read_ledger` (the cost guard, and through it the streaming evaluator), `abstention_cost.read_ledger` (the evaluator's cost summary), `corpus_viewer` (whose cache is keyed on the journal as well as the snapshot), `concurrency_repair`, `gemini_batch` (both reads) and `activate_exclusive_batch_mode`.
A reader that still reads the plain file, such as a `status.sh` of an older activation, sees the ledger as of the last compaction: at most `COMPACTION_INTERVAL_SECONDS` old on a concurrent run, and current after a process exits, because a process compacts once more at exit.
A process killed by a signal writes no such snapshot; the activation's `stop` compacts for it.

### What a reviewed operation binds

`expected_ledger_sha256` in a transition or a release binds the snapshot file.
Every broker start compacts a snapshot that lags its journal before it validates anything, and waits for the compaction lock rather than skip, so the file a reviewed command hashes is the state at that start.
Compute the hash after the writers are stopped, as the activation scripts do.

### What a stop leaves

A stop at any instant leaves a store that reconstructs.
A torn last line of the journal is not a record; the next process to append cuts it first, under the shared ledger lock.
A base record written before its snapshot names the snapshot it supersedes, which is still bound.
A failed `fsync` marks nothing durable, and the waiters run their own.

### What did not change

- Receipts are per-request immutable files, exactly as before.
- A reservation is durable before the provider call.
- The reservation, the settlement and the exact totals are unchanged.
- Per-phase halts, in-flight slots and the minute window are unchanged.
- The reviewed configuration and policy transitions and their hashes are unchanged.
- The identity record, the integrity halt and the immutable-event proof are unchanged.
- Ambiguous charges, orphan recovery and the completion labels are unchanged.

### The migration

`arctic-qa migrate-ledger-store --shared-ledger-file <path> --apply` converts a one-file ledger into the store; without `--apply` it proves and writes nothing.

It runs with the producer and the evaluator stopped, at a settled boundary.
It copies the ledger to `<name>.pre-store-archive-<utc>.json` (frozen, mode 0444), writes the base record and the empty journal, materializes the store again and proves that every field of the result is identical to the archive, byte for byte after canonical serialization.
`--action check [--archive-file <path>]` proves an existing store against its archive without writing anything.
A broker started on a ledger with no store beside it refuses and names this command.
The live ledger was converted at 04:53 UTC on 2026-09-17: 6,406 rows, 8 seconds, identical.

### The one-file ledger, until 2026-09-17

Until 2026-09-17 the ledger was one JSON file and nothing else.
Every read parsed it whole and proved every row; every commit proved every row again and rewrote the file; the status record was written with every commit.
`_ledger_evidence_fingerprint` named the state by the hash of that file.

The shape was correct and it was simple, and it held the run from the first paid call to 6,036 rows.
It ended because the work of one call grew with the history, and 16 calls in flight need a call's bookkeeping to be its own.

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

## The phase of a refused request

Every request records its `phase` from its first ledger record, not from its reservation.

The phase matters after a reviewed configuration transition is applied.
Such a transition is validated again on every broker start until its first construction request, and `_only_evaluation_activity_since` is the one exception to that check: an evaluation request may land in between, a construction request may not.
That reader takes a request without a `phase` for a construction request.

Before this rule, a refusal that stopped before the reservation carried no phase.
One such refusal of the evaluation phase, at 2026-09-16T23:03:53Z, made every later broker start refuse the applied transition and write an integrity halt.
Both the chapter 3 producer and the streaming evaluator then could not start.

The reviewed `settle-phaseless-refusal` command repairs one such row.
It is pinned in code to that one request key, in `PHASELESS_REFUSAL_SETTLEMENT_REQUEST`, and refuses any other.
It moves no money: it asserts that the row has no `submitted_at_utc`, no `usage`, no reservation and no cost, because a `not_submitted` row is a refusal recorded before the provider was called.
It reads the phase from the row's own evidence, which must all be present: the stage prefix `evaluation_answer:`, the evaluation trial, the evaluation gate hash and the evaluation policy hash.
It writes `<request-key>.phase-settlement.json` and the one field `phase`, and nothing else of the row changes.

Every broker start refuses while an integrity halt record is on disk, so the command supersedes that record as part of the one operation.
It accepts only a halt whose reason is `ValueError: the configuration transition ledger hash changed`, it renames the record instead of erasing it, it puts the record back when the settlement does not apply, and the settlement receipt binds the renamed record by hash.

```bash
PYTHONPATH=src python -m arctic_qa --json settle-phaseless-refusal \
  --request-key REQUEST_KEY \
  --expected-ledger-sha256 LEDGER_SHA256 \
  --review-file /PRIVATE/DIRECTORY/review.md \
  --streaming-budget-policy-file POLICY --price-config-file PRICE_CONFIG \
  --execution-gate-file /PRIVATE/DIRECTORY/gate.json \
  --shared-ledger-file LEDGER --model-receipts-dir RECEIPTS \
  --ledger-config-transition-file TRANSITION \
  --credential-file /PRIVATE/DIRECTORY/gemini.key \
  --prior-construction-spend-usd KNOWN_VALUE
```

Give the construction files of the pair the ledger already holds requests under.
A broker built on a pair with no request yet runs the whole transition validation again, which is the check this repair exists to satisfy.

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
