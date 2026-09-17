"""Read the shared ledger through its store and print what a cut-over needs.

The JSON file is the compacted snapshot, not the ledger. Read it with
`ledger_store.read_ledger` (AGENTS.md, "Parallel bookkeeping").
"""
import json, sys
from pathlib import Path
from arctic_qa import ledger_store

ledger = ledger_store.read_ledger(Path(sys.argv[1]))
inflight = [
    key for key, row in ledger["requests"].items()
    if row.get("state") == "submitted" and row.get("phase") == "benchmark_evaluation"
]
print(json.dumps({
    "halted": bool(ledger.get("halted")),
    "halt_reason": ledger.get("halt_reason"),
    "evaluation_halted": bool(ledger.get("evaluation_halted")),
    "evaluation_inflight": len(inflight),
    "requests": len(ledger["requests"]),
}))
