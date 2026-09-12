from __future__ import annotations

import html.parser
import json
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import SourceURLError
from .storage import SafeHTTPSRedirectHandler
from .util import atomic_json, atomic_write, canonical_json, sha256_bytes, sha256_file


MANIFEST_SCHEMA = "source-screening-run-manifest-v1"
PROGRESS_SCHEMA = "source-screening-progress-v1"
OVERLAY_SCHEMA = "source-screening-overlay-v1"
RECEIPT_SCHEMA = "source-screening-run-receipt-v1"
USER_AGENT = "arctic-qa-source-screening-r1/1.0"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-")[:100]


def _validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema") != "source-screening-policy-v1":
        raise ValueError("unsupported source-screening policy schema")
    for field in (
        "selection_size",
        "smoke_size",
        "maximum_attempts_per_source",
        "maximum_bytes_per_source",
        "maximum_aggregate_new_bytes",
        "maximum_network_seconds",
    ):
        if not isinstance(policy.get(field), int) or policy[field] < 1:
            raise ValueError(f"source-screening policy field must be positive: {field}")
    if policy["smoke_size"] > policy["selection_size"]:
        raise ValueError("source-screening smoke size exceeds selection size")
    if policy["maximum_attempts_per_source"] > 3:
        raise ValueError("source-screening attempts cannot exceed three")
    if policy["maximum_bytes_per_source"] > 50 * 1024 * 1024:
        raise ValueError("source-screening per-source size exceeds 50 MiB")
    if policy["maximum_aggregate_new_bytes"] > 5 * 1024 * 1024 * 1024:
        raise ValueError("source-screening aggregate size exceeds 5 GiB")
    if policy["maximum_network_seconds"] > 90 * 60:
        raise ValueError("source-screening network time exceeds 90 minutes")


def _queue_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not record.get("candidate_key"):
                raise ValueError(f"review queue key is missing at line {line_number}")
            records.append(record)
    return records


def _select_records(
    queue: list[dict[str, Any]], policy: dict[str, Any]
) -> list[dict[str, Any]]:
    allowed = list(policy["selection_queues"])
    rank = {name: index for index, name in enumerate(allowed)}
    eligible = [row for row in queue if (row.get("queue") or {}).get("name") in rank]
    eligible.sort(
        key=lambda row: (
            rank[(row.get("queue") or {})["name"]],
            int(row.get("sequence") or 0),
        )
    )
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in eligible:
        key = str(row["candidate_key"])
        if key in seen:
            raise ValueError(f"review queue repeats candidate key: {key}")
        seen.add(key)
        selected.append(row)
        if len(selected) == policy["selection_size"]:
            break
    if len(selected) != policy["selection_size"]:
        raise ValueError("review queue does not contain enough selectable candidates")
    return selected


def prepare_source_pass(
    *,
    queue_file: Path,
    candidates_file: Path,
    protocol_file: Path,
    prior_screening_file: Path,
    policy_file: Path,
    output_dir: Path,
    run_id: str,
    code_commit: str,
    viewer_progress_file: Path | None = None,
) -> dict[str, Any]:
    policy = _read_json(policy_file)
    protocol = _read_json(protocol_file)
    candidates = _read_json(candidates_file)
    _validate_policy(policy)
    if protocol.get("protocol_id") != policy.get("source_protocol_id"):
        raise ValueError("source-screening policy and protocol do not match")
    if not isinstance(candidates, list):
        raise ValueError("candidate input must be a JSON array")
    candidate_map = {str(row.get("candidate_key")): row for row in candidates}
    if len(candidate_map) != len(candidates):
        raise ValueError("candidate input contains a missing or repeated key")
    selected_queue = _select_records(_queue_records(queue_file), policy)
    selection = []
    for position, queued in enumerate(selected_queue, 1):
        key = str(queued["candidate_key"])
        candidate = candidate_map.get(key)
        if candidate is None:
            raise ValueError(f"selected queue key is absent from discovery: {key}")
        selection.append(
            {
                "position": position,
                "candidate_key": key,
                "selection_queue": (queued.get("queue") or {}).get("name"),
                "discovery_sequence": queued.get("sequence"),
                "metadata_disposition": queued.get("metadata_disposition"),
                "selection_reason": queued.get("reason_code"),
                "doi": candidate.get("doi"),
                "stable_id": candidate.get("stable_id"),
                "title": candidate.get("title"),
                "authors": candidate.get("authors") or [],
                "year": candidate.get("year"),
                "provider_type": candidate.get("type"),
                "open_access": candidate.get("open_access") or {},
                "landing_url": candidate.get("landing_url"),
                "version_relation": candidate.get("version_relation"),
                "correction_retraction": candidate.get("retraction_correction"),
            }
        )
    inputs = {
        "review_queue": _file_record(queue_file),
        "candidates": _file_record(candidates_file),
        "protocol": _file_record(protocol_file),
        "prior_screening": _file_record(prior_screening_file),
        "policy": _file_record(policy_file),
    }
    selection_hash = sha256_bytes(
        canonical_json([row["candidate_key"] for row in selection]).encode()
    )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "run_id": run_id,
        "policy_id": policy["policy_id"],
        "policy_hash": inputs["policy"]["sha256"],
        "producer_code_commit": code_commit,
        "created_at_utc": _now(),
        "selection_rule": policy["selection_rule"],
        "selection_size": policy["selection_size"],
        "smoke_size": policy["smoke_size"],
        "selection_keys_sha256": selection_hash,
        "inputs": inputs,
        "supersedes": {
            "path": str(prior_screening_file),
            "sha256": inputs["prior_screening"]["sha256"],
            "scope": "historical_initial_overlay",
        },
        "limits": {
            key: policy[key]
            for key in (
                "maximum_attempts_per_source",
                "maximum_bytes_per_source",
                "maximum_aggregate_new_bytes",
                "maximum_network_seconds",
                "minimum_seconds_between_requests",
                "request_timeout_seconds",
            )
        },
        "selection": selection,
    }
    manifest_path = output_dir / "run-manifest.json"
    if manifest_path.is_file():
        existing = _read_json(manifest_path)
        comparable = dict(manifest)
        comparable["created_at_utc"] = existing.get("created_at_utc")
        if existing != comparable:
            raise ValueError("cannot resume because the source-pass manifest changed")
        manifest = existing
    else:
        atomic_json(manifest_path, manifest, immutable=True)
    progress_path = output_dir / "progress.json"
    if not progress_path.is_file():
        progress = _progress_payload(manifest, "prepared", {}, None)
        atomic_json(progress_path, progress)
        _write_viewer_progress(viewer_progress_file, progress)
    return manifest


def _verify_inputs(manifest: dict[str, Any]) -> None:
    for name, expected in manifest["inputs"].items():
        path = Path(expected["path"])
        if not path.is_file() or _file_record(path) != expected:
            raise ValueError(f"cannot resume because source-pass input changed: {name}")


def _item_receipts(output_dir: Path) -> dict[int, dict[str, Any]]:
    receipts: dict[int, dict[str, Any]] = {}
    for path in sorted((output_dir / "items").glob("item-*.json")):
        receipt = _read_json(path)
        position = int(receipt["position"])
        if position in receipts:
            raise ValueError(f"source-pass item position repeats: {position}")
        receipts[position] = receipt
    return receipts


def _attempt_receipts(
    output_dir: Path, position: int, candidate_key: str
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    pattern = f"item-{position:06d}-attempt-*.json"
    for path in sorted((output_dir / "attempts").glob(pattern)):
        row = _read_json(path)
        expected_number = len(attempts) + 1
        if (
            row.get("candidate_key") != candidate_key
            or row.get("attempt") != expected_number
        ):
            raise ValueError(f"source-pass attempt receipt is inconsistent: {path}")
        attempts.append(
            {key: value for key, value in row.items() if key != "candidate_key"}
        )
    return attempts


def _counts(
    manifest: dict[str, Any], receipts: dict[int, dict[str, Any]], records=None
) -> dict[str, int]:
    attempted = sum(bool(row.get("attempts")) for row in receipts.values())
    counts = {
        "selected": manifest["selection_size"],
        "processed": len(receipts),
        "attempted": attempted,
        "retrieved": sum(
            str(row.get("access_state", "")).startswith("retrieved_")
            for row in receipts.values()
        ),
        "full_text_retrieved": sum(
            row.get("access_state") == "retrieved_full_text"
            for row in receipts.values()
        ),
        "full_text_reviewed": 0,
        "full_text_review_pending": sum(
            row.get("access_state") == "retrieved_full_text"
            for row in receipts.values()
        ),
        "eligible": 0,
        "excluded": 0,
        "pending": 0,
        "unattempted": manifest["selection_size"] - attempted,
    }
    if records is not None:
        counts["full_text_reviewed"] = sum(
            row.get("access_state") == "retrieved_full_text"
            and bool(row.get("decision_method_version"))
            for row in records
        )
        counts["full_text_review_pending"] = (
            counts["full_text_retrieved"] - counts["full_text_reviewed"]
        )
        counts["eligible"] = sum(
            row.get("scientific_eligibility") == "eligible" for row in records
        )
        counts["excluded"] = sum(
            row.get("scientific_eligibility") == "excluded" for row in records
        )
        counts["pending"] = sum(
            row.get("scientific_eligibility") == "pending" for row in records
        )
        counts["unattempted"] = sum(
            row.get("scientific_eligibility") == "unreviewed" for row in records
        )
    else:
        counts["pending"] = attempted
    return {key: int(value) for key, value in counts.items()}


def _progress_payload(
    manifest: dict[str, Any],
    state: str,
    receipts: dict[int, dict[str, Any]],
    message: str | None,
    *,
    started_at: str | None = None,
    completed_at: str | None = None,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": PROGRESS_SCHEMA,
        "state": state,
        "stage": "source_screening",
        "run_id": manifest["run_id"],
        "policy_id": manifest["policy_id"],
        "producer_code_commit": manifest["producer_code_commit"],
        "updated_at_utc": _now(),
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "message": message or "The source pass is prepared and is not running.",
        "counts": _counts(manifest, receipts, records),
        "selection_keys_sha256": manifest["selection_keys_sha256"],
    }


def _write_viewer_progress(path: Path | None, progress: dict[str, Any]) -> None:
    if path is None:
        return
    viewer_state = (
        "not_running" if progress["state"] == "prepared" else progress["state"]
    )
    atomic_json(
        path,
        {
            "schema": "corpus-progress-v1",
            "state": viewer_state,
            "stage": "source_screening",
            "updated_at_utc": progress["updated_at_utc"],
            "message": progress["message"],
            "run_id": progress["run_id"],
            "policy_id": progress["policy_id"],
            "processed": progress["counts"]["processed"],
            "total": progress["counts"]["selected"],
            "started_at_utc": progress.get("started_at_utc"),
            "completed_at_utc": progress.get("completed_at_utc"),
            "source_counts": progress["counts"],
        },
    )


class _TextCollector(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _extract_text(body: bytes, media_type: str) -> tuple[str, str, bool]:
    if media_type == "application/pdf":
        if not body.startswith(b"%PDF"):
            raise ValueError("retrieved PDF does not have a PDF signature")
        with tempfile.TemporaryDirectory(prefix="arctic-source-pass-") as directory:
            source = Path(directory) / "source.pdf"
            target = Path(directory) / "source.txt"
            source.write_bytes(body)
            result = subprocess.run(
                ["pdftotext", "-layout", str(source), str(target)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                raise ValueError(f"pdftotext error: {result.stderr.strip()[:500]}")
            return (
                target.read_text(encoding="utf-8", errors="replace"),
                "pdftotext-layout",
                True,
            )
    raw = body.decode("utf-8", errors="replace")
    if media_type in {"application/xml", "text/xml"}:
        root = ET.fromstring(raw)
        return " ".join(" ".join(root.itertext()).split()), "xml-itertext", True
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _TextCollector()
        parser.feed(raw)
        return (
            "\n".join(part.strip() for part in parser.parts if part.strip()),
            "html-text",
            False,
        )
    if media_type == "text/plain":
        return raw, "utf8-replacement", False
    raise ValueError(f"unsupported retrieved media type: {media_type}")


def _identity_resolves(candidate: dict[str, Any], text: str) -> bool:
    identity_region = _identity_region(text)
    doi = _normalize_doi(str(candidate.get("doi") or ""))
    if doi and doi in _doi_tokens(identity_region):
        return True
    normalized = " ".join(identity_region.casefold().split())
    title = " ".join(str(candidate.get("title") or "").casefold().split())
    return bool(len(title) >= 20 and title in normalized)


def _identity_region(text: str) -> str:
    """Return the front matter before a reference section, with a fixed bound."""
    front = text[:12000]
    reference = re.search(
        r"(?:^|[\r\n])\s*(?:references|bibliography)\s*(?:[\r\n]|$)",
        front,
        flags=re.IGNORECASE,
    )
    return front[: reference.start()] if reference else front


def _normalize_doi(value: str) -> str:
    normalized = urllib.parse.unquote(value).strip().casefold()
    normalized = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", normalized)
    return normalized.rstrip(".,;:)]}")


def _doi_tokens(text: str) -> set[str]:
    return {
        _normalize_doi(match.group(0))
        for match in re.finditer(
            r"(?<![a-z0-9])10\.\d{4,9}/[^\s\"<>]+",
            urllib.parse.unquote(text).casefold(),
        )
    }


def _validate_source_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("source-pass retrieval requires an HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source-pass URL cannot contain credentials")


def _network_fetch(url: str, *, max_bytes: int, timeout: float) -> dict[str, Any]:
    _validate_source_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/pdf,application/xml,text/xml,text/html;q=0.8,*/*;q=0.1",
        },
    )
    opener = urllib.request.build_opener(SafeHTTPSRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                return {
                    "state": "error",
                    "reason_code": "content_length_over_limit",
                    "status": response.status,
                    "declared_bytes": int(declared),
                    "retryable": False,
                }
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                return {
                    "state": "error",
                    "reason_code": "stream_over_limit",
                    "observed_bytes": len(body),
                    "retryable": False,
                }
            return {
                "state": "retrieved",
                "status": response.status,
                "final_url": response.geturl(),
                "media_type": response.headers.get_content_type().casefold(),
                "body": body,
            }
    except SourceURLError:
        return {
            "state": "error",
            "reason_code": "redirect_url_not_https",
            "retryable": False,
        }
    except urllib.error.HTTPError as error:
        return {
            "state": "error",
            "reason_code": "http_error",
            "status": error.code,
            "retryable": error.code in {429, 500, 502, 503, 504},
        }
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        return {
            "state": "error",
            "reason_code": "transport_error",
            "error": f"{type(error).__name__}: {error}",
            "retryable": True,
        }


def _process_item(
    *,
    candidate: dict[str, Any],
    manifest: dict[str, Any],
    output_dir: Path,
    fetcher: Callable[..., dict[str, Any]],
    sleep_fn: Callable[[float], None],
) -> dict[str, Any]:
    position = int(candidate["position"])
    key = str(candidate["candidate_key"])
    prior_attempts = _attempt_receipts(output_dir, position, key)
    started = (
        str(prior_attempts[0].get("attempted_at_utc")) if prior_attempts else _now()
    )
    url = str((candidate.get("open_access") or {}).get("url") or "")
    base = {
        "schema": "source-screening-item-receipt-v1",
        "run_id": manifest["run_id"],
        "position": position,
        "candidate_key": key,
        "title": candidate.get("title"),
        "doi": candidate.get("doi"),
        "started_at_utc": started,
        "source_url": url or None,
        "attempts": prior_attempts,
    }
    if not url:
        return {
            **base,
            "completed_at_utc": _now(),
            "access_state": "unattempted_no_open_access_url",
            "reason_code": "no_open_access_source_url",
            "scientific_eligibility": "unreviewed",
        }
    try:
        _validate_source_url(url)
    except ValueError:
        return {
            **base,
            "completed_at_utc": _now(),
            "access_state": "unattempted_unsafe_source_url",
            "reason_code": "source_url_not_https",
            "scientific_eligibility": "unreviewed",
        }
    limits = manifest["limits"]
    if len(prior_attempts) > limits["maximum_attempts_per_source"]:
        raise ValueError("source-pass attempt receipts exceed the configured limit")
    result: dict[str, Any] | None = None
    attempts = list(prior_attempts)
    if attempts and attempts[-1].get("state") == "retrieved":
        return {
            **base,
            "attempts": attempts,
            "completed_at_utc": _now(),
            "access_state": "retrieval_interrupted_after_response",
            "reason_code": "retrieved_response_body_not_committed_before_interruption",
            "scientific_eligibility": "pending",
        }
    if attempts and not attempts[-1].get("retryable"):
        result = attempts[-1]
    for attempt in range(len(attempts) + 1, limits["maximum_attempts_per_source"] + 1):
        if attempts:
            sleep_fn(float(limits["minimum_seconds_between_requests"]))
        attempted_at = _now()
        result = fetcher(
            url,
            max_bytes=int(limits["maximum_bytes_per_source"]),
            timeout=float(limits["request_timeout_seconds"]),
        )
        attempt_row = {key: value for key, value in result.items() if key != "body"}
        attempt_row.update({"attempt": attempt, "attempted_at_utc": attempted_at})
        attempts.append(attempt_row)
        atomic_json(
            output_dir / "attempts" / f"item-{position:06d}-attempt-{attempt}.json",
            {"candidate_key": key, **attempt_row},
            immutable=True,
        )
        if result.get("state") == "retrieved" or not result.get("retryable"):
            break
    if result is None:
        result = attempts[-1]
    if result.get("state") != "retrieved":
        return {
            **base,
            "attempts": attempts,
            "completed_at_utc": _now(),
            "access_state": "not_retrieved",
            "reason_code": result.get("reason_code") or "retrieval_error",
            "scientific_eligibility": "pending",
        }
    body = result.get("body")
    if not isinstance(body, bytes) or not body:
        return {
            **base,
            "attempts": attempts,
            "completed_at_utc": _now(),
            "access_state": "retrieved_invalid_content",
            "reason_code": "empty_or_invalid_response_body",
            "scientific_eligibility": "pending",
        }
    digest = sha256_bytes(body)
    media_type = str(result.get("media_type") or "application/octet-stream").casefold()
    suffix = {
        "application/pdf": ".pdf",
        "application/xml": ".xml",
        "text/xml": ".xml",
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "text/plain": ".txt",
    }.get(media_type, ".bin")
    original = output_dir / "originals" / digest[:2] / digest / f"source{suffix}"
    atomic_write(original, body, immutable=True)
    try:
        text, parser, full_text = _extract_text(body, media_type)
    except (ValueError, ET.ParseError, subprocess.SubprocessError) as error:
        return {
            **base,
            "attempts": attempts,
            "completed_at_utc": _now(),
            "access_state": "retrieved_extraction_error",
            "reason_code": "source_extraction_error",
            "source_content_hash": digest,
            "source_path": str(original),
            "media_type": media_type,
            "bytes": len(body),
            "error": str(error),
            "scientific_eligibility": "pending",
        }
    extraction_hash = sha256_bytes(text.encode())
    extraction = output_dir / "extracted" / digest[:2] / digest / "text.txt"
    atomic_write(extraction, text.encode(), immutable=True)
    identity_resolved = _identity_resolves(candidate, text)
    if not identity_resolved:
        access_state = "retrieved_identity_unresolved"
        reason = "retrieved_source_identity_not_verified"
    elif not full_text:
        access_state = "retrieved_landing_page"
        reason = "full_text_unavailable_from_open_access_url"
    else:
        access_state = "retrieved_full_text"
        reason = "source_evidence_review_required"
    return {
        **base,
        "attempts": attempts,
        "completed_at_utc": _now(),
        "access_state": access_state,
        "reason_code": reason,
        "source_content_hash": digest,
        "source_version": f"retrieved-object-sha256:{digest}",
        "source_path": str(original),
        "media_type": media_type,
        "bytes": len(body),
        "final_url": result.get("final_url") or url,
        "extraction_path": str(extraction),
        "extraction_sha256": extraction_hash,
        "parser": parser,
        "identity_verified": identity_resolved,
        "full_text": full_text,
        "scientific_eligibility": "pending",
    }


def run_source_pass(
    *,
    action: str,
    queue_file: Path,
    candidates_file: Path,
    protocol_file: Path,
    prior_screening_file: Path,
    policy_file: Path,
    output_dir: Path,
    run_id: str,
    code_commit: str,
    viewer_progress_file: Path | None = None,
    decisions_file: Path | None = None,
    fetcher: Callable[..., dict[str, Any]] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if action not in {"prepare", "smoke", "continue", "decide"}:
        raise ValueError(
            "source-pass action must be prepare, smoke, continue, or decide"
        )
    manifest = prepare_source_pass(
        queue_file=queue_file,
        candidates_file=candidates_file,
        protocol_file=protocol_file,
        prior_screening_file=prior_screening_file,
        policy_file=policy_file,
        output_dir=output_dir,
        run_id=run_id,
        code_commit=code_commit,
        viewer_progress_file=viewer_progress_file,
    )
    _verify_inputs(manifest)
    receipts = _item_receipts(output_dir)
    if action == "prepare":
        return _read_json(output_dir / "progress.json")
    if action == "decide":
        if decisions_file is None:
            raise ValueError("source-pass decide requires a decisions file")
        return apply_source_decisions(
            output_dir=output_dir,
            decisions_file=decisions_file,
            viewer_progress_file=viewer_progress_file,
        )
    target = manifest["smoke_size"] if action == "smoke" else manifest["selection_size"]
    if action == "smoke" and len(receipts) > target:
        raise ValueError("the smoke action cannot follow expanded source processing")
    previous = _read_json(output_dir / "progress.json")
    started_at = previous.get("started_at_utc") or _now()
    running = _progress_payload(
        manifest,
        "running",
        receipts,
        f"Source retrieval is running: {len(receipts)} of {target} target records processed.",
        started_at=started_at,
    )
    atomic_json(output_dir / "progress.json", running)
    _write_viewer_progress(viewer_progress_file, running)
    fetch = fetcher or _network_fetch
    network_started = time.monotonic()
    aggregate_bytes = sum(int(row.get("bytes") or 0) for row in receipts.values())
    made_network_request = False
    for candidate in manifest["selection"]:
        position = int(candidate["position"])
        if position in receipts or position > target:
            continue
        if (
            time.monotonic() - network_started
            > manifest["limits"]["maximum_network_seconds"]
        ):
            paused = _progress_payload(
                manifest,
                "paused",
                receipts,
                "The source pass reached its network-time limit.",
                started_at=started_at,
            )
            atomic_json(output_dir / "progress.json", paused)
            _write_viewer_progress(viewer_progress_file, paused)
            return paused
        if made_network_request and (candidate.get("open_access") or {}).get("url"):
            sleep_fn(float(manifest["limits"]["minimum_seconds_between_requests"]))
        try:
            item = _process_item(
                candidate=candidate,
                manifest=manifest,
                output_dir=output_dir,
                fetcher=fetch,
                sleep_fn=sleep_fn,
            )
        except Exception as error:
            failed = _progress_payload(
                manifest,
                "error",
                receipts,
                f"Source retrieval stopped with {type(error).__name__}: {str(error)[:300]}",
                started_at=started_at,
            )
            atomic_json(output_dir / "progress.json", failed)
            _write_viewer_progress(viewer_progress_file, failed)
            raise
        made_network_request = made_network_request or bool(item.get("attempts"))
        aggregate_bytes += int(item.get("bytes") or 0)
        if aggregate_bytes > manifest["limits"]["maximum_aggregate_new_bytes"]:
            failed = _progress_payload(
                manifest,
                "error",
                receipts,
                "Source retrieval stopped because the aggregate source size limit was exceeded.",
                started_at=started_at,
            )
            atomic_json(output_dir / "progress.json", failed)
            _write_viewer_progress(viewer_progress_file, failed)
            raise ValueError("source pass exceeded its aggregate source size")
        atomic_json(
            output_dir / "items" / f"item-{position:06d}.json",
            item,
            immutable=True,
        )
        receipts[position] = item
        running = _progress_payload(
            manifest,
            "running",
            receipts,
            f"Source retrieval is running: {len(receipts)} of {target} target records processed.",
            started_at=started_at,
        )
        atomic_json(output_dir / "progress.json", running)
        _write_viewer_progress(viewer_progress_file, running)
    message = (
        "The 10-candidate smoke is complete. Source decisions and viewer state require review before expansion."
        if action == "smoke"
        else "Acquisition is complete for the selected batch. Source decisions require finalization."
    )
    paused = _progress_payload(
        manifest, "paused", receipts, message, started_at=started_at
    )
    atomic_json(output_dir / "progress.json", paused)
    _write_viewer_progress(viewer_progress_file, paused)
    return paused


def _validate_decision(
    decision: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, Any]:
    verdict = decision.get("scientific_eligibility")
    if verdict not in {"eligible", "excluded", "pending"}:
        raise ValueError("unsupported scientific eligibility decision")
    if not str(decision.get("decision_author") or "").strip():
        raise ValueError("an explicit source decision requires a decision author")
    method = str(decision.get("decision_method_version") or "")
    if method not in {
        "codex-native-semantic-source-review-v1",
        "human-semantic-source-review-v1",
    }:
        raise ValueError(
            "an explicit source decision requires a declared semantic review method"
        )
    if receipt.get("access_state") != "retrieved_full_text":
        raise ValueError("an explicit source decision requires verified full text")
    if decision.get("source_content_hash") != receipt.get("source_content_hash"):
        raise ValueError("decision source hash does not match the retrieved object")
    if decision.get("source_version") != receipt.get("source_version"):
        raise ValueError("decision source version does not match the retrieved object")
    extraction_path = Path(receipt["extraction_path"])
    if sha256_file(extraction_path) != receipt.get("extraction_sha256"):
        raise ValueError("decision extraction hash does not match the stored text")
    text = extraction_path.read_text(encoding="utf-8")
    passages = decision.get("evidence_passages")
    if not isinstance(passages, list) or not passages:
        raise ValueError("an explicit source decision requires evidence passages")
    for passage in passages:
        locator = passage.get("locator") or {}
        if locator.get("extraction_sha256") != receipt.get("extraction_sha256"):
            raise ValueError("decision locator uses the wrong extraction hash")
        try:
            start = int(locator["start_offset"])
            end = int(locator["end_offset"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("decision locator offsets are invalid") from error
        quote = passage.get("quote")
        if not isinstance(quote, str) or text[start:end] != quote:
            raise ValueError("decision quote does not equal the stored source text")
    criteria = decision.get("criteria") or {}
    required = {
        "published_primary_findings",
        "stable_identity_version",
        "geography",
        "lawful_access",
        "correction_retraction",
    }
    if set(criteria) != required:
        raise ValueError("decision criteria are incomplete")
    if verdict == "eligible":
        expected = {
            "published_primary_findings": "met",
            "stable_identity_version": "met",
            "geography": "core",
            "lawful_access": "met",
        }
        if any(
            (criteria.get(name) or {}).get("verdict") != value
            for name, value in expected.items()
        ):
            raise ValueError("eligible decision does not meet all source criteria")
        correction = (criteria.get("correction_retraction") or {}).get("verdict")
        if correction not in {"no_accessible_adverse_evidence", "unknown_not_checked"}:
            raise ValueError("eligible decision has an unsupported correction state")
        if correction == "unknown_not_checked" and not decision.get("limitations"):
            raise ValueError("unknown correction coverage requires a limitation")
    if verdict == "excluded":
        exclusion = any(
            value
            in {
                "not_met",
                "duplicate",
                "mixed",
                "subarctic",
                "nonarctic",
                "adverse_evidence",
            }
            for value in (
                (criteria.get(name) or {}).get("verdict") for name in required
            )
        )
        if not exclusion:
            raise ValueError("excluded decision lacks a source-supported exclusion")
    return decision


def _default_overlay_record(
    selected: dict[str, Any], receipt: dict[str, Any] | None
) -> dict[str, Any]:
    if receipt is None or str(receipt.get("access_state", "")).startswith(
        "unattempted_"
    ):
        eligibility = "unreviewed"
        decision = "unreviewed"
        reason = "no_open_access_source_url" if receipt else "not_attempted"
        access_state = receipt.get("access_state") if receipt else "unattempted"
    else:
        eligibility = "pending"
        decision = "pending"
        reason = str(receipt.get("reason_code") or "source_review_pending")
        access_state = str(receipt.get("access_state") or "unknown")
    return {
        "candidate_key": selected["candidate_key"],
        "title": selected.get("title"),
        "doi": selected.get("doi"),
        "position": selected["position"],
        "access_state": access_state,
        "decision": decision,
        "scientific_eligibility": eligibility,
        "geography_verdict": "unresolved",
        "reason_code": reason,
        "source_content_hash": receipt.get("source_content_hash") if receipt else None,
        "source_version": receipt.get("source_version") if receipt else None,
        "evidence_passages": [],
        "decision_author": None,
        "decision_method_version": None,
        "correction_retraction": "unknown_not_checked",
        "limitations": [
            "Overall scientific eligibility was not resolved from the available source evidence."
        ],
    }


def apply_source_decisions(
    *,
    output_dir: Path,
    decisions_file: Path,
    viewer_progress_file: Path | None = None,
) -> dict[str, Any]:
    manifest = _read_json(output_dir / "run-manifest.json")
    _verify_inputs(manifest)
    receipts = _item_receipts(output_dir)
    if len(receipts) != manifest["selection_size"]:
        raise ValueError("source decisions require all selected acquisition records")
    payload = _read_json(decisions_file)
    if payload.get("schema") != "source-decision-proposals-v1":
        raise ValueError("unsupported source-decision proposal schema")
    decisions: dict[str, dict[str, Any]] = {}
    for decision in payload.get("decisions") or []:
        key = str(decision.get("candidate_key") or "")
        if not key or key in decisions:
            raise ValueError("source decisions contain a missing or repeated key")
        decisions[key] = decision
    selected_keys = {row["candidate_key"] for row in manifest["selection"]}
    unknown = set(decisions) - selected_keys
    if unknown:
        raise ValueError(
            f"source decision key is outside the selected batch: {min(unknown)}"
        )
    decision_file_record = _file_record(decisions_file)
    current_pointer = output_dir / "run-receipt-current.json"
    overlay_pointer = output_dir / "overlay-current.json"
    if current_pointer.is_file() and overlay_pointer.is_file():
        current = _read_json(current_pointer)
        current_receipt_path = output_dir / str(current.get("file") or "")
        overlay_current = _read_json(overlay_pointer)
        current_overlay_path = output_dir / str(overlay_current.get("file") or "")
        if current_receipt_path.is_file() and current_overlay_path.is_file():
            current_overlay = _read_json(current_overlay_path)
            if current_overlay.get("decision_proposals") == decision_file_record:
                return {**_read_json(current_receipt_path), "idempotent_replay": True}
    records = []
    for selected in manifest["selection"]:
        receipt = receipts.get(int(selected["position"]))
        record = _default_overlay_record(selected, receipt)
        decision = decisions.get(selected["candidate_key"])
        if decision is not None:
            assert receipt is not None
            checked = _validate_decision(decision, receipt)
            record.update(
                {
                    "decision": "include"
                    if checked["scientific_eligibility"] == "eligible"
                    else "exclude"
                    if checked["scientific_eligibility"] == "excluded"
                    else "pending",
                    "scientific_eligibility": checked["scientific_eligibility"],
                    "geography_verdict": checked["criteria"]["geography"]["verdict"],
                    "reason_code": checked["reason_code"],
                    "evidence_passages": checked["evidence_passages"],
                    "criteria": checked["criteria"],
                    "decision_author": checked.get("decision_author"),
                    "decision_method_version": checked.get("decision_method_version"),
                    "correction_retraction": checked["criteria"][
                        "correction_retraction"
                    ]["verdict"],
                    "limitations": checked.get("limitations") or [],
                }
            )
        records.append(record)
    existing = sorted(output_dir.glob("source-screening-overlay-r*.json"))
    revision = len(existing) + 1
    previous = existing[-1] if existing else None
    overlay = {
        "schema": OVERLAY_SCHEMA,
        "run_id": manifest["run_id"],
        "policy_id": manifest["policy_id"],
        "revision": revision,
        "created_at_utc": _now(),
        "producer_code_commit": manifest["producer_code_commit"],
        "selection_keys_sha256": manifest["selection_keys_sha256"],
        "supersedes": {
            "path": str(previous) if previous else manifest["supersedes"]["path"],
            "sha256": sha256_file(previous)
            if previous
            else manifest["supersedes"]["sha256"],
            "scope": "prior_source_screening_overlay",
        },
        "decision_proposals": decision_file_record,
        "records": records,
        "counts": _counts(manifest, receipts, records),
    }
    overlay_path = output_dir / f"source-screening-overlay-r{revision}.json"
    atomic_json(overlay_path, overlay, immutable=True)
    overlay_hash = sha256_file(overlay_path)
    atomic_json(
        output_dir / "overlay-current.json",
        {"revision": revision, "file": overlay_path.name, "sha256": overlay_hash},
    )
    input_after = {
        name: _file_record(Path(record["path"]))
        for name, record in manifest["inputs"].items()
    }
    recorded_at = _now()
    review_complete = overlay["counts"]["full_text_review_pending"] == 0
    state = "completed" if review_complete else "paused"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "state": state,
        "run_id": manifest["run_id"],
        "policy_id": manifest["policy_id"],
        "producer_code_commit": manifest["producer_code_commit"],
        "recorded_at_utc": recorded_at,
        "completed_at_utc": recorded_at if review_complete else None,
        "selection_size": manifest["selection_size"],
        "selection_keys_sha256": manifest["selection_keys_sha256"],
        "overlay_revision": revision,
        "overlay_file": overlay_path.name,
        "overlay_sha256": overlay_hash,
        "counts": overlay["counts"],
        "inputs_before": manifest["inputs"],
        "inputs_after": input_after,
        "inputs_unchanged": input_after == manifest["inputs"],
        "decision_method_counts": {
            method: sum(row.get("decision_method_version") == method for row in records)
            for method in sorted(
                {
                    str(row["decision_method_version"])
                    for row in records
                    if row.get("decision_method_version")
                }
            )
        },
        "deterministic_checks_are_not_semantic_screening": True,
        "source_eligibility_is_separate_from_geography": True,
    }
    receipt_path = output_dir / f"run-receipt-r{revision}.json"
    atomic_json(receipt_path, receipt, immutable=True)
    atomic_json(
        output_dir / "run-receipt-current.json",
        {
            "revision": revision,
            "file": receipt_path.name,
            "sha256": sha256_file(receipt_path),
        },
    )
    old_progress = _read_json(output_dir / "progress.json")
    progress = _progress_payload(
        manifest,
        state,
        receipts,
        "The bounded source pass is complete under its durable receipt."
        if review_complete
        else "Source retrieval is complete, but retrieved full-text review is incomplete.",
        started_at=old_progress.get("started_at_utc"),
        completed_at=recorded_at if review_complete else None,
        records=records,
    )
    progress["overlay_revision"] = revision
    atomic_json(output_dir / "progress.json", progress)
    _write_viewer_progress(viewer_progress_file, progress)
    return receipt
