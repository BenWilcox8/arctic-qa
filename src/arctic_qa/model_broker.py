from __future__ import annotations

import fcntl
import json
import re
import stat
import time
import urllib.error
from collections.abc import Callable
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
CONFIG_TRANSITION_V1_FIELDS = {
    "schema",
    "ledger_file",
    "from_price_config_sha256",
    "to_price_config_sha256",
    "expected_ledger_sha256",
    "expected_identity_sha256",
    "execution_gate_sha256",
    "integrated_code_commit",
    "review_record",
    "review_record_sha256",
    "reason",
    "authorized_at_utc",
}
CONFIG_TRANSITION_V2_FIELDS = CONFIG_TRANSITION_V1_FIELDS | {
    "from_config_transition_sha256",
    "from_policy_file",
    "from_policy_sha256",
    "to_policy_sha256",
    "changed_policy_fields",
    "maximum_authorized_cumulative_tranche_usd",
}
POLICY_TRANSITION_CHANGES = (
    {"live_test_maximum_papers": {"from": 20, "to": 40}},
    {
        "live_test_maximum_papers": {"from": 40, "to": 41},
        "live_test_maximum_generation_submissions": {"from": 100, "to": 101},
    },
    {
        "live_test_maximum_papers": {"from": 41, "to": None},
        "live_test_maximum_generation_submissions": {"from": 101, "to": None},
    },
)
CEILING_EXTENSION_CHANGE: dict[str, Any] = {}
AUTHORIZED_CAP_REASON = "the paid request exceeds the authorized live-test cap"
PER_REQUEST_CAP_REASON = "the paid request exceeds USD 0.25"
ALLOWED_LIVE_TEST_LIMITS = {(20, 100), (40, 100), (41, 101), (None, None)}
STREAM_INPUT_BINDING_VERSION = "stream-input-binding-v1"
STREAM_INPUT_GATE_FIELDS = {
    "continuation_artifact",
    "continuation_access_run_id",
    "continuation_run_manifest_sha256",
    "continuation_run_receipt_sha256",
    "continuation_frozen_manifest_sha256",
    "continuation_order_sha256",
    "continuation_family_count",
    "authorized_new_run_id",
    "authorized_campaign_id",
    "eligibility_prompt_sha256",
    "eligibility_schema_sha256",
    "eligibility_policy_sha256",
}
USAGE_RECONCILIATION_FIELDS = {
    "schema",
    "request_key",
    "received_receipt_sha256",
    "ambiguous_receipt_sha256",
    "config_transition_sha256",
    "price_config_sha256",
    "normalized_usage",
    "actual_cost_usd",
    "ledger_sha256_before",
    "gate_sha256",
    "integrated_code_commit",
    "review_record",
    "review_record_sha256",
    "reconciled_at_utc",
}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _normalized_usage(response: Any) -> dict[str, Any]:
    usage = response.get("usageMetadata") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        raise ValueError("provider usage is absent")
    normalized = dict(usage)
    if "thoughtsTokenCount" not in normalized:
        prompt = normalized.get("promptTokenCount")
        candidates = normalized.get("candidatesTokenCount")
        total = normalized.get("totalTokenCount")
        values = (prompt, candidates, total)
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in values
            )
            or total != prompt + candidates
        ):
            raise ValueError("provider usage cannot prove zero thinking tokens")
        normalized["thoughtsTokenCount"] = 0
    names = (
        "promptTokenCount",
        "candidatesTokenCount",
        "thoughtsTokenCount",
        "totalTokenCount",
    )
    values = [normalized.get(name) for name in names]
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values
    ) or values[3] != sum(values[:3]):
        raise ValueError("provider usage is inconsistent")
    return normalized


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
        "away_maximum_generation_submissions": 5000,
        "maximum_concurrent_generation_requests": 2,
        "maximum_generation_requests_per_minute": 10,
        "maximum_output_tokens_including_thinking": 8192,
        "automatic_transport_generation_retries": 0,
    }
    for field, expected in exact_int.items():
        if value.get(field) != expected:
            raise ValueError(f"streaming budget value changed: {field}")
    live_test_limits = (
        value.get("live_test_maximum_papers"),
        value.get("live_test_maximum_generation_submissions"),
    )
    if live_test_limits not in ALLOWED_LIVE_TEST_LIMITS:
        raise ValueError("streaming budget live-test limits changed")
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
    source_version_id: str,
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
                "source_version_id": source_version_id,
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
        config_transition_file: Path | None = None,
    ) -> None:
        self.policy_file = policy_file.resolve()
        self.price_config_file = price_config_file.resolve()
        self.execution_gate_file = execution_gate_file.resolve()
        self.ledger_file = ledger_file.resolve()
        self.receipts_dir = receipts_dir.resolve()
        self.credential_file = credential_file.resolve()
        self.config_transition_file = (
            config_transition_file.resolve() if config_transition_file else None
        )
        self.policy = _validate_policy(self.policy_file)
        self.config = _config(self.price_config_file)
        self.active_price_config_sha256 = sha256_file(self.price_config_file)
        self.prior = _money(prior_construction_spend_usd, "prior construction spend")
        self.transport = transport
        self._config_transition_sha256: str | None = None
        self._config_transition_event_path: Path | None = None
        self._authorized_live_test_ceiling_usd: Decimal | None = None
        self._status_observer: Callable[[Path], None] | None = None
        self._stream_input_binding: dict[str, Any] | None = None
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

    @property
    def _status_file(self) -> Path:
        return self.ledger_file.with_name(f"{self.ledger_file.stem}.status.json")

    @property
    def _integrity_file(self) -> Path:
        return self.ledger_file.with_name(
            f".{self.ledger_file.name}.integrity-halt.json"
        )

    @staticmethod
    def _request_event_stem(request_key: str, request: dict[str, Any]) -> str:
        if request.get("resumed_from_not_submitted_sha256") is not None:
            return f"{request_key}.resume-{request['config_transition_sha256']}"
        return request_key

    def _ledger_identity(self) -> dict[str, Any]:
        return {
            "schema": "shared-paid-call-ledger-identity-v1",
            "ledger_file": str(self.ledger_file),
            "policy_sha256": sha256_file(self.policy_file),
            "price_config_sha256": self.active_price_config_sha256,
            "prior_construction_spend_usd": str(self.prior),
        }

    def _validate_initial_identity(
        self, identity: dict[str, Any], ledger: dict[str, Any]
    ) -> None:
        required = {
            "schema",
            "ledger_file",
            "policy_sha256",
            "price_config_sha256",
            "prior_construction_spend_usd",
        }
        if not isinstance(identity, dict) or set(identity) != required:
            raise ValueError("the shared paid-call ledger identity record changed")
        if (
            identity["schema"] != "shared-paid-call-ledger-identity-v1"
            or identity["ledger_file"] != str(self.ledger_file)
            or _money(identity["prior_construction_spend_usd"], "identity prior")
            != self.prior
        ):
            raise ValueError("the shared paid-call ledger identity record changed")
        if (
            ledger.get("schema") != "shared-paid-call-ledger-v1"
            or ledger.get("policy_sha256") != identity["policy_sha256"]
            or ledger.get("price_config_sha256") != identity["price_config_sha256"]
            or _money(ledger.get("prior_construction_spend_usd"), "prior") != self.prior
        ):
            raise ValueError("the shared paid-call ledger identity changed")

    @staticmethod
    def _transition_fields(authorization: dict[str, Any]) -> set[str]:
        if authorization.get("schema") == "shared-paid-call-config-transition-v1":
            return CONFIG_TRANSITION_V1_FIELDS
        if authorization.get("schema") == "shared-paid-call-config-transition-v2":
            return CONFIG_TRANSITION_V2_FIELDS
        return set()

    @staticmethod
    def _transition_policy_hashes(
        authorization: dict[str, Any], identity: dict[str, Any]
    ) -> tuple[str, str]:
        if authorization.get("schema") == "shared-paid-call-config-transition-v2":
            return (
                str(authorization.get("from_policy_sha256") or ""),
                str(authorization.get("to_policy_sha256") or ""),
            )
        initial = str(identity["policy_sha256"])
        return initial, initial

    def _transition_pairs(
        self, authorization: dict[str, Any], identity: dict[str, Any]
    ) -> tuple[tuple[str, str], tuple[str, str]]:
        from_policy, to_policy = self._transition_policy_hashes(authorization, identity)
        return (
            (str(authorization.get("from_price_config_sha256") or ""), from_policy),
            (str(authorization.get("to_price_config_sha256") or ""), to_policy),
        )

    def _apply_transition_controls(self, authorization: dict[str, Any]) -> None:
        if authorization.get("schema") == "shared-paid-call-config-transition-v2":
            self._authorized_live_test_ceiling_usd = _money(
                authorization["maximum_authorized_cumulative_tranche_usd"],
                "transition tranche",
                positive=True,
            )

    @staticmethod
    def _is_ceiling_extension(authorization: dict[str, Any]) -> bool:
        return (
            authorization.get("schema") == "shared-paid-call-config-transition-v2"
            and authorization.get("changed_policy_fields") == CEILING_EXTENSION_CHANGE
        )

    def _read_transition_event(self, path: Path) -> dict[str, Any]:
        event = _read(path)
        if not isinstance(event, dict) or set(event) != {
            "schema",
            "authorization",
            "transition_authorization_sha256",
            "applied_at_utc",
        }:
            raise ValueError("the applied price configuration transition changed")
        authorization = event["authorization"]
        expected_fields = (
            self._transition_fields(authorization)
            if isinstance(authorization, dict)
            else set()
        )
        if (
            event["schema"] != "shared-paid-call-config-transition-event-v1"
            or not isinstance(authorization, dict)
            or set(authorization) != expected_fields
        ):
            raise ValueError("the applied price configuration transition changed")
        authorization_hash = sha256_bytes(canonical_json(authorization).encode())
        if (
            event["transition_authorization_sha256"] != authorization_hash
            or path.name != f"config-transition-{authorization_hash}.json"
        ):
            raise ValueError("the applied price configuration transition changed")
        try:
            applied = datetime.fromisoformat(
                str(event["applied_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError(
                "the applied price configuration transition changed"
            ) from error
        if applied.tzinfo is None:
            raise ValueError("the applied price configuration transition changed")
        return event

    def _validate_transition_durable_bindings(
        self, authorization: dict[str, Any]
    ) -> None:
        gate_phase = _read(self.execution_gate_file).get("allowed_phase")
        if gate_phase not in PHASES:
            raise ValueError("the configuration transition gate phase is invalid")
        gate = _validate_gate(self.execution_gate_file, gate_phase)
        if authorization.get("changed_policy_fields") == POLICY_TRANSITION_CHANGES[
            -1
        ] or self._is_ceiling_extension(authorization):
            self._validate_stream_input_gate(gate)
        if (
            authorization["execution_gate_sha256"]
            != sha256_file(self.execution_gate_file)
            or authorization["integrated_code_commit"] != gate["integrated_code_commit"]
            or authorization["review_record"] != gate["review_record"]
            or authorization["review_record_sha256"] != gate.get("review_record_sha256")
        ):
            raise ValueError("the configuration transition review changed")
        review_path = Path(authorization["review_record"]).resolve()
        if not review_path.is_file() or authorization[
            "review_record_sha256"
        ] != sha256_file(review_path):
            raise ValueError("the configuration transition review is invalid")

    def _validate_transition_authorization(
        self,
        authorization: dict[str, Any],
        *,
        identity: dict[str, Any],
        ledger: dict[str, Any],
    ) -> None:
        expected_fields = (
            self._transition_fields(authorization)
            if isinstance(authorization, dict)
            else set()
        )
        if not expected_fields or set(authorization) != expected_fields:
            raise ValueError("the configuration transition fields changed")
        initial_pair = (
            identity["price_config_sha256"],
            identity["policy_sha256"],
        )
        active_pair = (
            self.active_price_config_sha256,
            sha256_file(self.policy_file),
        )
        from_pair, to_pair = self._transition_pairs(authorization, identity)
        if (
            authorization["ledger_file"] != str(self.ledger_file)
            or to_pair != active_pair
        ):
            raise ValueError("the configuration transition identity changed")
        if authorization["schema"] == "shared-paid-call-config-transition-v1":
            if from_pair != initial_pair:
                raise ValueError("the configuration transition identity changed")
        else:
            changed_policy_fields = authorization["changed_policy_fields"]
            ceiling_extension = self._is_ceiling_extension(authorization)
            if (
                not ceiling_extension
                and changed_policy_fields not in POLICY_TRANSITION_CHANGES
            ):
                raise ValueError("the policy transition change set changed")
            tranche = _money(
                authorization["maximum_authorized_cumulative_tranche_usd"],
                "transition tranche",
                positive=True,
            )
            if from_pair[0] != to_pair[0]:
                raise ValueError("the policy transition identity changed")
            source_policy = Path(authorization["from_policy_file"]).resolve()
            if (
                not source_policy.is_file()
                or sha256_file(source_policy) != from_pair[1]
            ):
                raise ValueError("the policy transition source changed")
            source_value = _read(source_policy)
            active_value = _read(self.policy_file)
            predecessor = authorization["from_config_transition_sha256"]
            if ceiling_extension:
                if (
                    from_pair != to_pair
                    or tranche != Decimal("10")
                    or active_value != source_value
                    or predecessor is None
                ):
                    raise ValueError("the policy transition identity changed")
                matching_predecessors = []
                for path in self.receipts_dir.glob("config-transition-*.json"):
                    if sha256_file(path) != predecessor:
                        continue
                    event = self._read_transition_event(path)
                    prior = event["authorization"]
                    if (
                        self._transition_pairs(prior, identity)[1] == from_pair
                        and prior.get("changed_policy_fields")
                        == POLICY_TRANSITION_CHANGES[-1]
                        and _money(
                            prior.get("maximum_authorized_cumulative_tranche_usd"),
                            "predecessor tranche",
                            positive=True,
                        )
                        == Decimal("5")
                    ):
                        matching_predecessors.append(path)
                if len(matching_predecessors) != 1:
                    raise ValueError("the policy transition predecessor changed")
            else:
                if from_pair[1] == to_pair[1] or tranche != Decimal("5"):
                    raise ValueError("the policy transition identity changed")
                expected_value = dict(source_value)
                for field, limits in changed_policy_fields.items():
                    if source_value.get(field) != limits["from"]:
                        raise ValueError("the policy transition source limit changed")
                    expected_value[field] = limits["to"]
                if active_value != expected_value:
                    raise ValueError(
                        "the policy transition changes more than one field"
                    )
                if from_pair == initial_pair:
                    if predecessor is not None:
                        raise ValueError("the policy transition predecessor changed")
                else:
                    matching_predecessors = []
                    for path in self.receipts_dir.glob("config-transition-*.json"):
                        event = self._read_transition_event(path)
                        prior = event["authorization"]
                        if self._transition_pairs(prior, identity)[1] == from_pair:
                            matching_predecessors.append(path)
                    if len(matching_predecessors) != 1 or predecessor != sha256_file(
                        matching_predecessors[0]
                    ):
                        raise ValueError("the policy transition predecessor changed")
        if authorization["expected_ledger_sha256"] != sha256_file(self.ledger_file):
            raise ValueError("the configuration transition ledger hash changed")
        if (
            ledger["halted"]
            or ledger["inflight"] != 0
            or _money(ledger["reserved_usd"], "reserved") != 0
            or _money(ledger["ambiguous_reserved_usd"], "ambiguous") != 0
        ):
            raise ValueError("a configuration transition requires a settled ledger")
        if authorization["expected_identity_sha256"] != sha256_file(
            self._identity_file
        ):
            raise ValueError("the configuration transition identity hash changed")
        self._validate_transition_durable_bindings(authorization)
        if not str(authorization["reason"]).strip():
            raise ValueError("the configuration transition reason is absent")
        try:
            authorized = datetime.fromisoformat(
                str(authorization["authorized_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("the configuration transition time is invalid") from error
        if authorized.tzinfo is None:
            raise ValueError("the configuration transition time is invalid")

    def _authorize_active_config(
        self, identity: dict[str, Any], ledger: dict[str, Any]
    ) -> None:
        initial_pair = (
            identity["price_config_sha256"],
            identity["policy_sha256"],
        )
        active_pair = (
            self.active_price_config_sha256,
            sha256_file(self.policy_file),
        )
        if active_pair == initial_pair:
            if self.config_transition_file is not None:
                raise ValueError("a configuration transition is not necessary")
            return
        requested_authorization = (
            _read(self.config_transition_file)
            if self.config_transition_file is not None
            else None
        )
        requested_authorization_hash = (
            sha256_bytes(canonical_json(requested_authorization).encode())
            if requested_authorization is not None
            else None
        )
        matching_events: list[tuple[Path, dict[str, Any]]] = []
        for path in self.receipts_dir.glob("config-transition-*.json"):
            event = self._read_transition_event(path)
            authorization = event["authorization"]
            if authorization.get("ledger_file") != str(self.ledger_file):
                raise ValueError("the applied price configuration transition changed")
            if self._transition_pairs(authorization, identity)[1] == active_pair:
                matching_events.append((path, event))
        selected_events = [
            (path, event)
            for path, event in matching_events
            if event["transition_authorization_sha256"] == requested_authorization_hash
        ]
        if requested_authorization is None:
            selected_events = matching_events
        if len(selected_events) > 1:
            raise ValueError("multiple applied price configuration transitions exist")
        if selected_events:
            event_path, event = selected_events[0]
            authorization = event["authorization"]
            authorization_hash = event["transition_authorization_sha256"]
            allowed_event_hashes = {sha256_file(event_path)}
            if self._is_ceiling_extension(authorization):
                allowed_event_hashes.add(authorization["from_config_transition_sha256"])
                matching_hashes = {sha256_file(path) for path, _ in matching_events}
                if matching_hashes != allowed_event_hashes:
                    raise ValueError(
                        "multiple applied price configuration transitions exist"
                    )
            elif len(matching_events) > 1:
                raise ValueError(
                    "multiple applied price configuration transitions exist"
                )
            active_requests = [
                request
                for request in ledger["requests"].values()
                if (
                    request.get("price_config_sha256", identity["price_config_sha256"]),
                    request.get("policy_sha256", identity["policy_sha256"]),
                )
                == active_pair
            ]
            if active_requests and any(
                request.get("config_transition_sha256") not in allowed_event_hashes
                for request in active_requests
            ):
                raise ValueError(
                    "a paid-call request lacks its authorized config transition"
                )
            if active_requests:
                self._validate_transition_durable_bindings(authorization)
            else:
                self._validate_transition_authorization(
                    authorization, identity=identity, ledger=ledger
                )
            event_hash = sha256_file(event_path)
            self._config_transition_sha256 = event_hash
            self._config_transition_event_path = event_path
            self._apply_transition_controls(authorization)
            return
        if matching_events and not self._is_ceiling_extension(
            requested_authorization or {}
        ):
            if self.config_transition_file is None:
                raise ValueError(
                    "multiple applied price configuration transitions exist"
                )
            raise ValueError("the price configuration transition file changed")
        if self.config_transition_file is None:
            raise ValueError("the active configuration requires a reviewed transition")
        authorization = requested_authorization
        assert authorization is not None
        self._validate_transition_authorization(
            authorization, identity=identity, ledger=ledger
        )
        authorization_hash = sha256_bytes(canonical_json(authorization).encode())
        event_path = self.receipts_dir / f"config-transition-{authorization_hash}.json"
        atomic_json(
            event_path,
            {
                "schema": "shared-paid-call-config-transition-event-v1",
                "authorization": authorization,
                "transition_authorization_sha256": authorization_hash,
                "applied_at_utc": _now(),
            },
            immutable=True,
        )
        self._config_transition_sha256 = sha256_file(event_path)
        self._config_transition_event_path = event_path
        self._apply_transition_controls(authorization)

    def _initialize(self) -> None:
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.ledger_file.is_file():
                if self._integrity_file.exists():
                    raise ValueError(
                        "the shared paid-call ledger has an integrity halt"
                    )
                if not self._identity_file.is_file():
                    raise ValueError(
                        "the shared paid-call ledger identity record is absent"
                    )
                identity = _read(self._identity_file)
                ledger = _read(self.ledger_file)
                self._validate_initial_identity(identity, ledger)
                ledger = self._validated_ledger()
                self._authorize_active_config(identity, ledger)
                self._publish_status(ledger)
                return
            if self._identity_file.exists():
                raise ValueError(
                    "the shared paid-call ledger is absent after initialization"
                )
            if self.policy["live_test_maximum_papers"] != 20:
                raise ValueError(
                    "an expanded policy requires an existing reviewed ledger"
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
                "price_config_sha256": self.active_price_config_sha256,
                "prior_construction_spend_usd": str(self.prior),
                "reserved_usd": "0",
                "spent_usd": "0",
                "ambiguous_reserved_usd": "0",
                "generation_submissions": 0,
                "count_requests": 0,
                "inflight": 0,
                "accepted_question_count": 0,
                "accepted_families": {},
                "family_bindings": {},
                "paper_bindings": {},
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
            self._commit_ledger(ledger)

    @staticmethod
    def _empty_usage_row() -> dict[str, Any]:
        return {
            "submissions": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "reserved_usd": "0",
            "spent_usd": "0",
            "ambiguous_usd": "0",
        }

    @staticmethod
    def _empty_live_row() -> dict[str, Any]:
        return {
            "submissions": 0,
            "reserved_usd": "0",
            "spent_usd": "0",
            "ambiguous_usd": "0",
        }

    def _record_integrity_halt(self, error: Exception) -> None:
        if self._integrity_file.exists():
            return
        atomic_json(
            self._integrity_file,
            {
                "schema": "shared-paid-call-ledger-integrity-halt-v1",
                "ledger_file": str(self.ledger_file),
                "ledger_sha256": sha256_file(self.ledger_file)
                if self.ledger_file.is_file()
                else None,
                "reason": f"{type(error).__name__}: {error}",
                "recorded_at_utc": _now(),
            },
            immutable=True,
        )

    def _publish_integrity_halt(self, error: Exception) -> None:
        try:
            previous = _read(self._status_file) if self._status_file.is_file() else {}
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {}
        status = {
            **previous,
            "schema": "shared-gemini-broker-status-v2",
            "ledger_file": str(self.ledger_file),
            "ledger_sha256": sha256_file(self.ledger_file)
            if self.ledger_file.is_file()
            else None,
            "policy_sha256": sha256_file(self.policy_file),
            "price_config_sha256": sha256_file(self.price_config_file),
            "updated_at_utc": _now(),
            "stages": previous.get("stages", {}),
            "papers": previous.get("papers", {}),
            "limits": previous.get("limits", {}),
            "usage": previous.get("usage", {}),
            "remaining": previous.get("remaining", {}),
            "halted": True,
            "halt_reason": f"{type(error).__name__}: {error}",
            "integrity_valid": False,
            "status_state": "integrity_halted",
        }
        atomic_json(self._status_file, status)
        if self._status_observer is not None:
            self._status_observer(self._status_file)

    def _validated_ledger(self) -> dict[str, Any]:
        if self._integrity_file.exists():
            raise ValueError("the shared paid-call ledger has an integrity halt")
        try:
            ledger = _read(self.ledger_file)
            self._validate_ledger(ledger)
            self._validate_immutable_events(ledger)
            self._validate_active_transition_event(ledger)
            return ledger
        except Exception as error:
            self._record_integrity_halt(error)
            self._publish_integrity_halt(error)
            raise ValueError(
                f"the shared paid-call ledger failed integrity validation: {error}"
            ) from error

    def _validate_active_transition_event(self, ledger: dict[str, Any]) -> None:
        path = self._config_transition_event_path
        if path is None:
            return
        if not path.is_file():
            raise ValueError("the applied price configuration transition is absent")
        if sha256_file(path) != self._config_transition_sha256:
            raise ValueError("the applied price configuration transition changed")
        event = self._read_transition_event(path)
        authorization = event["authorization"]
        identity = _read(self._identity_file)
        initial_pair = (
            identity["price_config_sha256"],
            identity["policy_sha256"],
        )
        active_pair = (
            self.active_price_config_sha256,
            sha256_file(self.policy_file),
        )
        matching_paths = []
        for candidate_path in self.receipts_dir.glob("config-transition-*.json"):
            candidate = self._read_transition_event(candidate_path)
            candidate_authorization = candidate["authorization"]
            if (
                candidate_authorization["ledger_file"] == str(self.ledger_file)
                and self._transition_pairs(candidate_authorization, identity)[1]
                == active_pair
            ):
                matching_paths.append(candidate_path)
        allowed_paths = {path}
        if self._is_ceiling_extension(authorization):
            predecessor = authorization["from_config_transition_sha256"]
            predecessor_paths = [
                candidate_path
                for candidate_path in matching_paths
                if sha256_file(candidate_path) == predecessor
            ]
            if len(predecessor_paths) != 1:
                raise ValueError("the policy transition predecessor changed")
            allowed_paths.add(predecessor_paths[0])
        if set(matching_paths) != allowed_paths:
            if len(matching_paths) > 1:
                raise ValueError(
                    "multiple applied price configuration transitions exist"
                )
            raise ValueError("the applied price configuration transition changed")
        active_requests = [
            request
            for request in ledger["requests"].values()
            if (
                request.get("price_config_sha256", initial_pair[0]),
                request.get("policy_sha256", initial_pair[1]),
            )
            == active_pair
        ]
        if active_requests:
            self._validate_transition_durable_bindings(authorization)
        else:
            self._validate_transition_authorization(
                authorization, identity=identity, ledger=ledger
            )
        if (
            authorization["ledger_file"] != str(self.ledger_file)
            or self._transition_pairs(authorization, identity)[1] != active_pair
            or authorization["expected_identity_sha256"]
            != sha256_file(self._identity_file)
        ):
            raise ValueError("the applied price configuration transition changed")

    def _validate_immutable_events(self, ledger: dict[str, Any]) -> None:
        identity = _read(self._identity_file)
        initial_pair = (
            ledger["price_config_sha256"],
            ledger["policy_sha256"],
        )
        allowed_pairs = {initial_pair}
        transition_events_by_pair: dict[tuple[str, str], set[str]] = {}
        transition_authorizations_by_hash: dict[str, dict[str, Any]] = {}
        pending_events: list[
            tuple[Path, dict[str, Any], tuple[str, str], tuple[str, str]]
        ] = []
        for path in self.receipts_dir.glob("config-transition-*.json"):
            event = self._read_transition_event(path)
            authorization = event["authorization"]
            from_pair, target_pair = self._transition_pairs(authorization, identity)
            if authorization["ledger_file"] != str(self.ledger_file) or authorization[
                "expected_identity_sha256"
            ] != sha256_file(self._identity_file):
                raise ValueError("the applied price configuration transition changed")
            pending_events.append((path, authorization, from_pair, target_pair))
        while pending_events:
            progressed = False
            for item in list(pending_events):
                path, authorization, from_pair, target_pair = item
                if from_pair not in allowed_pairs:
                    continue
                event_hash = sha256_file(path)
                existing_hashes = transition_events_by_pair.get(target_pair, set())
                if existing_hashes:
                    predecessor = authorization.get("from_config_transition_sha256")
                    prior = transition_authorizations_by_hash.get(str(predecessor))
                    if (
                        not self._is_ceiling_extension(authorization)
                        or existing_hashes != {predecessor}
                        or prior is None
                        or prior.get("changed_policy_fields")
                        != POLICY_TRANSITION_CHANGES[-1]
                        or _money(
                            prior.get("maximum_authorized_cumulative_tranche_usd"),
                            "predecessor tranche",
                            positive=True,
                        )
                        != Decimal("5")
                        or _money(
                            authorization.get(
                                "maximum_authorized_cumulative_tranche_usd"
                            ),
                            "transition tranche",
                            positive=True,
                        )
                        != Decimal("10")
                    ):
                        raise ValueError(
                            "multiple applied price configuration transitions exist"
                        )
                if authorization["schema"] == "shared-paid-call-config-transition-v1":
                    if from_pair != initial_pair:
                        raise ValueError(
                            "the applied price configuration transition changed"
                        )
                else:
                    if not self._is_ceiling_extension(authorization):
                        prior_hashes = transition_events_by_pair.get(from_pair, set())
                        expected_predecessor = (
                            None
                            if from_pair == initial_pair
                            else next(iter(prior_hashes))
                        )
                        if (
                            authorization["from_config_transition_sha256"]
                            != expected_predecessor
                        ):
                            raise ValueError(
                                "the policy transition predecessor changed"
                            )
                allowed_pairs.add(target_pair)
                transition_events_by_pair.setdefault(target_pair, set()).add(event_hash)
                transition_authorizations_by_hash[event_hash] = authorization
                pending_events.remove(item)
                progressed = True
            if not progressed:
                raise ValueError("the applied configuration transition chain changed")
        reconciliation_events: dict[str, tuple[Path, dict[str, Any]]] = {}
        for path in self.receipts_dir.glob("*.usage-reconciliation.json"):
            event = self._read_usage_reconciliation(path)
            request_key = event["request_key"]
            if request_key in reconciliation_events:
                raise ValueError("multiple usage reconciliation events exist")
            reconciliation_events[request_key] = (path, event)
        for path in self.receipts_dir.iterdir():
            match = re.fullmatch(
                r"([a-f0-9]{64})(?:\.resume-[a-f0-9]{64})?"
                r"(?:\.(?:submitted|received))?\.json",
                path.name,
            )
            if match and match.group(1) not in ledger["requests"]:
                raise ValueError(
                    "an immutable paid-call event is absent from the ledger"
                )
        base_fields = (
            "request_key",
            "request_sha256",
            "run_id",
            "stage",
            "paper_id",
            "family_id",
            "source_version_id",
            "model",
            "gate_sha256",
            "price_config_sha256",
            "policy_sha256",
            "config_transition_sha256",
        )
        for request_key, request in ledger["requests"].items():
            resume_receipt_sha256 = request.get("resumed_from_not_submitted_sha256")
            resumed = resume_receipt_sha256 is not None
            if resumed:
                original_path = self.receipts_dir / f"{request_key}.json"
                if (
                    not re.fullmatch(r"[a-f0-9]{64}", str(resume_receipt_sha256 or ""))
                    or not original_path.is_file()
                    or sha256_file(original_path) != resume_receipt_sha256
                ):
                    raise ValueError("a resumed request lost its not-submitted receipt")
                original = _read(original_path)
                stable_fields = tuple(
                    name
                    for name in base_fields
                    if name not in {"gate_sha256", "config_transition_sha256"}
                )
                if (
                    any(
                        original.get(name) != request.get(name)
                        for name in stable_fields
                    )
                    or original.get("state") != "not_submitted"
                    or original.get("reason") != AUTHORIZED_CAP_REASON
                    or original.get("live_call_made") is not False
                    or original.get("config_transition_sha256")
                    != request.get("resumed_from_config_transition_sha256")
                ):
                    raise ValueError("a resumed request changed its prior identity")
            request_config_hash = request.get(
                "price_config_sha256", ledger["price_config_sha256"]
            )
            request_policy_hash = request.get("policy_sha256", ledger["policy_sha256"])
            request_pair = (request_config_hash, request_policy_hash)
            if request_pair not in allowed_pairs:
                raise ValueError(
                    "a paid-call request uses an unauthorized configuration"
                )
            transition_hash = request.get("config_transition_sha256")
            if request_pair == initial_pair:
                if transition_hash is not None:
                    raise ValueError(
                        "an initial-config request has a transition binding"
                    )
            elif transition_hash not in transition_events_by_pair.get(
                request_pair, set()
            ):
                raise ValueError(
                    "a paid-call request lacks its authorized config transition"
                )
            event_stem = self._request_event_stem(request_key, request)
            submitted_path = self.receipts_dir / f"{event_stem}.submitted.json"
            final_path = self.receipts_dir / f"{event_stem}.json"
            if submitted_path.is_file():
                submitted = _read(submitted_path)
                if any(
                    submitted.get(name) != request.get(name) for name in base_fields
                ):
                    raise ValueError("an immutable submitted event changed identity")
                if request.get("state") in {
                    "submitted",
                    "completed",
                    "ambiguous_charge",
                } and _money(
                    submitted.get("reserved_usd"),
                    "submitted reservation",
                    positive=True,
                ) != _money(
                    request.get("reserved_usd"), "ledger reservation", positive=True
                ):
                    raise ValueError("an immutable submitted reservation changed")
            state = request.get("state")
            if state not in {
                "completed",
                "ambiguous_charge",
                "count_error",
                "too_large_not_ready",
                "not_submitted",
            }:
                continue
            if not final_path.is_file():
                raise ValueError("a terminal paid request lacks its immutable receipt")
            final = _read(final_path)
            if any(final.get(name) != request.get(name) for name in base_fields):
                raise ValueError("an immutable final event changed request identity")
            reconciliation = reconciliation_events.pop(request_key, None)
            reconciliation_sha256 = request.get("usage_reconciliation_sha256")
            if reconciliation_sha256 is not None:
                if state != "completed" or reconciliation is None:
                    raise ValueError("a usage reconciliation event is absent")
                reconciliation_path, event = reconciliation
                received_path = self.receipts_dir / f"{event_stem}.received.json"
                if (
                    not re.fullmatch(r"[a-f0-9]{64}", reconciliation_sha256)
                    or sha256_file(reconciliation_path) != reconciliation_sha256
                    or final.get("state") != "ambiguous_charge"
                    or not received_path.is_file()
                    or event["received_receipt_sha256"] != sha256_file(received_path)
                    or event["ambiguous_receipt_sha256"] != sha256_file(final_path)
                    or event["config_transition_sha256"]
                    != request.get("config_transition_sha256")
                    or event["price_config_sha256"]
                    != request.get("price_config_sha256")
                    or event["normalized_usage"] != request.get("usage")
                    or _money(event["actual_cost_usd"], "reconciled actual cost")
                    != _money(request.get("actual_cost_usd"), "ledger actual cost")
                ):
                    raise ValueError("a usage reconciliation event changed")
            else:
                if reconciliation is not None and state != "ambiguous_charge":
                    raise ValueError("an unapplied usage reconciliation event exists")
                if final.get("state") != state:
                    raise ValueError("an immutable final event changed request state")
                if state == "completed" and (
                    _money(final.get("actual_cost_usd"), "final actual cost")
                    != _money(request.get("actual_cost_usd"), "ledger actual cost")
                    or final.get("usage") != request.get("usage")
                ):
                    raise ValueError("an immutable final event changed cost or usage")

        if reconciliation_events:
            raise ValueError("a usage reconciliation event lacks a ledger request")

        accepted_events: dict[str, str] = {}
        for path in self.receipts_dir.glob("accepted-*.json"):
            value = _read(path)
            if value.get("schema") != "shared-paid-call-accepted-item-v1":
                raise ValueError("an accepted-item event has an invalid schema")
            family_id = value.get("family_id")
            item_id = value.get("item_id")
            expected_name = f"accepted-{sha256_bytes(str(family_id).encode())}.json"
            if (
                not isinstance(family_id, str)
                or not family_id
                or not isinstance(item_id, str)
                or not item_id
                or path.name != expected_name
            ):
                raise ValueError("an accepted-item event has an invalid identity")
            accepted_events[family_id] = item_id
        if accepted_events != ledger["accepted_families"]:
            raise ValueError("the accepted-item ledger differs from immutable events")

    def _read_usage_reconciliation(self, path: Path) -> dict[str, Any]:
        event = _read(path)
        if not isinstance(event, dict) or set(event) != USAGE_RECONCILIATION_FIELDS:
            raise ValueError("a usage reconciliation event changed")
        request_key = event.get("request_key")
        if (
            event.get("schema") != "shared-paid-call-usage-reconciliation-v1"
            or not isinstance(request_key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", request_key)
            or path.name != f"{request_key}.usage-reconciliation.json"
        ):
            raise ValueError("a usage reconciliation event changed")
        for field in (
            "received_receipt_sha256",
            "ambiguous_receipt_sha256",
            "price_config_sha256",
            "ledger_sha256_before",
            "gate_sha256",
            "review_record_sha256",
        ):
            if not re.fullmatch(r"[a-f0-9]{64}", str(event.get(field) or "")):
                raise ValueError("a usage reconciliation event changed")
        transition = event.get("config_transition_sha256")
        if transition is not None and not re.fullmatch(
            r"[a-f0-9]{64}", str(transition)
        ):
            raise ValueError("a usage reconciliation event changed")
        usage = _normalized_usage({"usageMetadata": event.get("normalized_usage")})
        if usage != event["normalized_usage"] or usage["thoughtsTokenCount"] != 0:
            raise ValueError("a usage reconciliation event changed")
        actual = _cost(
            self.config,
            usage["promptTokenCount"],
            usage["candidatesTokenCount"] + usage["thoughtsTokenCount"],
        )
        if _money(event.get("actual_cost_usd"), "reconciled cost") != actual:
            raise ValueError("a usage reconciliation event changed")
        review_path = Path(str(event.get("review_record") or "")).resolve()
        if (
            not str(event.get("integrated_code_commit") or "").strip()
            or not review_path.is_file()
            or event["review_record_sha256"] != sha256_file(review_path)
        ):
            raise ValueError("a usage reconciliation review changed")
        try:
            reconciled = datetime.fromisoformat(
                str(event["reconciled_at_utc"]).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError("a usage reconciliation time changed") from error
        if reconciled.tzinfo is None:
            raise ValueError("a usage reconciliation time changed")
        return event

    def _validate_ledger(self, ledger: dict[str, Any]) -> None:
        required = {
            "schema",
            "policy_sha256",
            "price_config_sha256",
            "prior_construction_spend_usd",
            "reserved_usd",
            "spent_usd",
            "ambiguous_reserved_usd",
            "generation_submissions",
            "count_requests",
            "inflight",
            "accepted_question_count",
            "accepted_families",
            "family_bindings",
            "paper_bindings",
            "live_test_papers",
            "recent_submission_times_utc",
            "requests",
            "stages",
            "papers",
            "halted",
            "halt_reason",
            "created_at_utc",
            "updated_at_utc",
        }
        if not isinstance(ledger, dict) or set(ledger) != required:
            raise ValueError("the shared paid-call ledger fields changed")
        if ledger["schema"] != "shared-paid-call-ledger-v1":
            raise ValueError("the shared paid-call ledger schema changed")
        if (
            not isinstance(ledger["requests"], dict)
            or not isinstance(ledger["family_bindings"], dict)
            or not isinstance(ledger["paper_bindings"], dict)
        ):
            raise ValueError("the shared paid-call ledger mappings are invalid")
        if not isinstance(ledger["halted"], bool):
            raise ValueError("the shared paid-call halt state is invalid")

        expected_stages: dict[str, dict[str, Any]] = {}
        expected_papers: dict[str, dict[str, Any]] = {}
        expected_live: dict[str, dict[str, Any]] = {}
        totals = {
            "reserved": Decimal("0"),
            "spent": Decimal("0"),
            "ambiguous": Decimal("0"),
        }
        submissions = 0
        inflight = 0
        submitted_states = {"submitted", "completed", "ambiguous_charge"}
        terminal_states = {
            "completed",
            "ambiguous_charge",
            "count_error",
            "too_large_not_ready",
            "not_submitted",
        }
        for key, request in ledger["requests"].items():
            if not re.fullmatch(r"[a-f0-9]{64}", key) or not isinstance(request, dict):
                raise ValueError("the shared paid-call request identity is invalid")
            if request.get("request_key") != key:
                raise ValueError("a paid-call request key does not match its record")
            for name in (
                "request_sha256",
                "run_id",
                "stage",
                "paper_id",
                "family_id",
                "source_version_id",
                "model",
                "gate_sha256",
            ):
                if not isinstance(request.get(name), str) or not request[name]:
                    raise ValueError(f"a paid-call request lacks {name}")
            request_config_hash = request.get("price_config_sha256")
            if request_config_hash is not None and (
                not isinstance(request_config_hash, str)
                or not re.fullmatch(r"[a-f0-9]{64}", request_config_hash)
            ):
                raise ValueError("a paid-call request has an invalid price config hash")
            request_policy_hash = request.get("policy_sha256")
            if request_policy_hash is not None and (
                not isinstance(request_policy_hash, str)
                or not re.fullmatch(r"[a-f0-9]{64}", request_policy_hash)
            ):
                raise ValueError("a paid-call request has an invalid policy hash")
            transition_hash = request.get("config_transition_sha256")
            if transition_hash is not None and (
                not isinstance(transition_hash, str)
                or not re.fullmatch(r"[a-f0-9]{64}", transition_hash)
            ):
                raise ValueError(
                    "a paid-call request has an invalid config transition hash"
                )
            resume_receipt_sha256 = request.get("resumed_from_not_submitted_sha256")
            resume_transition_sha256 = request.get(
                "resumed_from_config_transition_sha256"
            )
            if (resume_receipt_sha256 is None) != (resume_transition_sha256 is None):
                raise ValueError("a paid-call resume binding is incomplete")
            for value in (resume_receipt_sha256, resume_transition_sha256):
                if value is not None and (
                    not isinstance(value, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", value)
                ):
                    raise ValueError("a paid-call resume binding is invalid")
            if resume_transition_sha256 is not None and (
                transition_hash is None or transition_hash == resume_transition_sha256
            ):
                raise ValueError("a paid-call resume transition did not advance")
            if request["stage"] not in STAGES:
                raise ValueError("a paid-call request has an unsupported stage")
            binding = ledger["family_bindings"].get(request["family_id"])
            if binding != {
                "paper_id": request["paper_id"],
                "source_version_id": request["source_version_id"],
            }:
                raise ValueError("a paid-call family binding is inconsistent")
            state = request.get("state")
            if state not in {"counting", *terminal_states, "submitted"}:
                raise ValueError("a paid-call request state is invalid")
            if state not in submitted_states:
                continue
            phase = request.get("phase")
            if phase not in PHASES:
                raise ValueError("a submitted paid-call phase is invalid")
            reserved = _money(
                request.get("reserved_usd"), "request reservation", positive=True
            )
            submissions += 1
            stage = expected_stages.setdefault(
                request["stage"], self._empty_usage_row()
            )
            paper = expected_papers.setdefault(
                request["family_id"],
                {
                    **{
                        key: value
                        for key, value in self._empty_usage_row().items()
                        if key != "submissions"
                    },
                    "paper_id": request["paper_id"],
                    "source_version_id": request["source_version_id"],
                },
            )
            stage["submissions"] += 1
            live = None
            if phase == "live_test":
                live = expected_live.setdefault(
                    request["family_id"], self._empty_live_row()
                )
                live["submissions"] += 1
            if state == "submitted":
                inflight += 1
                totals["reserved"] += reserved
                stage["reserved_usd"] = str(
                    _money(stage["reserved_usd"], "stage reserved") + reserved
                )
                paper["reserved_usd"] = str(
                    _money(paper["reserved_usd"], "paper reserved") + reserved
                )
                if live is not None:
                    live["reserved_usd"] = str(
                        _money(live["reserved_usd"], "live reserved") + reserved
                    )
            elif state == "ambiguous_charge":
                totals["ambiguous"] += reserved
                stage["ambiguous_usd"] = str(
                    _money(stage["ambiguous_usd"], "stage ambiguous") + reserved
                )
                paper["ambiguous_usd"] = str(
                    _money(paper["ambiguous_usd"], "paper ambiguous") + reserved
                )
                if live is not None:
                    live["ambiguous_usd"] = str(
                        _money(live["ambiguous_usd"], "live ambiguous") + reserved
                    )
            else:
                actual = _money(request.get("actual_cost_usd"), "request actual cost")
                if actual > reserved:
                    raise ValueError("a paid-call actual cost exceeds its reservation")
                usage = request.get("usage")
                if not isinstance(usage, dict):
                    raise ValueError("a completed paid-call request lacks usage")
                for field in (
                    "promptTokenCount",
                    "candidatesTokenCount",
                    "thoughtsTokenCount",
                ):
                    value = usage.get(field)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                    ):
                        raise ValueError(
                            "a completed paid-call request has invalid usage"
                        )
                totals["spent"] += actual
                stage["spent_usd"] = str(
                    _money(stage["spent_usd"], "stage spent") + actual
                )
                paper["spent_usd"] = str(
                    _money(paper["spent_usd"], "paper spent") + actual
                )
                if live is not None:
                    live["spent_usd"] = str(
                        _money(live["spent_usd"], "live spent") + actual
                    )
                for target in (stage, paper):
                    target["input_tokens"] += usage["promptTokenCount"]
                    target["output_tokens"] += usage["candidatesTokenCount"]
                    target["thinking_tokens"] += usage["thoughtsTokenCount"]

        source_bindings: dict[str, str] = {}
        expected_paper_bindings: dict[str, dict[str, str]] = {}
        for family_id, binding in ledger["family_bindings"].items():
            if (
                not isinstance(family_id, str)
                or not family_id
                or not isinstance(binding, dict)
                or set(binding) != {"paper_id", "source_version_id"}
                or not all(
                    isinstance(value, str) and value for value in binding.values()
                )
            ):
                raise ValueError("a paid-call family binding is invalid")
            previous = source_bindings.setdefault(
                binding["source_version_id"], family_id
            )
            if previous != family_id:
                raise ValueError(
                    "one source version is bound to multiple paper families"
                )
            paper_binding = {
                "family_id": family_id,
                "source_version_id": binding["source_version_id"],
            }
            previous_paper = expected_paper_bindings.setdefault(
                binding["paper_id"], paper_binding
            )
            if previous_paper != paper_binding:
                raise ValueError("one paper ID is bound to multiple paper families")
        if ledger["paper_bindings"] != expected_paper_bindings:
            raise ValueError("the paid-call paper bindings are inconsistent")
        for name, value in {
            "reserved_usd": totals["reserved"],
            "spent_usd": totals["spent"],
            "ambiguous_reserved_usd": totals["ambiguous"],
        }.items():
            if _money(ledger.get(name), name) != value:
                raise ValueError(f"the shared paid-call {name} total is inconsistent")
        for name, value in {
            "generation_submissions": submissions,
            "count_requests": len(ledger["requests"]),
            "inflight": inflight,
            "accepted_question_count": len(ledger["accepted_families"]),
        }.items():
            if ledger.get(name) != value:
                raise ValueError(f"the shared paid-call {name} total is inconsistent")

        def rows_match(actual: Any, expected: dict[str, dict[str, Any]]) -> bool:
            if not isinstance(actual, dict) or set(actual) != set(expected):
                return False
            for identity, expected_row in expected.items():
                actual_row = actual.get(identity)
                if not isinstance(actual_row, dict) or set(actual_row) != set(
                    expected_row
                ):
                    return False
                for name, value in expected_row.items():
                    if name.endswith("_usd"):
                        if _money(actual_row.get(name), name) != _money(value, name):
                            return False
                    elif actual_row.get(name) != value:
                        return False
            return True

        for name, value in {
            "stages": expected_stages,
            "papers": expected_papers,
            "live_test_papers": expected_live,
        }.items():
            if not rows_match(ledger.get(name), value):
                raise ValueError(f"the shared paid-call {name} total is inconsistent")
        if (
            not isinstance(ledger["recent_submission_times_utc"], list)
            or len(ledger["recent_submission_times_utc"]) > submissions
        ):
            raise ValueError("the paid-call submission window is inconsistent")
        for value in ledger["recent_submission_times_utc"]:
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def _status_payload(self, ledger: dict[str, Any]) -> dict[str, Any]:
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
        live_test_cap = _money(self.policy["live_test_suballocation_usd"], "live test")
        if self._authorized_live_test_ceiling_usd is not None:
            live_test_cap = min(live_test_cap, self._authorized_live_test_ceiling_usd)
        construction_used = self.prior + away_used
        accepted = int(ledger["accepted_question_count"])
        cutoff = datetime.now(UTC) - timedelta(minutes=1)
        recent_count = sum(
            datetime.fromisoformat(value.replace("Z", "+00:00")) > cutoff
            for value in ledger["recent_submission_times_utc"]
        )
        live_submissions = sum(
            int(row.get("submissions", 0))
            for row in ledger["live_test_papers"].values()
        )
        papers = {}
        paper_cap = _money(self.policy["maximum_paper_cost_usd"], "paper cap")
        for family_id, row in ledger["papers"].items():
            used = sum(
                _money(row[name], f"paper {name}")
                for name in ("reserved_usd", "spent_usd", "ambiguous_usd")
            )
            papers[family_id] = {**row, "remaining_usd": str(paper_cap - used)}
        limits = {
            key: self.policy[key]
            for key in (
                "project_lifetime_ceiling_usd",
                "reserved_for_benchmark_evaluation_usd",
                "dataset_construction_allocation_usd",
                "construction_review_checkpoint_usd",
                "accepted_question_target",
                "away_session_total_ceiling_usd",
                "live_test_suballocation_usd",
                "live_test_maximum_papers",
                "live_test_maximum_generation_submissions",
                "away_maximum_generation_submissions",
                "maximum_request_reserved_cost_usd",
                "maximum_paper_cost_usd",
                "maximum_concurrent_generation_requests",
                "maximum_generation_requests_per_minute",
                "maximum_output_tokens_including_thinking",
                "automatic_transport_generation_retries",
            )
        }
        limits["authorized_live_test_ceiling_usd"] = (
            str(self._authorized_live_test_ceiling_usd)
            if self._authorized_live_test_ceiling_usd is not None
            else None
        )
        usage = {
            "project_lifetime_usd": str(construction_used),
            "benchmark_evaluation_usd": "0",
            "dataset_construction_usd": str(construction_used),
            "construction_checkpoint_usd": str(construction_used),
            "away_session_usd": str(away_used),
            "live_test_usd": str(live_used),
            "accepted_questions": accepted,
            "live_test_papers": len(ledger["live_test_papers"]),
            "live_test_generation_submissions": live_submissions,
            "away_generation_submissions": int(ledger["generation_submissions"]),
            "concurrent_generation_requests": int(ledger["inflight"]),
            "generation_requests_in_current_minute": recent_count,
        }
        remaining = {
            "project_lifetime_usd": str(
                _money(self.policy["project_lifetime_ceiling_usd"], "lifetime")
                - construction_used
            ),
            "benchmark_evaluation_usd": str(
                _money(
                    self.policy["reserved_for_benchmark_evaluation_usd"],
                    "evaluation reserve",
                )
            ),
            "dataset_construction_usd": str(
                _money(
                    self.policy["dataset_construction_allocation_usd"],
                    "construction allocation",
                )
                - construction_used
            ),
            "construction_checkpoint_usd": str(
                _money(self.policy["construction_review_checkpoint_usd"], "checkpoint")
                - construction_used
            ),
            "away_session_usd": str(
                _money(self.policy["away_session_total_ceiling_usd"], "away")
                - away_used
            ),
            "live_test_usd": str(live_test_cap - live_used),
            "accepted_questions": int(self.policy["accepted_question_target"])
            - accepted,
            "live_test_papers": (
                None
                if self.policy["live_test_maximum_papers"] is None
                else int(self.policy["live_test_maximum_papers"])
                - len(ledger["live_test_papers"])
            ),
            "live_test_generation_submissions": (
                None
                if self.policy["live_test_maximum_generation_submissions"] is None
                else int(self.policy["live_test_maximum_generation_submissions"])
                - live_submissions
            ),
            "away_generation_submissions": int(
                self.policy["away_maximum_generation_submissions"]
            )
            - int(ledger["generation_submissions"]),
            "concurrent_generation_requests": int(
                self.policy["maximum_concurrent_generation_requests"]
            )
            - int(ledger["inflight"]),
            "generation_requests_in_current_minute": int(
                self.policy["maximum_generation_requests_per_minute"]
            )
            - recent_count,
        }
        return {
            "schema": "shared-gemini-broker-status-v2",
            "ledger_file": str(self.ledger_file),
            "ledger_sha256": sha256_file(self.ledger_file),
            "policy_sha256": sha256_file(self.policy_file),
            "initial_policy_sha256": ledger["policy_sha256"],
            "price_config_sha256": self.active_price_config_sha256,
            "initial_price_config_sha256": ledger["price_config_sha256"],
            "config_transition_sha256": self._config_transition_sha256,
            "updated_at_utc": ledger["updated_at_utc"],
            "spent_usd": ledger["spent_usd"],
            "reserved_usd": ledger["reserved_usd"],
            "ambiguous_reserved_usd": ledger["ambiguous_reserved_usd"],
            "away_remaining_usd": remaining["away_session_usd"],
            "live_test_remaining_usd": remaining["live_test_usd"],
            "construction_checkpoint_remaining_usd": remaining[
                "construction_checkpoint_usd"
            ],
            "cost_per_accepted_question_usd": (
                str(_money(ledger["spent_usd"], "spent") / accepted)
                if accepted
                else None
            ),
            "generation_submissions": ledger["generation_submissions"],
            "count_requests": ledger["count_requests"],
            "inflight": ledger["inflight"],
            "accepted_question_count": accepted,
            "halted": ledger["halted"],
            "halt_reason": ledger["halt_reason"],
            "integrity_valid": True,
            "status_state": "valid",
            "limits": limits,
            "usage": usage,
            "remaining": remaining,
            "stages": ledger["stages"],
            "papers": papers,
        }

    def _publish_status(self, ledger: dict[str, Any]) -> None:
        atomic_json(self._status_file, self._status_payload(ledger))
        if self._status_observer is not None:
            self._status_observer(self._status_file)

    def set_status_observer(self, observer: Callable[[Path], None] | None) -> None:
        """Refresh a derived custody record after each durable broker state."""
        self._status_observer = observer
        if observer is not None:
            observer(self._status_file)

    @staticmethod
    def _validate_stream_input_gate(gate: dict[str, Any]) -> None:
        version = gate.get("continuation_input_binding_version")
        if version != STREAM_INPUT_BINDING_VERSION:
            raise ValueError("the reviewed continuation input version changed")
        if any(field not in gate for field in STREAM_INPUT_GATE_FIELDS):
            raise ValueError("the reviewed continuation input binding is incomplete")

    def _validate_stream_input_binding(
        self,
        gate: dict[str, Any],
        binding: dict[str, Any] | None,
        *,
        request_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        version = gate.get("continuation_input_binding_version")
        if version is None:
            return None
        self._validate_stream_input_gate(gate)
        if binding is None:
            raise ValueError("the reviewed continuation input is not bound")
        expected_dir = Path(str(gate["continuation_artifact"])).resolve()
        access_run_dir = binding["access_run_dir"]
        if access_run_dir.resolve() != expected_dir:
            raise ValueError("the reviewed continuation input directory changed")
        if (
            binding["run_id"] != gate["authorized_new_run_id"]
            or binding["campaign_id"] != gate["authorized_campaign_id"]
            or (
                request_run_id is not None
                and request_run_id != gate["authorized_new_run_id"]
            )
        ):
            raise ValueError("the reviewed continuation run identity changed")
        for field, gate_field in (
            ("eligibility_prompt_file", "eligibility_prompt_sha256"),
            ("eligibility_schema_file", "eligibility_schema_sha256"),
            ("eligibility_policy_file", "eligibility_policy_sha256"),
        ):
            path = binding[field]
            if not path.is_file() or sha256_file(path) != gate[gate_field]:
                raise ValueError("the reviewed continuation eligibility inputs changed")
        manifest_path = expected_dir / "run-manifest.json"
        receipt_path = expected_dir / "run-receipt.json"
        if (
            not manifest_path.is_file()
            or not receipt_path.is_file()
            or sha256_file(manifest_path) != gate["continuation_run_manifest_sha256"]
            or sha256_file(receipt_path) != gate["continuation_run_receipt_sha256"]
        ):
            raise ValueError("the reviewed continuation input receipts changed")
        manifest = _read(manifest_path)
        receipt = _read(receipt_path)
        family_count = gate["continuation_family_count"]
        selection = manifest.get("selection")
        if (
            isinstance(family_count, bool)
            or not isinstance(family_count, int)
            or family_count < 1
            or manifest.get("schema") != "article-access-manifest-v1"
            or manifest.get("run_id") != gate["continuation_access_run_id"]
            or manifest.get("target_total") != family_count
            or not isinstance(selection, list)
            or len(selection) != family_count
            or manifest.get("frozen_manifest_sha256")
            != gate["continuation_frozen_manifest_sha256"]
            or manifest.get("remaining_order_sha256")
            != gate["continuation_order_sha256"]
            or receipt.get("schema") != "article-access-run-receipt-v1"
            or receipt.get("state") != "completed"
            or receipt.get("run_id") != manifest.get("run_id")
            or receipt.get("run_manifest_sha256") != sha256_file(manifest_path)
            or receipt.get("frozen_manifest_sha256")
            not in (None, gate["continuation_frozen_manifest_sha256"])
            or receipt.get("remaining_order_sha256")
            != gate["continuation_order_sha256"]
            or (receipt.get("counts") or {}).get("target") != family_count
        ):
            raise ValueError("the reviewed continuation input identity changed")
        return binding

    def bind_stream_input(
        self,
        access_run_dir: Path,
        *,
        phase: str,
        run_id: str,
        campaign_id: str,
        eligibility_prompt_file: Path,
        eligibility_schema_file: Path,
        eligibility_policy_file: Path,
    ) -> None:
        """Bind a reviewed live continuation input before provider use."""
        gate = _validate_gate(self.execution_gate_file, phase)
        binding = {
            "access_run_dir": access_run_dir.resolve(),
            "run_id": run_id,
            "campaign_id": campaign_id,
            "eligibility_prompt_file": eligibility_prompt_file.resolve(),
            "eligibility_schema_file": eligibility_schema_file.resolve(),
            "eligibility_policy_file": eligibility_policy_file.resolve(),
        }
        self._stream_input_binding = self._validate_stream_input_binding(gate, binding)

    def stream_input_binding_required(self) -> bool:
        """Report whether this gate uses the versioned input contract."""
        return (
            _read(self.execution_gate_file).get("continuation_input_binding_version")
            is not None
        )

    def _commit_ledger(self, ledger: dict[str, Any]) -> None:
        self._validate_ledger(ledger)
        atomic_json(self.ledger_file, ledger)
        self._publish_status(ledger)

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
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            return self._status_payload(ledger)

    def effective_receipt_path(self, request_key: str) -> Path:
        """Return the validated path for the current final receipt."""
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the paid-call request key is invalid")
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            request = ledger["requests"].get(request_key)
            if request is None:
                raise ValueError("the paid-call request does not exist")
            path = self.receipts_dir / (
                f"{self._request_event_stem(request_key, request)}.json"
            )
            if not path.is_file():
                raise ValueError("the paid-call request has no final receipt")
            return path

    def effective_receipt(self, request_key: str) -> dict[str, Any]:
        """Read a final receipt through validated reconciliation custody."""
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the paid-call request key is invalid")
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            request = ledger["requests"].get(request_key)
            if request is None:
                raise ValueError("the paid-call request does not exist")
            event_stem = self._request_event_stem(request_key, request)
            final_path = self.receipts_dir / f"{event_stem}.json"
            final = _read(final_path)
            reconciliation_sha256 = request.get("usage_reconciliation_sha256")
            if reconciliation_sha256 is None:
                return final

            reconciliation_path = (
                self.receipts_dir / f"{request_key}.usage-reconciliation.json"
            )
            received_path = self.receipts_dir / f"{event_stem}.received.json"
            reconciliation = self._read_usage_reconciliation(reconciliation_path)
            received = _read(received_path)
            if (
                request.get("state") != "completed"
                or sha256_file(reconciliation_path) != reconciliation_sha256
                or reconciliation["received_receipt_sha256"]
                != sha256_file(received_path)
                or reconciliation["ambiguous_receipt_sha256"] != sha256_file(final_path)
            ):
                raise ValueError("the reconciled paid-call receipt changed")
            return {
                **final,
                "state": "completed",
                "response": received["response"],
                "usage": request["usage"],
                "actual_cost_usd": request["actual_cost_usd"],
                "usage_reconciliation_sha256": reconciliation_sha256,
                "received_receipt_sha256": reconciliation["received_receipt_sha256"],
                "ambiguous_receipt_sha256": reconciliation["ambiguous_receipt_sha256"],
            }

    def record_accepted(self, *, family_id: str, item_id: str) -> dict[str, Any]:
        if not family_id or not item_id:
            raise ValueError("accepted item identity is missing")
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            old = ledger["accepted_families"].get(family_id)
            if old and old != item_id:
                raise ValueError("a paper family already has an accepted item")
            duplicate_family = next(
                (
                    other_family
                    for other_family, accepted_item in ledger[
                        "accepted_families"
                    ].items()
                    if accepted_item == item_id and other_family != family_id
                ),
                None,
            )
            if duplicate_family:
                raise ValueError(
                    "an accepted item is already assigned to another paper family"
                )
            if not old and len(ledger["accepted_families"]) >= int(
                self.policy["accepted_question_target"]
            ):
                raise ValueError("the accepted-question target is complete")
            if not old:
                atomic_json(
                    self.receipts_dir
                    / f"accepted-{sha256_bytes(family_id.encode())}.json",
                    {
                        "schema": "shared-paid-call-accepted-item-v1",
                        "family_id": family_id,
                        "item_id": item_id,
                        "recorded_at_utc": _now(),
                    },
                    immutable=True,
                )
            ledger["accepted_families"][family_id] = item_id
            ledger["accepted_question_count"] = len(ledger["accepted_families"])
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)
        return self.status()

    def reconcile_omitted_thought_usage(self, request_key: str) -> dict[str, Any]:
        """Settle one saved response whose exact token total proves zero thoughts."""
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the reconciled request key is invalid")
        operation = self._operation_lock_file.open("a+")
        try:
            fcntl.flock(operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            operation.close()
            raise ValueError("another paid broker operation is active") from error
        try:
            with self._lock_file.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                ledger = self._validated_ledger()
                request = ledger["requests"].get(request_key)
                if request is None:
                    raise ValueError("the reconciled request does not exist")
                reconciliation_path = (
                    self.receipts_dir / f"{request_key}.usage-reconciliation.json"
                )
                if request.get("usage_reconciliation_sha256") is not None:
                    event = self._read_usage_reconciliation(reconciliation_path)
                    return {
                        "schema": "shared-paid-call-usage-reconciliation-result-v1",
                        "request_key": request_key,
                        "applied": False,
                        "actual_cost_usd": event["actual_cost_usd"],
                        "reconciliation_receipt": str(reconciliation_path),
                        "reconciliation_receipt_sha256": sha256_file(
                            reconciliation_path
                        ),
                    }
                if request.get("state") != "ambiguous_charge":
                    raise ValueError("the request does not have an ambiguous charge")
                if (
                    ledger.get("halted") is not True
                    or ledger.get("halt_reason") != "ambiguous_generation_charge"
                ):
                    raise ValueError("the ambiguous-charge halt state changed")

                event_stem = self._request_event_stem(request_key, request)
                final_path = self.receipts_dir / f"{event_stem}.json"
                received_path = self.receipts_dir / f"{event_stem}.received.json"
                final = _read(final_path)
                received = _read(received_path)
                if (
                    final.get("state") != "ambiguous_charge"
                    or final.get("error") != "KeyError: 'thoughtsTokenCount'"
                    or received.get("state") != "response_received"
                    or final.get("response") != received.get("response")
                ):
                    raise ValueError(
                        "the ambiguous response is not the omitted-thoughts case"
                    )
                raw_usage = received["response"].get("usageMetadata")
                if (
                    not isinstance(raw_usage, dict)
                    or "thoughtsTokenCount" in raw_usage
                    or any(
                        field not in raw_usage
                        for field in (
                            "promptTokenCount",
                            "candidatesTokenCount",
                            "totalTokenCount",
                        )
                    )
                ):
                    raise ValueError(
                        "the saved usage does not omit only the thought-token value"
                    )
                usage = _normalized_usage(received["response"])
                actual = _cost(
                    self.config,
                    usage["promptTokenCount"],
                    usage["candidatesTokenCount"] + usage["thoughtsTokenCount"],
                )
                reserved = _money(
                    request.get("reserved_usd"), "reconciled reservation", positive=True
                )
                if actual > reserved:
                    raise ValueError("the reconciled cost exceeds the reservation")

                gate = _validate_gate(self.execution_gate_file, request["phase"])
                review_path = Path(gate["review_record"]).resolve()
                if not review_path.is_file() or gate.get(
                    "review_record_sha256"
                ) != sha256_file(review_path):
                    raise ValueError("the usage reconciliation review is invalid")
                event = {
                    "schema": "shared-paid-call-usage-reconciliation-v1",
                    "request_key": request_key,
                    "received_receipt_sha256": sha256_file(received_path),
                    "ambiguous_receipt_sha256": sha256_file(final_path),
                    "config_transition_sha256": request.get("config_transition_sha256"),
                    "price_config_sha256": request.get(
                        "price_config_sha256", ledger["price_config_sha256"]
                    ),
                    "normalized_usage": usage,
                    "actual_cost_usd": str(actual),
                    "ledger_sha256_before": sha256_file(self.ledger_file),
                    "gate_sha256": sha256_file(self.execution_gate_file),
                    "integrated_code_commit": gate["integrated_code_commit"],
                    "review_record": str(review_path),
                    "review_record_sha256": gate["review_record_sha256"],
                    "reconciled_at_utc": _now(),
                }
                if reconciliation_path.is_file():
                    existing = self._read_usage_reconciliation(reconciliation_path)
                    comparison = dict(event)
                    comparison["reconciled_at_utc"] = existing["reconciled_at_utc"]
                    if existing != comparison:
                        raise ValueError("the usage reconciliation event changed")
                    event = existing
                else:
                    atomic_json(reconciliation_path, event, immutable=True)
                reconciliation_sha256 = sha256_file(reconciliation_path)

                ledger["ambiguous_reserved_usd"] = str(
                    _money(ledger["ambiguous_reserved_usd"], "ambiguous") - reserved
                )
                ledger["spent_usd"] = str(_money(ledger["spent_usd"], "spent") + actual)
                stage = ledger["stages"][request["stage"]]
                paper = ledger["papers"][request["family_id"]]
                for row in (stage, paper):
                    row["ambiguous_usd"] = str(
                        _money(row["ambiguous_usd"], "ambiguous") - reserved
                    )
                    row["spent_usd"] = str(_money(row["spent_usd"], "spent") + actual)
                    row["input_tokens"] += usage["promptTokenCount"]
                    row["output_tokens"] += usage["candidatesTokenCount"]
                    row["thinking_tokens"] += usage["thoughtsTokenCount"]
                if request["phase"] == "live_test":
                    live = ledger["live_test_papers"][request["family_id"]]
                    live["ambiguous_usd"] = str(
                        _money(live["ambiguous_usd"], "live ambiguous") - reserved
                    )
                    live["spent_usd"] = str(
                        _money(live["spent_usd"], "live spent") + actual
                    )
                request.update(
                    {
                        "state": "completed",
                        "actual_cost_usd": str(actual),
                        "usage": usage,
                        "usage_reconciliation_sha256": reconciliation_sha256,
                        "reconciled_at_utc": event["reconciled_at_utc"],
                    }
                )
                unresolved = any(
                    row.get("state") == "ambiguous_charge"
                    for row in ledger["requests"].values()
                )
                if not unresolved and int(ledger["inflight"]) == 0:
                    ledger["halted"] = False
                    ledger["halt_reason"] = None
                ledger["updated_at_utc"] = _now()
                self._validate_ledger(ledger)
                self._validate_immutable_events(ledger)
                self._commit_ledger(ledger)
                return {
                    "schema": "shared-paid-call-usage-reconciliation-result-v1",
                    "request_key": request_key,
                    "applied": True,
                    "actual_cost_usd": str(actual),
                    "reconciliation_receipt": str(reconciliation_path),
                    "reconciliation_receipt_sha256": reconciliation_sha256,
                }
        finally:
            operation.close()

    def _halt(self, reason: str) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            ledger["halted"] = True
            ledger["halt_reason"] = reason
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)

    def _mark_not_submitted(self, request_key: str, state: str, reason: str) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            request = ledger["requests"][request_key]
            if request.get("state") != "counting":
                raise ValueError("the paid request is not in its counting state")
            request["state"] = state
            request["reason"] = reason
            request["completed_at_utc"] = _now()
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)

    def _count_event(self, request_key: str, base: dict[str, Any]) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            if ledger["halted"]:
                raise ValueError(
                    f"the paid-call broker is halted: {ledger['halt_reason']}"
                )
            if request_key in ledger["requests"]:
                raise ValueError("the paid request key already exists")
            binding = {
                "paper_id": base["paper_id"],
                "source_version_id": base["source_version_id"],
            }
            old_binding = ledger["family_bindings"].get(base["family_id"])
            if old_binding is not None and old_binding != binding:
                raise ValueError(
                    "the paper family is already bound to another paper or source version"
                )
            for family_id, item in ledger["family_bindings"].items():
                if (
                    item["source_version_id"] == base["source_version_id"]
                    and family_id != base["family_id"]
                ):
                    raise ValueError(
                        "the source version is already bound to another paper family"
                    )
            paper_binding = {
                "family_id": base["family_id"],
                "source_version_id": base["source_version_id"],
            }
            old_paper_binding = ledger["paper_bindings"].get(base["paper_id"])
            if old_paper_binding is not None and old_paper_binding != paper_binding:
                raise ValueError(
                    "the paper ID is already bound to another family or source version"
                )
            ledger["family_bindings"].setdefault(base["family_id"], binding)
            ledger["paper_bindings"].setdefault(base["paper_id"], paper_binding)
            ledger["count_requests"] += 1
            ledger["requests"][request_key] = {**base, "state": "counting"}
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)

    def _resume_not_submitted(
        self, request_key: str, base: dict[str, Any]
    ) -> int | None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            request = ledger["requests"].get(request_key)
            if request is None:
                return None
            if (
                request.get("state") != "not_submitted"
                or request.get("reason") != AUTHORIZED_CAP_REASON
                or request.get("resumed_from_not_submitted_sha256") is not None
            ):
                raise ValueError("the paid request key already exists")
            event_path = self._config_transition_event_path
            if event_path is None or not event_path.is_file():
                raise ValueError("the paid request lacks a ceiling extension")
            authorization = self._read_transition_event(event_path)["authorization"]
            if (
                not self._is_ceiling_extension(authorization)
                or authorization["from_config_transition_sha256"]
                != request.get("config_transition_sha256")
                or base.get("config_transition_sha256")
                != self._config_transition_sha256
            ):
                raise ValueError("the paid request lacks a ceiling extension")
            stable_fields = {
                "request_key",
                "request_sha256",
                "run_id",
                "stage",
                "paper_id",
                "family_id",
                "source_version_id",
                "model",
                "price_config_sha256",
                "policy_sha256",
            }
            if any(request.get(name) != base.get(name) for name in stable_fields):
                raise ValueError("the paid request resume identity changed")
            original_path = self.receipts_dir / f"{request_key}.json"
            original = _read(original_path) if original_path.is_file() else {}
            exact_input = original.get("input_tokens")
            if (
                original.get("state") != "not_submitted"
                or original.get("reason") != AUTHORIZED_CAP_REASON
                or original.get("live_call_made") is not False
                or isinstance(exact_input, bool)
                or not isinstance(exact_input, int)
                or exact_input < 0
            ):
                raise ValueError("the paid request resume receipt changed")
            prior_transition = request["config_transition_sha256"]
            request.update(base)
            request.update(
                {
                    "state": "counting",
                    "resumed_from_not_submitted_sha256": sha256_file(original_path),
                    "resumed_from_config_transition_sha256": prior_transition,
                }
            )
            request.pop("reason", None)
            request.pop("completed_at_utc", None)
            ledger["updated_at_utc"] = _now()
            self._commit_ledger(ledger)
            return exact_input

    def _pace(self) -> None:
        """Wait until the frozen per-minute submission window has room."""
        while True:
            with self._lock_file.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                ledger = self._validated_ledger()
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
        family_id: str,
        stage: str,
        reserved: Decimal,
    ) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
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
                raise ValueError(PER_REQUEST_CAP_REASON)
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
                family_id,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "reserved_usd": "0",
                    "spent_usd": "0",
                    "ambiguous_usd": "0",
                    "paper_id": paper_id,
                    "source_version_id": request["source_version_id"],
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
                live_test_cap = _money(
                    self.policy["live_test_suballocation_usd"], "live test"
                )
                if self._authorized_live_test_ceiling_usd is not None:
                    live_test_cap = min(
                        live_test_cap, self._authorized_live_test_ceiling_usd
                    )
                if live_used + reserved > live_test_cap:
                    raise ValueError(
                        "the paid request exceeds the authorized live-test cap"
                    )
                paper_limit = self.policy["live_test_maximum_papers"]
                if (
                    paper_limit is not None
                    and family_id not in ledger["live_test_papers"]
                    and len(ledger["live_test_papers"]) >= int(paper_limit)
                ):
                    raise ValueError("the live test reached its paper limit")
                live_submissions = sum(
                    int(row.get("submissions", 0))
                    for row in ledger["live_test_papers"].values()
                )
                submission_limit = self.policy[
                    "live_test_maximum_generation_submissions"
                ]
                if submission_limit is not None and live_submissions >= int(
                    submission_limit
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
                    family_id,
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
            self._commit_ledger(ledger)

    def _settle(
        self,
        request_key: str,
        *,
        actual: Decimal | None,
        usage: dict[str, int] | None,
    ) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            request = ledger["requests"][request_key]
            if request.get("state") != "submitted":
                raise ValueError("the paid request is not submitted")
            reserved = _money(request["reserved_usd"], "reservation", positive=True)
            paper = ledger["papers"][request["family_id"]]
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
                ledger["live_test_papers"].get(request["family_id"])
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
            self._commit_ledger(ledger)

    def _completed_receipt(
        self, submitted: dict[str, Any], response: Any
    ) -> tuple[dict[str, Any], Decimal | None, dict[str, int] | None]:
        try:
            usage = _normalized_usage(response)
            values = [
                usage[name]
                for name in (
                    "promptTokenCount",
                    "candidatesTokenCount",
                    "thoughtsTokenCount",
                    "totalTokenCount",
                )
            ]
            actual = _cost(self.config, values[0], values[1] + values[2])
            if actual > _money(submitted["reserved_usd"], "reservation", positive=True):
                raise ValueError("provider usage exceeds the reservation")
        except Exception as error:
            return (
                {
                    **submitted,
                    "state": "ambiguous_charge",
                    "error": f"{type(error).__name__}: {error}",
                    "response": response,
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                },
                None,
                None,
            )
        return (
            {
                **submitted,
                "state": "completed",
                "actual_cost_usd": str(actual),
                "usage": usage,
                "response": response,
                "live_call_made": True,
                "completed_at_utc": _now(),
            },
            actual,
            usage,
        )

    def _recover_orphans(self) -> None:
        with self._lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = self._validated_ledger()
            requests = {key: dict(row) for key, row in ledger["requests"].items()}
        for request_key, request in requests.items():
            event_stem = self._request_event_stem(request_key, request)
            final_path = self.receipts_dir / f"{event_stem}.json"
            received_path = self.receipts_dir / f"{event_stem}.received.json"
            if request["state"] in {"completed", "ambiguous_charge"}:
                if not final_path.is_file():
                    error = ValueError(
                        "a terminal paid request lacks its immutable final receipt"
                    )
                    self._record_integrity_halt(error)
                    raise error
                continue
            if request["state"] != "submitted":
                continue
            if final_path.is_file():
                receipt = _read(final_path)
                state = receipt.get("state")
                if state == "completed":
                    actual = _money(
                        receipt.get("actual_cost_usd"), "recovered actual cost"
                    )
                    usage = receipt.get("usage")
                elif state == "ambiguous_charge":
                    actual = None
                    usage = None
                else:
                    raise ValueError("an orphan final receipt has an invalid state")
            elif received_path.is_file():
                received = _read(received_path)
                if received.get("request_key") != request_key:
                    raise ValueError("a received provider response has the wrong key")
                receipt, actual, usage = self._completed_receipt(
                    {
                        **request,
                        "state": "submitted",
                        "input_tokens": received.get("input_tokens"),
                    },
                    received.get("response"),
                )
                atomic_json(final_path, receipt, immutable=True)
            else:
                actual = None
                usage = None
                receipt = {
                    **request,
                    "state": "ambiguous_charge",
                    "error": "interrupted request has no durable provider response",
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                }
                atomic_json(final_path, receipt, immutable=True)
            self._settle(request_key, actual=actual, usage=usage)

    def execute(
        self,
        *,
        phase: str,
        run_id: str,
        stage: str,
        paper_id: str,
        family_id: str,
        source_version_id: str,
        request_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if sha256_file(self.price_config_file) != self.active_price_config_sha256:
            raise ValueError("the active price configuration changed after startup")
        if phase not in PHASES or stage not in STAGES:
            raise ValueError("the paid request phase or stage is unsupported")
        if not all((run_id, paper_id, family_id, source_version_id, request_key)):
            raise ValueError("the paid request identity is incomplete")
        if not re.fullmatch(r"[a-f0-9]{64}", request_key):
            raise ValueError("the paid request key must be a lowercase SHA-256 value")
        _validate_payload(payload, self.config)
        expected_key = broker_request_key(
            model=self.config["model"],
            run_id=run_id,
            stage=stage,
            paper_id=paper_id,
            family_id=family_id,
            source_version_id=source_version_id,
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
            "source_version_id": source_version_id,
            "model": self.config["model"],
            "gate_sha256": sha256_file(self.execution_gate_file),
            "price_config_sha256": self.active_price_config_sha256,
            "policy_sha256": sha256_file(self.policy_file),
            "config_transition_sha256": self._config_transition_sha256,
        }
        operation = self._operation_lock_file.open("a+")
        try:
            fcntl.flock(operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            operation.close()
            raise ValueError("another paid broker operation is active") from error
        try:
            self._recover_orphans()
            gate = _validate_gate(self.execution_gate_file, phase)
            self._validate_stream_input_binding(
                gate, self._stream_input_binding, request_run_id=run_id
            )
            self._pace()
            client = self.transport or GeminiTransport(
                self.config["api_base"], _load_key(self.credential_file)
            )
            exact_input = self._resume_not_submitted(request_key, base)
            resumed = exact_input is not None
            if exact_input is None:
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
                        raise ValueError(
                            "countTokens did not return a nonnegative integer"
                        )
                except Exception as error:
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
                    self._mark_not_submitted(
                        request_key, "count_error", f"{type(error).__name__}: {error}"
                    )
                    self._halt(f"countTokens error: {type(error).__name__}")
                    return receipt
            event_stem = (
                f"{request_key}.resume-{self._config_transition_sha256}"
                if resumed
                else request_key
            )
            if exact_input > int(self.config["maximum_input_tokens"]):
                receipt = {
                    **base,
                    "state": "too_large_not_ready",
                    "input_tokens": exact_input,
                    "live_call_made": False,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{event_stem}.json",
                    receipt,
                    immutable=True,
                )
                self._mark_not_submitted(
                    request_key,
                    "too_large_not_ready",
                    "counted request exceeds the model input limit",
                )
                self._halt("counted request exceeds the model input limit")
                return receipt
            output_limit = int(payload["generationConfig"]["maxOutputTokens"])
            reserved = _cost(self.config, exact_input, output_limit)
            try:
                self._reserve(
                    request_key=request_key,
                    phase=phase,
                    paper_id=paper_id,
                    family_id=family_id,
                    stage=stage,
                    reserved=reserved,
                )
            except ValueError as error:
                receipt = {
                    **base,
                    "state": "not_submitted",
                    "input_tokens": exact_input,
                    "reason": str(error),
                    "live_call_made": False,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{event_stem}.json",
                    receipt,
                    immutable=True,
                )
                self._mark_not_submitted(request_key, "not_submitted", str(error))
                return receipt
            submitted = {
                **base,
                "state": "submitted",
                "input_tokens": exact_input,
                "reserved_usd": str(reserved),
                "submitted_at_utc": _now(),
            }
            atomic_json(
                self.receipts_dir / f"{event_stem}.submitted.json",
                submitted,
                immutable=True,
            )
            try:
                response = client.post(self.config["model"], "generateContent", payload)
            except urllib.error.HTTPError as error:
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
                    self.receipts_dir / f"{event_stem}.json",
                    receipt,
                    immutable=True,
                )
                self._settle(request_key, actual=None, usage=None)
                return receipt
            except Exception as error:
                receipt = {
                    **submitted,
                    "state": "ambiguous_charge",
                    "error": f"{type(error).__name__}: provider outcome unknown",
                    "live_call_made": True,
                    "completed_at_utc": _now(),
                }
                atomic_json(
                    self.receipts_dir / f"{event_stem}.json",
                    receipt,
                    immutable=True,
                )
                self._settle(request_key, actual=None, usage=None)
                return receipt
            received = {
                **submitted,
                "state": "response_received",
                "response": response,
                "live_call_made": True,
                "received_at_utc": _now(),
            }
            atomic_json(
                self.receipts_dir / f"{event_stem}.received.json",
                received,
                immutable=True,
            )
            receipt, actual, usage = self._completed_receipt(submitted, response)
            atomic_json(
                self.receipts_dir / f"{event_stem}.json",
                receipt,
                immutable=True,
            )
            self._settle(request_key, actual=actual, usage=usage)
            return receipt
        finally:
            operation.close()
