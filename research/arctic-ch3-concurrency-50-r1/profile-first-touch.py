"""The cost of the first touch of each tracked map, on the live row count."""
from __future__ import annotations
import statistics, sys, time
from pathlib import Path
sys.path.insert(0, "src")
from arctic_qa import ledger_store

LED = Path("/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1/shared-paid-call-ledger.json")
state, seq, offset = ledger_store.materialize(LED)
print("rows", len(state["requests"]))

def t(label, fn, n=5):
    xs = []
    for _ in range(n):
        s = time.perf_counter(); fn(); xs.append(time.perf_counter() - s)
    print(f"{label:52s} med {statistics.median(xs)*1000:8.1f} ms")

t("plain() of the whole requests map", lambda: ledger_store.plain(state["requests"]), n=3)

wrapped = ledger_store.wrap(state)
key = next(iter(wrapped["requests"]))
def first_touch():
    wrapped.clear_dirty()
    wrapped["requests"][key]["state"] = "counting"
t("first touch of the requests map, per commit", first_touch, n=10)
