# Parallel bookkeeping for the shared paid-call ledger

Captain's asks, 2026-09-17:

- 03:35 UTC: "If possible, increase the concurrency to 16"
- 03:36 UTC: "Please try to improve the bookkeeping or refactor that to be
  parallelizable to."
- 03:36 UTC addendum: the target is 16 papers in flight; only a proven Google
  limit with HTTP 429 evidence may leave the run below it.

Run `chapter3-7dc6485-r3`, campaign `arctic-qa-production-campaign-003`,
shared ledger
`/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json`.

## 1. The measurement

Measured at 03:40 to 03:55 UTC on 2026-09-17, on the live ledger and on a
hard-linked copy of it, while the producer (four paper threads) and the
streaming evaluator both ran. The scripts are beside this report:
`profile-read-path.py`, `profile-commit-path.py`, `profile-storage.py`.

The ledger at the time: 8.16 MB, 6,036 request rows, 23,615 receipt files.

### One ledger read

| step | cost |
| --- | --- |
| `sha256_file`, the evidence fingerprint | 5 ms |
| `_read`: open and `json.load` of 8.16 MB | 90 ms |
| `_validate_ledger`, on every read | 150 ms |
| `_validate_immutable_events`, no row moved | 566 ms |
| `_validate_immutable_events`, cold full proof | 1,900 to 2,200 ms |
| `_validate_active_transition_event` | under 1 ms |

A read where nothing changed costs 235 ms. A read where any row moved, which
under four threads is almost every read, costs **800 ms**.

The 566 ms of the "warm" immutable-event proof is not row work. It is eight
`glob` passes over a 23,615-entry directory, 34 to 40 ms each, plus the
per-row signature pass:

| scan | cost |
| --- | --- |
| `glob config-transition-*.json` | 34 ms |
| `glob *.usage-reconciliation.json` | 39 ms |
| `glob *.ambiguous-continuation.json` | 39 ms |
| `glob *.orphaned-continuation.json` | 39 ms |
| `glob *.count-error-continuation.json` | 40 ms |
| `glob *.pretransport-settlement.json` | 40 ms |
| `glob *.http-rejection-settlement.json` | 38 ms |
| `glob *.phaseless-refusal-settlement.json` | 40 ms |
| `os.listdir` of the same directory | 22 ms |

Eight globs read the same directory eight times. One `scandir` reads it once.

### One ledger commit

| step | cost |
| --- | --- |
| `_validate_ledger`, again inside `_commit_ledger` | 150 ms |
| `atomic_json` of the whole 8.16 MB ledger | 460 ms |
| `_status_payload` | 36 ms |
| `atomic_json` of the 163 KB status file | 264 to 666 ms |

### The storage, which is the finding

`/mnt/crdata` is a USB-attached spinning disk (`sdb`, `One Touch HDD`,
`rotational=1`). The root filesystem is an SSD (`sda`). The same durable write,
under the same live load:

| write | HDD (`/mnt/crdata`) | SSD (root) |
| --- | --- | --- |
| `atomic_write` 200 B | 348 ms | 7.3 ms |
| `atomic_write` 163 KB | 264 ms | 6.8 ms |
| `atomic_write` 8.16 MB | 471 ms | 70 ms |
| append one line and `fsync` | 155 ms | 14.9 ms |

The cost of a durable write on this device is almost independent of its size.
It is `fsync` latency on a rotating USB disk under concurrent load, 150 to
470 ms each, with a tail over one second. `atomic_write` pays two of them: the
temporary file and the directory.

### The attribution of one paid call

A paid construction call reads the ledger about five times (orphan recovery,
the paper cost cap, the pacing window, the count registration session, the
reservation, the settlement) and commits it three times (the count event, the
reservation, the settlement). Each commit also writes the status file.

| what | per call |
| --- | --- |
| three ledger commits, durable, on the HDD | 1.4 s |
| three status-file writes, durable, on the HDD | 0.8 to 2.0 s |
| immutable-event proof on the reads where a row moved | 0.5 to 1.1 s |
| JSON parse and the full `_validate_ledger` per read and per commit | 0.9 to 1.2 s |
| the orphan-recovery custody scan (`is_file` of every terminal row) | 0.1 s |

The measured admission is about 3 seconds, and the parts above add to more
than that, because the ledger session already collapses several reads into one
and because the immutable-event proof is skipped where the fingerprint has not
moved. The ranking is what matters, and it is stable:

1. durable writes on a spinning disk,
2. directory-wide scans of the receipts directory,
3. the whole-file parse and the whole-ledger validation, on every read.

None of the three is work that one paid call owes. All three are work
proportional to the whole history, paid again by every call.

### Why 16 in flight is not reachable without this

The number of calls in flight settles at the call length over the serialised
admission length. The median call is 8 seconds. Sixteen in flight therefore
needs an admission of **0.5 seconds or less**. Today it is about 3 seconds,
which settles at 3 in flight, which is what the run shows.

## 2. The design

"Parallel bookkeeping" in `docs/SHARED_MODEL_BROKER.md` is the contract.
The code is `src/arctic_qa/ledger_store.py` and the two seams of the broker, `_validated_ledger` and `_commit_ledger`.

The ledger file stays where it is as the compacted snapshot.
Every committed mutation is one line appended to `.shared-paid-call-ledger.json.journal` beside it: an absolute assignment of the keys the mutation changed, never an increment, so a replay from any earlier point reaches the same state.
A base record binds the snapshot to the journal by the hash of the snapshot's bytes and names the snapshot it superseded, so a stop between the two writes of a compaction leaves a bound store.
A snapshot that matches neither record was changed outside the store and fails closed.

A process holds the state in memory as tracked containers that report the keys a mutation touched.
A read applies the records another process appended since the last read.
A commit proves the rows that moved (`_validate_ledger_delta`: the contribution of each moved row is subtracted, proved again and added back, and everything the full pass compares is compared again), appends one line, and releases the shared ledger lock before the flush.
One `fsync` then covers every record the concurrent calls appended before it.
A compactor thread rewrites the snapshot and the status record every 30 seconds without the ledger lock, and runs the full row-by-row proof each time; a broker that runs one operation at a time writes the snapshot with every commit, so every reviewed command and every test sees an exact file.
Every reader in the repository reads the store: the producer, the evaluator, the cost guard, the website, the batch selection, the exclusive batch activation, the concurrency repair.

Measured on a copy of the live ledger (6,287 rows) on the data disk:

| operation | before | after |
| --- | --- | --- |
| read, nothing changed | 235 ms | 10 ms |
| read, a row moved | 800 ms | 10 ms |
| the proof of one moved row | 150 ms (every row) | 10 ms |
| one commit, durable | 460 to 700 ms | 57 ms a commit from sixteen threads (10 flushes for 64 commits) |
| status record | 264 to 666 ms per commit | off the hot path |

## 3. The migration

`arctic-qa migrate-ledger-store --apply` at 04:53:05 to 04:53:13 UTC, with the producer and the evaluator stopped at a boundary with nothing submitted and nothing counting of any caller.
6,406 request rows; USD 96.282550 spent, USD 0.021016 reserved, USD 0.123539 ambiguous; identical before and after, canonical hash `e787c90e…`; archive `shared-paid-call-ledger.pre-store-archive-20260917T045309Z.json`, frozen 0444.
The same command proved identical on a copy of the live ledger at 6,320 rows before the live run.

## 4. The cutover

| minute (UTC) | what |
| --- | --- |
| 04:49:55 | stop begins: evaluator at its boundary (04:52:13), producer at ours (04:52:55), whole ledger `inflight` 0 |
| 04:53:13 | migration proved, 8 seconds |
| 04:53:37 | v12 transition refused on `e56c6a1`: the tranche rule named the concurrency change and not the parallel one, so the USD 5 default applied; nothing written to the ledger |
| 04:58:48 | v12 transition refused on `e0dff2c`: the no-replay probe (`validate_no_replay_liabilities`, built without `__init__`) lacked the receipts-listing cache; the ledger carries an ambiguous charge and a retained reservation, which routes a transition through that probe, and no fixture ledger had one; read as "requires a settled ledger" |
| 05:01:19 | policy v12 applied on `948c860`: 16 concurrent, 100 a minute, money unchanged, event `config-transition-40053dc6…` |
| 05:03:36 | launch on `948c860` exited at once: `--jev-ranking-file` was on the Jev worker's branch (`6b614cc`) and not on the main I had merged |
| 05:04:59 | producer relaunched on `1ba7d6f` (merge of the Jev branch), 16 paper workers, the Jev flag carried; first paid call within a minute |
| 05:05:57 | evaluator restarted on `1ba7d6f` after `reset-failed` (the killed predecessor left the transient unit failed) |
| 05:06 | guard and viewer restarted on the snapshot, so they read the store |
| 05:08:56 to 05:11:13 | hot-swap onto `b7f4e77` after two blocker findings of the adversarial audit (section 6): stop at a boundary, relaunch, evaluator at 05:11:49, readers |
| 05:17:12 | the evaluator unit exited: `sqlite3.OperationalError: database is locked` (section 7) |
| 05:23:31 | the state database switched to WAL; 05:24:02 evaluator restarted on `0d5e696`; readers again at 05:24:43 |

The producer of run `chapter3-7dc6485-r3` is left on `b7f4e77` with sixteen paper workers and four option workers, under policy v12, reading and writing the store, until the final swap onto the landed commit.

## 5. The measured window

Fifteen minutes on `b7f4e77`, 05:12:10 to 05:27:11 UTC, sixteen paper workers, policy v12, read through the store (`activation-state-b7f4e77.json`, `observations`):

| measure | before (03:00 window, 4 workers, one-file ledger) | after |
| --- | --- | --- |
| requests a minute | 6.73 (sequential baseline about 3) | 20.8 |
| paid requests in the window | 101 | 312 |
| peak in flight | 3 | 11 |
| questions accepted in the window | - | 9 (33 to 42) |
| HTTP 429 / 503 | 0 / 0 | 0 / 0 |
| ambiguous charges | 0 | 0 |
| candidate processing faults | 1 | 0 |
| lock give-ups | 0 | 0 |
| journal records appended | - | 983 (seq 413 to 1396), 1.7 MB; the snapshot rewritten every 30 s off the hot path |

Papers screened in the window: 30 (120 an hour), against 43 in the 03:00 window.
The count is not comparable: the Jev live order now hands the workers the papers most likely to be eligible, so far more of them go through the whole generation chain instead of one screening call, which is what the request and the acceptance counts show.

The sixteenth slot was not reached.
The number in flight settled around 11 at its peak and 3 to 5 on average, with 20.8 calls a minute of 8 seconds.
The ledger is no longer what serialises the workers: every lock hold in the window was under a second and the store's own cost per call is about 100 ms.
What is left is the producer's own work between two calls, under one interpreter lock: the process ran at 60 to 75 percent of one core for the whole window, with one thread busy at any instant.
A proven Google limit was not reached: no 429 and no 503 in 312 calls at up to 11 in flight.

## 8. The exit at 05:25

At 05:25:40 UTC the `b7f4e77` producer ended with `{"code":"VALUEERROR","message":"the source version is already bound to another paper family"}`: a plain `ValueError` from the family-binding check of `_count_event`, which `broker_provider.broker_boundary` marks a whole-run stop, exactly as the duplicate-key `ValueError` did at 02:32 UTC.
The refusal reserved nothing and charged nothing; the ledger holds no source version bound to two families, because the check fires before the binding is written.
The identity of the refused request is not in the ledger (no row is written before the refusal) and the producer's stop line names no paper; with the new error class the next such refusal is recorded against its paper in the routing ledger, which names it.

Fixed on `7d42bbf`: the three binding refusals raise `errors.PaperBindingConflictError`, which is in `broker_provider._PAPER_LEVEL_BROKER_ERRORS` and in the non-stop set of `streaming._ends_the_run`, so the family is recorded and skipped and the run continues.
The producer was relaunched at 05:29:22 UTC on `7d42bbf` with sixteen paper workers, as a successor of the applied v12 transition, with the evaluator live (a successor start applies no transition, so the start-order rule does not hold it).

## 6. The adversarial audit

A four-lens audit (money path, concurrency and locking, crash safety and integrity, readers and docs) with two independent refuters per finding ran against the change while it was live.
Every finding it confirmed is fixed on the branch and covered by a test in `tests/test_ledger_store.py`:

- `LedgerStore.flush` waited for the read offset, which a peer's records move past this process's last write; in a process with one admission (the evaluator) it would never have ended. A flush now covers this process's own writes.
- `materialize` and `apply_journal` hashed the file again after reading its bytes, so a compaction between the two reads would bind the old bytes to the new base record and skip the records between them. The binding is of the bytes that were read.
- A torn journal tail was never cut, so the next append would glue two lines into one unreadable record. The next process to append cuts it, under the shared ledger lock.
- A broker on a ledger with no store created a journal with no base record and halted on its second read. It refuses and names the migration.
- A journal record that moved only a paper row was applied without a proof. Any applied record owes the comparison of the totals.
- A refused reservation left the store dirty and forced a reload of the whole snapshot on every following read. An abandoned mutation rolls back in place.
- A failed `fsync` marked bytes durable. It marks nothing, and the waiters run their own.
- Readers on the plain file (the evaluator's cost summary, the concurrency repair, the batch selection, the exclusive batch activation) read the store.
- The base record carries the journal offset, so a reader seeks past the records the snapshot holds instead of parsing the whole journal.
- A process compacts once more at exit, and a start waits for the compaction lock rather than skip, so a reviewed operation binds the state.
- `initialize_store` writes the base record before the journal and refuses a leftover store beside a fresh ledger.

Findings the refuters rejected named the parent commits of fixes already landed; none named a defect that survives on the branch.

## 7. The state database under sixteen writers

At 05:17:12 UTC the evaluator unit exited with `sqlite3.OperationalError: database is locked`.
The state database ran in rollback-journal mode, so every write transaction of the sixteen producer threads blocked every reader, and the evaluator's read-only connection had the five-second default timeout while the producer's writer had thirty seconds (`db.BUSY_TIMEOUT_SECONDS`, from `aec5201`).

Fixed on `0d5e696` and live since 05:24 UTC: every opener of the state database goes through `db.connect_read_only`, which carries the shared timeout; the database runs in write-ahead logging (switched live at 05:23:31, kept by `Database.__init__` for a fresh one), so a reader never waits for a writer; and a locked read is a retry (`db.retry_locked_read`), never an exit.
The guard and the viewer were restarted on the same snapshot.
