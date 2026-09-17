"""Measure the parallel store against a copy of the live ledger.

The read path, the commit path and the group flush, at the live row count, on
the live data disk. Run it against a hard-linked copy, never the live ledger.
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "src")
from arctic_qa import ledger_store, model_broker as mb  # noqa: E402
from arctic_qa.util import sha256_file  # noqa: E402

D = Path(sys.argv[1])
LEDGER = D / "shared-paid-call-ledger.json"
RECEIPTS = D / "model-receipts"
FM = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data")

b = object.__new__(mb.SharedGeminiBroker)
b.ledger_file = LEDGER
b.receipts_dir = RECEIPTS
b.policy_file = (
    FM / "arctic-ch3-paper-concurrency-r1"
    / "streaming-dataset-budget-policy-v11-chapter3-concurrency.json"
)
b.price_config_file = Path("config/gemini-eligibility-v1.json").resolve()
b.config = mb._config(b.price_config_file)
b.policy = mb._validate_policy(b.policy_file)
b.prior = Decimal("0")
b.active_price_config_sha256 = sha256_file(b.price_config_file)
b._row_contributions = None
b._ledger_aggregate = None
b._aggregate_state_id = None
b._receipt_listing = None
b._custody_proved = set()
b._store = ledger_store.LedgerStore(LEDGER)

if not ledger_store.journal_file(LEDGER).exists():
    ledger_store.initialize_store(LEDGER)

state = b._store.load()
print(f"rows {len(state['requests'])}, bytes {LEDGER.stat().st_size}")


def t(label, fn, n=20):
    xs = []
    for index in range(n):
        s = time.perf_counter()
        fn(index)
        xs.append(time.perf_counter() - s)
    m = statistics.median(xs)
    print(f"{label:52s} med {m * 1000:8.2f} ms  max {max(xs) * 1000:8.2f}")
    return m


print()
print("== the proof ==")
t("full pass, every row (_prove_ledger)", lambda i: b._prove_ledger(state), n=3)
b._validate_ledger(state)
key = next(iter(state["requests"]))
t("delta pass, one row moved", lambda i: b._validate_ledger_delta(state, {key}))

print()
print("== one commit ==")


def commit(index):
    state["updated_at_utc"] = f"2026-09-17T05:00:{index:02d}Z"
    b._store.commit(state, now="2026-09-17T05:00:00Z")


t("append one journal record (no flush)", commit)
def commit_and_flush(index):
    commit(index)
    b._store.flush()


t("append and flush", commit_and_flush, n=10)

print()
print("== one read ==")
t("read, nothing changed", lambda i: b._store.read())

print()
print("== sixteen threads, one flush each wave ==")
before_fsync = b._store.fsync_count
before_append = b._store.append_count
lock = threading.Lock()


def worker(index):
    with lock:
        state["updated_at_utc"] = f"2026-09-17T05:01:{index % 60:02d}Z"
        b._store.commit(state, now="2026-09-17T05:01:00Z")
    b._store.flush()


started = time.perf_counter()
with ThreadPoolExecutor(max_workers=16) as pool:
    for future in [pool.submit(worker, index) for index in range(64)]:
        future.result()
elapsed = time.perf_counter() - started
print(
    f"64 commits from 16 threads in {elapsed:.2f} s; "
    f"appends {b._store.append_count - before_append}, "
    f"flushes {b._store.fsync_count - before_fsync}, "
    f"{elapsed / 64 * 1000:.1f} ms a commit"
)

print()
print("== the compaction, off the hot path ==")
t("write the snapshot and the base record",
  lambda i: ledger_store.write_snapshot(LEDGER, state, b._store.sequence), n=3)
