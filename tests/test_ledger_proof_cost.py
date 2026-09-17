"""What one ledger read of a concurrent broker is allowed to cost.

The parallel store made a ledger read cost the records another process
appended. The proof on top of it still cost the whole history: on the live
chapter 3 ledger of 2026-09-17 (8,230 request rows, 32,251 receipt files) one
warm read took 371 ms, and a paid call makes five to seven of them. That is
what held the exclusive operation lock a mean 2.4 s per paid call and capped
the run at about 21 requests a minute whatever the thread count was.

These tests hold the shape that fixed it: the receipts directory is listed
once and everything derived from it is derived once, and the immutable-event
proof replays only the rows the store reports moved. Each one also holds the
proof itself, because a cheaper proof that proves less is not the change.
"""

from __future__ import annotations

import json
import os
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

from arctic_qa import ledger_store, model_broker  # noqa: E402
from arctic_qa.model_broker import SharedGeminiBroker  # noqa: E402

from test_model_broker import (  # noqa: E402
    Transport,
    execute,
    fixture,
    write_json,
)


def _scaled_policy(tmp_path: Path, *, slots: int, per_minute: int) -> Path:
    """The shipped policy at one registered request rate."""
    source = ROOT / "config" / "streaming-dataset-budget-policy-v1.json"
    policy = json.loads(source.read_text(encoding="utf-8"))
    policy["maximum_concurrent_generation_requests"] = slots
    policy["maximum_generation_requests_per_minute"] = per_minute
    path = tmp_path / f"policy-{slots}-{per_minute}.json"
    write_json(path, policy)
    return path


# Four stages of the shipped price configuration, so one paper can make four
# distinct paid requests under the twenty-paper live-test cap.
_STAGES = (
    "eligibility",
    "finding_answer_extraction",
    "answer_verification",
    "option_verification",
)


def _concurrent(tmp_path: Path, transport=None, *, policy_file: Path | None = None):
    """A broker in the shape the chapter 3 producer runs in."""
    values = fixture(
        tmp_path,
        transport=transport or Transport(),
        policy_file=policy_file,
    )
    broker = values["broker"]
    broker.concurrent_construction = True
    broker.deferred_snapshot = True
    return values


class _CountingScandir:
    """Count every listing of the receipts directory."""

    def __init__(self, receipts_dir: Path) -> None:
        self.receipts_dir = receipts_dir
        self.count = 0
        self._real = os.scandir

    def __call__(self, path):  # type: ignore[no-untyped-def]
        if Path(path) == self.receipts_dir:
            self.count += 1
        return self._real(path)


def test_a_warm_read_lists_the_receipts_directory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing is a timer, not the directory fingerprint.

    Under paper concurrency the directory moves on every paid call of every
    worker, so the fingerprint alone made a 32,251-entry ``scandir`` part of
    nearly every read: 56 ms of the 371 ms a warm proof cost.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    execute(broker, paper="p1")
    counter = _CountingScandir(broker.receipts_dir)
    monkeypatch.setattr(os, "scandir", counter)
    for _ in range(8):
        broker._validated_ledger()
    assert counter.count == 0, counter.count

    # A sequential broker, which is every reviewed operation, keeps the exact
    # fingerprint and sees a receipt the moment it lands.
    broker.concurrent_construction = False
    (broker.receipts_dir / "later.json").write_text("{}", encoding="utf-8")
    broker._validated_ledger()
    assert counter.count == 1, counter.count


def test_the_evaluator_shape_lists_the_receipts_directory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streaming evaluator is the ledger's other live writer, and says so.

    It holds no construction request, so ``concurrent_construction`` says
    nothing about it, and the listing refresh hung on that flag alone. The
    producer moves the receipts directory on every paid call of every worker,
    so the evaluator paid a full listing, and a re-derivation of everything
    read out of it, on every ledger read it made inside the exclusive
    operation lock: 0.25 s a read against 0.026 s kept, measured on the live
    chapter 3 ledger at 13,309 rows and 52,716 receipts on 2026-09-17. Three
    sections of one paid Gemini call held that lock about 22 s in total, and
    the arm ran at 13.7 questions an hour because of it.
    """
    values = fixture(tmp_path, transport=Transport())
    broker = values["broker"]
    # The evaluator's shape: concurrent requests, no concurrent construction.
    broker.concurrent_requests = True
    broker.deferred_snapshot = True
    assert broker.concurrent_construction is False
    execute(broker, paper="p1")
    counter = _CountingScandir(broker.receipts_dir)
    monkeypatch.setattr(os, "scandir", counter)
    for _ in range(8):
        broker._validated_ledger()
    assert counter.count == 0, counter.count

    # The producer writes a receipt of its own, which moves the directory.
    # The evaluator sees it at the next listing, which the timer bounds; it
    # does not re-list, and does not throw away its derived values, on a move
    # it did not make.
    (broker.receipts_dir / "peer.json").write_text("{}", encoding="utf-8")
    broker._validated_ledger()
    assert counter.count == 0, counter.count

    # A reviewed operation runs alone and keeps the exact fingerprint.
    broker.concurrent_requests = False
    (broker.receipts_dir / "later.json").write_text("{}", encoding="utf-8")
    broker._validated_ledger()
    assert counter.count == 1, counter.count


def test_the_evaluator_broker_says_it_shares_the_ledger() -> None:
    """The evaluator's own factory sets the flags, not a caller of them.

    ``abstention_cli._broker`` builds every broker the streaming evaluator and
    the concurrent plan use. Both say that they share the ledger, and both ask
    to hear a scheduling refusal instead of being handed one they can never
    re-ask: the trial then waits it out and is left pending.
    """
    source = (ROOT / "src" / "arctic_qa" / "abstention_cli.py").read_text(
        encoding="utf-8"
    )
    assert "concurrent_requests=bool(concurrent)," in source
    assert (
        "defer_transient_reservations=bool(defer_transient_reservations),"
    ) in source
    assert source.count("evaluation_gate_file=gate,\n                    concurrent=True,") == 2
    assert source.count("defer_transient_reservations=True,") == 2
    assert "deferred_snapshot=True" not in source


def test_a_concurrent_construction_broker_still_shares_the_ledger(
    tmp_path: Path,
) -> None:
    """The producer's flag still implies the listing refresh.

    A concurrent construction run is one of the ledger's live writers by
    definition, so the two flags are not independent.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    assert broker.concurrent_requests is True
    broker.concurrent_construction = False
    assert broker.concurrent_requests is False


def test_a_second_broker_of_one_ledger_does_not_start_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process starts a ledger once, not once a question.

    The streaming evaluator builds a broker a question, because the derived
    evaluation gate belongs to the question. Every one of them compacted the
    whole ledger at its start: a 60 MB materialization, a proof of every row
    and an 18 MB durable write, 1.16 s measured on an idle machine on
    2026-09-17, all of it under the shared ledger lock that each of the
    producer's 75 workers takes for every paid call.
    """
    values = fixture(tmp_path, transport=Transport())
    first = values["broker"]
    first.concurrent_requests = True
    first.deferred_snapshot = True
    execute(first, paper="p1")

    compactions: list[bool] = []
    real = SharedGeminiBroker.compact

    def record(self, *, wait: bool = False):  # type: ignore[no-untyped-def]
        compactions.append(wait)
        return real(self, wait=wait)

    monkeypatch.setattr(SharedGeminiBroker, "compact", record)
    second = model_broker.SharedGeminiBroker(
        policy_file=first.policy_file,
        price_config_file=first.price_config_file,
        execution_gate_file=first.execution_gate_file,
        ledger_file=first.ledger_file,
        receipts_dir=first.receipts_dir,
        credential_file=first.credential_file,
        prior_construction_spend_usd=Decimal("0"),
        concurrent_requests=True,
    )
    assert compactions == []
    # The state is the same one: the second broker reads every row the first
    # one wrote, and proves it.
    assert sorted(second._validated_ledger()["requests"]) == sorted(
        first._validated_ledger()["requests"]
    )

    # A reviewed operation runs one at a time and binds the snapshot file, so
    # it compacts whatever another broker of this process did.
    third = model_broker.SharedGeminiBroker(
        policy_file=first.policy_file,
        price_config_file=first.price_config_file,
        execution_gate_file=first.execution_gate_file,
        ledger_file=first.ledger_file,
        receipts_dir=first.receipts_dir,
        credential_file=first.credential_file,
        prior_construction_spend_usd=Decimal("0"),
    )
    assert third.concurrent_requests is False
    assert compactions == [True]


def test_one_compactor_thread_serves_one_ledger(tmp_path: Path) -> None:
    """A broker a question left a compactor thread a question behind it.

    Each held a whole copy of the ledger and registered an ``atexit``
    compaction of its own. The snapshot is derived, so one writer of it is
    enough.
    """
    values = fixture(tmp_path, transport=Transport())
    first = values["broker"]
    first.concurrent_requests = True
    first.deferred_snapshot = True
    execute(first, paper="p1")
    assert first._compaction_thread is not None

    second = model_broker.SharedGeminiBroker(
        policy_file=first.policy_file,
        price_config_file=first.price_config_file,
        execution_gate_file=first.execution_gate_file,
        ledger_file=first.ledger_file,
        receipts_dir=first.receipts_dir,
        credential_file=first.credential_file,
        prior_construction_spend_usd=Decimal("0"),
        concurrent_requests=True,
    )
    second.deferred_snapshot = True
    second._start_compactor()
    assert second._compaction_thread is None

    # The owner gives the ledger back when it stops, so the next broker of it
    # keeps the snapshot current.
    first.stop_compactor()
    second._start_compactor()
    assert second._compaction_thread is not None
    second.stop_compactor()


def test_a_second_broker_starts_from_what_the_process_proved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broker a question must not prove the whole history a question.

    The evaluator builds one broker per question, and its start proved every
    row against its receipts and stated the final receipt of every terminal
    row: 2.6 s and 0.24 s at 15,347 rows on 2026-09-17, under the shared
    ledger lock.
    """
    values = fixture(tmp_path, transport=Transport())
    first = values["broker"]
    first.concurrent_requests = True
    first.deferred_snapshot = True
    for paper in ("p1", "p2", "p3"):
        execute(first, paper=paper)
    first._validated_ledger()

    proved: list[str] = []
    real = SharedGeminiBroker._validate_request_events

    def record(self, ledger, *, request_key, **kwargs):  # type: ignore[no-untyped-def]
        proved.append(request_key)
        return real(self, ledger, request_key=request_key, **kwargs)

    monkeypatch.setattr(SharedGeminiBroker, "_validate_request_events", record)
    second = model_broker.SharedGeminiBroker(
        policy_file=first.policy_file,
        price_config_file=first.price_config_file,
        execution_gate_file=first.execution_gate_file,
        ledger_file=first.ledger_file,
        receipts_dir=first.receipts_dir,
        credential_file=first.credential_file,
        prior_construction_spend_usd=Decimal("0"),
        concurrent_requests=True,
    )
    # Not one row re-proved, and the custody of the terminal receipts carried
    # over with it.
    assert proved == []
    assert second._custody_proved == first._custody_proved
    assert second._immutable_events_proved == first._immutable_events_proved
    # The copies are its own, so one broker never writes into another's proof.
    assert second._immutable_events_proved is not first._immutable_events_proved
    assert second._custody_proved is not first._custody_proved


def test_a_seeded_broker_still_proves_a_row_that_moved(tmp_path: Path) -> None:
    """The seed is a starting point, never a pass. Every row is compared.

    A seeded broker takes the signature of every row and re-proves the ones
    whose bytes differ from the ones this process proved, which is the same
    check a warm broker makes between two reads.
    """
    values = fixture(tmp_path, transport=Transport())
    first = values["broker"]
    first.concurrent_requests = True
    first.deferred_snapshot = True
    execute(first, paper="p1")
    first._validated_ledger()

    # A peer moves one row behind the process's back.
    peer = ledger_store.LedgerStore(first.ledger_file)
    state = peer.load()
    moved = sorted(state["requests"])[0]
    state["requests"][moved]["run_id"] = "another-run"
    peer.commit(state, now="2026-09-17T12:00:00Z")
    peer.flush()

    with pytest.raises(ValueError, match="integrity"):
        model_broker.SharedGeminiBroker(
            policy_file=first.policy_file,
            price_config_file=first.price_config_file,
            execution_gate_file=first.execution_gate_file,
            ledger_file=first.ledger_file,
            receipts_dir=first.receipts_dir,
            credential_file=first.credential_file,
            prior_construction_spend_usd=Decimal("0"),
            concurrent_requests=True,
        )


def test_a_seeded_broker_still_takes_the_full_pass_when_it_is_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seed carries the clock of the last full pass, never resets it.

    The custody of a terminal receipt is proved with the rows, and the full
    pass every IMMUTABLE_EVENT_REVALIDATION_SECONDS is what catches a receipt
    taken away behind this process's back. A seed that reset that clock would
    push the bound out for ever.
    """
    values = fixture(tmp_path, transport=Transport())
    first = values["broker"]
    first.concurrent_requests = True
    first.deferred_snapshot = True
    execute(first, paper="p1")
    first._validated_ledger()
    assert first._immutable_events_proved_at is not None

    second = model_broker.SharedGeminiBroker(
        policy_file=first.policy_file,
        price_config_file=first.price_config_file,
        execution_gate_file=first.execution_gate_file,
        ledger_file=first.ledger_file,
        receipts_dir=first.receipts_dir,
        credential_file=first.credential_file,
        prior_construction_spend_usd=Decimal("0"),
        concurrent_requests=True,
    )
    assert second._immutable_events_proved_at == first._immutable_events_proved_at

    # When the bound arrives, the full pass runs over every row and clears the
    # seeded custody set, so a receipt taken away behind this process's back is
    # caught there exactly as it always was.
    proved: list[str] = []
    real = SharedGeminiBroker._validate_request_events

    def record(self, ledger, *, request_key, **kwargs):  # type: ignore[no-untyped-def]
        proved.append(request_key)
        return real(self, ledger, request_key=request_key, **kwargs)

    monkeypatch.setattr(SharedGeminiBroker, "_validate_request_events", record)
    second._immutable_events_proved_at = None
    second._validated_ledger()
    assert proved == sorted(
        ledger_store.read_ledger(second.ledger_file)["requests"]
    ), proved
    # The seeded custody is gone with it, so the next recovery states the final
    # receipt of every terminal row again and a receipt taken away is caught.
    assert second._custody_proved == set()


def test_a_warm_read_replays_only_the_rows_that_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store reports the rows a record moved, and the proof trusts that.

    A signature of every row cost 136 ms of one warm proof on the live ledger.
    The same tracking already carries the money proof of the delta.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    for paper in ("p1", "p2", "p3"):
        execute(broker, paper=paper)

    proved: list[str] = []
    real = SharedGeminiBroker._validate_request_events

    def record(self, ledger, *, request_key, **kwargs):  # type: ignore[no-untyped-def]
        proved.append(request_key)
        return real(self, ledger, request_key=request_key, **kwargs)

    # One read first, so every row the three calls wrote carries its proof.
    broker._validated_ledger()
    monkeypatch.setattr(SharedGeminiBroker, "_validate_request_events", record)
    for _ in range(5):
        broker._validated_ledger()
    assert proved == []

    # The row a peer's journal record moves is proved again, and only it.
    peer = ledger_store.LedgerStore(broker.ledger_file)
    state = peer.load()
    moved = sorted(state["requests"])[0]
    state["requests"][moved]["run_id"] = "another-run"
    peer.commit(state, now="2026-09-17T07:00:00Z")
    peer.flush()
    with pytest.raises(ValueError, match="integrity"):
        broker._validated_ledger()
    assert proved == [moved]


def test_a_row_changed_behind_the_store_is_still_caught(tmp_path: Path) -> None:
    """A snapshot rewritten outside the journal owes every signature again.

    ``_pending_full`` means the requests map was replaced whole and which rows
    moved is not known, so the proof falls back to the signature of each row
    rather than trust a list it was never given.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    execute(broker, paper="p1")
    broker._validated_ledger()

    state, seq, offset = ledger_store.materialize(broker.ledger_file)
    key = sorted(state["requests"])[0]
    state["requests"][key]["stage"] = "generation"
    with ledger_store.held_compaction_lock(broker.ledger_file, wait=True):
        ledger_store.write_snapshot(
            broker.ledger_file, state, seq, journal_offset=offset
        )
    with pytest.raises(ValueError, match="integrity"):
        broker._validated_ledger()


def test_a_receipt_absent_from_the_ledger_is_still_caught(tmp_path: Path) -> None:
    """The name-to-key scan is derived once per listing, and it still proves."""
    values = _concurrent(tmp_path)
    broker = values["broker"]
    execute(broker, paper="p1")
    broker._validated_ledger()
    stranger = "0" * 64
    (broker.receipts_dir / f"{stranger}.json").write_text("{}", encoding="utf-8")
    # A sequential broker sees the new name at once; the concurrent one sees
    # it at the next listing, which is what the timer bounds.
    broker.concurrent_construction = False
    with pytest.raises(ValueError, match="integrity"):
        broker._validated_ledger()


def test_an_accepted_item_is_proved_against_the_receipt_it_just_wrote(
    tmp_path: Path,
) -> None:
    """A receipt and its ledger row move together, so a stale listing is not a proof.

    The accepted-item answer was derived once per listing. A concurrent broker
    re-lists on a timer, so for a few seconds the listing did not hold the
    receipt the ledger row named, and the next read raised "the accepted-item
    ledger differs from immutable events". That false halt stopped the chapter
    3 producer at 07:40:51 UTC on 2026-09-17.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    execute(broker, paper="p1")
    broker._validated_ledger()
    broker.record_accepted(family_id="family-p1", item_id="item-1")
    # No wait for the refresh timer: the read that follows the write proves it.
    broker._validated_ledger()
    assert broker.status()["integrity_valid"] is True
    ledger = ledger_store.read_ledger(broker.ledger_file)
    assert ledger["accepted_families"] == {"family-p1": "item-1"}
    # A receipt taken away behind this process's back is caught by the full
    # pass, which is the bound the custody of a terminal receipt already has.
    for path in broker.receipts_dir.glob("accepted-*.json"):
        path.chmod(0o600)
        path.unlink()
    broker._immutable_events_proved_at = None
    with pytest.raises(ValueError, match="integrity"):
        broker._validated_ledger()


def test_one_paid_call_takes_three_exclusive_sections(tmp_path: Path) -> None:
    """The mutations are inside the lock and the free count is outside it.

    Holding the free token count, its retries or the pacing wait inside the
    exclusive operation lock faulted 19 papers in 40 minutes on 2026-09-16.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    sections: list[str] = []
    held: list[str] = []
    real_acquire = SharedGeminiBroker._acquire_operation_lock
    transport = broker.transport

    def acquire(self, section, request_key):  # type: ignore[no-untyped-def]
        sections.append(section)
        held.append(section)
        return real_acquire(self, section, request_key)

    real_release = SharedGeminiBroker._release_operation_lock

    def release(self, handle):  # type: ignore[no-untyped-def]
        if held:
            held.pop()
        return real_release(self, handle)

    counted: list[list[str]] = []
    real_post = transport.post

    def post(model, method, body):  # type: ignore[no-untyped-def]
        if method == "countTokens":
            counted.append(list(held))
        return real_post(model, method, body)

    broker._acquire_operation_lock = acquire.__get__(broker)  # type: ignore[assignment]
    broker._release_operation_lock = release.__get__(broker)  # type: ignore[assignment]
    transport.post = post  # type: ignore[assignment]
    receipt = execute(broker, paper="p1")
    assert receipt["state"] == "completed"
    assert sections == ["orphan_recovery", "count_registration", "reserve"]
    # The free token count holds no exclusive section.
    assert counted == [[]], counted


def test_the_reservation_counts_the_evaluation_calls_the_totals_count() -> None:
    """The cheap count is the same number the full evaluation totals report.

    ``_evaluation_totals`` parses a Decimal for every evaluation row of the
    whole history, which was 12 ms of the reservation's exclusive section at
    889 rows. The reservation asks only how many are in flight.
    """
    ledger = {
        "requests": {
            "a": {
                "phase": "benchmark_evaluation",
                "state": "submitted",
                "reserved_usd": "0.01",
            },
            "b": {
                "phase": "benchmark_evaluation",
                "state": "completed",
                "reserved_usd": "0.01",
                "actual_cost_usd": "0.005",
            },
            "c": {
                "phase": "benchmark_evaluation",
                "state": "orphaned_no_replay",
                "reserved_usd": "0.02",
            },
            "d": {
                "phase": "away_production",
                "state": "submitted",
                "reserved_usd": "0.03",
            },
            "e": {"state": "submitted", "reserved_usd": "0.04"},
        }
    }
    totals = SharedGeminiBroker._evaluation_totals(ledger)
    assert SharedGeminiBroker._evaluation_inflight(ledger) == int(totals["inflight"])
    assert SharedGeminiBroker._evaluation_inflight(ledger) == 1
    ledger["inflight"] = 3
    assert SharedGeminiBroker._phase_inflight(ledger, "away_production") == 2
    assert SharedGeminiBroker._phase_inflight(ledger, "benchmark_evaluation") == 1


def test_the_fifty_at_once_rate_pair_is_registered_and_nothing_between_it(
    tmp_path: Path,
) -> None:
    """Policy v13 moves the two request-rate limits together and nothing else."""
    assert (50, 300) in model_broker.ALLOWED_REQUEST_RATES
    assert model_broker.CHAPTER3_SCALE_CHANGE in model_broker.POLICY_TRANSITION_CHANGES
    assert model_broker.CHAPTER3_SCALE_CHANGE == {
        "maximum_concurrent_generation_requests": {"from": 16, "to": 50},
        "maximum_generation_requests_per_minute": {"from": 100, "to": 300},
    }
    model_broker._validate_policy(_scaled_policy(tmp_path, slots=50, per_minute=300))
    for slots, per_minute in ((50, 100), (16, 300), (32, 300), (50, 200)):
        with pytest.raises(ValueError, match="streaming budget value changed"):
            model_broker._validate_policy(
                _scaled_policy(tmp_path, slots=slots, per_minute=per_minute)
            )


def test_the_seventy_five_at_once_rate_pair_is_registered_and_nothing_between_it(
    tmp_path: Path,
) -> None:
    """Policy v15 moves the two request-rate limits together and nothing else.

    Captain order 2026-09-17 09:14 UTC: "Also raise concurrency for the night
    to 75 instead of 50. If this gives problems, lower it to 50 again." The
    fall back needs no transition, so the fifty pair stays registered.
    """
    assert (75, 450) in model_broker.ALLOWED_REQUEST_RATES
    assert (50, 300) in model_broker.ALLOWED_REQUEST_RATES
    assert model_broker.CHAPTER3_NIGHT_CHANGE in model_broker.POLICY_TRANSITION_CHANGES
    assert model_broker.CHAPTER3_NIGHT_CHANGE == {
        "maximum_concurrent_generation_requests": {"from": 50, "to": 75},
        "maximum_generation_requests_per_minute": {"from": 300, "to": 450},
    }
    model_broker._validate_policy(_scaled_policy(tmp_path, slots=75, per_minute=450))
    for slots, per_minute in ((75, 300), (50, 450), (64, 450), (75, 400)):
        with pytest.raises(ValueError, match="streaming budget value changed"):
            model_broker._validate_policy(
                _scaled_policy(tmp_path, slots=slots, per_minute=per_minute)
            )


def test_the_seventy_five_at_once_transition_names_the_six_hundred_tranche() -> None:
    """A rate transition names the construction ceiling already authorized.

    The USD 600 allocation moved that ceiling on 2026-09-17, so a rate
    transition applied after it names the six-hundred tranche and not the
    expansion one. The rule is read from the source, as the fifty pair's is.
    """
    source = Path(model_broker.__file__).read_text(encoding="utf-8")
    rule = source[source.index("elif changed_policy_fields in (") :]
    rule = rule[: rule.index('expected_tranche = Decimal("5")')]
    assert "CHAPTER3_NIGHT_CHANGE" in rule
    assert "CHAPTER3_SIX_HUNDRED_CUMULATIVE_CEILING_USD" in rule
    assert (
        model_broker.CHAPTER3_SIX_HUNDRED_CUMULATIVE_CEILING_USD
        != model_broker.CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD
    )


def test_the_fifty_at_once_transition_names_the_expansion_tranche() -> None:
    """A rate transition moves no money, so its tranche is the expansion ceiling.

    The parallel apply of 2026-09-17 04:53 UTC was refused with "the policy
    transition identity changed" because the tranche rule did not name the
    change. The rule is read from the source here, so a fourth rate pair
    cannot forget it silently.
    """
    source = Path(model_broker.__file__).read_text(encoding="utf-8")
    rule = source[source.index("elif changed_policy_fields in (") :]
    rule = rule[: rule.index('expected_tranche = Decimal("5")')]
    assert "CHAPTER3_SCALE_CHANGE" in rule


def test_the_six_hundred_ceiling_is_registered_and_couples_its_checkpoint(
    tmp_path: Path,
) -> None:
    """Policy v14 moves the away ceiling and its checkpoint together, and nothing else.

    Captain order 2026-09-17 08:25 UTC: "Up the budget to $600 and make sure
    that we never exceed $1000 for the whole project." The chapter 3 allocation
    becomes USD 600 on top of the USD 53.990121 spent before this run, so both
    money limits become USD 653.990121. The project lifetime ceiling does not
    move.
    """
    assert model_broker.CHAPTER3_SIX_HUNDRED_CUMULATIVE_CEILING_USD == Decimal(
        "653.990121"
    )
    assert model_broker.CHAPTER3_SIX_HUNDRED_CHANGE in model_broker.CEILING_CHANGES
    assert (
        model_broker.CHAPTER3_SIX_HUNDRED_CHANGE
        in model_broker.POLICY_TRANSITION_CHANGES
    )

    source = ROOT / "config" / "streaming-dataset-budget-policy-v1.json"
    base = json.loads(source.read_text(encoding="utf-8"))
    base["maximum_concurrent_generation_requests"] = 50
    base["maximum_generation_requests_per_minute"] = 300
    base["away_maximum_generation_submissions"] = 20000
    base["accepted_question_target"] = 2000
    base["live_test_suballocation_usd"] = "20.00"
    base["live_test_maximum_papers"] = None
    base["live_test_maximum_generation_submissions"] = None

    accepted = dict(base)
    accepted["away_session_total_ceiling_usd"] = "653.990121"
    accepted["construction_review_checkpoint_usd"] = "653.990121"
    path = tmp_path / "v14.json"
    write_json(path, accepted)
    policy = model_broker._validate_policy(path)
    assert policy["project_lifetime_ceiling_usd"] == "1000.00"

    # The ceiling and its checkpoint never move apart.
    for ceiling, checkpoint in (
        ("653.990121", "253.990121"),
        ("253.990121", "653.990121"),
        ("700.00", "700.00"),
    ):
        refused = dict(base)
        refused["away_session_total_ceiling_usd"] = ceiling
        refused["construction_review_checkpoint_usd"] = checkpoint
        write_json(path, refused)
        with pytest.raises(ValueError, match="streaming budget value changed"):
            model_broker._validate_policy(path)


def test_the_project_lifetime_ceiling_counts_every_phase() -> None:
    """USD 1,000 for the whole project, construction and evaluation together.

    The construction cap adds the evaluation liabilities before it compares,
    and the evaluation cap sums the whole ledger. Neither is per phase, so
    neither can be passed by spending in the other one.
    """
    source = Path(model_broker.__file__).read_text(encoding="utf-8")
    construction = source[source.index("def _check_construction_reservation") :]
    construction = construction[: construction.index("\n    def ")]
    assert (
        "if construction_used + evaluation_used + reserved > _money(\n"
        '            self.policy["project_lifetime_ceiling_usd"], "lifetime"\n'
        "        ):" in construction
    )
    evaluation = source[source.index("def _check_evaluation_reservation") :]
    evaluation = evaluation[: evaluation.index("\n    def ")]
    assert 'for name in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")' in (
        evaluation
    )
    assert (
        "if self.prior + all_used + reserved > _money(\n"
        '            self.policy["project_lifetime_ceiling_usd"], "lifetime"\n'
        "        ):" in evaluation
    )


class _SlowTransport(Transport):
    """Hold every generate call open, and report the peak overlap."""

    def __init__(self, hold_seconds: float = 0.5) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._hold = hold_seconds
        self.active = 0
        self.peak = 0

    def post(self, model: str, method: str, body: dict) -> dict:
        if method != "generateContent":
            return super().post(model, method, body)
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self._hold)
            response = super().post(model, method, body)
        finally:
            with self._lock:
                self.active -= 1
        for candidate in response.get("candidates", []):
            candidate["finishReason"] = "STOP"
        return response


def test_fifty_construction_calls_are_in_flight_at_once(tmp_path: Path) -> None:
    """Fifty papers reach the wire together under the v13 request rate.

    The admission is what sets how many calls can be in flight, so this is the
    end the serialised bookkeeping has to leave room for. The transport holds
    each call open, which is what the live 8-second model latency does.
    """
    transport = _SlowTransport(hold_seconds=0.5)
    values = _concurrent(
        tmp_path,
        transport,
        policy_file=_scaled_policy(tmp_path, slots=50, per_minute=300),
    )
    broker = values["broker"]
    # Twenty papers, four calls each: the live-test policy admits at most
    # twenty papers, and the point is the fifty threads, not the papers.
    work = [
        (f"paper-{paper}", _STAGES[index]) for paper in range(20) for index in range(4)
    ]
    with ThreadPoolExecutor(max_workers=50) as pool:
        receipts = [
            future.result()
            for future in [
                pool.submit(execute, broker, paper=paper, stage=stage)
                for paper, stage in work
            ]
        ]

    assert [receipt["state"] for receipt in receipts] == ["completed"] * len(work)
    assert transport.peak == 50, transport.peak
    status = broker.status()
    assert status["halted"] is False
    assert status["integrity_valid"] is True
    ledger = ledger_store.read_ledger(broker.ledger_file)
    assert ledger["inflight"] == 0
    assert Decimal(ledger["reserved_usd"]) == 0
    assert {row["state"] for row in ledger["requests"].values()} == {"completed"}


def test_the_serialised_bookkeeping_of_one_call_stays_small(tmp_path: Path) -> None:
    """The exclusive lock of one paid call is held for milliseconds, not seconds.

    A bound, not a benchmark: the machine this runs on is shared. What it
    holds is the shape, that the hold does not grow with the history. Fifty
    paid calls are made first, so the proof has rows and receipts to walk.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    for index in range(50):
        execute(broker, paper=f"p{index}")

    holds: list[float] = []
    real_release = SharedGeminiBroker._release_operation_lock

    def release(self, handle):  # type: ignore[no-untyped-def]
        record = self._operation_lock_waits.get(handle)
        if record is not None:
            holds.append(time.monotonic() - record[3])
        return real_release(self, handle)

    broker._release_operation_lock = release.__get__(broker)  # type: ignore[assignment]
    execute(broker, paper="p-measured")
    assert len(holds) == 3, holds
    assert sum(holds) < 0.2, holds


def test_one_ledger_read_of_a_warm_broker_makes_no_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pattern walk of the receipts directory on the call path.

    A usage reconciliation event globbed the whole directory for its receipt,
    which was 44 ms of every ledger read that proved it.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    execute(broker, paper="p1")
    globs: list[str] = []
    real = Path.glob

    def counting_glob(self, pattern, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == broker.receipts_dir:
            globs.append(str(pattern))
        return real(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", counting_glob)
    for _ in range(5):
        broker._validated_ledger()
    execute(broker, paper="p2")
    assert globs == [], globs
