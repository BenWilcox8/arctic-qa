"""Read-only profile: the commit side and the per-call scans."""
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
FM = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data")

b = object.__new__(mb.SharedGeminiBroker)
b.ledger_file = LEDGER
b.receipts_dir = RECEIPTS
b.policy_file = FM / "arctic-ch3-paper-concurrency-r1/streaming-dataset-budget-policy-v11-chapter3-concurrency.json"
b.price_config_file = Path("config/gemini-eligibility-v1.json").resolve()
b.execution_gate_file = FM / "arctic-ch3-concurrency-busy-r1/live-execution-gate-b578731-ch3cb.json"
b.config = mb._config(b.price_config_file)
b.policy = mb._validate_policy(b.policy_file)
b.prior = Decimal("0")
b.active_price_config_sha256 = sha256_file(b.price_config_file)
b.evaluation_policy = None
b.evaluation_config = None
b.evaluation_policy_file = None
b.evaluation_price_config_file = None
b.evaluation_gate_file = None
b.active_evaluation_price_config_sha256 = None
b._evaluation_binding = None
b._stream_input_binding = None
b._config_transition_sha256 = None
b._config_transition_event_path = None
b._status_observer = None
b._authorized_live_test_ceiling_usd = None
b._evaluation_transition_sha256 = None
b._immutable_events_proved = {}
b._immutable_events_context = None
b._immutable_events_proved_at = None
b._ledger_evidence_proved = None

led = mb._read(LEDGER)
print(f"ledger {LEDGER.stat().st_size} bytes, {len(led['requests'])} rows")

def t(label, fn, n=5):
    xs = []
    for _ in range(n):
        s = time.perf_counter()
        fn()
        xs.append(time.perf_counter() - s)
    m = statistics.median(xs)
    print(f"{label:52s} {m*1000:9.1f} ms")
    return m

print()
print("== one ledger commit ==")
c_val = t("  _validate_ledger (again, inside _commit_ledger)", lambda: b._validate_ledger(led))
c_write = t("  atomic_json of the whole ledger file", lambda: atomic_json(SCRATCH / "probe-commit.json", led))
c_status = t("  _status_payload", lambda: b._status_payload(led), n=3)
c_statw = t("  status file atomic_json", lambda: atomic_json(SCRATCH / "probe-status.json", b._status_payload(led)), n=3)
print(f"  -> commit total: {(c_val+c_write+c_statw)*1000:8.1f} ms")

print()
print("== the orphan-recovery custody scan ==")
def custody():
    n = 0
    for key, row in led["requests"].items():
        if row["state"] in {"completed", "ambiguous_charge"}:
            stem = b._request_event_stem(key, row)
            (RECEIPTS / f"{stem}.json").is_file()
            n += 1
    return n
t("  is_file() of every terminal row's final receipt", custody, n=3)
def snapshot():
    return {k: dict(v) for k, v in led["requests"].items()}
t("  dict copy of every request row (recovery snapshot)", snapshot, n=3)

print()
print("== receipts directory scans inside _validate_immutable_events ==")
for pat in ("config-transition-*.json", "*.usage-reconciliation.json",
            "*.ambiguous-continuation.json", "*.orphaned-continuation.json",
            "*.count-error-continuation.json", "*.pretransport-settlement.json",
            "*.http-rejection-settlement.json", "*.phaseless-refusal-settlement.json"):
    t(f"  glob {pat}", lambda p=pat: list(RECEIPTS.glob(p)), n=3)
t("  os.listdir of the receipts directory", lambda: os.listdir(RECEIPTS), n=3)
