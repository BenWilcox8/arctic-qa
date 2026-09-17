"""The rates of the streaming evaluator, with the Gemini arm read from spend.

`research/arctic-eval-parallel-items-r1/measure-rates.py` counts the questions
whose arm is complete, which is the right number over a long window and says
nothing over a short one: a wave of eight questions finishes nothing for
several minutes. The Gemini arm has a second measure that answers at once,
because a Gemini trial costs money: `charged_calls` and `usd` of each row the
window holds. A row records one pass over one question, so the trials of a
pass are the trials the arm ran in the window.

    measure-rates.py 2026-09-17T10:57:36Z [2026-09-17T11:20:00Z]
"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

WORK = Path("/mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r11")
GEMINI_TRIALS_PER_ITEM = 12


def parse(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def main():
    start = parse(sys.argv[1])
    end = parse(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(UTC)
    text = (WORK / "cost-journal.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    items = [row for row in rows if row.get("kind", "item") == "item"]
    window = [r for r in items if start <= parse(r["recorded_at_utc"]) <= end]
    minutes = (end - start).total_seconds() / 60.0
    print(f"window {start:%H:%M:%S}Z to {end:%H:%M:%S}Z ({minutes:.1f} minutes)")
    usd = sum(float(r["evaluation"]["google_gemini"]["usd"]) for r in window)
    calls = sum(
        int(r["evaluation"]["google_gemini"].get("charged_calls") or 0) for r in window
    )
    print(f"journal rows in the window: {len(window)} over "
          f"{len({r['item_id'] for r in window})} questions")
    print(f"Gemini USD recorded in the window: {usd:.4f}")
    if calls:
        print(f"Gemini trials recorded in the window: {calls} "
              f"({calls / minutes:.2f} a minute, "
              f"{calls * 60 / minutes / GEMINI_TRIALS_PER_ITEM:.1f} questions an hour)")
    else:
        print("no Gemini trial recorded in the window")


main()
