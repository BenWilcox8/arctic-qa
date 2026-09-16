# Concurrent evaluation and streaming cost tracking

Task: `arctic-abstention-streaming-eval-r1`, branch `fm/arctic-abstention-streaming-eval-r1`, 2026-09-16.

The captain asked for two systems.
The first runs all eight evaluated models on one question as concurrently as possible.
The second runs that plan on every machine-accepted question as soon as the pipeline produces it, and tracks what each question costs.

Both are built, tested and live.
The Gemini arm of the first system ran live and stopped: gemini-3.7-flash returned a usage record without its answer-token count, so the broker booked an ambiguous charge and halted the shared ledger.
CAUTION: the live chapter 3 production run cannot make a paid call until a reviewed continuation settles that one request.
Section 7 holds the evidence, the cause and the two settlement paths. The choice is the captain's or firstmate's.

A third instruction arrived during the work, in the captain's words:
"Make sure that the options are always shuffled between every model call, even the same model with the same effort level".
Section 1.1 holds that change.
It moved the prompt contract to `abstention-eval-prompt-v2`, so the live proof ran again under v2.

A fourth instruction paused one model, in the captain's words:
"pause the fable evaluation because I only have ~80% fable usage left today; I will run the fable benchmarking after the reset at 6:00pm today".
Section 8.4 holds that change.
Claude Fable 5.1 is paused with a resume time of 2026-09-16T23:00:00Z, and the other seven models keep running.

## 1. The plan

`config/benchmark-evaluation-plan-high-v1.json` holds the captain's plan of 2026-09-16.

| Vendor | Models | Preset | Calls in flight |
| --- | --- | --- | --- |
| `google_gemini` | gemini-3.8-flash, gemini-3.7-flash | `thinkingLevel: high` | 4 |
| `anthropic_claude_code` | claude-fable-5-1, claude-opus-5, claude-sonnet-5 | `--effort high` | 3 |
| `openai_codex` | gpt-6-astra, gpt-5.6-sol, gpt-5.6-terra | `model_reasoning_effort=high` | 3 |

Eight models, two conditions, three repeats: 48 trials per question.
The plan file also carries k = 4, so the gold-present condition drops the last distractor of the frozen order, as the captain decided.

### 1.1 One option order per call

The option order seed bound the set, the item, the condition and the repeat.
Every model and every arm therefore saw one order per trial.
The seed now binds the model and the arm as well, so no two calls share an order.
The seed stays deterministic, and the trial, the response row and the receipt all record it.
The request key still binds the exact prompt bytes, so two calls never collide.

The prompt contract moved from `abstention-eval-prompt-v1` to `abstention-eval-prompt-v2`.
The evaluation set identity now includes the prompt hash, so a new contract freezes a new set directory instead of reusing an immutable manifest of the old contract.
A run refuses a set that another contract froze.
An older set stays readable, which keeps the scores of its own runs reproducible.
The live proof of section 5 ran again under v2.
The v1 runs stay on disk as superseded evidence.

## 2. Concurrency limits and why

The three vendors always run at the same time, in one thread each.
Inside a vendor a thread pool keeps N calls in flight.
N is the smallest of the plan value, the vendor limit in the policy file, and the `--concurrency` override.

| Vendor | Limit | Reason |
| --- | --- | --- |
| Gemini | 4 in flight, 30 per minute | The evaluation ceiling, not the quota, is the real bound: 12 calls per question at about USD 0.03 each. Four in flight keeps one question under 20 seconds and leaves the key headroom for the construction run, which paces itself at 10 per minute. |
| Claude Code | 3 in flight, 12 per minute | The claude.ai quota is shared with every agent session on this machine. One call takes 2 to 18 seconds at the high preset, so three in flight already covers the three Claude models. |
| Codex | 3 in flight, 12 per minute | The ChatGPT quota is shared the same way. One call takes 12 to 45 seconds at the high preset, so the three Codex models are the slowest arm and set the wall time of a question. |

`config/benchmark-evaluation-policy-v2.json` holds these limits.
It keeps every control of policy v1: the USD 5.00 evaluation ceiling, the USD 0.25 per-request cap, 8 calls per item and condition and model and arm, no retry, no fallback, no re-ask, no budget rearm, and stop on the first ambiguous charge.
The new `vendors` block holds the per-vendor concurrency and pace.
A gate binds the policy by hash, so the limits cannot change under a reviewed run.

## 3. Concurrent evaluation requests in the shared ledger

The shared paid-call ledger serialised every request with one exclusive operation lock.
That lock is the reason the old runner made one call at a time.
Four changes give the evaluation phase its own concurrency without touching the construction path:

1. **Admission, not the whole call.**
   A construction request still holds the exclusive operation lock from the first check to the last ledger write.
   An evaluation request never takes that lock.
   It holds an in-process admission lock for the recovery, the gate check, the pace, `countTokens` and the reservation, then releases it before the live call.
2. **One in-flight lock per request.**
   During the live call an evaluation request holds an `flock` on a file named after its request key.
   Orphan recovery skips a submitted request whose lock another process holds, so a live sibling is never settled as an ambiguous charge.
   A crashed holder releases the lock, and the next recovery settles that request exactly as before.
3. **Per-phase slots and window.**
   The concurrency check and the minute window now count the requests of the active phase only.
   The construction view is the ledger total minus the evaluation requests in flight.
   An evaluation submission no longer enters `recent_submission_times_utc`, so the construction pace of 10 per minute stays exact.
4. **Own-run recovery.**
   An evaluation admission recovers only its own run.
   A construction run of another run id can be inside a live call with its response already durable and its settlement pending.
   A second recovery of that request settles it twice and stops the live production run.
   The custody check that every terminal request has its immutable final receipt still covers every run.

Nothing else changed.
One immutable receipt per request key, the ceiling, the reserve, the per-request cap, the per-item repeat limit, the gate binding and the halt on the first ambiguous charge all work as before.

The subscription ledger gained the same shape: a per-vendor in-flight limit, a per-vendor pace, an in-flight lock per request, and a recovery that files a row left by a crashed process as `interrupted`.
It also refuses to reopen under a different evaluation policy or vendor.

### 3.1 Why point 3 is not optional

Point 3 looks like bookkeeping. It is the control that keeps the two phases apart.
The expansion crew's commit `3cd57a8` on `main` makes the slots per phase; this branch adds the per-minute window, which that commit left shared.
A shared window has the same failure mode one layer down: the evaluation limit is 30 per minute and the construction limit is 10, so evaluation calls alone could refuse a construction call on the pace.

The ledger keeps one `inflight` total for every phase.
The construction limit is 2 requests and the evaluation limit is 4.
So four evaluation calls in flight read as four construction requests in flight, and the construction phase then refuses its next call with "the paid-call concurrency limit is complete".

That happened in production on 2026-09-16.
The chapter 3 producer ran a snapshot of the code that counts `inflight` globally, while this branch ran four Gemini evaluation calls at once, and the producer could not launch.
Firstmate ordered both Gemini models paused at once, and the evaluator held them from that moment.

The per-phase counting is therefore a change the construction path needs, not only the evaluation path.
The expansion crew's branch carries that fix with its own tests, and firstmate released it to production, so this branch keeps their implementation rather than a second one.

## 4. Measured wall time

### 4.1 Dry run with the scripted transports

One question, 48 trials, per-vendor latencies set to one tenth of the measured medium-preset latencies (Gemini 1.42 s, Claude 0.35 s, Codex 0.72 s):

| Schedule | Wall time | Gemini | Claude | Codex |
| --- | --- | --- | --- | --- |
| Serial, one call at a time | 37.52 s | 17.66 s | 6.59 s | 13.24 s |
| Concurrent, 4 / 3 / 3 | 4.60 s | 4.58 s | 2.25 s | 4.45 s |

The speedup is 8.16x.
At the real latencies the same schedule is about 375 s serial against 46 s concurrent per question.

### 4.2 Live run with the two subscription vendors

`runs/concurrent-test-r2`, one chapter 2 item, 36 calls at the high preset, prompt contract v2:

| Measure | Value |
| --- | --- |
| Wall time | 120.1 s |
| Sum of the 36 call latencies (the serial time) | 452.9 s |
| Speedup | 3.77x |
| Claude arm | 76.4 s wall, 118.2 s serial |
| Codex arm | 119.9 s wall, 334.8 s serial |

The superseded v1 run `runs/concurrent-test-r1` measured 140.2 s wall against 493.4 s serial, a speedup of 3.52x.

The live speedup is smaller than the dry-run speedup because the latencies inside one vendor differ by a factor of ten.
Three calls in flight cannot hide a 45-second call behind a 2-second one.
The Codex arm sets the wall time of a question.

## 5. Live results

### 5.1 Deliverable 1: `runs/concurrent-test-r2`

One accepted chapter 2 item (`aqa-a2a7c66461c0f2b46cf0`), evaluation set `abstention-eval-set-139624de40a5ced7`, k = 4, both conditions, three repeats, the `high` preset, prompt contract v2.
Six models, 36 of 36 calls complete, no invalid response.
Gates, review record and launcher: `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/private/concurrent-test-r2-*`.

| Model | N0 | N1 | N2 | N3 | N4 | N5 | Mean latency |
| --- | --- | --- | --- | --- | --- | --- | --- |
| claude-fable-5-1 | 0 | 2 | 0 | 1 | 0 | 3 | 12.4 s |
| claude-opus-5 | 0 | 3 | 0 | 0 | 1 | 2 | 5.0 s |
| claude-sonnet-5 | 0 | 3 | 0 | 0 | 2 | 1 | 2.3 s |
| gpt-6-astra | 0 | 3 | 0 | 0 | 3 | 0 | 22.0 s |
| gpt-5.6-sol | 0 | 3 | 0 | 0 | 3 | 0 | 15.5 s |
| gpt-5.6-terra | 0 | 2 | 1 | 0 | 3 | 0 | 18.3 s |

Invalid rate: 0 of 36.
Cost: USD 0 (subscription).
The list-price equivalent of the 36 calls is USD 0.71.

The 36 trials carry 36 distinct shuffle seeds and 35 distinct option orders.
Two of the 36 draws landed on one permutation by chance, which five options and 120 permutations make likely.
The seeds differ, so each call shuffled on its own, which is what the captain ordered.

On this one item claude-fable-5-1, claude-opus-5 and claude-sonnet-5 abstained in part of the gold-absent condition.
The three Codex models chose a distractor every time.
Three repeats of one item are not a result.
They show that the plan, the gates, the ledgers, the receipts and the scorer work end to end at the high preset.

### 5.2 Deliverable 2: the streaming evaluator, live

The evaluator ran against the live chapter 3 state database with backfill off, under prompt contract v2.
It evaluated every accepted chapter 3 item that the pipeline had produced, and then one more that landed while it ran.
Four items, 130 of 144 planned trials, one invalid response.

| Item | Trials | Wall time | Generation family USD | Family paid calls |
| --- | --- | --- | --- | --- |
| aqa-7f09e4bdf6bac5c50d4c | 36 | 102.4 s | 0.251680 | 15 |
| aqa-52dee70307a8186522bf | 36 | 79.3 s | 0.146654 | 13 |
| aqa-9d5b8cc412f835cc5201 | 22 | 161.5 s | 0.225114 | 19 |
| aqa-7e056efad24ee1a12f0c | 36 | 113.3 s | 0.174210 | 13 |

The third item holds 22 trials instead of 36.
The Claude arm stopped on its first call with `FileNotFoundError` on `/home/ben/.npm-global/bin/claude`.
An npm update of that package replaced the symlink for a moment, and one call hit that window.
The behaviour was correct: the harness error stopped the Claude arm, the Codex arm finished its 18 trials, the journal row records the stop and `complete: false`, and no call was retried, because the policy forbids a retry.
The fourth item ran both arms again, which confirms the cause was transient.
This run produced one change: a vendor that stops on an item is now paused for the rest of the watch, with a pause row in the journal, whatever the reason.
Before that change, the next item repeated the same stop.

Work directory: `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r2`.
It holds the frozen one-item sets, the derived gates per item and per vendor, the plan runs and `cost-journal.jsonl`.
Every item carries one distinct shuffle seed per trial.

The cost summary of the four items:

| Measure | Value |
| --- | --- |
| Accepted items evaluated | 4 |
| Trials | 130 |
| Generation USD per item (that item's own paid calls) | 0.199414 |
| Campaign spend per accepted item (chapter 3, 4 accepted) | 0.617370 |
| Gemini evaluation USD per item | 0 (the Gemini arm is not live) |
| Subscription tokens per item | 55,575 input, 383 output, 12,727 thinking |
| Subscription list-price equivalent per item | 0.647082 |
| Wall time per item | 114.1 s |
| Projection for 500 items | USD 99.71 generation, USD 323.54 subscription list-price equivalent, 15.9 hours of evaluation |

Per-model outcomes over the four items, 18 to 24 trials each:

| Model | N0 | N1 | N2 | N3 | N4 | N5 | ACC | Abstention rate | SSR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| claude-fable-5-1 | 1 | 2 | 4 | 4 | 5 | 4 | 0.316 | 0.421 | 0.526 |
| claude-opus-5 | 0 | 3 | 4 | 3 | 6 | 3 | 0.316 | 0.316 | 0.474 |
| claude-sonnet-5 | 0 | 3 | 3 | 4 | 8 | 1 | 0.211 | 0.263 | 0.421 |
| gpt-6-astra | 0 | 4 | 2 | 6 | 6 | 6 | 0.417 | 0.500 | 0.667 |
| gpt-5.6-sol | 0 | 5 | 7 | 0 | 12 | 0 | 0.208 | 0.000 | 0.208 |
| gpt-5.6-terra | 0 | 8 | 4 | 0 | 12 | 0 | 0.333 | 0.000 | 0.333 |

Four items are not a paper result. Three observations are worth the large run:

- The two smaller Codex models never abstained in 24 trials each, at the high preset, as in the earlier medium-preset test.
- gpt-6-astra has the best accuracy and the best self-knowledge score of the six.
- One claude-fable-5-1 response was not a single letter. That is the only invalid response in 130 trials. The Claude path has no decoder constraint, which section 1.5 of the subscription report explains.

The superseded v1 run of the evaluator (`streaming-r1-smoke`, three chapter 3 items, 108 calls) measured USD 0.207816 of generation per item, 0.675692 of subscription list-price equivalent per item, and 121.5 s per item.
Those items ran with one shared option order per trial, so their outcome counts belong to v1 and not to the paper.

### 5.3 The service is running

The unit `arctic-abstention-stream-r2` ran at commit `ffecccc` and idled between items:

```
{"at":"2026-09-16T09:39:20Z","evaluated":0,"event":"idle","poll":1}
```

It polls the state database every 30 seconds.
The next accepted chapter 3 item starts a run inside one poll interval.
Section 8.2 holds the commands to watch it, to read its cost summary and to stop it.

That unit is stopped now, and its journal at `streaming-r2` holds five items.
The cost guard of task `arctic-benchmark-guard-site-r1` reads that journal, so the directory stays where it is.
The all-vendor run of section 5.4 keeps its own journal at `streaming-r3`.

### 5.4 Deliverable 2 on all three vendors, live

Run `streaming-r3`, 2026-09-16 11:26 to 11:29 UTC, one accepted chapter 3
question on all three vendors. This is the first streaming run with the Gemini
arm in it.

| Measure | Value |
| --- | --- |
| Item | `aqa-7f09e4bdf6bac5c50d4c`, family `family-b1a8778edee7540f6917` |
| Trials | 48 of 48, no invalid response |
| Wall time | 167 s for the whole plan, the three vendors in parallel |
| Gemini | 12 paid calls, USD 0.246684, 2976 input, 12 answer, 65174 thinking tokens |
| Subscription | 36 calls at USD 0, 56968 input, 360 answer, 13757 thinking tokens, USD 0.728966 at list price |
| Generation of this item | 16 paid calls across 8 stages, USD 0.278026 |
| Campaign so far | 301 paid calls, USD 3.700621, 5 accepted items, USD 0.740124 per item |

So one question costs about USD 0.28 to build and about USD 0.25 to evaluate on
the two Gemini models at the high preset, with the other six models free on the
two subscriptions. The high preset thinks hard: 65174 thinking tokens against
12 answer tokens in 12 calls.

CAUTION: this run called Claude Fable 5.1 six times although the standing pause
held it. Section 5.5 holds that incident. The 48 trials of this item are
therefore complete, and the six Fable rows are usable data.

### 5.5 The pause that did not hold, and what it cost

The r3 run was the first live run with both a paused model and the Gemini
broker active. It called the paused model.

The cause was a name collision in the watcher. Before each item the evaluator
reads the pause file into `pause`. Then, when the Gemini broker exists, it
runs a precheck of the Gemini ceiling and assigned that precheck's
vendor-pause record to the same name. The precheck found room, so it returned
`None`, and the model pause became `None` before the item ran.

The unit tests did not catch it, because the scripted watcher passes no broker
factory, so the precheck branch never ran in a test. That is the gap: the tests
covered the pause and covered the ceiling precheck, but never both at once.

Commit `0526795` gives the vendor-pause record its own name and adds a test
that drives the watcher through the precheck with a model paused. The test
fails on the old code and passes on the new code.

CAUTION: six Claude Fable calls are spent against the captain's daily quota,
which he asked to protect. They cannot be returned. The captain ordered the
pause at about 80 percent of his daily Fable usage, so this is a real cost
against a limit he named.

The lesson for this project: a control that is read in one place and consumed
in another needs a test that drives the real path, not the scripted one. The
pause is now proved on the path that has a broker.

### 5.6 The final live pass: all vendors, the pause holding

Run `streaming-r5`, 2026-09-16 13:06 to 13:13 UTC, at commit `f64d289` on
`main` `3f7de6e`. This is the pass that proves the corrected pause on the path
that has a broker.

| Measure | Value |
| --- | --- |
| Item | `aqa-7f09e4bdf6bac5c50d4c` |
| Trials | 42 recorded of 48 planned, no invalid response |
| Held | 6 trials of `claude-fable-5-1`, `complete: false` |
| Models scored | 7, with no Fable row in any vendor directory |
| Gemini | 12 paid calls, USD 0.227800, one call in flight |
| Subscription | 30 calls at USD 0: Codex 18, Claude Code 12 |
| Wall time | 391 s, against 167 s at four Gemini calls in flight |
| Ledger after | `integrity_valid true`, `halted false`, nothing in flight |
| Evaluation phase | USD 0.627848 used of USD 200.00, 42 requests |

The Gemini arm ran one call in flight, not the plan's four. The chapter 3
producer runs from a read-only snapshot, and the snapshot that would make its
next construction call could not be proved from the ledger: the applied
construction transition binds commit `09fd733`, which precedes the per-phase
commit `3cd57a8`. One evaluation call in flight leaves the producer a free slot
even under the older global counting, so this pass could not repeat the outage
of the morning. The cost is wall time only: 391 s against 167 s.

Firstmate confirmed that caution on 2026-09-16: the Gemini arm stays at one
call in flight until the producer is redeployed from a commit that carries
`3cd57a8`. After that redeployment the arm runs at the plan's four.

The two phases stayed apart, which is the point of section 3.1. Before the run
the construction minute window held 4 entries. After 12 evaluation calls it
still held 4. No evaluation submission entered it.

The journal row reads `complete: false` with `pending_paused_trials: 6`, so the
item stays open. After 23:00 UTC the next pass runs those six Fable trials and
calls no other model again.

## 6. The cost row

One journal row per question. The fields, with the first live v2 row as the example:

```json
{
 "item_id": "aqa-7f09e4bdf6bac5c50d4c",
 "family_id": "family-b1a8778edee7540f6917",
 "run_id": "abstention-stream-r2-aqa-7f09e4bdf6bac5c50d4c",
 "plan_id": "arctic-abstention-plan-8-models-high-3-repeats-v1",
 "generation": {
  "campaign_id": "arctic-qa-production-campaign-003",
  "family": {"paid_calls": 15, "usd": "0.251680",
             "calls_by_stage": {"answer_verification": 1, "blinded_reconstruction": 1,
                                "distractor_generation": 1, "eligibility": 2,
                                "finding_answer_extraction": 1, "option_verification": 5,
                                "question_generation": 2, "standalone_verification": 2},
             "tokens": {"input": 177001, "output": 8692, "thinking": 1041},
             "ledger_paper_spent_usd": "0.817951"},
  "campaign_spent_usd": "2.329904", "campaign_paid_calls": 224,
  "campaign_accepted_items": 4, "campaign_usd_per_accepted_item": "0.582476"
 },
 "evaluation": {
  "planned_trials": 48, "recorded_trials": 36,
  "google_gemini": {"billing": "api_credits", "charged_calls": 0, "usd": "0.000000",
                    "reserved_usd": "0.000000", "ambiguous_usd": "0.000000",
                    "tokens": {"input": 0, "output": 0, "thinking": 0}},
  "subscription": {"anthropic_claude_code": {"billing": "subscription", "charged_usd": "0",
                     "calls": 18, "tokens": {"...": "..."},
                     "list_price_equivalent_usd": "...", "by_model": {"...": "..."}},
                   "openai_codex": {"...": "..."}},
  "subscription_tokens": {"input": 61663, "output": 432, "thinking": 13917},
  "subscription_list_price_equivalent_usd": "0.741488",
  "charged_usd": "0.000000",
  "vendors_paused": ["google_gemini"],
  "wall_seconds": 102.35
 },
 "outcomes_by_model": {"claude-fable-5-1": {"N0": 0, "N1": 0, "N2": 0, "N3": 3, "N4": 0, "N5": 3}},
 "invalid_count": 0, "invalid_rate": 0.0,
 "cumulative": {"items": 0, "trials": 0, "generation_family_usd": "0.000000",
                "evaluation_gemini_usd": "0.000000",
                "subscription_tokens": {"input": 0, "output": 0, "thinking": 0},
                "subscription_list_price_equivalent_usd": "0.000000", "wall_seconds": 0},
 "complete": true,
 "run_dir": "...", "gate_dir": "...", "recorded_at_utc": "2026-09-16T09:15:21Z"
}
```

Two more row kinds exist.
A `vendor_pause` row records a paused vendor with the reason and the remaining USD.
A `skipped_item` row records an accepted item that the set builder excluded, with the reason.

The generation cost has two numbers on purpose.
`family.usd` is the exact spend of that item's paper family, read from the construction ledger.
`campaign_usd_per_accepted_item` is the campaign spend divided by the accepted items so far, which includes every rejected candidate and every paper that produced nothing.
The second number is the one to use for a budget, and it is 2.3 times the first on the chapter 3 data so far.

The list-price equivalent is information only.
Claude Code and Codex calls bill the subscriptions at USD 0.
The rates come from the two official price pages, read on 2026-09-16, and live in `config/benchmark-evaluation-list-prices-v1.json`, which no gate binds.
The Claude rate uses the native split of fresh input, cache writes and cache reads.
The Codex rate uses the split of fresh and cached input.
Both output rates include the thinking tokens, as both harnesses report them inside the output count.

## 7. The Gemini arm: a live stop, its cause and its repair

Local `main` reached `3a99a66` while this task ran, which merged the chapter 3
broker release. This branch is rebased onto that commit, so its broker can open
the production ledger. The Gemini arm of the plan then ran live.

The run stopped on an ambiguous charge, and that charge halted the whole shared
ledger, so the live chapter 3 production run could not make a paid call. It is
repaired. Commit `183779b` is on local `main` and holds three changes:

1. The usage rule accepts an omitted zero answer-token count, the shape
   gemini-3.7-flash returned, as well as the omitted thinking count it already
   allowed. Both are proved by the recorded total.
2. The reviewed usage reconciliation settles either shape from the saved
   response, with no provider call, and records which count the provider
   omitted.
3. An ambiguous evaluation charge halts the evaluation phase only, through
   `evaluation_halted` in the ledger. Construction continues under its own
   ceiling, so an evaluation error can never stop the producer again.

The one live request was reconciled at USD 0.005460 from its saved response.
The ledger then validated with `integrity_valid true`, `halted false` and no
request in flight. Sections 7.1 to 7.3 keep the evidence and the reasoning that
led to that repair.

### 7.1 What ran

`runs/concurrent-test-r2-gemini`, the same chapter 2 item and evaluation set as
section 5.1, both conditions, three repeats, the `high` preset, on
gemini-3.8-flash and gemini-3.7-flash. 12 trials planned.

| Measure | Value |
| --- | --- |
| Calls submitted | 8 of 12 |
| Calls completed | 7 |
| Spend | USD 0.087228 |
| Ambiguous charge | 1 call, USD 0.030851 reserved |
| Calls never submitted | 4, because the vendor stops on the first stop |
| Wall time to the stop | 112 s |

| Model | Calls | Spend | Cost per call | Thinking tokens per call | Latency |
| --- | --- | --- | --- | --- | --- |
| gemini-3.8-flash | 4 | 0.059694 | 0.014924 | 2220 (example) | 34 to 64 s |
| gemini-3.7-flash | 3 | 0.027534 | 0.009178 | 1282 (example) | 29 to 53 s |

The seven completed calls answered with one letter every time, and no response
was invalid. Six of the seven were gold-present trials and every one chose the
gold option. The one gold-absent trial of each model split: gemini-3.8-flash
abstained (N5), gemini-3.7-flash chose a distractor (N4).

The high preset costs about four times the medium preset that the first canary
measured: USD 0.0149 per call against USD 0.0061, because the models think for
about 2200 tokens instead of 470.

### 7.2 The cause

gemini-3.7-flash returned a usage record with no `candidatesTokenCount`:

```json
{"promptTokenCount": 174, "thoughtsTokenCount": 1421, "totalTokenCount": 1595,
 "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 174}],
 "serviceTier": "standard"}
```

The answer text was the single letter `C` with a `thoughtSignature`, and the
finish reason was `STOP`, so the call succeeded at the model. The total equals
the prompt count plus the thinking count, so the omitted field means zero
visible answer tokens, although the model returned one letter.

`docs/SHARED_MODEL_BROKER.md` states the rule: the provider can omit
`thoughtsTokenCount` when it is zero, and every other missing or inconsistent
usage value causes an ambiguous charge. The broker therefore raised
`ValueError: provider usage is inconsistent`, reserved the full amount, wrote
the immutable receipts, and halted. That is the designed fail-closed behaviour,
not a defect of this task.

The other two gemini-3.7-flash calls of the same run returned
`candidatesTokenCount: 1`, and the four gemini-3.8-flash calls all did. The
quirk is therefore intermittent and it appeared on the model that had no price
entry until this task added one.

### 7.3 What the settlement needed

The reviewed settlement paths in the broker were, before the repair:

- `settle-http-rejection`, for a provider rejection before generation. This
  request is not a rejection: the model answered.
- `reconcile_omitted_thought_usage`, for one saved response with the
  omitted-thoughts pattern. Its guard requires the ambiguous error
  `KeyError: 'thoughtsTokenCount'`, so it refuses this receipt.

No reviewed path covers an omitted `candidatesTokenCount`. The settlement
therefore needs a decision above this task, because it changes how the shared
ledger books money:

1. **A second reconciliation rule.** Extend the reconciliation to the
   omitted-candidates pattern: `candidatesTokenCount` absent, the other three
   counts nonnegative integers, and the total equal to the prompt count plus
   the thinking count. The cost then uses zero visible answer tokens, which is
   the reading the total already proves. This needs a reviewed repair commit
   and a private gate, exactly as the omitted-thoughts rule did.
2. **An ambiguous continuation.** The five earlier ambiguous charges of this
   ledger each carry a reviewed `ambiguous-continuation-*` event, which is why
   the ledger was not halted before this run. The same instrument can cover
   this request and keep the reservation as spend.

Either path is a money action for the captain or firstmate. Firstmate
delegated the settlement to this task and chose path 1. The rule is now the
`_omitted_zero_usage_field` check of the broker, the request is reconciled, and
the ledger is clean. The deployed runtime of the producer had refused the
reconciliation event, because its own rule demanded `thoughtsTokenCount == 0`;
that is why the repair had to land on `main` before the producer could restart.

### 7.4 What the run proved anyway

The last construction call of the chapter 3 production run settled at
10:00:07 UTC, inside the window of this run (09:59:38 to 10:01:30). The two
phases therefore ran paid calls on one ledger at the same time, which is what
section 3 set out to make safe:

- Four evaluation calls were in flight while the construction run held its own
  slot, and no request of either phase was settled twice.
- The construction minute window holds only construction submissions.
- Every evaluation request wrote one immutable receipt per request key.
- The halt came from the provider usage rule, which is shared by both phases,
  not from the concurrency change.

The Gemini arm of the streaming evaluator stayed out of the running service
until the repair landed, and the service kept evaluating the six subscription
models per question. With `183779b` on `main` the Gemini arm runs again, and
four trials of `runs/concurrent-test-r2-gemini` are still pending.

## 8. Operating instructions

Run every command from the repository root inside `nix develop`, with `PYTHONPATH=src python -m arctic_qa --json abstention-eval`.

### 8.1 One question on the whole plan

```bash
--action plan-gates --eval-set-dir <set-dir> --run-id <id> --output-dir <gate-dir> \
  --review-record <review-file> --plan-file config/benchmark-evaluation-plan-high-v1.json \
  --evaluation-policy-file config/benchmark-evaluation-policy-v2.json
```

A reviewer sets `independent_review_verdict` to `pass` and `evaluation_enabled` to `true` in each gate.
Then:

```bash
--action run-plan --eval-set-dir <set-dir> --run-dir <run-dir> --run-id <id> \
  --plan-gate-dir <gate-dir> --plan-file config/benchmark-evaluation-plan-high-v1.json \
  --evaluation-policy-file config/benchmark-evaluation-policy-v2.json \
  --subscription-ledger-root <root> [the shared-ledger options of the Gemini arm]
```

`--vendors` runs a subset.
`--concurrency google_gemini=2,openai_codex=1` lowers the calls in flight.
`--serial` runs one call at a time, which is the baseline of the wall-time comparison.
`--action score-plan --run-dir <run-dir>` scores every vendor of the run together.
`--action dry-run-plan` runs the whole plan offline with the scripted transports and `--latency-seconds` for a wall-time measurement.

### 8.2 The streaming evaluator

The live run is configured by three files under `/mnt/crdata/research-abstention/arctic-qa/abstention-eval/private/`:

- `streaming-eval-r2-review.md`, the review record.
- `streaming-eval-r2-authorization.json`, the reviewed authorization.
- `streaming-eval-r2-launcher.sh`, the launcher.

Start it as a systemd user unit, the pattern the live publication snapshot service already uses:

```sh
systemd-run --user --unit=arctic-abstention-stream-r2 \
  --working-directory=/home/ben/.treehouse/arctic-qa-e841b2/33/arctic-qa \
  /mnt/crdata/research-abstention/arctic-qa/abstention-eval/private/streaming-eval-r2-launcher.sh
```

| Action | Command |
| --- | --- |
| Watch the log | `journalctl --user -u arctic-abstention-stream-r2 -f` |
| Read the state | `systemctl --user status arctic-abstention-stream-r2` |
| Stop it | `systemctl --user stop arctic-abstention-stream-r2` |
| Read the cost summary | `--action cost-summary --work-dir <work-dir> --project-items 500` |

The unit logs one JSON line per event to the journal: `idle`, `item_started`, `item_done`, `item_skipped`, `vendor_paused`, `item_bound_reached`, `deadline_reached`.
A stop signal ends the loop after the current question, so no receipt is left open.
A restart re-reads the cost journal and never repeats a finished question.

The authorization bounds the run.
The unit idles between items and exits 0 when the item bound is reached, so an inactive unit with `item_bound_reached` in its log is a normal end, not a failure.
The authorization binds the code commit, so a restart after a new commit needs a regenerated and re-reviewed authorization.
Raise `maximum_items` in a new authorization when the captain wants more questions.
The item bound of the live run is 6, because both subscription quotas are shared with the live agent crews.
Four items are evaluated, so the running service takes two more as the pipeline produces them.
The review record holds the bound history.

### 8.3 Raising the evaluation ceiling

The evaluation ceiling is a money control, so a larger one needs a reviewed transition, chained on the applied predecessor, as a construction ceiling does.
The baseline is USD 5.00, the ceiling of the first two evaluation policies.

The captain allocated USD 200 on 2026-09-16 for benchmarking the Gemini models, and firstmate passed that allocation to this task.
The step is applied on the production ledger:

| Field | Value |
| --- | --- |
| Active policy | `config/benchmark-evaluation-policy-v3.json` |
| Ceiling | USD 5.00 to USD 200.00 |
| Event | `evaluation-policy-transition-7989c8d2...json` in the receipt directory |
| Applied | 2026-09-16T11:13:56Z |
| Review record | `private/evaluation-ceiling-200-review.md` |
| Evaluation spend | USD 0.153364 of USD 200.00, USD 199.846636 remaining |
| Ledger after | `integrity_valid true`, `halted false`, no request in flight |

v3 differs from v2 in three fields only: the ceiling, the policy id and the purpose.
Every operational control is identical, and a test proves that.

The broker accepts only a registered step.
`EVALUATION_CEILING_CHANGES` in `src/arctic_qa/model_broker.py` names exactly one: USD 5.00 to USD 200.00.
Another amount needs its own constant and its own review before a transition file can apply.
A policy with a ceiling at or below the authorized one needs no transition, because a smaller ceiling only tightens the control.

The transition binds one gate hash, and the streaming evaluator derives one gate per item, so the evaluator cannot be the first start under the larger ceiling.
The action `apply-evaluation-ceiling` is that first start: it builds the broker with the transition file and a gate of its own, applies the transition, writes the immutable event, and makes no paid call.
Every later start reads the event, and each new evaluation receipt records its hash in `evaluation_policy_transition_sha256`.

```bash
--action apply-evaluation-ceiling \
  --evaluation-policy-file config/benchmark-evaluation-policy-v3.json \
  --evaluation-gate-file <transition gate> \
  --evaluation-policy-transition-file <transition file> \
  --shared-ledger-file <ledger> --model-receipts-dir <receipts> ...
```

The "The evaluation ceiling" section of `docs/ABSTENTION_EVALUATION.md` holds the full field list and every check.

### 8.4 Pausing one model

Captain order 2026-09-16, in his words:
"pause the fable evaluation because I only have ~80% fable usage left today; I will run the fable benchmarking after the reset at 6:00pm today".

The pause is per model, not per vendor, so Claude Opus 5 and Claude Sonnet 5 keep running on the same Claude Code harness.
The shipped file `config/benchmark-evaluation-model-pause-v1.json` holds the order:

```json
{
  "schema": "benchmark-evaluation-model-pause-v1",
  "paused_models": {
    "claude-fable-5-1": {
      "reason": "the captain's daily Claude Fable quota is about 80 percent used",
      "resume_at_utc": "2026-09-16T23:00:00Z"
    }
  }
}
```

A paused model's trials are held.
They are not dispatched, not recorded and not counted as invalid, so the run's invalid rate does not move.
The item's journal row names the models in `evaluation.models_paused`, counts the held trials in `evaluation.pending_paused_trials`, and reads `evaluation.complete: false`.
The item therefore stays open.
The next pass after 23:00 UTC takes that item up again and runs only the six missing Fable trials, because the run directory keeps every recorded trial.
That pass appends a later row for the same item, and the totals read the last row of each item, so a revisited item is never counted twice.

The evaluator re-reads the pause file before every item.
So a pause or a resume takes effect on the next question, with no restart of the unit.
The resume time needs no second command: the file states it once and the evaluator honours it.

| Control | Effect |
| --- | --- |
| `config/benchmark-evaluation-model-pause-v1.json` | The standing pause of the captain. Re-read before every item. |
| `--pause-file <path>` | Another pause file. Repeatable; a later file wins for the same model. |
| `--no-pause-file` | Ignore every file. |
| `--pause-model MODEL[=RESUME_UTC]` | Pause one model for this invocation. It wins over every file. |
| `--action pause-status` | Show which models are held now, and which files it read. |

This is also the documented interface for the cost guard of `arctic-benchmark-guard-site-r1`:
the guard writes its own pause file to hold a model, reads `--action pause-status` to prove the pause is in force, and reads `cost-journal.jsonl` and `--action cost-summary` for the numbers.
`docs/BENCHMARK_GUARD.md` on `main` states the guard's half of that contract, and it matches this one.
The guard owns its file and marks its entries with `"owner": "benchmark-cost-guard"`, so `--pause-file` is repeatable: the launcher gives the committed file and the guard's file, and a later file wins for the same model.
An entry an operator wrote stays exactly as written.
The schemas are `benchmark-evaluation-model-pause-v1`, `abstention-eval-pause-status-v1`, `abstention-eval-cost-row-v1` and `abstention-eval-cost-summary-v1`.
The "Paused models" section of `docs/ABSTENTION_EVALUATION.md` holds the full contract.

## 9. Tests

| Part | Result |
| --- | --- |
| `tests/test_abstention_plan.py` (new, 11 tests) | passed |
| `tests/test_abstention_watch.py` (new, 14 tests) | passed |
| `tests/test_abstention_render.py`, `_run`, `_broker`, `_subscription` | 51 passed |
| `tests/test_model_broker.py`, `test_broker_provider.py` | passed |
| Whole suite | see section 9.1 |

The new tests cover:

- The plan file, its 48 trials per item, and the concurrency resolution from the plan, the policy and the override.
- The whole plan through the scripted transports, with a measured concurrent wall time under half the serial wall time.
- Resume: a second invocation makes no new call, and a run directory with another plan is refused.
- One vendor that stops on the first failure while the other two finish their 18 trials.
- One gate per vendor with the right models, arms, presets, harness versions and reviewer verdict.
- Eight concurrent evaluation calls on one ledger: 2 to 4 in flight, every receipt distinct, the ledger not halted, and the construction window untouched.
- An evaluation call that does not take a construction slot while a construction call runs on the same ledger.
- An evaluation admission that leaves another run's in-flight request submitted, and a construction call that still recovers it.
- The subscription ledger with three calls in flight, its per-vendor limits, and a row left by a crashed process filed as `interrupted`.
- The watcher: the contract selection, the idle pass, the first item, the restart that repeats nothing, the item bound, the backfill switch, the vendor subset, and the ceiling pause that keeps the subscription vendors running.
- The vendor pause: a harness failure pauses that vendor for the rest of the watch, the other vendor evaluates every item, and the pause row names the reason.
- The model pause: the pause record merges the file and the command line, the resume time frees the model with no edit, an invalid model or timestamp is refused, and the file needs its `paused_models` block.
- The paused model in a plan run: 42 of the 48 trials run, the six Fable trials stay pending, the invalid count stays zero, no recorded row names the paused model, and a pass after the resume time runs those six and re-calls nothing else.
- The paused model in the streaming evaluator: the item's row names the held model and counts its trials, the item is not complete, a second pass before the resume holds the trials again, the pass after it completes the item, the totals read one row per item, and the finished item is never evaluated again.
- The `pause-status` action: the standing pause, the command-line pause, `--no-pause-file`, and two pause files where the guard's entry wins for the same model and its resume time frees it.
- The model pause on the path that has a broker: the ceiling precheck of the Gemini vendor keeps its own record, so a paused model stays held while Gemini keeps its slot. This is the defect of section 5.5.
- The evaluation ceiling: the shipped v3 policy differs from v2 in the ceiling and its names only, the registered step is the only one, and the construction reserve still covers it.
- The applied ceiling transition: the larger ceiling is refused without it, the event is immutable and names its predecessor, a later start needs no file, a paid call binds the event hash, and a fork of the chain is refused.
- Fourteen refusals of an altered transition: an unregistered change set, another ledger, a changed ledger snapshot, another gate, a changed or absent source, a changed target, another predecessor, an absent commit or reason, an invalid time, an absent or changed review record, another schema, and a policy that also moves a control field. No refused attempt writes an event.
- The `apply-evaluation-ceiling` action: it writes the event, reports the authorized ceiling and the ledger state, makes no call, and a second run changes nothing.
- The cost journal arithmetic: the native token split of both list-price rates, the family and campaign generation cost, the Gemini evaluation cost of one item, the cumulative block, the summary, the projection, and the per-model metrics against the scorer.
- The live wiring of both vendor kinds in one plan run: the Gemini vendor on the shared broker and the Codex vendor on its own ledger, from one gate directory.
- The per-call option order: two models at one preset on one trial differ, the same model at two presets differs, and the three repeats of one model differ. Every order stays a permutation of the same option set, and the seed stays deterministic and recorded.
- The set identity: a new prompt contract freezes a new set, and a run refuses a set that another contract froze.

### 9.1 Whole test suite

`nix develop -c bash -c 'PYTHONPATH=src pytest tests -o addopts="" -q'`.

| Run | Result |
| --- | --- |
| Commit `ffecccc`, before the rebase | 1260 passed in 13 minutes 41 seconds |
| Commit `82ff5cc`, the restored branch | 1298 passed, 1 failed in 18 minutes 13 seconds |
| Commit `598c34b`, the branch on main `ec0ba98` | 1344 passed in 17 minutes 42 seconds |
| Commit `f64d289`, the branch on main `3f7de6e` | 1355 passed in 17 minutes 47 seconds |
| Commit `353ae0c`, the branch tip | 1355 passed in 17 minutes 43 seconds |

The one failure of the second run was the reviewed chapter 2 MAX_TOKENS continuation, whose case matches its receipt by the exact usage message.
The widened usage rule of `183779b` changed that message for a record that omits both token counts.
Commit `3c45273` restores it, and the expansion crew's commit `09fd733` widens the case to accept either message, so the two fixes compose.
That defect was on `main`, not only on this branch: it broke a money-settlement path, and no named suite of this task covered it.

The third run also found two tests of this task that read the committed pause file, which is operational: an operator or the cost guard edits it while a run goes on.
Commit `598c34b` gives those tests their own record and asserts the committed file once, for the captain's standing entry.

`ruff check src tests` passes. `ruff format --check src tests` passes on all 115 files.

## 10. The captain's instructions, in his words

Three instructions shaped this task. The captain's exact words:

1. 2026-09-16, the evaluation plan: "Note that for the model evaluation stage I will be testing all of the following models on high reasoning level, on each question, once with gold-answer present and once not three times (to get a statistical distribution): Gemini 3.8 Flash, Gemini 3.7 Flash, Claude Fable 5.1, Claude Opus 5, Claude Sonnet 5, ChatGPT Astra, ChatGPT 5.6 Sol, ChatGPT 5.6 Terra. Set up a system so that all 8 of these can run as concurrently as possible on each question so that evaluation doesnt take forever."
2. The same order, the streaming half: "After that is set up, set up another system that immediately runs each machine accepted question from the pipeline on The benchmark and after each question track how much all of the model calls etc cost so that I can determine if I need to modify the rigor of my evaluation or change my budget allocated to QA generation/evaluation."
3. 2026-09-16, during the work: "Make sure that the options are always shuffled between every model call, even the same model with the same effort level."
4. 2026-09-16, during the work: "pause the fable evaluation because I only have ~80% fable usage left today; I will run the fable benchmarking after the reset at 6:00pm today."

Firstmate passed the captain's allocation for the Gemini benchmarking: USD 200 for the evaluation ceiling. Section 8.3 holds that change.

## 11. Deferred items

1. The remaining 4 trials of the Gemini arm of `runs/concurrent-test-r2-gemini`. The ambiguous charge of section 7 is settled and the ledger is clean, so they can run.
2. A live pass at the plan's four Gemini calls in flight. Section 5.6 ran one, because the producer snapshot that will make the next construction call could not be proved to carry `3cd57a8`. A producer snapshot at `3cd57a8` or later removes that caution.
3. The evaluation ceiling of USD 200.00 is the captain's whole allocation. The streaming authorization keeps its own, much smaller, Gemini USD bound per run, so a large benchmarking pass needs a new authorization with a bound the captain sets.
4. The evaluator derives one gate per item from one reviewed authorization. A stricter design lets a reviewer sign a contract-level gate that the broker validates directly. That design needs a new gate schema and a broker change.
5. The scripted dry run uses one latency per vendor. A per-model latency models the real schedule better, because inside one vendor the models differ by a factor of ten.
6. `campaign_usd_per_accepted_item` divides the campaign spend by the accepted items of the whole campaign. A per-window number, over the last N items, reacts faster to a change in the pipeline yield.
7. The subscription vendors have no decoder temperature and a residual harness prompt of 700 to 2600 tokens. Section 1.5 of `data/arctic-abstention-subscription-providers-r1/report.md` holds the full fairness caveat. Nothing in this task changes it.
8. Two processes must not share one evaluation run id. The in-process admission lock covers the window between the reservation and the in-flight lock inside one process, and the file lock covers every later step. A second process on the same run id can see a request of that window as an orphan. One reviewed gate authorizes one run id and one launcher, so the case does not arise today. A cross-process admission lock closes it.
9. The v1 evidence stays on disk: `runs/concurrent-test-r1` and `streaming-r1-smoke`. Their outcome counts belong to the v1 contract, where every model of one trial shared one option order. Their cost and wall-time numbers do not depend on the order.
10. Both Gemini models were paused for about an hour on 2026-09-16, by firstmate's order, while the shared ledger counted in-flight requests globally. The evaluator kept the subscription vendors running throughout. The pause is lifted: `main` carries `3cd57a8` and this branch carries the per-phase window. Only Claude Fable 5.1 is held now, until 23:00 UTC.
11. A paused *vendor* stays paused until the operator restarts the unit. An automatic re-test after a quiet period needs a new control in the policy file. A paused *model* needs no restart: section 8.4 holds that control, and its resume time frees the model by itself.
12. `gemini-3.7-flash` now has a price entry (USD 0.75 input, USD 3.75 output including thinking, the rate valid through 2026-12-31, read from the official pricing page on 2026-09-16). Its presets are low, medium and high: the model page states that `minimal` returns an error.
