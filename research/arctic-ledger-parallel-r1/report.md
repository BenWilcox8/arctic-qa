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
