"""The migration of a one-file paid-call ledger into the parallel store.

Read "Parallel bookkeeping" in ``docs/SHARED_MODEL_BROKER.md`` first.

The migration is a conversion of the form of the record, never of the record.
It runs with every writer stopped at a settled boundary, it keeps the file it
started from as a frozen archive, and it proves that the state the store
materializes is the state the archive holds, field by field and then byte for
byte.
"""

from __future__ import annotations

import fcntl
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import ledger_store
from .util import atomic_write, canonical_json, sha256_bytes, sha256_file

ARCHIVE_SCHEMA = "shared-paid-call-ledger-pre-store-archive-v1"
REPORT_SCHEMA = "shared-paid-call-ledger-store-migration-v1"

# Every top-level field of the ledger is compared. These are named one by one
# in the report as well, because they are the ones that are money.
MONEY_FIELDS = (
    "spent_usd",
    "reserved_usd",
    "ambiguous_reserved_usd",
    "prior_construction_spend_usd",
    "generation_submissions",
    "count_requests",
    "inflight",
    "accepted_question_count",
    "halted",
    "halt_reason",
    "evaluation_halted",
    "evaluation_halt_reason",
    "policy_sha256",
    "price_config_sha256",
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def archive_path(ledger_file: Path, stamp: str) -> Path:
    return ledger_file.with_name(
        f"{ledger_file.stem}.pre-store-archive-{stamp}{ledger_file.suffix}"
    )


def _row_receipts(ledger: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The binding of each request row to its immutable evidence."""
    rows = {}
    for key, row in (ledger.get("requests") or {}).items():
        rows[key] = {
            name: row.get(name)
            for name in (
                "state",
                "request_sha256",
                "gate_sha256",
                "policy_sha256",
                "price_config_sha256",
                "config_transition_sha256",
                "reserved_usd",
                "actual_cost_usd",
                "phase",
                "run_id",
                "stage",
                "paper_id",
                "family_id",
                "source_version_id",
            )
        }
    return rows


def _compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Prove the two states are the same state, and say how."""
    differences: list[str] = []
    for name in sorted(set(before) | set(after)):
        if canonical_json(before.get(name)) != canonical_json(after.get(name)):
            differences.append(name)
    rows_before = _row_receipts(before)
    rows_after = _row_receipts(after)
    row_differences = sorted(
        key
        for key in set(rows_before) | set(rows_after)
        if canonical_json(rows_before.get(key)) != canonical_json(rows_after.get(key))
    )
    return {
        "identical": not differences and not row_differences,
        "fields_compared": sorted(set(before) | set(after)),
        "fields_that_differ": differences,
        "request_rows_compared": len(set(rows_before) | set(rows_after)),
        "request_rows_that_differ": row_differences[:20],
        "money_fields": {name: after.get(name) for name in MONEY_FIELDS},
        "canonical_sha256_before": sha256_bytes(canonical_json(before).encode()),
        "canonical_sha256_after": sha256_bytes(canonical_json(after).encode()),
    }


def check(ledger_file: Path, archive: Path) -> dict[str, Any]:
    """Prove a store against the archive of the file it was made from."""
    ledger_file = Path(ledger_file)
    archive = Path(archive)
    before = ledger_store._read_json(archive)
    after = ledger_store.read_ledger(ledger_file)
    comparison = _compare(before, after)
    return {
        "schema": REPORT_SCHEMA,
        "action": "check",
        "ledger_file": str(ledger_file),
        "archive_file": str(archive),
        "archive_sha256": sha256_file(archive),
        "journal_file": str(ledger_store.journal_file(ledger_file)),
        "journal_applied_seq": ledger_store.snapshot_applied_seq(ledger_file),
        "checked_at_utc": _now(),
        **comparison,
    }


def migrate(ledger_file: Path, *, apply: bool = False) -> dict[str, Any]:
    """Convert a one-file ledger into the store, once, with every writer stopped."""
    ledger_file = Path(ledger_file)
    if not ledger_file.is_file():
        raise ValueError("the shared paid-call ledger is absent")
    journal = ledger_store.journal_file(ledger_file)
    already = journal.is_file()
    lock_path = ledger_file.with_name(f".{ledger_file.name}.lock")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = ledger_file.read_bytes()
        before = ledger_store._read_json(ledger_file)
        if already:
            # The store exists. Say so and prove nothing was lost, without a
            # second archive and without a write.
            existing = sorted(
                path
                for path in ledger_file.parent.glob(
                    f"{ledger_file.stem}.pre-store-archive-*{ledger_file.suffix}"
                )
            )
            if not existing:
                raise ValueError("the ledger store has no pre-store archive")
            report = check(ledger_file, existing[-1])
            report["action"] = "already-migrated"
            return report
        target = archive_path(ledger_file, stamp)
        report = {
            "schema": REPORT_SCHEMA,
            "action": "migrate" if apply else "dry-run",
            "ledger_file": str(ledger_file),
            "archive_file": str(target),
            "archive_sha256": sha256_bytes(raw),
            "journal_file": str(journal),
            "request_rows": len(before.get("requests") or {}),
            "migrated_at_utc": _now(),
        }
        if not apply:
            report.update(_compare(before, ledger_store.read_ledger(ledger_file)))
            return report
        atomic_write(target, raw, immutable=True)
        # The archive is the record of the file the store replaced,
        # so it is frozen exactly as a receipt is.
        target.chmod(0o444)
        ledger_store.initialize_store(ledger_file)
        after = ledger_store.read_ledger(ledger_file)
        report.update(_compare(before, after))
        if not report["identical"]:
            raise ValueError("the migrated ledger store is not the ledger it replaced")
        report["journal_applied_seq"] = ledger_store.snapshot_applied_seq(ledger_file)
        return report
