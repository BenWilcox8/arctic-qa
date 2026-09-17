"""Provider interface for the abstention evaluation.

One provider answers one rendered trial in one isolated session. The Google
Gemini implementation sends every call through the shared paid-call broker
under the ``benchmark_evaluation`` phase. The scripted transport lets the whole
harness run end to end with no paid call. The interface stays open for other
providers: implement :class:`EvaluationProvider` and register it in
:data:`PROVIDER_NAMES`.

Captain decision 7 (2026-09-16): Google AI models only for now, because the
budget is Google API credits; leave the door open for other models later.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .abstention_render import ABSTENTION_OPTION_TEXT, PROMPT_VERSION, prompt_sha256
from .model_broker import (
    EVALUATION_PHASE,
    SharedGeminiBroker,
    broker_request_key,
    evaluation_stage,
)
from .util import canonical_json, sha256_bytes, sha256_file, stable_id


PROVIDER_GOOGLE_GEMINI = "google_gemini"
PROVIDER_SCRIPTED = "scripted"
PROVIDER_NAMES = (PROVIDER_GOOGLE_GEMINI, PROVIDER_SCRIPTED)
RESPONSE_MIME_TYPE = "text/x.enum"
# Output caps per thinking arm. The cap includes thinking tokens (see
# docs/GEMINI_STRUCTURED_OUTPUT_BUDGET.md); a MAX_TOKENS finish is invalid (N0).
DEFAULT_OUTPUT_TOKENS_BY_ARM = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
}
COMPLETED = "completed"
SCRIPTED_POLICIES = ("gold", "abstain", "random", "always_a", "invalid")


@dataclass(frozen=True)
class EvaluationRequest:
    """One rendered trial and the ledger identity of its item."""

    trial: dict[str, Any]
    identity: dict[str, str]
    run_id: str


@dataclass(frozen=True)
class EvaluationResponse:
    """What one provider call returned, with its custody references."""

    state: str
    raw_text: str | None
    finish_reason: str | None
    usage: dict[str, int] | None
    cost_usd: str | None
    latency_seconds: float | None
    request_key: str | None
    request_sha256: str | None
    receipt_sha256: str | None
    receipt_file: str | None
    model_version: str | None
    response_id: str | None
    error: str | None
    resumed: bool = False
    # Subscription providers record the vendor, the exact model id, the
    # preset, the harness invocation and the raw final text here. The Gemini
    # provider leaves it None.
    harness: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "raw_text": self.raw_text,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "latency_seconds": self.latency_seconds,
            "request_key": self.request_key,
            "request_sha256": self.request_sha256,
            "receipt_sha256": self.receipt_sha256,
            "receipt_file": self.receipt_file,
            "model_version": self.model_version,
            "response_id": self.response_id,
            "error": self.error,
            "resumed": self.resumed,
            "harness": self.harness,
        }


class EvaluationProvider(Protocol):
    """One isolated call per trial. No history, no tools, no retry."""

    name: str

    def supports_enum_output(self, model: str) -> bool: ...

    def decoding(self, model: str, arm: str) -> dict[str, Any]: ...

    def answer(self, request: EvaluationRequest) -> EvaluationResponse: ...


def decoding_record(
    price_config: dict[str, Any],
    models: list[str],
    arms: list[str],
    output_tokens_by_arm: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Return the decoding record that the gate and the run binding share."""
    caps = dict(DEFAULT_OUTPUT_TOKENS_BY_ARM)
    caps.update(output_tokens_by_arm or {})
    entries = price_config["models"]
    for model in models:
        if model not in entries:
            raise ValueError(f"the evaluated model has no price entry: {model}")
        for arm in arms:
            if arm not in caps or arm not in entries[model]["thinking_levels"]:
                raise ValueError(
                    f"the thinking arm {arm} is not an official preset of {model}"
                )
    return {
        "candidate_count": 1,
        "response_mime_type": RESPONSE_MIME_TYPE,
        "temperature_by_model": {
            model: str(entries[model]["temperature"]) for model in sorted(models)
        },
        "temperature_rule": "API maximum for the model",
        "thinking_control": "thinkingConfig.thinkingLevel from the official presets",
        "max_output_tokens_by_arm": {
            arm: min(
                int(caps[arm]),
                min(int(entries[model]["maximum_output_tokens"]) for model in models),
            )
            for arm in sorted(arms)
        },
    }


def evaluation_payload(
    *,
    system_text: str,
    user_text: str,
    letters: str,
    temperature: str,
    max_output_tokens: int,
    arm: str,
) -> dict[str, Any]:
    """Return the exact broker payload for one trial: enum-constrained letter."""
    return {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "candidateCount": 1,
            "temperature": float(Decimal(temperature)),
            "responseMimeType": RESPONSE_MIME_TYPE,
            "responseJsonSchema": {"type": "string", "enum": list(letters)},
            "maxOutputTokens": max_output_tokens,
            "thinkingConfig": {"thinkingLevel": arm},
        },
        "store": False,
    }


def response_text(response: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (text, finish reason) from one raw generateContent response."""
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        return None, None
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return None, None
    parts = (candidate.get("content") or {}).get("parts")
    text = None
    if isinstance(parts, list):
        text = "".join(
            str(part.get("text", "")) for part in parts if isinstance(part, dict)
        )
    finish = candidate.get("finishReason")
    return text, str(finish) if finish is not None else None


def _seconds_between(start: Any, end: Any) -> float | None:
    from datetime import datetime

    try:
        first = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        last = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max((last - first).total_seconds(), 0.0)


def response_from_receipt(
    receipt: dict[str, Any],
    *,
    receipt_file: Path | None,
    latency_seconds: float | None,
    resumed: bool,
) -> EvaluationResponse:
    """Translate one immutable broker receipt into an evaluation response."""
    state = str(receipt.get("state") or "unknown")
    raw_text: str | None = None
    finish: str | None = None
    response = receipt.get("response")
    if isinstance(response, dict):
        raw_text, finish = response_text(response)
    usage = receipt.get("usage") if state == COMPLETED else None
    if latency_seconds is None:
        latency_seconds = _seconds_between(
            receipt.get("submitted_at_utc"), receipt.get("completed_at_utc")
        )
    return EvaluationResponse(
        state=state,
        raw_text=raw_text,
        finish_reason=finish,
        usage=dict(usage) if isinstance(usage, dict) else None,
        cost_usd=(
            str(receipt["actual_cost_usd"])
            if receipt.get("actual_cost_usd") is not None
            else None
        ),
        latency_seconds=latency_seconds,
        request_key=receipt.get("request_key"),
        request_sha256=receipt.get("request_sha256"),
        receipt_sha256=sha256_file(receipt_file) if receipt_file else None,
        receipt_file=str(receipt_file) if receipt_file else None,
        model_version=(
            str(response.get("modelVersion"))
            if isinstance(response, dict) and response.get("modelVersion")
            else None
        ),
        response_id=(
            str(response.get("responseId"))
            if isinstance(response, dict) and response.get("responseId")
            else None
        ),
        error=(
            str(receipt.get("error") or receipt.get("reason") or "")
            if state != COMPLETED
            else None
        )
        or None,
        resumed=resumed,
    )


class GeminiBrokerEvaluationProvider:
    """Answer trials through the shared broker under the evaluation phase."""

    name = PROVIDER_GOOGLE_GEMINI

    def __init__(
        self,
        broker: SharedGeminiBroker,
        *,
        run_id: str,
        decoding: dict[str, Any],
        admission: AbstractContextManager[Any] | None = None,
    ) -> None:
        if not broker.evaluation_enabled():
            raise ValueError("the broker has no benchmark evaluation configuration")
        self.broker = broker
        self.run_id = run_id
        self._decoding = decoding
        # The paid-call slots of the evaluation phase belong to the whole
        # process, and this provider belongs to one question. ``admission`` is
        # how the caller paces every question it runs at once under that one
        # limit: see :func:`arctic_qa.abstention_plan.evaluation_admission`.
        # Without it the questions in flight multiply the calls that reach the
        # broker by the questions in flight, and every call past the limit
        # waits out the broker's bounded wait and is refused.
        self._admission = admission

    def supports_enum_output(self, model: str) -> bool:
        return True

    def decoding(self, model: str, arm: str) -> dict[str, Any]:
        temperature = self._decoding["temperature_by_model"].get(model)
        cap = self._decoding["max_output_tokens_by_arm"].get(arm)
        if temperature is None or cap is None:
            raise ValueError("the run decoding record lacks this model or arm")
        return {
            "temperature": str(temperature),
            "max_output_tokens": int(cap),
            "thinking": {"thinkingLevel": arm},
        }

    def payload(self, trial: dict[str, Any]) -> dict[str, Any]:
        decoding = self.decoding(trial["model"], trial["arm"])
        return evaluation_payload(
            system_text=trial["system_text"],
            user_text=trial["user_text"],
            letters=trial["letters"],
            temperature=decoding["temperature"],
            max_output_tokens=decoding["max_output_tokens"],
            arm=trial["arm"],
        )

    def request_key(self, request: EvaluationRequest) -> str:
        trial = request.trial
        return broker_request_key(
            model=trial["model"],
            run_id=request.run_id,
            phase=EVALUATION_PHASE,
            stage=evaluation_stage(trial["model"]),
            paper_id=request.identity["paper_id"],
            family_id=request.identity["family_id"],
            source_version_id=request.identity["source_version_id"],
            payload=self.payload(trial),
            trial_id=trial["trial_id"],
        )

    def answer(self, request: EvaluationRequest) -> EvaluationResponse:
        trial = request.trial
        payload = self.payload(trial)
        request_key = self.request_key(request)
        receipt_path = self.broker.receipts_dir / f"{request_key}.json"
        if receipt_path.is_file():
            receipt = self.broker.effective_receipt(request_key)
            return response_from_receipt(
                receipt,
                receipt_file=self.broker.effective_receipt_path(request_key),
                latency_seconds=None,
                resumed=True,
            )
        started = time.monotonic()
        # The gate is taken around the paid call alone. A replayed receipt
        # above costs no slot, so it must never wait for one.
        with self._admission or nullcontext():
            receipt = self.broker.execute(
                phase=EVALUATION_PHASE,
                run_id=request.run_id,
                stage=evaluation_stage(trial["model"]),
                paper_id=request.identity["paper_id"],
                family_id=request.identity["family_id"],
                source_version_id=request.identity["source_version_id"],
                request_key=request_key,
                payload=payload,
                trial={
                    name: trial[name]
                    for name in (
                        "trial_id",
                        "eval_set_id",
                        "item_id",
                        "condition",
                        "arm",
                        "repeat",
                    )
                },
            )
        latency = time.monotonic() - started
        final_path = self.broker.effective_receipt_path(request_key)
        return response_from_receipt(
            receipt, receipt_file=final_path, latency_seconds=latency, resumed=False
        )


def scripted_key(model: str, payload: dict[str, Any]) -> str:
    """Key one scripted answer by the model and the exact payload."""
    return sha256_bytes(canonical_json({"model": model, "payload": payload}).encode())


class ScriptedTransport:
    """A Gemini transport that answers from a prepared letter map, offline.

    ``answers`` maps :func:`scripted_key` of the model and the exact
    generateContent payload to a scripted event: ``{"text": "B"}`` or a richer
    record with ``finish_reason`` and token counts. The map is keyed by model
    and payload, not by call order, so a changed call order never breaks the
    script (see the positional-fixture note in the project memory).
    """

    def __init__(
        self, answers: dict[str, dict[str, Any]], *, latency_seconds: float = 0.0
    ) -> None:
        self.answers = answers
        self.latency_seconds = latency_seconds
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def post(self, model: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.calls.append((model, method))
        if method == "countTokens":
            request = body["generateContentRequest"]
            text = canonical_json(request.get("contents"))
            text += canonical_json(request.get("systemInstruction"))
            return {"totalTokens": max(len(text) // 4, 1)}
        if method != "generateContent":
            raise ValueError(f"the scripted transport has no method {method}")
        key = scripted_key(model, body)
        event = self.answers.get(key)
        if event is None:
            raise ValueError("the scripted transport has no answer for this payload")
        latency = float(event.get("latency_seconds") or self.latency_seconds or 0)
        if latency > 0:
            time.sleep(latency)
        if event.get("raise") == "timeout":
            raise TimeoutError("scripted provider timeout")
        if event.get("raise") == "http":
            raise urllib.error.HTTPError(
                "https://scripted.invalid",
                int(event.get("status", 503)),
                "scripted",
                None,
                None,
            )
        prompt = int(event.get("prompt_tokens", 200))
        candidates = int(event.get("candidate_tokens", 1))
        thoughts = int(event.get("thinking_tokens", 40))
        parts = [{"text": event["text"]}] if event.get("text") is not None else []
        return {
            "responseId": f"scripted-{key[:12]}",
            "modelVersion": model,
            "candidates": [
                {
                    "content": {"parts": parts, "role": "model"},
                    "finishReason": event.get("finish_reason", "STOP"),
                }
            ],
            "usageMetadata": {
                "promptTokenCount": prompt,
                "candidatesTokenCount": candidates,
                "thoughtsTokenCount": thoughts,
                "totalTokenCount": prompt + candidates + thoughts,
            },
        }


def scripted_letter(trial: dict[str, Any], policy: str, *, seed: str) -> dict[str, Any]:
    """Return the scripted event for one trial under one answer policy."""
    if policy == "gold":
        letter = trial["gold_letter"] or trial["abstain_letter"]
        return {"text": letter}
    if policy == "abstain":
        return {"text": trial["abstain_letter"]}
    if policy == "always_a":
        return {"text": "A"}
    if policy == "invalid":
        return {"text": "I think the answer is B."}
    if policy == "random":
        digest = stable_id("scripted-random", seed, trial["trial_id"], length=8)
        index = int(digest.rsplit("-", 1)[1], 16) % len(trial["letters"])
        return {"text": trial["letters"][index]}
    raise ValueError(f"unsupported scripted policy: {policy}")


class ScriptedEvaluationProvider:
    """Answer trials from a policy or an explicit per-trial script, no broker."""

    name = PROVIDER_SCRIPTED

    def __init__(
        self,
        *,
        policy: str = "random",
        seed: str = "scripted",
        overrides: dict[str, dict[str, Any]] | None = None,
        temperature: str = "2.0",
    ) -> None:
        if policy not in SCRIPTED_POLICIES:
            raise ValueError(f"unsupported scripted policy: {policy}")
        self.policy = policy
        self.seed = seed
        self.overrides = overrides or {}
        self.temperature = temperature

    def supports_enum_output(self, model: str) -> bool:
        return True

    def decoding(self, model: str, arm: str) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_output_tokens": DEFAULT_OUTPUT_TOKENS_BY_ARM.get(arm, 8192),
            "thinking": {"thinkingLevel": arm},
        }

    def answer(self, request: EvaluationRequest) -> EvaluationResponse:
        trial = request.trial
        event = self.overrides.get(trial["trial_id"]) or scripted_letter(
            trial, self.policy, seed=self.seed
        )
        payload = evaluation_payload(
            system_text=trial["system_text"],
            user_text=trial["user_text"],
            letters=trial["letters"],
            temperature=self.temperature,
            max_output_tokens=self.decoding(trial["model"], trial["arm"])[
                "max_output_tokens"
            ],
            arm=trial["arm"],
        )
        usage = {
            "promptTokenCount": int(event.get("prompt_tokens", 200)),
            "candidatesTokenCount": int(event.get("candidate_tokens", 1)),
            "thoughtsTokenCount": int(event.get("thinking_tokens", 40)),
        }
        usage["totalTokenCount"] = sum(usage.values())
        return EvaluationResponse(
            state=str(event.get("state", COMPLETED)),
            raw_text=event.get("text"),
            finish_reason=str(event.get("finish_reason", "STOP")),
            usage=usage,
            cost_usd="0",
            latency_seconds=0.0,
            request_key=None,
            request_sha256=sha256_bytes(canonical_json(payload).encode()),
            receipt_sha256=None,
            receipt_file=None,
            model_version=trial["model"],
            response_id=f"scripted-{trial['trial_id']}",
            error=event.get("error"),
        )


def run_binding_record(
    *,
    eval_set_id: str,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats: int,
    decoding: dict[str, Any],
) -> dict[str, Any]:
    """Return the fields a run and its gate must agree on."""
    return {
        "eval_set_id": eval_set_id,
        "run_id": run_id,
        "models": sorted(models),
        "arms": sorted(arms),
        "repeats": repeats,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "abstention_option_text": ABSTENTION_OPTION_TEXT,
        "decoding": decoding,
    }


# --- Model enumeration (models.list is free) --------------------------------

_PRO_NAME = re.compile(r"(^|[-_])pro([-_]|$)")


def _load_key(path: Path) -> str:
    from .model_broker import _load_key as load_private_key

    return load_private_key(path)


def fetch_model_list(
    api_base: str, credential_file: Path, *, timeout: float = 60.0
) -> list[dict[str, Any]]:
    """Return every model the configured key can reach (models.list, free)."""
    key = _load_key(credential_file)
    models: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        query = {"pageSize": "200"}
        if page_token:
            query["pageToken"] = page_token
        url = f"{api_base.rstrip('/')}/models?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url, headers={"x-goog-api-key": key}, method="GET"
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            page = json.loads(response.read().decode("utf-8"))
        models.extend(page.get("models") or [])
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return models


def annotate_models(
    models: list[dict[str, Any]], price_config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Join the provider's model list with the local price and preset records."""
    entries = price_config.get("models") or {}
    rows = []
    for record in models:
        name = str(record.get("name") or "")
        model_id = name.split("/", 1)[1] if name.startswith("models/") else name
        entry = entries.get(model_id)
        methods = record.get("supportedGenerationMethods") or []
        rows.append(
            {
                "model": model_id,
                "display_name": record.get("displayName"),
                "version": record.get("version"),
                "is_pro": bool(_PRO_NAME.search(model_id)),
                "is_gemini": model_id.startswith("gemini"),
                "supports_generate_content": "generateContent" in methods,
                "supports_count_tokens": "countTokens" in methods,
                "supports_batch": "batchGenerateContent" in methods,
                "input_token_limit": record.get("inputTokenLimit"),
                "output_token_limit": record.get("outputTokenLimit"),
                "thinking": record.get("thinking"),
                "priced": entry is not None,
                "price_input_usd_per_million": (
                    entry["input_usd_per_million_tokens"] if entry else None
                ),
                "price_output_usd_per_million_including_thinking": (
                    entry["output_usd_per_million_tokens_including_thinking"]
                    if entry
                    else None
                ),
                "thinking_levels": entry["thinking_levels"] if entry else None,
                "temperature": entry["temperature"] if entry else None,
                "lifecycle": entry["lifecycle"] if entry else None,
                "description": record.get("description"),
            }
        )
    rows.sort(key=lambda row: (not row["is_pro"], row["model"]))
    return rows
