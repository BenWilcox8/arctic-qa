"""Read-only profile of one paid call's bookkeeping, on the live ledger."""
import json, os, sys, time, statistics
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, "src")
from arctic_qa import model_broker as mb
from arctic_qa.util import atomic_json, canonical_json, sha256_file

S = Path("/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1")
SCRATCH = Path("/mnt/crdata/research-abstention/arctic-qa/scratch-ledger-parallel-r1")
LEDGER = S / "shared-paid-call-ledger.json"
RECEIPTS = S / "model-receipts"

b = object.__new__(mb.SharedGeminiBroker)
b.ledger_file = LEDGER
b.receipts_dir = RECEIPTS
b.policy_file = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-paper-concurrency-r1/streaming-dataset-budget-policy-v11-chapter3-concurrency.json")
b.price_config_file = Path("config/gemini-eligibility-v1.json").resolve()
b.config = mb._config(b.price_config_file)
b.policy = mb._validate_policy(b.policy_file)
b.prior = Decimal("0")
b.active_price_config_sha256 = sha256_file(b.price_config_file)
b.evaluation_policy = None
b.evaluation_config = None
b.evaluation_policy_file = None
b.evaluation_price_config_file = None
b.evaluation_gate_file = None
b._config_transition_sha256 = None
b._config_transition_event_path = None
b.active_evaluation_price_config_sha256 = None
b._evaluation_binding = None
b._stream_input_binding = None
b.execution_gate_file = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-concurrency-busy-r1/live-execution-gate-b578731-ch3cb.json")
b._status_observer = None
b._authorized_live_test_ceiling_usd = None
b._evaluation_transition_sha256 = None
b._immutable_events_proved = {}
b._immutable_events_context = None
b._immutable_events_proved_at = None
b._ledger_evidence_proved = None
b._pacing_state = type("s", (), {})()
def _no(*a, **k):
    raise AssertionError("integrity halt attempted during a read-only profile")
b._record_integrity_halt = _no
b._publish_integrity_halt = _no

def t(label, fn, n=5):
    xs = []
    for _ in range(n):
        s = time.perf_counter()
        fn()
        xs.append(time.perf_counter() - s)
    m = statistics.median(xs)
    print(f"{label:52s} {m*1000:9.1f} ms")
    return m

raw = LEDGER.read_bytes()
led = json.loads(raw)
print(f"ledger {len(raw)} bytes, {len(led['requests'])} request rows, "
      f"{len(os.listdir(RECEIPTS))} receipt files")
print()
print("== one ledger read ==")
r_hash = t("  sha256_file (evidence fingerprint)", lambda: sha256_file(LEDGER))
r_read = t("  _read: open + json.load", lambda: mb._read(LEDGER))
r_val = t("  _validate_ledger (every read)", lambda: b._validate_ledger(led))
def cold_ie():
    b._immutable_events_proved = {}
    b._immutable_events_context = None
    b._validate_immutable_events(led)
r_ie_cold = t("  _validate_immutable_events (cold, full)", cold_ie, n=2)
r_ie_warm = t("  _validate_immutable_events (warm, no row moved)", lambda: b._validate_immutable_events(led), n=3)
r_tr = t("  _validate_active_transition_event", lambda: b._validate_active_transition_event(led), n=3)
print(f"  -> read, nothing changed:  {(r_hash+r_read+r_val)*1000:8.1f} ms")
print(f"  -> read, a row moved:      {(r_hash+r_read+r_val+r_ie_warm+r_tr)*1000:8.1f} ms")
print()
print("== one ledger commit ==")
out = SCRATCH / "probe-commit.json"
c_val = t("  _validate_ledger (again, in _commit_ledger)", lambda: b._validate_ledger(led))
c_write = t("  atomic_json write of the whole file", lambda: atomic_json(out, led))
c_status = t("  _status_payload", lambda: b._status_payload(led), n=3)
c_status_w = t("  status atomic_json", lambda: atomic_json(SCRATCH / "probe-status.json", b._status_payload(led)), n=3)
print(f"  -> commit: {(c_val+c_write+c_status_w)*1000:8.1f} ms")
print()
print("== the orphan-recovery custody scan (per paid call) ==")
def custody():
    n = 0
    for key, row in led["requests"].items():
        stem = b._request_event_stem(key, row)
        if row["state"] in {"completed", "ambiguous_charge"}:
            (RECEIPTS / f"{stem}.json").is_file()
            n += 1
    return n
t("  stat of every terminal row's final receipt", custody, n=3)
print()
print("== misc per-call helpers ==")
t("  canonical_json of every request row", lambda: [canonical_json(r) for r in led["requests"].values()], n=3)
