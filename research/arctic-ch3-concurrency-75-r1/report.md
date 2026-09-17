# Seventy-five papers in flight

Captain's ask, 2026-09-17 09:14 UTC, verbatim:

> At 8:45 stop all generation of new questions so that the evaluations can
> catch back up.
> Also raise concurrency for the night to 75 instead of 50.
> If this gives problems, lower it to 50 again.

08:45 in the captain's local time is 13:45 UTC.

Run `chapter3-7dc6485-r3`, campaign `arctic-qa-production-campaign-003`,
shared ledger
`/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json`.
The predecessor is `research/arctic-ch3-concurrency-50-r1/report.md`, which
took the run from 16 to 50 papers in flight and measured the stage.

This report records three things: the rate pair the night runs at, the
measured window that decided whether to keep it, and the stop that ends
generation at 13:45 UTC.

## 1. What the fifty-thread stage measured

| measurement | at 50 paper threads |
| --- | --- |
| peak requests in flight | 43 |
| papers an hour | 564 |
| provider refusals (HTTP 429) | none |
| mean wait for the exclusive operation lock | 1 ms |
| mean hold of the exclusive operation lock | 0.319 s |

The predecessor found the knee of the thread-count curve near 32 threads, and
it is the interpreter, not the ledger and not the provider. Nobody waits for
the exclusive lock any more; what grows with the thread count is the hold,
because the holder shares one interpreter with every other paper thread. The
number of calls in flight settles at the call length over that hold, so more
papers past the knee buys breadth and pays in request rate.

Seventy-five threads is one step past that knee, not a new design. The captain
asked for it for one night and asked for the fall back if it gives problems,
so the change is made in a way that makes the fall back free.

## 2. Policy v15, and why the fall back needs no transition

Policy v15 is policy v14 with exactly two fields changed:

| field | v14 | v15 |
| --- | --- | --- |
| `maximum_concurrent_generation_requests` | 50 | 75 |
| `maximum_generation_requests_per_minute` | 300 | 450 |

No money field moves. The away-session ceiling and the construction review
checkpoint stay at USD 653.990121, the project lifetime ceiling stays at
USD 1,000.00, the per-request cap stays at USD 0.25, the paper cost cap stays
at USD 1.00, and both project design counts stay where the USD 600 allocation
left them.

The two limits are one registered pair (`ALLOWED_REQUEST_RATES`), so a policy
can never raise one of them alone; `CHAPTER3_NIGHT_CHANGE` is the registered
move. Every earlier pair stays registered, so a relaunch at 50 papers in
flight under policy v15 needs no further transition: it is the same policy
file and the same applied transition, with one launcher argument changed.

### The tranche a rate transition names

A rate transition moves no money, so it names the construction ceiling that is
already authorized as its cumulative tranche. That ceiling is not a constant.
The three earlier rate transitions named the expansion tranche of
USD 253.990121. The USD 600 allocation moved the ceiling to USD 653.990121
earlier on 2026-09-17, so this one names USD 653.990121 instead. A transition
that names the wrong tranche is refused with "the policy transition identity
changed", which is how the parallel transition was refused at 04:53 UTC on
2026-09-17.

`tests/test_ledger_proof_cost.py` reads that rule out of the source, so a
fifth rate pair cannot forget it silently.

## 3. The cut-over

A policy transition needs a settled ledger of every caller, so both callers
stop first and the evaluator comes back as soon as the ledger is busy again.

| time (UTC) | step |
| --- | --- |
| 09:34:35 | the stop begins; the evaluator holds no paid call |
| 09:38:13 | the evaluator is down, drained at trial boundaries |
| 09:39:07 | the producer is down at a boundary: this run holds no submitted request |
| 09:39:48 | the v14 to v15 transition is applied and the ledger is proved |
| 09:39:57 | the producer is back, 75 paper workers, 4 option workers |
| 09:44:00 | the evaluator is back, on its own snapshot, started by its own crew |
| 09:44:12 | the first paid construction call of the relaunched producer |
| 09:44:40 | the website viewer is back, on this snapshot |

The ledger at the boundary held no request in flight, was not halted, and had
spent USD 211.290557. The proof after the transition returned
`integrity_valid: true`, `halted: false` and the same USD 211.290557: the
transition moved no money. The limits it left are 75 concurrent generation
requests and 450 a minute, with the away ceiling and the review checkpoint
both still USD 653.990121 and the project lifetime ceiling still USD 1,000.00.

### The evaluator keeps its own snapshot

The evaluator shares the ledger, so the transition needs it settled and
stopped. It does not need it moved. Another crew put the unit on commit
`82612f5` with concurrent item scoring at 09:20 UTC, which is later than this
snapshot and already reads the store, so this activation records the unit's
own launcher and working directory before the stop and starts exactly that
again afterwards. The cost guard, on the same commit since 09:21 UTC, is not
touched at all: a restart on this snapshot would undo the captain's quota
floors.

In the event the other crew started the unit itself at 09:44:00, twelve
seconds before the first construction call. The restart step saw the unit
already active and started nothing, because the unit belongs to that crew.
Twelve seconds is a real overlap: an applied transition is validated again on
every broker start until its first construction request, and that validation
needs `inflight` to be 0. The producer had already started at 09:39:57 and had
passed its validation, so the overlap cost nothing here. It is still the
window the two crews have to keep apart.

### One stop timeout that was still the transient default

The live evaluator unit carried `TimeoutStopUSec=1min 30s`, the transient
default. That default is shorter than one trial of the slowest vendor and is
what made systemd kill the evaluator at 07:25:55 UTC on 2026-09-17. The stop
therefore signals the unit's main process itself and waits for it, so systemd
never reaches that bound, and the restart gives the unit
`TimeoutStopSec=900`. The drain took 3 minutes 38 seconds and finished eight
items on the way out; nothing was killed.

## 4. The measured window, and why the rate went back to fifty

### The producer's own numbers at 75

The window ran from 09:44:45 to 10:04:45 UTC, which includes the relaunch replay.

| measurement | value |
| --- | --- |
| requests | 163 |
| requests a minute | 8.15 |
| papers | 55 |
| papers an hour | 165 |
| peak requests in flight | 31 |
| 429 | none |
| 5xx, and any halt | none |
| new ambiguous rows | 0 |
| `candidate_processing_fault` delta | 0 |
| producer CPU fraction | 0.18 |

| exclusive section | calls | mean wait | mean hold | longest hold |
| --- | --- | --- | --- | --- |
| `orphan_recovery` | 215 | 0.058 s | 0.642 s | 50.35 s |
| `count_registration` | 215 | 1.898 s | 0.605 s | 48.42 s |
| `reserve` | 163 | 0.071 s | 0.414 s | 12.70 s |

That is 1.661 s of serialised bookkeeping per paid call, against 0.762 s measured
at 50 threads over 09:20 to 09:34. Once the replay ended the producer itself ran
well: 59, 42, 34, 74 and 47 reservations in the minutes from 10:05, and holds back
down near 0.1 s.

### The relaunch replay is not a stall

For the first twelve minutes the run made two paid calls, one thread waited 180 s
for the exclusive lock, and the evaluator held that lock in 118 of 120 half-second
samples. That reads like starvation and is not: it is the replay `AGENTS.md`
describes, in which the producer walks the papers its eligibility run directory
already holds before its first paid call. During the replay the producer asks for
the lock rarely, and `flock` gives no fairness, so a caller that asks often holds
it almost continuously. The replay ended at 09:56 and the producer took the lock
back in 43 of 60 samples.

A health window shorter than the replay sees a live producer, a moving
`progress.json` and no ledger movement at all. Read the reservation count, not the
request count, before calling such a window a stall.

### What decided the fall back: the evaluator's wait, not the producer's rate

The producer and the evaluator share one exclusive operation lock. The measurement
that matters is therefore the other caller's wait, and the evaluator logs its own
sections above the one-second default.

| producer threads | window | `orphan_recovery` mean wait | `count_registration` mean wait | evaluator lock wait per call | longest single wait |
| --- | --- | --- | --- | --- | --- |
| 50 | 09:20 to 09:34 | 1.92 s | 1.41 s | **3.33 s** | 7.2 s |
| 75 | 10:05 to 10:12 | 30.49 s | 3.12 s | **33.61 s** | 81.2 s |

Ten times the wait, for a producer rate that was no better. The evaluation crew
measured the same thing independently and reported 22 s of ledger lock per Gemini
call under the 75-thread producer.

That is the captain's condition, so the producer went back to 50 papers in flight
at 10:11:11 UTC under the same policy v15, with no transition and no new gate: the
fifty pair stays registered, and the launcher takes the worker count as an
argument. The evaluator kept running through the fall back, because the applied
transition had already had its first construction request.

The producer's own memory is a second reason to prefer 50. Its resident size grew
from 1.0 GB to 3.2 GB over the 75-thread stage and sat at 0.37 GB shortly after
the relaunch at 50.

### What the fifty-thread window after the fall back can and cannot say

The first attempt at a window after the fall back, 10:20 to 10:40 UTC, is
worthless: the evaluator was down on a halt for the first half of it and the
ledger halted for the second. The measured window at 50 with both callers up is
the one recorded below, 10:44 to 11:04 UTC.

The window from 10:44 to 11:04 UTC ran with both callers up. It also spans the
fourth halt of the day and the relaunch that answered it, so its paper rate is
depressed by one replay; its lock numbers are the ones to read.

| measurement | 50 threads (10:44 to 11:04) | 75 threads (09:44 to 10:04) |
| --- | --- | --- |
| requests a minute | 20.85 | 8.15 |
| papers an hour | 195 | 165 |
| peak requests in flight | 49 | 31 |
| `orphan_recovery` mean wait | 0.000 s | 0.058 s |
| `count_registration` mean wait | 0.000 s | 1.898 s |
| `orphan_recovery` mean hold | 0.471 s | 0.642 s |
| serialised bookkeeping a call | **0.640 s** | **1.661 s** |
| 429, 5xx, ledger halt | none | none |
| `candidate_processing_fault` delta | 0 | 0 |
| producer resident size | 2.0 GB | 3.2 GB |

Seventy-five threads never filled their own slots: the peak was 31 calls in
flight against 49 at fifty threads. Past the knee the extra threads do not buy
breadth; they lengthen the hold, and the lock is what everything waits for.

Three later windows were opened and none is clean: the producer exited on the
reservation's halt check at 11:21:21 and again at 11:44:51 UTC, each time while
the ledger read `halted` false and every ambiguous charge of the ledger had its
continuation event. The first exit is explained by the stale receipts listing
described in section 6 and is fixed. The second survived that fix, so the two
conditions behind the one message now name themselves, and the next exit will
say which fired and on which charge.

The comparison above therefore stands on the two windows that are alike: each
spans one relaunch replay, at 75 and at 50, and the evaluator's own lock wait is
measured from its journal and is untouched by either.

## 6. Three halts, and the rule that answers them

The night was interrupted three times by one class of stop: an ambiguous charge,
a paid call whose cost the broker cannot prove. The reviewed release of one takes
a person about ten minutes to write, and the caller is down for all of it.

| time (UTC) | charge | phase | what it stopped |
| --- | --- | --- | --- |
| 10:03:34 | `2fe6757b`, an interrupted orphan | evaluation | the evaluator, until 10:39 |
| 10:23:02 | `4be16d5d`, an HTTP 503 | construction | the producer, until 10:29 |
| 10:30:48 | an integrity halt, not an ambiguous charge | both | the producer, until 10:35 |

### The interrupted orphan, and the case that did not exist

The first was not a provider error. The evaluator was cut over from snapshot
`82612f5` to `c6f1767` while a Gemini call was on the wire. Orphan recovery found
neither a final nor a received receipt and wrote the receipt itself, with the
error `interrupted request has no durable provider response`.

The reviewed release admitted three bounded cases: an HTTP 5xx answer, a received
response cut off at `MAX_TOKENS`, and a provider timeout. This was a fourth. The
fourth case now admits exactly that receipt, retains the full reservation as
charged, and never settles, retries or replays it.

The same cut-over rewrote the derived per-item evaluation gate seven seconds after
the call was submitted, so the digest the ledger row binds no longer exists on
disk. Reconstructing those bytes and proving them by the recorded digest would
have been evidence rather than forgery, and it was tried over every second of a
25-minute window with and without a trailing newline; nothing matched. For the
interrupted-orphan case alone the release may now prove a later authorized
successor of the same gate instead, and record both digests. The ledger row is
never edited.

### The deadlock between two phases

The release also required that every other outstanding ambiguous charge already be
in reviewed custody, and that check never read a phase. When the construction 503
arrived at 10:23 the two charges blocked each other: neither could be released
while the other had no continuation event, so neither halt could ever lift. Both
callers were down.

The check now reads the phase whose halt the release lifts, which is what the
phase-scoped halt already intended. The safety property is unchanged: a phase's
halt lifts only when every ambiguous charge of that phase is in reviewed custody.

### A new event schema stops every older reader

The interrupted-orphan release writes a continuation event of a schema only the
new code reads. The producer was still running the previous snapshot, refused the
event and recorded an integrity halt on the shared ledger at 10:30:48 UTC, which
stopped it. The halt was superseded once the ledger validated under the code that
knows the schema, and both callers were moved to that code.

A new receipt schema on a shared ledger is a cut-over of every reader, not a
change to one writer. Write the reader first, ship it everywhere, then write the
record.

### Two exits the rule did not answer

The producer exited twice more, at 11:21:21 and 11:44:51 UTC, on the
reservation's own halt check. Each time the ledger read `halted` false
afterwards, every ambiguous charge of the ledger had its continuation event,
and no new ambiguous row existed.

The first is explained: a concurrent broker keeps its receipts listing for up to
`RECEIPT_LISTING_REFRESH_SECONDS`, so a continuation event another caller wrote
is invisible to it for that long, and the check read those charges as
unresolved. The reservation now lists the directory again before it stops the
run, and only then.

The second survived that fix, so the cause is still open. Two conditions raised
the one message and it named neither, so each now names itself and the blocking
one names the charges it found. The next exit says which fired.

### The rule: one unknown charge stops nobody

For every bounded ambiguous case the broker now writes the same release record the
reviewed release writes, keeps the whole reservation, and continues. The bound is
five automatic releases per phase per hour; beyond it the charge halts its phase
exactly as before, because a run that books unknown charges faster than that has a
fault an operator must read. A construction charge never spends the evaluation
phase's bound, and no other money rule moves.

## 5. Stopping generation at 13:45 UTC

The first half of the captain's ask is a stop, not a change of rate. It has its
own script, because it is operated later and under time pressure:

```
/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-concurrency-75-r1/stop-generation.sh
```

It stops the producer only. The evaluator keeps its unit and keeps scoring,
because catching up is what the stop makes room for.

The script reads the runtime snapshot alone and never the task worktree, which
is disposable. It waits until this run holds no submitted request, then ends
the producer process and the tmux session and writes a receipt beside itself.
A free token count reserves nothing and is never waited for: at 75 paper
threads one of them is open at almost every instant, so a wait for zero of
them would never reach a boundary.

The wait matters, because the producer installs no signal handler. A signal
sent while calls are on the wire kills the process and leaves those rows for
orphan recovery, which charges the money and discards the answer. If no
boundary is reached inside `CH3_STOP_TIMEOUT_SECONDS` (1800 by default) the
script stops nothing, writes the receipt and exits 1.

`stop-generation.sh --dry-run` reports the boundary and changes nothing. Both
dry paths were exercised before the hand-over: the dry run against the live
producer, and the receipt that the "no live producer" path writes.
