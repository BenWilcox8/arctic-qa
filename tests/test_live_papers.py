from __future__ import annotations

import fcntl
import json
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import pytest

from arctic_qa import live_papers
from arctic_qa.corpus_viewer import CorpusArtifacts, CorpusServer
from arctic_qa.db import Database
from arctic_qa.live_papers import (
    ACTIVE_WINDOW_SECONDS,
    LIVE_PAPERS_SCHEMA,
    live_papers_report,
    read_shared_ledger,
    read_state_facts,
)
from arctic_qa.pipeline_trace import PipelineTraceStore

from test_corpus_viewer import fixture_corpus


VIEWER_PAGE = Path("src/arctic_qa/corpus_viewer.html")

# One instant owns this whole file. The producer records are fixtures, so the
# report is asked for its answer at a named minute, never at the wall clock.
NOW = datetime(2026, 9, 16, 21, 30, 0, tzinfo=UTC)
RUN_ID = "chapter3-fixture-r1"
CAMPAIGN_ID = "arctic-qa-production-campaign-003"

FLIGHT_FAMILY = "family-inflight"
FINISHED_FAMILY = "family-finished"
LABELLED_FAMILY = "family-labelled"


def stamp(minutes_before: float) -> str:
    moment = NOW - timedelta(minutes=minutes_before)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def request_row(
    *,
    family_id: str,
    paper_id: str,
    stage: str,
    submitted: float,
    completed: float | None,
    cost: str = "0.010000",
    state: str = "completed",
    run_id: str = RUN_ID,
    phase: str = "away_production",
    reserved: str = "0.020000",
) -> dict[str, object]:
    row: dict[str, object] = {
        "family_id": family_id,
        "paper_id": paper_id,
        "stage": stage,
        "state": state,
        "model": "gemini-3.1-pro-preview",
        "phase": phase,
        "run_id": run_id,
        "submitted_at_utc": stamp(submitted),
        "reserved_usd": reserved,
    }
    if completed is not None:
        row["completed_at_utc"] = stamp(completed)
        row["actual_cost_usd"] = cost
    return row


def fixture_ledger() -> dict[str, object]:
    """One ledger with a paper in flight, a finished paper and a labelled one."""
    requests = {
        # The paper in flight: an eligibility call an hour ago is the free
        # replay of a relaunched producer, and the burst of work is the three
        # recent calls.
        "0" * 64: request_row(
            family_id=FLIGHT_FAMILY,
            paper_id="10.1234/in-flight",
            stage="eligibility",
            submitted=61,
            completed=60,
        ),
        "1" * 64: request_row(
            family_id=FLIGHT_FAMILY,
            paper_id="10.1234/in-flight",
            stage="question_generation",
            submitted=4,
            completed=3.5,
            cost="0.030000",
        ),
        "2" * 64: request_row(
            family_id=FLIGHT_FAMILY,
            paper_id="10.1234/in-flight",
            stage="option_verification",
            submitted=1,
            completed=None,
            state="submitted",
        ),
        # The finished paper: the producer wrote its final state into the
        # progress record and no label exists for it.
        "3" * 64: request_row(
            family_id=FINISHED_FAMILY,
            paper_id="10.1234/finished",
            stage="eligibility",
            submitted=12,
            completed=11,
            cost="0.005000",
        ),
        # The labelled paper: the completion label owns its outcome and time.
        "4" * 64: request_row(
            family_id=LABELLED_FAMILY,
            paper_id="10.1234/labelled",
            stage="answer_verification",
            submitted=30,
            completed=29,
            cost="0.070000",
        ),
        # The evaluator shares this ledger. Its rows carry another run id and
        # the evaluation phase, and they must never reach this section.
        "5" * 64: request_row(
            family_id="evaluation-item:aqa-fixture",
            paper_id="evaluation-item:aqa-fixture",
            stage="evaluation_answer:gemini-3.8-flash",
            submitted=1,
            completed=0.5,
            run_id="abstention-stream-r1",
            phase="benchmark_evaluation",
        ),
    }
    return {
        "schema": "shared-paid-call-ledger-v1",
        "requests": requests,
        "spent_usd": "1.000000",
    }


def fixture_progress(state: str = "running") -> dict[str, object]:
    return {
        "schema": "streaming-dataset-progress-v1",
        "state": state,
        "run_id": CAMPAIGN_ID,
        "invocation_run_id": RUN_ID,
        "current_stage": "option_verification",
        "updated_at_utc": stamp(0.5),
        "message": "Processing 10.1234/in-flight.",
        "counts": {"accepted_qa": 1},
        "recent_papers": [
            {
                "paper_id": "src-labelled",
                "title": "The labelled Arctic paper",
                "current_stage": "completed",
                "final_state": "accepted",
                "final_reason": "machine_accepted_unverified",
            },
            {
                "paper_id": "10.1234/finished",
                "title": "The finished Arctic paper",
                "current_stage": "completed",
                "final_state": "generation_rejected",
                "final_reason": "eligible_arctic_scope_finding_unbound",
            },
        ],
    }


def fixture_state_db(path: Path, *, with_label: bool = True) -> Path:
    database = Database(path)
    database.migrate(path.parent / "backups")
    database.upsert_source(
        {
            "source_id": "src-labelled",
            "stable_id": "10.1234/labelled",
            "doi": "10.1234/labelled",
            "title": "The labelled Arctic paper",
            "authors": [],
            "paper_family_id": LABELLED_FAMILY,
            "provenance": {"adapter": "fixture"},
        }
    )
    database.upsert_source(
        {
            "source_id": "src-finished",
            "stable_id": "10.1234/finished",
            "doi": "10.1234/finished",
            "title": "The finished Arctic paper",
            "authors": [],
            "paper_family_id": FINISHED_FAMILY,
            "provenance": {"adapter": "fixture"},
        }
    )
    with database.transaction():
        for index in range(2):
            database.connection.execute(
                """INSERT INTO candidates (item_id,run_id,source_id,paper_family_id,
                generation_arm,candidate_json,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    f"item-labelled-{index}",
                    CAMPAIGN_ID,
                    "src-labelled",
                    LABELLED_FAMILY,
                    "answer_first",
                    "{}",
                    "machine_accepted_unverified",
                    stamp(30),
                    stamp(29),
                ),
            )
        # One call record of the same family. It is not a benchmark item, so it
        # must not raise the accepted question count.
        database.connection.execute(
            """INSERT INTO candidates (item_id,run_id,source_id,paper_family_id,
            generation_arm,candidate_json,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "item-labelled-call",
                CAMPAIGN_ID,
                "src-labelled",
                LABELLED_FAMILY,
                "answer_first",
                "{}",
                "generation_settled",
                stamp(30),
                stamp(29),
            ),
        )
    _ensure_completion_table(database.connection)
    if with_label:
        with database.transaction():
            database.connection.execute(
                """INSERT INTO paper_completions (completion_id,schema,run_id,
                campaign_id,candidate_key,paper_family_id,source_id,outcome_class,
                eligibility_decision,reason_code,detail_json,labelled_at_utc,
                labelled_by_commit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "paper-completion-fixture",
                    "streaming-paper-completion-v1",
                    RUN_ID,
                    CAMPAIGN_ID,
                    "10.1234/labelled",
                    LABELLED_FAMILY,
                    "src-labelled",
                    "generation_accepted",
                    "eligible",
                    "machine_accepted_unverified",
                    "{}",
                    stamp(28),
                    "fixture",
                ),
            )
    database.close()
    return path


def _ensure_completion_table(connection: sqlite3.Connection) -> None:
    """Create the completion table when this checkout has no column for it.

    The paper-completion task owns this table. The section must read the labels
    the moment that task lands, and must stay correct on a database that has no
    such table yet, so the fixture covers both shapes.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS paper_completions (
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
            UNIQUE(run_id, candidate_key))"""
    )
    connection.commit()


@pytest.fixture()
def facts(tmp_path: Path) -> dict[str, object]:
    return read_state_facts(
        fixture_state_db(tmp_path / "state.sqlite3"),
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
    )


def report(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "ledger": fixture_ledger(),
        "progress": fixture_progress(),
        "now": NOW,
    }
    arguments.update(overrides)
    return live_papers_report(**arguments)  # type: ignore[arg-type]


def test_the_paper_in_flight_is_the_only_paper_in_analysis(
    facts: dict[str, object],
) -> None:
    payload = report(facts=facts)
    assert payload["schema"] == LIVE_PAPERS_SCHEMA
    assert payload["availability"] == "available"
    assert payload["producer"]["running"] is True
    rows = payload["in_analysis"]
    assert [row["family_id"] for row in rows] == [FLIGHT_FAMILY]
    row = rows[0]
    assert row["paper_id"] == "10.1234/in-flight"
    assert row["stage"] == "option_verification"
    assert row["calls"] == 3
    assert row["open_calls"] == 1
    # Two settled calls at USD 0.01 and USD 0.03. The open call reserves money
    # but has spent none of it.
    assert row["cost_usd"] == "0.040000"
    assert row["reserved_usd"] == "0.020000"
    # The producer names no thread and no slot in any record it writes.
    assert row["slot"] is None


def test_the_time_in_analysis_ignores_the_free_replay_of_an_older_visit(
    facts: dict[str, object],
) -> None:
    row = report(facts=facts)["in_analysis"][0]
    # The burst starts at the question-generation call four minutes ago, not at
    # the replayed eligibility call of an hour ago.
    assert row["started_at_utc"] == stamp(4)
    assert row["in_analysis_seconds"] == pytest.approx(240, abs=1)


def test_the_finished_papers_carry_the_outcome_the_cost_and_the_count(
    facts: dict[str, object],
) -> None:
    rows = report(facts=facts)["finished"]
    assert [row["family_id"] for row in rows] == [FINISHED_FAMILY, LABELLED_FAMILY]
    finished, labelled = rows
    assert finished["outcome_class"] == "generation_rejected"
    assert finished["outcome"] == "rejected"
    assert finished["reason_code"] == "eligible_arctic_scope_finding_unbound"
    assert finished["cost_usd"] == "0.005000"
    assert finished["labelled"] is False
    # The label owns the outcome, the reason and the minute the paper finished.
    assert labelled["outcome_class"] == "generation_accepted"
    assert labelled["outcome"] == "accepted"
    assert labelled["question_count"] == 2
    assert labelled["cost_usd"] == "0.070000"
    # The last paid call of the paper, not the minute the label was written.
    assert labelled["finished_at_utc"] == stamp(29)
    assert labelled["labelled"] is True


def test_a_replayed_paper_takes_the_time_of_its_label(
    facts: dict[str, object],
) -> None:
    """A paper this run never called keeps the label as its only timestamp."""
    ledger = fixture_ledger()
    ledger["requests"].pop("4" * 64)  # type: ignore[union-attr]
    rows = report(ledger=ledger, facts=facts)["finished"]
    labelled = next(row for row in rows if row["family_id"] == LABELLED_FAMILY)
    assert labelled["calls"] == 0
    assert labelled["cost_usd"] == "0.000000"
    assert labelled["finished_at_utc"] == stamp(28)


def test_one_batch_of_labels_never_collapses_the_order(
    facts: dict[str, object],
) -> None:
    """A batch catch-up labels every paper at one minute; the calls order them."""
    completions = dict(facts["completions"])  # type: ignore[arg-type]
    for family in (FINISHED_FAMILY, LABELLED_FAMILY):
        completions[family] = {
            "paper_family_id": family,
            "outcome_class": "generation_rejected",
            "reason_code": "batch",
            "labelled_at_utc": stamp(1),
        }
    rows = report(facts={**facts, "completions": completions})["finished"]
    assert [row["family_id"] for row in rows] == [FINISHED_FAMILY, LABELLED_FAMILY]
    assert [row["finished_at_utc"] for row in rows] == [stamp(11), stamp(29)]


def test_a_labelled_paper_never_appears_in_analysis(tmp_path: Path) -> None:
    """A label alone removes a paper from the table inside the active window."""
    ledger = fixture_ledger()
    ledger["requests"]["4" * 64] = request_row(  # type: ignore[index]
        family_id=LABELLED_FAMILY,
        paper_id="10.1234/labelled",
        stage="answer_verification",
        submitted=2,
        completed=1.5,
        cost="0.070000",
    )
    # The progress record keeps no final state for this paper, so the label is
    # the only record that says the paper finished.
    progress = fixture_progress()
    progress["recent_papers"] = progress["recent_papers"][1:]  # type: ignore[index]
    labelled = read_state_facts(
        fixture_state_db(tmp_path / "labelled.sqlite3"),
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
    )
    unlabelled = read_state_facts(
        fixture_state_db(tmp_path / "unlabelled.sqlite3", with_label=False),
        campaign_id=CAMPAIGN_ID,
        run_id=RUN_ID,
    )
    with_label = report(ledger=ledger, progress=progress, facts=labelled)
    without_label = report(ledger=ledger, progress=progress, facts=unlabelled)
    assert [row["family_id"] for row in with_label["in_analysis"]] == [FLIGHT_FAMILY]
    assert with_label["completion_labels_present"] is True
    assert sorted(row["family_id"] for row in without_label["in_analysis"]) == [
        FLIGHT_FAMILY,
        LABELLED_FAMILY,
    ]


def test_a_state_database_without_the_label_table_still_reads(
    tmp_path: Path,
) -> None:
    path = fixture_state_db(tmp_path / "state.sqlite3", with_label=False)
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE paper_completions")
    connection.commit()
    connection.close()
    facts = read_state_facts(path, campaign_id=CAMPAIGN_ID, run_id=RUN_ID)
    assert facts["completion_labels_present"] is False
    payload = report(facts=facts)
    assert payload["completion_labels_present"] is False
    assert [row["family_id"] for row in payload["in_analysis"]] == [FLIGHT_FAMILY]


def test_the_evaluation_rows_of_the_shared_ledger_are_never_shown(
    facts: dict[str, object],
) -> None:
    payload = report(facts=facts)
    families = {row["family_id"] for row in payload["in_analysis"]}
    families |= {row["family_id"] for row in payload["finished"]}
    assert not any(family.startswith("evaluation-item:") for family in families)


def test_a_stopped_producer_is_named_instead_of_an_empty_table(
    facts: dict[str, object],
) -> None:
    progress = fixture_progress(state="error")
    progress["message"] = "Streaming stopped on ProviderError."
    payload = report(progress=progress, facts=facts)
    assert payload["producer"]["running"] is False
    assert payload["producer"]["state"] == "error"
    assert payload["message"] == "Streaming stopped on ProviderError."


def test_a_stale_running_record_is_not_a_running_producer(
    facts: dict[str, object],
) -> None:
    progress = fixture_progress()
    progress["updated_at_utc"] = stamp(20)
    payload = report(progress=progress, facts=facts, process_stale_after_seconds=300)
    assert payload["producer"]["running"] is False
    assert payload["producer"]["stale"] is True
    assert "No producer is running" in payload["message"]


def test_a_running_producer_with_no_recent_call_says_so(
    facts: dict[str, object],
) -> None:
    ledger = fixture_ledger()
    ledger["requests"].pop("1" * 64)  # type: ignore[union-attr]
    ledger["requests"].pop("2" * 64)  # type: ignore[union-attr]
    payload = report(ledger=ledger, facts=facts)
    assert payload["in_analysis"] == []
    assert "replays stored receipts" in payload["message"]


def test_the_window_bounds_the_table(facts: dict[str, object]) -> None:
    payload = report(facts=facts, window_seconds=60)
    assert payload["window_seconds"] == 60
    # Only the open call one minute ago is inside a 60-second window.
    assert [row["started_at_utc"] for row in payload["in_analysis"]] == [stamp(1)]


def test_the_finished_table_keeps_the_newest_rows_only(
    facts: dict[str, object],
) -> None:
    ledger = fixture_ledger()
    progress = fixture_progress()
    for index in range(12):
        family = f"family-old-{index:02d}"
        ledger["requests"][f"{index:064d}"] = request_row(  # type: ignore[index]
            family_id=family,
            paper_id=f"10.1234/old-{index:02d}",
            stage="eligibility",
            submitted=120 + index,
            completed=119 + index,
        )
        progress["recent_papers"].insert(  # type: ignore[union-attr]
            0,
            {
                "paper_id": f"10.1234/old-{index:02d}",
                "title": f"Older paper {index:02d}",
                "current_stage": "completed",
                "final_state": "rejected",
                "final_reason": "criterion_failed:study_geography",
            },
        )
    payload = report(ledger=ledger, progress=progress, facts=facts, finished_limit=10)
    rows = payload["finished"]
    assert len(rows) == 10
    stamps = [row["finished_at_utc"] for row in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_a_report_without_a_ledger_is_explicit() -> None:
    payload = live_papers_report(ledger=None, progress=None, now=NOW)
    assert payload["availability"] == "not_selected"
    assert payload["in_analysis"] == []
    assert payload["finished"] == []


def test_the_ledger_reader_takes_the_shared_lock_and_rejects_another_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared-paid-call-ledger.json"
    path.write_text(json.dumps(fixture_ledger()), encoding="utf-8")
    lock = path.with_name(f".{path.name}.lock")
    lock.write_bytes(b"")
    ledger = read_shared_ledger(path)
    assert set(ledger["requests"]) == set(fixture_ledger()["requests"])  # type: ignore[index]
    # The reader never writes: the lock file and the ledger keep their bytes.
    assert lock.read_bytes() == b""
    path.write_text(json.dumps({"schema": "other-v1"}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema is invalid"):
        read_shared_ledger(path)


def test_the_reader_gives_up_the_lock_rather_than_delay_a_paid_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that holds the lock never blocks the page past the bound."""
    path = tmp_path / "shared-paid-call-ledger.json"
    path.write_text(json.dumps(fixture_ledger()), encoding="utf-8")
    lock = path.with_name(f".{path.name}.lock")
    lock.write_bytes(b"")
    monkeypatch.setattr(live_papers, "_LOCK_WAIT_SECONDS", 0.2)
    with lock.open("a+") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX)
        started = time.monotonic()
        ledger = read_shared_ledger(path)
        waited = time.monotonic() - started
    assert set(ledger["requests"]) == set(fixture_ledger()["requests"])  # type: ignore[index]
    assert waited < 2


def test_http_route_serves_the_section_payload(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    fixture_corpus(corpus)
    namespace = tmp_path / "arctic-qa"
    namespace.mkdir()
    db_file = fixture_state_db(namespace / "state.sqlite3")
    ledger_file = namespace / "shared-paid-call-ledger.json"
    ledger_file.write_text(json.dumps(fixture_ledger()), encoding="utf-8")
    progress_file = namespace / "progress.json"
    progress_file.write_text(json.dumps(fixture_progress()), encoding="utf-8")
    store = PipelineTraceStore(
        namespace,
        db_file=db_file,
        receipts_dir=namespace / "model-receipts",
        ledger_file=ledger_file,
        eligibility_roots=(namespace / "eligibility",),
    )
    artifacts = CorpusArtifacts(
        corpus,
        "test-run",
        tmp_path / "runtime",
        shared_ledger_file=ledger_file,
        streaming_progress_file=progress_file,
        pipeline_trace_store=store,
    )
    server = CorpusServer(("127.0.0.1", 0), artifacts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/live-papers", timeout=10
        ) as response:
            assert response.status == HTTPStatus.OK
            payload = json.loads(response.read())
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=10) as response:
            page = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert payload["schema"] == LIVE_PAPERS_SCHEMA
    assert payload["producer"]["run_id"] == RUN_ID
    assert {row["family_id"] for row in payload["finished"]} == {
        FINISHED_FAMILY,
        LABELLED_FAMILY,
    }
    assert 'id="live-papers"' in page
    assert "Papers in analysis now" in page
    assert "/api/live-papers" in page


def test_the_page_keeps_every_existing_section_and_binds_the_new_one() -> None:
    page = VIEWER_PAGE.read_text(encoding="utf-8")
    for section in (
        "project-progress",
        "pipeline-method",
        "live-papers",
        "pipeline-inspector",
        "research-timeline",
        "live-dataset",
        "live-benchmark",
    ):
        assert f'id="{section}"' in page
    for element in (
        "live-papers-state",
        "live-papers-detail",
        "live-papers-meta",
        "live-papers-table",
        "live-papers-finished",
    ):
        assert f'id="{element}"' in page
    # The section stands above the per-paper inspector it introduces.
    assert page.index('id="live-papers"') < page.index('id="pipeline-inspector"')
    assert "loadLivePapers()" in page
    assert re.search(r"setInterval\(.*loadLivePapers\(\).*\}, 15000\)", page)


def test_the_active_window_is_longer_than_one_call_timeout() -> None:
    # The generation stages time out at 300 seconds, so a paper that waits on
    # one slow call must not leave the table before that call can end.
    assert ACTIVE_WINDOW_SECONDS > 300
