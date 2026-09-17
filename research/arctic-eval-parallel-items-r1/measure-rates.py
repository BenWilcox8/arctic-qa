"""Measure the streaming evaluator: per-arm completions per hour, in flight."""
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

WORK = Path("/mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r11")
STATE_DB = Path("/mnt/crdata/research-abstention/arctic-qa/state.sqlite3")
ARMS = {
    "google_gemini": ("gemini-3.8-flash", "gemini-3.7-flash"),
    "anthropic_claude_code": ("claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"),
    "openai_codex": ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"),
}
TRIALS_PER_MODEL = 6


def parse(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def rows():
    text = (WORK / "cost-journal.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def latest_at(items, moment):
    """The last row of each item recorded at or before `moment`."""
    latest = {}
    for row in items:
        if parse(row["recorded_at_utc"]) <= moment:
            latest[row["item_id"]] = row
    return latest


def arm_state(latest):
    """Per arm: the items whose every model of that arm has its 6 trials."""
    done = {arm: set() for arm in ARMS}
    for item_id, row in latest.items():
        counts = row.get("outcomes_by_model") or {}
        for arm, models in ARMS.items():
            if all(
                sum((counts.get(model) or {}).values()) >= TRIALS_PER_MODEL
                for model in models
            ):
                done[arm].add(item_id)
    return done


def accepted_rate():
    """Accepted questions of the campaign, and the rate over the last hour."""
    connection = sqlite3.connect(f"file:{STATE_DB}?mode=ro&immutable=0", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        found = connection.execute(
            "SELECT updated_at FROM candidates "
            "WHERE status='machine_accepted_unverified' "
            "AND run_id='arctic-qa-production-campaign-003' ORDER BY updated_at"
        ).fetchall()
    finally:
        connection.close()
    stamps = []
    for row in found:
        value = str(row["updated_at"])
        if "+" not in value and "Z" not in value:
            value += "Z"
        stamps.append(parse(value))
    now = datetime.now(UTC)
    hour = [s for s in stamps if s >= now - timedelta(hours=1)]
    return len(stamps), len(hour), stamps


def main():
    start = parse(sys.argv[1])
    end = parse(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(UTC)
    every = rows()
    items = [row for row in every if row.get("kind", "item") == "item"]
    before = arm_state(latest_at(items, start))
    after = arm_state(latest_at(items, end))
    minutes = (end - start).total_seconds() / 60.0
    print(f"window {start:%H:%M:%S}Z to {end:%H:%M:%S}Z ({minutes:.1f} minutes)")
    print()
    print(f"{'arm':24} {'complete at start':>17} {'at end':>7} {'gained':>7} {'per hour':>9}")
    for arm in ARMS:
        gained = len(after[arm] - before[arm])
        print(
            f"{arm:24} {len(before[arm]):>17} {len(after[arm]):>7} {gained:>7} "
            f"{gained * 60 / minutes:>9.1f}"
        )
    whole_before = set.intersection(*(before[a] for a in ARMS))
    whole_after = set.intersection(*(after[a] for a in ARMS))
    gained = len(whole_after - whole_before)
    print(
        f"{'all three arms':24} {len(whole_before):>17} {len(whole_after):>7} "
        f"{gained:>7} {gained * 60 / minutes:>9.1f}"
    )
    print()
    passes = [r for r in items if start <= parse(r["recorded_at_utc"]) <= end]
    print(f"journal rows in the window: {len(passes)} passes over "
          f"{len({r['item_id'] for r in passes})} questions")
    if passes:
        walls = [float(r["evaluation"]["wall_seconds"]) for r in passes]
        print(f"wall seconds per pass: mean {sum(walls)/len(walls):.1f}, "
              f"min {min(walls):.1f}, max {max(walls):.1f}")
        usd = sum(float(r["evaluation"]["google_gemini"]["usd"]) for r in passes)
        print(f"Gemini USD in the window: {usd:.4f}")
    watch = json.loads((WORK / "watch-state.json").read_text(encoding="utf-8"))
    print(f"item_workers {watch.get('item_workers')}, "
          f"in flight now {len(watch.get('items_in_flight') or [])}: "
          f"{watch.get('items_in_flight')}")
    print(f"active vendors {watch.get('active_vendors')}")
    total, hour, _ = accepted_rate()
    print(f"accepted questions of the campaign: {total} "
          f"({hour} in the last hour)")
    latest = latest_at(items, end)
    print(f"questions with a journal row: {len(latest)}")
    state = arm_state(latest)
    for arm in ARMS:
        print(f"  {arm}: {len(state[arm])} complete, "
              f"{len(latest) - len(state[arm])} open")
    counter = Counter()
    for row in latest.values():
        for arm, models in ARMS.items():
            for model in models:
                counter[model] += sum(
                    (row.get("outcomes_by_model") or {}).get(model, {}).values()
                )
    print("recorded trials per model:", dict(counter))


main()
