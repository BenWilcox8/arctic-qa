# Several questions at once, and the captain's quota floors

Task `arctic-eval-parallel-items-r1`, 2026-09-17, branch `fm/arctic-eval-parallel-items-r1`.
Commits: `82612f5` (the wave and the quota floors), `60f216c` (one count per question in the guard), `8901214` (four waves a poll cycle) and `c6f1767` (a busy ledger lock belongs to one question).

## The question

The captain wrote at 08:35 UTC, before he went to sleep:
"I need to have all results FINISHED by around 8:00am this morning.
Extrapolate current rates to determine if this is feasible with current rates".
He wrote at 08:52 UTC: "Try to have everything finished by 9:00", which is 14:00 UTC.
The verbatim orders are in `data/arctic-chapter3/captain-sleep-order-20260917.md` of the firstmate data root.

At 08:35 UTC the streaming evaluator scored one question at a time: 206 seconds of wall time per question, about 17 questions an hour.
The producer accepted about 58 an hour.
The evaluator was the bound on the dataset, not the producer.

## What was measured before the change

One question cannot fill the slots that the evaluation policy allows.
Its 12 Gemini trials run 4 at a time.
Its 18 trials of each subscription vendor run 3 at a time.
So every vendor drains its wave of trials and then waits for the slowest vendor of that question.
The Gemini arm is the cheap fast one, and it spent most of each question waiting on a subscription arm.

## What changed

The evaluator now scores up to `--item-workers` questions at once, 8 by default.
Nothing of one question moves: its own evaluation set, its own derived gates, its own run directory, and one journal row per pass.
The per-vendor slots do not move either, because a slot belongs to the vendor and not to the question.
`docs/ABSTENTION_EVALUATION.md`, "Several questions at once", holds the design and its four rules.

Three records were corrected on the way.

A revisit of a question no longer pauses its vendor on an old recorded stop.
Nothing retries a stop, so the vendor summary field `stopped_on` names it for ever.
The evaluator reads the new `stopped_this_pass` instead.
A revisit is how a paused arm finishes the questions it owes, so the old reading would have taken the arm down again at the first revisit.

A busy exclusive operation lock of the shared ledger no longer pauses a whole arm.
That refusal reserves nothing, submits nothing and charges nothing, and the producer records the same one against one paper and goes on.
The evaluator read it as a vendor stop, and it took the Gemini arm down at 09:58 UTC until the unit was restarted.
A wave meets that lock far more often than one question did: four Gemini threads queue on it, and every paid call holds it three times.

The cost guard counted every journal row, and the evaluator appends one row per pass.
At 09:21 UTC the guard reported 128 questions evaluated where 78 questions had a row.
The cost per question was 0.146 where it was 0.239, and the Codex list-price equivalent of a revisited question was counted twice.
`benchmark_guard.latest_item_rows` is now the guard's half of the evaluator's own rule.

## The captain's quota orders of 2026-09-17

`docs/BENCHMARK_GUARD.md`, "The captain's quota order of 2026-09-17", holds the rules and quotes the orders.
The ChatGPT arms run to a 10 percent floor of the Codex weekly window, which is the reserve of the paper worker.
The Claude arms run to a 5 percent floor of the 5-hour window or the 7-day window, and resume at that window's reset.
Fable stops when its weekly usage reaches 80 percent, and it does not resume by itself.

The rule `codex_projected_exhaustion` is retired.
It paused `gpt-6-astra` at 08:29 UTC and `gpt-5.6-sol` at 08:34 UTC, at 23 percent of the weekly window remaining, hours before the floor.
A retired rule can never be clear again, so the hysteresis of a clear rule would hold its entries for ever.
The guard now removes them at once, and it also removes its own entries whose resume time has passed.

## The live run

The evaluator runs as user unit `arctic-abstention-stream-r3` with 8 questions in flight.
The cost guard and the corpus viewer read the same journal, and both now count one row per question.
Each snapshot has its own successor authorization, and each one binds the same five files, the same bounds and the same work directory as `87c69ce` did:

| Snapshot | Started | What moved |
| --- | --- | --- |
| `82612f5` | 09:20 UTC, again 09:44 UTC | the wave of 8 questions and the quota floors |
| `60f216c` | 09:25 UTC (guard), 09:37 UTC (viewer) | one count per question |
| `8901214` | 09:59 UTC | four waves a poll cycle |
| `c6f1767` | 10:03 UTC | a busy ledger lock belongs to one question |

The 75-worker crew stopped the evaluator at 09:38 UTC for its policy v15 transition, because a live evaluator breaks the revalidation of a fresh transition.
It came back at 09:44 UTC on the `82612f5` launcher, at the producer's first paid call under v15.

`extrapolation.md` holds the measured rates and the projection of the dataset onto 13:00 and 14:00 UTC.

## Tests

Module tests before the restart, and the adjacent files after it.
The captain's standing order of 09:01 UTC forbids the whole suite while the producer is live, so the release suite was not run.

- `tests/test_abstention_watch.py`: the wave of four questions, the questions in flight, a vendor pause inside a wave, a paused model that leaves every question of a wave open, the ceiling precheck against the questions in flight, the bound on the worker count, the four waves of one poll cycle, and the busy ledger lock that belongs to one question.
- `tests/test_abstention_plan.py`: a second pass reports no stop of its own.
- `tests/test_benchmark_guard.py`: the 5 percent Claude floors, the 20 percent Fable bound, the retired rule and its pause entries, an expired guard entry, and one count per question.
- Also run: `tests/test_live_benchmark_viewer.py`, `tests/test_abstention_run.py`, `tests/test_abstention_render.py`, `tests/test_abstention_broker.py`, `tests/test_abstention_subscription.py`, `tests/test_cost_call_plan.py`.
