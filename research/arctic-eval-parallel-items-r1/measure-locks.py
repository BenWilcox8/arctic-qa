import collections
import re
import subprocess

out = subprocess.run(
    ["journalctl", "--user", "-u", "arctic-abstention-stream-r3", "--since", "04:44:00",
     "--no-pager", "-o", "cat"],
    capture_output=True, text=True).stdout
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
          f"mean hold {h/n:5.2f} s")
