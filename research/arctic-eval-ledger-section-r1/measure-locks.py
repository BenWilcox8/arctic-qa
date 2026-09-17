"""Measure the exclusive operation lock of the streaming evaluator.

The evaluator prints one line per section of the exclusive operation lock of
the shared paid-call ledger. The threshold is
`ARCTIC_QA_OPERATION_LOCK_LOG_SECONDS`, 1.0 s by default; the launcher of
snapshot 8477fd5 lowers it to 0.05 s so every section is in the log.

    measure-locks.py "05:57:00"              # a journalctl --since, in local time
    measure-locks.py "05:57:00" "06:20"      # and an --until
    measure-locks.py "05:57:00" "06:20" 1.0  # and the threshold of another run

The third argument drops every section under that threshold, which is how a
run at 0.05 s is compared with a run at the 1.0 s default. A window read at
the higher threshold is an undercount of the hold, never an overcount.
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
threshold = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
out = subprocess.run(command, capture_output=True, text=True).stdout
rows = []
for line in out.splitlines():
    if "[operation-lock]" not in line:
        continue
    m = re.search(r"section=(\S+).*waited_s=([0-9.]+) held_s=([0-9.]+)", line)
    if m:
        waited_s, held_s = float(m.group(2)), float(m.group(3))
        if max(waited_s, held_s) < threshold:
            continue
        rows.append((m.group(1), waited_s, held_s))
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
# A reserve section is not one paid call: the concurrency slots refuse a
# reservation often, and each refusal takes the section again. The paid calls
# of a window come from the ledger, through `measure-gemini-calls.py`, and the
# hold per call is this total over that count.
print(f"\n{len(rows)} sections at or above {threshold:.2f} s")
