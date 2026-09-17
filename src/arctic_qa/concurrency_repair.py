"""Repair of the papers one lock-contention fault hurt, and their dates.

Paper concurrency made the exclusive broker operation lock a queue, but the
first concurrent chapter 3 run kept the old bounded wait of 120 seconds and held
the free token count and the pacing wait inside the lock. Every peer thread
therefore queued behind a section that could last minutes, the wait expired and
the candidate fault containment recorded the paper with
``BrokerOperationBusyError``. 19 papers were recorded that way in the first 40
minutes of 2026-09-16.

Such a paper reserved nothing, submitted nothing and was charged nothing: the
refusal is about the lock, never about the paper. This module removes those
records so the next start analyses the paper again, and fills the completion
time of a label that was written before the label carried one.

No receipt and no ledger row is read, altered or deleted here. The module works
on the state database and on the run's progress file only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database, now
from .util import atomic_json, canonical_json


BUSY_ERROR_CLASS = "BrokerOperationBusyError"
REPAIR_SCHEMA = "streaming-concurrency-repair-v1"


def _fault_of(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("candidate_processing_fault")
    return value if isinstance(value, dict) else {}


def busy_fault_rejections(db: Database, *, campaign_id: str) -> list[dict[str, Any]]:
    """Return every routing row one operation-lock refusal wrote."""
    rows = []
    for row in db.rows(
        """SELECT rejection_id,source_id,detail_json,created_at
        FROM rejection_ledger
        WHERE stage='generation_routing' AND reason_code='candidate_processing_fault'
        ORDER BY created_at""",
        (),
    ):
        try:
            detail = json.loads(row["detail_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if detail.get("error_class") != BUSY_ERROR_CLASS:
            continue
        if detail.get("campaign_id") != campaign_id:
            continue
        rows.append(
            {
                "rejection_id": str(row["rejection_id"]),
                "candidate_key": str(detail.get("candidate_key") or ""),
                "family_id": str(detail.get("family_id") or ""),
                "source_id": row["source_id"],
                "stage": detail.get("stage"),
                "created_at": str(row["created_at"]),
            }
        )
    return rows


def busy_fault_call_records(db: Database, *, campaign_id: str) -> list[dict[str, Any]]:
    """Return every in-flight call record one operation-lock refusal marked."""
    rows = []
    for row in db.rows(
        """SELECT item_id,source_id,paper_family_id,candidate_json,updated_at
        FROM candidates WHERE run_id=? AND status='incomplete_infra'
        ORDER BY item_id""",
        (campaign_id,),
    ):
        try:
            record = json.loads(row["candidate_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if _fault_of(record).get("error_class") != BUSY_ERROR_CLASS:
            continue
        rows.append(
            {
                "item_id": str(row["item_id"]),
                "source_id": row["source_id"],
                "family_id": str(row["paper_family_id"]),
                "updated_at": str(row["updated_at"]),
                "record": record,
            }
        )
    return rows


def clear_busy_faults(
    db: Database,
    *,
    run_id: str,
    campaign_id: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Remove every record one operation-lock refusal left on a paper.

    The routing row goes, the fault note on the in-flight call record goes, and
    a completion label of such a paper goes with them, so the next start
    analyses the paper again. The call record itself stays: a row that says a
    call was opened and its outcome is unknown is evidence, and the producer
    settles it when it walks the family again.
    """
    rejections = busy_fault_rejections(db, campaign_id=campaign_id)
    call_records = busy_fault_call_records(db, campaign_id=campaign_id)
    keys = sorted({row["candidate_key"] for row in rejections if row["candidate_key"]})
    families = sorted(
        {row["family_id"] for row in rejections if row["family_id"]}
        | {row["family_id"] for row in call_records if row["family_id"]}
    )
    labels = [
        {
            "candidate_key": str(row["candidate_key"]),
            "outcome_class": str(row["outcome_class"]),
        }
        for row in db.rows(
            "SELECT candidate_key,outcome_class,paper_family_id FROM paper_completions "
            "WHERE run_id=? ORDER BY candidate_key",
            (run_id,),
        )
        if str(row["candidate_key"]) in set(keys)
        or str(row["paper_family_id"]) in set(families)
    ]
    report = {
        "schema": REPAIR_SCHEMA,
        "action": "clear-busy-faults",
        "run_id": run_id,
        "campaign_id": campaign_id,
        "applied": bool(apply),
        "generated_at_utc": now(),
        "papers": keys,
        "paper_count": len(keys),
        "rejection_rows": len(rejections),
        "call_records": len(call_records),
        "completion_labels_removed": len(labels),
        "labels": labels,
        "families": families,
    }
    if not apply:
        return report
    with db.transaction():
        for row in rejections:
            db.connection.execute(
                "DELETE FROM rejection_ledger WHERE rejection_id=?",
                (row["rejection_id"],),
            )
        for row in call_records:
            record = dict(row["record"])
            record.pop("candidate_processing_fault", None)
            record["reason_code"] = None
            db.connection.execute(
                "UPDATE candidates SET candidate_json=?,updated_at=? WHERE item_id=?",
                (canonical_json(record), now(), row["item_id"]),
            )
        for label in labels:
            db.connection.execute(
                "DELETE FROM paper_completions WHERE run_id=? AND candidate_key=?",
                (run_id, label["candidate_key"]),
            )
    return report


def _ledger_family_times(ledger_file: Path | None) -> dict[str, str]:
    """Return the last completed request time of each paper family."""
    if ledger_file is None or not ledger_file.is_file():
        return {}
    try:
        ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    times: dict[str, str] = {}
    for request in (ledger.get("requests") or {}).values():
        family_id = request.get("family_id")
        completed = request.get("completed_at_utc") or request.get("submitted_at_utc")
        if not family_id or not completed:
            continue
        key = str(family_id)
        if str(completed) > times.get(key, ""):
            times[key] = str(completed)
    return times


def _candidate_family_times(db: Database, *, campaign_id: str) -> dict[str, str]:
    times: dict[str, str] = {}
    for row in db.rows(
        "SELECT paper_family_id,updated_at FROM candidates WHERE run_id=?",
        (campaign_id,),
    ):
        family_id = str(row["paper_family_id"])
        value = str(row["updated_at"] or "")
        if value > times.get(family_id, ""):
            times[family_id] = value
    return times


def backfill_completion_dates(
    db: Database,
    *,
    run_id: str,
    campaign_id: str,
    ledger_file: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Fill the completion time of every label that was written without one.

    The time comes from the paper's own evidence: the last completed request of
    its family in the shared ledger, or the last update of one of its candidate
    rows. A label whose paper retains neither keeps no time, because a made-up
    time is worse than none.
    """
    ledger_times = _ledger_family_times(ledger_file)
    candidate_times = _candidate_family_times(db, campaign_id=campaign_id)
    filled: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for row in db.rows(
        """SELECT candidate_key,paper_family_id,completed_at_utc,labelled_at_utc
        FROM paper_completions WHERE run_id=? ORDER BY candidate_key""",
        (run_id,),
    ):
        if row["completed_at_utc"]:
            continue
        family_id = str(row["paper_family_id"])
        value = max(
            (
                time
                for time in (
                    ledger_times.get(family_id),
                    candidate_times.get(family_id),
                )
                if time
            ),
            default=None,
        )
        if value is None:
            unresolved.append(str(row["candidate_key"]))
            continue
        filled.append(
            {
                "candidate_key": str(row["candidate_key"]),
                "paper_family_id": family_id,
                "completed_at_utc": value,
                "source": (
                    "shared_ledger"
                    if value == ledger_times.get(family_id)
                    else "candidate_row"
                ),
            }
        )
    report = {
        "schema": REPAIR_SCHEMA,
        "action": "backfill-completion-dates",
        "run_id": run_id,
        "campaign_id": campaign_id,
        "applied": bool(apply),
        "generated_at_utc": now(),
        "filled": len(filled),
        "unresolved": len(unresolved),
        "unresolved_papers": unresolved,
        "papers": filled,
    }
    if not apply:
        return report
    with db.transaction():
        for item in filled:
            db.connection.execute(
                """UPDATE paper_completions SET completed_at_utc=?
                WHERE run_id=? AND candidate_key=? AND completed_at_utc IS NULL""",
                (item["completed_at_utc"], run_id, item["candidate_key"]),
            )
    return report


def backfill_progress_dates(
    progress_file: Path,
    *,
    completion_times: dict[str, str],
    apply: bool = False,
) -> dict[str, Any]:
    """Give every retained progress row the time its paper reached its state.

    The pipeline trace reads ``state_changed_at_utc`` of the progress row before
    anything else, so a row without it hides the time the receipts already
    answer. The producer writes the field now; this fills the rows a producer
    wrote before it did.
    """
    report: dict[str, Any] = {
        "schema": REPAIR_SCHEMA,
        "action": "backfill-progress-dates",
        "progress_file": str(progress_file),
        "applied": bool(apply),
        "generated_at_utc": now(),
        "rows": 0,
        "filled": 0,
        "unresolved": 0,
    }
    if not progress_file.is_file():
        report["progress_file_present"] = False
        return report
    progress = json.loads(progress_file.read_text(encoding="utf-8"))
    rows = progress.get("recent_papers")
    if not isinstance(rows, list):
        report["progress_file_present"] = False
        return report
    report["progress_file_present"] = True
    report["rows"] = len(rows)
    changed = False
    for row in rows:
        if not isinstance(row, dict) or row.get("state_changed_at_utc"):
            continue
        value = completion_times.get(str(row.get("paper_id") or ""))
        if not value:
            report["unresolved"] = int(report["unresolved"]) + 1
            continue
        row["state_changed_at_utc"] = value
        report["filled"] = int(report["filled"]) + 1
        changed = True
    if apply and changed:
        atomic_json(progress_file, progress)
    return report


def progress_times_by_paper(db: Database, *, run_id: str) -> dict[str, str]:
    """Return each labelled paper's completion time, by the id progress uses.

    A progress row names the paper by its ``source_id`` where the paper reached
    generation and by its candidate key otherwise, so both are answered.
    """
    times: dict[str, str] = {}
    for row in db.rows(
        """SELECT candidate_key,source_id,completed_at_utc FROM paper_completions
        WHERE run_id=? AND completed_at_utc IS NOT NULL""",
        (run_id,),
    ):
        value = str(row["completed_at_utc"])
        for identifier in (row["source_id"], row["candidate_key"]):
            if identifier:
                times[str(identifier)] = value
    return times
