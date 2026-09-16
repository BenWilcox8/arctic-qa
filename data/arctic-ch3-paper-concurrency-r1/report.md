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

<!-- MEASUREMENT -->

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

## Tests

`tests/test_paper_concurrency.py` runs four papers at once through `run_stream` and holds the result to the one-at-a-time run of the same four papers: the counts, the paper results in selection order, the accepted candidate rows and the export counts all match, and the meter proves the calls really overlapped.
