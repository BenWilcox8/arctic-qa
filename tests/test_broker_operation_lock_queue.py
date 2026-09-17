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
