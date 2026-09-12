from __future__ import annotations

import fcntl
import json
import re
import stat
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .gemini_eligibility import GeminiTransport, _config, _cost
from .util import atomic_json, canonical_json, sha256_bytes, sha256_file


STAGES = {
    "eligibility",
    "finding_answer_extraction",
    "question_generation",
    "blinded_reconstruction",
    "answer_verification",
    "distractor_generation",
    "option_verification",
    "repair",
}
PHASES = {"live_test", "away_production"}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _money(value: Any, name: str, *, positive: bool = False) -> Decimal:
    from .gemini_eligibility import _decimal

    return _decimal(value, name, positive=positive)


def _validate_policy(path: Path) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema") != "streaming-dataset-budget-policy-v1":
        raise ValueError("unsupported streaming budget policy schema")
    exact_money = {
        "project_lifetime_ceiling_usd": Decimal("1000"),
        "reserved_for_benchmark_evaluation_usd": Decimal("500"),
        "dataset_construction_allocation_usd": Decimal("500"),
        "construction_review_checkpoint_usd": Decimal("250"),
        "away_session_total_ceiling_usd": Decimal("25"),
        "live_test_suballocation_usd": Decimal("10"),
        "maximum_request_reserved_cost_usd": Decimal("0.25"),
        "maximum_paper_cost_usd": Decimal("1"),
    }
    for field, expected in exact_money.items():
        if _money(value.get(field), field, positive=True) != expected:
            raise ValueError(f"streaming budget value changed: {field}")
    exact_int = {
        "accepted_question_target": 500,
        "live_test_maximum_papers": 20,
        "live_test_maximum_generation_submissions": 100,
        "away_maximum_generation_submissions": 5000,
        "maximum_concurrent_generation_requests": 2,
        "maximum_generation_requests_per_minute": 10,
        "maximum_output_tokens_including_thinking": 8192,
        "automatic_transport_generation_retries": 0,
    }
    for field, expected in exact_int.items():
        if value.get(field) != expected:
            raise ValueError(f"streaming budget value changed: {field}")
    for field, expected in {
        "live_test_included_in_away_ceiling": True,
        "automatic_model_fallback": False,
        "automatic_budget_rearm": False,
        "stop_on_first_infrastructure_error_or_ambiguous_charge": True,
    }.items():
        if value.get(field) is not expected:
            raise ValueError(f"streaming budget control changed: {field}")
    if _money(value["dataset_construction_allocation_usd"], "construction") + _money(
        value["reserved_for_benchmark_evaluation_usd"], "evaluation"
    ) != _money(value["project_lifetime_ceiling_usd"], "lifetime"):
        raise ValueError("construction and evaluation allocations do not balance")
    return value


def _validate_gate(path: Path, phase: str) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema") != "streaming-live-execution-gate-v1":
        raise ValueError("unsupported streaming execution gate schema")
    if value.get("live_generation_enabled") is not True:
        raise ValueError("streaming live generation is disabled")
    if value.get("allowed_phase") != phase:
        raise ValueError("the streaming execution gate does not allow this phase")
    if value.get("independent_review_verdict") != "pass":
        raise ValueError("the integrated offline review did not pass")
    for field in ("integrated_code_commit", "review_record"):
        if not str(value.get(field) or "").strip():
            raise ValueError(f"the streaming execution gate lacks {field}")
    return value


def _credential_status(path: Path) -> str:
    if not path.is_file():
        return "not_set"
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        return "unsafe_permissions"
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        return "unsafe_permissions"
    return "private_file"


def _load_key(path: Path) -> str:
    if _credential_status(path) != "private_file":
        raise ValueError("the Gemini credential is absent or not private")
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("the Gemini credential file must contain one nonempty line")
    return value


def _validate_payload(payload: dict[str, Any], config: dict[str, Any]) -> None:
    if set(payload) != {"systemInstruction", "contents", "generationConfig", "store"}:
        raise ValueError("the broker request has unsupported top-level fields")
    if payload.get("store") is not False:
        raise ValueError("the broker request must disable provider storage")
    generation = payload.get("generationConfig") or {}
    allowed_generation = {
        "candidateCount",
        "temperature",
        "responseMimeType",
        "responseJsonSchema",
        "maxOutputTokens",
        "thinkingConfig",
    }
    if not isinstance(generation, dict) or not set(generation) <= allowed_generation:
        raise ValueError("the broker generation config contains a prohibited feature")
    if generation.get("candidateCount") != 1:
        raise ValueError("the broker request must ask for one candidate")
    output = generation.get("maxOutputTokens")
    if isinstance(output, bool) or not isinstance(output, int) or output < 1:
        raise ValueError("the broker output-token limit is invalid")
    if output > int(config["maximum_output_tokens"]):
        raise ValueError("the broker output-token limit exceeds the price config")
    for instruction in (payload.get("systemInstruction"), *payload.get("contents", [])):
        if not isinstance(instruction, dict):
            raise ValueError("the broker request content is invalid")
        parts = instruction.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError("the broker request content lacks text parts")
        if any(not isinstance(part, dict) or set(part) != {"text"} for part in parts):
            raise ValueError("the broker request contains a non-text part")


def broker_request_key(
    *,
    model: str,
    run_id: str,
    stage: str,
    paper_id: str,
    family_id: str,
    payload: dict[str, Any],
) -> str:
    """Bind one request key to its model, pipeline identity, and exact payload."""
    return sha256_bytes(
        canonical_json(
            {
                "model": model,
                "stage": stage,
                "paper_id": paper_id,
                "family_id": family_id,
                "payload": payload,
            }
        ).encode()
    )


class SharedGeminiBroker:
    """Meter every dataset-generation stage through one fail-closed ledger."""

    def __init__(
        self,
        *,
        policy_file: Path,
        price_config_file: Path,
        execution_gate_file: Path,
        ledger_file: Path,
        receipts_dir: Path,
        credential_file: Path,
        prior_construction_spend_usd: Decimal,
        transport: Any | None = None,
    ) -> None:
        self.policy_file = policy_file.resolve()
        self.price_config_file = price_config_file.resolve()
        self.execution_gate_file = execution_gate_file.resolve()
        self.ledger_file = ledger_file.resolve()
        self.receipts_dir = receipts_dir.resolve()
        self.credential_file = credential_file.resolve()
        self.policy = _validate_policy(self.policy_file)
        self.config = _config(self.price_config_file)
        self.prior = _money(prior_construction_spend_usd, "prior construction spend")
        self.transport = transport
        self._initialize()

    @property
    def _lock_file(self) -> Path:
        return self.ledger_file.with_name(f".{self.ledger_file.name}.lock")

    @property
    def _operation_lock_file(self) -> Path:
        return self.ledger_file.with_name(f".{self.ledger_file.name}.operation.lock")

    @property
    def _identity_file(self) -> Path:
        return self.ledger_file.with_name(f".{self.ledger_file.name}.identity.json")

    def _ledger_identity(self) -> dict[str, Any]:
        return {
            "schema": "shared-paid-call-ledger-identity-v1",
            "ledger_file": str(self.ledger_file),
            "policy_sha256": sha256_file(self.policy_file),
            "price_config_sha256": sha256_file(self.price_config_file),
            "prior_construction_spend_usd": str(self.prior),
        }

    def _initialize(self) -> None:
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.ledger_file.is_file():
                if not self._identity_file.is_file():
                    raise ValueError(
                        "the shared paid-call ledger identity record is absent"
                    )
                if _read(self._identity_file) != self._ledger_identity():
                    raise ValueError(
                        "the shared paid-call ledger identity record changed"
                    )
                ledger = _read(self.ledger_file)
                if (
                    ledger.get("schema") != "shared-paid-call-ledger-v1"
                    or ledger.get("policy_sha256") != sha256_file(self.policy_file)
                    or ledger.get("price_config_sha256")
                    != sha256_file(self.price_config_file)
                    or _money(ledger.get("prior_construction_spend_usd"), "prior")
                    != self.prior
                ):
                    raise ValueError("the shared paid-call ledger identity changed")
                return
            if self._identity_file.exists():
                raise ValueError(
                    "the shared paid-call ledger is absent after initialization"
                )
            if self.prior > _money(
                self.policy["construction_review_checkpoint_usd"], "checkpoint"
            ):
                raise ValueError(
                    "prior construction spend exceeds the review checkpoint"
                )
            ledger = {
                "schema": "shared-paid-call-ledger-v1",
                "policy_sha256": sha256_file(self.policy_file),
                "price_config_sha256": sha256_file(self.price_config_file),
                "prior_construction_spend_usd": str(self.prior),
                "reserved_usd": "0",
                "spent_usd": "0",
                "ambiguous_reserved_usd": "0",
                "generation_submissions": 0,
                "count_requests": 0,
                "inflight": 0,
                "accepted_question_count": 0,
                "accepted_families": {},
                "live_test_papers": {},
                "recent_submission_times_utc": [],
                "requests": {},
                "stages": {},
                "papers": {},
                "halted": False,
                "halt_reason": None,
                "created_at_utc": _now(),
                "updated_at_utc": _now(),
            }
            atomic_json(self._identity_file, self._ledger_identity(), immutable=True)
            atomic_json(self.ledger_file, ledger)

    def doctor(self) -> dict[str, Any]:
        gate = _read(self.execution_gate_file)
        return {
            "schema": "shared-gemini-broker-doctor-v1",
            "credential_source": _credential_status(self.credential_file),
            "live_generation_enabled": gate.get("live_generation_enabled") is True,
            "independent_review_verdict": gate.get("independent_review_verdict"),
            "model": self.config["model"],
            "ledger": self.status(),
            "live_call_made": False,
        }

    def status(self) -> dict[str, Any]:
        ledger = _read(self.ledger_file)
        away_used = sum(
            _money(ledger[name], name)
            for name in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")
        )
        live_used = sum(
            _money(row.get("reserved_usd", 0), "live reserved")
            + _money(row.get("spent_usd", 0), "live spent")
            + _money(row.get("ambiguous_usd", 0), "live ambiguous")
            for row in ledger["live_test_papers"].values()
        )
        accepted = int(ledger["accepted_question_count"])
        return {
            "schema": "shared-gemini-broker-status-v1",
            "updated_at_utc": ledger["updated_at_utc"],
            "spent_usd": ledger["spent_usd"],
            "reserved_usd": ledger["reserved_usd"],
            "ambiguous_reserved_usd": ledger["ambiguous_reserved_usd"],
            "away_remaining_usd": str(
                _money(self.policy["away_session_total_ceiling_usd"], "away")
                - away_used
            ),
            "live_test_remaining_usd": str(
                _money(self.policy["live_test_suballocation_usd"], "live test")
                - live_used
            ),
            "construction_checkpoint_remaining_usd": str(
                _money(self.policy["construction_review_checkpoint_usd"], "checkpoint")
                - self.prior
                - away_used
            ),
            "cost_per_accepted_question_usd": (
                str(_money(ledger["spent_usd"], "spent") / accepted)
                if accepted
                else None
            ),
            "generation_submissions": ledger["generation_submissions"],
            "count_requests": ledger["count_requests"],
            "inflight": ledger["inflight"],
            "accepted_question_count": ledger["accepted_question_count"],
            "halted": ledger["halted"],
            "halt_reason": ledger["halt_reason"],
            "stages": ledger["stages"],
            "papers": ledger["papers"],
        }

    def record_accepted(self, *, family_id: str, item_id: str) -> dict[str, Any]:
        if not family_id or not item_id:
            raise ValueError("accepted item identity is missing")
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = _read(self.ledger_file)
            old = ledger["accepted_families"].get(family_id)
            if old and old != item_id:
                raise ValueError("a paper family already has an accepted item")
            if not old and len(ledger["accepted_families"]) >= int(
                self.policy["accepted_question_target"]
            ):
                raise ValueError("the accepted-question target is complete")
            ledger["accepted_families"][family_id] = item_id
            ledger["accepted_question_count"] = len(ledger["accepted_families"])
            ledger["updated_at_utc"] = _now()
            atomic_json(self.ledger_file, ledger)
        return self.status()

    def _halt(self, reason: str) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = _read(self.ledger_file)
            ledger["halted"] = True
            ledger["halt_reason"] = reason
            ledger["updated_at_utc"] = _now()
            atomic_json(self.ledger_file, ledger)

    def _mark_not_submitted(self, request_key: str, state: str, reason: str) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = _read(self.ledger_file)
            request = ledger["requests"][request_key]
            if request.get("state") != "counting":
                raise ValueError("the paid request is not in its counting state")
            request["state"] = state
            request["reason"] = reason
            request["completed_at_utc"] = _now()
            ledger["updated_at_utc"] = _now()
            atomic_json(self.ledger_file, ledger)

    def _count_event(self, request_key: str, base: dict[str, Any]) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = _read(self.ledger_file)
            if ledger["halted"]:
                raise ValueError(
                    f"the paid-call broker is halted: {ledger['halt_reason']}"
                )
            if request_key in ledger["requests"]:
                raise ValueError("the paid request key already exists")
            ledger["count_requests"] += 1
            ledger["requests"][request_key] = {**base, "state": "counting"}
            ledger["updated_at_utc"] = _now()
            atomic_json(self.ledger_file, ledger)

    def _pace(self) -> None:
        """Wait until the frozen per-minute submission window has room."""
        while True:
            ledger = _read(self.ledger_file)
            cutoff = datetime.now(UTC) - timedelta(minutes=1)
            recent = sorted(
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                for value in ledger["recent_submission_times_utc"]
                if datetime.fromisoformat(value.replace("Z", "+00:00")) > cutoff
            )
            limit = int(self.policy["maximum_generation_requests_per_minute"])
            if len(recent) < limit:
                return
            wait_seconds = max(
                (recent[0] + timedelta(minutes=1) - datetime.now(UTC)).total_seconds(),
                0,
            )
            if wait_seconds:
                time.sleep(min(wait_seconds + 0.01, 60.0))

    def _reserve(
        self,
        *,
        request_key: str,
        phase: str,
        paper_id: str,
        stage: str,
        reserved: Decimal,
    ) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = _read(self.ledger_file)
            request = ledger["requests"].get(request_key)
            if not request or request.get("state") != "counting":
                raise ValueError("the paid request is not ready for reservation")
            if ledger["halted"] or _money(
                ledger["ambiguous_reserved_usd"], "ambiguous"
            ):
                raise ValueError("the paid-call broker is halted")
            if reserved > _money(
                self.policy["maximum_request_reserved_cost_usd"], "request"
            ):
                raise ValueError("the paid request exceeds USD 0.25")
            used = sum(
                _money(ledger[name], name)
                for name in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")
            )
            if used + reserved > _money(
                self.policy["away_session_total_ceiling_usd"], "away"
            ):
                raise ValueError("the paid request exceeds the USD 25 away cap")
            construction_used = self.prior + used
            if construction_used + reserved > _money(
                self.policy["construction_review_checkpoint_usd"], "checkpoint"
            ):
                raise ValueError("the paid request exceeds the construction checkpoint")
            if ledger["generation_submissions"] >= int(
                self.policy["away_maximum_generation_submissions"]
            ):
                raise ValueError("the away-session submission limit is complete")
            if ledger["accepted_question_count"] >= int(
                self.policy["accepted_question_target"]
            ):
                raise ValueError("the accepted-question target is complete")
            paper = ledger["papers"].setdefault(
                paper_id,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "reserved_usd": "0",
                    "spent_usd": "0",
                    "ambiguous_usd": "0",
                },
            )
            paper_used = sum(
                _money(paper[name], name)
                for name in ("reserved_usd", "spent_usd", "ambiguous_usd")
            )
            if paper_used + reserved > _money(
                self.policy["maximum_paper_cost_usd"], "paper"
            ):
                raise ValueError("the paid request exceeds the paper cost limit")
            if phase == "live_test":
                live_used = sum(
                    _money(row.get("reserved_usd", 0), "live reserved")
                    + _money(row.get("spent_usd", 0), "live spent")
                    + _money(row.get("ambiguous_usd", 0), "live ambiguous")
                    for row in ledger["live_test_papers"].values()
                )
                if live_used + reserved > _money(
                    self.policy["live_test_suballocation_usd"], "live test"
                ):
                    raise ValueError(
                        "the paid request exceeds the USD 10 live-test cap"
                    )
                if paper_id not in ledger["live_test_papers"] and len(
                    ledger["live_test_papers"]
                ) >= int(self.policy["live_test_maximum_papers"]):
                    raise ValueError("the live test reached its paper limit")
                live_submissions = sum(
                    int(row.get("submissions", 0))
                    for row in ledger["live_test_papers"].values()
                )
                if live_submissions >= int(
                    self.policy["live_test_maximum_generation_submissions"]
                ):
                    raise ValueError("the live-test submission limit is complete")
            if ledger["inflight"] >= int(
                self.policy["maximum_concurrent_generation_requests"]
            ):
                raise ValueError("the paid-call concurrency limit is complete")
            cutoff = datetime.now(UTC) - timedelta(minutes=1)
            recent = [
                value
                for value in ledger["recent_submission_times_utc"]
                if datetime.fromisoformat(value.replace("Z", "+00:00")) > cutoff
            ]
            if len(recent) >= int(
                self.policy["maximum_generation_requests_per_minute"]
            ):
                raise ValueError("the paid-call minute limit is complete")
            ledger["recent_submission_times_utc"] = recent + [_now()]
            ledger["reserved_usd"] = str(
                _money(ledger["reserved_usd"], "reserved") + reserved
            )
            ledger["generation_submissions"] += 1
            ledger["inflight"] += 1
            paper["reserved_usd"] = str(
                _money(paper["reserved_usd"], "paper reserved") + reserved
            )
            stage_row = ledger["stages"].setdefault(
                stage,
                {
                    "submissions": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "reserved_usd": "0",
                    "spent_usd": "0",
                    "ambiguous_usd": "0",
                },
            )
            stage_row["submissions"] += 1
            stage_row["reserved_usd"] = str(
                _money(stage_row["reserved_usd"], "stage reserved") + reserved
            )
            if phase == "live_test":
                live = ledger["live_test_papers"].setdefault(
                    paper_id,
                    {
                        "submissions": 0,
                        "reserved_usd": "0",
                        "spent_usd": "0",
                        "ambiguous_usd": "0",
                    },
                )
                live["submissions"] += 1
                live["reserved_usd"] = str(
                    _money(live["reserved_usd"], "live reserved") + reserved
                )
            request.update(
                {
                    "state": "submitted",
                    "phase": phase,
                    "reserved_usd": str(reserved),
                    "submitted_at_utc": _now(),
                }
            )
            ledger["updated_at_utc"] = _now()
            atomic_json(self.ledger_file, ledger)

    def _settle(
        self,
        request_key: str,
        *,
        actual: Decimal | None,
        usage: dict[str, int] | None,
    ) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = _read(self.ledger_file)
            request = ledger["requests"][request_key]
            if request.get("state") != "submitted":
                raise ValueError("the paid request is not submitted")
            reserved = _money(request["reserved_usd"], "reservation", positive=True)
            paper = ledger["papers"][request["paper_id"]]
            stage = ledger["stages"][request["stage"]]
            ledger["reserved_usd"] = str(
                _money(ledger["reserved_usd"], "reserved") - reserved
            )
            paper["reserved_usd"] = str(
                _money(paper["reserved_usd"], "paper reserved") - reserved
            )
            stage["reserved_usd"] = str(
                _money(stage["reserved_usd"], "stage reserved") - reserved
            )
            live = (
                ledger["live_test_papers"].get(request["paper_id"])
                if request["phase"] == "live_test"
                else None
            )
            if live:
                live["reserved_usd"] = str(
                    _money(live["reserved_usd"], "live reserved") - reserved
                )
            if actual is None:
                amount = reserved
                ledger["ambiguous_reserved_usd"] = str(
                    _money(ledger["ambiguous_reserved_usd"], "ambiguous") + amount
                )
                paper["ambiguous_usd"] = str(
                    _money(paper["ambiguous_usd"], "paper ambiguous") + amount
                )
                stage["ambiguous_usd"] = str(
                    _money(stage["ambiguous_usd"], "stage ambiguous") + amount
                )
                if live:
                    live["ambiguous_usd"] = str(
                        _money(live["ambiguous_usd"], "live ambiguous") + amount
                    )
                request["state"] = "ambiguous_charge"
                ledger["halted"] = True
                ledger["halt_reason"] = "ambiguous_generation_charge"
            else:
                actual = _money(actual, "actual cost")
                if actual > reserved:
                    raise ValueError("actual cost exceeds the paid request reservation")
                ledger["spent_usd"] = str(_money(ledger["spent_usd"], "spent") + actual)
                paper["spent_usd"] = str(
                    _money(paper["spent_usd"], "paper spent") + actual
                )
                stage["spent_usd"] = str(
                    _money(stage["spent_usd"], "stage spent") + actual
                )
                if live:
                    live["spent_usd"] = str(
                        _money(live["spent_usd"], "live spent") + actual
                    )
                if usage:
                    stage["input_tokens"] += usage["promptTokenCount"]
                    stage["output_tokens"] += usage["candidatesTokenCount"]
                    stage["thinking_tokens"] += usage["thoughtsTokenCount"]
                    paper["input_tokens"] += usage["promptTokenCount"]
                    paper["output_tokens"] += usage["candidatesTokenCount"]
                    paper["thinking_tokens"] += usage["thoughtsTokenCount"]
                request["state"] = "completed"
                request["actual_cost_usd"] = str(actual)
                request["usage"] = usage
            ledger["inflight"] = max(int(ledger["inflight"]) - 1, 0)
            request["completed_at_utc"] = _now()
            ledger["updated_at_utc"] = _now()
            atomic_json(self.ledger_file, ledger)

    def execute(
        self,
        *,
        phase: str,
        run_id: str,
        stage: str,
        paper_id: str,
        family_id: str,
        request_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if phase not in PHASES or stage not in STAGES:
            raise ValueError("the paid request phase or stage is unsupported")
        if not all((run_id, paper_id, family_id, request_key)):
            raise ValueError("the paid request identity is incomplete")
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the paid request key must be a lowercase SHA-256 value")
        _validate_gate(self.execution_gate_file, phase)
        _validate_payload(payload, self.config)
        expected_key = broker_request_key(
            model=self.config["model"],
            run_id=run_id,
            stage=stage,
            paper_id=paper_id,
            family_id=family_id,
            payload=payload,
        )
        if request_key != expected_key:
            raise ValueError("the paid request key does not bind the exact request")
        request_hash = sha256_bytes(canonical_json(payload).encode())
        base = {
            "request_key": request_key,
            "request_sha256": request_hash,
            "run_id": run_id,
            "stage": stage,
            "paper_id": paper_id,
            "family_id": family_id,
            "model": self.config["model"],
            "gate_sha256": sha256_file(self.execution_gate_file),
        }
        operation = self._operation_lock_file.open("a+")
        try:
            fcntl.flock(operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            operation.close()
            raise ValueError("another paid broker operation is active") from error
        try:
            ledger = _read(self.ledger_file)
            orphan = next(
                (
                    key
                    for key, row in ledger["requests"].items()
                    if row.get("state") == "submitted"
                ),
                None,
            )
            if orphan:
                self._settle(orphan, actual=None, usage=None)
                raise ValueError(
                    "an interrupted paid request became an ambiguous charge"
                )
            self._pace()
            client = self.transport or GeminiTransport(
                self.config["api_base"], _load_key(self.credential_file)
            )
            self._count_event(request_key, base)
            try:
                counted = client.post(
                    self.config["model"],
                    "countTokens",
                    {
                        "generateContentRequest": {
                            "model": f"models/{self.config['model']}",
                            **payload,
                        }
                    },
                )
                exact_input = counted["totalTokens"]
                if (
                    isinstance(exact_input, bool)
                    or not isinstance(exact_input, int)
                    or exact_input < 0
                ):
                    raise ValueError("countTokens did not return a nonnegative integer")
            except Exception as error:
                self._mark_not_submitted(
                    request_key, "count_error", f"{type(error).__name__}: {error}"
                )
                self._halt(f"countTokens error: {type(error).__name__}")
                receipt = {
                    **base,
                    "state": "count_error",
                    "error": f"{type(error).__name__}: {error}",
                    "live_call_made": False,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{request_key}.json",
                    receipt,
                    immutable=True,
                )
                return receipt
            if exact_input > int(self.config["maximum_input_tokens"]):
                self._mark_not_submitted(
                    request_key,
                    "too_large_not_ready",
                    "counted request exceeds the model input limit",
                )
                self._halt("counted request exceeds the model input limit")
                receipt = {
                    **base,
                    "state": "too_large_not_ready",
                    "input_tokens": exact_input,
                    "live_call_made": False,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{request_key}.json",
                    receipt,
                    immutable=True,
                )
                return receipt
            output_limit = int(payload["generationConfig"]["maxOutputTokens"])
            reserved = _cost(self.config, exact_input, output_limit)
            try:
                self._reserve(
                    request_key=request_key,
                    phase=phase,
                    paper_id=paper_id,
                    stage=stage,
                    reserved=reserved,
                )
            except ValueError as error:
                self._mark_not_submitted(request_key, "not_submitted", str(error))
                receipt = {
                    **base,
                    "state": "not_submitted",
                    "input_tokens": exact_input,
                    "reason": str(error),
                    "live_call_made": False,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{request_key}.json",
                    receipt,
                    immutable=True,
                )
                return receipt
            submitted = {
                **base,
                "state": "submitted",
                "input_tokens": exact_input,
                "reserved_usd": str(reserved),
                "submitted_at_utc": _now(),
            }
            atomic_json(
                self.receipts_dir / f"{request_key}.submitted.json",
                submitted,
                immutable=True,
            )
            try:
                response = client.post(self.config["model"], "generateContent", payload)
            except urllib.error.HTTPError as error:
                self._settle(request_key, actual=None, usage=None)
                receipt = {
                    **submitted,
                    "state": "ambiguous_charge",
                    "error_class": "known_http_response_unknown_charge",
                    "http_status": error.code,
                    "retry_after": error.headers.get("Retry-After")
                    if error.headers
                    else None,
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{request_key}.json",
                    receipt,
                    immutable=True,
                )
                return receipt
            except Exception as error:
                self._settle(request_key, actual=None, usage=None)
                receipt = {
                    **submitted,
                    "state": "ambiguous_charge",
                    "error": f"{type(error).__name__}: provider outcome unknown",
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{request_key}.json",
                    receipt,
                    immutable=True,
                )
                return receipt
            usage = (
                response.get("usageMetadata") if isinstance(response, dict) else None
            )
            try:
                if not isinstance(usage, dict):
                    raise ValueError("provider usage is absent")
                names = (
                    "promptTokenCount",
                    "candidatesTokenCount",
                    "thoughtsTokenCount",
                    "totalTokenCount",
                )
                values = [usage[name] for name in names]
                if any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in values
                ) or values[3] != sum(values[:3]):
                    raise ValueError("provider usage is inconsistent")
                actual = _cost(self.config, values[0], values[1] + values[2])
                if actual > reserved:
                    raise ValueError("provider usage exceeds the reservation")
            except Exception as error:
                self._settle(request_key, actual=None, usage=None)
                receipt = {
                    **submitted,
                    "state": "ambiguous_charge",
                    "error": f"{type(error).__name__}: {error}",
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{request_key}.json",
                    receipt,
                    immutable=True,
                )
                return receipt
            self._settle(request_key, actual=actual, usage=usage)
            receipt = {
                **submitted,
                "state": "completed",
                "actual_cost_usd": str(actual),
                "usage": usage,
                "response": response,
                "live_call_made": True,
                "completed_at_utc": _now(),
            }
            atomic_json(
                self.receipts_dir / f"{request_key}.json",
                receipt,
                immutable=True,
            )
            return receipt
        finally:
            operation.close()
