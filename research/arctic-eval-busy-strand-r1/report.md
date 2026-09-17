# The busy lock that closed the question, and the arm a package upgrade turned off

Task `arctic-eval-busy-strand-r1`, 2026-09-17.
Base `main` 4547bbc. The code change is 7aca1b4, which is the snapshot the live
unit runs and the commit the successor authorization binds; this record is the
commit after it, on branch `fm/arctic-eval-busy-strand-r1`.
Live service: unit `arctic-abstention-stream-r3`, work directory
`/mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r11`.

## Question

Two defects stopped the streaming abstention evaluator from finishing its questions.

1. Questions closed with fewer than their 48 responses whenever the exclusive
   operation lock of the shared paid-call ledger was busy.
2. The Claude arm made no call after 13:21 UTC although no model was paused and
   the quota was not spent.

## What the busy lock did

The containment that shipped with 88d2384 stopped a `BrokerOperationBusyError`
from ending the unit. It did not stop it from ending the question.

The refusal still reached the vendor as a stop. `abstention_plan.run_vendor`
set its stop flag on it, dispatched no further trial of that vendor, and
raised. `run_plan` recorded the raise as the vendor's error, and the watch read
that error through `vendor_stop_reason` and emitted
`vendor_stopped_on_this_item`.

In the three hours to 14:30 UTC the unit journal holds 87 `item_done` events
with `complete: false` against 16 with `complete: true`, and each of the 58
vendor stops of that window is the busy lock on the Gemini arm.

The event was not the damage. The journal row was.
`abstention_cost.cost_row` sets `evaluation.complete` from the paused models and
the paused vendors alone:

    "complete": int(pending_paused_trials) == 0 and not (vendors_paused or [])

A busy lock is neither, so the row said the question was complete while its
`recorded_trials` was far below its `planned_trials`.
`CostJournal.row_is_complete` reads that flag, `completed_item_ids` closed the
question, and no later pass ever took it up.

The top-level `row["complete"]` was `False`, which is what the `item_done`
event prints. Nothing reads it. That is why the journal and the events
disagreed for three hours and the disagreement told nobody anything.

## What stopped the Claude arm

`watch-state.json` at 14:28 UTC:

    "paused_vendors": {"anthropic_claude_code": {"reason":
      "failed: the harness exited with None: FileNotFoundError: [Errno 2]
       No such file or directory: '/home/ben/.npm-global/bin/claude'"}}

The binary at `/home/ben/.npm-global/bin/claude` was reinstalled at 12:06 UTC.
The path was gone for a moment. `SubprocessTransport.run` catches the spawn
`OSError` and returns it as an exit with no return code, the trial was recorded
as a failed response, and the vendor was paused for the rest of the invocation.
The unit journal holds that pause at 12:06, 12:13, 12:48 and 13:19:39 UTC, and
the same shape at 00:26, 06:36, 08:10, 11:05, 11:13 and 11:48 UTC.

The binary was back within the minute each time. Nothing asked again, because a
vendor pause lifts only on a start. The last Claude trial of that invocation is
at 13:21 UTC, which is the calls that were already in flight at 13:19:39.

The captain-owned pause file is not the cause. Its three `resume_at_utc` values
are 2026-09-17T10:11:00Z, all passed, and the arms did score between 10:11 and
13:21 UTC. `item_done` reporting `models_paused: []` is consistent with that.
The wave picker is not the cause either: `wave_order` gives every arm a share
of every wave, and it cannot give a share to an arm that is not in `vendors`.

## The fix

One rule covers both. A refusal that reaches the trial *before the provider
sees the request* reserved nothing, submitted nothing and charged nothing, so it
proves nothing about the model and nothing about the next trial. The no-retry
rule of the evaluation policy is about a call the provider answered and does not
reach it.

`abstention_plan.PRE_PROVIDER_REFUSALS` is that closed set of two:
`errors.BrokerOperationBusyError` and the new `errors.HarnessUnavailableError`.

- `run_vendor` waits such a refusal out inside the trial: four attempts with a
  doubling backoff from 5 s to a 60 s ceiling. Past the bound the trial is left
  pending. Nothing is recorded, the other trials of that vendor keep running,
  and nothing stops.
- The harness half is a probe that runs before the trial reserves its ledger
  row, and the transport owns it: `SubprocessTransport.probe` asks,
  `ScriptedSubscriptionTransport.probe` starts no process and answers by doing
  nothing. So a vanished binary now reserves nothing and records nothing.
- A pending trial is owed. The vendor summary carries `deferred_trials` and
  `deferred_reasons`, the plan summary sums them, and the journal row carries
  `evaluation.pending_deferred_trials`, which both `evaluation.complete` and
  `CostJournal.row_is_complete` read. The next pass runs exactly the missing
  trials.
- An arm that a vanished binary already paused lifts its own pause:
  `resume_vendors_whose_harness_returned` probes the binary the authorization's
  subscription registry names, before every question the evaluator admits, and
  resumes the arm with a `vendor_resumed` event and a `vendor_resume` journal
  row. It is the sibling of `resume_gemini_if_released`.

No money rule moves. Nothing is retried after a provider answered, no receipt is
replayed, no not-submitted request is resumed and no ledger row is edited. The
waits are outside every exclusive section, because the refusal they wait out is
precisely the failure to take one.

## The questions already stranded

Measured at 14:59:51 UTC with the unit stopped and settled, over the last row of
each question, which is how `CostJournal.latest_item_rows` reads the journal.
The file is `stranded-questions-20260917T1459Z.json`, which also lists the 89
question ids.

| Measure | Value |
| --- | --- |
| Questions with a journal row | 195 |
| Read complete by `row_is_complete` | 171 |
| Still open | 24 |
| Closed but below 48 responses | 89 |
| Trials never asked | 1290 |
| Recorded of 48, mean / min / max | 33.5 / 9 / 47 |

Trials never asked, by model:

| Model | Missing |
| --- | --- |
| gemini-3.7-flash | 255 |
| gemini-3.8-flash | 241 |
| claude-sonnet-5 | 224 |
| claude-opus-5 | 205 |
| claude-fable-5-1 | 190 |
| gpt-5.6-terra | 63 |
| gpt-5.6-sol | 60 |
| gpt-6-astra | 52 |

29 of the 89 owe the Gemini arm alone; the other 60 owe a subscription arm as
well.

These questions are not reopened. Reopening one is a contract change, because
the recorded stop inside it needs a judgement first, and that judgement is the
captain's.

### What a bounded reopen would look like

A reopen is one reviewed operation over the journal, not a code change, and it
needs four bounds.

1. **Scope.** Only questions whose missing trials have *no recorded response
   row*. A trial with a recorded non-completed row was answered by nothing but
   still went out; re-asking it is the retry the policy forbids. A trial with no
   row at all never went out. Of the 1290 missing trials, all are of the second
   kind, because a recorded row counts toward `recorded_trials`.
2. **The record.** Append one `reopen` row per question that names the question,
   the trials it owes per model, the reason the question closed, and the
   reviewer. Never edit a row. `row_is_complete` then reads the later row, which
   is the mechanism a paused model already uses.
3. **The money.** The Gemini half is 496 trials. At the measured rate of this
   run, USD 26.627095 over 1681 recorded Gemini trials, that is about USD 8,
   inside the USD 200 authorization and needing no transition. The 794
   subscription trials cost USD 0 and spend quota, so they need the captain's
   quota decision and not a ledger one.
4. **The rate.** Reopen in waves the evaluator can absorb, oldest question
   first, so the reopened backlog never starves the live questions the producer
   is still accepting. `wave_order` already gives every arm a share of each
   wave.

The precondition for all of it is this snapshot: without it the reopened
question is closed short again the first time the lock is busy.

## What this task did not fix

### The stop that kills the harness mid-call

`systemctl --user stop` sends SIGTERM to the whole control group, because the
transient unit ran at the systemd default `KillMode=control-group`. That reaches
the Claude Code and Codex child processes. The transport reports the killed
child as an exit (143 at 14:59:20 UTC, -15 at 14:54:24 UTC), the trial is
recorded as a failed response, the arm stops inside that question, and the
question is closed at its partial count. Each stop of this task did that to the
eight questions it had in flight: 16 of the 89 stranded questions were closed by
this task's own two stops.

The evaluator already holds the other half of the contract: SIGTERM sets a flag
that `run_vendor` reads before it dispatches each trial, so the trials it has
not started stay pending. The missing half is that the trials already in flight
must be allowed to finish.

The unit therefore runs at `KillMode=mixed` from 15:02:24 UTC, which sends
SIGTERM to the main process alone and leaves the children to finish inside the
`TimeoutStopSec=10min` the unit already carries. The launcher records it. This
is an operator property and not a code change.

The rule underneath it is the captain's: a question an arm stopped inside is
closed at its partial count, and `evaluation.complete` is true although
`recorded_trials` is below `planned_trials`. That predates this task.

### The gap between the probe and the spawn

The probe runs before the trial reserves its row; the child process is started a
moment later, after the invocation is built and the row is submitted. A
reinstall that lands inside that window still takes the path away, the spawn
still fails, and the trial is still recorded as a failed response.

It happened once under this snapshot. The Claude Code binary was reinstalled
again at 15:06 UTC, and at 15:09:55 UTC the arm was paused on a fresh spawn
failure: the response row carries `resumed: false` and `code_commit: 7aca1b4`,
so it is not an old receipt replayed.

The containment held, which is the point of the second half of the fix. The
`vendor_resumed` event is stamped the same second, 15:09:55 UTC, and the next
question started at 15:09:56 UTC with all three arms. What was a dead arm for
two hours is now a pause of under a second and one burnt trial.

Closing the window itself needs the spawn failure to unwind the reserved
subscription-ledger row, which is a change to that ledger's contract rather
than to the evaluator, so it is reported and not made here.

The reinstalls themselves are outside this repository. The binary at
`/home/ben/.npm-global/bin/claude` was replaced at 09:31, 12:06 and 15:06 UTC
on 2026-09-17. Whatever keeps the harness up to date should hold the old path
until the new one is in place, or the evaluator should be told to pause over
the upgrade.

### The transient reservation refusal, which is the remaining strand path

The paid-call concurrency slots and the minute window of the evaluation phase
are the broker's other two refusals that "describe the moment, not the
request". `execute` waits `TRANSIENT_RESERVATION_RETRY_SECONDS` for room and
then writes an immutable `not_submitted` receipt. That receipt is a recorded
response, so the vendor stops inside the question and the question is closed at
its partial count, exactly as the busy lock did.

It happened twice in the first wave of eight questions after the relaunch, at
15:14:45 and 15:15:15 UTC. The arm was not paused either time, because the
reason is in `ITEM_SCOPED_REASONS`, but both questions were closed short. It is
now the dominant strand path, and it is more frequent than it was, for the
reason the previous snapshot's record already predicted: an arm that is no
longer losing its trials to the busy lock is fast enough to fill the slots.

This one is not fixed here, and deliberately so. Unlike the busy lock it leaves
a receipt, and `GeminiBrokerEvaluationProvider.answer` replays a receipt it
finds, so the trial is refused again on every later pass. Re-running the request
key instead means `_resume_not_submitted`, which requires a reviewed
configuration transition that is the direct successor of the one the request was
refused under. That is a money-governance path and the captain's to open, so
this task reports it rather than automating it.

The producer already carries the reader half of the same rule
(`broker_provider.invoke` skips a receipt whose reason is in
`RESUMABLE_NOT_SUBMITTED_REASONS`). Giving the evaluator that half is the shape
of the fix, and it needs the reviewed transition to exist before it can work.


## The confirmation window

Fifteen minutes from the relaunch, 15:02:24 to 15:18:24 UTC, one wave of eight
questions.

| Measure | Three hours before | This window |
| --- | --- | --- |
| Busy-lock occurrences | 58 vendor stops | 0 |
| `item_done` complete true | 16 | 5 |
| `item_done` complete false | 87 | 3 |
| Claude trials | none after 13:21 UTC | 192 |

The Claude arm is scoring again: 192 trials, after making no call at all between
13:21 UTC and the relaunch. It was paused once inside the window, at 15:09:55
UTC, on a fresh spawn failure from the 15:06 UTC reinstall, and `vendor_resumed`
is stamped the same second. The next question started at 15:09:56 UTC with all
three arms. The Gemini arm ran 89 trials. The Codex arm ran none, because the
`wave_mix` event records that none of the eight questions of this wave owed it
any.

Of the three questions that closed short, one is the harness spawn race and two
are the transient reservation refusal. Neither is the busy lock, which is absent
from the record.

The unit runs with `KillMode=mixed`, `TimeoutStopSec=10min` and `Nice=10`.

## Guards

- `tests/test_abstention_plan.py`: the bounded wait, the trial left pending, the
  vendor that keeps running, the next pass that finishes it, and the probe that
  refuses a binary that cannot be started.
- `tests/test_abstention_watch.py`: the question that stays open end to end, the
  reader that never calls such a row complete, the arm that comes back when its
  binary does, and the pause that is told apart from every other pause.
- `tests/test_abstention_subscription.py`: the probe that reserves nothing and
  records nothing, and the same trial running once the binary is back.

## Artifacts

| File | What it holds |
| --- | --- |
| `stranded-questions-20260917T1459Z.json` | The settled count, the per-model missing trials and the 89 question ids. |

The reviewed successor authorization, its review record and the launcher are
`streaming-eval-r11-{authorization,review,launcher}-7aca1b4.*` under
`/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-authorization-r8/`.
