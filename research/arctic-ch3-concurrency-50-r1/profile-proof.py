"""Attribute the cost of one warm ledger read on the live data.

Read-only: the store is loaded from the live snapshot and journal, the
receipts are a hard-linked copy, and only `_validate_immutable_events` and the
orphan-recovery loop shape are timed. Nothing is written.
"""
from __future__ import annotations
import statistics, sys, time
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, "src")
from arctic_qa import ledger_store, model_broker as mb
from arctic_qa.util import sha256_file

SRC = Path("/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1")
PROBE = Path("/mnt/crdata/research-abstention/arctic-qa/scratch-concurrency-50-r1/probe")
FM = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data")

b = object.__new__(mb.SharedGeminiBroker)
b.ledger_file = SRC / "shared-paid-call-ledger.json"
b.receipts_dir = SRC / "model-receipts"
b.policy_file = FM / "arctic-ledger-parallel-r1/streaming-dataset-budget-policy-v12-chapter3-parallel.json"
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
b._immutable_events_proved = {}
b._immutable_events_context = None
b._immutable_events_proved_at = None
b._ledger_evidence_proved = None
b._immutable_events_pending = None
b._receipt_derived = {}
b._receipt_listing_refresh_seconds = 0.0
b.evaluation_config = None
b.evaluation_policy = None
b._store = ledger_store.LedgerStore(b.ledger_file)

state = b._store.load()
print(f"rows {len(state['requests'])}  snapshot {b.ledger_file.stat().st_size} B")

def t(label, fn, n=5):
    xs = []
    for i in range(n):
        s = time.perf_counter(); fn(); xs.append(time.perf_counter() - s)
    print(f"{label:52s} med {statistics.median(xs)*1000:8.1f} ms  min {min(xs)*1000:8.1f}")

t("immutable-event proof, cold full pass", lambda: b._validate_immutable_events(state), n=1)
# warm: every row already proved, the listing already cached
t("immutable-event proof, warm (no row moved)", lambda: b._validate_immutable_events(state), n=5)

def warm_with_fresh_listing():
    b._receipt_listing = None
    b._validate_immutable_events(state)
t("immutable-event proof, warm + rescan of receipts", warm_with_fresh_listing, n=5)

def orphan_loop():
    requests = {key: dict(row) for key, row in state["requests"].items()}
    n = 0
    for request_key, request in requests.items():
        stem = b._request_event_stem(request_key, request)
        final_path = b.receipts_dir / f"{stem}.json"
        received_path = b.receipts_dir / f"{stem}.received.json"
        if request["state"] in {"completed", "ambiguous_charge"}:
            if request_key in b._custody_proved:
                continue
            n += 1
            continue
    return n
t("orphan-recovery loop shape (no custody proved)", orphan_loop, n=3)
b._custody_proved = {k for k, r in state["requests"].items() if r["state"] in {"completed", "ambiguous_charge"}}
t("orphan-recovery loop shape (custody proved)", orphan_loop, n=3)
t("_validate_ledger (full money proof)", lambda: b._validate_ledger(state), n=3)
key = next(iter(state["requests"]))
t("_validate_ledger_delta (one row)", lambda: b._validate_ledger_delta(state, {key}), n=10)

print()
print("== after the change ==")
b._immutable_events_pending = set()
t("immutable-event proof, warm, no row moved", lambda: b._validate_immutable_events(state), n=8)
def one_row():
    b._immutable_events_pending = {key}
    b._validate_immutable_events(state)
t("immutable-event proof, warm, one row moved", one_row, n=8)
def rescan():
    b._receipt_listing = None
    b._immutable_events_pending = {key}
    b._validate_immutable_events(state)
t("immutable-event proof, warm, receipts re-listed", rescan, n=5)
b._immutable_events_pending = None
t("immutable-event proof, requests replaced whole", lambda: b._validate_immutable_events(state), n=5)

import cProfile, pstats, io
b._immutable_events_pending = set()
b._validate_immutable_events(state)
pr = cProfile.Profile(); pr.enable()
for _ in range(10):
    b._immutable_events_pending = {key}
    b._validate_immutable_events(state)
pr.disable()
s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(22)
print(s.getvalue())
