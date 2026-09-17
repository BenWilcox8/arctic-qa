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
| HTTP 429 / 503 | 0 / 0 | 0 / 0 |
| ambiguous charges | 0 | 0 |
| candidate processing faults | 1 | 0 |
| producer CPU | 0.67 of a core | 0.45 of a core |
| machine load, 8 cores | 9 | 13.6 |

558 paid requests in the window, 87 papers screened. The number in flight now
tracks the thread count: 33 at its peak against 32 paper workers, where 16
workers reached 11. Nobody waits for the lock any more: the mean wait is one
millisecond.

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

## 9. What was not done, and why

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
