# Chapter 3 paper concurrency

The chapter 3 producer handled one paper and one model call at a time.
It made about 3 requests a minute and finished about 13 papers an hour.
The model answered in about 7 seconds; the other 12 seconds of each call were the pre-count, the ledger lock, the receipts and the gate check.

The captain's order of 2026-09-16 21:20 UTC is to make the concurrency work quickly, with the simplest architecture: keep the existing per-paper chain and give each paper its own thread.
A later addendum states that 4 papers in flight is the minimum target, not a ceiling.

## What the change is

### 1. Several papers at once

`run_stream` claims papers from the frozen selection order and runs each one on its own thread.
The per-paper chain is unchanged: eligibility, the source import, generation, routing, options, validation and persistence.

The pick-up order is the frozen order.
The completion order is not that order, so the run sorts `paper_results` back into the selection order before it returns.
Nothing else downstream reads the completion order: the ledger is keyed by request identity, and the state database is keyed by item, source and family.

A paper is claimed only while fewer than `--max-papers` papers have been claimed.
Every claimed paper adds exactly one to `processed`, on every path including the containment path, so the paper bound holds as it did one at a time.

A fault of one paper never reaches another.
`_contain_candidate_processing_fault` settles that family and the other threads keep working.
A run-ending error (`_ends_the_run`) stops the pick-up of new papers, lets the papers in flight finish and is then raised out of `run_stream` ahead of the export, as before.

### 2. Several option verdicts inside one paper

The option verifier calls of one paper run in waves.
The rank-order stop of `docs/STREAMING_DATASET.md` holds at the wave boundary instead of the call boundary: a wave is never wider than the options the target still needs.
A paper whose first wave all verifies buys exactly the calls it bought one at a time.

### 3. The broker admits one request at a time and calls concurrently

The evaluation phase already had the right shape, so construction now uses it.
Each request serialises its admission (orphan recovery, gate check, pace, count, reservation) behind one in-process admission lock, takes its own in-flight lock, releases the admission and only then makes the live call.

A construction request also holds the exclusive operation lock through its admission, because a reviewed operation of the ledger must not overlap the accounting of a paid request.
A concurrent construction request releases that lock with its admission, so one paper's live call never blocks another's.
Without `concurrent_construction` the lock is held for the whole call, exactly as before.

Orphan recovery already skips a submitted request whose in-flight lock is held, so a live sibling is never settled twice.

### 4. The shared pieces

- The sqlite state database keeps one connection and serialises every statement and every transaction behind one reentrant `Database.lock`. A commit on a shared connection is connection-wide, so two interleaved transactions would commit each other's half-written work.
- The progress file is written under one lock, and `last_error_stage` is per thread, because it belongs to the paper that raised.
- The counts, the paper results and the eligibility decision maps move under one tally lock.

### 5. The policy

`CHAPTER3_CONCURRENCY_CHANGE` moves `maximum_concurrent_generation_requests` from 2 to 8 and `maximum_generation_requests_per_minute` from 10 to 40, together and alone.
The two are one registered pair (`ALLOWED_REQUEST_RATES`), so no policy can raise one of them without the other.

The transition moves no money.
Every money ceiling, the per-request cap, the paper cost cap and every project design count stay exactly as the USD 200 expansion left them, and the transition names the same cumulative tranche of USD 253.990121.
The schema is not weakened: the change set is enumerated like every other one, and `_validate_policy` admits only the two registered rate pairs.

## The command

Both flags default to 4. `--phase offline` forces both to 1, because a scripted provider answers in script order.

```bash
python -m arctic_qa --json stream --phase away_production ... \
  --paper-workers 4 --option-workers 4
```

## The activation set

`build-concurrency.py` in this directory builds and cuts over the activation set, in the shape of `build-expansion.py` of `arctic-ch3-expansion-200-r1`:

```bash
python build-concurrency.py <commit> prepare
python build-concurrency.py <commit> apply-transition
python build-concurrency.py <commit> launch
python build-concurrency.py <commit> observe 900
```

`prepare` reads only.
`apply-transition` writes one immutable transition event and makes no paid call.
`launch` stops the live producer, waits for a zero-in-flight boundary and starts the new one under the same run id in tmux session `arctic-ch3-production-r1`.

The activation artifacts (the runtime snapshot, the gate, the transition, the launcher and the state) live under
`/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-paper-concurrency-r1/`,
because the ledger binds their absolute paths and this worktree is disposable.
`streaming-dataset-budget-policy-v11-chapter3-concurrency.json` in this directory is the record copy of the policy the run binds there.

## Measurement

### Before, one paper at a time

Read from the shared ledger's construction requests, best sustained 20-minute
window of the r3 run on 2026-09-16 after the lock-wait release:

| Window from | Requests | Requests a minute | Paper families | Families an hour | Calls a family |
|---|---|---|---|---|---|
| 19:45:09Z | 64 | 3.20 | 7 | 21.0 | 9.1 |

The captain's brief measured the same shape: about 14 calls and 4.5 minutes a
paper, about 13 papers an hour, about 3 requests a minute.

### After, four papers at a time

Not measured by this task. The captain's sequencing order of 23:16 UTC gave
the single relaunch to `arctic-ch3-paper-completion-r1`, which merges this
branch's tip and launches with the batch label applied once the ledger reads
`integrity_valid` true (it did at 23:18:02 UTC, after the evaluator worker's
reviewed settle of the phase-less row `52c5da75`).

To measure it after that launch, from this worktree or any checkout of the
branch:

```bash
D=/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-paper-concurrency-r1
nix develop -c python $D/build-concurrency.py <launched-commit> observe 900
```

`observe` waits for the free replay to end (the construction submission
count moves), then samples the shared ledger and `progress.json` every 30
seconds for 900 seconds and prints requests a minute, paper families an hour,
spend, accepted questions, the peak in-flight count and whether the producer
is still alive. It reads only. Compare against the baseline table above.

What to look for:

- `requests_per_minute` against the 3.20 baseline, and whether it sits at or
  below the v11 window of 40.
- `peak_inflight` against the v11 slot count of 8: four papers with option
  waves of four can reach it; a peak stuck at 1 means the admission is still
  serialised somewhere.
- Any `ambiguous_charge` receipt with `http_status` 429 in
  `model-receipts/`: that is Google's limit, and the number of them in the
  window says how hard it binds. None in 15 minutes means the policy caps,
  not the provider, are the bound at this concurrency.
- The replay time itself (`replay_seconds`): the first relaunch that runs the
  replay on four threads.

The captain's addendum asks whether four papers held steadily. `peak_inflight`
at or above 4 across the samples, with no 429 receipts, is the yes.

## The cut of 2026-09-16

Three refusals stood between the green suite and a running producer. None of
them moved money, and none was in the concurrency itself.

1. `apply-transition` looked for the predecessor event by name. An event file
   is named for the canonical hash of its authorization, while the ledger
   status reports the hash of the file, which is also what a successor names
   as its predecessor. The search is by content now.
2. `launch` flipped `activation_state` to `started` in the gate. The gate hash
   is bound into the transition authorization and into every receipt, so the
   runtime refused the ledger. The gate was restored to its bound bytes and is
   never rewritten again; every gate of this run keeps
   `authorized_not_started` for its life.
3. The producer started, replayed, and exited at 23:04 UTC with `the
   configuration transition ledger hash changed`. That one was a fault in the
   broker, not in the activation. Before its first construction request an
   applied transition is validated again on every start, and the ledger may
   only carry evaluation activity since the application.
   `_only_evaluation_activity_since` read that from the `phase` field of each
   row. A request refused before its reservation never records a phase: the
   count event creates the row and the reserve writes the phase. The
   production ledger held thirty-one evaluation rows of that shape, and the
   evaluator wrote another at 23:03:53 UTC. The tolerance now reads the stage
   family when the phase is absent, which `execute` already holds to agree
   with the phase. `tests/test_phase_scoped_slots.py` drives the real repeat
   refusal to produce a phase-less row and pins both directions.

The write side is untouched: a row still records its phase at the reserve and
not before. Recording it at the count event would fix new rows only, and the
thirty-one rows already on the production ledger need the read side to be
right anyway.

## Follow-up, not built here

A relaunched producer replays the papers its eligibility run directory already
holds before its first paid call. That replay is free but it cost about 22
minutes on every relaunch of 2026-09-16 (18:28 to 18:48, 18:58 to 19:19, and
the 21:53 cut had still not made a paid call at 22:10).

Two cheap ways to shorten it, in the order they are worth trying:

1. This change already runs the replay on `--paper-workers` threads, because
   the replay is the same per-paper chain reaching an already-settled result.
   Measure the replay of the first concurrent relaunch before building
   anything else.
2. If it is still long, cache the deterministic eligibility re-validation.
   `_validate_brokered_eligibility` recomputes the same verdict from the same
   job row and the same prompt, schema and policy hashes on every relaunch.
   A row keyed by the job key and those three hashes turns the recompute into
   a read. It changes no paid call and no decision.

Neither is built here; the captain asked for the concurrency first.
`arctic-ch3-paper-completion-r1` takes the startup skip and a per-paper
completion label on top of this branch, so item 2 is that task's, not a second
owner of the same idea.

A quiet moment for a relaunch is any moment the shared ledger reports
`inflight` 0: the producer is then between paid calls, and a paper boundary
follows within one call. `build-concurrency.py launch` waits for that boundary
itself before it stops anything.

## A test time bomb, found on the way

The bounded suite at `cae0172` failed five abstention pause tests that had
passed one hour earlier on the same code. They copied the captain's standing
pause literally, `resume_at_utc` 2026-09-16T23:00:00Z, as a hold that was
still in the future; the suite that ran after 23:00 UTC found it expired. The
run path reads the resume time against the wall clock, so a held-model test
now places its resume one day ahead of the real clock
(`future_resume_utc`, `held_fable_pause`), and the CLI status test asserts
that the live list agrees with the rule rather than that a dated entry is
live. `fable_pause` stays a faithful copy of the record, because one test
asserts exactly that against the committed file.

## Tests

`tests/test_paper_concurrency.py` runs four papers at once through `run_stream` and holds the result to the one-at-a-time run of the same four papers: the counts, the paper results in selection order, the accepted candidate rows and the export counts all match, and the meter proves the calls really overlapped.
