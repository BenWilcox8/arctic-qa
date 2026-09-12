from __future__ import annotations

import html.parser
import http.client
import ipaddress
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .source_pass import _extract_text, _identity_resolves
from .util import atomic_json, atomic_write, canonical_json, sha256_bytes, sha256_file


MANIFEST_SCHEMA = "article-access-manifest-v1"
PROGRESS_SCHEMA = "article-access-progress-v1"
ITEM_SCHEMA = "article-access-item-v1"
STATES = {
    "not_checked",
    "checking",
    "working_landing_page_only",
    "download_available",
    "full_text_ready",
    "access_pending",
    "retryable_error",
    "extraction_pending",
    "OCR_required",
    "identity_pending",
    "no_source_found",
}
USER_AGENT = "arctic-qa-access-readiness-r1/1.0"
MIN_EXTRACTED_TEXT_CHARS = 2000


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


def _validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema") != "article-access-policy-v1":
        raise ValueError("unsupported article-access policy schema")
    expected = policy.get("target_dispositions") or {}
    if expected != {"priority_seed": 2, "retained_article_type": 16339}:
        raise ValueError("article-access policy must preserve the frozen target counts")
    if int(policy.get("workers", 0)) not in {2, 3, 4}:
        raise ValueError("article-access workers must be between two and four")
    if int(policy.get("maximum_bytes_per_source", 0)) > 50 * 1024 * 1024:
        raise ValueError("article-access source limit exceeds 50 MiB")
    if int(policy.get("minimum_free_bytes", 0)) < 50 * 1024 * 1024 * 1024:
        raise ValueError("article-access free-space guard is less than 50 GiB")
    if int(policy.get("maximum_new_bytes_total", 0)) > 100 * 1024**3:
        raise ValueError("article-access total size limit exceeds 100 GiB")


def _load_target(
    queue_file: Path, candidates_file: Path, policy: dict[str, Any]
) -> list[dict[str, Any]]:
    wanted: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    with queue_file.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            disposition = str(row.get("metadata_disposition") or "")
            if disposition not in policy["target_dispositions"]:
                continue
            key = str(row.get("candidate_key") or "")
            if not key or key in wanted:
                raise ValueError(
                    f"target queue key is missing or repeated at line {line_number}"
                )
            wanted[key] = row
            counts[disposition] += 1
    if dict(counts) != policy["target_dispositions"]:
        raise ValueError(f"frozen target count mismatch: {dict(counts)}")
    candidates = _read_json(candidates_file)
    by_key = {str(row.get("candidate_key")): row for row in candidates}
    selected: list[dict[str, Any]] = []
    order = {"priority_seed": 0, "retained_article_type": 1}
    for key, queued in sorted(
        wanted.items(),
        key=lambda pair: (
            order[pair[1]["metadata_disposition"]],
            int(pair[1].get("sequence") or 0),
            pair[0],
        ),
    ):
        source = by_key.get(key)
        if source is None:
            raise ValueError(f"target key is absent from discovery: {key}")
        selected.append(
            {
                "position": len(selected) + 1,
                "candidate_key": key,
                "subgroup": queued["metadata_disposition"],
                "discovery_sequence": queued.get("sequence"),
                "doi": source.get("doi"),
                "stable_id": source.get("stable_id"),
                "title": source.get("title"),
                "authors": source.get("authors") or [],
                "year": source.get("year"),
                "open_access": source.get("open_access") or {},
                "landing_url": source.get("landing_url"),
            }
        )
    return selected


def prepare_access_run(
    *,
    queue_file: Path,
    candidates_file: Path,
    protocol_file: Path,
    policy_file: Path,
    output_dir: Path,
    run_id: str,
    code_commit: str,
    reuse_source_run_dir: Path | None = None,
    reuse_access_run_dir: Path | None = None,
) -> dict[str, Any]:
    policy = _read_json(policy_file)
    protocol = _read_json(protocol_file)
    _validate_policy(policy)
    if protocol.get("protocol_id") != policy.get("source_protocol_id"):
        raise ValueError("article-access policy and protocol do not match")
    selection = _load_target(queue_file, candidates_file, policy)
    inputs = {
        "review_queue": _file_record(queue_file),
        "candidates": _file_record(candidates_file),
        "protocol": _file_record(protocol_file),
        "policy": _file_record(policy_file),
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "run_id": run_id,
        "created_at_utc": _now(),
        "producer_code_commit": code_commit,
        "policy_id": policy["policy_id"],
        "protocol_id": protocol["protocol_id"],
        "inputs": inputs,
        "target_counts": policy["target_dispositions"],
        "target_total": len(selection),
        "selection_keys_sha256": sha256_bytes(
            canonical_json([row["candidate_key"] for row in selection]).encode()
        ),
        "limits": {
            key: value
            for key, value in policy.items()
            if key.startswith(("maximum_", "minimum_", "workers", "request_"))
        },
        "smoke_sizes": policy["smoke_sizes"],
        "reuse_source_run_dir": str(reuse_source_run_dir.resolve())
        if reuse_source_run_dir
        else None,
        "reuse_access_run_dir": str(reuse_access_run_dir.resolve())
        if reuse_access_run_dir
        else None,
        "selection": selection,
    }
    path = output_dir / "run-manifest.json"
    if path.is_file():
        old = _read_json(path)
        candidate = dict(manifest)
        candidate["created_at_utc"] = old.get("created_at_utc")
        if old != candidate:
            raise ValueError(
                "cannot resume because the article-access manifest changed"
            )
        manifest = old
    else:
        atomic_json(path, manifest, immutable=True)
    if not (output_dir / "progress.json").is_file():
        _write_progress(
            output_dir,
            manifest,
            "prepared",
            "The access run is prepared and is not running.",
        )
    return manifest


def _verify_inputs(manifest: dict[str, Any]) -> None:
    for name, expected in manifest["inputs"].items():
        path = Path(expected["path"])
        if not path.is_file() or _file_record(path) != expected:
            raise ValueError(
                f"cannot resume because the article-access input changed: {name}"
            )


def _receipts(
    output_dir: Path, manifest: dict[str, Any] | None = None
) -> dict[int, dict[str, Any]]:
    result = {}
    for path in sorted((output_dir / "items").glob("item-*.json")):
        row = _read_json(path)
        position = int(row.get("position") or 0)
        if position in result:
            raise ValueError("an article-access receipt position is repeated")
        if path.name != f"item-{position:06d}.json":
            raise ValueError("an article-access receipt file name is inconsistent")
        if manifest is not None:
            if not 1 <= position <= int(manifest["target_total"]):
                raise ValueError("an article-access receipt position is out of range")
            selected = manifest["selection"][position - 1]
            if (
                row.get("schema") != ITEM_SCHEMA
                or row.get("run_id") != manifest["run_id"]
                or row.get("candidate_key") != selected["candidate_key"]
                or row.get("subgroup") != selected["subgroup"]
                or row.get("access_state") not in STATES
            ):
                raise ValueError(
                    "an article-access receipt does not match its manifest"
                )
            if row.get("access_state") == "full_text_ready":
                source = Path(str(row.get("source_path") or ""))
                extraction = Path(str(row.get("extraction_path") or ""))
                coverage = row.get("extraction_coverage") or {}
                if (
                    not source.is_file()
                    or not extraction.is_file()
                    or sha256_file(source) != row.get("source_content_hash")
                    or sha256_file(extraction) != row.get("extraction_sha256")
                    or row.get("identity_verified") is not True
                    or extraction.stat().st_size < MIN_EXTRACTED_TEXT_CHARS
                    or coverage.get("article_body_recognized") is not True
                    or not _identity_resolves(
                        selected,
                        extraction.read_text(encoding="utf-8", errors="replace"),
                    )
                ):
                    raise ValueError("a ready article-access receipt is not verifiable")
        result[position] = row
    return result


def _counts(
    manifest: dict[str, Any], receipts: dict[int, dict[str, Any]]
) -> dict[str, int]:
    states = Counter(str(row.get("access_state")) for row in receipts.values())
    counts = {state: int(states[state]) for state in sorted(STATES)}
    counts["not_checked"] = int(manifest["target_total"]) - len(receipts)
    counts.update(
        {
            "target": int(manifest["target_total"]),
            "checked": len(receipts),
            "working_links": sum(
                bool(row.get("link_working")) for row in receipts.values()
            ),
            "ready_for_eligibility": int(states["full_text_ready"]),
            "unchecked": int(manifest["target_total"]) - len(receipts),
            "priority_seed": int(manifest["target_counts"]["priority_seed"]),
            "retained_article_type": int(
                manifest["target_counts"]["retained_article_type"]
            ),
        }
    )
    return counts


def _write_overlay(output_dir: Path, receipts: dict[int, dict[str, Any]]) -> None:
    data = b"".join(
        (canonical_json(row) + "\n").encode() for _, row in sorted(receipts.items())
    )
    atomic_write(output_dir / "access-overlay.ndjson", data)


def _write_progress(
    output_dir: Path,
    manifest: dict[str, Any],
    state: str,
    message: str,
    *,
    started_at: str | None = None,
    completed_at: str | None = None,
    receipts: dict[int, dict[str, Any]] | None = None,
    active: list[dict[str, Any]] | None = None,
    write_overlay: bool = True,
) -> dict[str, Any]:
    receipts = receipts if receipts is not None else _receipts(output_dir)
    counts = _counts(manifest, receipts)
    counts["checking"] = len(active or [])
    counts["not_checked"] = max(counts["not_checked"] - len(active or []), 0)
    counts["unchecked"] = counts["not_checked"]
    progress = {
        "schema": PROGRESS_SCHEMA,
        "state": state,
        "stage": "article_access_readiness",
        "run_id": manifest["run_id"],
        "policy_id": manifest["policy_id"],
        "producer_code_commit": manifest["producer_code_commit"],
        "updated_at_utc": _now(),
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "message": message,
        "counts": counts,
        "active": active or [],
        "latest_event_at_utc": _now(),
        "selection_keys_sha256": manifest["selection_keys_sha256"],
    }
    atomic_json(output_dir / "progress.json", progress)
    if write_overlay:
        _write_overlay(output_dir, receipts)
    return progress


def _public_addresses(
    url: str, resolver: Callable[..., Any] = socket.getaddrinfo
) -> list[str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must use public HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source URL cannot contain credentials")
    if parsed.hostname.casefold() == "localhost":
        raise ValueError("source URL cannot use localhost")
    try:
        addresses = {
            item[4][0]
            for item in resolver(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as error:
        raise ValueError(f"source hostname did not resolve: {error}") from error
    if not addresses:
        raise ValueError("source hostname did not resolve")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ValueError("source hostname resolved to a non-public address")
    return sorted(addresses)


def _public_url(url: str, resolver: Callable[..., Any] = socket.getaddrinfo) -> None:
    _public_addresses(url, resolver)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        parsed = urllib.parse.urlsplit(f"http://{self.host}")
        last_error: OSError | None = None
        for address in _public_addresses(f"http://{self.host}"):
            try:
                self.sock = socket.create_connection(
                    (address, parsed.port or 80), self.timeout, self.source_address
                )
                return
            except OSError as error:
                last_error = error
        raise last_error or OSError("no public source address connected")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        parsed = urllib.parse.urlsplit(f"https://{self.host}")
        hostname = parsed.hostname or ""
        last_error: OSError | None = None
        for address in _public_addresses(f"https://{self.host}"):
            try:
                raw = socket.create_connection(
                    (address, parsed.port or 443), self.timeout, self.source_address
                )
                self.sock = self._context.wrap_socket(raw, server_hostname=hostname)
                return
            except OSError as error:
                last_error = error
        raise last_error or OSError("no public source address connected")


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_PinnedHTTPConnection, request)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PinnedHTTPSConnection, request)


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, maximum: int) -> None:
        super().__init__()
        self.maximum = maximum

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        count = int(request.headers.get("X-Arctic-Redirect-Count", "0")) + 1
        if count > self.maximum:
            raise urllib.error.HTTPError(
                new_url, code, "redirect limit exceeded", headers, file_pointer
            )
        _public_url(new_url)
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        if redirected is not None:
            redirected.add_unredirected_header("X-Arctic-Redirect-Count", str(count))
        return redirected


class HostPacer:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname or ""
        with self._lock:
            remaining = self._cooldown_until.get(host, 0.0) - time.monotonic()
            if remaining > 0:
                raise RuntimeError(
                    f"source host is in cooldown for {remaining:.1f} seconds"
                )
            delay = self.delay - (time.monotonic() - self._last.get(host, 0.0))
            if delay > 0:
                time.sleep(delay)
            self._last[host] = time.monotonic()

    def cool_down(self, url: str, seconds: float) -> None:
        host = urllib.parse.urlsplit(url).hostname or ""
        with self._lock:
            self._cooldown_until[host] = max(
                self._cooldown_until.get(host, 0.0), time.monotonic() + seconds
            )


def fetch_public(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    maximum_redirects: int,
    maximum_retry_after: int,
    pacer: HostPacer,
) -> dict[str, Any]:
    _public_url(url)
    last: dict[str, Any] = {}
    for attempt in range(1, 4):
        try:
            pacer.wait(url)
        except RuntimeError as error:
            return {
                "state": "retryable_error",
                "reason_code": "host_cooldown",
                "error": str(error),
                "attempt": attempt,
            }
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,application/xml,text/xml,text/html,text/plain;q=0.8,*/*;q=0.1",
            },
        )
        try:
            with urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _PinnedHTTPHandler(),
                _PinnedHTTPSHandler(context=ssl.create_default_context()),
                PublicRedirectHandler(maximum_redirects),
            ).open(request, timeout=timeout) as response:
                final_url = response.geturl()
                _public_url(final_url)
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    return {
                        "state": "access_pending",
                        "reason_code": "content_length_over_limit",
                        "attempt": attempt,
                        "status": response.status,
                    }
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    return {
                        "state": "access_pending",
                        "reason_code": "stream_over_limit",
                        "attempt": attempt,
                        "status": response.status,
                    }
                return {
                    "state": "downloaded",
                    "attempt": attempt,
                    "status": response.status,
                    "final_url": final_url,
                    "media_type": response.headers.get_content_type().casefold(),
                    "body": body,
                    "checked_at_utc": _now(),
                }
        except urllib.error.HTTPError as error:
            retryable = error.code in {429, 500, 502, 503, 504}
            last = {
                "state": "retryable_error" if retryable else "access_pending",
                "reason_code": "http_error",
                "status": error.code,
                "attempt": attempt,
            }
            if not retryable or attempt == 3:
                return last
            retry_after = error.headers.get("Retry-After") if error.headers else None
            delay = (
                min(int(retry_after), maximum_retry_after)
                if retry_after and retry_after.isdigit()
                else min(2**attempt, maximum_retry_after)
            )
            pacer.cool_down(url, delay)
            return last
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as error:
            last = {
                "state": "retryable_error",
                "reason_code": "transport_or_url_error",
                "error": f"{type(error).__name__}: {error}",
                "attempt": attempt,
            }
            if attempt == 3:
                return last
            pacer.cool_down(url, min(2**attempt, maximum_retry_after))
            return last
    return last


class _LinkCollector(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if (
            tag == "meta"
            and str(values.get("name", "")).casefold() == "citation_pdf_url"
        ):
            if values.get("content"):
                self.links.append(str(values["content"]))
        if (
            tag == "a"
            and values.get("href")
            and ".pdf" in str(values["href"]).casefold()
        ):
            self.links.append(str(values["href"]))


def _landing_links(body: bytes, base_url: str) -> list[str]:
    parser = _LinkCollector()
    parser.feed(body.decode("utf-8", errors="replace"))
    return list(
        dict.fromkeys(urllib.parse.urljoin(base_url, link) for link in parser.links)
    )[:10]


def _xml_has_article_body(body: bytes) -> bool:
    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return False
    body_nodes = [
        node for node in root.iter() if node.tag.rsplit("}", 1)[-1].casefold() == "body"
    ]
    return any(
        len(" ".join(node.itertext()).strip()) >= MIN_EXTRACTED_TEXT_CHARS
        for node in body_nodes
    )


def _identity_resolves_source(
    candidate: dict[str, Any], extracted_text: str, source_body: bytes, media_type: str
) -> bool:
    """Check identity without treating an XML reference list as article metadata."""
    if media_type not in {"application/xml", "text/xml"}:
        return _identity_resolves(candidate, extracted_text)
    try:
        root = ET.fromstring(source_body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return False
    parents = {child: parent for parent in root.iter() for child in parent}
    for node in list(root.iter()):
        if node.tag.rsplit("}", 1)[-1].casefold() in {
            "ref-list",
            "reference-list",
            "bibliography",
        }:
            parent = parents.get(node)
            if parent is not None:
                parent.remove(node)
    identity_text = " ".join(root.itertext())
    return _identity_resolves(candidate, identity_text)


def _reuse(candidate: dict[str, Any], source_run: Path | None) -> dict[str, Any] | None:
    if source_run is None or not source_run.is_dir():
        return None
    for path in (source_run / "items").glob("item-*.json"):
        row = _read_json(path)
        if row.get("candidate_key") != candidate["candidate_key"]:
            continue
        source = Path(str(row.get("source_path") or ""))
        text = Path(str(row.get("extraction_path") or ""))
        if row.get("access_state") != "retrieved_full_text" or not row.get(
            "identity_verified"
        ):
            return None
        if not source.is_file() or not text.is_file():
            return None
        if sha256_file(source) != row.get("source_content_hash") or sha256_file(
            text
        ) != row.get("extraction_sha256"):
            return None
        extracted_text = text.read_text(encoding="utf-8", errors="replace")
        media_type = str(row.get("media_type") or "").casefold()
        if (
            len(extracted_text) < MIN_EXTRACTED_TEXT_CHARS
            or not _identity_resolves_source(
                candidate, extracted_text, source.read_bytes(), media_type
            )
            or (
                media_type in {"application/xml", "text/xml"}
                and not _xml_has_article_body(source.read_bytes())
            )
        ):
            return None
        return {
            "access_state": "full_text_ready",
            "reason_code": "verified_historical_object_reused",
            "link_working": True,
            "checked_at_utc": row.get("completed_at_utc"),
            "final_url": row.get("final_url"),
            "media_type": media_type,
            "source_path": str(source),
            "source_content_hash": row["source_content_hash"],
            "extraction_path": str(text),
            "extraction_sha256": row["extraction_sha256"],
            "identity_verified": True,
            "extraction_coverage": {
                "characters": len(extracted_text),
                "article_body_recognized": True,
                "figures": "unknown_not_extracted",
                "tables": "unknown_not_extracted",
                "supplements": "unknown_not_extracted",
                "ocr": "not_applied",
            },
            "reused_from": str(path),
            "new_bytes": 0,
        }
    return None


def _reuse_access(
    candidate: dict[str, Any], access_run: Path | None
) -> dict[str, Any] | None:
    if access_run is None or not access_run.is_dir():
        return None
    path = access_run / "items" / f"item-{int(candidate['position']):06d}.json"
    if not path.is_file():
        return None
    row = _read_json(path)
    if (
        row.get("candidate_key") != candidate["candidate_key"]
        or row.get("access_state") not in STATES
    ):
        raise ValueError("a reused access receipt does not match the frozen selection")
    if row.get("access_state") == "full_text_ready":
        source = Path(str(row.get("source_path") or ""))
        extraction = Path(str(row.get("extraction_path") or ""))
        if not source.is_file() or not extraction.is_file():
            raise ValueError("a reused ready source object is missing")
        if sha256_file(source) != row.get("source_content_hash") or sha256_file(
            extraction
        ) != row.get("extraction_sha256"):
            raise ValueError("a reused ready source hash changed")
        coverage = row.get("extraction_coverage") or {}
        extracted_text = extraction.read_text(encoding="utf-8", errors="replace")
        if (
            row.get("identity_verified") is not True
            or extraction.stat().st_size < MIN_EXTRACTED_TEXT_CHARS
            or coverage.get("article_body_recognized") is not True
            or not _identity_resolves_source(
                candidate,
                extracted_text,
                source.read_bytes(),
                str(row.get("media_type") or "").casefold(),
            )
        ):
            return None
    return {
        **row,
        "schema": ITEM_SCHEMA,
        "completed_at_utc": _now(),
        "reused_from": str(path),
        "new_bytes": 0,
    }


def _process(
    candidate: dict[str, Any],
    manifest: dict[str, Any],
    output_dir: Path,
    fetcher: Callable[..., dict[str, Any]],
    pacer: HostPacer,
    invocation_deadline: float | None = None,
) -> dict[str, Any]:
    base = {
        "schema": ITEM_SCHEMA,
        "run_id": manifest["run_id"],
        "position": candidate["position"],
        "candidate_key": candidate["candidate_key"],
        "subgroup": candidate["subgroup"],
        "title": candidate.get("title"),
        "doi": candidate.get("doi"),
        "started_at_utc": _now(),
    }
    prior_access = _reuse_access(
        candidate,
        Path(manifest["reuse_access_run_dir"])
        if manifest.get("reuse_access_run_dir")
        else None,
    )
    if prior_access:
        return {**base, **prior_access, "run_id": manifest["run_id"]}
    reused = _reuse(
        candidate,
        Path(manifest["reuse_source_run_dir"])
        if manifest.get("reuse_source_run_dir")
        else None,
    )
    if reused:
        return {
            **base,
            **reused,
            "completed_at_utc": _now(),
            "provenance": [
                {"kind": "historical_verified_object", "path": reused["reused_from"]}
            ],
        }
    urls = []
    oa = (candidate.get("open_access") or {}).get("url")
    for kind, url in (
        ("metadata_open_access", oa),
        ("metadata_landing", candidate.get("landing_url")),
        (
            "doi_resolver",
            f"https://doi.org/{candidate['doi']}" if candidate.get("doi") else None,
        ),
    ):
        if url and str(url) not in [item[1] for item in urls]:
            urls.append((kind, str(url)))
    if not urls:
        return {
            **base,
            "completed_at_utc": _now(),
            "access_state": "no_source_found",
            "reason_code": "no_source_url_in_metadata",
            "link_working": False,
            "new_bytes": 0,
            "provenance": [],
        }
    attempts = []
    landing: dict[str, Any] | None = None
    checked_urls: set[str] = set()
    candidate_deadline = time.monotonic() + float(
        manifest["limits"].get("maximum_network_seconds_per_candidate", 120)
    )
    if invocation_deadline is not None:
        candidate_deadline = min(candidate_deadline, invocation_deadline)
    for kind, url in urls:
        if url in checked_urls:
            continue
        checked_urls.add(url)
        if time.monotonic() >= candidate_deadline:
            attempts.append(
                {
                    "url": url,
                    "provenance_kind": kind,
                    "state": "retryable_error",
                    "reason_code": "candidate_network_deadline",
                }
            )
            break
        try:
            result = fetcher(
                url,
                max_bytes=int(manifest["limits"]["maximum_bytes_per_source"]),
                timeout=min(
                    float(manifest["limits"]["request_timeout_seconds"]),
                    max(candidate_deadline - time.monotonic(), 1.0),
                ),
                maximum_redirects=int(manifest["limits"]["maximum_redirects"]),
                maximum_retry_after=int(
                    manifest["limits"]["maximum_retry_after_seconds"]
                ),
                pacer=pacer,
            )
        except ValueError as error:
            result = {
                "state": "access_pending",
                "reason_code": "blocked_source_url",
                "error": str(error),
            }
        attempts.append(
            {key: value for key, value in result.items() if key != "body"}
            | {"url": url, "provenance_kind": kind}
        )
        if result.get("state") != "downloaded":
            continue
        body = result.get("body")
        if not isinstance(body, bytes) or not body:
            continue
        media = str(result.get("media_type") or "application/octet-stream")
        if media in {"text/html", "application/xhtml+xml"}:
            landing = result
            for link in reversed(
                _landing_links(body, str(result.get("final_url") or url))
            ):
                if link not in checked_urls and link not in [item[1] for item in urls]:
                    urls.append(("landing_pdf_link", link))
            continue
        digest = sha256_bytes(body)
        suffix = {
            "application/pdf": ".pdf",
            "application/xml": ".xml",
            "text/xml": ".xml",
            "text/plain": ".txt",
        }.get(media, ".bin")
        original = output_dir / "originals" / digest[:2] / digest / f"source{suffix}"
        try:
            text, parser, full_text = _extract_text(body, media)
        except (ValueError, ET.ParseError, subprocess.SubprocessError) as error:
            return {
                **base,
                "completed_at_utc": _now(),
                "access_state": "extraction_pending",
                "reason_code": "deterministic_extraction_error",
                "link_working": True,
                "final_url": result.get("final_url"),
                "source_path": str(original),
                "source_content_hash": digest,
                "media_type": media,
                "new_bytes": len(body),
                "error": str(error),
                "attempts": attempts,
                "_source_body": body,
            }
        recognized_xml_body = media not in {
            "application/xml",
            "text/xml",
        } or _xml_has_article_body(body)
        extraction = output_dir / "extracted" / digest[:2] / digest / "text.txt"
        extraction_bytes = text.encode()
        extraction_hash = sha256_bytes(extraction_bytes)
        identity = _identity_resolves_source(candidate, text, body, media)
        if media == "application/pdf" and len(text.strip()) < 200:
            state, reason = "OCR_required", "pdf_text_is_too_short"
        elif not recognized_xml_body:
            state, reason = "extraction_pending", "xml_article_body_not_recognized"
        elif not full_text:
            state, reason = (
                "download_available",
                "download_is_not_recognized_as_full_text",
            )
        elif len(text.strip()) < MIN_EXTRACTED_TEXT_CHARS:
            state, reason = "extraction_pending", "extracted_text_is_too_short"
        elif not identity:
            state, reason = "identity_pending", "source_identity_not_verified"
        else:
            state, reason = (
                "full_text_ready",
                "full_text_extracted_and_identity_verified",
            )
        return {
            **base,
            "completed_at_utc": _now(),
            "access_state": state,
            "reason_code": reason,
            "link_working": True,
            "checked_at_utc": result.get("checked_at_utc"),
            "final_url": result.get("final_url"),
            "source_path": str(original),
            "source_content_hash": digest,
            "extraction_path": str(extraction),
            "extraction_sha256": extraction_hash,
            "parser": parser,
            "extraction_coverage": {
                "characters": len(text),
                "article_body_recognized": bool(full_text and recognized_xml_body),
                "figures": "unknown_not_extracted",
                "tables": "unknown_not_extracted",
                "supplements": "unknown_not_extracted",
                "ocr": "not_applied",
            },
            "identity_verified": identity,
            "media_type": media,
            "source_bytes": len(body),
            "extraction_bytes": len(extraction_bytes),
            "new_bytes": len(body) + len(extraction_bytes),
            "attempts": attempts,
            "_source_body": body,
            "_extraction_body": extraction_bytes,
        }
    if landing:
        return {
            **base,
            "completed_at_utc": _now(),
            "access_state": "working_landing_page_only",
            "reason_code": "working_page_has_no_retrieved_full_text",
            "link_working": True,
            "checked_at_utc": landing.get("checked_at_utc"),
            "final_url": landing.get("final_url"),
            "new_bytes": 0,
            "attempts": attempts,
        }
    state = (
        "retryable_error"
        if any(row.get("state") == "retryable_error" for row in attempts)
        else "access_pending"
    )
    return {
        **base,
        "completed_at_utc": _now(),
        "access_state": state,
        "reason_code": "all_known_sources_failed",
        "link_working": False,
        "new_bytes": 0,
        "attempts": attempts,
    }


def _persist_artifacts(
    item: dict[str, Any], *, committed_bytes: int, byte_cap: int
) -> dict[str, Any]:
    """Persist fetched content only after the coordinator accepts its byte cost."""
    item_bytes = int(item.get("new_bytes") or 0)
    if committed_bytes + item_bytes > byte_cap:
        raise ValueError("article-access new-byte limit reached before source write")
    stored = dict(item)
    source_body = stored.pop("_source_body", None)
    extraction_body = stored.pop("_extraction_body", None)
    expected_bytes = (len(source_body) if source_body is not None else 0) + (
        len(extraction_body) if extraction_body is not None else 0
    )
    if expected_bytes != item_bytes:
        raise ValueError("article-access durable artifact byte count changed")
    if source_body is not None:
        if not isinstance(source_body, bytes):
            raise ValueError("article-access source body is not bytes")
        source_bytes = int(stored.get("source_bytes", len(source_body)))
        if len(source_body) != source_bytes:
            raise ValueError("article-access source byte count changed")
        if sha256_bytes(source_body) != stored.get("source_content_hash"):
            raise ValueError("article-access source body hash changed")
        atomic_write(Path(stored["source_path"]), source_body, immutable=True)
    if extraction_body is not None:
        if not isinstance(extraction_body, bytes):
            raise ValueError("article-access extraction body is not bytes")
        if sha256_bytes(extraction_body) != stored.get("extraction_sha256"):
            raise ValueError("article-access extraction body hash changed")
        atomic_write(Path(stored["extraction_path"]), extraction_body, immutable=True)
    return stored


def run_access_readiness(
    *,
    action: str,
    queue_file: Path,
    candidates_file: Path,
    protocol_file: Path,
    policy_file: Path,
    output_dir: Path,
    run_id: str,
    code_commit: str,
    reuse_source_run_dir: Path | None = None,
    reuse_access_run_dir: Path | None = None,
    max_items: int | None = None,
    max_network_seconds: int | None = None,
    max_new_bytes: int | None = None,
    fetcher: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if action not in {"prepare", "smoke10", "smoke100", "continue", "status"}:
        raise ValueError("article-access action is not supported")
    manifest = prepare_access_run(
        queue_file=queue_file,
        candidates_file=candidates_file,
        protocol_file=protocol_file,
        policy_file=policy_file,
        output_dir=output_dir,
        run_id=run_id,
        code_commit=code_commit,
        reuse_source_run_dir=reuse_source_run_dir,
        reuse_access_run_dir=reuse_access_run_dir,
    )
    _verify_inputs(manifest)
    receipts = _receipts(output_dir, manifest)
    if action in {"prepare", "status"}:
        return _read_json(output_dir / "progress.json")
    target = {"smoke10": 10, "smoke100": 100, "continue": manifest["target_total"]}[
        action
    ]
    if max_items is not None:
        target = min(target, len(receipts) + max_items)
    started_at = (
        _read_json(output_dir / "progress.json").get("started_at_utc") or _now()
    )
    _write_progress(
        output_dir,
        manifest,
        "running",
        f"Access readiness is running for {target} target records.",
        started_at=started_at,
        receipts=receipts,
    )
    seconds = min(
        int(
            max_network_seconds
            or manifest["limits"]["maximum_network_seconds_per_invocation"]
        ),
        int(manifest["limits"]["maximum_network_seconds_per_invocation"]),
    )
    existing_new_bytes = sum(
        int(row.get("new_bytes") or 0) for row in receipts.values()
    )
    total_remaining = max(
        int(manifest["limits"]["maximum_new_bytes_total"]) - existing_new_bytes, 0
    )
    byte_cap = min(int(max_new_bytes or total_remaining), total_remaining)
    deadline = time.monotonic() + seconds
    new_bytes = 0
    pacer = HostPacer(
        float(manifest["limits"]["minimum_seconds_between_host_requests"])
    )
    fetch = fetcher or fetch_public
    pending = [
        row
        for row in manifest["selection"]
        if int(row["position"]) <= target and int(row["position"]) not in receipts
    ]
    workers = int(manifest["limits"]["workers"])
    stop_reason = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        iterator = iter(pending)
        while True:
            while (
                len(futures) < workers
                and time.monotonic() < deadline
                and stop_reason is None
            ):
                if shutil.disk_usage(output_dir).free < int(
                    manifest["limits"]["minimum_free_bytes"]
                ):
                    stop_reason = "The access run reached the free-space guard."
                    break
                try:
                    row = next(iterator)
                except StopIteration:
                    break
                futures[
                    pool.submit(
                        _process, row, manifest, output_dir, fetch, pacer, deadline
                    )
                ] = row
                active = [
                    {
                        "position": int(item["position"]),
                        "candidate_key": item["candidate_key"],
                        "event": "checking",
                    }
                    for item in futures.values()
                ]
                _write_progress(
                    output_dir,
                    manifest,
                    "running",
                    f"Access readiness is checking {len(active)} candidate records.",
                    started_at=started_at,
                    receipts=receipts,
                    active=active,
                    write_overlay=False,
                )
            if not futures:
                break
            future = next(as_completed(futures))
            row = futures.pop(future)
            item = future.result()
            item_bytes = int(item.get("new_bytes") or 0)
            try:
                item = _persist_artifacts(
                    item, committed_bytes=new_bytes, byte_cap=byte_cap
                )
            except ValueError as error:
                if "new-byte limit" not in str(error):
                    raise
                stop_reason = "The access run reached the new-byte limit."
                break
            new_bytes += item_bytes
            atomic_json(
                output_dir / "items" / f"item-{int(row['position']):06d}.json",
                item,
                immutable=True,
            )
            receipts[int(row["position"])] = item
            active = [
                {
                    "position": int(value["position"]),
                    "candidate_key": value["candidate_key"],
                    "event": "checking",
                }
                for value in futures.values()
            ]
            _write_progress(
                output_dir,
                manifest,
                "running",
                f"Access readiness recorded item {int(row['position'])}. {len(active)} candidate records remain active.",
                started_at=started_at,
                receipts=receipts,
                active=active,
                write_overlay=False,
            )
        for future in futures:
            future.cancel()
    complete_target = all(position in receipts for position in range(1, target + 1))
    complete_all = len(receipts) == int(manifest["target_total"])
    if complete_all:
        state, message, completed = (
            "completed",
            "Access readiness checked every record in the frozen target.",
            _now(),
        )
        receipt = {
            "schema": "article-access-run-receipt-v1",
            "run_id": manifest["run_id"],
            "completed_at_utc": completed,
            "selection_keys_sha256": manifest["selection_keys_sha256"],
            "counts": _counts(manifest, receipts),
        }
        atomic_json(output_dir / "run-receipt.json", receipt, immutable=True)
    elif complete_target and action.startswith("smoke"):
        state, message, completed = (
            "paused",
            f"The {target}-record smoke gate is complete. The full run is not complete.",
            None,
        )
    else:
        state, message, completed = (
            "paused",
            stop_reason or "The invocation stopped at its bounded checkpoint.",
            None,
        )
    return _write_progress(
        output_dir,
        manifest,
        state,
        message,
        started_at=started_at,
        completed_at=completed,
        receipts=receipts,
    )
