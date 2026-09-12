from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path
from typing import Any

from .util import atomic_json, atomic_write, canonical_json, sha256_bytes, sha256_file


CRITERIA = (
    "published_primary_findings",
    "stable_identity_version",
    "study_geography",
    "access_rights_evidence",
    "correction_retraction_coverage",
)

ELIGIBILITY_RESPONSE_V2 = "eligibility-response-v2"
ELIGIBILITY_STATUS_MAPPING_VERSION = "eligibility-criterion-status-map-v1"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Gemini {name} is not a valid decimal") from error
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        word = "positive" if positive else "nonnegative"
        raise ValueError(f"Gemini {name} must be {word}")
    return result


def _config(path: Path) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema") != "gemini-eligibility-config-v1":
        raise ValueError("unsupported Gemini eligibility config schema")
    if value.get("config_id") != "arctic-gemini-eligibility-r1-config-v2":
        raise ValueError("the Gemini eligibility config revision is not approved")
    if value.get("model") != "gemini-3.8-flash":
        raise ValueError("the Gemini model has no verified price record")
    if value.get("fallback_model") is not None:
        raise ValueError("automatic Gemini model fallback is not permitted")
    if value.get("api_base") != "https://generativelanguage.googleapis.com/v1beta":
        raise ValueError("the Gemini API base is not the approved HTTPS endpoint")
    if value.get("api_key_environment_variable") != "GEMINI_API_KEY":
        raise ValueError("the Gemini credential variable changed")
    if value.get("thinking_level") != "low":
        raise ValueError("the Gemini thinking level must be low")
    for field in ("maximum_input_tokens", "maximum_output_tokens"):
        if not isinstance(value.get(field), int) or value[field] <= 0:
            raise ValueError(f"Gemini {field} must be positive")
    for field in (
        "project_budget_usd",
        "prior_project_spend_usd",
        "input_usd_per_million_tokens",
        "output_usd_per_million_tokens_including_thinking",
    ):
        _decimal(value.get(field), field, positive=field != "prior_project_spend_usd")
    exact_decimals = {
        "project_budget_usd": Decimal("1000"),
        "input_usd_per_million_tokens": Decimal("0.75"),
        "output_usd_per_million_tokens_including_thinking": Decimal("3.75"),
    }
    for field, expected in exact_decimals.items():
        if _decimal(value[field], field) != expected:
            raise ValueError(f"the verified Gemini value changed: {field}")
    if value["maximum_input_tokens"] != 1_048_576:
        raise ValueError("the verified Gemini input limit changed")
    if value["maximum_output_tokens"] != 8192:
        raise ValueError("the configured Gemini output limit must be 8192")
    start = date.fromisoformat(value["price_valid_from"])
    end = date.fromisoformat(value["price_valid_through"])
    if not start <= date.today() <= end:
        raise ValueError("Gemini pricing is not active; update the price record")
    if not str(value.get("price_source", "")).startswith("https://ai.google.dev/"):
        raise ValueError("Gemini price source is not an official Google URL")
    return value


def _safety(path: Path) -> dict[str, Any]:
    value = _read(path)
    if value.get("schema") != "captain-gemini-initial-safety-policy-v1":
        raise ValueError("unsupported Gemini safety policy schema")
    required = {
        "project_lifetime_ceiling_usd": Decimal("1000"),
        "initial_phase_ceiling_usd": Decimal("1"),
        "maximum_run_allocation_usd": Decimal("1"),
        "maximum_request_reserved_cost_usd": Decimal("0.25"),
    }
    for field, expected in required.items():
        if _decimal(value.get(field), field, positive=True) != expected:
            raise ValueError(f"Gemini safety value changed: {field}")
    exact = {
        "maximum_generation_submissions_initial_phase": 3,
        "maximum_concurrent_generation_requests": 1,
        "minimum_seconds_between_generation_submissions": 60,
        "maximum_count_requests_initial_phase": 10,
        "maximum_run_wall_seconds": 1200,
        "maximum_output_tokens_including_thinking": 8192,
        "automatic_generation_retries": 0,
    }
    for field, expected in exact.items():
        if value.get(field) != expected:
            raise ValueError(f"Gemini safety value changed: {field}")
    for field in (
        "stop_on_first_error_or_ambiguous_charge",
        "automatic_model_fallback",
        "automatic_budget_rearm",
        "automatic_next_run",
        "key_presence_authorizes_generation",
    ):
        expected = field == "stop_on_first_error_or_ambiguous_charge"
        if value.get(field) is not expected:
            raise ValueError(f"Gemini safety control changed: {field}")
    if not isinstance(value.get("live_generation_enabled"), bool):
        raise ValueError("Gemini live-generation control must be a boolean")
    return value


def _segments(text: str, page_chars: int = 12000) -> list[dict[str, Any]]:
    result = []
    for start in range(0, len(text), page_chars):
        number = len(result) + 1
        result.append(
            {
                "source_block_id": f"text-block-{number:05d}",
                "section_id": "extracted-text",
                "page_id": None,
                "start": start,
                "end": min(start + page_chars, len(text)),
                "text": text[start : start + page_chars],
            }
        )
    return result


def _response_contract_version(schema: dict[str, Any]) -> str:
    value = (schema.get("properties") or {}).get("schema_version", {}).get("const")
    return value if value == ELIGIBILITY_RESPONSE_V2 else "eligibility-response-v1"


def _line_fragments(text: str, maximum_bytes: int) -> list[str]:
    fragments: list[str] = []
    for line in text.splitlines(keepends=True):
        current = ""
        current_bytes = 0
        for character in line:
            size = len(character.encode("utf-8"))
            if current and current_bytes + size > maximum_bytes:
                fragments.append(current)
                current = ""
                current_bytes = 0
            current += character
            current_bytes += size
        if current:
            fragments.append(current)
    if text and not fragments:
        fragments.append(text)
    return fragments


def _span_blocks_v2(
    text: str, extraction_sha256: str, block_bytes: int = 12000
) -> list[dict[str, Any]]:
    if block_bytes <= 0:
        raise ValueError("eligibility span block size must be positive")
    encoded = text.encode("utf-8")
    if sha256_bytes(encoded) != extraction_sha256:
        raise ValueError("eligibility extraction hash does not match the source text")
    fragments = _line_fragments(text, block_bytes)
    grouped: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for fragment in fragments:
        size = len(fragment.encode("utf-8"))
        if current and current_size + size > block_bytes:
            grouped.append(current)
            current = []
            current_size = 0
        current.append(fragment)
        current_size += size
    if current:
        grouped.append(current)

    blocks: list[dict[str, Any]] = []
    byte_cursor = 0
    span_number = 0
    for block_number, group in enumerate(grouped, start=1):
        block_text = "".join(group)
        block_encoded = block_text.encode("utf-8")
        block_id = f"text-block-{block_number:05d}"
        block_sha256 = sha256_bytes(block_encoded)
        spans = []
        for fragment in group:
            fragment_encoded = fragment.encode("utf-8")
            span_number += 1
            start = byte_cursor
            end = start + len(fragment_encoded)
            spans.append(
                {
                    "span_id": f"s{span_number:06d}",
                    "source_block_id": block_id,
                    "section_id": "extracted-text",
                    "page_id": None,
                    "extraction_sha256": extraction_sha256,
                    "block_sha256": block_sha256,
                    "span_sha256": sha256_bytes(fragment_encoded),
                    "start_byte": start,
                    "end_byte": end,
                    "text": fragment,
                }
            )
            byte_cursor = end
        blocks.append(
            {
                "source_block_id": block_id,
                "section_id": "extracted-text",
                "page_id": None,
                "extraction_sha256": extraction_sha256,
                "block_sha256": block_sha256,
                "start_byte": byte_cursor - len(block_encoded),
                "end_byte": byte_cursor,
                "text": block_text,
                "spans": spans,
            }
        )
    return blocks


def _span_manifest_v2(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    extraction_sha256 = blocks[0]["extraction_sha256"] if blocks else sha256_bytes(b"")
    return {
        "schema": "eligibility-span-manifest-v1",
        "response_schema_version": ELIGIBILITY_RESPONSE_V2,
        "extraction_sha256": extraction_sha256,
        "blocks": [
            {
                key: block[key]
                for key in (
                    "source_block_id",
                    "section_id",
                    "page_id",
                    "extraction_sha256",
                    "block_sha256",
                    "start_byte",
                    "end_byte",
                )
            }
            | {
                "spans": [
                    {
                        key: span[key]
                        for key in (
                            "span_id",
                            "source_block_id",
                            "section_id",
                            "page_id",
                            "extraction_sha256",
                            "block_sha256",
                            "span_sha256",
                            "start_byte",
                            "end_byte",
                        )
                    }
                    for span in block["spans"]
                ]
            }
            for block in blocks
        ],
    }


def _validation_evidence(
    text: str, extraction_sha256: str, schema: dict[str, Any]
) -> list[dict[str, Any]]:
    if _response_contract_version(schema) == ELIGIBILITY_RESPONSE_V2:
        return _span_blocks_v2(text, extraction_sha256)
    return _segments(text)


def _render_span_blocks_v2(blocks: list[dict[str, Any]]) -> str:
    rendered = []
    for block in blocks:
        parts = [f"[source_block_id={block['source_block_id']}]\n"]
        parts.extend(f"{span['span_id']}\t{span['text']}" for span in block["spans"])
        rendered.append("".join(parts))
    return "".join(rendered)


def _persist_span_manifest_v2(
    run_dir: Path,
    job_key: str,
    text: str,
    extraction_sha256: str,
    schema: dict[str, Any],
) -> dict[str, str]:
    if _response_contract_version(schema) != ELIGIBILITY_RESPONSE_V2:
        return {}
    manifest = _span_manifest_v2(_span_blocks_v2(text, extraction_sha256))
    manifest_sha256 = sha256_bytes(canonical_json(manifest).encode())
    path = run_dir / "span-manifests" / f"{job_key}.json"
    atomic_json(path, manifest, immutable=True)
    return {
        "span_manifest_path": str(path.resolve()),
        "span_manifest_sha256": manifest_sha256,
    }


def _correction_metadata(source: dict[str, Any]) -> dict[str, Any]:
    return source.get("correction_metadata") or {
        "provided": False,
        "known_status": "unknown",
        "source": None,
        "as_of": None,
    }


def _known_context_gaps(source: dict[str, Any]) -> list[str]:
    coverage = source.get("extraction_coverage") or {}
    gaps = []
    for field in ("figures", "tables", "supplements", "ocr"):
        value = coverage.get(field)
        if value in {None, "unknown", "unknown_not_extracted", "not_extracted"}:
            gaps.append(f"{field}:{value or 'unknown'}")
    correction = _correction_metadata(source)
    if not correction["provided"] or correction["known_status"] == "unknown":
        gaps.append("correction_retraction_coverage:unknown")
    return gaps


def _identity(
    source: dict[str, Any],
    config: dict[str, Any],
    prompt_path: Path,
    schema_path: Path,
    policy_path: Path,
) -> dict[str, Any]:
    return {
        "source": {
            key: source.get(key)
            for key in (
                "candidate_key",
                "title",
                "doi",
                "authors",
                "year",
                "subgroup",
                "source_content_hash",
                "extraction_sha256",
                "extraction_coverage",
                "media_type",
                "final_url",
                "correction_metadata",
            )
        },
        "generation": {
            key: config.get(key)
            for key in (
                "model",
                "fallback_model",
                "api_base",
                "maximum_input_tokens",
                "maximum_output_tokens",
                "thinking_level",
            )
        },
        "prompt_sha256": sha256_file(prompt_path),
        "schema_sha256": sha256_file(schema_path),
        "policy_sha256": sha256_file(policy_path),
    }


def _job_key(
    source: dict[str, Any],
    config: dict[str, Any],
    prompt_path: Path,
    schema_path: Path,
    policy_path: Path,
) -> str:
    return sha256_bytes(
        canonical_json(
            _identity(source, config, prompt_path, schema_path, policy_path)
        ).encode()
    )


def _request_payload(
    *,
    source: dict[str, Any],
    policy: dict[str, Any],
    text: str,
    prompt: str,
    schema: dict[str, Any],
    config: dict[str, Any],
    request_id: str,
    policy_sha256: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    metadata = {
        key: source.get(key)
        for key in (
            "candidate_key",
            "title",
            "doi",
            "authors",
            "year",
            "subgroup",
            "source_content_hash",
            "extraction_sha256",
            "extraction_coverage",
            "media_type",
            "final_url",
        )
    }
    metadata["known_context_gaps"] = _known_context_gaps(source)
    correction = _correction_metadata(source)
    hashes = {
        "policy_sha256": policy_sha256,
        "source_version_sha256": str(source["source_content_hash"]),
        "extracted_text_sha256": str(source["extraction_sha256"]),
        "metadata_sha256": sha256_bytes(canonical_json(metadata).encode()),
    }
    if _response_contract_version(schema) == ELIGIBILITY_RESPONSE_V2:
        span_blocks = _span_blocks_v2(text, str(source["extraction_sha256"]))
        span_manifest = _span_manifest_v2(span_blocks)
        hashes["span_manifest_sha256"] = sha256_bytes(
            canonical_json(span_manifest).encode()
        )
        user_text = "\n".join(
            (
                "<ELIGIBILITY_SCREEN_REQUEST_V2>",
                f"request_id: {request_id}",
                f"status_mapping_version: {ELIGIBILITY_STATUS_MAPPING_VERSION}",
                f"input_hashes: {canonical_json(hashes)}",
                f"policy: {canonical_json(policy)}",
                f"metadata: {canonical_json(metadata)}",
                f"correction_metadata: {canonical_json(correction)}",
                "ARTICLE_SPANS_BEGIN",
                _render_span_blocks_v2(span_blocks),
                "ARTICLE_SPANS_END",
                "</ELIGIBILITY_SCREEN_REQUEST_V2>",
            )
        )
        return (
            {
                "systemInstruction": {"parts": [{"text": prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_text}]}],
                "generationConfig": {
                    "candidateCount": 1,
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                    "maxOutputTokens": int(config["maximum_output_tokens"]),
                    "thinkingConfig": {"thinkingLevel": config["thinking_level"]},
                },
                "store": False,
            },
            hashes,
        )
    blocks = [
        f"[source_block_id={row['source_block_id']}; section_id={row['section_id']}; page_id={row['page_id']}]\n{row['text']}"
        for row in _segments(text)
    ]
    user_text = "\n".join(
        (
            "<ELIGIBILITY_SCREEN_REQUEST_V1>",
            f"request_id: {request_id}",
            f"input_hashes: {canonical_json(hashes)}",
            f"policy: {canonical_json(policy)}",
            f"metadata: {canonical_json(metadata)}",
            f"correction_metadata: {canonical_json(correction)}",
            "ARTICLE_TEXT_BEGIN",
            *blocks,
            "ARTICLE_TEXT_END",
            "</ELIGIBILITY_SCREEN_REQUEST_V1>",
        )
    )
    return (
        {
            "systemInstruction": {"parts": [{"text": prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "candidateCount": 1,
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "maxOutputTokens": int(config["maximum_output_tokens"]),
                "thinkingConfig": {"thinkingLevel": config["thinking_level"]},
            },
            "store": False,
        },
        hashes,
    )


def _cost(config: dict[str, Any], input_tokens: int, output_tokens: int) -> Decimal:
    if (
        not isinstance(input_tokens, int)
        or not isinstance(output_tokens, int)
        or input_tokens < 0
        or output_tokens < 0
    ):
        raise ValueError("Gemini token usage must be nonnegative integers")
    amount = (
        Decimal(input_tokens)
        * _decimal(config["input_usd_per_million_tokens"], "input price")
        / Decimal(1_000_000)
    )
    amount += (
        Decimal(output_tokens)
        * _decimal(
            config["output_usd_per_million_tokens_including_thinking"], "output price"
        )
        / Decimal(1_000_000)
    )
    return amount.quantize(Decimal("0.000001"), rounding=ROUND_UP)


def _project_path(run_dir: Path, explicit: Path | None = None) -> Path:
    return (
        explicit.resolve()
        if explicit
        else run_dir.parent / "project-budget-ledger.json"
    )


def _lock_path(project_path: Path) -> Path:
    return project_path.with_name(f".{project_path.name}.lock")


def _run_key(run_dir: Path) -> str:
    return sha256_bytes(str(run_dir.resolve()).encode())


def _run_budget_view(run_dir: Path, project: dict[str, Any]) -> dict[str, Any]:
    run = project["runs"][_run_key(run_dir)]
    used = sum(
        _decimal(project[key], key)
        for key in (
            "prior_project_spend_usd",
            "reserved_usd",
            "spent_usd",
            "ambiguous_reserved_usd",
        )
    )
    view = {
        "schema": "gemini-budget-ledger-v2",
        "project_cap_usd": project["project_cap_usd"],
        "initial_phase_ceiling_usd": project["initial_phase_ceiling_usd"],
        "prior_project_spend_usd": project["prior_project_spend_usd"],
        "project_reserved_usd": project["reserved_usd"],
        "project_spent_usd": project["spent_usd"],
        "project_ambiguous_reserved_usd": project["ambiguous_reserved_usd"],
        "project_remaining_usd": str(
            _decimal(project["project_cap_usd"], "cap") - used
        ),
        "initial_phase_remaining_usd": str(
            _decimal(project["initial_phase_ceiling_usd"], "phase ceiling")
            - _decimal(project["reserved_usd"], "reserved")
            - _decimal(project["spent_usd"], "spent")
            - _decimal(project["ambiguous_reserved_usd"], "ambiguous")
        ),
        "run_allocation_usd": run["allocation_usd"],
        "reserved_usd": run["reserved_usd"],
        "spent_usd": run["spent_usd"],
        "ambiguous_reserved_usd": run["ambiguous_reserved_usd"],
        "phase_count_requests": project["phase_count_requests"],
        "phase_generation_submissions": project["phase_generation_submissions"],
        "phase_inflight": project["phase_inflight"],
        "updated_at_utc": project["updated_at_utc"],
    }
    atomic_json(run_dir / "budget-ledger.json", view)
    return view


def init_budget(
    run_dir: Path,
    config: dict[str, Any],
    allocation: Decimal,
    project_ledger_file: Path | None = None,
    safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cap = _decimal(config["project_budget_usd"], "project budget", positive=True)
    prior = _decimal(config.get("prior_project_spend_usd"), "prior project spend")
    allocation = _decimal(allocation, "run allocation", positive=True)
    if safety:
        cap = min(
            cap, _decimal(safety["project_lifetime_ceiling_usd"], "lifetime ceiling")
        )
        if allocation > _decimal(safety["maximum_run_allocation_usd"], "run limit"):
            raise ValueError("Gemini run allocation exceeds the initial safety limit")
    if allocation > cap - prior:
        raise ValueError("Gemini run allocation exceeds the available project cap")
    run_dir.mkdir(parents=True, exist_ok=True)
    project_path = _project_path(run_dir, project_ledger_file)
    project_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock_path(project_path).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if project_path.is_file():
            project = _read(project_path)
            if (
                _decimal(project["project_cap_usd"], "project cap") != cap
                or _decimal(project["prior_project_spend_usd"], "prior spend") != prior
            ):
                raise ValueError("Gemini project budget inputs changed")
        else:
            project = {
                "schema": "gemini-project-budget-v1",
                "project_cap_usd": str(cap),
                "initial_phase_ceiling_usd": str(
                    _decimal(safety["initial_phase_ceiling_usd"], "phase ceiling")
                    if safety
                    else cap
                ),
                "prior_project_spend_usd": str(prior),
                "reserved_usd": "0",
                "spent_usd": "0",
                "ambiguous_reserved_usd": "0",
                "phase_count_requests": 0,
                "phase_generation_submissions": 0,
                "phase_inflight": 0,
                "last_submission_at_utc": None,
                "requests": {},
                "runs": {},
                "updated_at_utc": _now(),
            }
        phase_ceiling = (
            _decimal(safety["initial_phase_ceiling_usd"], "phase ceiling")
            if safety
            else cap
        )
        if (
            project_path.is_file()
            and _decimal(project.get("initial_phase_ceiling_usd"), "phase ceiling")
            != phase_ceiling
        ):
            raise ValueError("Gemini initial phase ceiling changed")
        run_key = _run_key(run_dir)
        existing = project["runs"].get(run_key)
        if (
            existing
            and _decimal(existing["allocation_usd"], "existing allocation")
            != allocation
        ):
            raise ValueError("Gemini run allocation changed")
        project["runs"].setdefault(
            run_key,
            {
                "run_dir": str(run_dir.resolve()),
                "allocation_usd": str(allocation),
                "reserved_usd": "0",
                "spent_usd": "0",
                "ambiguous_reserved_usd": "0",
            },
        )
        project["updated_at_utc"] = _now()
        atomic_json(project_path, project)
        return _run_budget_view(run_dir, project)


def reserve_budget(
    run_dir: Path,
    amount: Decimal,
    project_ledger_file: Path | None = None,
    safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    amount = _decimal(amount, "reservation", positive=True)
    if safety and amount > _decimal(
        safety["maximum_request_reserved_cost_usd"], "request limit"
    ):
        raise ValueError("Gemini request reservation exceeds USD 0.25")
    project_path = _project_path(run_dir, project_ledger_file)
    with _lock_path(project_path).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        project = _read(project_path)
        run = project["runs"][_run_key(run_dir)]
        project_used = sum(
            _decimal(project[key], key)
            for key in (
                "prior_project_spend_usd",
                "reserved_usd",
                "spent_usd",
                "ambiguous_reserved_usd",
            )
        )
        run_used = sum(
            _decimal(run[key], key)
            for key in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")
        )
        if (
            project_used + amount > _decimal(project["project_cap_usd"], "cap")
            or (
                _decimal(project["reserved_usd"], "reserved")
                + _decimal(project["spent_usd"], "spent")
                + _decimal(project["ambiguous_reserved_usd"], "ambiguous")
                + amount
                > _decimal(project["initial_phase_ceiling_usd"], "phase ceiling")
            )
            or run_used + amount > _decimal(run["allocation_usd"], "allocation")
        ):
            raise ValueError(
                "Gemini reservation exceeds the project cap or run allocation"
            )
        project["reserved_usd"] = str(
            _decimal(project["reserved_usd"], "reserved") + amount
        )
        run["reserved_usd"] = str(
            _decimal(run["reserved_usd"], "run reserved") + amount
        )
        project["updated_at_utc"] = _now()
        atomic_json(project_path, project)
        return _run_budget_view(run_dir, project)


def _phase_event(
    run_dir: Path, safety: dict[str, Any], project_path: Path, event: str
) -> None:
    with _lock_path(project_path).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        project = _read(project_path)
        if _decimal(project["ambiguous_reserved_usd"], "ambiguous") != 0:
            raise ValueError("an ambiguous Gemini charge blocks the initial phase")
        if event == "count":
            if (
                project["phase_count_requests"]
                >= safety["maximum_count_requests_initial_phase"]
            ):
                raise ValueError("Gemini initial count-request limit is complete")
            project["phase_count_requests"] += 1
        elif event == "submit":
            if (
                project["phase_generation_submissions"]
                >= safety["maximum_generation_submissions_initial_phase"]
            ):
                raise ValueError("Gemini initial submission limit is complete")
            if (
                project["phase_inflight"]
                >= safety["maximum_concurrent_generation_requests"]
            ):
                raise ValueError("a Gemini generation request is already in flight")
            last = project.get("last_submission_at_utc")
            if last:
                elapsed = (
                    datetime.now(UTC)
                    - datetime.fromisoformat(last.replace("Z", "+00:00"))
                ).total_seconds()
                if elapsed < safety["minimum_seconds_between_generation_submissions"]:
                    raise ValueError("the Gemini submission interval is not complete")
            project["phase_generation_submissions"] += 1
            project["phase_inflight"] += 1
            project["last_submission_at_utc"] = _now()
        elif event == "finish":
            project["phase_inflight"] = max(project["phase_inflight"] - 1, 0)
        project["updated_at_utc"] = _now()
        atomic_json(project_path, project)
        _run_budget_view(run_dir, project)


def reconcile_budget(
    run_dir: Path,
    reserved: Decimal,
    actual: Decimal | None,
    project_ledger_file: Path | None = None,
) -> dict[str, Any]:
    reserved = _decimal(reserved, "reconciliation reservation", positive=True)
    actual_value = _decimal(actual, "actual cost") if actual is not None else None
    project_path = _project_path(run_dir, project_ledger_file)
    with _lock_path(project_path).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        project = _read(project_path)
        run = project["runs"][_run_key(run_dir)]
        if reserved > _decimal(run["reserved_usd"], "run reserved"):
            raise ValueError("Gemini reconciliation exceeds the reservation")
        project["reserved_usd"] = str(
            _decimal(project["reserved_usd"], "reserved") - reserved
        )
        run["reserved_usd"] = str(
            _decimal(run["reserved_usd"], "run reserved") - reserved
        )
        key = "spent_usd" if actual_value is not None else "ambiguous_reserved_usd"
        amount = actual_value if actual_value is not None else reserved
        project[key] = str(_decimal(project[key], key) + amount)
        run[key] = str(_decimal(run[key], key) + amount)
        project["updated_at_utc"] = _now()
        atomic_json(project_path, project)
        return _run_budget_view(run_dir, project)


def _authorize_submission(
    run_dir: Path,
    job_key: str,
    amount: Decimal,
    safety: dict[str, Any],
    project_path: Path,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    amount = _decimal(amount, "reservation", positive=True)
    if amount > _decimal(safety["maximum_request_reserved_cost_usd"], "request limit"):
        raise ValueError("Gemini request reservation exceeds USD 0.25")
    with _lock_path(project_path).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        project = _read(project_path)
        run = project["runs"][_run_key(run_dir)]
        requests = project.setdefault("requests", {})
        if job_key in requests:
            raise ValueError("the Gemini job already has a submission record")
        if _decimal(project["ambiguous_reserved_usd"], "ambiguous") != 0:
            raise ValueError("an ambiguous Gemini charge blocks the initial phase")
        if (
            project["phase_generation_submissions"]
            >= safety["maximum_generation_submissions_initial_phase"]
        ):
            raise ValueError("Gemini initial submission limit is complete")
        if (
            project["phase_inflight"]
            >= safety["maximum_concurrent_generation_requests"]
        ):
            raise ValueError("a Gemini generation request is already in flight")
        last = project.get("last_submission_at_utc")
        if last:
            elapsed = (
                datetime.now(UTC) - datetime.fromisoformat(last.replace("Z", "+00:00"))
            ).total_seconds()
            if elapsed < safety["minimum_seconds_between_generation_submissions"]:
                raise ValueError("the Gemini submission interval is not complete")
        project_used = sum(
            _decimal(project[key], key)
            for key in (
                "prior_project_spend_usd",
                "reserved_usd",
                "spent_usd",
                "ambiguous_reserved_usd",
            )
        )
        phase_used = sum(
            _decimal(project[key], key)
            for key in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")
        )
        run_used = sum(
            _decimal(run[key], key)
            for key in ("reserved_usd", "spent_usd", "ambiguous_reserved_usd")
        )
        if (
            project_used + amount > _decimal(project["project_cap_usd"], "cap")
            or phase_used + amount
            > _decimal(project["initial_phase_ceiling_usd"], "phase ceiling")
            or run_used + amount > _decimal(run["allocation_usd"], "allocation")
        ):
            raise ValueError("Gemini reservation exceeds a budget limit")
        project["reserved_usd"] = str(
            _decimal(project["reserved_usd"], "reserved") + amount
        )
        run["reserved_usd"] = str(
            _decimal(run["reserved_usd"], "run reserved") + amount
        )
        project["phase_generation_submissions"] += 1
        project["phase_inflight"] += 1
        project["last_submission_at_utc"] = _now()
        requests[job_key] = {
            "state": "authorized",
            "run_key": _run_key(run_dir),
            "reserved_usd": str(amount),
            "authorized_at_utc": _now(),
            "receipt": receipt,
        }
        project["updated_at_utc"] = _now()
        atomic_json(project_path, project)
        return _run_budget_view(run_dir, project)


def _settle_submission(
    run_dir: Path,
    job_key: str,
    actual: Decimal | None,
    project_path: Path,
) -> dict[str, Any]:
    actual_value = _decimal(actual, "actual cost") if actual is not None else None
    with _lock_path(project_path).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        project = _read(project_path)
        request = (project.get("requests") or {}).get(job_key)
        if not request or request.get("state") != "authorized":
            raise ValueError("the Gemini submission is not authorized")
        run = project["runs"][request["run_key"]]
        reserved = _decimal(
            request["reserved_usd"], "request reservation", positive=True
        )
        project["reserved_usd"] = str(
            _decimal(project["reserved_usd"], "reserved") - reserved
        )
        run["reserved_usd"] = str(
            _decimal(run["reserved_usd"], "run reserved") - reserved
        )
        key = "spent_usd" if actual_value is not None else "ambiguous_reserved_usd"
        amount = actual_value if actual_value is not None else reserved
        project[key] = str(_decimal(project[key], key) + amount)
        run[key] = str(_decimal(run[key], key) + amount)
        project["phase_inflight"] = max(int(project["phase_inflight"]) - 1, 0)
        request["state"] = "settled" if actual_value is not None else "ambiguous"
        request["actual_usd"] = str(actual_value) if actual_value is not None else None
        request["settled_at_utc"] = _now()
        project["updated_at_utc"] = _now()
        atomic_json(project_path, project)
        return _run_budget_view(run_dir, project)


def _strict_json_loads(text: str) -> Any:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=pairs)


def _schema_errors(
    value: Any, schema: dict[str, Any], root: dict[str, Any]
) -> list[str]:
    if "$ref" in schema:
        reference = str(schema["$ref"])
        if not reference.startswith("#/"):
            return ["unsupported_schema_reference"]
        target: Any = root
        for part in reference[2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        if not isinstance(target, dict):
            return ["unresolved_schema_reference"]
        return _schema_errors(value, target, root)
    expected_type = schema.get("type")
    allowed_types = (
        expected_type if isinstance(expected_type, list) else [expected_type]
    )
    type_matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if expected_type is not None and not any(
        type_matches.get(name, False) for name in allowed_types
    ):
        return ["schema_type_invalid"]
    if "const" in schema and value != schema["const"]:
        return ["schema_const_invalid"]
    if "enum" in schema and value not in schema["enum"]:
        return ["schema_enum_invalid"]
    errors: list[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        if any(field not in value for field in required):
            errors.append("schema_required_field_missing")
        if schema.get("additionalProperties") is False and not set(value) <= set(
            properties
        ):
            errors.append("schema_additional_field")
        for field, child in properties.items():
            if field in value:
                errors.extend(_schema_errors(value[field], child, root))
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            errors.append("schema_array_too_short")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append("schema_array_too_long")
        child = schema.get("items")
        if isinstance(child, dict):
            for item in value:
                errors.extend(_schema_errors(item, child, root))
    return errors


def _validate_response_v1(
    value: Any,
    segments: list[dict[str, Any]],
    *,
    expected: dict[str, Any],
    response_schema: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    errors.extend(_schema_errors(value, response_schema, response_schema))
    required = {
        "schema_version",
        "request_id",
        "overall",
        "overall_reason_codes",
        "criteria",
        "known_missing_context",
        "correction_metadata_used",
        "input_echo",
    }
    if not isinstance(value, dict) or set(value) != required:
        return {
            "valid": False,
            "errors": ["response_shape_invalid"],
            "resolved_evidence": [],
            "decision": "uncertain",
        }
    if (
        value.get("schema_version") != "eligibility-response-v1"
        or value.get("request_id") != expected["request_id"]
    ):
        errors.append("response_identity_mismatch")
    if value.get("input_echo") != expected["input_echo"]:
        errors.append("input_hash_mismatch")
    if value.get("correction_metadata_used") != expected["correction_metadata"]:
        errors.append("correction_metadata_mismatch")
    missing_context = value.get("known_missing_context")
    if isinstance(missing_context, list) and not set(
        expected["known_context_gaps"]
    ) <= set(missing_context):
        errors.append("known_context_gap_hidden")
    if (
        value.get("overall") not in {"eligible", "excluded", "uncertain"}
        or not isinstance(value.get("overall_reason_codes"), list)
        or not value["overall_reason_codes"]
    ):
        errors.append("overall_invalid")
    criteria = value.get("criteria") if isinstance(value.get("criteria"), list) else []
    if not isinstance(value.get("criteria"), list):
        errors.append("criteria_invalid")
    by_id = {}
    resolved = []
    blocks = {row["source_block_id"]: row for row in segments}
    for row in criteria:
        if not isinstance(row, dict) or set(row) != {
            "criterion_id",
            "status",
            "reason_codes",
            "evidence",
            "missing_context",
        }:
            errors.append("criterion_shape_invalid")
            continue
        criterion = row.get("criterion_id")
        if criterion in by_id:
            errors.append("criterion_repeated")
            continue
        by_id[criterion] = row
        if (
            criterion not in CRITERIA
            or row.get("status") not in {"satisfied", "failed", "uncertain"}
            or not isinstance(row.get("reason_codes"), list)
            or not row["reason_codes"]
            or not all(isinstance(code, str) and code for code in row["reason_codes"])
            or not isinstance(row.get("evidence"), list)
            or not isinstance(row.get("missing_context"), list)
        ):
            errors.append(f"criterion_invalid:{criterion}")
            continue
        if row["status"] in {"satisfied", "failed"} and not row["evidence"]:
            errors.append(f"criterion_evidence_missing:{criterion}")
        if row["status"] == "uncertain" and not row["missing_context"]:
            errors.append(f"criterion_missing_context_absent:{criterion}")
        for evidence in row["evidence"]:
            locator = evidence.get("locator") if isinstance(evidence, dict) else None
            quote = evidence.get("quote") if isinstance(evidence, dict) else None
            block = (
                blocks.get(locator.get("source_block_id"))
                if isinstance(locator, dict)
                else None
            )
            if (
                not block
                or locator.get("page_id") != block["page_id"]
                or locator.get("section_id") != block["section_id"]
                or not isinstance(quote, str)
                or not quote
            ):
                errors.append(f"evidence_invalid:{criterion}")
                continue
            positions = [
                match.start() for match in re.finditer(re.escape(quote), block["text"])
            ]
            if len(positions) != 1:
                errors.append(f"evidence_unmatched_or_ambiguous:{criterion}")
                continue
            start = int(block["start"]) + positions[0]
            resolved.append(
                {
                    "criterion": criterion,
                    "locator": locator,
                    "quote": quote,
                    "start": start,
                    "end": start + len(quote),
                }
            )
    if set(by_id) != set(CRITERIA):
        errors.append("criterion_set_invalid")
    required_satisfied = {
        "published_primary_findings",
        "stable_identity_version",
        "study_geography",
        "access_rights_evidence",
    }
    if any(row.get("status") == "failed" for row in by_id.values()):
        mapped = "excluded"
    elif all(
        by_id.get(name, {}).get("status") == "satisfied" for name in required_satisfied
    ) and by_id.get("correction_retraction_coverage", {}).get("status") in {
        "satisfied",
        "uncertain",
    }:
        mapped = "eligible"
    else:
        mapped = "uncertain"
    if value.get("overall") != mapped:
        errors.append("overall_contradicts_criteria")
    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "resolved_evidence": resolved,
        "decision": value.get("overall") if not errors else "uncertain",
    }


def _status_mapping_v2(by_id: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    failed = sorted(
        criterion
        for criterion in CRITERIA
        if by_id.get(criterion, {}).get("status") == "failed"
    )
    if failed:
        return "excluded", [f"criterion_failed:{criterion}" for criterion in failed]
    required = {
        "published_primary_findings",
        "stable_identity_version",
        "study_geography",
        "access_rights_evidence",
    }
    unresolved = sorted(
        criterion
        for criterion in required
        if by_id.get(criterion, {}).get("status") != "satisfied"
    )
    correction = by_id.get("correction_retraction_coverage", {}).get("status")
    if not unresolved and correction in {"satisfied", "uncertain"}:
        return "eligible", ["all_required_criteria_satisfied"]
    if correction not in {"satisfied", "uncertain"}:
        unresolved.append("correction_retraction_coverage")
    return "uncertain", [
        f"criterion_unresolved:{criterion}" for criterion in sorted(set(unresolved))
    ]


def _span_catalog_v2(
    blocks: list[dict[str, Any]], expected_hashes: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    catalog: dict[str, dict[str, Any]] = {}
    extraction = bytearray()
    block_cursor = 0
    expected_span_number = 1
    if not isinstance(blocks, list):
        return {}, ["evidence_catalog_changed"]
    for block_number, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            errors.append("evidence_catalog_changed")
            continue
        try:
            block_text = block["text"]
            block_bytes = block_text.encode("utf-8")
            block_start = block["start_byte"]
            block_end = block["end_byte"]
            spans = block["spans"]
        except (AttributeError, KeyError):
            errors.append("evidence_catalog_changed")
            continue
        block_bounds_valid = (
            isinstance(block_start, int)
            and isinstance(block_end, int)
            and block_start == block_cursor
            and block_end == block_start + len(block_bytes)
            and block_end >= block_start
        )
        if not block_bounds_valid:
            errors.append("evidence_block_out_of_bounds")
        safe_block_start = block_start if isinstance(block_start, int) else block_cursor
        safe_block_end = (
            block_end
            if isinstance(block_end, int)
            else safe_block_start + len(block_bytes)
        )
        if not isinstance(block_text, str):
            errors.append("evidence_catalog_changed")
        expected_block_id = f"text-block-{block_number:05d}"
        actual_block_sha256 = sha256_bytes(block_bytes)
        if (
            block.get("source_block_id") != expected_block_id
            or block.get("section_id") != "extracted-text"
            or block.get("page_id") is not None
            or block.get("extraction_sha256")
            != expected_hashes.get("extracted_text_sha256")
            or block.get("block_sha256") != actual_block_sha256
            or not isinstance(spans, list)
        ):
            errors.append("evidence_catalog_changed")
            spans = spans if isinstance(spans, list) else []
        span_bytes = bytearray()
        span_cursor = safe_block_start
        for span in spans:
            if not isinstance(span, dict):
                errors.append("evidence_catalog_changed")
                continue
            span_id = span.get("span_id")
            if isinstance(span_id, str) and span_id in catalog:
                errors.append("evidence_span_duplicate")
                continue
            try:
                source_text = span["text"]
                source_bytes = source_text.encode("utf-8")
                start = span["start_byte"]
                end = span["end_byte"]
            except (AttributeError, KeyError):
                errors.append("evidence_catalog_changed")
                continue
            span_bounds_valid = (
                isinstance(start, int)
                and isinstance(end, int)
                and start == span_cursor
                and start >= safe_block_start
                and end <= safe_block_end
                and end == start + len(source_bytes)
                and end > start
            )
            if not span_bounds_valid:
                errors.append("evidence_span_out_of_bounds")
            if not isinstance(source_text, str):
                errors.append("evidence_catalog_changed")
            expected_span_id = f"s{expected_span_number:06d}"
            if (
                span_id != expected_span_id
                or span.get("source_block_id") != expected_block_id
                or span.get("section_id") != "extracted-text"
                or span.get("page_id") is not None
                or span.get("extraction_sha256")
                != expected_hashes.get("extracted_text_sha256")
                or span.get("block_sha256") != actual_block_sha256
                or span.get("span_sha256") != sha256_bytes(source_bytes)
            ):
                errors.append("evidence_catalog_changed")
            if isinstance(span_id, str):
                catalog[span_id] = span
            span_bytes.extend(source_bytes)
            span_cursor = end if isinstance(end, int) else span_cursor
            expected_span_number += 1
        if bytes(span_bytes) != block_bytes:
            errors.append("evidence_catalog_changed")
        extraction.extend(block_bytes)
        block_cursor = safe_block_end
    if sha256_bytes(bytes(extraction)) != expected_hashes.get("extracted_text_sha256"):
        errors.append("evidence_catalog_changed")
    try:
        manifest_sha256 = sha256_bytes(
            canonical_json(_span_manifest_v2(blocks)).encode()
        )
    except (KeyError, TypeError):
        manifest_sha256 = None
        errors.append("evidence_catalog_changed")
    if manifest_sha256 != expected_hashes.get("span_manifest_sha256"):
        errors.append("evidence_manifest_hash_mismatch")
    return catalog, sorted(set(errors))


def _validate_response_v2(
    value: Any,
    blocks: list[dict[str, Any]],
    *,
    expected: dict[str, Any],
    response_schema: dict[str, Any],
) -> dict[str, Any]:
    errors = _schema_errors(value, response_schema, response_schema)
    required = {
        "schema_version",
        "status_mapping_version",
        "request_id",
        "criteria",
        "known_missing_context",
        "correction_metadata_used",
        "input_echo",
    }
    if not isinstance(value, dict) or set(value) != required:
        return {
            "valid": False,
            "errors": ["response_shape_invalid"],
            "resolved_evidence": [],
            "decision": "uncertain",
            "overall_reason_codes": ["response_shape_invalid"],
            "mapping_version": ELIGIBILITY_STATUS_MAPPING_VERSION,
        }
    if (
        value.get("schema_version") != ELIGIBILITY_RESPONSE_V2
        or value.get("request_id") != expected["request_id"]
    ):
        errors.append("response_identity_mismatch")
    if value.get("status_mapping_version") != ELIGIBILITY_STATUS_MAPPING_VERSION:
        errors.append("status_mapping_version_mismatch")
    if value.get("input_echo") != expected["input_echo"]:
        errors.append("input_hash_mismatch")
    if value.get("correction_metadata_used") != expected["correction_metadata"]:
        errors.append("correction_metadata_mismatch")
    missing_context = value.get("known_missing_context")
    if isinstance(missing_context, list) and not set(
        expected["known_context_gaps"]
    ) <= set(missing_context):
        errors.append("known_context_gap_hidden")

    catalog, catalog_errors = _span_catalog_v2(blocks, expected["input_echo"])
    errors.extend(catalog_errors)
    trusted_catalog = not catalog_errors
    criteria = value.get("criteria") if isinstance(value.get("criteria"), list) else []
    if not isinstance(value.get("criteria"), list):
        errors.append("criteria_invalid")
    by_id: dict[str, dict[str, Any]] = {}
    resolved: list[dict[str, Any]] = []
    for row in criteria:
        if not isinstance(row, dict) or set(row) != {
            "criterion_id",
            "status",
            "reason_codes",
            "evidence",
            "missing_context",
        }:
            errors.append("criterion_shape_invalid")
            continue
        criterion = row.get("criterion_id")
        if not isinstance(criterion, str) or criterion not in CRITERIA:
            errors.append(f"criterion_invalid:{criterion}")
            continue
        if criterion in by_id:
            errors.append("criterion_repeated")
            continue
        by_id[criterion] = row
        if (
            row.get("status") not in {"satisfied", "failed", "uncertain"}
            or not isinstance(row.get("reason_codes"), list)
            or not row["reason_codes"]
            or not all(isinstance(code, str) and code for code in row["reason_codes"])
            or not isinstance(row.get("evidence"), list)
            or not isinstance(row.get("missing_context"), list)
        ):
            errors.append(f"criterion_invalid:{criterion}")
            continue
        if row["status"] in {"satisfied", "failed"} and not row["evidence"]:
            errors.append(f"criterion_evidence_missing:{criterion}")
        if row["status"] == "uncertain" and not row["missing_context"]:
            errors.append(f"criterion_missing_context_absent:{criterion}")
        selected_for_criterion: set[str] = set()
        for evidence in row["evidence"]:
            if not isinstance(evidence, dict) or set(evidence) != {"span_ids"}:
                errors.append(f"evidence_invalid:{criterion}")
                continue
            span_ids = evidence.get("span_ids")
            if (
                not isinstance(span_ids, list)
                or not span_ids
                or not all(isinstance(span_id, str) and span_id for span_id in span_ids)
            ):
                errors.append(f"evidence_invalid:{criterion}")
                continue
            if (
                len(span_ids) != len(set(span_ids))
                or selected_for_criterion.intersection(span_ids)
            ):
                errors.append(f"evidence_span_duplicate:{criterion}")
            selected_for_criterion.update(span_ids)
            unknown = [span_id for span_id in span_ids if span_id not in catalog]
            if unknown:
                errors.append(f"evidence_span_unknown:{criterion}")
                continue
            if trusted_catalog:
                resolved.append(
                    {
                        "criterion": criterion,
                        "span_ids": list(span_ids),
                        "spans": [
                            {
                                "span_id": span_id,
                                "locator": {
                                    "source_block_id": catalog[span_id][
                                        "source_block_id"
                                    ],
                                    "section_id": catalog[span_id]["section_id"],
                                    "page_id": catalog[span_id]["page_id"],
                                },
                                "start_byte": catalog[span_id]["start_byte"],
                                "end_byte": catalog[span_id]["end_byte"],
                                "quote": catalog[span_id]["text"],
                                "source_bytes_sha256": catalog[span_id]["span_sha256"],
                            }
                            for span_id in span_ids
                        ],
                    }
                )
    if set(by_id) != set(CRITERIA):
        errors.append("criterion_set_invalid")
    mapped, reason_codes = _status_mapping_v2(by_id)
    unique_errors = sorted(set(errors))
    return {
        "valid": not unique_errors,
        "errors": unique_errors,
        "resolved_evidence": resolved if not catalog_errors else [],
        "decision": mapped if not unique_errors else "uncertain",
        "overall_reason_codes": reason_codes,
        "mapping_version": ELIGIBILITY_STATUS_MAPPING_VERSION,
    }


def validate_response(
    value: Any,
    segments: list[dict[str, Any]],
    *,
    expected: dict[str, Any],
    response_schema: dict[str, Any],
) -> dict[str, Any]:
    if _response_contract_version(response_schema) == ELIGIBILITY_RESPONSE_V2:
        return _validate_response_v2(
            value,
            segments,
            expected=expected,
            response_schema=response_schema,
        )
    return _validate_response_v1(
        value,
        segments,
        expected=expected,
        response_schema=response_schema,
    )


class GeminiTransport:
    def __init__(self, api_base: str, api_key: str, timeout: float = 120) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def post(self, model: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}/models/{urllib.parse.quote(model, safe='')}:{method}"
        request = urllib.request.Request(
            url,
            data=canonical_json(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirects()
        )
        with opener.open(request, timeout=self.timeout) as response:
            return _strict_json_loads(response.read().decode("utf-8"))


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise urllib.error.HTTPError(
            new_url, code, "Gemini redirects are not permitted", headers, file_pointer
        )


def _credential_status(path: Path | None, env_name: str) -> tuple[bool, str]:
    if os.environ.get(env_name):
        return True, "private_environment"
    if path is None or not path.is_file():
        return False, "not_set"
    mode = stat.S_IMODE(path.stat().st_mode)
    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
    if mode != 0o600 or parent_mode & 0o077:
        return False, "unsafe_permissions"
    return True, "private_file"


def _load_key(path: Path | None, env_name: str) -> str:
    key = os.environ.get(env_name)
    if key:
        return key
    present, source = _credential_status(path, env_name)
    if not present or source != "private_file":
        raise ValueError("Gemini credential is absent or has unsafe permissions")
    value = path.read_text(encoding="utf-8").strip()  # type: ignore[union-attr]
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Gemini credential file must contain one nonempty line")
    return value


def _terminal_index(run_dir: Path) -> dict[str, str]:
    result = {}
    for directory, state in (
        ("jobs", None),
        ("errors", "screening_error"),
        ("too-large", "too_large_not_ready"),
        ("ambiguous", "ambiguous_charge"),
    ):
        for path in (run_dir / directory).glob("*.json"):
            row = _read(path)
            result[path.stem] = state or str(row.get("state") or "screening_error")
    return result


def _prepare_run_manifest(
    run_dir: Path,
    access_run_dir: Path,
    sources: list[dict[str, Any]],
    config_file: Path,
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
    safety_policy_file: Path,
    project_ledger_file: Path,
) -> dict[str, Any]:
    access_manifest = access_run_dir / "run-manifest.json"
    access_progress = access_run_dir / "progress.json"
    inputs = {
        "access_manifest": {
            "path": str(access_manifest.resolve()),
            "sha256": sha256_file(access_manifest),
        },
        "access_progress": {
            "path": str(access_progress.resolve()),
            "sha256": sha256_file(access_progress),
        },
        "config": {
            "path": str(config_file.resolve()),
            "sha256": sha256_file(config_file),
        },
        "prompt": {
            "path": str(prompt_file.resolve()),
            "sha256": sha256_file(prompt_file),
        },
        "schema": {
            "path": str(schema_file.resolve()),
            "sha256": sha256_file(schema_file),
        },
        "policy": {
            "path": str(policy_file.resolve()),
            "sha256": sha256_file(policy_file),
        },
        "safety_policy": {
            "path": str(safety_policy_file.resolve()),
            "sha256": sha256_file(safety_policy_file),
        },
    }
    manifest = {
        "schema": "gemini-eligibility-run-manifest-v1",
        "run_id": run_dir.name,
        "created_at_utc": _now(),
        "inputs": inputs,
        "project_ledger_file": str(project_ledger_file.resolve()),
        "ready_source_count": len(sources),
        "ready_source_keys_sha256": sha256_bytes(
            canonical_json([row["candidate_key"] for row in sources]).encode()
        ),
        "ready_source_identity_sha256": sha256_bytes(
            canonical_json(
                [
                    {
                        "candidate_key": row["candidate_key"],
                        "source_content_hash": row["source_content_hash"],
                        "extraction_sha256": row["extraction_sha256"],
                    }
                    for row in sources
                ]
            ).encode()
        ),
    }
    path = run_dir / "run-manifest.json"
    if path.is_file():
        old = _read(path)
        candidate = dict(manifest)
        candidate["created_at_utc"] = old.get("created_at_utc")
        if old != candidate:
            raise ValueError("cannot resume because the Gemini run manifest changed")
        return old
    atomic_json(path, manifest, immutable=True)
    return manifest


def _recover_submission_states(project_path: Path) -> None:
    project = _read(project_path)
    for job_key, request in list((project.get("requests") or {}).items()):
        run = project["runs"][request["run_key"]]
        run_dir = Path(run["run_dir"])
        terminal = _terminal_index(run_dir)
        if job_key in terminal:
            continue
        receipt = request.get("receipt") or {
            "job_key": job_key,
            "candidate_key": "unknown",
        }
        if request.get("state") == "authorized":
            _settle_submission(run_dir, job_key, None, project_path)
            request = (_read(project_path).get("requests") or {})[job_key]
        if request.get("state") == "ambiguous":
            atomic_json(
                run_dir / "ambiguous" / f"{job_key}.json",
                {
                    **receipt,
                    "state": "ambiguous_charge",
                    "error": "The prior process stopped after request authorization. Provider billing and response state are unknown.",
                    "completed_at_utc": _now(),
                },
                immutable=True,
            )
        elif request.get("state") == "settled":
            atomic_json(
                run_dir / "errors" / f"{job_key}.json",
                {
                    **receipt,
                    "state": "screening_error",
                    "phase": "post_reconciliation_recovery",
                    "error": "The prior process stopped after cost reconciliation and before a terminal decision record.",
                    "completed_at_utc": _now(),
                },
                immutable=True,
            )


def _status(
    run_dir: Path,
    *,
    config: dict[str, Any],
    sources: list[dict[str, Any]],
    key_present: bool,
    enabled: bool,
    state: str,
    live_call_made: bool,
    estimated: Decimal,
) -> dict[str, Any]:
    terminals = _terminal_index(run_dir)
    manifest_path = run_dir / "run-manifest.json"
    manifest = _read(manifest_path) if manifest_path.is_file() else {}
    source_keys_hash = manifest.get("ready_source_keys_sha256")
    counts = {
        "full_text_ready": len(sources),
        "queued": max(len(sources) - len(terminals), 0),
        "completed": 0,
        "eligible": 0,
        "excluded": 0,
        "uncertain": 0,
        "screening_error": 0,
        "too_large_not_ready": 0,
        "ambiguous_charge": 0,
    }
    overlay = []
    for directory, fixed in (
        ("jobs", None),
        ("errors", "screening_error"),
        ("too-large", "too_large_not_ready"),
        ("ambiguous", "ambiguous_charge"),
    ):
        for path in (run_dir / directory).glob("*.json"):
            row = _read(path)
            if directory == "jobs":
                status = (
                    (row.get("validation") or {}).get("decision", "uncertain")
                    if row.get("state") == "completed"
                    else "screening_error"
                )
                if row.get("state") == "completed":
                    counts["completed"] += 1
                counts[status] += 1
            else:
                status = fixed  # type: ignore[assignment]
                counts[status] += 1  # type: ignore[index]
            overlay.append(
                {
                    "schema": "gemini-eligibility-overlay-row-v1",
                    "run_id": run_dir.name,
                    "ready_source_keys_sha256": source_keys_hash,
                    "candidate_key": row["candidate_key"],
                    "gemini_status": status,
                    "gemini_decision": status
                    if status in {"eligible", "excluded", "uncertain"}
                    else None,
                    "job_key": row["job_key"],
                }
            )
    terminal_candidates = {row["candidate_key"] for row in overlay}
    if len(terminal_candidates) != len(overlay):
        raise ValueError("a candidate has more than one Gemini terminal record")
    overlay.extend(
        {
            "schema": "gemini-eligibility-overlay-row-v1",
            "run_id": run_dir.name,
            "ready_source_keys_sha256": source_keys_hash,
            "candidate_key": row["candidate_key"],
            "gemini_status": "queued",
            "gemini_decision": None,
            "job_key": None,
        }
        for row in sources
        if row["candidate_key"] not in terminal_candidates
    )
    overlay_path = run_dir / "gemini-overlay.ndjson"
    atomic_write(
        overlay_path,
        b"".join((canonical_json(row) + "\n").encode() for row in overlay),
    )
    shown_state = state
    if not enabled:
        shown_state = "disabled_by_policy"
    elif not key_present and state == "prepared":
        shown_state = "disabled_no_key"
    status = {
        "schema": "gemini-eligibility-progress-v1",
        "state": shown_state,
        "model": config["model"],
        "configured_fallback": None,
        "updated_at_utc": _now(),
        "counts": counts,
        "overlay": {
            "file": overlay_path.name,
            "sha256": sha256_file(overlay_path),
            "rows": len(overlay),
            "ready_source_keys_sha256": source_keys_hash,
        },
        "run_manifest_sha256": sha256_file(manifest_path)
        if manifest_path.is_file()
        else None,
        "estimated_max_cost_usd": str(estimated),
        "budget": _read(run_dir / "budget-ledger.json"),
        "live_call_made": live_call_made,
        "safety": {
            "live_generation_enabled": enabled,
            "maximum_generation_submissions": 3,
            "maximum_count_requests": 10,
            "maximum_request_reserved_cost_usd": "0.25",
            "maximum_run_wall_seconds": 1200,
            "minimum_seconds_between_submissions": 60,
        },
        "message": "Gemini generation is disabled by the captain safety policy."
        if not enabled
        else "Gemini eligibility state comes from durable job records.",
    }
    atomic_json(run_dir / "progress.json", status)
    return status


def run_gemini_eligibility(
    *,
    action: str,
    access_run_dir: Path,
    run_dir: Path,
    config_file: Path,
    prompt_file: Path,
    schema_file: Path,
    policy_file: Path,
    safety_policy_file: Path,
    project_ledger_file: Path,
    max_cost_usd: Decimal,
    credential_file: Path | None = None,
    transport: GeminiTransport | Any | None = None,
) -> dict[str, Any]:
    if action not in {"doctor", "dry-run", "run", "resume", "pause", "status"}:
        raise ValueError("Gemini eligibility action is not supported")
    if action in {"run", "resume"} and transport is None:
        raise ValueError(
            "standalone Gemini execution is disabled; use the shared streaming broker"
        )
    config = _config(config_file)
    safety = _safety(safety_policy_file)
    if (
        config["maximum_output_tokens"]
        > safety["maximum_output_tokens_including_thinking"]
    ):
        raise ValueError("Gemini output tokens exceed the safety policy")
    key_present, credential_source = _credential_status(
        credential_file, config["api_key_environment_variable"]
    )
    if action == "doctor":
        state = "disabled_by_policy"
        if safety["live_generation_enabled"]:
            state = "ready" if key_present else "disabled_no_key"
        return {
            "schema": "gemini-eligibility-doctor-v1",
            "state": state,
            "model": config["model"],
            "fallback_model": None,
            "credential_source": credential_source,
            "live_generation_enabled": safety["live_generation_enabled"],
            "live_call_made": False,
            "price_valid_through": config["price_valid_through"],
        }

    prompt = prompt_file.read_text(encoding="utf-8")
    schema = _read(schema_file)
    policy = _read(policy_file)
    init_budget(run_dir, config, max_cost_usd, project_ledger_file, safety)
    access_progress = _read(access_run_dir / "progress.json")
    access_manifest = _read(access_run_dir / "run-manifest.json")
    quality_notice_path = access_run_dir / "quality-notice-r1.json"
    access_quarantined = False
    if quality_notice_path.is_file():
        quality_notice = _read(quality_notice_path)
        if quality_notice.get(
            "schema"
        ) != "article-access-quality-notice-v1" or quality_notice.get(
            "run_id"
        ) != access_manifest.get("run_id"):
            raise ValueError("the article-access quality notice is invalid")
        access_quarantined = quality_notice.get("status") == "superseded_quarantined"
    sources: list[dict[str, Any]] = []
    item_paths = (
        []
        if access_quarantined
        else sorted((access_run_dir / "items").glob("item-*.json"))
    )
    for path in item_paths:
        row = _read(path)
        if row.get("access_state") != "full_text_ready":
            continue
        position = int(row.get("position") or 0)
        if (
            row.get("schema") != "article-access-item-v1"
            or row.get("run_id") != access_manifest.get("run_id")
            or not 1 <= position <= int(access_manifest["target_total"])
            or row.get("candidate_key")
            != access_manifest["selection"][position - 1]["candidate_key"]
            or row.get("identity_verified") is not True
        ):
            raise ValueError(
                "a ready source receipt does not match the access manifest"
            )
        text_path = Path(str(row.get("extraction_path") or ""))
        if (
            not text_path.is_file()
            or sha256_file(text_path) != row.get("extraction_sha256")
            or (row.get("extraction_coverage") or {}).get("article_body_recognized")
            is not True
        ):
            raise ValueError("a ready source extraction is not verifiable")
        sources.append(row)

    terminals = _terminal_index(run_dir)
    planned: list[tuple[str, dict[str, Any], str, dict[str, Any], dict[str, str]]] = []
    estimated = Decimal("0")
    for source in sources:
        text = Path(source["extraction_path"]).read_text(encoding="utf-8")
        job_key = _job_key(source, config, prompt_file, schema_file, policy_file)
        payload, hashes = _request_payload(
            source=source,
            policy=policy,
            text=text,
            prompt=prompt,
            schema=schema,
            config=config,
            request_id=job_key,
            policy_sha256=sha256_file(policy_file),
        )
        estimate = (len(canonical_json(payload).encode()) + 3) // 4
        if job_key not in terminals:
            estimated += _cost(config, estimate, int(config["maximum_output_tokens"]))
            planned.append((job_key, source, text, payload, hashes))

    if action in {"status", "pause"}:
        return _status(
            run_dir,
            config=config,
            sources=sources,
            key_present=key_present,
            enabled=safety["live_generation_enabled"],
            state="paused" if action == "pause" else "prepared",
            live_call_made=False,
            estimated=estimated,
        )

    if access_quarantined:
        raise ValueError(
            "Gemini eligibility cannot use a quarantined article-access run"
        )

    if (
        access_progress.get("state") != "completed"
        or not (access_run_dir / "run-receipt.json").is_file()
    ):
        raise ValueError("Gemini eligibility requires a completed article-access stage")
    _prepare_run_manifest(
        run_dir,
        access_run_dir,
        sources,
        config_file,
        prompt_file,
        schema_file,
        policy_file,
        safety_policy_file,
        project_ledger_file,
    )
    if action == "dry-run":
        return _status(
            run_dir,
            config=config,
            sources=sources,
            key_present=key_present,
            enabled=safety["live_generation_enabled"],
            state="prepared",
            live_call_made=False,
            estimated=estimated,
        )
    if not safety["live_generation_enabled"]:
        _status(
            run_dir,
            config=config,
            sources=sources,
            key_present=key_present,
            enabled=False,
            state="prepared",
            live_call_made=False,
            estimated=estimated,
        )
        raise ValueError("Gemini generation is disabled by the captain safety policy")
    if not key_present and transport is None:
        raise ValueError("Gemini credential is not available")

    execution_path = project_ledger_file.with_name(
        f".{project_ledger_file.name}.generation.lock"
    )
    execution_lock = execution_path.open("a+")
    try:
        fcntl.flock(execution_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        execution_lock.close()
        raise ValueError("another Gemini generation process is active") from error
    _recover_submission_states(project_ledger_file)
    terminals = _terminal_index(run_dir)
    planned = [item for item in planned if item[0] not in terminals]
    if _decimal(_read(project_ledger_file)["ambiguous_reserved_usd"], "ambiguous") != 0:
        execution_lock.close()
        result = _status(
            run_dir,
            config=config,
            sources=sources,
            key_present=key_present,
            enabled=True,
            state="paused",
            live_call_made=False,
            estimated=estimated,
        )
        result["message"] = "An ambiguous Gemini charge requires manual reconciliation."
        atomic_json(run_dir / "progress.json", result)
        return result

    client = transport or GeminiTransport(
        config["api_base"],
        _load_key(credential_file, config["api_key_environment_variable"]),
    )
    started = time.monotonic()
    live_call_made = False
    stopped = False
    try:
        for job_key, source, text, payload, hashes in planned:
            project = _read(project_ledger_file)
            if (
                stopped
                or time.monotonic() - started >= safety["maximum_run_wall_seconds"]
                or project["phase_generation_submissions"]
                >= safety["maximum_generation_submissions_initial_phase"]
            ):
                break
            last = project.get("last_submission_at_utc")
            if last:
                elapsed = (
                    datetime.now(UTC)
                    - datetime.fromisoformat(last.replace("Z", "+00:00"))
                ).total_seconds()
                wait_seconds = max(
                    float(safety["minimum_seconds_between_generation_submissions"])
                    - elapsed,
                    0,
                )
                if (
                    time.monotonic() - started + wait_seconds
                    >= safety["maximum_run_wall_seconds"]
                ):
                    break
                if wait_seconds:
                    time.sleep(wait_seconds)
            base = {
                "schema": "gemini-eligibility-job-v1",
                "job_key": job_key,
                "candidate_key": source["candidate_key"],
                "model": config["model"],
                "source_content_hash": source["source_content_hash"],
                "extraction_sha256": source["extraction_sha256"],
                "policy_sha256": sha256_file(policy_file),
                "prompt_sha256": sha256_file(prompt_file),
                "schema_sha256": sha256_file(schema_file),
                "request_sha256": sha256_bytes(canonical_json(payload).encode()),
                "started_at_utc": _now(),
                **_persist_span_manifest_v2(
                    run_dir,
                    job_key,
                    text,
                    str(source["extraction_sha256"]),
                    schema,
                ),
            }
            try:
                _phase_event(run_dir, safety, project_ledger_file, "count")
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
                exact_value = count_result["totalTokens"]
                if isinstance(exact_value, bool) or not isinstance(exact_value, int):
                    raise ValueError("countTokens result is not an integer")
                exact_input = exact_value
                if exact_input < 0:
                    raise ValueError("countTokens result is negative")
                atomic_json(
                    run_dir / "counts" / f"{job_key}.json",
                    {
                        **base,
                        "state": "counted",
                        "input_tokens": exact_input,
                        "recorded_at_utc": _now(),
                    },
                    immutable=True,
                )
            except Exception as error:
                details = f"{type(error).__name__}: {error}"
                status_code = (
                    error.code if isinstance(error, urllib.error.HTTPError) else None
                )
                atomic_json(
                    run_dir / "errors" / f"{job_key}.json",
                    {
                        **base,
                        "state": "screening_error",
                        "phase": "countTokens",
                        "http_status": status_code,
                        "error": details,
                        "completed_at_utc": _now(),
                    },
                    immutable=True,
                )
                stopped = True
                continue
            if exact_input > int(config["maximum_input_tokens"]):
                atomic_json(
                    run_dir / "too-large" / f"{job_key}.json",
                    {
                        **base,
                        "state": "too_large_not_ready",
                        "input_tokens": exact_input,
                        "completed_at_utc": _now(),
                    },
                    immutable=True,
                )
                stopped = True
                continue
            reserved = _cost(config, exact_input, int(config["maximum_output_tokens"]))
            submission = {
                **base,
                "state": "submitted",
                "input_tokens": exact_input,
                "reserved_usd": str(reserved),
                "submitted_at_utc": _now(),
            }
            try:
                _authorize_submission(
                    run_dir,
                    job_key,
                    reserved,
                    safety,
                    project_ledger_file,
                    submission,
                )
            except ValueError as error:
                atomic_json(
                    run_dir / "errors" / f"{job_key}.json",
                    {
                        **base,
                        "state": "screening_error",
                        "phase": "pre_submit",
                        "error": str(error),
                        "completed_at_utc": _now(),
                    },
                    immutable=True,
                )
                stopped = True
                continue
            atomic_json(
                run_dir / "submitted" / f"{job_key}.json",
                submission,
                immutable=True,
            )
            live_call_made = True
            try:
                raw = client.post(config["model"], "generateContent", payload)
            except urllib.error.HTTPError as error:
                _settle_submission(run_dir, job_key, Decimal("0"), project_ledger_file)
                atomic_json(
                    run_dir / "errors" / f"{job_key}.json",
                    {
                        **submission,
                        "state": "screening_error",
                        "phase": "generateContent",
                        "charge_state": "known_response_without_usage",
                        "http_status": error.code,
                        "retry_after": error.headers.get("Retry-After")
                        if error.headers
                        else None,
                        "error": f"HTTPError: HTTP {error.code}",
                        "completed_at_utc": _now(),
                    },
                    immutable=True,
                )
                stopped = True
                continue
            except Exception as error:
                _settle_submission(run_dir, job_key, None, project_ledger_file)
                atomic_json(
                    run_dir / "ambiguous" / f"{job_key}.json",
                    {
                        **submission,
                        "state": "ambiguous_charge",
                        "error": f"{type(error).__name__}: provider outcome unknown",
                        "completed_at_utc": _now(),
                    },
                    immutable=True,
                )
                stopped = True
                continue
            atomic_json(
                run_dir / "responses" / f"{job_key}.json",
                {**submission, "state": "response_received", "raw_response": raw},
                immutable=True,
            )
            usage = raw.get("usageMetadata") if isinstance(raw, dict) else None
            try:
                if not isinstance(usage, dict):
                    raise ValueError("provider usage is absent")
                values = [
                    usage[name]
                    for name in (
                        "promptTokenCount",
                        "candidatesTokenCount",
                        "thoughtsTokenCount",
                        "totalTokenCount",
                    )
                ]
                if any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in values
                ):
                    raise ValueError("provider usage is not an integer")
                prompt_tokens, candidate_tokens, thought_tokens, total_tokens = values
                if min(values) < 0 or total_tokens != sum(values[:3]):
                    raise ValueError("provider usage is inconsistent")
                actual = _cost(config, prompt_tokens, candidate_tokens + thought_tokens)
                if actual > reserved:
                    raise ValueError("provider usage exceeds the reservation")
            except (KeyError, TypeError, ValueError) as error:
                _settle_submission(run_dir, job_key, None, project_ledger_file)
                atomic_json(
                    run_dir / "ambiguous" / f"{job_key}.json",
                    {
                        **submission,
                        "state": "ambiguous_charge",
                        "phase": "usage",
                        "error": str(error),
                        "completed_at_utc": _now(),
                    },
                    immutable=True,
                )
                stopped = True
                continue
            _settle_submission(run_dir, job_key, actual, project_ledger_file)
            candidates = raw.get("candidates") or []
            parsed = None
            if (
                not isinstance(candidates, list)
                or len(candidates) != 1
                or not isinstance(candidates[0], dict)
                or candidates[0].get("finishReason") != "STOP"
            ):
                validation = {
                    "valid": False,
                    "errors": ["candidate_count_or_finish_reason_invalid"],
                    "resolved_evidence": [],
                    "decision": "uncertain",
                }
            else:
                try:
                    parts = candidates[0]["content"]["parts"]
                    if not isinstance(parts, list) or len(parts) != 1:
                        raise ValueError("response parts are not exact")
                    response_text = parts[0]["text"]
                    if not isinstance(response_text, str) or not response_text:
                        raise ValueError("response text is empty")
                    parsed = _strict_json_loads(response_text)
                    validation = validate_response(
                        parsed,
                        _validation_evidence(
                            text, str(source["extraction_sha256"]), schema
                        ),
                        expected={
                            "request_id": job_key,
                            "input_echo": hashes,
                            "correction_metadata": _correction_metadata(source),
                            "known_context_gaps": _known_context_gaps(source),
                        },
                        response_schema=schema,
                    )
                except (
                    IndexError,
                    KeyError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ):
                    validation = {
                        "valid": False,
                        "errors": ["refusal_block_or_malformed_response"],
                        "resolved_evidence": [],
                        "decision": "uncertain",
                    }
            atomic_json(
                run_dir / "jobs" / f"{job_key}.json",
                {
                    **submission,
                    "state": "completed" if validation["valid"] else "screening_error",
                    "completed_at_utc": _now(),
                    "model_version": raw.get("modelVersion"),
                    "response_id": raw.get("responseId"),
                    "raw_response": raw,
                    "parsed_response": parsed,
                    "validation": validation,
                    "usage": usage,
                    "actual_cost_usd": str(actual),
                },
                immutable=True,
            )
            if not validation["valid"]:
                stopped = True
    finally:
        execution_lock.close()
    state = (
        "paused"
        if stopped or len(_terminal_index(run_dir)) < len(sources)
        else "completed"
    )
    return _status(
        run_dir,
        config=config,
        sources=sources,
        key_present=key_present,
        enabled=True,
        state=state,
        live_call_made=live_call_made,
        estimated=Decimal("0"),
    )
