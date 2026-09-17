# The exclusive ledger section of the streaming evaluator

The Gemini arm of the streaming abstention evaluator was bound by the one
exclusive operation lock of the shared paid-call ledger, and not by the model.
`research/arctic-eval-parallel-items-r1/extrapolation.md` measured the bound at
09:44 to 09:55 UTC on 2026-09-17: three sections of one paid Gemini call held
that lock about 22 s in total, which is 2.6 calls a minute and 13.7 questions
an hour, against about 300 accepted questions to score before 14:00 UTC.

This record holds where those 22 s went and what this task removed.

## Answer first

The exclusive section went from 10.56 s to 4.04 s per paid Gemini call, and
the arm from 19.8 to 40.8 questions an hour, measured live across the
cut-over of 11:16 UTC. The goal was under 2 s, and "What is left" says where
the rest of it is.

Two costs put the evaluator there, and neither is the money proof itself.

| What | Measured | After |
| --- | --- | --- |
| One ledger read, receipts fingerprint moved | 0.21 to 0.26 s | 0.026 s |
| One broker start | 3.93 s | 3.93 s at the first, about 2.6 s after it |
| The start compaction, under the shared ledger lock | 1.16 s a question | once a process |
| The duplicate materialization of the identity check | 0.19 s a question | removed |

Every number above was measured on the live chapter 3 ledger on 2026-09-17,
at 13,309 request rows, a 18 MB snapshot, a 41 MB journal and 52,716 receipt
files, on an idle machine with a warm page cache.
The live evaluator shares one interpreter and one rotating USB disk with the
producer, which multiplies each of them: the 50-crew measured the same
multiplier at 0.206 s a call at 32 paper threads and 0.319 s at 50, for the
same 26 ms of real work
(`research/arctic-ch3-concurrency-50-r1/report.md`).

## The first cost: the evaluator kept the exact receipts listing

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

## The second cost: the evaluator started the ledger once a question

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

## The cut-over

Snapshot `e46dd7f`, successor authorization
`streaming-eval-r11-authorization-e46dd7f.json`, review record
`streaming-eval-r11-review-e46dd7f.md`, unit `arctic-abstention-stream-r3`,
work directory `abstention-eval/streaming-r11`, at 11:16:08 UTC.
`resnapshot-evaluator.sh` is the script.

An earlier snapshot of the same branch, `8477fd5`, ran from 10:57:36 to
11:05:32 UTC and was replaced while the two crews of the night both held the
unit. The Gemini arm was idle for the whole of those eight minutes, so that
snapshot measured nothing; the captain's supervisor then gave the unit to this
task alone.

The stop was settled.
A cut-over while a Gemini call is on the wire orphans the call and halts the
evaluation phase, which happened at 10:03 UTC on 2026-09-17.
So the script reads the evaluation requests in flight from the ledger store,
waits for 0, then signals the process itself and waits for it to leave, rather
than let systemd kill it: the running unit carried the 90 s default
`TimeoutStopSec`, and that default killed the evaluator mid-trial at 07:25:55
UTC on 2026-09-17.
The evaluator left at a trial boundary after 130 s.
The new unit carries `TimeoutStopSec=600` and `nice 10`.

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

## The live measurement

The cut-over was at 11:16:08 UTC.
The before window is the evaluator on 09314ae, which carries neither change;
the after window is 11:20 to 11:33 UTC, which leaves out the first four
minutes, because a process start pays one cold broker start and that is now
once a process rather than once a question.

The two windows do not share a log threshold.
The unit on 09314ae kept the 1.00 s default, so its log holds no section under
that; the launcher of this snapshot lowers it to 0.05 s.
The like-for-like line reads the after window at 1.00 s as well, and the
before number is therefore an undercount of the hold, never an overcount.
`section-hold.txt` and `gemini-call-rate.txt` hold both readings.

| | Before, 09314ae | After, e46dd7f |
| --- | --- | --- |
| Exclusive section held per paid Gemini call | 10.56 s | 4.04 s |
| Waited for that section per paid call | 39.2 s | 9.5 s |
| Paid Gemini calls a minute | 3.96 | 8.15 |
| Questions an hour on the Gemini arm | 19.8 | 40.8 |

At the 0.05 s threshold, which sees every section, the after window holds
4.54 s per paid call.

The arm is 2.1 times faster, and 3.0 times faster than the 13.7 questions an
hour that `research/arctic-eval-parallel-items-r1/extrapolation.md` measured
at 09:55 UTC.
The goal of the task was under 2 s of exclusive section per call, and 4.04 s
is not that.
The next section says where the rest of it is.

The ledger stayed valid across the cut-over and the window: not halted, no
integrity record, and the evaluation phase running.
At 11:31:26 UTC the Gemini arm took a vendor pause of its own kind, an
ambiguous charge on an interrupted request, which the reviewed rule of
54695f1 continues through and which resumes the vendor on the next poll. That
pause is not of this change: the settled stop of the cut-over held the
evaluation requests in flight at zero and the evaluator left at a trial
boundary, fifteen minutes earlier.

## What is left

The broker start still proves every row of the ledger against its receipts:
2.29 s of the 3.93 s, at 13,309 rows, under the shared ledger lock, once a
question.
It is the largest ledger cost the evaluator still pays per question.

The proof is a pure function of the ledger bytes and the receipt files, so one
process could keep it and every broker of the same ledger could adopt it, as
the producer's one broker already does across its 75 threads.
Every `_validated_ledger` call sits inside `self._ledger_lock()`, and that lock
is an `flock` taken through a new open file description each time, so it
serializes the threads of one process exactly as it serializes two processes.
The state to share is the store, `_immutable_events_proved`, its context and
its clock, `_ledger_evidence_proved`, `_custody_proved`,
`_accepted_events_proved` and the receipts listing.

This task did not do it.
Most of those are rebound rather than mutated in place, so sharing them means
moving them onto one object and reaching them through properties, and a defect
in that object raises a false integrity halt, which stops the producer and the
evaluator together.
That is not a change to make three hours before a deadline, with the producer
live.
The alternative that needs no shared state is to stop building a broker per
question: the only thing that differs per question is the evaluation gate, and
a gate passed per request rather than held on the broker would let one broker
serve the whole run.
