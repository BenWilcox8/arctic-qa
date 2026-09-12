from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .db import Database, now
from .errors import (
    AmbiguousChargeError,
    BudgetError,
    BudgetOverageError,
    ProviderError,
)
from .util import canonical_json, redact, stable_id


@dataclass(frozen=True)
class ProviderResult:
    payload: dict[str, Any]
    returned_model: str
    request_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    actual_cost_usd: Decimal | None


class Provider(Protocol):
    name: str
    model: str

    def invoke(
        self,
        role: str,
        system: str,
        prompt: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> ProviderResult: ...


class FakeProvider:
    name = "fake"

    def __init__(self, model: str, script: Path):
        self.model = model
        self.events = [
            json.loads(line)
            for line in script.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.position = 0

    def invoke(
        self,
        role: str,
        system: str,
        prompt: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> ProviderResult:
        if self.position >= len(self.events):
            raise ProviderError(f"fake provider script has no event for role {role}")
        event = self.events[self.position]
        self.position += 1
        if event.get("role") not in (None, role):
            raise ProviderError(
                f"fake provider expected role {event.get('role')}, received {role}"
            )
        if any(
            value not in prompt for value in event.get("require_prompt_contains", [])
        ):
            raise ProviderError(
                f"fake provider prompt for {role} is missing required markers"
            )
        if any(value in prompt for value in event.get("forbid_prompt_contains", [])):
            raise ProviderError(
                f"fake provider prompt for {role} contains a forbidden marker"
            )
        kind = event.get("kind", "response")
        if kind == "timeout":
            raise TimeoutError("simulated provider timeout")
        if kind == "429":
            error = urllib.error.HTTPError(
                "https://fake.invalid", 429, "rate limited", {}, None
            )
            raise error
        if kind == "malformed":
            raise json.JSONDecodeError("simulated malformed JSON", "{", 1)
        if kind == "error":
            raise ProviderError(str(event.get("message", "simulated provider error")))
        response_payload = json.loads(canonical_json(event["response"]))
        match = re.search(r'"chunk_id":"([^"]+)"', prompt)
        if match:
            response_payload = _replace(
                response_payload, "{{chunk_id}}", match.group(1)
            )
        response_payload = _hydrate_locators(response_payload, prompt)
        return ProviderResult(
            payload=response_payload,
            returned_model=event.get("returned_model", self.model),
            request_id=event.get("request_id", f"fake-{self.position}"),
            input_tokens=event.get("input_tokens", 10),
            output_tokens=event.get("output_tokens", 10),
            actual_cost_usd=Decimal(str(event["actual_cost_usd"]))
            if event.get("actual_cost_usd") is not None
            else Decimal("0"),
        )


class ReplayProvider(FakeProvider):
    name = "replay"


class ClaudeProvider:
    name = "claude"

    def __init__(self, model: str):
        self.model = model

    def invoke(
        self,
        role: str,
        system: str,
        prompt: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> ProviderResult:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        body = canonical_json(
            {
                "model": self.model,
                "max_tokens": parameters.get("max_tokens", 2048),
                "temperature": parameters.get("temperature", 0),
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "output_config": {
                    "format": {
                        "type": "json_schema",
                        "schema": parameters["json_schema"],
                    }
                },
            }
        ).encode()
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            method="POST",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read())
        text = "".join(
            block.get("text", "")
            for block in raw.get("content", [])
            if block.get("type") == "text"
        )
        payload = json.loads(_json_object(text))
        usage = raw.get("usage", {})
        return ProviderResult(
            payload,
            raw.get("model", self.model),
            raw.get("id"),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            None,
        )


class GeminiProvider:
    name = "gemini"

    def __init__(self, model: str):
        self.model = model

    def invoke(
        self,
        role: str,
        system: str,
        prompt: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> ProviderResult:
        raise ProviderError(
            "legacy Gemini generation is disabled; use the shared streaming broker"
        )


def make_provider(name: str, model: str, script: Path | None) -> Provider:
    if name == "fake":
        if not script:
            raise ValueError("the fake provider requires --provider-script")
        return FakeProvider(model, script)
    if name == "replay":
        if not script:
            raise ValueError("the replay provider requires --provider-script")
        return ReplayProvider(model, script)
    if name == "claude":
        return ClaudeProvider(model)
    if name == "gemini":
        raise ValueError(
            "legacy Gemini generation is disabled; use the shared streaming broker"
        )
    raise ValueError(f"unknown provider: {name}")


def ensure_budget(db: Database, run_id: str, mode: str, limit_value: Decimal) -> None:
    if limit_value <= 0:
        raise BudgetError("Live provider mode requires an explicit nonzero budget.")
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO budgets(run_id,mode,limit_value,reserved_value,spent_value,updated_at)
            VALUES (?,?,?,'0','0',?)""",
            (run_id, mode, str(limit_value), now()),
        )
    existing = db.one("SELECT mode,limit_value FROM budgets WHERE run_id=?", (run_id,))
    if existing["mode"] != mode or Decimal(existing["limit_value"]) != limit_value:
        raise BudgetError("A resumed run must keep its original budget mode and limit.")


def _reserve(db: Database, run_id: str, amount: Decimal) -> None:
    budget = db.one("SELECT * FROM budgets WHERE run_id=?", (run_id,))
    if not budget:
        raise BudgetError("The run has no budget record.")
    available = (
        Decimal(budget["limit_value"])
        - Decimal(budget["reserved_value"])
        - Decimal(budget["spent_value"])
    )
    if amount > available:
        raise BudgetError(
            f"The request reservation {amount} exceeds the available budget {available}."
        )
    with db.transaction():
        db.connection.execute(
            "UPDATE budgets SET reserved_value=?,updated_at=? WHERE run_id=?",
            (str(Decimal(budget["reserved_value"]) + amount), now(), run_id),
        )


def _settle(db: Database, run_id: str, reserved: Decimal, spent: Decimal) -> None:
    budget = db.one("SELECT * FROM budgets WHERE run_id=?", (run_id,))
    with db.transaction():
        db.connection.execute(
            "UPDATE budgets SET reserved_value=?,spent_value=?,updated_at=? WHERE run_id=?",
            (
                str(max(Decimal("0"), Decimal(budget["reserved_value"]) - reserved)),
                str(Decimal(budget["spent_value"]) + spent),
                now(),
                run_id,
            ),
        )


def call_provider(
    db: Database,
    provider: Provider,
    *,
    run_id: str,
    entity_id: str,
    role: str,
    system: str,
    prompt: str,
    prompt_version: str,
    parameters: dict[str, Any],
    response_schema: dict[str, Any],
    reservation: Decimal,
    timeout: float,
    retries: int,
    rate_limit_seconds: float,
) -> ProviderResult:
    prompt_hash = provider_prompt_hash(
        provider, system, prompt, prompt_version, parameters
    )
    completed = db.one(
        """SELECT * FROM calls WHERE run_id=? AND entity_id=? AND role=? AND prompt_hash=? AND status='completed'
        ORDER BY attempt DESC LIMIT 1""",
        (run_id, entity_id, role, prompt_hash),
    )
    if completed:
        return ProviderResult(
            json.loads(completed["response_json"]),
            completed["returned_model"],
            completed["request_id"],
            completed["input_tokens"],
            completed["output_tokens"],
            Decimal(completed["actual_cost_usd"])
            if completed["actual_cost_usd"] is not None
            else None,
        )
    ambiguous = db.one(
        """SELECT call_id FROM calls WHERE run_id=? AND entity_id=? AND role=? AND prompt_hash=? AND status='ambiguous_charge' LIMIT 1""",
        (run_id, entity_id, role, prompt_hash),
    )
    resume_reconciled = getattr(provider, "resume_reconciled", None)
    if ambiguous and not (
        getattr(provider, "externally_metered", False) and callable(resume_reconciled)
    ):
        raise AmbiguousChargeError(
            f"The prior {role} request has an ambiguous charge receipt. Manual reconciliation is required."
        )
    if getattr(provider, "externally_metered", False):
        return _call_externally_metered(
            db,
            provider,
            run_id=run_id,
            entity_id=entity_id,
            role=role,
            system=system,
            prompt=prompt,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            parameters=parameters,
            response_schema=response_schema,
            timeout=timeout,
            retries=retries,
            rate_limit_seconds=rate_limit_seconds,
            resume_ambiguous=ambiguous is not None,
        )
    budget_mode = db.one("SELECT mode FROM budgets WHERE run_id=?", (run_id,))["mode"]
    attempt_bound = _request_bound(budget_mode, system, prompt, parameters, reservation)
    total_bound = attempt_bound * Decimal(retries + 1)
    _reserve(db, run_id, total_bound)
    spent = Decimal("0")
    for attempt in range(1, retries + 2):
        call_id = stable_id("call", run_id, entity_id, role, prompt_hash, attempt)
        with db.transaction():
            db.connection.execute(
                """INSERT INTO calls
                (call_id,run_id,entity_id,role,provider,requested_model,prompt_version,prompt_hash,
                 parameters_json,attempt,status,reserved_cost_usd,started_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'started',?,?)""",
                (
                    call_id,
                    run_id,
                    entity_id,
                    role,
                    provider.name,
                    provider.model,
                    prompt_version,
                    prompt_hash,
                    canonical_json(parameters),
                    attempt,
                    str(attempt_bound),
                    now(),
                ),
            )
        result: ProviderResult | None = None
        try:
            result = provider.invoke(role, system, prompt, parameters, timeout)
            _validate_schema(result.payload, response_schema)
        except urllib.error.HTTPError as error:
            status = "retryable" if error.code == 429 else "failed"
            _update_call_error(db, call_id, status, f"HTTP_{error.code}", str(error))
            if error.code == 429 and attempt <= retries:
                if rate_limit_seconds:
                    time.sleep(rate_limit_seconds)
                continue
            _settle(db, run_id, total_bound, spent)
            raise ProviderError(f"provider HTTP error {error.code}") from error
        except TimeoutError as error:
            with db.transaction():
                db.connection.execute(
                    "UPDATE calls SET status='ambiguous_charge',completed_at=?,error_code='TIMEOUT',error_text=? WHERE call_id=?",
                    (now(), redact(str(error)), call_id),
                )
            raise AmbiguousChargeError(
                "The provider request timed out after dispatch. The reserved budget remains held."
            ) from error
        except (json.JSONDecodeError, ValueError) as error:
            charged = _reported_usage(result, budget_mode) or attempt_bound
            spent += charged
            status = "retryable" if attempt <= retries else "failed"
            _update_call_error(db, call_id, status, "MALFORMED_RESPONSE", str(error))
            with db.transaction():
                db.connection.execute(
                    "UPDATE calls SET input_tokens=?,output_tokens=?,actual_cost_usd=? WHERE call_id=?",
                    (
                        result.input_tokens if result else None,
                        result.output_tokens if result else None,
                        str(charged),
                        call_id,
                    ),
                )
            if attempt <= retries:
                continue
            _settle(db, run_id, total_bound, spent)
            raise ProviderError(
                "The provider returned invalid structured JSON."
            ) from error
        except Exception as error:
            with db.transaction():
                db.connection.execute(
                    "UPDATE calls SET status='ambiguous_charge',completed_at=?,error_code='TRANSPORT_UNKNOWN',error_text=? WHERE call_id=?",
                    (now(), redact(str(error)), call_id),
                )
            raise AmbiguousChargeError(
                "The provider request ended without a confirmed charge state."
            ) from error
        actual = _reported_usage(result, budget_mode)
        if actual is None:
            spent += attempt_bound
            _settle(db, run_id, total_bound, spent)
            code = "UNKNOWN_TOKEN_USAGE" if budget_mode == "tokens" else "UNKNOWN_COST"
            _update_call_error(
                db, call_id, "failed", code, "provider did not return billable usage"
            )
            raise BudgetError("The provider did not return billable usage.")
        spent += actual
        overage = actual > attempt_bound or spent > total_bound
        _settle(db, run_id, total_bound, spent)
        with db.transaction():
            db.connection.execute(
                """UPDATE calls SET status=?,completed_at=?,returned_model=?,request_id=?,
                input_tokens=?,output_tokens=?,actual_cost_usd=?,response_json=? WHERE call_id=?""",
                (
                    "budget_overage" if overage else "completed",
                    now(),
                    result.returned_model,
                    result.request_id,
                    result.input_tokens,
                    result.output_tokens,
                    str(actual),
                    canonical_json(result.payload),
                    call_id,
                ),
            )
        if overage:
            raise BudgetOverageError(
                "Reported provider usage exceeded the conservative pre-dispatch bound. Further calls are stopped."
            )
        if rate_limit_seconds:
            time.sleep(rate_limit_seconds)
        return result
    raise ProviderError("provider retries were exhausted")


def provider_prompt_hash(
    provider: Provider,
    system: str,
    prompt: str,
    prompt_version: str,
    parameters: dict[str, Any],
) -> str:
    identity = getattr(provider, "request_identity", None)
    return stable_id(
        "prompt",
        system,
        prompt,
        prompt_version,
        provider.name,
        provider.model,
        identity() if callable(identity) else None,
        parameters,
    )


def _call_externally_metered(
    db: Database,
    provider: Provider,
    *,
    run_id: str,
    entity_id: str,
    role: str,
    system: str,
    prompt: str,
    prompt_version: str,
    prompt_hash: str,
    parameters: dict[str, Any],
    response_schema: dict[str, Any],
    timeout: float,
    retries: int,
    rate_limit_seconds: float,
    resume_ambiguous: bool,
) -> ProviderResult:
    if retries != 0 or rate_limit_seconds != 0:
        raise ValueError("the shared broker does not permit local retries or pacing")
    call_id = stable_id("call", run_id, entity_id, role, prompt_hash, 1)
    existing = db.one("SELECT status FROM calls WHERE call_id=?", (call_id,))
    allowed_existing = {"started", "failed"}
    if resume_ambiguous:
        allowed_existing.add("ambiguous_charge")
    if existing and existing["status"] not in allowed_existing:
        raise ProviderError("The shared-broker call journal has an invalid state.")
    if not existing:
        with db.transaction():
            db.connection.execute(
                """INSERT INTO calls
                (call_id,run_id,entity_id,role,provider,requested_model,prompt_version,prompt_hash,
                 parameters_json,attempt,status,started_at)
                VALUES (?,?,?,?,?,?,?,?,?,1,'started',?)""",
                (
                    call_id,
                    run_id,
                    entity_id,
                    role,
                    provider.name,
                    provider.model,
                    prompt_version,
                    prompt_hash,
                    canonical_json(parameters),
                    now(),
                ),
            )
    try:
        if resume_ambiguous:
            resume = getattr(provider, "resume_reconciled", None)
            if not callable(resume):
                raise AmbiguousChargeError(
                    "The shared-broker call has no safe reconciliation path."
                )
            result = resume(role, system, prompt, parameters, timeout)
        else:
            result = provider.invoke(role, system, prompt, parameters, timeout)
        _validate_schema(result.payload, response_schema)
    except AmbiguousChargeError as error:
        _update_call_error(
            db, call_id, "ambiguous_charge", error.code, redact(str(error))
        )
        raise
    except (BudgetError, ProviderError, json.JSONDecodeError, ValueError) as error:
        _update_call_error(
            db,
            call_id,
            "failed",
            getattr(error, "code", "BROKER_RESPONSE_INVALID"),
            redact(str(error)),
        )
        raise
    except Exception as error:
        _update_call_error(
            db,
            call_id,
            "ambiguous_charge",
            "BROKER_OUTCOME_UNKNOWN",
            redact(str(error)),
        )
        raise AmbiguousChargeError(
            "The shared broker request ended without a usable receipt."
        ) from error
    with db.transaction():
        db.connection.execute(
            """UPDATE calls SET status='completed',completed_at=?,returned_model=?,
            request_id=?,input_tokens=?,output_tokens=?,actual_cost_usd=?,response_json=?,
            error_code=NULL,error_text=NULL
            WHERE call_id=?""",
            (
                now(),
                result.returned_model,
                result.request_id,
                result.input_tokens,
                result.output_tokens,
                str(result.actual_cost_usd)
                if result.actual_cost_usd is not None
                else None,
                canonical_json(result.payload),
                call_id,
            ),
        )
    return result


def _request_bound(
    mode: str,
    system: str,
    prompt: str,
    parameters: dict[str, Any],
    user_floor: Decimal,
) -> Decimal:
    try:
        maximum_output = Decimal(str(parameters["max_tokens"]))
        configured_reasoning = Decimal(str(parameters.get("reasoning_token_cap", 0)))
        configured_overhead = Decimal(
            str(parameters.get("billable_token_overhead", 1024))
        )
    except Exception as error:
        raise BudgetError("A numeric provider usage cap is required.") from error
    if maximum_output <= 0 or configured_reasoning < 0 or configured_overhead < 0:
        raise BudgetError("Provider usage caps must be nonnegative.")
    reasoning = max(configured_reasoning, maximum_output)
    overhead = max(configured_overhead, Decimal("1024"))
    request_bytes = len(
        canonical_json(
            {"system": system, "prompt": prompt, "parameters": parameters}
        ).encode("utf-8")
    )
    input_token_bound = Decimal(request_bytes) + overhead
    token_bound = input_token_bound + maximum_output + reasoning
    if mode == "tokens":
        return max(user_floor, token_bound)
    pricing = parameters.get("pricing_usd_per_million_tokens")
    required_prices = {"input", "output", "reasoning"}
    if not isinstance(pricing, dict) or required_prices - pricing.keys():
        raise BudgetError(
            "USD mode requires configured input, output, and reasoning prices before dispatch."
        )
    try:
        cost_bound = (
            input_token_bound * Decimal(str(pricing["input"]))
            + maximum_output * Decimal(str(pricing["output"]))
            + reasoning * Decimal(str(pricing["reasoning"]))
        ) / Decimal("1000000")
    except Exception as error:
        raise BudgetError("Configured provider prices must be numeric.") from error
    if cost_bound < 0:
        raise BudgetError("Configured provider prices cannot be negative.")
    return max(user_floor, cost_bound)


def _reported_usage(result: ProviderResult | None, mode: str) -> Decimal | None:
    if result is None:
        return None
    if mode == "tokens":
        if result.input_tokens is None or result.output_tokens is None:
            return None
        return Decimal(result.input_tokens + result.output_tokens)
    return result.actual_cost_usd


def _update_call_error(
    db: Database, call_id: str, status: str, code: str, text: str
) -> None:
    with db.transaction():
        db.connection.execute(
            "UPDATE calls SET status=?,completed_at=?,error_code=?,error_text=? WHERE call_id=?",
            (status, now(), code, redact(text), call_id),
        )


def _json_object(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    return value


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    expected = schema.get("type")
    if expected:
        expected_types = expected if isinstance(expected, list) else [expected]
        predicates = {
            "object": lambda item: isinstance(item, dict),
            "array": lambda item: isinstance(item, list),
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: (
                isinstance(item, int) and not isinstance(item, bool)
            ),
            "number": lambda item: (
                isinstance(item, (int, float)) and not isinstance(item, bool)
            ),
            "boolean": lambda item: isinstance(item, bool),
            "null": lambda item: item is None,
        }
        if not any(predicates[kind](value) for kind in expected_types):
            raise ValueError(f"{path} must have type {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} must equal {schema['const']}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise ValueError(f"{path} is shorter than the minimum length")
    if isinstance(value, dict):
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise ValueError(f"{path} is missing fields: {sorted(missing)}")
        properties = schema.get("properties", {})
        for key, subschema in properties.items():
            if key in value:
                _validate_schema(value[key], subschema, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extras = value.keys() - properties.keys()
            if extras:
                raise ValueError(f"{path} has unexpected fields: {sorted(extras)}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{path} has too few items")
        for index, item in enumerate(value):
            _validate_schema(item, schema.get("items", {}), f"{path}[{index}]")


def _replace(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {key: _replace(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, old, new) for item in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def _hydrate_locators(value: Any, prompt: str) -> Any:
    match = re.search(r"SOURCE_DATA_BEGIN\n(.*?)\nSOURCE_DATA_END", prompt, flags=re.S)
    if not match:
        return value
    try:
        source_data = json.loads(match.group(1))
    except (json.JSONDecodeError, KeyError, TypeError):
        return value
    if isinstance(source_data, dict) and isinstance(source_data.get("text"), str):
        source_chunks = [source_data]
    elif isinstance(source_data, dict) and isinstance(source_data.get("chunks"), list):
        source_chunks = source_data["chunks"]
    else:
        return value

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            quote = item.get("evidence_quote")
            locator = item.get("locator")
            if (
                isinstance(quote, str)
                and isinstance(locator, dict)
                and locator.get("start_offset") == "auto"
            ):
                matching = next(
                    (
                        chunk
                        for chunk in source_chunks
                        if isinstance(chunk, dict)
                        and isinstance(chunk.get("text"), str)
                        and quote in chunk["text"]
                    ),
                    None,
                )
                if matching is not None:
                    start = matching["text"].find(quote)
                    locator["chunk_id"] = matching.get("chunk_id")
                    locator["start_offset"] = start
                    locator["end_offset"] = start + len(quote)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return value
