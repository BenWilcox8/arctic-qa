from __future__ import annotations

from pathlib import Path

from arctic_qa.db import Database, now
from arctic_qa.streaming import _validation_event_for_stored_candidate
from arctic_qa.util import canonical_json, stable_id


def _candidate(item_id: str, revision: str) -> dict[str, str]:
    return {"item_id": item_id, "revision": revision}


def _insert_candidate(
    db: Database, candidate: dict[str, str], *, status: str = "rejected"
) -> None:
    with db.transaction():
        db.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                candidate["item_id"],
                "test-run",
                "test-source",
                "test-family",
                "answer_first",
                canonical_json(candidate),
                status,
                now(),
                now(),
            ),
        )


def _insert_validation(
    db: Database,
    candidate: dict[str, str],
    *,
    event_id: str,
    label: str,
    include_hash: bool = True,
) -> None:
    details = {"labels": {}, "distractors": []}
    if include_hash:
        details["candidate_hash"] = stable_id(
            "candidate-payload", canonical_json(candidate)
        )
    with db.transaction():
        db.connection.execute(
            """INSERT INTO validation_events
            (event_id,item_id,stage,label,reason_codes_json,details_json,created_at)
            VALUES (?,?,'automated_acceptance',?,?,?,?)""",
            (
                event_id,
                candidate["item_id"],
                label,
                canonical_json([f"{label}_reason"]),
                canonical_json(details),
                now(),
            ),
        )


def _database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite3")
    db.migrate(tmp_path / "backups")
    return db


def test_selects_validation_bound_to_current_stored_payload(tmp_path: Path) -> None:
    db = _database(tmp_path)
    current = _candidate("item-current", "current")
    stale = _candidate("item-current", "stale")
    _insert_candidate(db, current)
    _insert_validation(
        db,
        current,
        event_id="event-current",
        label="machine_accepted_unverified",
    )
    _insert_validation(db, stale, event_id="event-stale", label="rejected")

    event = _validation_event_for_stored_candidate(
        db, db.one("SELECT * FROM candidates WHERE item_id=?", ("item-current",))
    )

    assert event == {
        "label": "machine_accepted_unverified",
        "reason_codes_json": '["machine_accepted_unverified_reason"]',
    }


def test_does_not_apply_an_old_rejection_to_a_newer_payload(tmp_path: Path) -> None:
    db = _database(tmp_path)
    current = _candidate("item-newer", "current")
    stale = _candidate("item-newer", "stale")
    _insert_candidate(db, current)
    _insert_validation(db, stale, event_id="event-old-rejection", label="rejected")

    event = _validation_event_for_stored_candidate(
        db, db.one("SELECT * FROM candidates WHERE item_id=?", ("item-newer",))
    )

    assert event is None


def test_keeps_immutable_legacy_validation_events_without_a_hash(tmp_path: Path) -> None:
    db = _database(tmp_path)
    current = _candidate("item-legacy", "current")
    _insert_candidate(db, current)
    _insert_validation(
        db,
        current,
        event_id="event-legacy",
        label="rejected",
        include_hash=False,
    )
    _insert_validation(
        db,
        current,
        event_id="event-current",
        label="machine_accepted_unverified",
    )

    event = _validation_event_for_stored_candidate(
        db, db.one("SELECT * FROM candidates WHERE item_id=?", ("item-legacy",))
    )

    assert event["label"] == "machine_accepted_unverified"
    assert db.one(
        "SELECT details_json FROM validation_events WHERE event_id=?", ("event-legacy",)
    ) == {"details_json": '{"distractors":[],"labels":{}}'}
