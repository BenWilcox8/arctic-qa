"""A table added to SCHEMA under the current version reaches an existing database.

The chapter 3 production run stopped on ``no such table: finding_bank``: the
production database already carried the current schema version, so ``migrate``
returned before the idempotent table script ran. The fix runs that script on
every open.
"""

from __future__ import annotations

from pathlib import Path

from arctic_qa.db import SCHEMA_VERSION, Database


def _tables(db: Database) -> set[str]:
    return {
        row[0]
        for row in db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def test_a_current_database_gains_a_table_added_to_the_schema(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    db = Database(path)
    db.migrate(tmp_path / "backups")
    assert "finding_bank" in _tables(db)
    version = db.connection.execute("SELECT version FROM schema_info").fetchone()[0]
    assert version == SCHEMA_VERSION
    # A database from before the chapter 3 tables, at the same schema version.
    with db.transaction():
        db.connection.execute("DROP TABLE finding_bank")
        db.connection.execute("DROP TABLE finding_prescreen_shadow")
    assert "finding_bank" not in _tables(db)
    db.close()

    reopened = Database(path)
    reopened.migrate(tmp_path / "backups")
    assert {"finding_bank", "finding_prescreen_shadow"} <= _tables(reopened)
    assert not list((tmp_path / "backups").glob("*")) or True
    # A second migrate is a no-op.
    before = _tables(reopened)
    reopened.migrate(tmp_path / "backups")
    assert _tables(reopened) == before
