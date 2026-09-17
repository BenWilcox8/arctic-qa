"""Read-only: what the start compaction of one evaluator broker costs."""
import time, shutil, sys
from pathlib import Path
from arctic_qa import ledger_store
from arctic_qa.model_broker import SharedGeminiBroker

R = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-ch3-production-run-r1")
D = Path("/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1")
APP = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-authorization-r8/runtime/app-c6f1767-arctic-eval-authorization-r8")
SCRATCH = Path(sys.argv[2])
SCRATCH.mkdir(parents=True, exist_ok=True)

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
t0 = time.monotonic()
state, seq, offset = ledger_store.materialize(broker.ledger_file)
t_mat = time.monotonic() - t0
t0 = time.monotonic()
broker._prove_ledger(state)
t_prove = time.monotonic() - t0
# The snapshot write, to a scratch copy on the same disk: never the live file.
target = SCRATCH / "snapshot.json"
shutil.copy2(broker.ledger_file, target)
base = ledger_store.journal_base_file(broker.ledger_file)
shutil.copy2(base, ledger_store.journal_base_file(target))
t0 = time.monotonic()
ledger_store.write_snapshot(target, state, seq, journal_offset=offset)
t_write = time.monotonic() - t0
print(f"materialize {t_mat:.3f} s  prove_ledger {t_prove:.3f} s  write_snapshot {t_write:.3f} s"
      f"  total {t_mat+t_prove+t_write:.3f} s")
