"""Construction requests that run at the same time on one shared ledger.

The chapter 3 producer held the exclusive operation lock for the whole of
every paid call, so one paper's call blocked every other paper's. A broker
built with ``concurrent_construction`` releases the admission and that lock
before the live call and holds only the request's own in-flight lock, which is
the shape the evaluation phase already had.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from arctic_qa.model_broker import (  # noqa: E402
    SharedGeminiBroker,
    broker_request_key,
    hold_operation_lock,
)

from test_model_broker import Transport, payload, write_json  # noqa: E402


class _OverlapTransport(Transport):
    """Hold each generate call open long enough to prove the overlap."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def post(self, model: str, method: str, body: dict) -> dict:
        if method != "generateContent":
            return super().post(model, method, body)
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.2)
            response = super().post(model, method, body)
        finally:
            with self._lock:
                self.active -= 1
        for candidate in response.get("candidates", []):
            candidate["finishReason"] = "STOP"
        return response


def _broker(tmp_path: Path, transport, *, concurrent: bool) -> SharedGeminiBroker:
    gate = tmp_path / "gate.json"
    write_json(
        gate,
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": True,
            "allowed_phase": "live_test",
            "integrated_code_commit": "fixture-commit",
            "independent_review_verdict": "pass",
            "review_record": "fixture-review",
        },
    )
    credential = tmp_path / "private" / "gemini.key"
    credential.parent.mkdir(mode=0o700, exist_ok=True)
    credential.write_text("unused-test-key", encoding="utf-8")
    credential.chmod(0o600)
    return SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=gate,
        ledger_file=tmp_path / "shared-ledger.json",
        receipts_dir=tmp_path / "receipts",
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        concurrent_construction=concurrent,
    )


def _execute(broker: SharedGeminiBroker, paper: str) -> dict:
    body = payload()
    key = broker_request_key(
        model=str(broker.config_for_stage("eligibility")["model"]),
        run_id="run-1",
        phase="live_test",
        stage="eligibility",
        paper_id=paper,
        family_id=f"family-{paper}",
        source_version_id=f"source-{paper}",
        payload=body,
    )
    return broker.execute(
        phase="live_test",
        run_id="run-1",
        stage="eligibility",
        paper_id=paper,
        family_id=f"family-{paper}",
        source_version_id=f"source-{paper}",
        request_key=key,
        payload=body,
    )


def test_two_construction_calls_are_in_flight_at_once(tmp_path: Path) -> None:
    transport = _OverlapTransport()
    broker = _broker(tmp_path, transport, concurrent=True)
    papers = ["p1", "p2"]
    with ThreadPoolExecutor(max_workers=len(papers)) as pool:
        receipts = list(pool.map(lambda paper: _execute(broker, paper), papers))

    assert [receipt["state"] for receipt in receipts] == ["completed", "completed"]
    assert transport.peak == 2, transport.peak
    status = broker.status()
    assert status["halted"] is False
    assert status["integrity_valid"] is True
    assert status["generation_submissions"] == 2
    ledger = json.loads((tmp_path / "shared-ledger.json").read_text(encoding="utf-8"))
    assert ledger["inflight"] == 0
    assert Decimal(ledger["reserved_usd"]) == 0
    assert {row["state"] for row in ledger["requests"].values()} == {"completed"}


def test_a_sequential_construction_call_keeps_the_whole_call_locked(
    tmp_path: Path,
) -> None:
    transport = _OverlapTransport()
    broker = _broker(tmp_path, transport, concurrent=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda paper: _execute(broker, paper), ["p1", "p2"]))

    assert [receipt["state"] for receipt in receipts] == ["completed", "completed"]
    # The operation lock is exclusive for the whole call, so the two never
    # overlap. That is the behaviour a run without the flag keeps.
    assert transport.peak == 1, transport.peak


def test_a_reviewed_operation_still_excludes_a_concurrent_admission(
    tmp_path: Path,
) -> None:
    transport = _OverlapTransport()
    broker = _broker(tmp_path, transport, concurrent=True)
    operation_path = (tmp_path / "shared-ledger.json").with_name(
        ".shared-ledger.json.operation.lock"
    )
    held = hold_operation_lock(operation_path)
    started = threading.Event()
    result: list[object] = []

    def request() -> None:
        started.set()
        try:
            result.append(_execute(broker, "p1"))
        except Exception as error:  # noqa: BLE001 - the test reads the class
            result.append(error)

    worker = threading.Thread(target=request)
    worker.start()
    started.wait(5)
    # The reviewed operation holds the lock, so the request has not been
    # admitted and no call has been made.
    time.sleep(0.5)
    assert transport.methods == []
    held.close()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert isinstance(result[0], dict) and result[0]["state"] == "completed"
