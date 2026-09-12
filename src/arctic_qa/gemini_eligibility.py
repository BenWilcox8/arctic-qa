from __future__ import annotations

import fcntl
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Any

from .util import atomic_json, canonical_json, sha256_bytes, sha256_file


CRITERIA = (
    "published_primary_findings",
    "stable_identity_version",
    "study_geography",
    "access_rights_evidence",
    "correction_retraction_coverage",
)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _config(path: Path) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema") != "gemini-eligibility-config-v1":
        raise ValueError("unsupported Gemini eligibility config schema")
    if value.get("fallback_model") is not None:
        raise ValueError("automatic Gemini model fallback is not permitted")
    if date.today() > date.fromisoformat(value["price_valid_through"]):
        raise ValueError("Gemini pricing is expired; update the versioned price record")
    return value


def _segments(text: str, page_chars: int = 12000) -> list[dict[str, Any]]:
    segments = []
    for start in range(0, len(text), page_chars):
        number = len(segments) + 1
        segments.append(
            {
                "locator": f"text-page-{number:05d}",
                "start": start,
                "end": min(start + page_chars, len(text)),
                "text": text[start : start + page_chars],
            }
        )
    return segments


def _request_payload(
    *,
    metadata: dict[str, Any],
    policy: dict[str, Any],
    text: str,
    prompt: str,
    schema: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    evidence = {
        "metadata": metadata,
        "frozen_policy": policy,
        "source_segments": _segments(text),
    }
    return {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user", "parts": [{"text": canonical_json(evidence)}]}],
        "generationConfig": {
            "candidateCount": 1,
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": int(config["maximum_output_tokens"]),
            "thinkingConfig": {"thinkingLevel": config["thinking_level"]},
        },
        "store": False,
    }


def _job_key(
    source: dict[str, Any],
    config: dict[str, Any],
    prompt_path: Path,
    schema_path: Path,
    policy_path: Path,
) -> str:
    identity = {
        "candidate_key": source["candidate_key"],
        "source_content_hash": source["source_content_hash"],
        "extraction_sha256": source["extraction_sha256"],
        "model": config["model"],
        "prompt_sha256": sha256_file(prompt_path),
        "schema_sha256": sha256_file(schema_path),
        "policy_sha256": sha256_file(policy_path),
    }
    return sha256_bytes(canonical_json(identity).encode())


def _cost(config: dict[str, Any], input_tokens: int, output_tokens: int) -> Decimal:
    input_cost = (
        Decimal(input_tokens)
        * Decimal(config["input_usd_per_million_tokens"])
        / Decimal(1_000_000)
    )
    output_cost = (
        Decimal(output_tokens)
        * Decimal(config["output_usd_per_million_tokens_including_thinking"])
        / Decimal(1_000_000)
    )
    return (input_cost + output_cost).quantize(Decimal("0.000001"), rounding=ROUND_UP)


def _budget_path(run_dir: Path) -> Path:
    return run_dir / "budget-ledger.json"


def init_budget(
    run_dir: Path, config: dict[str, Any], allocation: Decimal
) -> dict[str, Any]:
    cap = Decimal(config["project_budget_usd"])
    if allocation <= 0 or allocation > cap:
        raise ValueError(
            "Gemini run allocation must be positive and within the project cap"
        )
    path = _budget_path(run_dir)
    if not path.is_file():
        atomic_json(
            path,
            {
                "schema": "gemini-budget-ledger-v1",
                "project_cap_usd": str(cap),
                "run_allocation_usd": str(allocation),
                "prior_project_spend_usd": "0",
                "reserved_usd": "0",
                "spent_usd": "0",
                "ambiguous_reserved_usd": "0",
                "updated_at_utc": _now(),
            },
        )
    return _read(path)


def reserve_budget(run_dir: Path, amount: Decimal) -> dict[str, Any]:
    lock_path = run_dir / ".budget.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = _read(_budget_path(run_dir))
        used = sum(
            Decimal(ledger[key])
            for key in (
                "prior_project_spend_usd",
                "reserved_usd",
                "spent_usd",
                "ambiguous_reserved_usd",
            )
        )
        allocation_used = sum(
            Decimal(ledger[key])
            for key in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")
        )
        if used + amount > Decimal(
            ledger["project_cap_usd"]
        ) or allocation_used + amount > Decimal(ledger["run_allocation_usd"]):
            raise ValueError(
                "Gemini budget reservation exceeds the project cap or run allocation"
            )
        ledger["reserved_usd"] = str(Decimal(ledger["reserved_usd"]) + amount)
        ledger["updated_at_utc"] = _now()
        atomic_json(_budget_path(run_dir), ledger)
        return ledger


def reconcile_budget(
    run_dir: Path, reserved: Decimal, actual: Decimal | None
) -> dict[str, Any]:
    lock_path = run_dir / ".budget.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = _read(_budget_path(run_dir))
        ledger["reserved_usd"] = str(Decimal(ledger["reserved_usd"]) - reserved)
        key = "spent_usd" if actual is not None else "ambiguous_reserved_usd"
        ledger[key] = str(
            Decimal(ledger[key]) + (actual if actual is not None else reserved)
        )
        ledger["updated_at_utc"] = _now()
        atomic_json(_budget_path(run_dir), ledger)
        return ledger


def _validate_shape(value: Any) -> list[str]:
    errors = []
    if not isinstance(value, dict) or set(value) != {
        "criteria",
        "overall",
        "reason_codes",
        "sufficient_context",
    }:
        return ["response_shape_invalid"]
    if value.get("overall") not in {"eligible", "excluded", "uncertain"}:
        errors.append("overall_invalid")
    if not isinstance(value.get("reason_codes"), list) or not value["reason_codes"]:
        errors.append("reason_codes_invalid")
    criteria = value.get("criteria")
    if not isinstance(criteria, dict) or set(criteria) != set(CRITERIA):
        errors.append("criterion_set_invalid")
    return errors


def validate_response(value: Any, segments: list[dict[str, Any]]) -> dict[str, Any]:
    errors = _validate_shape(value)
    resolved = []
    by_locator = {row["locator"]: row for row in segments}
    if not errors:
        for criterion in CRITERIA:
            row = value["criteria"][criterion]
            if (
                not isinstance(row, dict)
                or set(row) != {"decision", "evidence"}
                or row.get("decision") not in {"yes", "no", "unknown", "not_applicable"}
                or not isinstance(row.get("evidence"), list)
            ):
                errors.append(f"criterion_invalid:{criterion}")
                continue
            for evidence in row["evidence"]:
                locator = (
                    evidence.get("locator") if isinstance(evidence, dict) else None
                )
                quote = evidence.get("quote") if isinstance(evidence, dict) else None
                segment = by_locator.get(locator)
                if not segment or not isinstance(quote, str) or not quote:
                    errors.append(f"evidence_invalid:{criterion}")
                    continue
                occurrences = [
                    match.start()
                    for match in re.finditer(re.escape(quote), segment["text"])
                ]
                if len(occurrences) != 1:
                    errors.append(f"evidence_unmatched_or_ambiguous:{criterion}")
                    continue
                start = int(segment["start"]) + occurrences[0]
                resolved.append(
                    {
                        "criterion": criterion,
                        "locator": locator,
                        "quote": quote,
                        "start": start,
                        "end": start + len(quote),
                    }
                )
    if (
        isinstance(value, dict)
        and value.get("overall") == "eligible"
        and (errors or not value.get("sufficient_context"))
    ):
        errors.append("eligible_requires_valid_sufficient_context")
    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "resolved_evidence": resolved,
        "decision": value.get("overall")
        if isinstance(value, dict) and not errors
        else "uncertain",
    }


class GeminiTransport:
    def __init__(self, api_base: str, api_key: str, timeout: float = 120) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def post(self, model: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}/models/{urllib.parse.quote(model, safe='')}:" + method
        request = urllib.request.Request(
            url,
            data=canonical_json(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())


def run_gemini_eligibility(
    *,
    action: str,
    access_run_dir: Path,
    run_dir: Path,
    config_file: Path,
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
    max_cost_usd: Decimal,
    transport: GeminiTransport | Any | None = None,
) -> dict[str, Any]:
    if action not in {"doctor", "dry-run", "run", "resume", "pause", "status"}:
        raise ValueError("Gemini eligibility action is not supported")
    config = _config(config_file)
    prompt = prompt_file.read_text(encoding="utf-8")
    schema = _read(schema_file)
    policy = _read(policy_file)
    key_present = bool(os.environ.get(config["api_key_environment_variable"]))
    if action == "doctor":
        return {
            "schema": "gemini-eligibility-doctor-v1",
            "state": "disabled_no_key" if not key_present else "ready",
            "model": config["model"],
            "fallback_model": None,
            "api_key": "set" if key_present else "not_set",
            "live_call_made": False,
            "price_valid_through": config["price_valid_through"],
        }
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger = init_budget(run_dir, config, max_cost_usd)
    sources = []
    for path in sorted((access_run_dir / "items").glob("item-*.json")):
        row = _read(path)
        if row.get("access_state") == "full_text_ready":
            sources.append(row)
    completed = {path.stem for path in (run_dir / "jobs").glob("*.json")}
    queued = 0
    too_large = 0
    estimated_cost = Decimal("0")
    jobs = []
    for source in sources:
        text_path = Path(source["extraction_path"])
        if sha256_file(text_path) != source["extraction_sha256"]:
            raise ValueError("ready source extraction hash changed")
        text = text_path.read_text(encoding="utf-8")
        payload = _request_payload(
            metadata={
                key: source.get(key)
                for key in (
                    "candidate_key",
                    "title",
                    "doi",
                    "source_content_hash",
                    "extraction_sha256",
                )
            },
            policy=policy,
            text=text,
            prompt=prompt,
            schema=schema,
            config=config,
        )
        estimated_tokens = (len(canonical_json(payload)) + 3) // 4
        job_key = _job_key(source, config, prompt_file, schema_file, policy_file)
        if job_key in completed:
            continue
        if estimated_tokens > int(config["maximum_input_tokens"]):
            too_large += 1
            continue
        queued += 1
        estimated_cost += _cost(
            config, estimated_tokens, int(config["maximum_output_tokens"])
        )
        jobs.append((job_key, source, text, payload, estimated_tokens))
    status = {
        "schema": "gemini-eligibility-progress-v1",
        "state": "disabled_no_key" if not key_present else "prepared",
        "model": config["model"],
        "configured_fallback": None,
        "updated_at_utc": _now(),
        "counts": {
            "full_text_ready": len(sources),
            "queued": queued,
            "too_large": too_large,
            "completed": len(completed),
        },
        "estimated_max_cost_usd": str(estimated_cost),
        "budget": ledger,
        "live_call_made": False,
    }
    if action in {"dry-run", "status", "pause"}:
        if action == "pause":
            status["state"] = "paused"
        atomic_json(run_dir / "progress.json", status)
        return status
    if not key_present and transport is None:
        atomic_json(run_dir / "progress.json", status)
        raise ValueError(
            f"Gemini is disabled; set {config['api_key_environment_variable']} in private environment storage"
        )
    client = transport or GeminiTransport(
        config["api_base"], os.environ[config["api_key_environment_variable"]]
    )
    for job_key, source, text, payload, _estimated in jobs:
        count_result = client.post(
            config["model"],
            "countTokens",
            {
                "generateContentRequest": {
                    "model": f"models/{config['model']}",
                    **payload,
                }
            },
        )
        exact_input = int(count_result["totalTokens"])
        if exact_input > int(config["maximum_input_tokens"]):
            continue
        reserved = _cost(config, exact_input, int(config["maximum_output_tokens"]))
        reserve_budget(run_dir, reserved)
        request_receipt = {
            "schema": "gemini-eligibility-job-v1",
            "job_key": job_key,
            "request_sha256": sha256_bytes(canonical_json(payload).encode()),
            "candidate_key": source["candidate_key"],
            "model": config["model"],
            "source_content_hash": source["source_content_hash"],
            "extraction_sha256": source["extraction_sha256"],
            "policy_sha256": sha256_file(policy_file),
            "prompt_sha256": sha256_file(prompt_file),
            "schema_sha256": sha256_file(schema_file),
            "input_tokens": exact_input,
            "reserved_usd": str(reserved),
            "started_at_utc": _now(),
        }
        atomic_json(
            run_dir / "submitted" / f"{job_key}.json",
            {**request_receipt, "state": "submitted"},
            immutable=True,
        )
        try:
            raw = client.post(config["model"], "generateContent", payload)
        except Exception:
            reconcile_budget(run_dir, reserved, None)
            atomic_json(
                run_dir / "ambiguous" / f"{job_key}.json",
                {
                    **request_receipt,
                    "state": "unknown_billable_attempt",
                    "completed_at_utc": _now(),
                },
                immutable=True,
            )
            raise
        candidates = raw.get("candidates") or []
        try:
            text_result = candidates[0]["content"]["parts"][0]["text"]
            parsed = json.loads(text_result)
            validation = validate_response(parsed, _segments(text))
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            parsed = None
            validation = {
                "valid": False,
                "errors": ["refusal_block_or_malformed_response"],
                "resolved_evidence": [],
                "decision": "uncertain",
            }
        usage = raw.get("usageMetadata") or {}
        actual = _cost(
            config,
            int(usage.get("promptTokenCount") or exact_input),
            int(usage.get("candidatesTokenCount") or 0)
            + int(usage.get("thoughtsTokenCount") or 0),
        )
        reconcile_budget(run_dir, reserved, actual)
        atomic_json(
            run_dir / "jobs" / f"{job_key}.json",
            {
                **request_receipt,
                "state": "completed" if validation["valid"] else "screening_error",
                "completed_at_utc": _now(),
                "model_version": raw.get("modelVersion"),
                "response_id": raw.get("responseId"),
                "raw_response": raw,
                "parsed_response": parsed,
                "validation": validation,
                "actual_cost_usd": str(actual),
            },
            immutable=True,
        )
    status["state"] = "completed"
    status["live_call_made"] = bool(jobs)
    status["updated_at_utc"] = _now()
    status["budget"] = _read(_budget_path(run_dir))
    atomic_json(run_dir / "progress.json", status)
    return status
