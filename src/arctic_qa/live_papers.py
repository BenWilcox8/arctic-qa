"""The papers the chapter 3 producer analyzes at this moment.

The producer runs several papers of one stream at the same time, one thread per
paper. Neither the progress record nor the shared ledger names that thread, so
this module never invents one: it derives the live picture from the record every
paid call already leaves behind.

Three read-only inputs make that picture:

* The shared paid-call ledger. Every model call of the run is one request row
  with ``family_id``, ``paper_id``, ``stage``, ``submitted_at_utc`` and
  ``completed_at_utc``. The rows of one paper family give its stage, its call
  count, its cost and the minute its current work started.
* The streaming progress record. It gives the producer state, the campaign, the
  invocation run id and the last 100 papers with a final state.
* The pipeline state database. It gives the title and the DOI of a paper, the
  accepted question count of a family, and the per-paper completion labels of
  the run.

The rule for "in analysis now" is one sentence: a paper family of the current
run whose most recent paid call moved inside the active window, and which has no
completion label and no final state yet.

Nothing here writes. The ledger is read under the shared form of the lock the
broker writes under, the state database is opened read-only, and no path of this
module touches the producer, a launcher or the evaluator.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


LIVE_PAPERS_SCHEMA = "corpus-viewer-live-papers-v1"
LEDGER_SCHEMA = "shared-paid-call-ledger-v1"

# A paper counts as in analysis while one of its paid calls moved inside this
# window. The window is longer than the 300-second call timeout of the
# generation stages, so a paper that waits on one slow call stays visible.
#
# The same window also separates two visits to one paper. A relaunched producer
# replays the receipts of a paper it visited hours ago, so the time in analysis
# is measured from the start of the current burst of calls, never from the first
# call the family ever made.
ACTIVE_WINDOW_SECONDS = 420

# The number of finished papers the section keeps.
FINISHED_LIMIT = 10

# The producer state that means a live run. The progress record is also called
# stale when its own timestamp is older than the process staleness bound, which
# is the rule the rest of the viewer already applies to this file.
RUNNING_STATE = "running"

# The candidate status of one accepted benchmark item. The other statuses are
# rejections, short-answer-only items and call records.
ACCEPTED_CANDIDATE_STATUS = "machine_accepted_unverified"
SHORT_ANSWER_CANDIDATE_STATUS = "incomplete_non_mcq"

# The completion label outcome classes, mapped to the words the section shows.
OUTCOME_LABELS = {
    "generation_accepted": "accepted",
    "incomplete_non_mcq": "accepted, short answer only",
    "generation_rejected": "rejected",
    "eligibility_excluded": "screened out",
    "eligibility_unresolved": "screened out, unresolved",
    "paper_cost_cap_reached": "cost cap reached",
}

# The final state the producer writes into the progress record for a paper that
# carries no completion label yet, mapped to the same words.
FINAL_STATE_OUTCOMES = {
    "accepted": "generation_accepted",
    "incomplete_non_mcq": "incomplete_non_mcq",
    "generation_rejected": "generation_rejected",
    "rejected": "eligibility_excluded",
    "unresolved": "eligibility_unresolved",
    "paper_cost_cap_reached": "paper_cost_cap_reached",
}

# A request that is still open. Every other state is settled money.
OPEN_REQUEST_STATES = frozenset({"submitted", "counting"})

# The evaluator shares this ledger. Its rows carry another run id, and this
# phase name is the second guard against them.
EVALUATION_PHASE = "benchmark_evaluation"

_LOCK_WAIT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.01


def _instant(value: Any) -> datetime | None:
    """Return one UTC instant from a ledger or progress timestamp."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _decimal_text(total: float) -> str:
    return f"{total:.6f}"


def _money(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read_shared_ledger(path: Path) -> dict[str, Any]:
    """Read the shared paid-call ledger under the shared form of its lock.

    The broker takes an exclusive lock on the sidecar lock file around every
    ledger write. This reader asks for the shared form of that same lock, so two
    readers never wait for each other and a writer is never interrupted.

    The lock is taken without a wait and retried for two seconds. A live
    producer holds the exclusive lock for a few milliseconds at a time, so the
    wait almost never ends. If it does end, the ledger is read without the lock:
    the broker replaces the file with ``os.replace``, so an unlocked read still
    sees one whole version. The read must never delay a paid call, and a viewer
    that waits on a producer is worse than a viewer that reads a settled file.
    """
    lock_path = path.with_name(f".{path.name}.lock")
    raw = _read_bytes_under_shared_lock(path, lock_path)
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != LEDGER_SCHEMA:
        raise ValueError("the shared paid-call ledger schema is invalid")
    if not isinstance(value.get("requests"), dict):
        raise ValueError("the shared paid-call ledger has no request index")
    return value


def _read_bytes_under_shared_lock(path: Path, lock_path: Path) -> bytes:
    if not lock_path.is_file():
        return path.read_bytes()
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    with lock_path.open("rb") as lock:
        held = False
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            return path.read_bytes()
        finally:
            if held:
                fcntl.flock(lock, fcntl.LOCK_UN)


def read_state_facts(
    db_file: Path | None, *, campaign_id: str, run_id: str
) -> dict[str, Any]:
    """Read the paper identities, the accepted counts and the completion labels.

    The database is opened read-only. The completion labels are written by the
    paper-completion task, so a database whose ``paper_completions`` table is
    absent gives no labels and no error.
    """
    empty: dict[str, Any] = {
        "families": {},
        "source_families": {},
        "completions": {},
        "completion_labels_present": False,
    }
    if db_file is None or not db_file.is_file():
        return empty
    connection = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        families: dict[str, dict[str, Any]] = {}
        source_families: dict[str, str] = {}
        for row in connection.execute(
            "SELECT source_id,doi,title,paper_family_id FROM sources"
        ):
            family_id = _text(row["paper_family_id"])
            source_id = _text(row["source_id"])
            if not family_id:
                continue
            if source_id:
                source_families[source_id] = family_id
            record = families.setdefault(
                family_id,
                {"title": "", "doi": "", "source_id": "", "accepted_questions": 0},
            )
            if not record["title"]:
                record["title"] = _text(row["title"])
            if not record["doi"]:
                record["doi"] = _text(row["doi"])
            if not record["source_id"]:
                record["source_id"] = source_id
        for row in connection.execute(
            """SELECT paper_family_id,status,COUNT(*) AS total FROM candidates
            WHERE run_id=? AND status IN (?,?) GROUP BY paper_family_id,status""",
            (campaign_id, ACCEPTED_CANDIDATE_STATUS, SHORT_ANSWER_CANDIDATE_STATUS),
        ):
            family_id = _text(row["paper_family_id"])
            if not family_id:
                continue
            record = families.setdefault(
                family_id,
                {"title": "", "doi": "", "source_id": "", "accepted_questions": 0},
            )
            if row["status"] == ACCEPTED_CANDIDATE_STATUS:
                record["accepted_questions"] = int(row["total"])
            else:
                record["short_answer_questions"] = int(row["total"])
        completions, present = _read_completions(connection, run_id=run_id)
        return {
            "families": families,
            "source_families": source_families,
            "completions": completions,
            "completion_labels_present": present,
        }
    finally:
        connection.close()


def _read_completions(
    connection: sqlite3.Connection, *, run_id: str
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Return the completion labels of one run, by paper family."""
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_completions'"
    ).fetchone()
    if table is None:
        return {}, False
    labels: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        """SELECT paper_family_id,candidate_key,outcome_class,reason_code,
        eligibility_decision,labelled_at_utc FROM paper_completions WHERE run_id=?""",
        (run_id,),
    ):
        family_id = _text(row["paper_family_id"])
        if family_id:
            labels[family_id] = dict(row)
    return labels, True


def _empty_family(family_id: str) -> dict[str, Any]:
    """Return the call aggregate of a paper this run has not called."""
    return {
        "family_id": family_id,
        "paper_id": "",
        "calls": 0,
        "open_calls": 0,
        "cost_usd": 0.0,
        "reserved_usd": 0.0,
        "events": [],
        "latest": None,
        "latest_stage": "",
        "models": set(),
    }


def _ledger_families(
    ledger: dict[str, Any], *, run_id: str
) -> dict[str, dict[str, Any]]:
    """Group the paid calls of one invocation run by paper family."""
    families: dict[str, dict[str, Any]] = {}
    for request in ledger.get("requests", {}).values():
        if not isinstance(request, dict):
            continue
        if _text(request.get("run_id")) != run_id:
            continue
        if _text(request.get("phase")) == EVALUATION_PHASE:
            continue
        family_id = _text(request.get("family_id"))
        if not family_id:
            continue
        record = families.setdefault(family_id, _empty_family(family_id))
        if not record["paper_id"]:
            record["paper_id"] = _text(request.get("paper_id"))
        record["calls"] += 1
        record["cost_usd"] += _money(request.get("actual_cost_usd"))
        state = _text(request.get("state"))
        if state in OPEN_REQUEST_STATES:
            record["open_calls"] += 1
            record["reserved_usd"] += _money(request.get("reserved_usd"))
        model = _text(request.get("model"))
        if model:
            record["models"].add(model)
        stage = _text(request.get("stage"))
        submitted = _instant(request.get("submitted_at_utc"))
        completed = _instant(request.get("completed_at_utc"))
        for moment in (submitted, completed):
            if moment is not None:
                record["events"].append(moment)
        moment = completed or submitted
        if moment is not None and (
            record["latest"] is None or moment > record["latest"]
        ):
            record["latest"] = moment
            record["latest_stage"] = stage
    return families


def _burst_start(events: list[datetime], *, window: timedelta) -> datetime | None:
    """Return the first call of the current uninterrupted burst of work.

    A relaunched producer replays the receipts of every paper it visited before,
    so the first call of a family can be hours older than the work happening
    now. The burst ends wherever two calls are further apart than the window.
    """
    if not events:
        return None
    ordered = sorted(events)
    start = ordered[-1]
    for index in range(len(ordered) - 1, 0, -1):
        if ordered[index] - ordered[index - 1] > window:
            break
        start = ordered[index - 1]
    return start


def _progress_finals(
    progress: dict[str, Any],
    *,
    source_families: dict[str, str],
    paper_families: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Return the final state of every recent paper, keyed by paper family.

    The producer names a paper by its DOI before the source is imported and by
    its source id after it. Both names reach the same family: the source id
    through the state database, the DOI through the paid calls of the ledger.
    """
    finals: dict[str, dict[str, Any]] = {}
    rows = progress.get("recent_papers")
    if not isinstance(rows, list):
        return finals
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        paper_id = _text(row.get("paper_id"))
        family_id = source_families.get(paper_id) or paper_families.get(
            paper_id.casefold()
        )
        if not family_id:
            continue
        finals[family_id] = {
            "position": position,
            "paper_id": paper_id,
            "title": _text(row.get("title")),
            "current_stage": _text(row.get("current_stage")),
            "final_state": _text(row.get("final_state")),
            "final_reason": _text(row.get("final_reason")),
        }
    return finals


def _producer(
    progress: dict[str, Any] | None,
    *,
    now: datetime,
    process_stale_after_seconds: int,
) -> dict[str, Any]:
    if not progress:
        return {
            "running": False,
            "state": None,
            "message": "No streaming-pipeline progress record is selected.",
            "run_id": None,
            "campaign_id": None,
            "updated_at_utc": None,
        }
    state = _text(progress.get("state")) or None
    updated = _instant(progress.get("updated_at_utc"))
    age = (now - updated).total_seconds() if updated else None
    stale = age is None or age > process_stale_after_seconds
    running = state == RUNNING_STATE and not stale
    if running:
        message = _text(progress.get("message")) or "The producer is running."
    elif state == RUNNING_STATE:
        message = (
            "The last producer record says running, but it is older than "
            f"{process_stale_after_seconds} seconds. No producer is running."
        )
    else:
        message = _text(progress.get("message")) or "No producer is running."
    return {
        "running": running,
        "state": state,
        "message": message,
        "run_id": _text(progress.get("invocation_run_id")) or None,
        "campaign_id": _text(progress.get("run_id")) or None,
        "updated_at_utc": _text(progress.get("updated_at_utc")) or None,
        "record_age_seconds": int(age) if age is not None else None,
        "stale": stale,
    }


def live_papers_report(
    *,
    ledger: dict[str, Any] | None,
    progress: dict[str, Any] | None,
    facts: dict[str, Any] | None = None,
    titles: dict[str, str] | None = None,
    now: datetime | None = None,
    window_seconds: int = ACTIVE_WINDOW_SECONDS,
    finished_limit: int = FINISHED_LIMIT,
    process_stale_after_seconds: int = 300,
    error: str | None = None,
) -> dict[str, Any]:
    """Derive the papers in analysis now and the papers that finished last.

    ``facts`` carries the state-database half: the family identities, the
    accepted question counts and the completion labels. ``titles`` is the
    corpus index of the viewer, keyed by folded DOI, and names a paper the state
    database has not imported yet.
    """
    moment = now or datetime.now(UTC)
    window = timedelta(seconds=max(int(window_seconds), 1))
    producer = _producer(
        progress, now=moment, process_stale_after_seconds=process_stale_after_seconds
    )
    report: dict[str, Any] = {
        "schema": LIVE_PAPERS_SCHEMA,
        "generated_at_utc": moment.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "availability": "available",
        "window_seconds": int(window.total_seconds()),
        "producer": producer,
        "in_analysis": [],
        "finished": [],
        "completion_labels_present": bool(
            (facts or {}).get("completion_labels_present")
        ),
        "message": "",
    }
    if error:
        return {**report, "availability": "unavailable", "message": error}
    if ledger is None or not producer["run_id"]:
        return {
            **report,
            "availability": "not_selected",
            "message": (
                "No shared paid-call ledger and streaming progress record are "
                "selected, so no live paper can be named."
            ),
        }

    facts = facts or {}
    families = _ledger_families(ledger, run_id=producer["run_id"])
    identities = dict(facts.get("families") or {})
    completions = dict(facts.get("completions") or {})
    paper_families = {
        _text(record["paper_id"]).casefold(): family_id
        for family_id, record in families.items()
        if _text(record["paper_id"])
    }
    finals = _progress_finals(
        progress or {},
        source_families=dict(facts.get("source_families") or {}),
        paper_families=paper_families,
    )

    # A paper this run replayed from stored receipts made no paid call, so the
    # ledger holds no row of it. Its completion label or its final state is the
    # whole record of it, and the finished table must still name it.
    known = list(families) + [
        family_id
        for family_id in list(completions) + list(finals)
        if family_id not in families
    ]
    in_analysis: list[dict[str, Any]] = []
    finished: list[dict[str, Any]] = []
    for family_id in known:
        record = families.get(family_id) or _empty_family(family_id)
        identity = identities.get(family_id) or {}
        final = finals.get(family_id) or {}
        label = completions.get(family_id) or {}
        title = (
            _text(identity.get("title"))
            or _text(final.get("title"))
            or _text((titles or {}).get(_text(record["paper_id"]).casefold()))
        )
        paper_id = (
            _text(record["paper_id"])
            or _text(identity.get("doi"))
            or _text(final.get("paper_id"))
        )
        latest: datetime | None = record["latest"]
        terminal = bool(label) or bool(_text(final.get("final_state")))
        if not terminal and latest is not None and moment - latest <= window:
            started = _burst_start(record["events"], window=window) or latest
            in_analysis.append(
                {
                    "family_id": family_id,
                    "paper_id": paper_id,
                    "title": title or paper_id or family_id,
                    "stage": record["latest_stage"] or None,
                    "calls": record["calls"],
                    "open_calls": record["open_calls"],
                    "cost_usd": _decimal_text(record["cost_usd"]),
                    "reserved_usd": _decimal_text(record["reserved_usd"]),
                    "models": sorted(record["models"]),
                    "started_at_utc": _stamp(started),
                    "latest_at_utc": _stamp(latest),
                    "in_analysis_seconds": int((moment - started).total_seconds()),
                    # The producer names no thread and no slot in any record it
                    # writes, so this column stays empty until one does.
                    "slot": None,
                }
            )
            continue
        if not terminal:
            continue
        finished.append(
            _finished_row(
                family_id=family_id,
                paper_id=paper_id,
                title=title or paper_id or family_id,
                record=record,
                identity=identity,
                label=label,
                final=final,
            )
        )

    in_analysis.sort(key=lambda row: (row["started_at_utc"] or "", row["family_id"]))
    finished.sort(
        key=lambda row: (row["finished_at_utc"] or "", row["_position"]), reverse=True
    )
    report["in_analysis"] = in_analysis
    report["finished"] = [
        {key: value for key, value in row.items() if key != "_position"}
        for row in finished[:finished_limit]
    ]
    report["message"] = _message(producer, in_analysis)
    return report


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _finished_row(
    *,
    family_id: str,
    paper_id: str,
    title: str,
    record: dict[str, Any],
    identity: dict[str, Any],
    label: dict[str, Any],
    final: dict[str, Any],
) -> dict[str, Any]:
    outcome_class = _text(label.get("outcome_class")) or FINAL_STATE_OUTCOMES.get(
        _text(final.get("final_state")), ""
    )
    reason_code = _text(label.get("reason_code")) or _text(final.get("final_reason"))
    # The last paid call of the paper is when the run stopped working on it. The
    # label time is not: a batch catch-up labels every finished paper of a run in
    # one transaction, so it gives every row the same minute and destroys the
    # order. The label time is the answer only for a paper this run replayed
    # from stored receipts, which made no call of its own.
    finished_at = _stamp(record["latest"]) or _stamp(
        _instant(label.get("labelled_at_utc"))
    )
    accepted = int(identity.get("accepted_questions") or 0)
    return {
        "_position": int(final.get("position") or 0),
        "family_id": family_id,
        "paper_id": paper_id,
        "title": title,
        "outcome_class": outcome_class or None,
        "outcome": OUTCOME_LABELS.get(outcome_class)
        or (_text(final.get("final_state")) or "unknown"),
        "reason_code": reason_code or None,
        "question_count": accepted,
        "calls": record["calls"],
        "cost_usd": _decimal_text(record["cost_usd"]),
        "finished_at_utc": finished_at,
        "labelled": bool(label),
    }


def _message(producer: dict[str, Any], in_analysis: list[dict[str, Any]]) -> str:
    if not producer["running"]:
        return producer["message"]
    if not in_analysis:
        return (
            "The producer is running, but no paper has moved a paid call inside "
            "the window. It replays stored receipts or waits for a budget slot."
        )
    count = len(in_analysis)
    noun = "paper" if count == 1 else "papers"
    return f"The producer analyzes {count} {noun} at this moment."
