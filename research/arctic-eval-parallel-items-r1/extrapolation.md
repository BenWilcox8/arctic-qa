# What the dataset looks like at 13:00 and 14:00 UTC

Measured on 2026-09-17, on snapshot `82612f5` of the streaming evaluator, with 8 questions in flight.
The captain asked for this at 08:35 UTC: "Extrapolate current rates to determine if this is feasible with current rates".

## Answer first

No.
The whole accepted set will not be complete on all three arms by 14:00 UTC.
About 130 of an expected 310 accepted questions will be complete on all three arms at the measured rates, and about 200 at the policy ceiling.
The Gemini arm finishes every question it is given.
The two subscription arms are the bound, and the Claude arm is the tighter one because it restarts at 10:11 UTC.

Three levers can change that answer.
They are at the end of this file.

## The measurement

Window: 09:21:00 to 09:40:05 UTC, 19.1 minutes, the first window after the cutover.
The evaluator held 8 questions in flight for the whole window.
No trial failed, no vendor was paused, and the log holds no HTTP 429 and no HTTP 503.

| Arm | Questions complete at 09:21 | At 09:40 | Gained | Per hour | Trials a minute |
| --- | --- | --- | --- | --- | --- |
| `google_gemini` | 69 | 71 | 2 | 6.3 | 0.84 |
| `anthropic_claude_code` | 52 | 52 | 0 | 0.0 | 0.0 |
| `openai_codex` | 66 | 73 | 7 | 22.0 | 6.4 |
| all three arms | 44 | 44 | 0 | 0.0 | 7.2 |

Two of those three numbers are not capacity.

The Claude arm is paused by the captain's own order until 10:11 UTC, so it did no work at all.
The Gemini arm was idle for want of demand, not for want of capacity: of the 79 questions with a journal row, 71 already held their 12 Gemini trials, so the wave had almost no Gemini work to give it.
16 Gemini calls went out in the window, for USD 0.1857.

The Codex number is capacity.
The Codex arm ran without a pause for the whole window at 6.4 trials a minute, against a policy limit of 12 a minute: 53 percent of its ceiling.
The gap is the harness call itself (10 to 17 seconds each, 3 in flight) and the two durable writes that each call costs the subscription ledger on this disk.

Other numbers of the window:

- 19 passes over 19 questions, mean wall time 335 seconds, longest 841 seconds.
  A pass is longer than the 206 seconds of one question before the change, because 8 questions now share the same per-vendor slots.
  Wall time per question is not the rate; the trials a minute are.
- The producer accepted 38 questions in the hour before the window, and 147 are accepted in the campaign.
- 79 questions hold a journal row.
  68 accepted questions have never been scored.
- Evaluation spend: USD 18.79 of the authorized USD 200.
  The guard extrapolates USD 99.88 for the whole run, which is inside the budget.

## What the arms can do

A question is 48 trials: 12 Gemini, 18 Claude Code, 18 Codex.
The evaluation policy `arctic-abstention-evaluation-policy-v3-gemini-benchmark` paces each vendor, and a wave of 8 questions does not change that:

| Arm | In flight | Calls a minute | Questions an hour at the ceiling | Measured |
| --- | --- | --- | --- | --- |
| `google_gemini` | 4 | 30 | 150 | not tested by this window |
| `anthropic_claude_code` | 3 | 12 | 40 | paused until 10:11 UTC |
| `openai_codex` | 3 | 12 | 40 | 22 |

The two subscription arms hold the same limit for the same reason, which the policy states: both quotas are shared with the live agent crews on this machine.

Before the change the evaluator scored 17 questions an hour with every arm.
The Codex arm alone now runs at 22 an hour while the Claude arm is off, so the wave lifted the per-arm rate by at least 1.3 times, and the ceiling it can reach is 2.4 times the old rate.
The old design could not reach that ceiling at all: one question gave the Codex arm 18 trials of work in a 206-second pass, which is 35 percent duty by construction.

## The projection

Assumptions, all measured except where stated:

- The producer accepts 40 questions an hour (38 measured over the last hour, 48 over the last 30 minutes at 75 workers, 31 to 35 over longer windows).
- Generation stops at 13:45 UTC on the captain's order of 09:14 UTC, so the accepted set stops growing then.
- The Claude arm starts at 10:11 UTC.
- The Claude arm reaches the same duty as the measured Codex arm, which is 21 questions an hour, and its ceiling is 40.
- The Gemini arm holds 30 questions an hour, which is below its 150 ceiling and above the 17 an hour it reached inside the old serial design.
- Nothing is paused by a quota floor. The guard reads 17 percent of the Claude 5-hour window, 64 percent of the Claude 7-day window, 45 percent of the Fable weekly window and 19 percent of the Codex weekly window. The Claude 5-hour window resets at 10:11 UTC.

At the measured rates:

| | 13:00 UTC | 14:00 UTC |
| --- | --- | --- |
| Accepted questions | 280 | 310 |
| `google_gemini` complete | 171 | 201 |
| `anthropic_claude_code` complete | 111 | 133 |
| `openai_codex` complete | 146 | 168 |
| complete on all three arms | 111 (40 percent) | 133 (43 percent) |

At the policy ceiling of each arm:

| | 13:00 UTC | 14:00 UTC |
| --- | --- | --- |
| Accepted questions | 280 | 310 |
| `google_gemini` complete | 280 | 310 |
| `anthropic_claude_code` complete | 165 | 204 |
| `openai_codex` complete | 206 | 246 |
| complete on all three arms | 165 (59 percent) | 204 (66 percent) |

The backlog inside those numbers: 27 questions owe the Claude arm 486 trials, which is 40 minutes of that arm at its limit, and they are the first work it takes at 10:11 UTC.
6 questions owe the Codex arm and 8 owe the Gemini arm.

## The three levers

1. Stop generation earlier.
   The Claude arm can finish about 204 questions by 14:00 UTC at its ceiling, counting the 52 it already holds.
   An accepted set of about 200 questions is therefore the largest set that can be complete on every arm at the deadline.
   At 40 accepted an hour the producer reaches 200 at about 11:00 UTC.
   This lever needs no code and no reviewed file: it is the producer's settled stop, four hours earlier than the captain's 13:45 UTC.
2. Raise the subscription pacing.
   The limit of 3 calls in flight and 12 a minute per subscription vendor lives in the evaluation policy, which the streaming authorization and every derived gate bind by hash.
   Raising it needs a new policy file, a new authorization and a new gate directory, and it spends the shared Claude and Codex quota faster.
   The captain's own floors then bite sooner: the Claude 5-hour window is at 17 percent now.
   Doubling the subscription pacing would take the all-arm count at 14:00 UTC from about 204 to about the whole accepted set, if the quota holds.
3. Accept an unbalanced dataset.
   The Gemini arm finishes every question it is given, and the Codex arm reaches about 80 percent of the accepted set at its ceiling.
   A dataset where every question has the Gemini and Codex arms, and two thirds have the Claude arm, is available at 14:00 UTC with no change at all.

## How to measure this again

The rates come from the cost journal and the unit log, and nothing here made a paid call of its own.

```bash
# Questions complete per arm over a window, and the questions in flight.
python research/arctic-eval-parallel-items-r1/measure-rates.py 2026-09-17T09:21:00Z
# Trials per model, the states, and any 429 or 503, from the unit log.
research/arctic-eval-parallel-items-r1/measure-trials.sh 04:21:00
# The two tables above, from the rates and the assumptions.
python research/arctic-eval-parallel-items-r1/project-deadlines.py
```

The three scripts are records: they hold the absolute paths and the item counts of this machine at 09:40 UTC on 2026-09-17.
The journal time of `measure-trials.sh` is local time, which was UTC minus five hours.
`research/arctic-eval-parallel-items-r1/report.md` holds what changed and why.
