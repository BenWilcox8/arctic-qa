from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from .errors import (
    AmbiguousChargeError,
    BudgetError,
    ProviderError,
    ProviderResponseError,
)
from .model_broker import AUTHORIZED_CAP_REASON, SharedGeminiBroker, broker_request_key
from .providers import ProviderResult
from .util import canonical_json, sha256_bytes


ROLE_STAGES = {
    "eligibility": "eligibility",
    "extractor": "finding_answer_extraction",
    "question_writer": "question_generation",
    "direct_joint": "question_generation",
    "reconstructor": "blinded_reconstruction",
    "answer_verifier": "answer_verification",
    "distractor_writer": "distractor_generation",
    "option_verifier": "option_verification",
    "correction": "repair",
}


@dataclass(frozen=True)
class BrokerProvider:
    """Adapt generation roles to the one shared, fail-closed Gemini broker."""

    broker: SharedGeminiBroker
    phase: str
    invocation_run_id: str
    paper_id: str | None = None
    family_id: str | None = None
    source_version_id: str | None = None

    name = "gemini"
    externally_metered = True

    @property
    def model(self) -> str:
        return str(self.broker.config["model"])

    def bind(
        self, *, paper_id: str, family_id: str, source_version_id: str
    ) -> BrokerProvider:
        if not paper_id or not family_id or not source_version_id:
            raise ValueError("the broker provider paper identity is incomplete")
        return replace(
            self,
            paper_id=paper_id,
            family_id=family_id,
            source_version_id=source_version_id,
        )

    def request_identity(self) -> dict[str, str | None]:
        return {
            "paper_id": self.paper_id,
            "family_id": self.family_id,
            "source_version_id": self.source_version_id,
        }

    def record_accepted(self, *, family_id: str, item_id: str) -> dict[str, Any]:
        return self.broker.record_accepted(family_id=family_id, item_id=item_id)

    def invoke(
        self,
        role: str,
        system: str,
        prompt: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> ProviderResult:
        if (
            self.paper_id is None
            or self.family_id is None
            or self.source_version_id is None
        ):
            raise ValueError("the broker provider is not bound to a paper")
        if timeout <= 0:
            raise ValueError("the provider timeout must be positive")
        try:
            stage = ROLE_STAGES[role]
        except KeyError as error:
            raise ValueError(
                f"the generation role has no broker stage: {role}"
            ) from error
        payload = _request_payload(system, prompt, parameters, self.broker.config)
        request_key = broker_request_key(
            model=self.model,
            run_id=self.invocation_run_id,
            phase=self.phase,
            stage=stage,
            paper_id=self.paper_id,
            family_id=self.family_id,
            source_version_id=self.source_version_id,
            payload=payload,
        )
        receipt_path = self.broker.receipts_dir / f"{request_key}.json"
        if receipt_path.is_file():
            receipt = self.broker.effective_receipt(request_key)
            if not (
                receipt.get("state") == "not_submitted"
                and receipt.get("reason") == AUTHORIZED_CAP_REASON
            ):
                _, result = self.read_receipt(
                    request_key=request_key,
                    role=role,
                    request_sha256=sha256_bytes(canonical_json(payload).encode()),
                )
                return result
        receipt = self.broker.execute(
            phase=self.phase,
            run_id=self.invocation_run_id,
            stage=stage,
            paper_id=self.paper_id,
            family_id=self.family_id,
            source_version_id=self.source_version_id,
            request_key=request_key,
            payload=payload,
        )
        return _provider_result(receipt, self.model)

    def read_receipt(
        self,
        *,
        request_key: str,
        role: str,
        request_sha256: str | None = None,
    ) -> tuple[dict[str, Any], ProviderResult]:
        """Read one receipt only after the broker validates its ledger custody."""
        if (
            self.paper_id is None
            or self.family_id is None
            or self.source_version_id is None
        ):
            raise ValueError("the broker provider is not bound to a paper")
        try:
            stage = ROLE_STAGES[role]
        except KeyError as error:
            raise ValueError(
                f"the generation role has no broker stage: {role}"
            ) from error
        receipt = self.broker.effective_receipt(request_key)
        _validate_receipt(
            receipt,
            request_key=request_key,
            request_sha256=request_sha256 or str(receipt.get("request_sha256") or ""),
            stage=stage,
            paper_id=self.paper_id,
            family_id=self.family_id,
            source_version_id=self.source_version_id,
            model=self.model,
        )
        return receipt, _provider_result(receipt, self.model)

    def resume_reconciled(
        self,
        role: str,
        system: str,
        prompt: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> ProviderResult:
        """Resume only from an authenticated reconciliation without transport."""
        if (
            self.paper_id is None
            or self.family_id is None
            or self.source_version_id is None
        ):
            raise ValueError("the broker provider is not bound to a paper")
        if timeout <= 0:
            raise ValueError("the provider timeout must be positive")
        try:
            stage = ROLE_STAGES[role]
        except KeyError as error:
            raise ValueError(
                f"the generation role has no broker stage: {role}"
            ) from error
        payload = _request_payload(system, prompt, parameters, self.broker.config)
        request_key = broker_request_key(
            model=self.model,
            run_id=self.invocation_run_id,
            phase=self.phase,
            stage=stage,
            paper_id=self.paper_id,
            family_id=self.family_id,
            source_version_id=self.source_version_id,
            payload=payload,
        )
        reconciliation_path = (
            self.broker.receipts_dir / f"{request_key}.usage-reconciliation.json"
        )
        if not reconciliation_path.is_file():
            raise AmbiguousChargeError("the broker request has no usage reconciliation")
        _, result = self.read_receipt(
            request_key=request_key,
            role=role,
            request_sha256=sha256_bytes(canonical_json(payload).encode()),
        )
        return result


def _request_payload(
    system: str,
    prompt: str,
    parameters: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    schema = parameters.get("json_schema")
    if not isinstance(schema, dict):
        raise ValueError("the broker request requires a JSON response schema")
    output = parameters.get("max_tokens")
    if isinstance(output, bool) or not isinstance(output, int) or output < 1:
        raise ValueError("the broker request requires a positive output-token limit")
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "candidateCount": 1,
            "temperature": parameters.get("temperature", 0),
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": output,
            "thinkingConfig": {"thinkingLevel": config["thinking_level"]},
        },
        "store": False,
    }


def _validate_receipt(
    receipt: dict[str, Any],
    *,
    request_key: str,
    request_sha256: str,
    stage: str,
    paper_id: str,
    family_id: str,
    source_version_id: str,
    model: str,
) -> None:
    expected = {
        "request_key": request_key,
        "request_sha256": request_sha256,
        "stage": stage,
        "paper_id": paper_id,
        "family_id": family_id,
        "source_version_id": source_version_id,
        "model": model,
    }
    if not str(receipt.get("run_id") or "").strip() or any(
        receipt.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("the existing broker receipt does not match the request")


def _provider_result(receipt: dict[str, Any], model: str) -> ProviderResult:
    state = receipt.get("state")
    if state == "ambiguous_charge":
        raise AmbiguousChargeError("the broker recorded an ambiguous model charge")
    if state == "not_submitted":
        raise BudgetError(
            str(receipt.get("reason") or "the broker stopped the request")
        )
    if state != "completed":
        raise ProviderError(f"the broker stopped with state {state}")
    response = receipt.get("response")
    if not isinstance(response, dict):
        raise ProviderError("the completed broker receipt lacks a response")
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ProviderResponseError(
            "the broker response does not contain one candidate"
        )
    candidate = candidates[0]
    if not isinstance(candidate, dict) or candidate.get("finishReason") != "STOP":
        raise ProviderResponseError("the broker response did not finish normally")
    parts = (candidate.get("content") or {}).get("parts")
    if not isinstance(parts, list) or not parts:
        raise ProviderResponseError("the broker response contains no JSON text")
    text = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict)
    ).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProviderResponseError(
            "the broker response contains malformed JSON"
        ) from error
    if not isinstance(value, dict):
        raise ProviderResponseError("the broker response JSON is not an object")
    usage = receipt.get("usage") or {}
    actual = receipt.get("actual_cost_usd")
    return ProviderResult(
        payload=value,
        returned_model=str(response.get("modelVersion") or model),
        request_id=response.get("responseId"),
        input_tokens=usage.get("promptTokenCount"),
        output_tokens=(usage.get("candidatesTokenCount") or 0)
        + (usage.get("thoughtsTokenCount") or 0),
        actual_cost_usd=Decimal(str(actual)) if actual is not None else None,
    )
