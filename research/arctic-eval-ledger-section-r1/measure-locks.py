"""Measure the exclusive operation lock of the streaming evaluator.

The evaluator prints one line per section of the exclusive operation lock of
the shared paid-call ledger. The threshold is
`ARCTIC_QA_OPERATION_LOCK_LOG_SECONDS`, 1.0 s by default; the launcher of
snapshot 8477fd5 lowers it to 0.05 s so every section is in the log.

    measure-locks.py "05:57:00"          # a journalctl --since, in local time
    measure-locks.py "05:57:00" "06:20"  # and an --until
"""
import collections
import re
import subprocess
import sys

command = [
    "journalctl", "--user", "-u", "arctic-abstention-stream-r3",
    "--since", sys.argv[1], "--no-pager", "-o", "cat",
]
if len(sys.argv) > 2:
    command += ["--until", sys.argv[2]]
out = subprocess.run(command, capture_output=True, text=True).stdout
rows = []
for line in out.splitlines():
    if "[operation-lock]" not in line:
        continue
    m = re.search(r"section=(\S+).*waited_s=([0-9.]+) held_s=([0-9.]+)", line)
    if m:
        rows.append((m.group(1), float(m.group(2)), float(m.group(3))))
held = sum(r[2] for r in rows)
waited = sum(r[1] for r in rows)
print(f"sections {len(rows)}  held {held:.1f} s  waited {waited:.1f} s")
by = collections.defaultdict(lambda: [0, 0.0, 0.0])
for name, w, h in rows:
    by[name][0] += 1
    by[name][1] += w
    by[name][2] += h
for name, (n, w, h) in sorted(by.items(), key=lambda kv: -kv[1][2]):
    print(f"  {name:22} {n:3d} calls  waited {w:6.1f} s  held {h:6.1f} s  "
          f"mean wait {w/n:5.2f} s  mean hold {h/n:5.2f} s")
calls = by.get("reserve", [0])[0]
if calls:
    per_call = held / calls
    print(f"\nreserve sections (one per paid call): {calls}")
    print(f"exclusive section per paid call: {per_call:.2f} s held, "
          f"{waited / calls:.2f} s waited")
    print(f"a call an hour at that hold alone: {3600 / per_call:.0f} "
          f"({3600 / per_call / 12:.1f} questions an hour on the Gemini arm)")
