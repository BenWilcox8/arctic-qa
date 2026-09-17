# The exclusive ledger section of the streaming evaluator

The Gemini arm of the streaming abstention evaluator was bound by the one
exclusive operation lock of the shared paid-call ledger, and not by the model.
`research/arctic-eval-parallel-items-r1/extrapolation.md` measured the bound at
09:44 to 09:55 UTC on 2026-09-17: three sections of one paid Gemini call held
that lock about 22 s in total, which is 2.6 calls a minute and 13.7 questions
an hour, against about 300 accepted questions to score before 14:00 UTC.

This record holds where those 22 s went and what this task removed.

## Answer first

Four costs are measured and removed, each on the live ledger. What is **not**
established is a live before-and-after of the arm's rate: the producer's own
load moved between every pair of windows this task could take, and it moves
the same numbers. "What the live windows do and do not show" holds that in
full, and gives the command to run after 13:45 UTC, when the captain's order
stops generation and a producer-idle window comes for free.

The costs, and none of them is the money proof itself.

| What | Was | Now |
| --- | --- | --- |
| One ledger read, receipts fingerprint moved | 0.21 to 0.26 s | 0.026 s |
| The proof of every row at a broker start | 2.6 s a question | once a process, then the signatures alone |
| The custody of every terminal receipt at a start | 0.24 s a question | once a process |
| The compaction of the whole ledger at a start | 1.16 s a question | once a process |
| The duplicate materialization of the identity check | 0.19 s a question | removed |

Every number above was measured on the live chapter 3 ledger on 2026-09-17,
on an idle machine with a warm page cache: the reads at 13,309 request rows,
a 18 MB snapshot, a 41 MB journal and 52,716 receipt files; the row proof and
the custody at 15,347 rows, three hours later.
The evaluator builds one broker per question, because the derived evaluation
gate belongs to the question, so every "a question" line above was paid once a
question, under the shared ledger lock.
The live evaluator shares one interpreter and one rotating USB disk with the
producer, which multiplies each of them: the 50-crew measured the same
multiplier at 0.206 s a call at 32 paper threads and 0.319 s at 50, for the
same 26 ms of real work
(`research/arctic-ch3-concurrency-50-r1/report.md`).

## The receipts listing the evaluator kept exact

`RECEIPT_LISTING_REFRESH_SECONDS` bounds how often a broker lists the receipts
directory again.
Without it the listing is exact: the directory is stated, and the listing is
taken again the moment the fingerprint moves.
Everything derived from the listing is thrown away with it.

The producer moves that fingerprint on every paid call of every one of its
workers, so the exact fingerprint makes a full `scandir` of 52,716 entries,
plus every derivation over those names, part of every ledger read.
The 50-crew gave the producer the bounded refresh in `378afe1`.
It hung the refresh on `concurrent_construction`, which is a flag about
construction requests.
The evaluator holds no construction request, so it never got the bound.

`research/arctic-eval-ledger-section-r1/measure-one-read.py` measures one read
of the live ledger with the fingerprint moved:

```
moved-fingerprint read, listing thrown away: 0.206, 0.211, 0.243, 0.259, 0.249 s
moved-fingerprint read, listing kept:        0.026, 0.026, 0.025, 0.026, 0.026 s
```

The difference is the listing (0.078 s), the seven name filters over it
(0.129 s) and the request-key scan (0.052 s).
A paid call reads the ledger several times inside the exclusive sections.

`concurrent_requests` is the flag now: whether another live writer shares this
ledger.
A concurrent construction run is one by definition, so the producer's flag
still implies it, and no reviewed operation changes.
The evaluator's own broker factory sets it.

## The whole-ledger start the evaluator ran once a question

The evaluator builds one broker per question, because the derived evaluation
gate belongs to the question.
A broker start materializes the store, proves every row of it against the
receipts, publishes the status record and compacts a snapshot that lags its
journal, all under the shared ledger lock.

`research/arctic-eval-ledger-section-r1/measure-one-start.py` measures the
compaction of that start on the live ledger:

```
materialize 0.190 s  prove_ledger 0.405 s  write_snapshot 0.569 s  total 1.164 s
```

The start also materialized the same two files a second time, for the identity
check alone: another 0.190 s.
Each start left a compactor thread, a whole copy of the ledger and an `atexit`
compaction behind it; by the fourth hour of an evaluator that is a thread a
question.

A broker of a ledger this process already started, and that says it shares the
ledger, now skips the compaction of the start.
The compactor thread of the first start keeps the snapshot current within
`COMPACTION_INTERVAL_SECONDS`.
The identity check reads the store instead of materializing the files again.
One compactor thread serves one ledger per process.
A reviewed operation is its own process and still compacts at its start,
because it binds the file.

## What did not move

No money rule.
The same rows are proved against the same receipts by the same code, under the
same locks, with the same ceilings, halts, gates and prices.
The staleness of the receipts listing changes, from exact to the bound the
producer already runs under, and the full immutable-event pass still re-lists
the directory first and still runs every
`IMMUTABLE_EVENT_REVALIDATION_SECONDS`.
A process does not prove again, at a second broker start, what it proved at
the first and keeps proving on every read.

`tests/test_ledger_proof_cost.py` holds the shape and the proof together: each
test that removes a cost also shows that the thing it removed is still caught.

## The cut-overs

The unit is `arctic-abstention-stream-r3`, the work directory is
`abstention-eval/streaming-r11`, and `resnapshot-evaluator.sh` is the script.
Each snapshot has its own successor authorization and review record beside it,
under `arctic-eval-authorization-r8`.

| Snapshot | Live from | What it carried |
| --- | --- | --- |
| `8477fd5` | 10:57:36 UTC | the receipts listing and the once-a-process start |
| `e46dd7f` | 11:16:08 UTC | and the share of every wave for every arm |
| `71e3c35` | 11:49:14 UTC | and the resume asked at every admission |
| `8a61b0f` | 12:02:45 UTC | and the full slot kept inside one question |
| `f121d93` | 12:30:22 UTC | and the seeded start, and the contained busy lock |

`8477fd5` was replaced at 11:05:32 UTC while the two crews of the night both
held the unit, and the Gemini arm was idle for the whole of its eight minutes,
so it measured nothing. The captain's supervisor then gave the unit to this
task alone. `8a61b0f` was restarted once at 12:21:24 UTC, after the watch
exited on the busy lock that `f121d93` contains.

The stop was settled every time.
A cut-over while a Gemini call is on the wire orphans the call and halts the
evaluation phase, which happened at 10:03 UTC on 2026-09-17.
So the script reads the evaluation requests in flight from the ledger store,
waits for 0, then signals the process itself and waits for it to leave, rather
than let systemd kill it: the running unit carried the 90 s default
`TimeoutStopSec`, and that default killed the evaluator mid-trial at 07:25:55
UTC on 2026-09-17.
The evaluator left at a trial boundary each time, after 45 to 130 s.
Every unit this task started carries `TimeoutStopSec=600` and `nice 10`.

The new launcher also lowers
`ARCTIC_QA_OPERATION_LOCK_LOG_SECONDS` to 0.05 s, so every section of the
exclusive operation lock is in the unit's log and the hold is measurable.
It changes what is printed and nothing else.

## The wave that left an arm idle

The pick-up order of the evaluator is the oldest accepted question first, and
a paused arm bends it.
Every question the arm held stays open and keeps its place at the front of the
queue, so when the pause lifts a wave of them fills every slot, and the arms
that already finished those questions idle.

The captain paused the Claude arm at 06:39 UTC and resumed it at 10:11 UTC.
That left 33 questions that owed Claude alone at the front of the queue.
All eight slots took them, and the ledger holds no paid Gemini call between
10:03 and 11:06 UTC while 23 questions owed the Gemini arm trials.
The slow arm idled while the fast one worked, which is the opposite of what
eight questions in flight are for.

Each wave now keeps a share for every arm that has a question to give it:
`ceil(item_workers / vendors)` each, the arm with the fewest open questions
served first, and the oldest question of that arm first.
The rest of the wave fills in the pick-up order, so no question is held back
and the order inside every group is the pick-up order.
The first wave after the cut-over:

```
{"event":"wave_mix","open_by_vendor":{"anthropic_claude_code":8,"google_gemini":7,"openai_codex":7},"slots":8}
```

Seven of the eight questions owed the Gemini arm trials, against none of the
eight an hour earlier.

## What the live windows do and do not show

The numbers in "Answer first" are of the ledger itself: one read, one start,
one compaction, timed directly. They stand on their own.

The rate of the Gemini arm is a different claim, and this task cannot make it.
Three windows were taken, and the producer's load is different in every one.
`measure-phase-rate.py` reads both phases out of the ledger and
`producer-load.txt` holds the reading:

```
11:05-11:12Z (before, 09314ae):  away_production 6.65/min,  evaluation 3.96/min
11:20-11:33Z (after,  e46dd7f):  away_production 0.54/min,  evaluation 8.38/min
12:34-12:45Z (after,  f121d93):  away_production    0/min,  evaluation 2.27/min
```

The before window carried a busy producer and the after window an almost idle
one. That difference alone can account for the change, so the pair proves
nothing about this task's code, and an earlier draft of this report that read
it as a 2.1-times gain was wrong.

The third window says the same thing from the other side. It holds no
construction row at all and is slower than both, at 4 to 7 s of hold per
section. The producer process was running through it, replaying its run
directory after its 12:18 UTC relaunch: that costs CPU and disk for about
twenty minutes and writes no ledger row. The 50-crew measured what such
contention does to a hold, and it is the same mechanism
(`research/arctic-ch3-concurrency-50-r1/report.md`).

So the honest statement of this task is: the ledger work is measured, the
throughput claim is not.

### The measurement that is still owed

The captain's order stops generation at 13:45 UTC. After that the producer
makes no paid call, and a producer-idle window costs nothing. Take twenty
minutes of it and compare against `before-09314ae.txt`, which is the only
window with the old code:

```
cd <the arctic-qa worktree>
nix develop -c python research/arctic-eval-ledger-section-r1/measure-locks.py \
  "<HH:MM local start>" "<HH:MM local end>" 1.0
nix develop -c env PYTHONPATH=src python \
  research/arctic-eval-ledger-section-r1/measure-gemini-calls.py \
  /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  <START>Z <END>Z
nix develop -c env PYTHONPATH=src python \
  research/arctic-eval-ledger-section-r1/measure-phase-rate.py \
  /mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json \
  <START>Z <END>Z
```

`journalctl --since` takes local time and the ledger takes UTC, which is why
the two differ above. The third command is the control: it must report no
`away_production` row in the window, and the before window's 6.65 a minute is
what the old reading carried. The hold per paid call is the total held of the
first command over the call count of the second, at the same 1.0 s threshold
the old unit logged at.

## What the night cost the arm, and what it did not

Three defects took the Gemini arm down after the first cut-over, and each one
is a stop that reserved nothing, submitted nothing and charged nothing:

- the ambiguous-charge resume ran once a poll cycle, and a cycle is four
  waves. The arm was dark from 11:31:26 UTC on a charge the ledger had
  already released. Every admission asks now.
- a full concurrency slot or a full minute window paused the whole arm. The
  broker names those two as the refusals that describe the moment and not the
  request. They joined `ITEM_SCOPED_REASONS` after they took the arm down at
  11:55 UTC, minutes after it became fast enough to fill a slot.
- a `BrokerOperationBusyError` raised in the frame around the plan was the
  question's error, and one question's error halts the wave and ends the
  watch. The unit exited 1 at 12:14:20 UTC with one such error per question of
  the wave. It is contained against the question now.

The first two were found by watching the live arm after a cut-over. The third
was found by the supervisor, from the unit's exit, and not by this task.

## What is left

The evaluator still builds one broker per question, and that start still costs
the store's own load, the money validation of the whole ledger and the
signature of every row: about one second at 15,347 rows, under the shared
ledger lock. A seeded start removed the receipt proof behind those signatures,
which was the large half, and left this.

The change that removes the rest needs no shared state at all: stop building a
broker per question. The only thing that differs per question is the
evaluation gate, and a gate passed per request rather than held on the broker
would let one broker serve the whole run. That is a money-path interface, so
it wants its own task and its own review, not a night under a deadline.

Two further things this night showed and did not fix.

The producer exits on `the paid-call broker is halted` even when the automatic
continuation of `54695f1` lifts that halt moments later; it did so three times
before 12:12 UTC. Telling a halt the continuation owns from a halt that needs
a supervisor is that rule's own business, and what ends a run is a reviewed
boundary (`streaming.RUN_ENDING_LEDGER_STOPS`).

A question whose arm stopped inside it keeps its partial trials and is not
reopened by a later pass, which is the reviewed rule. Under a storm of busy
locks that is a question burned: the arm lost every question of the wave
between 12:13 and 12:14 UTC that way. The rule is right when a stop is rare;
it wants revisiting if the stop is common.
