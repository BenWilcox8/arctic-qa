"""The parallel bookkeeping store of the shared paid-call ledger.

Read "Parallel bookkeeping" in ``docs/SHARED_MODEL_BROKER.md`` first.

The ledger used to be one JSON file that every operation read whole, proved
whole and wrote whole. At 6,036 rows that cost about 3 seconds of serialised
work per paid call, and the number of calls in flight settles at the call
length over the admission length: 8 seconds over 3 is three calls, which is
what the chapter 3 run showed. These tests hold the store to the two things
that had to change and to everything that must not: the cost of one call is
its own, and the money is exactly the money.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from arctic_qa import ledger_migration, ledger_store  # noqa: E402
from arctic_qa.util import canonical_json  # noqa: E402

from test_model_broker import Transport, execute, fixture, write_json  # noqa: E402


def _parallel_policy(tmp_path: Path) -> Path:
    """The shipped policy at the registered sixteen-at-once request rate."""
    source = ROOT / "config" / "streaming-dataset-budget-policy-v1.json"
    policy = json.loads(source.read_text(encoding="utf-8"))
    policy["maximum_concurrent_generation_requests"] = 16
    policy["maximum_generation_requests_per_minute"] = 100
    path = tmp_path / "parallel-policy.json"
    write_json(path, policy)
    return path


def _concurrent(tmp_path: Path, transport=None, *, parallel: bool = False):
    """A broker in the shape the chapter 3 producer runs in."""
    values = fixture(
        tmp_path,
        transport=transport or Transport(),
        policy_file=_parallel_policy(tmp_path) if parallel else None,
    )
    values["broker"].concurrent_construction = True
    values["broker"].deferred_snapshot = True
    return values


# Four stages of the shipped price configuration, so one paper can make four
# distinct paid requests.
_STAGES = (
    "eligibility",
    "finding_answer_extraction",
    "answer_verification",
    "option_verification",
)


def _state(values) -> dict:
    return ledger_store.read_ledger(values["ledger"])


# -- the tracked containers -------------------------------------------------


def test_the_delta_names_only_what_changed() -> None:
    state = ledger_store.wrap(
        {
            "spent_usd": "0",
            "requests": {"a" * 64: {"state": "counting"}},
            "stages": {},
            "papers": {},
            "live_test_papers": {},
            "family_bindings": {},
            "paper_bindings": {},
            "accepted_families": {},
        }
    )
    assert ledger_store._delta(state) == {}
    state["requests"]["b" * 64] = {"state": "counting"}
    state["spent_usd"] = "1"
    delta = ledger_store._delta(state)
    assert set(delta) == {"requests", "spent_usd"}
    assert set(delta["requests"]["set"]) == {"b" * 64}


def test_a_row_edited_in_place_is_reported() -> None:
    """The broker edits rows in place, deep inside the accounting."""
    state = ledger_store.wrap(
        {"requests": {"a" * 64: {"state": "submitted"}}, "spent_usd": "0"}
    )
    state["requests"]["a" * 64]["state"] = "completed"
    delta = ledger_store._delta(state)
    assert delta["requests"]["set"]["a" * 64]["state"] == "completed"


def test_a_row_replaced_by_a_plain_dict_is_still_tracked() -> None:
    state = ledger_store.wrap({"requests": {}, "spent_usd": "0"})
    state["requests"]["a" * 64] = {"state": "counting"}
    state.clear_dirty()
    state["requests"]["a" * 64]["state"] = "submitted"
    delta = ledger_store._delta(state)
    assert delta["requests"]["set"]["a" * 64]["state"] == "submitted"


# -- the journal ------------------------------------------------------------


def test_a_record_replayed_twice_changes_nothing(tmp_path: Path) -> None:
    """A delta is an absolute assignment, which is what makes a stop safe."""
    state = {"spent_usd": "0", "requests": {}}
    delta = {
        "spent_usd": {"value": "3"},
        "requests": {"set": {"a" * 64: {"state": "completed"}}},
    }
    ledger_store.apply_delta(state, delta)
    once = canonical_json(state)
    ledger_store.apply_delta(state, delta)
    assert canonical_json(state) == once


def test_a_torn_tail_is_not_a_record(tmp_path: Path) -> None:
    """A stop in the middle of an append leaves bytes, never a record."""
    ledger = tmp_path / "ledger.json"
    write_json(ledger, {"spent_usd": "0", "requests": {}})
    ledger_store.initialize_store(ledger)
    journal = ledger_store.journal_file(ledger)
    record = {
        "schema": ledger_store.JOURNAL_RECORD_SCHEMA,
        "seq": 1,
        "at": "2026-09-17T00:00:00Z",
        "delta": {"spent_usd": {"value": "5"}},
    }
    with journal.open("ab") as handle:
        handle.write((canonical_json(record) + "\n").encode())
        handle.write(b'{"schema":"shared-paid-call-ledger-journ')
    assert ledger_store.read_ledger(ledger)["spent_usd"] == "5"


def test_a_snapshot_changed_outside_the_store_fails_closed(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    write_json(ledger, {"spent_usd": "0", "requests": {}})
    ledger_store.initialize_store(ledger)
    write_json(ledger, {"spent_usd": "9", "requests": {}})
    with pytest.raises(ValueError, match="changed outside the store"):
        ledger_store.read_ledger(ledger)


def test_a_stop_between_the_base_record_and_the_snapshot_reconstructs(
    tmp_path: Path,
) -> None:
    """The base record is written first and names the snapshot it supersedes."""
    ledger = tmp_path / "ledger.json"
    write_json(ledger, {"spent_usd": "0", "requests": {}})
    ledger_store.initialize_store(ledger)
    store = ledger_store.LedgerStore(ledger)
    state = store.load()
    state["spent_usd"] = "4"
    store.commit(state, now="2026-09-17T00:00:00Z")
    store.flush()
    # The base record lands; the machine stops before the snapshot does.
    before = ledger.read_bytes()
    ledger_store.write_snapshot(ledger, state, store.sequence)
    ledger.write_bytes(before)
    assert ledger_store.read_ledger(ledger)["spent_usd"] == "4"


# -- one broker, sixteen threads -------------------------------------------


def test_sixteen_threads_admit_settle_and_prove_exact_totals(
    tmp_path: Path,
) -> None:
    """The captain's target, in miniature: sixteen at once, exact money."""
    values = _concurrent(tmp_path, parallel=True)
    broker = values["broker"]
    # Sixteen papers, four calls each: the live-test policy admits at most
    # twenty papers, and the point is the sixteen threads, not the papers.
    work = [
        (f"paper-{paper}", index)
        for paper in range(16)
        for index in range(4)
    ]
    count = len(work)

    with ThreadPoolExecutor(max_workers=16) as pool:
        receipts = [
            future.result()
            for future in [
                pool.submit(execute, broker, paper=paper, stage=stage)
                for paper, stage in [
                    (paper, _STAGES[index]) for paper, index in work
                ]
            ]
        ]

    assert [receipt["state"] for receipt in receipts] == ["completed"] * count
    state = _state(values)
    assert len(state["requests"]) == count
    assert state["inflight"] == 0
    assert state["halted"] is False
    assert state["generation_submissions"] == count
    expected = sum(
        Decimal(receipt["actual_cost_usd"]) for receipt in receipts
    )
    assert Decimal(state["spent_usd"]) == expected
    assert Decimal(state["reserved_usd"]) == Decimal("0")
    assert len(state["papers"]) == 16
    # Every committed state is a state the full row-by-row proof accepts.
    broker._prove_ledger(state)


def test_a_wave_of_commits_shares_its_flushes(tmp_path: Path) -> None:
    """Group commit is the answer to a 150 to 470 ms durable write."""
    values = _concurrent(tmp_path, parallel=True)
    broker = values["broker"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        for future in [
            pool.submit(execute, broker, paper=f"paper-{paper}", stage=_STAGES[index])
            for paper in range(12)
            for index in range(4)
        ]:
            future.result()

    count = 48

    store = broker._store
    assert store.append_count >= count
    # One flush per append would be the old shape. The wave shares them.
    assert store.fsync_count < store.append_count


def test_the_compactor_writes_a_snapshot_the_full_proof_accepts(
    tmp_path: Path,
) -> None:
    values = _concurrent(tmp_path, parallel=True)
    broker = values["broker"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [
            pool.submit(execute, broker, paper=f"paper-{index}") for index in range(16)
        ]:
            future.result()

    assert broker.compact() is True
    snapshot = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert len(snapshot["requests"]) == 16
    # The snapshot and the store say the same thing.
    assert canonical_json(snapshot) == canonical_json(_state(values))
    broker._prove_ledger(snapshot)


def test_every_reader_sees_the_committed_state_before_a_compaction(
    tmp_path: Path,
) -> None:
    """The website, the guard and the viewer read the store, not the file."""
    from arctic_qa import benchmark_guard, live_papers

    values = _concurrent(tmp_path)
    broker = values["broker"]
    execute(broker, paper="paper-1")
    execute(broker, paper="paper-2")

    snapshot = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert len(snapshot["requests"]) == 0  # the compactor has not run yet

    for reader in (
        ledger_store.read_ledger,
        benchmark_guard.read_ledger,
        live_papers.read_shared_ledger,
    ):
        state = reader(values["ledger"])
        assert len(state["requests"]) == 2
        assert Decimal(state["spent_usd"]) > 0


# -- the migration ----------------------------------------------------------


def test_the_migration_keeps_every_field_and_freezes_the_archive(
    tmp_path: Path,
) -> None:
    values = fixture(tmp_path, transport=Transport())
    for index in range(3):
        execute(values["broker"], paper=f"paper-{index}")
    before = json.loads(values["ledger"].read_text(encoding="utf-8"))

    # A ledger written before the store existed has no journal beside it.
    ledger_store.journal_file(values["ledger"]).unlink(missing_ok=True)
    ledger_store.journal_base_file(values["ledger"]).unlink(missing_ok=True)

    dry = ledger_migration.migrate(values["ledger"])
    assert dry["action"] == "dry-run"
    assert dry["identical"] is True

    report = ledger_migration.migrate(values["ledger"], apply=True)
    assert report["identical"] is True
    assert report["fields_that_differ"] == []
    assert report["request_rows_that_differ"] == []
    assert report["request_rows"] == 3
    archive = Path(report["archive_file"])
    assert archive.is_file()
    assert json.loads(archive.read_text(encoding="utf-8")) == before
    assert archive.stat().st_mode & 0o222 == 0

    after = ledger_store.read_ledger(values["ledger"])
    assert canonical_json(after) == canonical_json(before)
    assert ledger_migration.check(values["ledger"], archive)["identical"] is True


def test_the_migration_refuses_to_run_twice(tmp_path: Path) -> None:
    values = fixture(tmp_path, transport=Transport())
    execute(values["broker"], paper="paper-1")
    ledger_store.journal_file(values["ledger"]).unlink(missing_ok=True)
    ledger_store.journal_base_file(values["ledger"]).unlink(missing_ok=True)
    ledger_migration.migrate(values["ledger"], apply=True)
    again = ledger_migration.migrate(values["ledger"], apply=True)
    assert again["action"] == "already-migrated"
    assert again["identical"] is True


def test_a_migrated_ledger_keeps_taking_paid_calls(tmp_path: Path) -> None:
    values = fixture(tmp_path, transport=Transport())
    execute(values["broker"], paper="paper-1")
    spent_before = Decimal(_state(values)["spent_usd"])
    ledger_store.journal_file(values["ledger"]).unlink(missing_ok=True)
    ledger_store.journal_base_file(values["ledger"]).unlink(missing_ok=True)
    ledger_migration.migrate(values["ledger"], apply=True)

    second = fixture(tmp_path, transport=Transport())
    receipt = execute(second["broker"], paper="paper-2")
    assert receipt["state"] == "completed"
    state = _state(values)
    assert len(state["requests"]) == 2
    assert Decimal(state["spent_usd"]) > spent_before


# -- the sixteen-at-once rate pair -------------------------------------------


def test_the_parallel_rate_pair_is_registered_and_nothing_between_it(
    tmp_path: Path,
) -> None:
    """Policy v12 moves the two request-rate limits together and nothing else."""
    from arctic_qa import model_broker

    assert (16, 100) in model_broker.ALLOWED_REQUEST_RATES
    assert model_broker.CHAPTER3_PARALLEL_CHANGE in model_broker.POLICY_TRANSITION_CHANGES
    assert model_broker.CHAPTER3_PARALLEL_CHANGE == {
        "maximum_concurrent_generation_requests": {"from": 8, "to": 16},
        "maximum_generation_requests_per_minute": {"from": 40, "to": 100},
    }
    source = ROOT / "config" / "streaming-dataset-budget-policy-v1.json"
    base = json.loads(source.read_text(encoding="utf-8"))
    accepted = dict(base)
    accepted["maximum_concurrent_generation_requests"] = 16
    accepted["maximum_generation_requests_per_minute"] = 100
    path = tmp_path / "v12.json"
    write_json(path, accepted)
    model_broker._validate_policy(path)
    for slots, per_minute in ((16, 40), (8, 100), (12, 100), (16, 60)):
        refused = dict(base)
        refused["maximum_concurrent_generation_requests"] = slots
        refused["maximum_generation_requests_per_minute"] = per_minute
        write_json(path, refused)
        with pytest.raises(ValueError, match="streaming budget value changed"):
            model_broker._validate_policy(path)


def test_the_parallel_transition_names_the_expansion_tranche() -> None:
    """A rate transition moves no money, so its tranche is the expansion ceiling.

    The live apply of 2026-09-17 04:53 UTC was refused with "the policy
    transition identity changed" because the tranche rule did not name the
    parallel change. The rule is read from the source here, so a third rate
    pair cannot forget it silently.
    """
    import inspect

    from arctic_qa import model_broker

    source = inspect.getsource(model_broker.SharedGeminiBroker._validate_transition_authorization)
    tuple_start = source.index("CHAPTER3_EXPANSION_CHANGE,")
    tuple_end = source.index("expected_tranche = CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD")
    named = source[tuple_start:tuple_end]
    assert "CHAPTER3_CONCURRENCY_CHANGE" in named
    assert "CHAPTER3_PARALLEL_CHANGE" in named


def test_the_no_replay_probe_reads_the_receipts_listing(tmp_path: Path) -> None:
    """The reviewed transition path builds a broker without __init__.

    The live v12 transition was refused at 04:58 UTC on 2026-09-17 with
    "requires a settled ledger or validated no-replay holds" because the probe
    lacked the receipts-listing cache. The ledger held an ambiguous charge and
    a retained reservation, which is what routes a transition through here.
    """
    from arctic_qa.model_broker import SharedGeminiBroker

    receipts = tmp_path / "receipts"
    receipts.mkdir()
    ledger = {"requests": {}, "ambiguous_reserved_usd": "0.1", "reserved_usd": "0.02"}
    assert SharedGeminiBroker.validate_no_replay_liabilities(
        ledger=ledger, receipts_dir=receipts
    ) == {}


def test_a_flush_after_reading_a_peer_record_returns(tmp_path: Path) -> None:
    """The flush covers this process's own writes; a peer's are the peer's.

    The adversarial audit of 2026-09-17 found that a flush after ``read``
    tailed another process's record waited for an offset this process never
    wrote, and could only end when a peer thread committed. In the streaming
    evaluator, which admits one call at a time, it would never have ended.
    """
    ledger = tmp_path / "ledger.json"
    write_json(ledger, {"spent_usd": "0", "requests": {}})
    ledger_store.initialize_store(ledger)
    ours = ledger_store.LedgerStore(ledger)
    state = ours.load()
    state["spent_usd"] = "1"
    ours.commit(state, now="2026-09-17T00:00:00Z")
    ours.flush()
    peer = ledger_store.LedgerStore(ledger)
    peer_state = peer.load()
    peer_state["spent_usd"] = "2"
    peer.commit(peer_state, now="2026-09-17T00:00:01Z")
    peer.flush()
    assert ours.read()["spent_usd"] == "2"
    done = threading.Event()

    def flush() -> None:
        ours.flush()
        done.set()

    threading.Thread(target=flush, daemon=True).start()
    assert done.wait(5.0), "the flush spun on a peer's record"


def test_the_snapshot_is_bound_by_the_bytes_that_were_read(tmp_path: Path) -> None:
    """A compaction between the read and the binding must not skip records."""
    ledger = tmp_path / "ledger.json"
    write_json(ledger, {"spent_usd": "0", "requests": {}})
    ledger_store.initialize_store(ledger)
    store = ledger_store.LedgerStore(ledger)
    state = store.load()
    state["spent_usd"] = "1"
    store.commit(state, now="2026-09-17T00:00:00Z")
    store.flush()
    old_bytes = ledger.read_bytes()
    # The compaction lands after a reader took its bytes.
    ledger_store.write_snapshot(ledger, state, store.sequence)
    stale = json.loads(old_bytes.decode())
    assert ledger_store.apply_journal(ledger, stale, old_bytes)["spent_usd"] == "1"
    fresh_bytes = ledger.read_bytes()
    assert ledger_store.apply_journal(
        ledger, json.loads(fresh_bytes.decode()), fresh_bytes
    )["spent_usd"] == "1"
