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
from .errors import AmbiguousChargeError, BudgetError, ProviderError
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
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ProviderError("GEMINI_API_KEY is not set")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(self.model, safe='')}:generateContent"
        body = canonical_json(
            {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": parameters.get("temperature", 0),
                    "maxOutputTokens": parameters.get("max_tokens", 2048),
                    "responseMimeType": "application/json",
                    "responseJsonSchema": parameters["json_schema"],
                },
            }
        ).encode()
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"content-type": "application/json", "x-goog-api-key": key},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read())
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
        usage = raw.get("usageMetadata", {})
        return ProviderResult(
            json.loads(_json_object(text)),
            self.model,
            raw.get("responseId"),
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
            None,
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
        return GeminiProvider(model)
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
    schema_required: set[str],
    reservation: Decimal,
    timeout: float,
    retries: int,
    rate_limit_seconds: float,
) -> ProviderResult:
    prompt_hash = stable_id("prompt", system, prompt, prompt_version)
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
    if ambiguous:
        raise AmbiguousChargeError(
            f"The prior {role} request has an ambiguous charge receipt. Manual reconciliation is required."
        )
    for attempt in range(1, retries + 2):
        call_id = stable_id("call", run_id, entity_id, role, prompt_hash, attempt)
        _reserve(db, run_id, reservation)
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
                    str(reservation),
                    now(),
                ),
            )
        try:
            result = provider.invoke(role, system, prompt, parameters, timeout)
            if not isinstance(result.payload, dict):
                raise ValueError("structured response must be a JSON object")
            missing = schema_required - result.payload.keys()
            if missing:
                raise ValueError(
                    f"structured response is missing fields: {sorted(missing)}"
                )
        except urllib.error.HTTPError as error:
            _settle(db, run_id, reservation, Decimal("0"))
            status = "retryable" if error.code == 429 else "failed"
            _update_call_error(db, call_id, status, f"HTTP_{error.code}", str(error))
            if error.code == 429 and attempt <= retries:
                if rate_limit_seconds:
                    time.sleep(rate_limit_seconds)
                continue
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
            _settle(db, run_id, reservation, Decimal("0"))
            _update_call_error(
                db, call_id, "retryable", "MALFORMED_RESPONSE", str(error)
            )
            if attempt <= retries:
                continue
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
        budget_mode = db.one("SELECT mode FROM budgets WHERE run_id=?", (run_id,))[
            "mode"
        ]
        if budget_mode == "tokens":
            if result.input_tokens is None or result.output_tokens is None:
                _update_call_error(
                    db,
                    call_id,
                    "failed",
                    "UNKNOWN_TOKEN_USAGE",
                    "provider did not return token usage",
                )
                raise BudgetError(
                    "The provider did not return token usage for token-budget mode."
                )
            actual = Decimal(result.input_tokens + result.output_tokens)
        else:
            actual = result.actual_cost_usd
        if actual is None:
            if budget_mode != "tokens":
                _update_call_error(
                    db,
                    call_id,
                    "failed",
                    "UNKNOWN_PRICE",
                    "provider did not return cost",
                )
                raise BudgetError(
                    "The provider price is unknown. Use explicit token-budget mode or configured pricing."
                )
        _settle(db, run_id, reservation, actual)
        with db.transaction():
            db.connection.execute(
                """UPDATE calls SET status='completed',completed_at=?,returned_model=?,request_id=?,
                input_tokens=?,output_tokens=?,actual_cost_usd=?,response_json=? WHERE call_id=?""",
                (
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
        if rate_limit_seconds:
            time.sleep(rate_limit_seconds)
        return result
    raise ProviderError("provider retries were exhausted")


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
        source_text = json.loads(match.group(1))["text"]
    except (json.JSONDecodeError, KeyError, TypeError):
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
                start = source_text.find(quote)
                locator["start_offset"] = start
                locator["end_offset"] = start + len(quote) if start >= 0 else -1
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return value
