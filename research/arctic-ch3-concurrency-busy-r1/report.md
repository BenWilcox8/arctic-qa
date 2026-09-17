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

## 5. The second fault: the admission, not the lock

The first relaunch cleared the refusals but not the throughput: 2.8 requests a
minute and 15 papers an hour, against a sequential baseline of about 3 and 13.
The captain asked what serialises the four threads.

### What the measurement says

Every `[operation-lock]` line of the 01:22 run carries `waited_s=0.00`. No
thread ever waited for the lock, so the lock is not the serialiser, and it is
released before the HTTP call. The holds are the finding:

| section | hold |
| --- | --- |
| `orphan_recovery` | 4 to 5 s |
| `count_registration` | 11 to 20 s |
| `reserve` | 4 to 7 s |

About 25 seconds of admission per paid call, and the admission is serialised by
design (`_admission_lock`: one admission at a time, N calls on the wire).
`research/arctic-ch3-concurrency-busy-r1/overlap.py` on the ledger for
01:25 to 01:58:

```
calls 97, window 32.9 min, calls_per_minute 2.95,
peak_in_flight 1, calls_that_overlap_an_earlier_one 0,
median_call_seconds 8
```

A call lasts 8 seconds and the admission in front of the next one lasts 25, so
the call is always over before the next request is admitted. Zero overlap is
the arithmetic of those two numbers, not a lock held across the call.

### Where the 25 seconds went

Measured against the live ledger (5,393 rows, 7.4 MB; 21,508 receipt files):

| step | cost |
| --- | --- |
| read and parse the ledger | 0.08 s |
| `_validate_ledger` (the ledger's own consistency) | 0.14 s |
| `_validate_immutable_events` | 2.4 s |
| `_validate_active_transition_event` | 0.03 s |

`_validate_immutable_events` made 27,606 `stat` calls and read 11,074 receipt
files, once for every ledger read. A paid call reads the ledger about seven
times (orphan recovery, the count-retry open, the resume, the count event, the
reservation, the settlement), so 17 of the 25 seconds were that one function
proving the same 5,393 rows again.

### The repair

The proof is a pure function of the ledger bytes and of the receipt files
beside them, and a receipt is written immutable (0444) and never rewritten. So:

- The proof of one row is kept under a signature of that row
  (`canonical_json` of the ledger entry) and replayed only for a row that
  moved.
- The whole proof is skipped while neither the ledger bytes nor the receipts
  directory has changed (`_ledger_evidence_fingerprint`).
- A full pass runs on broker construction and again every
  `IMMUTABLE_EVENT_REVALIDATION_SECONDS` (300 s), so a receipt changed behind
  the process's back is still caught, within five minutes.
- `_validate_ledger`, the ledger's own hash and total consistency, still runs
  on every single read. Nothing about the money is proved less often.

Measured on the live ledger after the change: 1.80 s a read before, 0.75 s
after a change to the ledger, 0.21 s with none.

Relaunched at 01:58:09 UTC on commit 229d136.

## 6. The third fault: the evaluator held the shared ledger lock

Cutting the proof cost took the producer to 5.27 requests a minute and 112
papers an hour, and overlap appeared (peak 2 in flight, was 1). The admission
was still about 20 seconds, and `count_registration` still held 10 to 13 of
them, which the producer's own work no longer explains.

Measured directly, while the producer and the evaluator both ran: the **shared
ledger lock** (`.shared-paid-call-ledger.json.lock`, not the operation lock) was
held by someone else for a median of 2.35 s and up to 6.56 s per acquisition.
The holder is the streaming abstention evaluator, `abstention-eval --action
watch`, which shares this ledger and, on the snapshot it ran, still proved 5,393
rows against 21,508 receipt files on every read. The count registration took
that lock three times, which is the 7 to 10 seconds that were left.

Two answers, both applied:

- `research/arctic-ch3-concurrency-busy-r1/cutover-evaluator.py` moved the
  evaluator onto this branch's snapshot at 02:27:58 UTC, under a successor
  authorization bound to the running commit. `abstention_watch` refuses an
  authorization whose `integrated_code_commit` is not the running one, and
  passing the old commit while running new code would have been a false record
  on a money path, so the successor names the predecessor and its review
  exactly as a successor execution gate does. The five files the authorization
  binds are byte-identical between the two commits, and the script refuses
  otherwise; no bound, plan, policy, price or model moved.
- The three steps that register a counted request now run in one
  `_ledger_session`: the shared lock is taken once and the validated ledger is
  read once. No other writer can change the file while the session holds the
  lock, so the second read would return the same bytes, and a commit writes the
  very object the session holds.

Measured after the evaluator restarted: the shared ledger lock wait fell from a
median of 2.35 s to 0.21 s.

## 7. The fourth fault: a counting row of a stopped start

The 02:29 relaunch ended at 02:32:10 UTC, three minutes in, with

```
{"code":"VALUEERROR","message":"the paid request key already exists"}
```

The stop before it waited for a boundary with none of the run's calls
*submitted*, which is the right boundary for money: nothing was on the wire.
But a thread can also be between the free count event and the reservation, and
that leaves a `counting` row: registered, not reserved, not submitted, not
charged, with no receipt. Row `d74d534e...`, stage `standalone_verification`,
paper `10.5194/acp-23-10451-2023`, was exactly that. The next start walked back
to the same call, built the same request key, met its own row and raised a plain
`ValueError`, which `broker_provider.broker_boundary` marks a whole-run stop.

Three changes:

- A `counting` row of the same request identity is reused. The free count runs
  again and nothing is charged; `count_requests` is not incremented, because it
  counts rows and the ledger totals check it against `len(requests)`. The
  identity fields are `SharedGeminiBroker.REQUEST_IDENTITY_FIELDS`; a row whose
  key matches but whose paper, family, source version, stage, phase or model
  differs is not this request and is still refused.
- Every other existing key raises `errors.DuplicateRequestKeyError`, which
  reserves nothing and submits nothing and is therefore in
  `broker_provider._PAPER_LEVEL_BROKER_ERRORS` and in the non-stop set of
  `streaming._ends_the_run`. The family is recorded and skipped, and the run
  continues.
- The activation script's stop now also waits out a `counting` row of the run,
  so a graceful stop leaves none behind.

## 8. The measured window

Every activation state file in the task data directory holds its own samples
under `observation`. The sequential baseline is about 3 requests a minute and
about 13 papers an hour.

| run | what was fixed | requests a minute | papers an hour | peak in flight | calls overlapping an earlier one |
| --- | --- | --- | --- | --- | --- |
| sequential baseline | - | ~3 | ~13 | 1 | 0 |
| 01:22, 5653055 | the lock queue and the dates | 2.8 | 15 | 1 | 0 of 97 |
| 02:12, 229d136 | the per-row immutable-event proof | 5.27 | 112 | 2 | 3 of 29 |
| 03:00, b578731 | the ledger session, the evaluator, the counting row | 6.73 | 172 | 3 | 48 of 79 |

The last window is 15 minutes from 03:00:00 to 03:15:00 UTC: 101 paid requests
and 43 papers screened, with a median call of 8 seconds. The producer ran the
window with no exit.

Faults in that window: one, `OperationalError: database is locked`, contained
against one paper as designed. The state database is written by four paper
threads and read and written by the evaluator's own process, and SQLite gave up
at its 5-second default. `db.BUSY_TIMEOUT_SECONDS` is now 30 seconds, which is
generous against a write that takes milliseconds. It takes effect at the next
start of the producer.

### What limits it now

Eight papers in flight is not reachable while an admission costs seconds: the
number in flight settles at the call length over the admission length, and the
call is 8 seconds. The policy allows 8 concurrent requests and 40 a minute, and
neither is the bound; no HTTP 429 has ever been recorded. The bound is the
ledger work of an admission, so the next step, if one is wanted, is fewer and
cheaper ledger reads per call, not a higher limit.

### The models

`gemini-3.1-pro-preview` is called on the concurrent path exactly as before.
Between the 01:22 relaunch and 02:00 it completed 51 calls across
`answer_agreement`, `answer_verification`, `blinded_reconstruction`,
`option_verification` and `standalone_verification`, beside 49 of
`gemini-3.8-flash`, with no error.

### A flake to know about

A whole-suite run that overlaps a live producer can fail eight timing-sensitive
tests of `tests/test_abstention_watch.py`, and one wall-clock assertion of
`tests/test_abstention_plan.py`
(`test_ledger_accepts_concurrent_evaluation_calls_and_keeps_construction_pacing`,
which asserts eight concurrent calls finish inside 1.2 s and measured 1.53 s).
Every one of them passes on its own, in its module and in its neighbourhood,
and the whole suite passed twice on the same commit while the producer was
quieter. They are assertions about wall-clock time on a machine that is also
running the paid producer and the evaluator, not failures of this change.

## 9. Tests

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
- `tests/test_broker_operation_lock_queue.py` also pins the admission: an
  unchanged ledger is not proved against its receipts again, a changed one is,
  a row that moved is proved again while a still row is not, a changed receipt
  is still caught, the count registration takes the shared ledger lock once, a
  session reads the ledger once and is reentrant, a `counting` row of a dead
  start is reused while one of another request is refused, and a duplicate key
  never ends the run.
- `tests/test_completion_dates_and_repair.py` also pins the state database's
  wait for a lock another connection holds.
- `tests/test_broker_operation_lock_wait.py` keeps the sequential bound.
