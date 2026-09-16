"""The bounded retry of the free countTokens preflight.

At 21:11 UTC on 2026-09-16 the chapter 3 producer of run chapter3-7dc6485-r3
exited with ``the broker stopped with state count_error``. The provider
answered one ``countTokens`` call with HTTP 503, the broker wrote a count-error
receipt on the first failure and halted the whole shared ledger, and the
producer exited. ``countTokens`` is free: it reserves nothing, submits nothing
and charges nothing, so that failure could never have made the money uncertain.

These tests pin the repair. A transient count failure is retried in place. One
that outlives the retry bounds one paper family and halts nothing. A permanent
count failure keeps its halt. And the free call that charged nothing is never
recorded as an unknown charge.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from arctic_qa import model_broker, streaming  # noqa: E402
from arctic_qa.broker_provider import BrokerProvider, broker_boundary  # noqa: E402
from arctic_qa.db import Database  # noqa: E402
from arctic_qa.errors import (  # noqa: E402
    AmbiguousChargeError,
    CountUnavailableError,
    is_run_stop,
)
from arctic_qa.model_broker import (  # noqa: E402
    COUNT_RETRY_ATTEMPTS,
    count_error_is_transient,
    PERMANENT_COUNT_FAILURE,
    TRANSIENT_COUNT_FAILURE,
)
from arctic_qa.providers import call_provider  # noqa: E402
from arctic_qa.streaming import run_stream  # noqa: E402

from test_broker_provider import SOURCE_VERSION, broker_fixture  # noqa: E402
from test_ch3_candidate_fault_containment import (  # noqa: E402
    _fault_on_the_first_family,
    _stream_arguments,
)


COUNT_ERROR = "HTTPError: HTTP Error 503: Service Unavailable"


@pytest.fixture(autouse=True)
def instant_count_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the bounded backoff, without its two minutes of waiting."""
    monkeypatch.setattr(model_broker, "COUNT_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(model_broker, "COUNT_RETRY_MAXIMUM_SECONDS", 0.0)


class CountFailureTransport:
    """Answer ``countTokens`` with one HTTP status, always."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.methods: list[str] = []

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            raise urllib.error.HTTPError(
                "https://example.invalid", self.status, "refused", {}, None
            )
        raise AssertionError("a failed count must never generate")


def _parameters() -> dict[str, Any]:
    return {
        "temperature": 0,
        "max_tokens": 1000,
        "json_schema": {
            "type": "object",
            "required": ["question"],
            "properties": {"question": {"type": "string"}},
            "additionalProperties": False,
        },
    }


def _bound_provider(broker) -> BrokerProvider:
    return BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="count-retry-r1",
    ).bind(
        paper_id="paper-1",
        family_id="family-1",
        source_version_id=SOURCE_VERSION,
    )


# The broker seam.


def test_the_broker_seam_leaves_the_count_unavailable_error_alone() -> None:
    with pytest.raises(CountUnavailableError) as raised:
        with broker_boundary():
            raise CountUnavailableError(COUNT_ERROR, stage="eligibility")

    assert is_run_stop(raised.value) is False
    assert streaming._ends_the_run(raised.value) is False


def test_an_exhausted_transient_count_bounds_one_family_at_the_provider(
    tmp_path: Path,
) -> None:
    transport = CountFailureTransport(503)
    broker = broker_fixture(tmp_path, transport)
    provider = _bound_provider(broker)

    with pytest.raises(CountUnavailableError) as raised:
        provider.invoke(
            "question_writer", "System", "Prompt", _parameters(), timeout=30
        )

    assert raised.value.stage == "question_generation"
    assert transport.methods == ["countTokens"] * COUNT_RETRY_ATTEMPTS
    status = broker.status()
    assert status["halted"] is False
    assert status["generation_submissions"] == 0
    assert Decimal(status["spent_usd"]) == Decimal("0")
    receipt = json.loads(
        next((tmp_path / "receipts").glob("*.json")).read_text(encoding="utf-8")
    )
    assert receipt["state"] == "count_error"
    assert receipt["count_failure_class"] == TRANSIENT_COUNT_FAILURE
    assert receipt["live_call_made"] is False
    assert len(receipt["count_attempts"]) == COUNT_RETRY_ATTEMPTS


def test_a_permanent_count_failure_still_halts_the_ledger(tmp_path: Path) -> None:
    transport = CountFailureTransport(404)
    broker = broker_fixture(tmp_path, transport)
    provider = _bound_provider(broker)

    with pytest.raises(Exception) as raised:
        provider.invoke(
            "question_writer", "System", "Prompt", _parameters(), timeout=30
        )

    assert not isinstance(raised.value, CountUnavailableError)
    assert transport.methods == ["countTokens"]
    assert broker.status()["halted"] is True
    receipt = json.loads(
        next((tmp_path / "receipts").glob("*.json")).read_text(encoding="utf-8")
    )
    assert receipt["count_failure_class"] == PERMANENT_COUNT_FAILURE


def test_a_free_count_that_charged_nothing_is_never_an_unknown_charge(
    tmp_path: Path,
) -> None:
    """``providers`` turns an unnamed exception into an ambiguous charge.

    A ``countTokens`` failure charges nothing, so it must never reach that
    branch. The call journal records the attempt as failed, not ambiguous.
    """
    transport = CountFailureTransport(503)
    broker = broker_fixture(tmp_path, transport)
    provider = _bound_provider(broker)
    database = Database(tmp_path / "state.sqlite3")
    database.migrate(tmp_path / "backups")

    with pytest.raises(CountUnavailableError):
        call_provider(
            database,
            provider,
            run_id="count-retry-r1",
            entity_id="entity-1",
            role="question_writer",
            system="System",
            prompt="Prompt",
            prompt_version="v1",
            parameters=_parameters(),
            response_schema=_parameters()["json_schema"],
            reservation=Decimal("0"),
            timeout=30,
            retries=0,
            rate_limit_seconds=0,
        )

    rows = database.rows("SELECT status,error_code FROM calls", ())
    assert [row["status"] for row in rows] == ["failed"]
    assert rows[0]["error_code"] == "COUNT_TOKENS_UNAVAILABLE"
    assert not issubclass(CountUnavailableError, AmbiguousChargeError)


class RecoveringCountTransport:
    """Fail the count of the first request, then answer every count."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.methods: list[str] = []
        self.counts = 0

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            self.counts += 1
            if self.counts <= self.failures:
                raise urllib.error.HTTPError(
                    "https://example.invalid", 503, "Service Unavailable", {}, None
                )
            return {"totalTokens": 100}
        return {
            "responseId": "count-retry-response-1",
            "modelVersion": "gemini-3.8-flash",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [{"text": json.dumps({"question": "What changed?"})}]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 115,
            },
        }


def test_a_stored_transient_count_error_is_counted_again_not_replayed(
    tmp_path: Path,
) -> None:
    """The 22:27 UTC exit.

    The stored count-error receipt is not a result. The producer re-asks the
    same paper under the same request key on its next pass, and the broker
    counts it again instead of handing the old refusal back.
    """
    transport = RecoveringCountTransport(failures=COUNT_RETRY_ATTEMPTS)
    broker = broker_fixture(tmp_path, transport)
    provider = _bound_provider(broker)

    with pytest.raises(CountUnavailableError):
        provider.invoke(
            "question_writer", "System", "Prompt", _parameters(), timeout=30
        )

    key = next(
        path.stem
        for path in (tmp_path / "receipts").glob("*.json")
        if path.stem.count(".") == 0
    )
    assert transport.methods == ["countTokens"] * COUNT_RETRY_ATTEMPTS

    result = provider.invoke(
        "question_writer", "System", "Prompt", _parameters(), timeout=30
    )

    assert result.payload == {"question": "What changed?"}
    assert transport.methods[-2:] == ["countTokens", "generateContent"]
    assert (tmp_path / "receipts" / f"{key}.count-retry-1.json").is_file()
    status = broker.status()
    assert status["halted"] is False
    assert status["generation_submissions"] == 1


def test_a_count_error_receipt_without_a_class_is_read_back_and_fails_closed() -> None:
    """A receipt written before the bounded retry records no class."""
    transient = {
        "state": "count_error",
        "live_call_made": False,
        "error": "HTTPError: HTTP Error 503: Service Unavailable",
    }
    permanent = {
        "state": "count_error",
        "live_call_made": False,
        "error": "HTTPError: HTTP Error 404: Not Found",
    }
    unreadable = {
        "state": "count_error",
        "live_call_made": False,
        "error": "RuntimeError: something else",
    }
    assert count_error_is_transient(transient) is True
    assert count_error_is_transient(permanent) is False
    assert count_error_is_transient(unreadable) is False
    assert count_error_is_transient({**transient, "live_call_made": True}) is False
    assert count_error_is_transient({**transient, "state": "completed"}) is False


# The producer contains it.


def test_an_exhausted_transient_count_is_contained_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 21:11 UTC exit, replayed over two papers."""
    arguments: dict[str, Any] = _stream_arguments(tmp_path, "count-unavailable")
    _fault_on_the_first_family(
        monkeypatch, CountUnavailableError(COUNT_ERROR, stage="question_generation")
    )

    result = run_stream(**arguments)

    assert result["state"] == "completed"
    assert result["counts"]["processed"] == 2
    assert result["counts"]["candidate_processing_fault"] == 1
    first, second = result["paper_results"]
    assert first["disposition"] == "candidate_processing_fault"
    fault = first["candidate_processing_fault"]
    assert fault["error_class"] == "CountUnavailableError"
    assert fault["error_message"] == COUNT_ERROR
    assert second["disposition"] != "candidate_processing_fault"
