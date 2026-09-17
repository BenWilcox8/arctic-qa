# Chapter 3 paper completion labels

Captain order of 2026-09-16, 22:20 UTC:

> Label each paper that has been analyzed by the most recent iteration of the system as completed.
> Only analyze papers that have not been already analyzed from now on and label the ones that have in one batch.

The run is `chapter3-7dc6485-r3` on campaign `arctic-qa-production-campaign-003`.

## 1. The problem

The producer keeps no bookmark.
At every start it walks the frozen paper order from the first paper.
For every call it would make it looks for a completed receipt in the shared ledger, and it validates that receipt and the run authorization before it reuses the receipt.

That walk is free and CPU-bound, and it grew with the ledger.
Three relaunches of 2026-09-16 measured about 22 minutes between launch and the first paid call, at about 5,000 receipts:

| Launch | First paid call | Startup |
| --- | --- | --- |
| 18:28 UTC | 18:48 UTC | about 20 minutes |
| 18:58 UTC | 19:19 UTC | about 21 minutes |
| 21:53 UTC | about 22:15 UTC | about 22 minutes |

The eligibility half of the walk is the larger half.
`_validate_brokered_eligibility` reads the whole extraction of a paper, rebuilds the span manifest, rebuilds the request payload, hashes it, and reads the receipt.
It runs twice for each visited paper: once in `_trusted_brokered_eligibility_decisions` and once in the paper loop.
The generation half then replays every stored candidate path of each eligible paper.

## 2. What was built

A paper the run finished carries a durable label.

- **The record.** One row of `paper_completions` in the state database, schema `streaming-paper-completion-v1`, keyed by the invocation run id and the candidate key.
  The row records the campaign, the paper family, the source id, the outcome class, the eligibility decision, the first reason code, the label time and the commit that labelled it.
  The table joins the database schema under the current version, like the finding bank before it.
  `SCHEMA_VERSION` stays 5 on purpose: the live state database is shared with the benchmark evaluator, and the evaluator's pinned snapshot refuses any other version.
- **The rule.** `paper_completion.classify_paper`, written out in the "Paper completion labels" section of `docs/STREAMING_DATASET.md`.
- **The batch.** `label-completed-papers` labels every analyzed paper of one run in one transaction and prints the counts per outcome class and per incomplete reason.
  Without `--apply` it writes nothing.
  It reads stored state only: no provider call, no receipt validation, no ledger lock.
- **The startup skip.** The producer reads the labels of its run id at start and skips a labelled paper before it reads one receipt of it.
  The labelled paper's recorded eligibility decision keeps the run counts true.
- **The self-labelling.** The producer writes the label the moment a paper reaches a terminal outcome, so the batch is a one-time catch-up.

Ledger integrity is untouched.
No receipt is altered or deleted, and the label has no acceptance authority.
A label is removed with one `DELETE` of its row; the paper then keeps today's walk.

## 3. One owner for the terminal rule

The terminal rungs of the generation ladder moved out of `_progress_generation` into `streaming._stored_generation_outcome`.
The producer walks that ladder to find its next paid call; the label rule walks the same ladder to ask whether the run already finished the paper.
A second ladder would let a labelled paper differ from the paper the producer would have finished, which is the fault that split the option repair trigger set and ended the run on 2026-09-16 at 16:28 UTC.

The extracted function writes nothing.
A routing rung that reaches a gate review flag reports it in `gate_review` and the producer records it, so the read-only caller stays read-only.
The label rule passes no slot lookup, so a family whose routing wants one is reported as not complete.

## 4. The label rule

A paper is analyzed when the run reached a terminal outcome for it:

| Outcome | Outcome class |
| --- | --- |
| Eligibility decision `excluded` | `eligibility_excluded` |
| Eligibility decision `uncertain`, or an invalid answer, which stays uncertain | `eligibility_unresolved` |
| Generation accepted (`machine_accepted_unverified`) | `generation_accepted` |
| Generation rejected with routing exhausted | `generation_rejected` |
| A valid short answer whose distractors failed, final | `incomplete_non_mcq` |
| The per-paper cost cap reached | `paper_cost_cap_reached` |

A paper mid-family takes no label: an open call record, an unsettled or ambiguous request, a reviewed no-replay row, a stored path that routes to another attempt or a slot lookup, a candidate that still needs its validation, a paper not screened under the active prompt, schema and policy version, a paper whose access is not `full_text_ready`, a paper the free token count refused, and a paper a candidate processing fault settled.

One correction was made during the build.
The first version of the rule required the eligibility job's `state` to be `completed`, and that left 25 papers unlabelled.
The job state is `screening_error` whenever the deterministic validation refused the answer, but the producer's decision for such a paper is `uncertain`, and it is final for this prompt version.
The rule now reads the stored job's own validation decision, which is the decision the producer records.

## 5. The dry run

The producer stopped at 22:27 UTC on a stored `count_error` replay, before this work reached the live state.
The dry run below ran against a backup copy of the live state database, taken with the sqlite backup API at 22:45 UTC, so nothing of the live run was touched.

`dry-run-on-db-copy-2026-09-16T2245Z.json` holds the full report: the counts, every complete paper with its class, and every mid-family paper except the 4,208 not yet screened.

| Outcome class | Papers |
| --- | --- |
| `eligibility_excluded` | 42 |
| `eligibility_unresolved` | 43 |
| `generation_accepted` | 16 |
| `generation_rejected` | 102 |
| `incomplete_non_mcq` | 4 |
| `paper_cost_cap_reached` | 1 |
| **Total to label** | **208** |

Of 4,420 papers in the frozen order, 4,212 are not analyzed: 4,208 were never screened, and four are mid-family:

| Paper | Why it is not complete |
| --- | --- |
| `10.48550/arxiv.2406.18417` | `operational_ambiguous_charge_http_500` |
| `10.16993/tellusb.1880` | `operational_ambiguous_charge_http_500` |
| `10.1371/journal.pone.0096079` | `operational_ambiguous_charge_provider_timeout` |
| `10.31857/s0030157424030103` | an open call record (`incomplete_infra`) |

The 208 complete papers agree with the live progress record of 22:27 UTC, which counts 42 excluded, 43 unresolved, 102 generation rejected, 16 accepted and 1 cost-capped.

The dry run walked all 4,420 papers in 43 seconds.

## 5a. The apply, rehearsed, and the shared database

The apply was rehearsed on a second copy of the same backup: it wrote 208 labels, and a second run of it wrote none and reported 208 already labelled.

The live state database is shared with the benchmark evaluator, which must keep running.
The evaluator opens the database through `Database.migrate`, and that method raises on any schema version other than its own.
So the label table joins `SCHEMA` under the current version, and `SCHEMA_VERSION` stays 5.
Both evaluator snapshots were run against the labelled copy on 2026-09-16:

| Snapshot | Result |
| --- | --- |
| `app-a0b9a82-arctic-abstention-streaming-eval-r1` (running) | `SCHEMA_VERSION` 5; migrate ok; 50 accepted items readable; the label table still holds its 208 rows |
| `app-ea00336-arctic-abstention-stream-r4` (prepared cutover) | `SCHEMA_VERSION` 5; migrate ok; 50 accepted items readable |

An older reader that does not know the table leaves it alone, because its `SCHEMA` never drops a table and the version already matches.

## 6. Landing on the paper concurrency

`arctic-ch3-paper-concurrency-r1` rewrites the same paper loop into one thread per paper.
This work lands on top of theirs.
A trial merge of their branch on 2026-09-16 auto-merged `db.py`, which holds their threaded `Database` and this table together, and left twelve conflicts: seven in `streaming.py`, three in `tests/test_streaming.py`, one in `cli.py` and one in `research/README.md`.
All of them are in the paper loop, the counts and the resume tests that both tasks changed.

Under their loop the label work keeps its shape.
The skip is the first thing `process_paper` does for a paper, before any binding or receipt read.
The counts of a skipped paper are taken under the same tally lock as every other paper's counts.
The self-labelling stays at the three terminal points of the per-paper chain.

## 7. The activation

`build-completion.py` in this directory prepares and cuts over the activation set, in the shape of `build-concurrency.py` of `arctic-ch3-paper-concurrency-r1`.

This activation moves no policy field.
It inherits the budget policy, the ledger transition and the worker counts of the concurrency activation it succeeds, so it authorizes no new spend and writes no ledger transition.
It writes one new execution gate, which binds the new code commit, the new runtime snapshot and its own review record.

The launcher names the commit with `--code-commit`.
A runtime snapshot is a `git archive` extraction with no `.git`, so `git rev-parse` inside it answers with the parent repository's commit, which is the wrong commit and a silent one.

`launch` stops the producer at a zero-in-flight boundary, runs the batch dry run and then the apply while nothing writes the table, starts the new producer in tmux session `arctic-ch3-production-r1`, and measures launch to first paid call.

## 8. Result of the live cut

### The batch label

Applied to the live state database at 23:38 UTC on 2026-09-16, in one
transaction, while the producer was stopped.

| Outcome class | Papers |
| --- | --- |
| `generation_rejected` | 102 |
| `eligibility_unresolved` | 43 |
| `eligibility_excluded` | 42 |
| `generation_accepted` | 16 |
| `incomplete_non_mcq` | 4 |
| `paper_cost_cap_reached` | 1 |
| **Total** | **208** |

`label-apply-2026-09-16T233707Z.json` in the activation directory holds the
report, and the table read back holds the same 208 rows.
A second apply writes none, so a relaunch of the activation never disturbs it.

### Three faults of the activation script, and what they teach

The launch needed four attempts. Every fault was in the activation script, not
in the label or the producer, and each one is the same mistake: reading a
shared resource as if it belonged to this run.

1. **The producer was found by its run id alone.** `label-completed-papers`
   takes the same `--run-id`, so the launch saw its own label subprocess as the
   live producer and would have stopped the batch it had just started. The
   match now needs the `stream` subcommand. Caught before anything was written.
2. **`inflight` was read as this run's.** The counter covers every caller of the
   shared ledger, and the benchmark evaluator keeps calling. The launch refused
   to proceed on the evaluator's traffic. The producer-stop boundary now counts
   only requests whose `run_id` is this run's; the batch label needs no ledger
   at all.
3. **`generation_submissions` was read as this run's.** The first launch
   reported a first paid call 23.2 seconds after start. That was the
   evaluator's next call on the shared counter: the producer had already exited
   at broker construction, and this run had submitted nothing since 21:10 UTC.
   A live pid proves nothing, because the broker is constructed seconds after
   exec and a refusal there exits at once. A start now counts only when the
   producer's own progress record moves while the process is up, and the first
   paid call counts only requests this run submitted.

### The settled-ledger window

An applied policy transition is validated again on every broker construction,
and that validation refuses a ledger with any request in flight
(`a configuration transition requires a settled ledger`).
The benchmark evaluator shares the ledger and calls continuously: sampled over
one minute on 2026-09-16, the ledger was settled about half the time, in
windows of about thirty seconds.
A producer therefore starts only inside one of those windows, and `nix develop`
plus the Python import can spend that window before the broker is built.
The launch waits for a settled window before every broker construction and
retries a start that loses it.
This is the same wall the concurrency activation met at 22:59 UTC.

**A refused construction is not a quiet failure.** It writes an integrity halt
file, and a halt blocks every broker start on that ledger, the evaluator's
included. The 23:39:44 UTC halt came from this task's own start attempt, and it
deadlocked both units: the producer could not start under the halt, the
evaluator could not start under the halt, and only a broker start runs the
orphan recovery that would settle the evaluator's stranded request. Retrying a
start into a busy shared ledger therefore makes the problem worse. The starts
must be serialised instead.

### The launch that worked

Firstmate serialised them (order of 2026-09-16 23:46 UTC): the evaluator worker
stopped the evaluator unit, superseded the halt and settled the in-flight
evaluation request; the producer then started alone; the evaluator restarts
after the producer's first paid construction call closes the
transition-validation window for good.

| Measure | Value |
| --- | --- |
| Launched | 2026-09-16 23:52:40 UTC |
| Producer PID | 2239101 |
| tmux session | `arctic-ch3-production-r1` |
| Commit | `6fa9163`, 4 paper workers, 4 option workers |
| Broker construction | passed on attempt 1 |
| First paid construction call | 2026-09-16 23:53:59 UTC |
| **Launch to first paid call** | **79.6 seconds** |
| This run's submissions | 923 before, 924 after |

### Startup before and after

| Launch | Startup to first paid call |
| --- | --- |
| 18:28 UTC, before the label | about 20 minutes |
| 18:58 UTC, before the label | about 21 minutes |
| 21:53 UTC, before the label | about 22 minutes |
| **23:52 UTC, with 208 papers labelled** | **79.6 seconds** |

The 80 seconds are not all replay: they include the process start, the `nix
develop` shell, the broker construction and the walk over the 4,212 unlabelled
papers of the frozen order, which are cheap because they were never screened.


## 9. Tests

The whole release suite at the merged head `b9d1350`: 1473 tests, none failed, `ruff check` and `ruff format --check` clean.
The log is `suite-b9d1350.log` in the activation directory.

The producer ran on commit `6fa9163`, whose `src/`, `tests/`, `config/` and
`schemas/` are the same as `72c395a`, where the suite was also green with 1433
tests. A run of the suite before the merge of `main` at 00:5x UTC failed five
abstention pause tests; `774da61` on `main`, which holds a paused model against
the real clock, is their fix, and they pass at `b9d1350`.


`tests/test_paper_completion.py`:

- the label rule on a fixture state with one paper per outcome class and one paper of every mid-family shape, which takes no label;
- the batch, its dry run, its idempotence and its single transaction;
- the startup skip, which fails the test if a labelled paper reaches generation, source import, eligibility pairing or receipt validation;
- the self-labelling on a terminal outcome, and the absence of a label for a mid-family disposition;
- one guard that every terminal disposition has exactly one outcome class and one run count.

Five tests of `tests/test_streaming.py` changed because the label changes what a second run does.
Two resume guards now remove the label first, so they keep checking the unlabelled paper, and the re-validation guard then shows the labelled skip on a third run.
Three resume tests now assert the skip instead of the replay.
