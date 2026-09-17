import os, sys, time, statistics
from pathlib import Path
sys.path.insert(0,"src")
from arctic_qa.util import atomic_write
def bench(root, label):
    d = Path(root); d.mkdir(parents=True, exist_ok=True)
    for size, name in ((200, "tiny"), (163305, "status"), (8166160, "ledger")):
        xs=[]
        for i in range(8):
            data = b"z"*size + bytes(str(i),"ascii")
            s=time.perf_counter(); atomic_write(d/f"b-{name}.bin", data); xs.append(time.perf_counter()-s)
        print(f"{label:10s} atomic_write {name:7s} {size:>9d} B  med {statistics.median(xs)*1000:8.1f} ms  max {max(xs)*1000:8.1f}")
    xs=[]
    p = d/"b-append.log"
    with open(p,"ab") as h:
        for i in range(20):
            s=time.perf_counter(); h.write(b"line %d\n"%i); h.flush(); os.fsync(h.fileno()); xs.append(time.perf_counter()-s)
    print(f"{label:10s} append+fsync (200 B)            med {statistics.median(xs)*1000:8.1f} ms  max {max(xs)*1000:8.1f}")
bench(sys.argv[1], sys.argv[2])
