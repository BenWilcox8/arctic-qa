# The operation lock as a queue, and the completion date of a paper

Captain's asks, 2026-09-17 00:28 and 00:31 UTC, for the chapter 3 concurrent
producer:

1. "Make sure that the completion dates are preserved for concurrent runs; right
   now they all say unknown"
2. "Also right now many papers say 'Processing ended with the retained reason:
   BrokerOperationBusyError.' Be sure to fix this"

Run `chapter3-7dc6485-r3`, campaign `arctic-qa-production-campaign-003`.

## 1. What the first concurrent run did

The run started at 23:51 UTC on 2026-09-16 with four paper threads on commit
6fa9163. In its first 40 minutes it screened 26 papers, made 72 paid requests
and accepted no new question. `candidate_processing_fault` went from 1 to 18 and
`incomplete_infra` from 1 to 11. The website showed many papers with "Processing
ended with the retained reason: BrokerOperationBusyError" and a completion date
of "Unknown".

Measured on the live state at 00:50 UTC: 19 routing rows and 8 in-flight call
records carried `BrokerOperationBusyError`, all of them written after 23:51.

### The lock

`BrokerOperationBusyError` is the bounded wait for the exclusive broker
operation lock (`OPERATION_LOCK_WAIT_SECONDS`, 120 seconds) giving up. The bound
was written for a sequential producer that met the lock only when a reviewed
operation of another task held it, which is short.

Under concurrency the lock is not a rare meeting. `execute` took it before the
admission and held it through:

- the orphan recovery,
- the execution-gate validation,
- `_pace`, which sleeps up to 60 seconds at a time until the frozen per-minute
  window has room,
- the free `countTokens` call and its bounded retry of about two minutes,
- the reservation and its transient retry of up to 90 seconds.

With four threads one of them was almost always inside that section, so the
other three queued behind a provider round trip plus a pacing sleep. 120 seconds
was not enough, the wait expired, and the candidate fault containment recorded
the paper. The refusal says nothing about the paper: it reserved nothing,
submitted nothing and was charged nothing.

It also explains the throughput. Everything except the live call was serialised,
so four threads did no more work per minute than one.

### The date

The pipeline trace reads the producer's progress row before the stored evidence
(`pipeline_trace._paper_records` and `_project_run`). The row named the state
but carried no time, and the code then wrote
`state_entered_at_utc = progress_row.get("state_changed_at_utc")`, which is
`None`. That replaced the time the receipts and the candidate rows already
answered, and the page printed "Unknown".

The window keeps the last 100 papers, so every paper of the concurrent run was
inside it. The sequential run had the same row shape; its papers had simply
fallen out of the window and kept their receipt-derived time.

## 2. The repair of the code

### The lock is a queue

`model_broker.SharedGeminiBroker._acquire_operation_lock` now owns the wait.
A concurrent construction request waits `OPERATION_LOCK_QUEUE_CEILING_SECONDS`
(600 seconds, a sanity bound in minutes) with a heartbeat line every 30 seconds.
A reached ceiling is not a verdict on the paper, so it retries the same request
key: `OPERATION_LOCK_QUEUE_ROUNDS` (3) rounds, 30 minutes in total. Only the
last exhausted round raises `BrokerOperationBusyError`, which the producer
contains against one paper exactly as before. The sequential path keeps its
120-second bound and its whole-call lock unchanged.

### The exclusive section holds the mutation alone

For a concurrent construction request the lock is taken by
`_exclusive_operation` around each ledger mutation and nothing else:

| section | what is inside |
| --- | --- |
| `orphan_recovery` | the recovery of this run's stranded requests |
| `count_registration` | the count-retry round, the resume, the count event |
| `count_error` | a count error the retries did not clear |
| `too_large_halt` | the halt of a request over the model input limit |
| `reserve` | the reservation, the submitted receipt and the in-flight lock |

The free token count, its retries, the pacing wait and the wait between two
reservation attempts are outside it. The `reserve` section still runs from
before the reservation until the in-flight lock is held, because a peer's orphan
recovery must never meet a reserved request with no in-flight lock.

`_exclusive_operation` is a no-op where the whole-call lock is already held:
`flock` of one file from a second descriptor of one process blocks against
itself.

### The measurement

Every section prints one line on stderr when its wait or its hold reaches a
second:

```
[operation-lock] <utc> pid=<n> thread=paper_1 section section=reserve request=<key16> waited_s=0.08 held_s=0.31
```

A wait longer than 30 seconds prints a `waiting` line while it waits, a reached
ceiling prints `ceiling_retry`, and an exhausted last round prints `gave_up`.
The launcher keeps stderr in its log, so the lock is measurable from the log
alone.

### The completion date

- `streaming._Progress.paper` records `state_changed_at_utc`: the moment the
  paper reached this state, kept across later rows of the same state.
- `paper_completions` gains a nullable `completed_at_utc` column, added by
  `db.Database._add_missing_columns` under the current schema version. A
  nullable column changes nothing for an earlier reader, so the version does not
  move and the benchmark evaluator's pinned snapshot still opens the database.
- The producer writes the paper's own completion time into its label.
- `pipeline_trace` reads the label as the fallback and, above all, never
  replaces a known time with nothing.

### The repair command

`arctic-qa repair-concurrency-faults --action {clear-busy-faults,
backfill-completion-dates,both}`, dry by default and `--apply` to write. It
reads and writes stored state only: no provider call, no receipt read, nothing
altered in the shared ledger.

- `clear-busy-faults` deletes the routing rows of the refusal, clears the fault
  note from the in-flight call records, deletes a completion label of such a
  paper and, with `--streaming-progress-file`, drops its progress row. The call
  record itself stays: a row saying a call was opened and its outcome is unknown
  is evidence, and the producer settles it when it walks the family again.
- `backfill-completion-dates` fills a label written without a time, from the
  last completed request of its family in the shared ledger or the last update
  of one of its candidate rows, and fills the progress rows the same way.

## 3. What was repaired on the live run

Applied at 00:59 and 01:05 UTC on 2026-09-17,
`research/arctic-ch3-concurrency-busy-r1/repair-*.json`:

| what | count |
| --- | --- |
| papers whose refusal was cleared | 19 |
| routing rows deleted | 19 |
| in-flight call records cleared | 8 |
| completion labels deleted | 0 |
| progress rows of a refused paper dropped | 19 |
| completion labels given their completion date | 224 |
| progress rows given their state time | 77 |

The 19 papers:

```
10.1016/j.scitotenv.2016.12.152        10.1017/aog.2025.10034
10.1038/s41467-026-76245-5             10.1038/s41586-022-05205-y
10.1038/s44183-024-00047-9             10.1109/mcg.2023.3286228
10.1134/s1024856023040085              10.1136/bmjopen-2020-047514
10.1139/w11-004                        10.1371/journal.pone.0014491
10.1371/journal.pone.0219319           10.21443/3034-1434-2024-2-3-100-114
10.21685/1680-0826-2025-19-3-5         10.3201/eid1912.130319
10.3201/eid2901.220154                 10.32604/rig.2026.075844
10.3389/fmicb.2017.00317               10.59887/2073-6673.2024.17(1)-3
10.7146/mog-ms.v43.150098
```

None of them carried a completion label, so none was skipped as finished; the
records were what the website showed. No receipt was altered.

The website (`/api/pipeline-trace`, 25 newest papers) before and after:

| | before | after |
| --- | --- | --- |
| papers with a completion date | 1 of 25 | 19 of 25 |
| papers showing the refusal | 9 of 25 | 0 of 25 |

The six without a date are papers still mid-family: three were in flight when
the producer was stopped and three are eligible with generation pending. A paper
that is not finished has no completion time to show.

## 4. The stop and the relaunch

The producer of the first concurrent run was stopped at 00:34:45 UTC. The stop
waited for a zero-in-flight boundary on the shared ledger, but the kill went out
before the wait, so one `standalone_verification` request of the run was left
`submitted`. It is a stranded request of this run, not a charge: the broker's
orphan recovery settles it before the first paid call of the next start, which
is the path `docs/SHARED_MODEL_BROKER.md` describes. The broker was constructed
against the live ledger afterwards and answered `integrity_valid: true`,
`halted: false`.

Relaunched at 01:22:03 UTC under the same run id from the snapshot at
`/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-concurrency-busy-r1/runtime/app-5653055-arctic-ch3-concurrency-busy-r1`,
gate `live-execution-gate-5653055-ch3cb.json`, launcher
`launcher-5653055-ch3cb.sh`, four paper workers and four option workers.

**The start applied no policy transition**: the budget policy, its hash and the
applied ledger transition are the ones the predecessor activation left, and
nothing about money moves. The benchmark evaluator (unit
`arctic-abstention-stream-r3`) was therefore left running, as the start-order
rule allows.

## 5. The measured window

See `activation-state-5653055.json`, key `observation`, in the task data
directory for the samples and the summary. The sequential baseline is about 3
requests a minute and about 13 papers an hour.

## 6. Tests

- `tests/test_broker_operation_lock_queue.py`: four paper threads queue for one
  lock and every request completes; a concurrent request waits past the
  sequential bound while the sequential one still refuses at it; a reached
  ceiling retries the same request key; an exhausted last round reserves,
  submits and charges nothing; the free token count is outside the exclusive
  section; the log carries the wait and the hold; the sequential path keeps one
  lock for the whole call and never deadlocks on itself.
- `tests/test_completion_dates_and_repair.py`: the producer records the time of
  each state; the concurrent path records the date the sequential path did; the
  label carries the completion time and not only the label time; a state that
  does not change keeps its first time; a progress row without a time no longer
  hides the known one; the repair clears every refusal and nothing else, drops
  the progress row, is idempotent, and the backfill fills a label written
  without a time; the label table takes the new column without a version bump.
- `tests/test_broker_operation_lock_wait.py` keeps the sequential bound.
