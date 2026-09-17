#!/usr/bin/env bash
# Print ONE line when the streaming abstention evaluation has finished, and
# nothing at all while it still has work it can do.
#
# Task arctic-eval-reopen-r1, captain order 2026-09-17 15:48 UTC: the final
# dataset snapshot is taken when every evaluation has finished. This is the
# read-only detector of that moment. Firstmate wraps it in a timed check.
#
# Finished means one of two things.
#   1. Every accepted question of the campaign holds all 48 of its planned
#      responses.
#   2. What is left is a shortfall this snapshot cannot clear. The line names
#      it. Two causes are known:
#      - the question's run directory belongs to another run id, so this
#        evaluator would have to ask all 48 of its trials again, which the
#        evaluation policy forbids;
#      - every trial the question still owes belongs to a model that is paused
#        with no resume time, which is the captain's Fable cap.
#
# The line is data, not prose: it starts with "finished" and states the counts.
#
# Exit code: 0 when the detector could tell (with or without a line), non-zero
# when it could not. A non-zero exit is never "finished".
#
# Read-only. It opens the state database read-only, and it writes nothing.
#
# Usage: eval-finished.sh [work-dir] [state-db]

set -euo pipefail

WORK="${1:-/mnt/crdata/research-abstention/arctic-qa/abstention-eval/streaming-r11}"
STATE_DB="${2:-/mnt/crdata/research-abstention/arctic-qa/state.sqlite3}"
APP=/home/ben/.treehouse/firstmate-c40011/6/firstmate/data/arctic-eval-authorization-r8/runtime/app-7aca1b4-arctic-eval-authorization-r8
PAUSE_FILES=(
  "$APP/config/benchmark-evaluation-model-pause-v1.json"
  /mnt/crdata/research-abstention/arctic-qa/abstention-eval/guard-r1/model-pause.json
)
CAMPAIGN=arctic-qa-production-campaign-003

exec nice -n 10 nix develop "path:$APP" -c python - \
  "$WORK" "$STATE_DB" "$CAMPAIGN" "${PAUSE_FILES[@]}" <<'PYEOF'
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

WORK = Path(sys.argv[1])
STATE_DB = Path(sys.argv[2])
CAMPAIGN = sys.argv[3]
PAUSE_FILES = [Path(name) for name in sys.argv[4:]]
VENDORS = ("anthropic_claude_code", "google_gemini", "openai_codex")
PLANNED = 48


def read_jsonl(path):
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def latest_item_rows():
    latest = {}
    for row in read_jsonl(WORK / "cost-journal.jsonl"):
        if row.get("kind", "item") == "item":
            latest[str(row["item_id"])] = row
    return latest


def accepted_item_ids():
    """The accepted questions of the campaign, read from the state database."""
    connection = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    try:
        found = connection.execute(
            "SELECT item_id FROM candidates "
            "WHERE status='machine_accepted_unverified' AND run_id=?",
            (CAMPAIGN,),
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in found}


def permanently_paused_models():
    """The models paused with no resume time, which is the captain's hard cap.

    A pause with a resume time in the past holds nothing, and one with a
    resume time in the future is a wait, not an end. Only a pause with no
    resume time at all can make a trial unaskable.
    """
    now = datetime.now(UTC)
    result = set()
    for path in PAUSE_FILES:
        if not path.is_file():
            continue
        entries = (json.loads(path.read_text(encoding="utf-8")) or {}).get(
            "paused_models"
        ) or {}
        for model, entry in entries.items():
            resume = (entry or {}).get("resume_at_utc")
            if resume is None:
                result.add(model)
                continue
            moment = datetime.fromisoformat(str(resume).replace("Z", "+00:00"))
            if moment > now:
                result.discard(model)
    return result


def owed_by_model(item_id):
    """The planned trials of this question with no recorded response row."""
    run_dir = WORK / "runs" / item_id
    planned = {}
    recorded = set()
    for vendor in VENDORS:
        for trial in read_jsonl(run_dir / vendor / "trials.jsonl"):
            planned[str(trial["trial_id"])] = str(trial["model"])
        for row in read_jsonl(run_dir / vendor / "responses.jsonl"):
            recorded.add(str(row["trial_id"]))
    owed = Counter(
        model for trial, model in planned.items() if trial not in recorded
    )
    return len(recorded), owed


def main():
    latest = latest_item_rows()
    accepted = accepted_item_ids()
    paused = permanently_paused_models()

    unknown = sorted(accepted - set(latest))
    whole = 0
    blocked = []
    open_questions = 0
    never_answered = 0
    for item_id in sorted(accepted):
        row = latest.get(item_id)
        if row is None:
            continue
        if not (WORK / "runs" / item_id).is_dir():
            blocked.append(
                f"{item_id} holds {row['evaluation']['recorded_trials']} of "
                f"{PLANNED} and its run directory belongs to run id "
                f"{row.get('run_id')}"
            )
            continue
        recorded, owed = owed_by_model(item_id)
        never_answered += sum(
            1
            for vendor in VENDORS
            for line in read_jsonl(WORK / "runs" / item_id / vendor / "responses.jsonl")
            if (line.get("response") or {}).get("state") != "completed"
        )
        if recorded >= PLANNED and not owed:
            whole += 1
        elif owed and all(model in paused for model in owed):
            blocked.append(
                f"{item_id} owes {sum(owed.values())} trials of "
                f"{', '.join(sorted(owed))}, paused with no resume time"
            )
        else:
            open_questions += 1

    if unknown:
        return
    if open_questions:
        return
    if blocked:
        print(
            f"finished: {whole} of {len(accepted)} accepted questions hold all "
            f"{PLANNED} responses; {len(blocked)} cannot be cleared by the "
            f"reopen rule: {'; '.join(blocked)}; "
            f"{never_answered} recorded responses hold no provider answer"
        )
        return
    print(
        f"finished: every one of the {len(accepted)} accepted questions holds all "
        f"{PLANNED} responses; {never_answered} of {len(accepted) * PLANNED} "
        f"recorded responses hold no provider answer"
    )


main()
PYEOF
