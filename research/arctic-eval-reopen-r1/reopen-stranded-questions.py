"""Re-open every question an arm stopped inside, for the trials nobody asked.

Captain order 2026-09-17 15:48 UTC: "reopen all the open questions that were
not evaluated on".

A question the streaming evaluator closed below its 48 planned responses is
closed for good: `CostJournal.completed_item_ids` reads the last row of the
question, and `evaluation.complete` of that row was set from the paused models
and the paused vendors alone. A busy ledger lock, a missing harness binary, a
vendor pause or a stop of the unit is none of those, so the row said complete
while `recorded_trials` was far below `planned_trials`. The record is
`research/arctic-eval-busy-strand-r1/report.md`.

This operation re-opens such a question. It follows the four bounds that
report states.

1. Scope. Only a trial with NO recorded response row is re-opened. A trial
   whose row records an answer, a refusal or a failure went out once and is
   never asked again: `abstention_plan.run_vendor` reads the recorded rows as
   its `done` set, so those trials stay done whatever this operation writes.
   The re-opened trials are exactly the ones the provider never saw.
2. The record. One appended row per question, never an edit. The row is the
   superseded row with its costs unchanged, so no total moves, plus a
   `reopen` block that names the captain order, the reason, the trials owed
   per model and the trial ids.
3. The money. The Gemini half is paid. The operation prints the projected USD
   at the run's own measured rate before it writes anything.
4. The rate. `--vendor` and `--limit` cut the questions into batches, so the
   first batch can reach an idle evaluator in a minute and the rest can follow
   while it scores. Inside one batch the evaluator paces itself:
   `abstention_watch.wave_order` cuts the backlog into waves of
   `item_workers * WAVE_CYCLES` and gives every arm a share of each one.

A batch may be applied while the evaluator runs. Two rules keep that safe: a
question the evaluator holds in flight is never re-opened, because the row that
pass appends would supersede the re-opened one; and the rows are written with
one `os.write` to a file opened `O_APPEND`, which the kernel serializes against
the evaluator's own append.

Re-opening is idempotent. A question whose last row is already this task's
re-open of the same trials is left alone.

A question whose run directory belongs to another run id is NOT re-opened:
the evaluator would build a new run directory under its own run id, find no
recorded row and ask all 48 trials again, which is the retry the evaluation
policy forbids. Such a question is reported and left alone.

Run it with the evaluator stopped. Read-only without `--apply`.

    python reopen-stranded-questions.py --work-dir <dir> [--apply]
"""

import argparse
import json
import os
from collections import Counter, OrderedDict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

VENDORS = ("anthropic_claude_code", "google_gemini", "openai_codex")
MODEL_VENDOR = {
    "claude-fable-5-1": "anthropic_claude_code",
    "claude-opus-5": "anthropic_claude_code",
    "claude-sonnet-5": "anthropic_claude_code",
    "gemini-3.7-flash": "google_gemini",
    "gemini-3.8-flash": "google_gemini",
    "gpt-5.6-sol": "openai_codex",
    "gpt-5.6-terra": "openai_codex",
    "gpt-6-astra": "openai_codex",
}
CAPTAIN_ORDER = "captain order 2026-09-17 15:48 UTC"
TASK = "arctic-eval-reopen-r1"
REOPEN_SCHEMA = "abstention-eval-stranded-question-reopen-v1"
REASON = (
    "the question is not whole: an arm stopped inside it before the trials "
    "below went out, so the provider never saw them, and the row that closed "
    "the question set evaluation.complete from the paused models and the "
    "paused vendors alone. Only the trials with no recorded response row are "
    "re-opened; every recorded trial stays as it was written. See "
    "research/arctic-eval-busy-strand-r1/report.md and "
    "research/arctic-eval-reopen-r1/report.md."
)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_item_rows(journal: Path) -> "OrderedDict[str, dict]":
    latest: OrderedDict[str, dict] = OrderedDict()
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind", "item") != "item":
            continue
        latest[str(row["item_id"])] = row
    return latest


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def missing_trials(run_dir: Path) -> tuple[dict[str, str], list[dict]]:
    """The planned trials with no response row, and the recorded non-answers.

    The second half is what this operation cannot re-open: a trial the
    provider never answered but whose refusal or failure is already a
    recorded row. Re-asking it needs the row and its receipt to be superseded,
    which is a change to the money evidence and not a journal operation.
    """
    planned: dict[str, str] = {}
    recorded: dict[str, dict] = {}
    for vendor in VENDORS:
        for trial in read_jsonl(run_dir / vendor / "trials.jsonl"):
            planned[str(trial["trial_id"])] = str(trial["model"])
        for row in read_jsonl(run_dir / vendor / "responses.jsonl"):
            recorded[str(row["trial_id"])] = row
    owed = {
        trial_id: model
        for trial_id, model in planned.items()
        if trial_id not in recorded
    }
    unanswered = [
        row
        for row in recorded.values()
        if (row.get("response") or {}).get("state") != "completed"
    ]
    return owed, unanswered


def gemini_usd_per_trial(latest: "OrderedDict[str, dict]") -> Decimal:
    usd = sum(
        (Decimal(row["evaluation"]["google_gemini"]["usd"]) for row in latest.values()),
        Decimal("0"),
    )
    trials = sum(
        count
        for row in latest.values()
        for model, outcomes in (row.get("outcomes_by_model") or {}).items()
        if model.startswith("gemini")
        for count in outcomes.values()
    )
    return (usd / trials) if trials else Decimal("0")


def reopen_row(row: dict, owed: dict[str, str], now: str) -> dict:
    """The superseded row, its costs unchanged, re-opened for the owed trials.

    ``pending_deferred_trials`` is the field `CostJournal.row_is_complete`
    already reads for a trial the provider never saw, which is exactly what
    each owed trial is. The paused-model and paused-vendor fields are cleared,
    because they described the pass that closed the question: what the
    question owes now is a set of trials nobody has ever asked, and the next
    pass records its own pauses itself.
    """
    fixed = json.loads(json.dumps(row))
    fixed.pop("error", None)
    per_model = Counter(owed.values())
    fixed["complete"] = False
    evaluation = fixed["evaluation"]
    evaluation["complete"] = False
    evaluation["models_paused"] = []
    evaluation["vendors_paused"] = []
    evaluation["pending_paused_trials"] = 0
    evaluation["pending_deferred_trials"] = len(owed)
    evaluation["deferred_reasons"] = sorted(
        set(evaluation.get("deferred_reasons") or [])
        | {f"reopened by {TASK}: {CAPTAIN_ORDER}"}
    )
    fixed["recorded_at_utc"] = now
    fixed["reopen"] = {
        "schema": REOPEN_SCHEMA,
        "task": TASK,
        "captain_order": CAPTAIN_ORDER,
        "reason": REASON,
        "reopened_at_utc": now,
        "supersedes_recorded_at_utc": row.get("recorded_at_utc"),
        "closed_error": row.get("error"),
        "recorded_trials": int(evaluation["recorded_trials"]),
        "planned_trials": int(evaluation["planned_trials"]),
        "trials_reopened": len(owed),
        "trials_reopened_by_model": dict(sorted(per_model.items())),
        "trial_ids": sorted(owed),
    }
    return fixed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report-file", type=Path)
    parser.add_argument(
        "--vendor",
        action="append",
        choices=VENDORS,
        help="re-open only the questions that owe this arm a trial; repeatable",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="re-open at most this many questions, oldest question first",
    )
    arguments = parser.parse_args()

    work_dir: Path = arguments.work_dir
    journal = work_dir / "cost-journal.jsonl"
    latest = latest_item_rows(journal)
    rate = gemini_usd_per_trial(latest)

    in_flight = set(
        json.loads((work_dir / "watch-state.json").read_text(encoding="utf-8")).get(
            "items_in_flight"
        )
        or []
    ) if (work_dir / "watch-state.json").is_file() else set()

    reopened: list[dict] = []
    skipped: list[dict] = []
    held: list[str] = []
    already: list[str] = []
    unanswered_rows: list[dict] = []
    by_model: Counter = Counter()
    for item_id, row in latest.items():
        run_dir = work_dir / "runs" / item_id
        if not run_dir.is_dir():
            skipped.append(
                {
                    "item_id": item_id,
                    "run_id": row.get("run_id"),
                    "recorded_trials": row["evaluation"]["recorded_trials"],
                    "planned_trials": row["evaluation"]["planned_trials"],
                    "why": (
                        "the run directory of this question belongs to another "
                        "run id, so this evaluator would ask all of its trials "
                        "again, which the evaluation policy forbids"
                    ),
                }
            )
            continue
        owed, unanswered = missing_trials(run_dir)
        for entry in unanswered:
            unanswered_rows.append(
                {
                    "item_id": item_id,
                    "model": entry["model"],
                    "state": (entry.get("response") or {}).get("state"),
                    "error": (entry.get("response") or {}).get("error"),
                }
            )
        if not owed:
            continue
        if item_id in in_flight:
            # The pass that holds it appends its own row when it finishes, and
            # that row would supersede this one.
            held.append(item_id)
            continue
        earlier = row.get("reopen") or {}
        if earlier.get("task") == TASK and earlier.get("trial_ids") == sorted(owed):
            already.append(item_id)
            continue
        if arguments.vendor and not {
            MODEL_VENDOR[model] for model in owed.values()
        } & set(arguments.vendor):
            continue
        if arguments.limit is not None and len(reopened) >= int(arguments.limit):
            continue
        by_model.update(owed.values())
        reopened.append({"item_id": item_id, "row": reopen_row(row, owed, utc_now())})

    gemini_trials = sum(
        count for model, count in by_model.items() if model.startswith("gemini")
    )
    summary = {
        "schema": "abstention-eval-reopen-summary-v1",
        "task": TASK,
        "captain_order": CAPTAIN_ORDER,
        "work_dir": str(work_dir),
        "measured_at_utc": utc_now(),
        "applied": bool(arguments.apply),
        "questions_with_a_row": len(latest),
        "questions_reopened": len(reopened),
        "trials_reopened": sum(by_model.values()),
        "trials_reopened_by_model": dict(sorted(by_model.items())),
        "gemini_trials_reopened": gemini_trials,
        "gemini_usd_per_trial": str(rate),
        "projected_gemini_usd": str((rate * gemini_trials).quantize(Decimal("0.000001"))),
        "questions_not_reopened": skipped,
        "questions_in_flight_left_alone": held,
        "questions_already_reopened": already,
        "recorded_trials_the_provider_never_answered": unanswered_rows,
        "reopened_item_ids": [entry["item_id"] for entry in reopened],
    }
    if arguments.apply:
        # One os.write per row to an O_APPEND file: the kernel serializes it
        # against the evaluator's own append, so a batch may be applied while
        # the evaluator scores.
        handle = os.open(journal, os.O_WRONLY | os.O_APPEND)
        try:
            for entry in reopened:
                payload = (json.dumps(entry["row"], sort_keys=True) + "\n").encode(
                    "utf-8"
                )
                written = os.write(handle, payload)
                if written != len(payload):
                    raise OSError("the journal append was cut short")
        finally:
            os.close(handle)
    if arguments.report_file:
        arguments.report_file.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    printable = dict(summary)
    printable["reopened_item_ids"] = f"{len(reopened)} ids"
    printable["recorded_trials_the_provider_never_answered"] = (
        f"{len(unanswered_rows)} rows"
    )
    print(json.dumps(printable, indent=2, sort_keys=True))


main()
