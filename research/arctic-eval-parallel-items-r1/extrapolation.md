# What the dataset looks like at 13:00 and 14:00 UTC

Measured on 2026-09-17 on the live streaming evaluator, with 8 questions in flight.
The captain asked for this at 08:35 UTC: "Extrapolate current rates to determine if this is feasible with current rates".

## Answer first

The whole accepted set will not be complete on all three arms by 14:00 UTC, and the size of the set is now a choice.

About 170 questions is the most that can be complete on every arm by 14:00 UTC, whatever else changes.
The Claude arm sets that number: it restarts at 10:11 UTC on the captain's own order and it scores about 31 questions an hour.

The accepted set is what the plan decides:

| Plan | Accepted at 14:00 UTC | Complete on all three arms | Share |
| --- | --- | --- | --- |
| Generation runs to 13:45 UTC, the captain's order of 09:14 UTC | 301 | 137 | 46 percent |
| Generation stops at 11:00 UTC | 191 | 170 | 89 percent |
| Generation stops at 10:00 UTC | 151 | 151 | 100 percent |

Generation after about 12:00 UTC adds accepted questions that no arm will finish before the deadline.
Stopping it earlier also makes the Gemini arm faster, because the producer and the evaluator share one exclusive section of the paid-call ledger.

## Two measurements

The first window covers the backlog.
The second covers fresh questions, which is the steady state that the projection needs.

| | Window 1 | Window 2 |
| --- | --- | --- |
| Time | 09:21 to 09:40 UTC | 09:44 to 09:59 UTC |
| Length | 19.1 minutes | 15 minutes |
| Questions in flight | 8 | 8 |
| Work | the backlog of open arms | 8 questions never scored before |
| Gemini trials a minute | 0.84 | 2.9 |
| Codex trials a minute | 6.4 | 9.4 |
| Claude trials a minute | 0 (paused until 10:11 UTC) | 0 (paused until 10:11 UTC) |
| Questions complete in the window | 7 Codex, 2 Gemini | none finished inside the window |

Window 1 measures the Codex arm and nothing else.
The Claude arm is held by the captain's own pause until 10:11 UTC.
The Gemini arm had almost no work: 71 of the 79 questions with a journal row already held their 12 Gemini trials.

Window 2 measures every arm that runs.
No question finished inside it, and that is the wave working as designed: 8 fresh questions need 96 Gemini trials together, which is 33 minutes at the measured Gemini rate.
Wall time per question is not the rate.
The trials a minute are, and they convert exactly: 12 Gemini trials and 18 trials of each subscription vendor make one question.

No trial failed in either window.
No vendor was paused, and the log holds no HTTP 429 and no HTTP 503.

## What bounds each arm

| Arm | Trials a question | Measured | Questions an hour | What bounds it |
| --- | --- | --- | --- | --- |
| `google_gemini` | 12 | 2.9 calls a minute | 15 | the exclusive section of the shared paid-call ledger |
| `anthropic_claude_code` | 18 | paused until 10:11 UTC | 31 expected, 40 at the policy | its own pace in the evaluation policy |
| `openai_codex` | 18 | 9.4 calls a minute | 31 | its own pace in the evaluation policy |

The two subscription arms are paced by the evaluation policy at 3 calls in flight and 12 a minute each, which is 40 questions an hour.
The Codex arm reached 78 percent of that.
Nothing else on this machine touches those two paces, because each subscription vendor keeps its own ledger and its own file lock.

The Gemini arm is different, and this is the finding of the measurement.
Every paid Gemini call takes the one exclusive operation lock of the shared paid-call ledger three times, and holds it for about 12 seconds in total:

```
sections 56  held 262.4 s  waited 1316.3 s        (6.9 minutes of wall time)
  reserve             21 calls  waited 795.2 s  held 174.4 s  mean hold 8.31 s
  orphan_recovery     19 calls  waited 414.5 s  held  77.6 s  mean hold 4.08 s
  count_registration  16 calls  waited 106.6 s  held  10.3 s  mean hold 0.65 s
```

The four Gemini threads of the wave therefore queue behind each other: they waited 1316 seconds of thread time in 414 seconds of wall time.
At 12 seconds of exclusive section per call the ceiling is about 5 calls a minute, which is 25 questions an hour, and the arm reached 2.9.
The ledger held 10,600 requests and a 33 MB journal at the time, on the rotating USB data disk.

This is not new work of the wave.
The serial evaluator scored 17 questions an hour, which is 3.4 Gemini calls a minute: the same ceiling.
The wave lifted the subscription arms from 17 to 31 questions an hour and left the Gemini arm where it was.
`research/arctic-ch3-concurrency-50-r1/report.md` holds the measurement of that exclusive section, and the four rules that already took one warm ledger read from 371 ms to 12 ms.

The producer writes to the same ledger.
A construction request appends a record, so the next evaluation reserve revalidates the immutable events it has not seen and lists the receipts directory again.
That is why the Gemini arm should reach its policy pace once the producer stops, and why the projection below reads the Gemini arm at 40 questions an hour after that moment.

## The projection

Assumptions:

- The producer accepts 40 questions an hour (38 measured over the hour before the window, 48 over the last 30 minutes at 75 workers, 31 to 35 over longer windows).
- The Claude arm starts at 10:11 UTC and reaches the measured Codex rate of 31 questions an hour.
- The Gemini arm holds 15 questions an hour while the producer writes to the ledger, and 40 after it stops.
- The Codex arm holds 31 questions an hour.
- At 10:00 UTC: 151 accepted questions; 71 complete on the Gemini arm, 52 on the Claude arm, 73 on the Codex arm, and 44 on all three.
- No quota floor fires. The guard reads 17 percent of the Claude 5-hour window, 64 percent of the Claude 7-day window, 45 percent of the Fable weekly window and 19 percent of the Codex weekly window. The 5-hour window resets at 10:11 UTC.
- Evaluation spend is USD 18.79 of the authorized USD 200, and the guard extrapolates USD 99.88 for the whole run. Money is not a bound tonight.

Generation runs to 13:45 UTC, which is the captain's order of 09:14 UTC:

| | 13:00 UTC | 14:00 UTC |
| --- | --- | --- |
| Accepted questions | 271 | 301 |
| `google_gemini` complete | 116 | 137 |
| `anthropic_claude_code` complete | 139 | 170 |
| `openai_codex` complete | 166 | 197 |
| complete on all three arms | 116 (43 percent) | 137 (46 percent) |

Generation stops at 11:00 UTC:

| | 13:00 UTC | 14:00 UTC |
| --- | --- | --- |
| Accepted questions | 191 | 191 |
| `google_gemini` complete | 166 | 191 |
| `anthropic_claude_code` complete | 139 | 170 |
| `openai_codex` complete | 166 | 191 |
| complete on all three arms | 139 (73 percent) | 170 (89 percent) |

Generation stops at 10:00 UTC:

| | 13:00 UTC | 14:00 UTC |
| --- | --- | --- |
| Accepted questions | 151 | 151 |
| `google_gemini` complete | 151 | 151 |
| `anthropic_claude_code` complete | 139 | 151 |
| `openai_codex` complete | 151 | 151 |
| complete on all three arms | 139 (92 percent) | 151 (100 percent) |

The backlog inside those numbers: 27 questions owe the Claude arm 486 trials, which is 40 minutes of that arm at its pace, and they are the first work it takes at 10:11 UTC.
6 questions owe the Codex arm and 8 owe the Gemini arm.

## The levers, in the order they pay

1. Stop generation earlier.
   It caps the pool and it frees the exclusive section for the Gemini arm, and it needs no code and no reviewed file.
   Stopping between 10:30 and 12:00 UTC keeps the largest set that can be complete on every arm, which is about 170 questions.
   The captain asked for 300 to 400 questions "determined by the cost and the time": the time gives 170 complete questions by 14:00 UTC, and the rest of the accepted set carries two arms of three.
2. Make the exclusive section of the ledger cheaper for an evaluation call.
   A Gemini call holds it for 12 seconds, of which 8.3 seconds is the reservation.
   This is the one change that lifts the Gemini ceiling while the producer runs, and `arctic-ch3-concurrency-50-r1` already owns that measurement.
   It is not a change to make at 10:00 UTC on a deadline.
3. Raise the subscription pacing in the evaluation policy.
   The limit of 3 calls in flight and 12 a minute per subscription vendor is bound by hash into the streaming authorization and every derived gate, so raising it needs a new policy file, a new authorization and a new gate directory.
   It would lift the Claude ceiling from 40 questions an hour, and it spends the shared Claude and Codex quota faster, so the captain's own floors bite sooner.
4. Accept an unbalanced dataset.
   Every accepted question can carry the Gemini and Codex arms by 14:00 UTC under any plan that stops generation by 11:00 UTC.
   The Claude arm covers about 170 of them.

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
