"""The exclusive operation lock as a queue, under paper concurrency.

The first concurrent chapter 3 run kept the bounded wait of the sequential
producer and held the free token count, its retries and the pacing wait inside
the exclusive operation lock. Four paper threads therefore queued behind a
section that could last minutes, the 120-second bound expired and the candidate
fault containment recorded the paper with ``BrokerOperationBusyError``: 19
papers in the first 40 minutes of 2026-09-16, none of them with a fault of its
own.

These tests pin the repair. A concurrent request waits for the whole queue in
front of it, a wait that reaches the sanity ceiling retries the same request key
instead of faulting the paper, and the exclusive section holds the ledger
mutation alone.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from arctic_qa import model_broker  # noqa: E402
from arctic_qa.errors import BrokerOperationBusyError  # noqa: E402

from test_broker_operation_lock_wait import Holder  # noqa: E402
from test_model_broker import Transport, execute, fixture  # noqa: E402


def _concurrent(tmp_path: Path, transport=None):
    values = fixture(tmp_path, transport=transport or Transport())
    values["broker"].concurrent_construction = True
    return values


class SlowCountTransport(Transport):
    """A transport whose free token count takes as long as a real one."""

    def __init__(self, seconds: float) -> None:
        super().__init__()
        self.seconds = seconds
        self.counting = threading.Event()

    def post(self, model: str, method: str, body: dict) -> dict:
        if method == "countTokens":
            self.counting.set()
            time.sleep(self.seconds)
        return super().post(model, method, body)


def test_four_paper_threads_queue_and_every_request_completes(
    tmp_path: Path,
) -> None:
    """Four threads, one lock, no fault: the chapter 3 shape, in miniature."""
    values = _concurrent(tmp_path)
    broker = values["broker"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = [
            future.result()
            for future in [
                pool.submit(execute, broker, paper=f"p{index}") for index in range(4)
            ]
        ]

    assert [receipt["state"] for receipt in receipts] == ["completed"] * 4
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["inflight"] == 0
    assert ledger["halted"] is False
    assert len(ledger["requests"]) == 4


def test_a_concurrent_request_waits_past_the_sequential_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue uses the ceiling; only the sequential path keeps the bound."""
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_WAIT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_QUEUE_CEILING_SECONDS", 30.0)
    values = _concurrent(tmp_path)
    broker = values["broker"]

    with Holder(broker._operation_lock_file, seconds=1.0):
        receipt = execute(broker)

    assert receipt["state"] == "completed"

    sequential = fixture(tmp_path / "sequential", transport=Transport())["broker"]
    with Holder(sequential._operation_lock_file):
        with pytest.raises(BrokerOperationBusyError):
            execute(sequential)


def test_a_reached_ceiling_retries_the_same_request_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ceiling is a sanity bound, not a verdict on the paper."""
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_QUEUE_CEILING_SECONDS", 0.25)
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_QUEUE_ROUNDS", 4)
    values = _concurrent(tmp_path)
    broker = values["broker"]

    with Holder(broker._operation_lock_file, seconds=0.6):
        receipt = execute(broker)

    assert receipt["state"] == "completed"
    lines = capsys.readouterr().err
    assert "ceiling_retry" in lines
    assert "gave_up" not in lines


def test_every_round_of_the_ceiling_is_spent_before_the_busy_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_QUEUE_CEILING_SECONDS", 0.1)
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_QUEUE_ROUNDS", 3)
    values = _concurrent(tmp_path)
    broker = values["broker"]
    transport = broker.transport

    with Holder(broker._operation_lock_file):
        with pytest.raises(BrokerOperationBusyError):
            execute(broker)

    # A refusal of the lock reserves nothing, submits nothing and charges
    # nothing, so the producer contains it against one paper exactly as before.
    assert transport.methods == []
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["requests"] == {}
    assert ledger["inflight"] == 0
    lines = capsys.readouterr().err
    assert lines.count("ceiling_retry") == 2
    assert "gave_up" in lines


def test_the_free_token_count_is_outside_the_exclusive_section(
    tmp_path: Path,
) -> None:
    """Only the mutation is exclusive; the count and the pace are not.

    The count held the lock in the first concurrent run, so every peer thread
    waited for a provider round trip on every request.
    """
    transport = SlowCountTransport(1.0)
    values = _concurrent(tmp_path, transport=transport)
    broker = values["broker"]
    taken: list[float] = []

    def request() -> dict:
        return execute(broker)

    def take_the_lock() -> None:
        assert transport.counting.wait(timeout=10.0)
        started = time.monotonic()
        handle = model_broker.hold_operation_lock(
            broker._operation_lock_file, wait_seconds=5.0
        )
        taken.append(time.monotonic() - started)
        handle.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipt = pool.submit(request)
        peer = pool.submit(take_the_lock)
        peer.result()
        assert receipt.result()["state"] == "completed"

    # The lock was free while the count was on the wire.
    assert taken and taken[0] < 0.5


def test_the_log_measures_the_wait_and_the_hold_of_a_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    values = _concurrent(tmp_path)
    broker = values["broker"]

    with Holder(broker._operation_lock_file, seconds=1.2):
        assert execute(broker)["state"] == "completed"

    lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("[operation-lock]")
    ]
    sections = [line for line in lines if " section " in line]
    assert sections, lines
    assert any("waited_s=" in line and "held_s=" in line for line in sections)


def test_a_long_wait_says_so_while_it_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_HEARTBEAT_SECONDS", 0.2)
    values = _concurrent(tmp_path)
    broker = values["broker"]

    with Holder(broker._operation_lock_file, seconds=0.8):
        assert execute(broker)["state"] == "completed"

    assert "waiting" in capsys.readouterr().err


def test_a_sequential_request_keeps_one_lock_for_the_whole_call(
    tmp_path: Path,
) -> None:
    """The historical shape is unchanged, and it never deadlocks on itself."""
    values = fixture(tmp_path, transport=Transport())
    broker = values["broker"]

    assert execute(broker)["state"] == "completed"
    assert broker._whole_call_operation == {}
    assert broker._operation_lock_waits == {}
    handle = model_broker.hold_operation_lock(broker._operation_lock_file)
    handle.close()


# The admission itself, which is what serialised the threads.


def test_an_unchanged_ledger_is_not_proved_against_its_receipts_again(
    tmp_path: Path,
) -> None:
    """The proof is a pure function of the ledger bytes and the receipts.

    Re-proving 5,393 rows and 21,508 receipt files on every one of the seven
    ledger reads a paid call makes took 17 seconds of a 25-second admission,
    and the admission is serialised, so the four paper threads made no more
    calls a minute than one thread did.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    execute(broker)
    proofs: list[int] = [0]
    original = broker._validate_immutable_events

    def counted(ledger):
        proofs[0] += 1
        return original(ledger)

    broker._validate_immutable_events = counted  # type: ignore[method-assign]

    broker._validated_ledger()
    broker._validated_ledger()
    broker._validated_ledger()

    assert proofs[0] == 1


def test_a_changed_ledger_is_proved_again(tmp_path: Path) -> None:
    values = _concurrent(tmp_path)
    broker = values["broker"]
    execute(broker, paper="p1")
    broker._validated_ledger()
    proofs: list[int] = [0]
    original = broker._validate_immutable_events

    def counted(ledger):
        proofs[0] += 1
        return original(ledger)

    broker._validate_immutable_events = counted  # type: ignore[method-assign]

    execute(broker, paper="p2")
    broker._validated_ledger()

    assert proofs[0] >= 1


def test_a_row_that_moved_is_proved_again_and_a_still_row_is_not(
    tmp_path: Path,
) -> None:
    values = _concurrent(tmp_path)
    broker = values["broker"]
    execute(broker, paper="p1")
    broker._validated_ledger()
    proved = dict(broker._immutable_events_proved)
    assert proved

    execute(broker, paper="p2")
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))

    # Every row of the first call is unchanged and keeps its proof; the row of
    # the second call is new and carries one of its own.
    assert set(broker._immutable_events_proved) == set(ledger["requests"])
    for key, signature in proved.items():
        assert broker._immutable_events_proved[key] == signature


def test_a_receipt_that_changes_is_still_caught(tmp_path: Path) -> None:
    """The proof is skipped, never dropped: a changed receipt still refuses."""
    values = _concurrent(tmp_path)
    broker = values["broker"]
    receipt = execute(broker)
    broker._validated_ledger()
    stem = broker.receipts_dir / f"{receipt['request_key']}.json"
    stored = json.loads(stem.read_text(encoding="utf-8"))
    stored["actual_cost_usd"] = "9.999999"
    stem.chmod(0o644)
    stem.write_text(json.dumps(stored), encoding="utf-8")

    # The receipts directory moved, so the fingerprint no longer holds.
    broker._immutable_events_proved = {}
    broker._ledger_evidence_proved = None
    with pytest.raises(ValueError, match="integrity validation"):
        broker._validated_ledger()


def test_the_count_registration_takes_the_shared_ledger_lock_once(
    tmp_path: Path,
) -> None:
    """Three steps register a counted request, under one held lock.

    The shared ledger lock is shared with the benchmark evaluator, whose hold
    was a median of 2.35 seconds on 2026-09-17, so each acquisition cost the
    producer that wait. Three of them were about seven seconds of every paid
    call.
    """
    values = _concurrent(tmp_path)
    broker = values["broker"]
    opened: list[str] = []
    original = type(broker._lock_file).open

    def counted(self, *args, **kwargs):
        if self == broker._lock_file:
            opened.append("ledger")
        return original(self, *args, **kwargs)

    import arctic_qa.model_broker as module

    monkey = module.Path.open
    module.Path.open = counted  # type: ignore[method-assign]
    try:
        with broker._ledger_session():
            broker._open_count_retry(
                "a" * 64, {"phase": "live_test"}, phase="live_test"
            )
            broker._resume_not_submitted("a" * 64, {})
    finally:
        module.Path.open = monkey  # type: ignore[method-assign]

    assert opened.count("ledger") == 1


def test_a_session_reads_the_ledger_once(tmp_path: Path) -> None:
    values = _concurrent(tmp_path)
    broker = values["broker"]
    reads: list[int] = [0]
    original = broker._validated_ledger

    def counted():
        reads[0] += 1
        return original()

    broker._validated_ledger = counted  # type: ignore[method-assign]
    with broker._ledger_session():
        with broker._locked_ledger() as first:
            pass
        with broker._locked_ledger() as second:
            pass

    assert reads[0] == 1
    assert first is second


def test_a_session_is_reentrant_and_does_not_deadlock(tmp_path: Path) -> None:
    values = _concurrent(tmp_path)
    broker = values["broker"]

    with broker._ledger_session():
        with broker._ledger_session():
            with broker._locked_ledger() as ledger:
                assert "requests" in ledger

    # The lock is free again outside the session.
    with broker._locked_ledger() as ledger:
        assert "requests" in ledger
