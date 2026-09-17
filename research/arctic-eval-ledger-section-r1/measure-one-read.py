"""Read-only: the marginal cost of one ledger read whose receipt fingerprint moved.

Under the live producer the receipts directory moves on every paid call of
every worker, so the evaluator's every ledger read takes this path.
"""
import time, sys
from pathlib import Path
from arctic_qa import model_broker as mb
from arctic_qa.model_broker import SharedGeminiBroker

R = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-production-run-r1")
D = Path("/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1")
APP = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-authorization-r8/runtime/app-c6f1767-arctic-eval-authorization-r8")

timings = {}
counts = {}
def wrap(name, fn):
    def inner(*a, **k):
        t0 = time.monotonic()
        counts[name] = counts.get(name, 0) + 1
        try:
            return fn(*a, **k)
        finally:
            timings[name] = timings.get(name, 0.0) + time.monotonic() - t0
    return inner

for name in ("_validate_immutable_events", "_receipt_names", "_validate_accepted_item_events",
             "_validate_ledger", "_validate_ledger_delta", "_validate_request_events",
             "_receipt_request_keys", "_ambiguous_continuation_events",
             "_orphaned_continuation_events", "_listing_derived", "_receipt_paths"):
    if hasattr(SharedGeminiBroker, name):
        setattr(SharedGeminiBroker, name, wrap(name.strip("_"), getattr(SharedGeminiBroker, name)))

broker = SharedGeminiBroker(
    policy_file=R / "streaming-dataset-budget-policy-v9-chapter3.json",
    price_config_file=R / "runtime/app-c545cf8-arctic-ch3-production-run-r1/config/gemini-eligibility-v1.json",
    execution_gate_file=R / "live-execution-gate-c545cf8-ch3.json",
    ledger_file=D / "shared-paid-call-ledger.json",
    receipts_dir=D / "model-receipts",
    credential_file=Path("/home/ben/.config/arctic-qa/gemini-api-key"),
    prior_construction_spend_usd=0,
    config_transition_file=R / "ledger-config-transition-9f4cb18-ch3.json",
    evaluation_policy_file=APP / "config/benchmark-evaluation-policy-v3.json",
    evaluation_price_config_file=APP / "config/benchmark-evaluation-prices-v1.json",
    evaluation_gate_file=Path(sys.argv[1]),
    evaluation_policy_transition_file=None,
)
print("constructed")
for i in range(6):
    timings.clear(); counts.clear()
    # exactly what a moved receipts directory does to a warm broker
    broker._ledger_evidence_proved = None
    broker._receipt_listing = None  # what a moved receipts directory does
    t0 = time.monotonic()
    broker._validated_ledger()
    dt = time.monotonic() - t0
    print(f"moved-fingerprint read #{i}: {dt:.3f} s  "
          f"{ {k: (counts[k], round(v,3)) for k,v in sorted(timings.items()) if v > 0.002} }", flush=True)
