"""Count the paid Gemini calls of the evaluator in a window, from the ledger.

The cost journal records one row per pass over one question, and a revisit
carries the whole question's cost, so a short window cannot be read from it.
The shared paid-call ledger records every request with the minute it was
submitted, which is the call rate itself.

    measure-gemini-calls.py <ledger> 2026-09-17T10:57:36Z [2026-09-17T11:20:00Z]
"""
import sys
from datetime import UTC, datetime
from pathlib import Path

from arctic_qa import ledger_store

GEMINI_TRIALS_PER_ITEM = 12


def parse(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def main():
    ledger = ledger_store.read_ledger(Path(sys.argv[1]))
    start = parse(sys.argv[2])
    end = parse(sys.argv[3]) if len(sys.argv) > 3 else datetime.now(UTC)
    submitted = []
    for row in ledger["requests"].values():
        if row.get("phase") != "benchmark_evaluation":
            continue
        at = row.get("submitted_at_utc")
        if not at:
            continue
        moment = parse(at)
        if start <= moment <= end:
            submitted.append(moment)
    minutes = (end - start).total_seconds() / 60.0
    print(f"window {start:%H:%M:%S}Z to {end:%H:%M:%S}Z ({minutes:.1f} minutes)")
    print(f"paid Gemini calls submitted: {len(submitted)}")
    if submitted:
        rate = len(submitted) / minutes
        print(f"  {rate:.2f} calls a minute, "
              f"{rate * 60 / GEMINI_TRIALS_PER_ITEM:.1f} questions an hour")
        print(f"  first {min(submitted):%H:%M:%S}Z, last {max(submitted):%H:%M:%S}Z")


main()
