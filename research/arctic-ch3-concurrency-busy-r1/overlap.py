#!/usr/bin/env python
"""Count how many paid calls of one run were on the wire at once.

Reads the shared ledger and pairs each request of the run with its submitted
and completed times. The answer is the number of intervals that overlap, which
is what tells a concurrent producer from a serialized one.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

LEDGER = Path(
    "/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1"
    "/shared-paid-call-ledger.json"
)
RUN_ID = "chapter3-7dc6485-r3"


def moment(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def main() -> int:
    since = moment(sys.argv[1]) if len(sys.argv) > 1 else None
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    spans = []
    for request in ledger["requests"].values():
        if str(request.get("run_id")) != RUN_ID:
            continue
        started = request.get("submitted_at_utc")
        ended = request.get("completed_at_utc")
        if not started or not ended:
            continue
        start, end = moment(started), moment(ended)
        if since and start < since:
            continue
        spans.append((start, end))
    spans.sort()
    if not spans:
        print(json.dumps({"calls": 0}))
        return 0
    events = [(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans]
    events.sort()
    live = peak = 0
    overlapping = 0
    for _, delta in events:
        live += delta
        peak = max(peak, live)
    for index, (start, _) in enumerate(spans):
        if any(other_end > start for _, other_end in spans[:index]):
            overlapping += 1
    window = (spans[-1][1] - spans[0][0]).total_seconds() / 60.0
    durations = sorted((end - start).total_seconds() for start, end in spans)
    print(
        json.dumps(
            {
                "calls": len(spans),
                "first": spans[0][0].isoformat(),
                "last": spans[-1][1].isoformat(),
                "window_minutes": round(window, 2),
                "calls_per_minute": round(len(spans) / window, 2) if window else None,
                "peak_in_flight": peak,
                "calls_that_overlap_an_earlier_one": overlapping,
                "median_call_seconds": durations[len(durations) // 2],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
