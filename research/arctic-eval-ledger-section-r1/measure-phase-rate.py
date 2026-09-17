import sys, collections
from datetime import datetime
from pathlib import Path
from arctic_qa import ledger_store

def parse(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))

ledger = ledger_store.read_ledger(Path(sys.argv[1]))
windows = [(parse(sys.argv[i]), parse(sys.argv[i + 1])) for i in range(2, len(sys.argv), 2)]
for start, end in windows:
    counts = collections.Counter()
    for row in ledger["requests"].values():
        at = row.get("submitted_at_utc")
        if not at:
            continue
        moment = parse(at)
        if start <= moment <= end:
            counts[row.get("phase") or "construction(no phase)"] += 1
    minutes = (end - start).total_seconds() / 60.0
    print(f"{start:%H:%M}-{end:%H:%M}Z ({minutes:.1f} min): " + ", ".join(
        f"{name} {n} ({n/minutes:.2f}/min)" for name, n in sorted(counts.items())
    ) or "nothing")
