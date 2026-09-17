"""The completion date of a paper, and the repair of the lock-refused papers.

The chapter 3 website printed "Unknown" for the completion date of every paper
of the concurrent run. Two causes, one per half of this file.

The producer's progress row carried no time at all, and the pipeline trace reads
that row before anything else: a row with no time replaced the time the receipts
already answered. The producer now records when each paper reached its state,
the completion label carries the paper's own completion time, and the trace
falls back to that label.

The same run recorded 19 papers with ``BrokerOperationBusyError``, a refusal of
the exclusive operation lock that reserved nothing and charged nothing. The
repair command removes those records so the next start analyses the paper again.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from arctic_qa import concurrency_repair, paper_completion, streaming
from arctic_qa.db import Database
from arctic_qa.paths import DataPaths
from arctic_qa.pipeline_trace import PipelineTraceStore
from arctic_qa.providers import FakeProvider
from arctic_qa.streaming import run_stream
from arctic_qa.util import canonical_json

from test_streaming import FIXTURES, streaming_fixture


RUN_ID = "completion-date-fixture-r1"
CAMPAIGN_ID = "completion-date-fixture-campaign"
CODE_COMMIT = "def5678"


def _open_database(tmp_path: Path) -> tuple[DataPaths, Database]:
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    return paths, database


def _stream_arguments(tmp_path: Path, *, paper_workers: int = 1) -> dict[str, Any]:
    access, eligibility = streaming_fixture(tmp_path)
    paths, database = _open_database(tmp_path)
    return {
        "db": database,
        "namespace": paths.namespace,
        "run_id": RUN_ID,
        "campaign_id": CAMPAIGN_ID,
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "author": FakeProvider("fake-author", FIXTURES / "fake-author.jsonl"),
        "verifier": FakeProvider("fake-verifier", FIXTURES / "fake-verifier.jsonl"),
        "max_papers": 1,
        "code_commit": CODE_COMMIT,
        "paper_workers": paper_workers,
    }


# The completion date the producer records.


def test_the_producer_records_when_each_paper_reached_its_state(
    tmp_path: Path,
) -> None:
    arguments = _stream_arguments(tmp_path)

    run_stream(**arguments)

    progress = json.loads(
        (arguments["namespace"] / "streaming-dataset-r1" / "progress.json").read_text(
            encoding="utf-8"
        )
    )
    rows = progress["recent_papers"]
    assert rows
    for row in rows:
        assert row["state_changed_at_utc"], row
        assert row["state_changed_at_utc"].endswith("+00:00")


def test_the_concurrent_path_records_the_date_the_sequential_path_did(
    tmp_path: Path,
) -> None:
    sequential = _stream_arguments(tmp_path / "one", paper_workers=1)
    concurrent = _stream_arguments(tmp_path / "four", paper_workers=4)

    run_stream(**sequential)
    run_stream(**concurrent)

    def dates(arguments: dict[str, Any]) -> list[str]:
        labels = paper_completion.load_completions(arguments["db"], run_id=RUN_ID)
        return [str(row["completed_at_utc"] or "") for row in labels.values()]

    assert dates(sequential)
    assert dates(sequential) != [""]
    assert all(value for value in dates(concurrent))
    assert len(dates(concurrent)) == len(dates(sequential))


def test_the_label_carries_the_completion_time_and_not_only_the_label_time(
    tmp_path: Path,
) -> None:
    arguments = _stream_arguments(tmp_path)

    run_stream(**arguments)

    labels = paper_completion.load_completions(arguments["db"], run_id=RUN_ID)
    assert labels
    for row in labels.values():
        assert row["completed_at_utc"]
        assert row["labelled_at_utc"]
        assert row["completed_at_utc"] <= row["labelled_at_utc"]


def test_a_state_that_does_not_change_keeps_its_first_time(
    monkeypatch: Any,
) -> None:
    ticks = iter(f"2026-09-17T00:0{index}:00+00:00" for index in range(9))
    monkeypatch.setattr(streaming, "now", lambda: next(ticks))
    progress = streaming._Progress.__new__(streaming._Progress)
    progress.recent = []
    progress._lock = threading.RLock()
    progress.write = lambda *_, **__: None  # type: ignore[method-assign]

    progress.paper(
        paper_id="p1", title="t", current_stage="eligibility", final_state=None
    )
    first = progress.paper_completed_at("p1")
    progress.paper(
        paper_id="p1", title="t", current_stage="eligibility", final_state=None
    )
    assert progress.paper_completed_at("p1") == first

    progress.paper(
        paper_id="p1", title="t", current_stage="completed", final_state="accepted"
    )
    assert progress.paper_completed_at("p1") != first


def test_a_progress_row_without_a_time_no_longer_hides_the_known_time(
    tmp_path: Path,
) -> None:
    """The exact shape the website showed: a state change, and no time with it."""
    arguments = _stream_arguments(tmp_path)
    run_stream(**arguments)
    namespace = arguments["namespace"]
    progress_file = namespace / "streaming-dataset-r1" / "progress.json"
    progress = json.loads(progress_file.read_text(encoding="utf-8"))
    for row in progress["recent_papers"]:
        row.pop("state_changed_at_utc", None)
        row["final_state"] = "rejected"
    progress_file.write_text(canonical_json(progress), encoding="utf-8")

    store = PipelineTraceStore(namespace)
    items = store.list_papers(limit=25)["items"]

    assert items
    assert all(item["state_entered_at_utc"] for item in items), items


# The repair of the papers the lock refused.


def _busy_fault(
    database: Database, *, candidate_key: str, family_id: str, campaign_id: str
) -> None:
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": candidate_key,
        "contract_version": streaming.CANDIDATE_PROCESSING_FAULT_CONTRACT_VERSION,
        "error_class": "BrokerOperationBusyError",
        "error_message": "another paid broker operation is active",
        "family_id": family_id,
        "stage": "eligibility",
    }
    with database.transaction():
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,NULL,'generation_routing',?,?,'2026-09-16T23:55:41+00:00')""",
            (
                f"rejection-busy-{candidate_key}",
                streaming.CANDIDATE_PROCESSING_FAULT_REASON_CODE,
                canonical_json(detail),
            ),
        )


def _busy_call_record(
    database: Database, *, family_id: str, campaign_id: str, item_id: str
) -> None:
    record = {
        "state": "incomplete_infra",
        "reason_code": streaming.CANDIDATE_PROCESSING_FAULT_REASON_CODE,
        "candidate_processing_fault": {
            "error_class": "BrokerOperationBusyError",
            "error_message": "another paid broker operation is active",
            "stage": "generation",
        },
    }
    with database.transaction():
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES (?,?,?,?,'answer_first',?,'incomplete_infra',?,?)""",
            (
                item_id,
                campaign_id,
                f"src-{family_id}",
                family_id,
                canonical_json(record),
                "2026-09-16T23:55:41+00:00",
                "2026-09-16T23:55:41+00:00",
            ),
        )


def _other_fault(database: Database, *, campaign_id: str) -> None:
    detail = {
        "campaign_id": campaign_id,
        "candidate_key": "10.1/keeps-its-fault",
        "error_class": "ValueError",
        "error_message": "a fault of the paper itself",
        "family_id": "family-other",
        "stage": "generation",
    }
    with database.transaction():
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES ('rejection-other',NULL,NULL,'generation_routing',?,?,
                    '2026-09-16T23:55:41+00:00')""",
            (
                streaming.CANDIDATE_PROCESSING_FAULT_REASON_CODE,
                canonical_json(detail),
            ),
        )


def test_the_repair_clears_every_lock_refusal_and_nothing_else(
    tmp_path: Path,
) -> None:
    _, database = _open_database(tmp_path)
    _busy_fault(
        database,
        candidate_key="10.1/one",
        family_id="family-one",
        campaign_id=CAMPAIGN_ID,
    )
    _busy_call_record(
        database, family_id="family-one", campaign_id=CAMPAIGN_ID, item_id="call-one"
    )
    _other_fault(database, campaign_id=CAMPAIGN_ID)
    paper_completion.record_completion(
        database,
        paper_completion.completion_row(
            run_id=RUN_ID,
            campaign_id=CAMPAIGN_ID,
            candidate_key="10.1/one",
            family_id="family-one",
            source_id=None,
            outcome_class="eligibility_excluded",
            eligibility_decision="excluded",
            reason_code=None,
            code_commit=CODE_COMMIT,
        ),
    )

    dry = concurrency_repair.clear_busy_faults(
        database, run_id=RUN_ID, campaign_id=CAMPAIGN_ID
    )
    assert dry["papers"] == ["10.1/one"]
    assert dry["rejection_rows"] == 1
    assert dry["call_records"] == 1
    assert dry["completion_labels_removed"] == 1
    assert database.rows("SELECT rejection_id FROM rejection_ledger")

    applied = concurrency_repair.clear_busy_faults(
        database, run_id=RUN_ID, campaign_id=CAMPAIGN_ID, apply=True
    )

    assert applied["applied"] is True
    remaining = database.rows("SELECT rejection_id FROM rejection_ledger")
    assert [row["rejection_id"] for row in remaining] == ["rejection-other"]
    assert paper_completion.load_completions(database, run_id=RUN_ID) == {}
    record = json.loads(
        database.rows("SELECT candidate_json FROM candidates WHERE item_id='call-one'")[
            0
        ]["candidate_json"]
    )
    assert "candidate_processing_fault" not in record
    assert record["reason_code"] is None
    assert record["state"] == "incomplete_infra"


def test_the_repair_is_idempotent(tmp_path: Path) -> None:
    _, database = _open_database(tmp_path)
    _busy_fault(
        database,
        candidate_key="10.1/one",
        family_id="family-one",
        campaign_id=CAMPAIGN_ID,
    )

    concurrency_repair.clear_busy_faults(
        database, run_id=RUN_ID, campaign_id=CAMPAIGN_ID, apply=True
    )
    again = concurrency_repair.clear_busy_faults(
        database, run_id=RUN_ID, campaign_id=CAMPAIGN_ID, apply=True
    )

    assert again["paper_count"] == 0
    assert again["rejection_rows"] == 0


def test_the_backfill_fills_a_label_written_without_a_time(tmp_path: Path) -> None:
    paths, database = _open_database(tmp_path)
    row = paper_completion.completion_row(
        run_id=RUN_ID,
        campaign_id=CAMPAIGN_ID,
        candidate_key="10.1/two",
        family_id="family-two",
        source_id=None,
        outcome_class="eligibility_excluded",
        eligibility_decision="excluded",
        reason_code=None,
        code_commit=CODE_COMMIT,
    )
    paper_completion.record_completion(database, row)
    ledger = tmp_path / "shared-paid-call-ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "requests": {
                    "k1": {
                        "family_id": "family-two",
                        "completed_at_utc": "2026-09-17T00:12:00Z",
                    },
                    "k2": {
                        "family_id": "family-two",
                        "completed_at_utc": "2026-09-17T00:10:00Z",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    dry = concurrency_repair.backfill_completion_dates(
        database, run_id=RUN_ID, campaign_id=CAMPAIGN_ID, ledger_file=ledger
    )
    assert dry["filled"] == 1
    assert dry["papers"][0]["completed_at_utc"] == "2026-09-17T00:12:00Z"
    assert (
        paper_completion.load_completions(database, run_id=RUN_ID)["10.1/two"][
            "completed_at_utc"
        ]
        is None
    )

    concurrency_repair.backfill_completion_dates(
        database,
        run_id=RUN_ID,
        campaign_id=CAMPAIGN_ID,
        ledger_file=ledger,
        apply=True,
    )

    labels = paper_completion.load_completions(database, run_id=RUN_ID)
    assert labels["10.1/two"]["completed_at_utc"] == "2026-09-17T00:12:00Z"
    assert paths.database.is_file()


def test_the_label_table_takes_the_completion_column_without_a_version_bump(
    tmp_path: Path,
) -> None:
    """An existing database gains the nullable column and keeps its version."""
    paths = DataPaths.open(tmp_path, test_mode=True)
    connection = sqlite3.connect(paths.database)
    connection.executescript(
        """CREATE TABLE schema_info (version INTEGER NOT NULL);
        INSERT INTO schema_info(version) VALUES (5);
        CREATE TABLE paper_completions (
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
            UNIQUE(run_id, candidate_key));"""
    )
    connection.commit()
    connection.close()

    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")

    columns = {
        str(row[1])
        for row in database.connection.execute("PRAGMA table_info(paper_completions)")
    }
    assert "completed_at_utc" in columns
    version = database.connection.execute("SELECT version FROM schema_info").fetchone()
    assert version[0] == 5
