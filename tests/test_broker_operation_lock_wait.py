"""The bounded wait for the exclusive broker operation lock.

At 18:26 UTC on 2026-09-16 the chapter 3 producer of run chapter3-7dc6485-r3
exited with ``another paid broker operation is active``. A release of another
task held the exclusive operation lock of the shared ledger for a reviewed
operation, the producer's request met that lock, and the broker refused it at
once. The refusal crossed the broker seam, which marks every broker refusal a
whole-run stop, so the per-candidate containment could not hold it.

These tests pin the repair. The broker's one ordinary request path waits a
bounded time for the lock. The bound raises ``BrokerOperationBusyError``, and
the producer contains it against one family like any other fault. Every
reviewed operation keeps its immediate refusal against every other reviewed
operation.
"""

from __future__ import annotations

import fcntl
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from arctic_qa import model_broker, streaming  # noqa: E402
from arctic_qa.broker_provider import broker_boundary  # noqa: E402
from arctic_qa.errors import BrokerOperationBusyError, is_run_stop  # noqa: E402
from arctic_qa.model_broker import (  # noqa: E402
    OPERATION_LOCK_BUSY_REASON,
    hold_operation_lock,
)
from arctic_qa.streaming import run_stream  # noqa: E402

from test_ch3_candidate_fault_containment import (  # noqa: E402
    _fault_on_the_first_family,
    _stream_arguments,
)
from test_model_broker import Transport, execute, fixture  # noqa: E402


# A lock hold shorter than a test is allowed to wait. It is real time, so it
# stays small; the bound under test is a hundred times larger.
HELD_SECONDS = 0.4


class Holder:
    """Hold the exclusive operation lock of one ledger from another thread.

    ``flock`` is held by an open file description, not by a thread, so a second
    handle of this process conflicts with the first exactly as another worker's
    handle does. The thread lets the waiting caller run while the lock is held.
    """

    def __init__(self, path: Path, *, seconds: float | None = None) -> None:
        self.path = path
        self.seconds = seconds
        self.taken = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with self.path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.taken.set()
            self.release.wait(timeout=30.0)
            fcntl.flock(handle, fcntl.LOCK_UN)

    def __enter__(self) -> Holder:
        self.thread.start()
        assert self.taken.wait(timeout=10.0)
        if self.seconds is not None:
            timer = threading.Timer(self.seconds, self.release.set)
            timer.daemon = True
            timer.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.release.set()
        self.thread.join(timeout=10.0)


# The helper: one lock, two policies.


def test_a_lock_released_inside_the_bound_lets_the_request_proceed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.json.operation.lock"
    started = time.monotonic()

    with Holder(path, seconds=HELD_SECONDS):
        handle = hold_operation_lock(
            path,
            wait_seconds=model_broker.OPERATION_LOCK_WAIT_SECONDS,
            busy_error=BrokerOperationBusyError,
        )

    waited = time.monotonic() - started
    assert waited >= HELD_SECONDS
    handle.close()


def test_a_lock_held_past_the_bound_raises_the_busy_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.json.operation.lock"
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_WAIT_INTERVAL_SECONDS", 0.05)

    with Holder(path):
        with pytest.raises(BrokerOperationBusyError) as raised:
            hold_operation_lock(
                path,
                wait_seconds=model_broker.OPERATION_LOCK_WAIT_SECONDS,
                busy_error=BrokerOperationBusyError,
            )

    assert str(raised.value) == OPERATION_LOCK_BUSY_REASON
    assert isinstance(raised.value, ValueError)


def test_the_wait_sleeps_to_its_bound_and_no_further(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait polls on the interval, and its last sleep ends at the bound."""
    path = tmp_path / "ledger.json.operation.lock"
    clock = [1_000.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(model_broker.time, "sleep", sleep)
    monkeypatch.setattr(model_broker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_WAIT_INTERVAL_SECONDS", 1.0)

    with Holder(path):
        with pytest.raises(BrokerOperationBusyError):
            hold_operation_lock(
                path, wait_seconds=2.5, busy_error=BrokerOperationBusyError
            )

    assert sleeps == [1.0, 1.0, 0.5]


def test_an_exclusive_operation_refuses_a_held_lock_at_once(tmp_path: Path) -> None:
    """Two reviewed operations of one ledger must never overlap."""
    path = tmp_path / "ledger.json.operation.lock"

    with Holder(path):
        started = time.monotonic()
        with pytest.raises(ValueError) as raised:
            hold_operation_lock(path)
        refused = time.monotonic() - started

    assert str(raised.value) == OPERATION_LOCK_BUSY_REASON
    assert not isinstance(raised.value, BrokerOperationBusyError)
    assert refused < 1.0


def test_a_free_lock_is_taken_and_released(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json.operation.lock"

    handle = hold_operation_lock(
        path, wait_seconds=30.0, busy_error=BrokerOperationBusyError
    )
    handle.close()

    second = hold_operation_lock(path)
    second.close()


def test_a_wait_the_caller_does_not_ask_for_keeps_the_plain_refusal(
    tmp_path: Path,
) -> None:
    """The wait and the error it raises travel together, never apart.

    A tuned-down bound must not turn a reviewed operation's refusal into a
    contained fault, and it must not turn an ordinary request's bound into a
    run stop.
    """
    path = tmp_path / "ledger.json.operation.lock"

    with Holder(path):
        with pytest.raises(BrokerOperationBusyError):
            hold_operation_lock(
                path, wait_seconds=0.0, busy_error=BrokerOperationBusyError
            )
        with pytest.raises(ValueError) as raised:
            hold_operation_lock(path, wait_seconds=0.2)

    assert not isinstance(raised.value, BrokerOperationBusyError)


# The broker's ordinary request path.


def test_a_request_waits_for_a_reviewed_operation_and_then_runs(
    tmp_path: Path,
) -> None:
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    broker = values["broker"]

    with Holder(broker._operation_lock_file, seconds=HELD_SECONDS):
        receipt = execute(broker)

    assert receipt["state"] == "completed"
    assert transport.methods == ["countTokens", "generateContent"]


def test_a_request_past_the_bound_reserves_nothing_and_submits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    broker = values["broker"]
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(model_broker, "OPERATION_LOCK_WAIT_INTERVAL_SECONDS", 0.05)

    with Holder(broker._operation_lock_file):
        with pytest.raises(BrokerOperationBusyError, match=OPERATION_LOCK_BUSY_REASON):
            execute(broker)

    assert transport.methods == []
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["halted"] is False
    assert ledger["inflight"] == 0
    assert ledger["requests"] == {}
    # The lock is free again, so the next request of the run runs as usual.
    monkeypatch.undo()
    assert execute(broker)["state"] == "completed"


def test_a_reviewed_operation_of_the_broker_keeps_its_immediate_refusal(
    tmp_path: Path,
) -> None:
    values = fixture(tmp_path, transport=Transport())
    broker = values["broker"]
    key = "a" * 64

    with Holder(broker._operation_lock_file):
        started = time.monotonic()
        with pytest.raises(ValueError) as raised:
            broker.reconcile_omitted_thought_usage(key)
        refused = time.monotonic() - started

    assert str(raised.value) == OPERATION_LOCK_BUSY_REASON
    assert not isinstance(raised.value, BrokerOperationBusyError)
    assert refused < 1.0


# The producer contains the bound-exceeded case.


def test_the_broker_seam_leaves_the_busy_error_alone() -> None:
    with pytest.raises(BrokerOperationBusyError) as raised:
        with broker_boundary():
            raise BrokerOperationBusyError(OPERATION_LOCK_BUSY_REASON)

    assert is_run_stop(raised.value) is False
    assert streaming._ends_the_run(raised.value) is False


def test_the_immediate_refusal_of_a_reviewed_operation_still_ends_the_run() -> None:
    """Only the bounded wait is contained; the old refusal keeps its meaning."""
    with pytest.raises(ValueError) as raised:
        with broker_boundary():
            raise ValueError(OPERATION_LOCK_BUSY_REASON)

    assert is_run_stop(raised.value) is True
    assert streaming._ends_the_run(raised.value) is True


def test_the_bound_exceeded_case_is_contained_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 18:26 UTC exit, replayed over two papers."""
    arguments: dict[str, Any] = _stream_arguments(tmp_path, "broker-operation-busy")
    _fault_on_the_first_family(
        monkeypatch, BrokerOperationBusyError(OPERATION_LOCK_BUSY_REASON)
    )

    result = run_stream(**arguments)

    assert result["state"] == "completed"
    assert result["counts"]["processed"] == 2
    assert result["counts"]["candidate_processing_fault"] == 1
    first, second = result["paper_results"]
    assert first["disposition"] == "candidate_processing_fault"
    fault = first["candidate_processing_fault"]
    assert fault["error_class"] == "BrokerOperationBusyError"
    assert fault["error_message"] == OPERATION_LOCK_BUSY_REASON
    assert second["disposition"] != "candidate_processing_fault"


def test_a_contained_bound_exceeded_case_leaves_a_routing_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments: dict[str, Any] = _stream_arguments(tmp_path, "broker-operation-busy-row")
    _fault_on_the_first_family(
        monkeypatch, BrokerOperationBusyError(OPERATION_LOCK_BUSY_REASON)
    )

    run_stream(**arguments)

    rows = arguments["db"].rows(
        """SELECT reason_code,detail_json FROM rejection_ledger
        WHERE stage='generation_routing' AND reason_code=?""",
        (streaming.CANDIDATE_PROCESSING_FAULT_REASON_CODE,),
    )
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail_json"])
    assert detail["error_class"] == "BrokerOperationBusyError"
    assert detail["error_message"] == OPERATION_LOCK_BUSY_REASON
