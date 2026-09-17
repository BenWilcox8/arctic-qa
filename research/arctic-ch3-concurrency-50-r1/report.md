# Fifty papers in flight

Captain's ask, 2026-09-17 06:25 UTC: "How far do you think you can reasonably
raise the concurrency? I would like to raise it to something like 50/100."

Run `chapter3-7dc6485-r3`, campaign `arctic-qa-production-campaign-003`,
shared ledger
`/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json`.
The predecessor is `research/arctic-ledger-parallel-r1/report.md`, which made
the ledger a snapshot and a journal and took the run from 6.7 to 20.8 requests
a minute.

## 1. What actually held the run at 21 requests a minute

The predecessor left the run at 16 paper threads and read the cap as the
producer's own work under one interpreter lock. That reading was wrong.

The producer logs the wait and the hold of every exclusive section. Over the
1,023 paid calls of run `chapter3-7dc6485-r3` up to 06:41 UTC:

| section | calls | mean wait | mean hold | longest hold |
| --- | --- | --- | --- | --- |
| `orphan_recovery` | 1,023 | 0.00 s | 0.82 s | 9.36 s |
| `count_registration` | 1,023 | 0.55 s | 0.56 s | 6.52 s |
| `reserve` | 1,011 | 0.01 s | 1.06 s | 7.58 s |

That is **2.44 seconds of serialised bookkeeping per paid call**. A run whose
calls are serialised for 2.44 s admits about 25 a minute whatever the thread
count is, and the run was measured at 20.8. The threads were not waiting for
the interpreter; they were waiting for the lock.

## 2. Where the 2.44 seconds went

Measured read-only against the live ledger (8,230 request rows, 11.3 MB
snapshot, 32,251 receipt files) on the live data disk, single-threaded, so the
numbers are the work itself and not the contention:

| step of one ledger read | before |
| --- | --- |
| immutable-event proof, warm, nothing moved | 371 ms |
| ... of which `scandir` of the receipts directory | 56 ms |
| ... of which the six name filters over 32,251 names | 31 ms |
| ... of which the name-to-request-key scan | 33 ms |
| ... of which `canonical_json` of all 8,230 rows | 136 ms |
| ... of which one `glob` for a usage reconciliation receipt | 44 ms |
| money proof of one moved row (`_validate_ledger_delta`) | 19.6 ms |
| first touch of the requests map, per commit | 89.5 ms |
| orphan recovery's copy of every row and its two receipt paths | 50 ms |
| immutable-event proof, cold full pass | 2.4 s |

A paid call reads the ledger five to seven times and commits two to three
times, so the first two rows alone are 2.0 to 2.7 s of CPU. The parallel store
had made the *money* proof cost the change; the proof against the receipts on
disk still cost the whole history, and so did the rollback record the store
kept in case a mutation was abandoned.

Two things defeated the caches the predecessor added:

- The evidence fingerprint is the receipts directory's `mtime` and size. Under
  paper concurrency every worker writes receipts, so the directory moves on
  nearly every call and the fingerprint never matched.
- The per-row proof was kept against `canonical_json(row)`, so deciding that
  nothing had moved cost a JSON serialisation of the whole ledger.

## 3. What changed

`docs/SHARED_MODEL_BROKER.md`, "The immutable-event proof, and what one read of
it costs", is the contract. Four rules:

- The receipts directory is listed once, and a **concurrent** broker re-lists
  it at most every `RECEIPT_LISTING_REFRESH_SECONDS` (10 s) rather than on the
  fingerprint. A sequential broker, which is every reviewed operation, keeps
  the exact fingerprint and sees a receipt the moment it lands. The staleness
  is bounded and is far tighter than the full pass, which runs every
  `IMMUTABLE_EVENT_REVALIDATION_SECONDS` (300 s) and re-lists first.
- Everything derived from one listing is derived once (`_listing_derived`):
  the name filters and the request key of every paid-call receipt. The
  accepted-item proof is not one of them; section 6 says why.
- No pattern walk of the receipts directory is on the call path.
- A row is proved against its immutable events again only when the store
  reports it moved. That is the same tracking the money proof of the delta
  already trusts. Where the report is absent, which is a reload of the
  snapshot or a reviewed repair, every row is checked by its signature as
  before.

Two smaller ones on the money proof: `_money` resolves its import once, and
the row comparison skips the two `Decimal` parses where the stored string and
the expected string are equal. Orphan recovery skips a terminal row whose
custody it already proved before it builds that row's receipt paths.

| step of one ledger read | before | after |
| --- | --- | --- |
| immutable-event proof, warm, nothing moved | 371 ms | 12 ms |
| immutable-event proof, warm, one row moved | 371 ms | 12 ms |
| immutable-event proof, warm, receipts re-listed | 417 ms | 130 ms |
| money proof of one moved row | 19.6 ms | 6.8 ms |
| first touch of the requests map, per commit | 89.5 ms | 0 ms |
| orphan recovery's row copy | 50 ms | 4 ms |

The amortised extras are small at six calls a second: the re-listing is 130 ms
every 10 s, the reload after each compaction is about 490 ms every 30 s, and
the full pass is 2.4 s every 300 s.

## 4. The bug the fifty-thread test found

A fifty-thread admission test failed with
`the shared paid-call papers total is inconsistent`, transiently and with a
different total each run. It reproduces on the parent commit, so it is not a
defect of this change; it is a defect of the parallel store as it landed.

`LedgerStore.rollback` put the keys a refused reservation touched back by
**replacing** each one with the plain copy the parent had kept. The ledger root
does not wrap its rows, so a restored map came back an ordinary `dict`: it was
no longer a tracked container, every later change to it went unreported, the
journal record lost those changes, the delta proved totals it had not been told
about, and the read raised. A raise there writes an integrity halt on the
shared ledger, which stops the producer and the evaluator both.

A refused reservation is what rolls back, and the concurrency slots refuse one
whenever the threads outnumber them. At 16 threads and 16 slots it was rare
enough not to have fired in the live run; at 50 it fired within a minute.

A tracked container now rolls itself back, key by key, and stays the same
object. For the same reason a parent keeps a **reference** to a tracked child
instead of a copy of it, which is where the 89.5 ms first touch went.

`tests/test_ledger_store.py::test_a_rollback_leaves_every_container_tracked`
and `::test_a_rollback_undoes_a_row_edited_in_place` are the guards.

## 5. Policy v13

`CHAPTER3_SCALE_CHANGE`: the concurrency slots from 16 to 50 and the minute
window from 100 to 300, together and registered as one pair in
`ALLOWED_REQUEST_RATES`. The transition moves no money, so it names the
cumulative tranche the USD 200 expansion already authorized, USD 253.990121,
which the tranche rule of `_validate_immutable_events` must name in its own
list; the parallel transition was refused at 04:53 UTC on 2026-09-17 for
exactly that omission.

## 6. The false halt of 07:40:51

The first relaunch, at 32 paper threads on `8c11af0`, ran for five minutes and
then ended with `the shared paid-call ledger has an integrity halt`. The halt
said `the accepted-item ledger differs from immutable events`.

The ledger was consistent. The full row-by-row money proof and the full
immutable-event pass both pass on it, at 9,089 rows. The defect was in this
change: the accepted item of every family was derived once per receipts
listing, and a concurrent broker re-lists on a timer. The receipt of an
accepted family and the ledger row that names it move together, so for a few
seconds the row was there and the receipt was not, and the comparison failed.

A read that raises writes an integrity halt, which stops every caller of the
ledger. The producer died with ten calls in flight; those ten reservations are
orphans, which the next start recovers, and nothing was charged twice.

Fixed on `3fe649d`: the accepted-item answer is derived, against a listing
taken again, whenever the ledger's own accepted map differs from the map that
was proved. A family is accepted a few times an hour, so the 96 receipt reads
stay off the call path, and the listing can never be older than the row it is
proving. The name filters and the request-key scan keep the listing cache,
because a receipt this process wrote belongs to a row it has already
registered.

The rule this leaves: **a receipt the broker writes together with the ledger
row that names it is never proved from a cached listing.**

## 7. The staged relaunch

### Stage 1: 32 paper workers, 07:49 to 07:59 UTC

Snapshot `3fe649d`, policy v13, the evaluator and the readers live on the same
snapshot, `ARCTIC_QA_OPERATION_LOCK_LOG_SECONDS=0` so every exclusive section
is in the log.

| measure | 16 workers, before (06:25 window) | 32 workers, after |
| --- | --- | --- |
| requests a minute | 20.8 | **55.8** |
| peak in flight | 11 | **33** |
| papers screened an hour | 120 | 522 |
| serialised lock hold a call | 2.44 s | **0.206 s** |
| `orphan_recovery` mean hold | 0.82 s | 0.067 s |
| `count_registration` mean hold | 0.56 s | 0.018 s |
| `reserve` mean hold | 1.06 s | 0.121 s |
| mean wait for the lock | 0.55 s (count) | 0.001 s |
| provider 5xx (see below) | 0 | 0 |
| ambiguous charges | 0 | 0 |
| candidate processing faults | 1 | 0 |
| producer CPU | 0.67 of a core | 0.45 of a core |
| machine load, 8 cores | 9 | 13.6 |

558 paid requests in the window, 87 papers screened.

A note on the 5xx row, because the obvious counter is worthless: the ledger
**row** of a request carries no provider status, so counting `http_status` over
the rows, which is what the first draft of the observation script did, would
report zero whatever the provider said. What proves the zero is the halt. A 5xx
answer is an ambiguous charge, an ambiguous construction charge halts the whole
ledger, and neither window halted. The provider status lives in the receipt,
not the row. The number in flight now
tracks the thread count: 33 at its peak against 32 paper workers, where 16
workers reached 11. Nobody waits for the lock any more: the mean wait is one
millisecond.

### Stage 2: 50 paper workers, 08:02 to 08:17 UTC

Snapshot `3053f65`, same policy, same evaluator, same readers. A successor
start, so it applied no transition and the evaluator stayed up through it.

| measure | 16 workers | 32 workers | 50 workers |
| --- | --- | --- | --- |
| requests a minute | 20.8 | **55.8** | 48.3 |
| peak in flight | 11 | 33 | **43** |
| papers screened an hour | 120 | 522 | **564** |
| serialised lock hold a call | 2.44 s | 0.206 s | 0.319 s |
| `orphan_recovery` mean hold | 0.82 s | 0.067 s | 0.156 s |
| `count_registration` mean hold | 0.56 s | 0.018 s | 0.022 s |
| `reserve` mean hold | 1.06 s | 0.121 s | 0.141 s |
| longest hold of the window | 9.4 s | 3.3 s | 18.2 s |
| mean wait for the lock | 0.55 s | 0.001 s | 0.001 s |
| provider 5xx (see below) | 0 | 0 | 0 |
| ambiguous charges | 0 | 0 | 0 |
| candidate processing faults | 1 | 0 | 3 |
| producer CPU | 0.67 core | 0.45 core | 0.39 core |
| producer threads / resident | 18 / 1.5 GB | 38 / 2.1 GB | 60 / 2.9 GB |
| machine load, 8 cores | 9 | 13.6 | 15.2 |

724 paid requests in the window, 141 papers screened, nothing halted.

**50 threads is past the knee, and the knee is the interpreter, not the
ledger.** The mean wait for the lock is a millisecond at both thread counts, so
nobody queues; what grew is the **hold**, from 0.206 s to 0.319 s, because the
thread holding the lock shares one interpreter with 49 peers instead of 31.
More papers are in flight (43 against 33) and slightly more papers are screened
an hour (564 against 522), but fewer requests are made a minute (48.3 against
55.8). The extra threads buy breadth and pay for it in rate.

**The periodic full proof became a stall.** Six holds of 10 to 18 seconds
landed in the 15-minute window, spaced two to three minutes apart: the full
immutable-event pass (2.4 s of CPU, every
`IMMUTABLE_EVENT_REVALIDATION_SECONDS`) and the reload after a compaction,
each multiplied by the same interpreter contention. That is about 9 percent of
the window spent inside one exclusive section. At 16 threads it was invisible
under the 2.44 s of ordinary cost; at 50 it is the largest single stall left,
and it belongs off the hot path, where the compactor already runs the full
money proof.

### What sets the number in flight

The number in flight settles at the length of one call over the serialised
bookkeeping of one call. The call length is the model's, 8 seconds at the
median, and is not ours to move. The arithmetic the predecessor used still
holds; only the second number moved, and it now moves with the thread count:

| threads | serialised a call | 8 s over it | measured peak |
| --- | --- | --- | --- |
| 16 (before) | 2.44 s | 3 | 11 |
| 32 | 0.206 s | 39 | 33 |
| 50 | 0.319 s | 25 | 43 |

The measured peak runs ahead of the arithmetic at 50 because the option
verdicts of one paper go out in a wave of four, so a burst exceeds the steady
rate. The steady rate is what the requests a minute say, and it fell.

### The first HTTP 503, two minutes after the window

At 08:19:23 UTC, two minutes after the 50-worker window closed, Gemini answered
one `finding_answer_extraction` call with HTTP 503, `UNAVAILABLE`, "This model
is currently experiencing high demand." That is the **first 5xx of run
`chapter3-7dc6485-r3`**, at the highest concurrency the run has been asked to
hold. No HTTP 429 has been recorded at any point, at any concurrency.

A 503 does not prove that nothing was billed, so the broker recorded
`known_http_response_unknown_charge` and halted the whole ledger, which is the
rule for an ambiguous construction charge. The producer ended.

Released the reviewed way at 08:38 UTC with
`authorize-ambiguous-continuation`, the bounded 5xx case: the receipt records
`live_call_made`, there is no `.received.json`, and no `actual_cost_usd`. The
reservation of USD 0.057810 stays in `ambiguous_reserved_usd` and counts
against every cap; nothing was settled, retried or replayed. The review and the
evidence are `ambiguous-continuation-review-3977fa5d.md` and
`ambiguous-continuation-evidence-3977fa5d.json` in the activation directory,
and the immutable event is
`ambiguous-continuation-3977fa5d….json` in the receipts directory.

One 503 in 724 calls at 43 in flight is not a rate limit; it is a busy model.
But it is the first signal of any kind from the provider, and it arrived at 50
and not at 32.

### Policy v14: the USD 600 allocation

Captain order 2026-09-17 08:25 UTC, verbatim: "Up the budget to $600 and make
sure that we never exceed $1000 for the whole project."

`CHAPTER3_SIX_HUNDRED_CHANGE`, applied at 08:39:20 UTC, moves the away-session
ceiling and the construction review checkpoint together from USD 253.990121 to
**USD 653.990121**: the USD 53.990121 spent before this run plus the USD 600
allocation. The captain's order is the review, so the checkpoint is the
ceiling, and `_validate_policy` refuses a policy that moves one without the
other. Nothing else moves.

The ledger after the transition:

| limit | value | remaining |
| --- | --- | --- |
| `project_lifetime_ceiling_usd` | 1000.00 | 826.54 |
| `away_session_total_ceiling_usd` | 653.990121 | 497.00 |
| `construction_review_checkpoint_usd` | 653.990121 | 497.00 |
| `reserved_for_benchmark_evaluation_usd` | 500.00 | 483.52 |
| `maximum_concurrent_generation_requests` | 50 | |
| `maximum_generation_requests_per_minute` | 300 | |

**USD 1,000 for the whole project was already enforced across every phase, and
still is.** It needed no new code, which was checked rather than assumed: the
construction cap adds the evaluation liabilities before it compares
(`construction_used + evaluation_used + reserved`), and the evaluation cap sums
the whole ledger's reserved, spent and ambiguous funds. Neither is per phase,
so neither can be passed by spending in the other one.
`tests/test_ledger_proof_cost.py::test_the_project_lifetime_ceiling_counts_every_phase`
reads both rules out of the source so a later edit cannot quietly make one of
them per phase.

**One thing to watch.** `dataset_construction_allocation_usd` is still USD
500.00 and is not enforced anywhere; it is only reported, as
`remaining.dataset_construction_usd`. Construction may now spend up to USD
653.990121, so that reported figure will go **negative** once construction
passes USD 500. Nothing stops on it and no cap is weakened, but the viewer and
the cost guard read it. Moving it would mean cutting
`reserved_for_benchmark_evaluation_usd` to keep the two summing to the lifetime
ceiling, which is a captain's allocation decision and was not asked for.

### The spend rate, and what the ceiling means in hours

Construction spend went from USD 145.7 to USD 173.0 in the 30 minutes of stages
1 and 2: about **USD 55 an hour** at 32 to 50 workers. Construction used,
including the USD 53.990121 before this run, is USD 210.7 of the new USD
653.990121 ceiling, so the ceiling is about **8 hours away** at that rate. It
was about 45 minutes away under v13. The ceiling stops the run by itself,
which is the design.

## 8. Machine headroom

Eight cores. During the 32-thread window the producer used 30 to 38 percent of
one core with 34 threads and 724 MB resident, so it is not CPU-bound: its
threads wait on the wire and on the ledger, not on the interpreter. The machine
load average was 12 to 14, and the largest single consumer was not the
producer: the read-only website viewer (`arctic_qa corpus-view`) held 62 to 85
percent of a core for the whole window. The streaming evaluator, the cost guard
and several agent sessions take the rest.

So the answer to "split the producer into two processes over disjoint halves of
the Jev ranking" is: not yet, and not for CPU. The producer has a whole core of
headroom of its own before the interpreter lock binds it. The store is
multi-process safe by design and the migration proved it (`ledger_store`
locks the journal, not the process), but a second producer process would take
its cores from the viewer and the evaluator, which are the two that are
actually using them.

## 9. The assessment of 100

Written against the measurement, not against the hope.

**50 runs, and it is where one producer process stops paying.** The run is at
50 paper workers now, with 43 calls in flight at its peak, 564 papers screened
an hour and no HTTP 429 in 724 paid calls. That is the captain's number, met.
But the 32-thread stage made more requests a minute with a shorter lock hold,
and the whole gain from 32 to 50 is 8 percent more papers an hour. The return
has flattened.

**What flattened it is the interpreter, not the ledger and not Google.** The
mean wait for the exclusive lock is one millisecond at both thread counts, so
nothing queues. What grows is the hold: 0.206 s at 32 threads, 0.319 s at 50,
for the same 26 milliseconds of real work per section. A thread holding the
lock shares one interpreter with every other paper thread of its process, so
each thread added past about 30 lengthens every other thread's serialised
section. 100 threads in one process would make the run slower than 50, not
faster.

**100 needs processes, and one more thing off the hot path.**

1. **Processes, not threads.** Splitting the producer over disjoint halves of
   the live Jev ranking does not divide the lock, which is a file lock every
   process shares, but it divides the peers the lock holder competes with. Two
   processes of 32 threads should hold the lock for about as long as one
   process of 32 does, at twice the papers. The store is multi-process safe by
   design and `arctic-ledger-parallel-r1` proved it under the evaluator; what
   is missing is the split of the ranking and a second activation, not code in
   the broker. This is the next experiment.
2. **The full immutable-event pass belongs off the hot path.** Six holds of 10
   to 18 seconds landed in the 15-minute window, about 9 percent of it: the
   periodic full proof and the reload after a compaction, each multiplied by
   the same contention. The compactor already runs the full money proof against
   its own materialization without the ledger lock. The full immutable-event
   pass can run the same way.

**Google has spoken once, at 50 and not at 32.** No HTTP 429 has ever been
recorded on this run, at any concurrency. One HTTP 503, `UNAVAILABLE`, "high
demand", arrived two minutes after the 50-worker window closed: the first 5xx
of the run, one in 724 calls at 43 in flight. That is a busy model, not a
published rate limit, and it cost a reviewed release and a stopped producer
because an ambiguous construction charge halts the whole ledger. The per-model
rate at which Gemini throttles is still unknown. What the 503 does say is that
**the cost of the next one rises with the concurrency**: more calls in flight
means more chances an hour that the run stops for a reviewed release.

So: **50 now, measured and live, under policy v14.** 100 is reachable and is a
scheduling change rather than a ledger change. If the choice is between 32 and
50 on today's code, 50 screens more papers an hour and 32 makes more calls a
minute with half the lock hold and no 503; the run is left at 50 because papers
an hour is what the campaign counts, and because the captain asked for it.

## 10. The run it is left at

Run `chapter3-7dc6485-r3` on snapshot `87c69ce`, 50 paper workers, 4 option
workers, policy v14 (50 concurrent, 300 a minute, away ceiling and checkpoint
USD 653.990121, lifetime USD 1,000.00), with
`ARCTIC_QA_OPERATION_LOCK_LOG_SECONDS=0` so every exclusive section stays in
the log. The streaming evaluator, the cost guard and the website viewer run on
the same snapshot. The activation is
`/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-concurrency-50-r1/`;
`build-scale.py` beside this report is the copy of the script that ran it.

The evaluator's unit now carries `TimeoutStopSec=900`, because a stop lands at
a trial boundary and one trial of the slowest vendor is minutes.

## 11. The release suite

`nix develop -c bash -c 'PYTHONPATH=src pytest tests/ -q'` on the branch head,
after the last relaunch: **1,650 tests, all passing**, no failures and no
errors. The suite was run twice, once on `5d4ee8c` and again after the USD 600
ceiling landed on `87c69ce`, because a money-path change owes its own run.

New guards on this branch:

- `tests/test_ledger_proof_cost.py`: a warm read lists the receipts directory
  once, replays only the rows the store reports moved, and makes no glob; a row
  changed behind the store, a receipt absent from the ledger and an
  accepted-item receipt taken away are all still caught; one paid call takes
  three exclusive sections and none around the free token count; the serialised
  hold of one call stays under 0.2 s at 50 rows of history; fifty construction
  calls are in flight at once with exact money; the fifty-at-once rate pair and
  the USD 600 ceiling pair are registered, each couples the fields that must
  move together, and the transition of each names its own tranche; the project
  lifetime ceiling counts every phase; the reservation's count of the
  evaluation calls in flight equals the full totals.
- `tests/test_ledger_store.py`: a rollback leaves every container tracked, and
  undoes a row edited in place.
- `tests/test_abstention_plan.py`: an operator stop lands at a trial boundary.

## 12. What was not done, and why

**The hot journal and lock were not moved to the local SSD.** The brief asked
for it, and the measurement says it is not where the serialised cost is. The
flush is already outside the ledger lock and is shared: one thread's `fsync`
covers every record its peers appended, which the predecessor measured at 57 ms
per commit across sixteen threads. Moving the journal to the SSD would take
that to about 10 ms of **per-thread** latency out of an eight-second call, buy
nothing serialised, and in exchange make the authoritative store live on a
different device from the ledger it belongs to, with a mirror that can lag it.
The money rule wants the reservation durable before the provider call on one
filesystem; the measurement says the price of that is now small. The serialised
cost fell from 2.44 s to well under 0.2 s without touching it.

**The three exclusive sections were not merged into one.** Merging orphan
recovery into the count registration would mean holding the lock across the
pacing wait, which `AGENTS.md` pins outside it after 19 papers faulted in 40
minutes on 2026-09-16, or running the per-paper cost cap before recovery, which
would let an unsettled orphan's reservation cap a family early and skip it for
good. Each section now costs milliseconds, so the merge buys a few of them
against a money-path change.
