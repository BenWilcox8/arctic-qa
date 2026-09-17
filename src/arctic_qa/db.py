from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .util import canonical_json


SCHEMA_VERSION = 5


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    stable_id TEXT NOT NULL,
    doi TEXT,
    title TEXT NOT NULL,
    authors_json TEXT NOT NULL,
    published_date TEXT,
    source_version TEXT,
    retrieval_url TEXT,
    retrieved_at TEXT,
    content_hash TEXT,
    media_type TEXT,
    license TEXT,
    access_state TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    corrections_json TEXT NOT NULL,
    zotero_json TEXT NOT NULL,
    paper_family_id TEXT NOT NULL,
    geography_state TEXT NOT NULL,
    geography_confidence TEXT NOT NULL,
    inclusion_reason TEXT,
    duplicate_of TEXT,
    scope_rule_version TEXT,
    scope_evidence_json TEXT NOT NULL,
    eligibility_state TEXT NOT NULL,
    discipline TEXT,
    year INTEGER,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_doi ON sources(doi) WHERE doi IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sources_family ON sources(paper_family_id);
CREATE TABLE IF NOT EXISTS discovery_events (
    event_id TEXT PRIMARY KEY,
    adapter TEXT NOT NULL,
    query TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    page_token TEXT,
    page_number INTEGER NOT NULL,
    response_hash TEXT NOT NULL,
    replay_path TEXT,
    result_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    source_id TEXT,
    kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(source_id, kind, content_hash)
);
CREATE TABLE IF NOT EXISTS stage_receipts (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    output_json TEXT,
    error_code TEXT,
    error_text TEXT,
    UNIQUE(run_id, entity_id, stage, input_hash)
);
CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    returned_model TEXT,
    prompt_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    request_id TEXT,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reserved_cost_usd TEXT,
    actual_cost_usd TEXT,
    error_code TEXT,
    error_text TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    response_json TEXT
);
CREATE TABLE IF NOT EXISTS budgets (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    limit_value TEXT NOT NULL,
    reserved_value TEXT NOT NULL,
    spent_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    paper_family_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    selection_policy_version TEXT NOT NULL,
    answer_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, paper_family_id, selection_policy_version)
);
CREATE TABLE IF NOT EXISTS finding_bank (
    bank_row_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    paper_family_id TEXT NOT NULL,
    bank_key TEXT NOT NULL,
    extraction_entity_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    span_ids_json TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    admission_status TEXT NOT NULL,
    admission_reason_code TEXT,
    frozen_finding_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_finding_bank_family
    ON finding_bank(run_id, paper_family_id, bank_key, admission_status);
CREATE TABLE IF NOT EXISTS finding_prescreen_shadow (
    prescreen_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    paper_family_id TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    verdict_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, source_id, contract_version)
);
CREATE TABLE IF NOT EXISTS candidates (
    item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    paper_family_id TEXT NOT NULL,
    generation_arm TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_events (
    event_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    label TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejection_ledger (
    rejection_id TEXT PRIMARY KEY,
    item_id TEXT,
    source_id TEXT,
    stage TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_completions (
    completion_id TEXT PRIMARY KEY,
    schema TEXT NOT NULL,
    run_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    candidate_key TEXT NOT NULL,
    paper_family_id TEXT NOT NULL,
    source_id TEXT,
    outcome_class TEXT NOT NULL,
    eligibility_decision TEXT,
    reason_code TEXT,
    detail_json TEXT NOT NULL,
    labelled_at_utc TEXT NOT NULL,
    labelled_by_commit TEXT NOT NULL,
    UNIQUE(run_id, candidate_key)
);
CREATE INDEX IF NOT EXISTS idx_paper_completions_run
    ON paper_completions(run_id);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    """One sqlite connection, serialised across the threads that share it.

    The paper-concurrent producer runs several papers on one database. All of
    them use this one connection, so every statement and every transaction is
    taken under ``lock``: a commit on a shared connection is connection-wide,
    and two interleaved transactions would commit each other's half-written
    work. The lock is reentrant, so a method that reads and then writes under
    it stays one unit.

    ``lock`` is public: a caller that must read and then write atomically,
    such as a claim over a shared row, holds it across both.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def migrate(self, backup_dir: Path) -> None:
        existing = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_info'"
        ).fetchone()
        if existing:
            row = self.connection.execute("SELECT version FROM schema_info").fetchone()
            if row and row[0] == SCHEMA_VERSION:
                # The version is current, but a table added to SCHEMA under the
                # same version (the chapter 3 finding bank, the per-paper
                # completion label) is created here. Every statement in SCHEMA
                # is IF NOT EXISTS, so this is a no-op on a complete database.
                # A new table that no earlier reader queries keeps the version:
                # the live state database is shared with the benchmark
                # evaluator, whose pinned snapshot refuses any other version.
                with self.transaction():
                    self.connection.executescript(SCHEMA)
                return
            if self.path.stat().st_size:
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = (
                    backup_dir
                    / f"state-before-v{SCHEMA_VERSION}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
                )
                if not backup.exists():
                    shutil.copy2(self.path, backup)
            if row and row[0] == 1:
                columns = {
                    item[1]
                    for item in self.connection.execute(
                        "PRAGMA table_info(candidates)"
                    ).fetchall()
                }
                with self.transaction():
                    if "run_id" not in columns:
                        self.connection.execute(
                            "ALTER TABLE candidates ADD COLUMN run_id TEXT NOT NULL DEFAULT 'legacy-unknown'"
                        )
                    self.connection.execute(
                        """CREATE TABLE artifacts_v2 (
                        artifact_id TEXT PRIMARY KEY,
                        source_id TEXT,
                        kind TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        media_type TEXT,
                        created_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        UNIQUE(source_id, kind, content_hash)
                        )"""
                    )
                    self.connection.execute(
                        "INSERT INTO artifacts_v2 SELECT * FROM artifacts"
                    )
                    self.connection.execute("DROP TABLE artifacts")
                    self.connection.execute(
                        "ALTER TABLE artifacts_v2 RENAME TO artifacts"
                    )
                    self.connection.execute("UPDATE schema_info SET version=2")
            row = self.connection.execute("SELECT version FROM schema_info").fetchone()
            if row and row[0] == 2:
                with self.transaction():
                    self.connection.execute(
                        """CREATE TABLE IF NOT EXISTS findings (
                        finding_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        paper_family_id TEXT NOT NULL,
                        chunk_id TEXT NOT NULL,
                        selection_policy_version TEXT NOT NULL,
                        answer_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(run_id, source_id, selection_policy_version)
                        )"""
                    )
                    self.connection.execute("UPDATE schema_info SET version=4")
            row = self.connection.execute("SELECT version FROM schema_info").fetchone()
            if row and row[0] == 3:
                with self.transaction():
                    self.connection.execute(
                        "ALTER TABLE findings RENAME TO findings_v3"
                    )
                    self.connection.execute(
                        """CREATE TABLE findings (
                        finding_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        paper_family_id TEXT NOT NULL,
                        chunk_id TEXT NOT NULL,
                        selection_policy_version TEXT NOT NULL,
                        answer_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(run_id, source_id, selection_policy_version)
                        )"""
                    )
                    self.connection.execute(
                        """INSERT INTO findings
                        (finding_id,run_id,source_id,paper_family_id,chunk_id,
                         selection_policy_version,answer_json,status,created_at)
                        SELECT finding_id,'legacy-unknown',source_id,paper_family_id,
                               chunk_id,selection_policy_version,answer_json,status,created_at
                        FROM findings_v3"""
                    )
                    self.connection.execute("DROP TABLE findings_v3")
                    self.connection.execute("UPDATE schema_info SET version=4")
            row = self.connection.execute("SELECT version FROM schema_info").fetchone()
            if row and row[0] == 4:
                duplicate = self.connection.execute(
                    """SELECT run_id,paper_family_id,selection_policy_version,
                              COUNT(*) AS count
                    FROM findings
                    GROUP BY run_id,paper_family_id,selection_policy_version
                    HAVING COUNT(*) > 1
                    ORDER BY run_id,paper_family_id,selection_policy_version
                    LIMIT 1"""
                ).fetchone()
                if duplicate:
                    raise RuntimeError(
                        "cannot migrate findings with duplicate paper-family policy "
                        f"rows: run={duplicate[0]}, family={duplicate[1]}, "
                        f"policy={duplicate[2]}, count={duplicate[3]}"
                    )
                with self.transaction():
                    self.connection.execute(
                        "ALTER TABLE findings RENAME TO findings_v4"
                    )
                    self.connection.execute(
                        """CREATE TABLE findings (
                        finding_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        paper_family_id TEXT NOT NULL,
                        chunk_id TEXT NOT NULL,
                        selection_policy_version TEXT NOT NULL,
                        answer_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(run_id,paper_family_id,selection_policy_version)
                        )"""
                    )
                    self.connection.execute(
                        "INSERT INTO findings SELECT * FROM findings_v4"
                    )
                    self.connection.execute("DROP TABLE findings_v4")
                    self.connection.execute(
                        "UPDATE schema_info SET version=?", (SCHEMA_VERSION,)
                    )
        with self.transaction():
            self.connection.executescript(SCHEMA)
            row = self.connection.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row[0] != SCHEMA_VERSION:
                raise RuntimeError(f"unsupported database schema version: {row[0]}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            try:
                yield
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [
                dict(row) for row in self.connection.execute(sql, parameters).fetchall()
            ]

    def one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(sql, parameters).fetchone()
        return dict(row) if row else None

    def upsert_source(self, source: dict[str, Any]) -> bool:
        with self.lock:
            return self._upsert_source(source)

    def _upsert_source(self, source: dict[str, Any]) -> bool:
        existing = None
        if source.get("doi"):
            existing = self.one("SELECT * FROM sources WHERE doi = ?", (source["doi"],))
        if existing is None:
            existing = self.one(
                "SELECT * FROM sources WHERE source_id = ?", (source["source_id"],)
            )
        timestamp = now()
        if existing:
            provenance = json.loads(existing["provenance_json"])
            new_event = source.get("provenance", {})
            if new_event not in provenance:
                provenance.append(new_event)
            with self.transaction():
                self.connection.execute(
                    """UPDATE sources SET provenance_json=?,
                    discipline=COALESCE(discipline,?),year=COALESCE(year,?),updated_at=?
                    WHERE source_id=?""",
                    (
                        canonical_json(provenance),
                        source.get("discipline"),
                        source.get("year"),
                        timestamp,
                        existing["source_id"],
                    ),
                )
            return False
        values = {
            "source_version": None,
            "retrieval_url": None,
            "retrieved_at": None,
            "content_hash": None,
            "media_type": None,
            "license": None,
            "access_state": "metadata_only",
            "corrections": [],
            "zotero": {},
            "geography_state": "unresolved",
            "geography_confidence": "unresolved",
            "inclusion_reason": None,
            "duplicate_of": None,
            "scope_rule_version": None,
            "scope_evidence": {},
            "eligibility_state": "pending",
            "discipline": None,
            "year": None,
            "metadata": {},
            **source,
        }
        with self.transaction():
            self.connection.execute(
                """INSERT INTO sources (
                    source_id, stable_id, doi, title, authors_json, published_date,
                    source_version, retrieval_url, retrieved_at, content_hash, media_type,
                    license, access_state, provenance_json, corrections_json, zotero_json,
                    paper_family_id, geography_state, geography_confidence, inclusion_reason,
                    duplicate_of, scope_rule_version, scope_evidence_json, eligibility_state,
                    discipline, year, metadata_json, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    values["source_id"],
                    values["stable_id"],
                    values.get("doi"),
                    values["title"],
                    canonical_json(values.get("authors", [])),
                    values.get("published_date"),
                    values.get("source_version"),
                    values.get("retrieval_url"),
                    values.get("retrieved_at"),
                    values.get("content_hash"),
                    values.get("media_type"),
                    values.get("license"),
                    values["access_state"],
                    canonical_json([values.get("provenance", {})]),
                    canonical_json(values["corrections"]),
                    canonical_json(values["zotero"]),
                    values["paper_family_id"],
                    values["geography_state"],
                    values["geography_confidence"],
                    values["inclusion_reason"],
                    values["duplicate_of"],
                    values["scope_rule_version"],
                    canonical_json(values["scope_evidence"]),
                    values["eligibility_state"],
                    values["discipline"],
                    values["year"],
                    canonical_json(values["metadata"]),
                    timestamp,
                    timestamp,
                ),
            )
        return True

    def begin_stage(self, receipt: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
        with self.lock:
            return self._begin_stage(receipt)

    def _begin_stage(
        self, receipt: dict[str, str]
    ) -> tuple[str, dict[str, Any] | None]:
        existing = self.one(
            "SELECT * FROM stage_receipts WHERE run_id=? AND entity_id=? AND stage=? AND input_hash=?",
            (
                receipt["run_id"],
                receipt["entity_id"],
                receipt["stage"],
                receipt["input_hash"],
            ),
        )
        if existing and existing["status"] == "completed":
            return "completed", existing
        if existing and existing["status"] in ("started", "ambiguous_charge"):
            return "interrupted", existing
        with self.transaction():
            self.connection.execute(
                """INSERT OR REPLACE INTO stage_receipts
                (receipt_id,run_id,entity_id,stage,input_hash,status,started_at)
                VALUES (?,?,?,?,?,'started',?)""",
                (
                    receipt["receipt_id"],
                    receipt["run_id"],
                    receipt["entity_id"],
                    receipt["stage"],
                    receipt["input_hash"],
                    now(),
                ),
            )
        return "started", None

    def finish_stage(self, receipt_id: str, output: Any) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE stage_receipts SET status='completed',completed_at=?,output_json=? WHERE receipt_id=?",
                (now(), canonical_json(output), receipt_id),
            )

    def fail_stage(self, receipt_id: str, status: str, code: str, text: str) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE stage_receipts SET status=?,completed_at=?,error_code=?,error_text=? WHERE receipt_id=?",
                (status, now(), code, text, receipt_id),
            )
