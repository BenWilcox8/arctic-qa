# What the dataset looks like at 13:00 and 14:00 UTC

Measured on 2026-09-17 on the live streaming evaluator, with 8 questions in flight.
The captain asked for this at 08:35 UTC: "Extrapolate current rates to determine if this is feasible with current rates".

## Answer first

No.
About 112 questions will be complete on all three arms at 13:00 UTC and about 126 at 14:00 UTC, out of 151 accepted now and up to 301 accepted by then.

The Gemini arm sets that number, and no plan of the night moves it.
Every paid Gemini call costs about 22 seconds of the one exclusive section of the shared paid-call ledger, so the arm runs at 2.7 calls a minute, which is 13.7 questions an hour.
That cost is the ledger's own size, not the producer's traffic: it was measured while the producer made one paid call in four minutes.

The plan decides the share, not the count:

| Plan | Accepted at 14:00 UTC | Complete on all three arms | Share |
| --- | --- | --- | --- |
| Generation runs to 13:45 UTC, the captain's order of 09:14 UTC | 301 | 126 | 42 percent |
| Generation stops at 11:00 UTC | 191 | 126 | 66 percent |
| Generation stops at 10:00 UTC | 151 | 126 | 83 percent |

Generation after about 11:00 UTC adds accepted questions that no arm will finish before the deadline.
The one lever that lifts the count is the exclusive section of the ledger, and 10:00 UTC on a deadline is the wrong moment to change it.

## Two measurements

The first window covers the backlog.
The second covers fresh questions, which is the steady state that the projection needs.

| | Window 1 | Window 2 |
| --- | --- | --- |
| Time | 09:21 to 09:40 UTC | 09:44 to 09:55 UTC |
| Length | 19.1 minutes | 11.1 minutes |
| Questions in flight | 8 | 8 |
| Work | the backlog of open arms | 8 questions never scored before |
| Gemini trials a minute | 0.84 | 2.6 |
| Codex trials a minute | 6.4 | 9.0 |
| Claude trials a minute | 0 (paused until 10:11 UTC) | 0 (paused until 10:11 UTC) |
| Questions complete in the window | 7 Codex, 2 Gemini | none finished inside the window |

Window 1 measures the Codex arm and nothing else.
The Claude arm is held by the captain's own pause until 10:11 UTC.
The Gemini arm had almost no work: 71 of the 79 questions with a journal row already held their 12 Gemini trials.

Window 2 measures every arm that runs.
No question finished inside it, and that is the wave working as designed: 8 fresh questions need 96 Gemini trials together, which is 37 minutes at the measured Gemini rate.
Wall time per question is not the rate.
The trials a minute are, and they convert exactly: 12 Gemini trials and 18 trials of each subscription vendor make one question.

No trial failed in either window.
No vendor was paused, and the log holds no HTTP 429 and no HTTP 503.

## What bounds each arm

| Arm | Trials a question | Measured | Questions an hour | What bounds it |
| --- | --- | --- | --- | --- |
| `google_gemini` | 12 | 2.6 calls a minute | 13.7 | the exclusive section of the shared paid-call ledger |
| `anthropic_claude_code` | 18 | paused until 10:11 UTC | 31 expected, 40 at the policy | its own pace in the evaluation policy |
| `openai_codex` | 18 | 9.0 calls a minute | 31 | its own pace in the evaluation policy |

The two subscription arms are paced by the evaluation policy at 3 calls in flight and 12 a minute each, which is 40 questions an hour.
The Codex arm reached 78 percent of that.
Nothing else on this machine touches those two paces, because each subscription vendor keeps its own ledger and its own file lock.

The Gemini arm is different, and this is the finding of the measurement.
Every paid Gemini call takes the one exclusive operation lock of the shared paid-call ledger three times, and the three holds add up:

```
sections 70  held 514.0 s  waited 1907.0 s        (11.1 minutes of wall time)
  reserve             27 calls  waited 1041.3 s  held 239.6 s  mean hold 8.87 s
  count_registration  20 calls  waited  262.2 s  held 166.5 s  mean hold 8.32 s
  orphan_recovery     23 calls  waited  603.5 s  held 107.9 s  mean hold 4.69 s
```

One paid Gemini call takes that section three times and holds it for about 22 seconds in total.
60 divided by 22 is 2.7 calls a minute, and the arm measured 2.6.
The arm is exactly lock-bound, and the four Gemini threads of the wave queue behind each other: they waited 1907 seconds of thread time in 667 seconds of wall time, and the lock was held for 77 percent of the window by this one process.
The ledger held more than 10,600 requests and a 38 MB journal, on the rotating USB data disk.

The cost is the ledger's own size and not the producer's traffic.
The window measured six evaluation rows and no construction row in its last four minutes, with the holds above.
So stopping the producer does not lift this arm, and neither does any number of questions in flight.

This is not new work of the wave.
The serial evaluator scored 17 questions an hour, which is 3.4 Gemini calls a minute: the same ceiling, measured a day earlier on a smaller ledger.
The wave lifted the subscription arms from 17 to 31 questions an hour and left the Gemini arm where it was.
`research/arctic-ch3-concurrency-50-r1/report.md` holds the measurement of that exclusive section, and the four rules that already took one warm ledger read from 371 ms to 12 ms.

The three sections say where the 22 seconds go.
The reservation and the count registration each revalidate the immutable events they have not seen and list the receipts directory again, and both costs grow with the ledger.
`count_registration` held the lock for 0.65 seconds in the first window and 8.32 seconds in this one, which is the growth inside one hour.

## The projection

Assumptions:

- The producer accepts 40 questions an hour (38 measured over the hour before the window, 48 over the last 30 minutes at 75 workers, 31 to 35 over longer windows).
- The Claude arm starts at 10:11 UTC and reaches the measured Codex rate of 31 questions an hour.
- The Gemini arm holds 13.7 questions an hour, whatever the producer does.
- The Codex arm holds 31 questions an hour.
- At 10:00 UTC: 151 accepted questions; 71 complete on the Gemini arm, 52 on the Claude arm, 73 on the Codex arm, and 44 on all three.
- No quota floor fires. The guard reads 17 percent of the Claude 5-hour window, 64 percent of the Claude 7-day window, 45 percent of the Fable weekly window and 19 percent of the Codex weekly window. The 5-hour window resets at 10:11 UTC.
- Evaluation spend is USD 18.79 of the authorized USD 200, and the guard extrapolates USD 99.88 for the whole run. Money is not a bound tonight.

Generation runs to 13:45 UTC, which is the captain's order of 09:14 UTC:

| | 13:00 UTC | 14:00 UTC |
| --- | --- | --- |
| Accepted questions | 271 | 301 |
| `google_gemini` complete | 112 | 126 |
| `anthropic_claude_code` complete | 139 | 170 |
| `openai_codex` complete | 166 | 197 |
| complete on all three arms | 112 (41 percent) | 126 (42 percent) |

Generation stops at 11:00 UTC:

| | 13:00 UTC | 14:00 UTC |
| --- | --- | --- |
| Accepted questions | 191 | 191 |
| `google_gemini` complete | 112 | 126 |
| `anthropic_claude_code` complete | 139 | 170 |
| `openai_codex` complete | 166 | 191 |
| complete on all three arms | 112 (59 percent) | 126 (66 percent) |

Generation stops at 10:00 UTC:

| | 13:00 UTC | 14:00 UTC |
| --- | --- | --- |
| Accepted questions | 151 | 151 |
| `google_gemini` complete | 112 | 126 |
| `anthropic_claude_code` complete | 139 | 151 |
| `openai_codex` complete | 151 | 151 |
| complete on all three arms | 112 (74 percent) | 126 (83 percent) |

The backlog inside those numbers: 27 questions owe the Claude arm 486 trials, which is 40 minutes of that arm at its pace, and they are the first work it takes at 10:11 UTC.
6 questions owe the Codex arm and 8 owe the Gemini arm.

## The levers, in the order they pay

1. Stop generation by about 11:00 UTC.
   It needs no code and no reviewed file: it is the producer's settled stop, which the captain already plans for 13:45 UTC.
   It does not raise the count of complete questions, because the Gemini arm is the wall.
   It raises the share from 42 to 66 percent, and every question it does not accept is a question no arm would have finished.
   The captain asked for 300 to 400 questions "determined by the cost and the time": the time gives about 126 questions on all three arms.
2. Make the exclusive section of the ledger cheaper for an evaluation call.
   This is the one lever that lifts the count, and it is worth about 5 questions an hour for every 5 seconds it takes off the 22.
   `arctic-ch3-concurrency-50-r1` owns that measurement and the four rules it already applied.
   Two candidates are visible in the numbers above: the reservation and the count registration each revalidate the immutable events and list the receipts directory, and a compaction of the 38 MB journal would cut what they read.
   Neither is a change to make at 10:00 UTC on a deadline, and both touch the money path of a live run.
3. Accept an unbalanced dataset.
   Every accepted question can carry the Codex arm by 14:00 UTC under a plan that stops generation by 11:00 UTC, and about 170 can carry the Claude arm.
   The Gemini arm covers about 126.
   The per-model tables of the cost summary already read that way: each model has its own trial count, and a question missing one arm is not a question missing from the dataset.
4. Raise the subscription pacing in the evaluation policy.
   The limit of 3 calls in flight and 12 a minute per subscription vendor is bound by hash into the streaming authorization and every derived gate, so raising it needs a new policy file, a new authorization and a new gate directory.
   It lifts the two arms that are not the wall, so it pays nothing tonight.

## How to measure this again

The rates come from the cost journal and the unit log, and nothing here made a paid call of its own.

```bash
# Questions complete per arm over a window, and the questions in flight.
python research/arctic-eval-parallel-items-r1/measure-rates.py 2026-09-17T09:44:00Z
# Trials per model, the states, and any 429 or 503, from the unit log.
research/arctic-eval-parallel-items-r1/measure-trials.sh 04:44:00
# The exclusive section of the ledger, per section kind.
python research/arctic-eval-parallel-items-r1/measure-locks.py
# The three tables above, from the rates and the assumptions.
python research/arctic-eval-parallel-items-r1/project-deadlines.py
```

The four scripts are records: they hold the absolute paths, the item counts and the assumptions of this machine at 10:00 UTC on 2026-09-17.
The journal time of `measure-trials.sh` and `measure-locks.py` is local time, which was UTC minus five hours.
`research/arctic-eval-parallel-items-r1/report.md` holds what changed in the code and why.
