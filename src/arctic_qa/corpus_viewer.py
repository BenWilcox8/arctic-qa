from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .metadata_prefilter import DISPOSITIONS
from .util import sha256_file


PROGRESS_STATES = {"not_running", "running", "paused", "error", "completed"}
ELIGIBILITY_FILTERS = {"all", "unreviewed", "pending", "eligible", "excluded"}
PENDING_FILTERS = {
    "all",
    "unreviewed",
    "access-pending",
    "geography-pending",
    "other-pending",
}
PAGE_SIZES = {10, 25, 50, 100}
ACCESS_FILTERS = {
    "all",
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
GEMINI_FILTERS = {
    "all",
    "not_started",
    "queued",
    "eligible",
    "excluded",
    "uncertain",
    "screening_error",
    "too_large_not_ready",
}
METADATA_DISPOSITION_FILTERS = {"all", *DISPOSITIONS}
CACHE_SCHEMA_VERSION = "corpus-view-cache-v4"
SCHEMA = """
CREATE TABLE candidates (
    candidate_key TEXT PRIMARY KEY,
    doi TEXT,
    stable_id TEXT,
    title TEXT NOT NULL,
    title_search TEXT NOT NULL,
    authors_json TEXT NOT NULL,
    year INTEGER,
    venue TEXT,
    item_type TEXT,
    landing_url TEXT,
    repository_url TEXT,
    decision TEXT NOT NULL,
    eligibility TEXT NOT NULL,
    pending_reason TEXT,
    access_status TEXT,
    reason_code TEXT NOT NULL,
    evidence_locator TEXT,
    evidence_quote TEXT,
    selected INTEGER NOT NULL DEFAULT 0,
    zotero_url TEXT,
    metadata_disposition TEXT,
    metadata_reason_code TEXT,
    metadata_evidence_field TEXT,
    metadata_evidence_value TEXT,
    metadata_queue TEXT,
    metadata_flags_json TEXT,
    metadata_title_terms_json TEXT,
    metadata_policy_id TEXT,
    metadata_run_id TEXT,
    source_geography TEXT,
    source_pass_run_id TEXT,
    source_pass_position INTEGER,
    source_decision_method TEXT,
    source_limitations_json TEXT,
    access_readiness_state TEXT NOT NULL DEFAULT 'not_checked',
    access_readiness_reason TEXT,
    access_checked_at TEXT,
    access_final_url TEXT,
    gemini_status TEXT NOT NULL DEFAULT 'not_started',
    gemini_decision TEXT
);
CREATE INDEX candidates_title_search ON candidates(title_search);
CREATE INDEX candidates_doi ON candidates(doi);
CREATE INDEX candidates_eligibility ON candidates(eligibility, pending_reason);
CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _file_fingerprint(path: Path | None) -> str:
    if path is None or not path.is_file():
        return "missing"
    stat = path.stat()
    return f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"


def _directory_fingerprint(path: Path | None) -> str:
    if path is None or not path.is_dir():
        return "missing"
    rows = []
    for item in sorted(path.glob("*.json")):
        stat = item.stat()
        rows.append(f"{item.name}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _screening_revision(path: Path) -> int:
    match = re.search(r"-r(\d+)\.json$", path.name)
    return int(match.group(1)) if match else -1


def _safe_external_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value.startswith(("https://", "http://")) else None


def _pending_reason(reason_code: str) -> str:
    if "access" in reason_code or "http" in reason_code or "unavailable" in reason_code:
        return "access-pending"
    if any(word in reason_code for word in ("latitude", "marine_region", "geography")):
        return "geography-pending"
    return "other-pending"


def _safe_json_bytes(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return text.encode("utf-8")


class CorpusArtifacts:
    """Build a disposable query index from immutable corpus records."""

    def __init__(
        self,
        corpus_root: Path,
        run_id: str,
        runtime_dir: Path,
        *,
        progress_file: Path | None = None,
        zotero_receipts_dir: Path | None = None,
        metadata_run_dir: Path | None = None,
        source_run_dir: Path | None = None,
        access_run_dir: Path | None = None,
        gemini_run_dir: Path | None = None,
        stale_after_seconds: int = 86400,
        process_stale_after_seconds: int = 300,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", run_id):
            raise ValueError("run-id contains unsupported characters")
        if stale_after_seconds < 1:
            raise ValueError("stale-after-seconds must be positive")
        if process_stale_after_seconds < 1:
            raise ValueError("process-stale-after-seconds must be positive")
        self.corpus_root = corpus_root.resolve()
        self.run_id = run_id
        self.runtime_dir = runtime_dir.resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.progress_file = progress_file.resolve() if progress_file else None
        self.zotero_receipts_dir = (
            zotero_receipts_dir.resolve() if zotero_receipts_dir else None
        )
        self.metadata_run_dir = metadata_run_dir.resolve() if metadata_run_dir else None
        self.source_run_dir = source_run_dir.resolve() if source_run_dir else None
        self.access_run_dir = access_run_dir.resolve() if access_run_dir else None
        self.gemini_run_dir = gemini_run_dir.resolve() if gemini_run_dir else None
        self.stale_after_seconds = stale_after_seconds
        self.process_stale_after_seconds = process_stale_after_seconds
        self.run_dir = self.corpus_root / "ledgers" / f"run-{run_id}"
        self.candidates_file = self.run_dir / "deduplicated-candidates.json"
        self.summary_file = self.run_dir / "summary.json"
        self.query_receipts_file = self.run_dir / "query-receipts.json"
        self.protocol_file = self.corpus_root / "protocol" / "protocol-v2.json"
        self.database = self.runtime_dir / f"corpus-view-{run_id}.sqlite3"
        self._lock = threading.RLock()
        self._small_fingerprint = ""
        self._last_error: str | None = None
        self.refresh()

    def _screening_file(self) -> Path | None:
        files = list(self.run_dir.glob("initial-screening-ledger-r*.json"))
        if not files:
            plain = self.run_dir / "initial-screening-ledger.json"
            return plain if plain.is_file() else None
        return max(files, key=_screening_revision)

    def _metadata_progress_file(self) -> Path | None:
        if self.metadata_run_dir is None:
            return None
        return self.metadata_run_dir / "progress.json"

    def _metadata_receipt_file(self) -> Path | None:
        if self.metadata_run_dir is None:
            return None
        return self.metadata_run_dir / "run-receipt.json"

    def _metadata_dispositions_file(self) -> Path | None:
        if self.metadata_run_dir is None:
            return None
        return self.metadata_run_dir / "metadata-dispositions.ndjson"

    def _source_progress_file(self) -> Path | None:
        return self.source_run_dir / "progress.json" if self.source_run_dir else None

    def _source_pointer_file(self, name: str) -> Path | None:
        return self.source_run_dir / name if self.source_run_dir else None

    def _source_pointed_file(self, name: str) -> Path | None:
        pointer_path = self._source_pointer_file(name)
        if pointer_path is None or not pointer_path.is_file():
            return None
        pointer = _read_json(pointer_path)
        file_name = str(pointer.get("file") or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.json", file_name):
            raise ValueError(f"the {name} source-pass pointer is invalid")
        target = self.source_run_dir / file_name  # type: ignore[operator]
        if not target.is_file() or sha256_file(target) != pointer.get("sha256"):
            raise ValueError(f"the {name} source-pass target is unavailable or changed")
        return target

    def _base_fingerprint(self) -> str:
        return f"{CACHE_SCHEMA_VERSION}:{_file_fingerprint(self.candidates_file)}"

    def _overlay_fingerprint(self) -> str:
        parts = [
            _file_fingerprint(self._screening_file()),
            _file_fingerprint(self.summary_file),
            _file_fingerprint(self.query_receipts_file),
            _file_fingerprint(self.protocol_file),
            _file_fingerprint(self.progress_file),
            _directory_fingerprint(self.zotero_receipts_dir),
            _file_fingerprint(self._metadata_progress_file()),
            _file_fingerprint(self._metadata_receipt_file()),
            _file_fingerprint(self._metadata_dispositions_file()),
            _file_fingerprint(self._source_progress_file()),
            _file_fingerprint(self._source_pointer_file("overlay-current.json")),
            _file_fingerprint(self._source_pointer_file("run-receipt-current.json")),
            _file_fingerprint(
                self.access_run_dir / "progress.json" if self.access_run_dir else None
            ),
            _file_fingerprint(
                self.access_run_dir / "access-overlay.ndjson"
                if self.access_run_dir
                else None
            ),
            _file_fingerprint(
                self.gemini_run_dir / "progress.json" if self.gemini_run_dir else None
            ),
            _file_fingerprint(
                self.gemini_run_dir / "budget-ledger.json"
                if self.gemini_run_dir
                else None
            ),
            _directory_fingerprint(
                self.gemini_run_dir / "jobs" if self.gemini_run_dir else None
            ),
        ]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    def _cached_base_fingerprint(self) -> str | None:
        if not self.database.is_file():
            return None
        try:
            with sqlite3.connect(self.database) as connection:
                row = connection.execute(
                    "SELECT value FROM cache_meta WHERE key='base_fingerprint'"
                ).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def _rebuild_base(self, fingerprint: str) -> None:
        candidates = _read_json(self.candidates_file)
        if not isinstance(candidates, list):
            raise ValueError("the discovery ledger must contain a JSON array")
        temporary = self.database.with_suffix(".sqlite3.next")
        if temporary.exists():
            temporary.unlink()
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript(SCHEMA)
            rows = []
            for item in candidates:
                key = str(item.get("candidate_key") or "").strip()
                title = str(item.get("title") or "Untitled")
                if not key:
                    continue
                authors = (
                    item.get("authors") if isinstance(item.get("authors"), list) else []
                )
                repository = (item.get("open_access") or {}).get("url")
                rows.append(
                    (
                        key,
                        item.get("doi"),
                        item.get("stable_id"),
                        title,
                        title.casefold(),
                        json.dumps(authors, ensure_ascii=False),
                        item.get("year"),
                        item.get("venue"),
                        item.get("type"),
                        _safe_external_url(item.get("landing_url")),
                        _safe_external_url(repository),
                        "pending",
                        "unreviewed",
                        "unreviewed",
                        item.get("rights_access"),
                        str(
                            item.get("reason_code")
                            or "metadata_discovered_source_screening_required"
                        ),
                    )
                )
            connection.executemany(
                """INSERT INTO candidates (
                    candidate_key,doi,stable_id,title,title_search,authors_json,year,
                    venue,item_type,landing_url,repository_url,decision,eligibility,
                    pending_reason,access_status,reason_code
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            connection.execute(
                "INSERT INTO cache_meta (key,value) VALUES ('base_fingerprint',?)",
                (fingerprint,),
            )
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary, self.database)

    def _zotero_links(self) -> dict[str, str]:
        links: dict[str, str] = {}
        if self.zotero_receipts_dir is None or not self.zotero_receipts_dir.is_dir():
            return links
        for path in self.zotero_receipts_dir.glob("*.json"):
            try:
                receipt = _read_json(path)
                identity = receipt.get("source_identity") or {}
                item = receipt.get("item") or {}
                doi = str(identity.get("value") or "").casefold()
                key = str(item.get("key") or "")
                if (
                    receipt.get("state") == "verified-stored-not-accepted"
                    and identity.get("type") == "doi"
                    and doi
                    and re.fullmatch(r"[A-Z0-9]{8}", key)
                ):
                    links[doi] = f"zotero://select/library/items/{key}"
            except (OSError, ValueError, TypeError):
                continue
        return links

    def _apply_metadata_overlay(self, connection: sqlite3.Connection) -> None:
        receipt_file = self._metadata_receipt_file()
        dispositions_file = self._metadata_dispositions_file()
        if receipt_file is None or not receipt_file.is_file():
            return
        receipt = _read_json(receipt_file)
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "metadata-prefilter-run-receipt-v1"
            or receipt.get("state") != "completed"
        ):
            raise ValueError("the metadata run receipt is invalid or incomplete")
        if dispositions_file is None or not dispositions_file.is_file():
            raise ValueError("the completed metadata disposition file is unavailable")
        if sha256_file(dispositions_file) != receipt.get("dispositions_sha256"):
            raise ValueError("the completed metadata disposition file hash changed")
        seen: set[str] = set()
        counts: dict[str, int] = {}
        with dispositions_file.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                record = json.loads(line)
                key = str(record.get("candidate_key") or "")
                if not key or key in seen:
                    raise ValueError(
                        f"metadata disposition key is missing or repeated at line {line_number}"
                    )
                disposition = record.get("metadata_disposition")
                if disposition not in DISPOSITIONS:
                    raise ValueError(
                        f"unsupported metadata disposition at line {line_number}"
                    )
                evidence = record.get("evidence") or {}
                evidence_value = evidence.get("value")
                if not isinstance(evidence_value, str):
                    evidence_value = json.dumps(evidence_value, ensure_ascii=False)
                cursor = connection.execute(
                    """UPDATE candidates SET metadata_disposition=?,
                    metadata_reason_code=?,metadata_evidence_field=?,
                    metadata_evidence_value=?,metadata_queue=?,metadata_flags_json=?,
                    metadata_title_terms_json=?,metadata_policy_id=?,metadata_run_id=?
                    WHERE candidate_key=?""",
                    (
                        disposition,
                        record.get("reason_code"),
                        evidence.get("field"),
                        evidence_value,
                        (record.get("queue") or {}).get("name"),
                        json.dumps(
                            record.get("metadata_flags") or [], ensure_ascii=False
                        ),
                        json.dumps(
                            record.get("title_review_terms") or [], ensure_ascii=False
                        ),
                        record.get("policy_id"),
                        record.get("run_id"),
                        key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"metadata disposition does not match discovery key: {key}"
                    )
                seen.add(key)
                counts[disposition] = counts.get(disposition, 0) + 1
        if len(seen) != receipt.get("total"):
            raise ValueError("metadata disposition count does not match its receipt")
        if counts != receipt.get("disposition_counts"):
            raise ValueError(
                "metadata disposition subtotals do not match their receipt"
            )
        discovered = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        if len(seen) != discovered:
            raise ValueError("metadata dispositions do not account for every candidate")

    def _apply_overlays(self, fingerprint: str) -> None:
        screening_file = self._screening_file()
        screening = _read_json(screening_file) if screening_file else []
        links = self._zotero_links()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """UPDATE candidates SET decision='pending', eligibility='unreviewed',
                pending_reason='unreviewed', selected=0, evidence_locator=NULL,
                evidence_quote=NULL, zotero_url=NULL,metadata_disposition=NULL,
                metadata_reason_code=NULL,metadata_evidence_field=NULL,
                metadata_evidence_value=NULL,metadata_queue=NULL,
                metadata_flags_json=NULL,metadata_title_terms_json=NULL,
                metadata_policy_id=NULL,metadata_run_id=NULL,source_geography=NULL,
                source_pass_run_id=NULL,source_pass_position=NULL,
                source_decision_method=NULL,source_limitations_json=NULL,
                access_readiness_state='not_checked',access_readiness_reason=NULL,
                access_checked_at=NULL,access_final_url=NULL,
                gemini_status='not_started',gemini_decision=NULL"""
            )
            for item in screening:
                decision = str(item.get("decision") or "pending")
                reason = str(item.get("reason_code") or "pending_reason_not_recorded")
                if decision == "include":
                    eligibility = "eligible"
                    pending = None
                elif decision == "exclude":
                    eligibility = "excluded"
                    pending = None
                else:
                    eligibility = "pending"
                    pending = _pending_reason(reason)
                evidence = item.get("study_setting_evidence") or {}
                connection.execute(
                    """UPDATE candidates SET decision=?,eligibility=?,pending_reason=?,
                    access_status=?,reason_code=?,evidence_locator=?,evidence_quote=?,
                    selected=1,zotero_url=? WHERE candidate_key=?""",
                    (
                        decision,
                        eligibility,
                        pending,
                        item.get("access_status"),
                        reason,
                        evidence.get("locator") if isinstance(evidence, dict) else None,
                        evidence.get("evidence")
                        if isinstance(evidence, dict)
                        else None,
                        links.get(str(item.get("candidate_key") or "").casefold()),
                        item.get("candidate_key"),
                    ),
                )
            self._apply_metadata_overlay(connection)
            self._apply_source_overlay(connection, links)
            self._apply_access_overlay(connection)
            self._apply_gemini_overlay(connection)
            connection.execute(
                "INSERT OR REPLACE INTO cache_meta (key,value) VALUES ('overlay_fingerprint',?)",
                (fingerprint,),
            )
            connection.commit()

    def _apply_access_overlay(self, connection: sqlite3.Connection) -> None:
        path = (
            self.access_run_dir / "access-overlay.ndjson"
            if self.access_run_dir
            else None
        )
        if path is None or not path.is_file():
            return
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                state = item.get("access_state")
                if state not in ACCESS_FILTERS - {"all"}:
                    raise ValueError("the access-readiness state is unsupported")
                cursor = connection.execute(
                    """UPDATE candidates SET access_readiness_state=?,access_readiness_reason=?,
                    access_checked_at=?,access_final_url=? WHERE candidate_key=?""",
                    (
                        state,
                        item.get("reason_code"),
                        item.get("checked_at_utc"),
                        _safe_external_url(item.get("final_url")),
                        item.get("candidate_key"),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("an access-readiness key does not match discovery")

    def _apply_gemini_overlay(self, connection: sqlite3.Connection) -> None:
        jobs = self.gemini_run_dir / "jobs" if self.gemini_run_dir else None
        if jobs is None or not jobs.is_dir():
            return
        for path in jobs.glob("*.json"):
            item = _read_json(path)
            validation = item.get("validation") or {}
            decision = (
                validation.get("decision") if validation.get("valid") else "uncertain"
            )
            status = decision if item.get("state") == "completed" else "screening_error"
            connection.execute(
                "UPDATE candidates SET gemini_status=?,gemini_decision=? WHERE candidate_key=?",
                (status, decision, item.get("candidate_key")),
            )

    def _apply_source_overlay(
        self, connection: sqlite3.Connection, links: dict[str, str]
    ) -> None:
        overlay_file = self._source_pointed_file("overlay-current.json")
        receipt_file = self._source_pointed_file("run-receipt-current.json")
        if overlay_file is None and receipt_file is None:
            return
        if overlay_file is None or receipt_file is None:
            raise ValueError("the source-pass completion pointers are incomplete")
        overlay = _read_json(overlay_file)
        receipt = _read_json(receipt_file)
        if (
            overlay.get("schema") != "source-screening-overlay-v1"
            or receipt.get("schema") != "source-screening-run-receipt-v1"
            or receipt.get("state") not in {"paused", "completed"}
            or receipt.get("overlay_sha256") != sha256_file(overlay_file)
        ):
            raise ValueError("the source-pass receipt or overlay is invalid")
        records = overlay.get("records")
        if not isinstance(records, list) or len(records) != receipt.get(
            "selection_size"
        ):
            raise ValueError("the source-pass overlay count does not match its receipt")
        seen: set[str] = set()
        for item in records:
            key = str(item.get("candidate_key") or "")
            if not key or key in seen:
                raise ValueError(
                    "the source-pass overlay has a missing or repeated key"
                )
            eligibility = item.get("scientific_eligibility")
            if eligibility not in {"eligible", "excluded", "pending", "unreviewed"}:
                raise ValueError("the source-pass eligibility state is unsupported")
            reason = str(item.get("reason_code") or "source_reason_not_recorded")
            passages = item.get("evidence_passages") or []
            first = passages[0] if passages else {}
            locator = first.get("locator") or {}
            locator_text = None
            if locator:
                section = locator.get("section") or "source text"
                page = locator.get("page")
                offsets = (
                    f"offsets {locator.get('start_offset')}-{locator.get('end_offset')}"
                )
                locator_text = (
                    f"{section}; page {page}; {offsets}"
                    if page
                    else f"{section}; {offsets}"
                )
            cursor = connection.execute(
                """UPDATE candidates SET decision=?,eligibility=?,pending_reason=?,
                access_status=?,reason_code=?,evidence_locator=?,evidence_quote=?,
                selected=1,zotero_url=COALESCE(?,zotero_url),source_geography=?,
                source_pass_run_id=?,source_pass_position=?,source_decision_method=?,
                source_limitations_json=? WHERE candidate_key=?""",
                (
                    item.get("decision") or eligibility,
                    eligibility,
                    _pending_reason(reason)
                    if eligibility == "pending"
                    else "unreviewed"
                    if eligibility == "unreviewed"
                    else None,
                    item.get("access_state"),
                    reason,
                    locator_text,
                    first.get("quote"),
                    links.get(key.casefold()),
                    item.get("geography_verdict"),
                    overlay.get("run_id"),
                    item.get("position"),
                    item.get("decision_method_version"),
                    json.dumps(item.get("limitations") or [], ensure_ascii=False),
                    key,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"source-pass key does not match discovery: {key}")
            seen.add(key)
        if overlay.get("counts") != receipt.get("counts"):
            raise ValueError("the source-pass counts do not match their receipt")

    def refresh(self) -> None:
        with self._lock:
            try:
                required = (
                    self.candidates_file,
                    self.summary_file,
                    self.query_receipts_file,
                    self.protocol_file,
                )
                missing = next((path for path in required if not path.is_file()), None)
                if missing:
                    raise FileNotFoundError(
                        f"required corpus artifact is unavailable: {missing}"
                    )
                base = self._base_fingerprint()
                if self._cached_base_fingerprint() != base:
                    self._rebuild_base(base)
                    self._small_fingerprint = ""
                overlay = self._overlay_fingerprint()
                if overlay != self._small_fingerprint:
                    self._apply_overlays(overlay)
                    self._small_fingerprint = overlay
                self._last_error = None
            except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
                self._last_error = str(error)

    def _progress(self) -> dict[str, Any]:
        absent = {
            "schema": "corpus-progress-v1",
            "telemetry": "absent",
            "state": None,
            "stage": None,
            "updated_at_utc": None,
            "message": "No progress record is available. Current process state is unknown.",
        }
        if self.progress_file is None or not self.progress_file.is_file():
            return absent
        try:
            value = _read_json(self.progress_file)
            if value.get("schema") != "corpus-progress-v1":
                raise ValueError("unsupported progress schema")
            if value.get("state") not in PROGRESS_STATES:
                raise ValueError("unsupported progress state")
            updated_text = value.get("updated_at_utc")
            if not isinstance(updated_text, str):
                raise ValueError("progress timestamp is missing")
            updated = datetime.fromisoformat(updated_text.replace("Z", "+00:00"))
            if updated.tzinfo is None:
                raise ValueError("progress timestamp has no time zone")
            age = (datetime.now(UTC) - updated.astimezone(UTC)).total_seconds()
            if age > self.process_stale_after_seconds:
                return {
                    **absent,
                    "telemetry": "stale",
                    "last_observed_state": value["state"],
                    "stage": value.get("stage"),
                    "updated_at_utc": updated_text,
                    "message": "The progress record is stale. Current process state is unknown.",
                }
            return {
                "schema": value["schema"],
                "telemetry": "observed",
                "state": value["state"],
                "stage": value.get("stage"),
                "updated_at_utc": value.get("updated_at_utc"),
                "message": value.get("message") or "No progress message was recorded.",
                "run_id": value.get("run_id"),
                "policy_id": value.get("policy_id"),
                "processed": value.get("processed"),
                "total": value.get("total"),
                "started_at_utc": value.get("started_at_utc"),
                "completed_at_utc": value.get("completed_at_utc"),
                "disposition_counts": value.get("disposition_counts"),
                "source_counts": value.get("source_counts"),
            }
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            return {
                **absent,
                "telemetry": "invalid",
                "message": f"Progress record error: {error}",
            }

    def _metadata_progress(self) -> dict[str, Any]:
        absent = {
            "schema": "metadata-prefilter-progress-v1",
            "telemetry": "absent",
            "state": "not_started",
            "run_id": None,
            "policy_id": None,
            "producer_code_commit": None,
            "started_at_utc": None,
            "updated_at_utc": None,
            "completed_at_utc": None,
            "processed": 0,
            "total": None,
            "disposition_counts": {},
            "queue_counts": {},
            "message": "No metadata-processing run is selected.",
        }
        receipt_file = self._metadata_receipt_file()
        progress_file = self._metadata_progress_file()
        try:
            if receipt_file is not None and receipt_file.is_file():
                receipt = _read_json(receipt_file)
                if (
                    receipt.get("schema") != "metadata-prefilter-run-receipt-v1"
                    or receipt.get("state") != "completed"
                ):
                    raise ValueError("the metadata receipt is invalid")
                return {
                    **absent,
                    "telemetry": "observed",
                    "state": "completed",
                    "run_id": receipt.get("run_id"),
                    "policy_id": receipt.get("policy_id"),
                    "producer_code_commit": receipt.get("producer_code_commit"),
                    "started_at_utc": receipt.get("started_at_utc"),
                    "updated_at_utc": receipt.get("completed_at_utc"),
                    "completed_at_utc": receipt.get("completed_at_utc"),
                    "processed": receipt.get("processed"),
                    "total": receipt.get("total"),
                    "disposition_counts": receipt.get("disposition_counts") or {},
                    "queue_counts": receipt.get("queue_counts") or {},
                    "message": "The durable metadata-processing receipt is complete.",
                }
            if progress_file is None or not progress_file.is_file():
                return absent
            progress = _read_json(progress_file)
            if progress.get("schema") != "metadata-prefilter-progress-v1":
                raise ValueError("the metadata progress schema is unsupported")
            state = progress.get("state")
            if state not in {"not_running", "running", "paused", "error", "completed"}:
                raise ValueError("the metadata progress state is unsupported")
            processed = progress.get("processed")
            total = progress.get("total")
            if (
                not isinstance(processed, int)
                or not isinstance(total, int)
                or not 0 <= processed <= total
            ):
                raise ValueError("the metadata progress counts are invalid")
            updated_text = progress.get("updated_at_utc")
            if not isinstance(updated_text, str):
                raise ValueError("the metadata progress timestamp is missing")
            updated = datetime.fromisoformat(updated_text.replace("Z", "+00:00"))
            if updated.tzinfo is None:
                raise ValueError("the metadata progress timestamp has no time zone")
            age = (datetime.now(UTC) - updated.astimezone(UTC)).total_seconds()
            if age > self.process_stale_after_seconds and state != "completed":
                return {
                    **absent,
                    "telemetry": "stale",
                    "state": None,
                    "last_observed_state": state,
                    "run_id": progress.get("run_id"),
                    "policy_id": progress.get("policy_id"),
                    "producer_code_commit": progress.get("producer_code_commit"),
                    "started_at_utc": progress.get("started_at_utc"),
                    "updated_at_utc": updated_text,
                    "processed": processed,
                    "total": total,
                    "disposition_counts": progress.get("disposition_counts") or {},
                    "queue_counts": progress.get("queue_counts") or {},
                    "message": "The metadata progress record is stale. Current state is unknown.",
                }
            return {
                **absent,
                **progress,
                "telemetry": "observed",
                "message": f"Metadata-only processing is {state}: {processed} of {total} records processed.",
            }
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            return {
                **absent,
                "telemetry": "invalid",
                "state": None,
                "message": f"Metadata-processing record error: {error}",
            }

    def _source_progress(self) -> dict[str, Any]:
        absent = {
            "schema": "source-screening-progress-v1",
            "telemetry": "absent",
            "state": "not_started",
            "run_id": None,
            "policy_id": None,
            "producer_code_commit": None,
            "started_at_utc": None,
            "updated_at_utc": None,
            "completed_at_utc": None,
            "counts": {},
            "message": "No bounded source pass is selected.",
        }
        try:
            receipt_file = self._source_pointed_file("run-receipt-current.json")
            if receipt_file is not None:
                receipt = _read_json(receipt_file)
                if receipt.get(
                    "schema"
                ) != "source-screening-run-receipt-v1" or receipt.get("state") not in {
                    "paused",
                    "completed",
                }:
                    raise ValueError("the source-pass receipt is invalid")
                progress_file = self._source_progress_file()
                progress = (
                    _read_json(progress_file)
                    if progress_file and progress_file.is_file()
                    else {}
                )
                counts = dict(receipt.get("counts") or {})
                reviewed = sum(
                    int(value)
                    for value in (receipt.get("decision_method_counts") or {}).values()
                )
                counts.setdefault("full_text_reviewed", reviewed)
                counts.setdefault(
                    "full_text_review_pending",
                    max(int(counts.get("full_text_retrieved") or 0) - reviewed, 0),
                )
                state = receipt.get("state")
                return {
                    **absent,
                    "telemetry": "observed",
                    "state": state,
                    "run_id": receipt.get("run_id"),
                    "policy_id": receipt.get("policy_id"),
                    "producer_code_commit": receipt.get("producer_code_commit"),
                    "started_at_utc": progress.get("started_at_utc"),
                    "updated_at_utc": receipt.get("completed_at_utc")
                    or receipt.get("recorded_at_utc"),
                    "completed_at_utc": receipt.get("completed_at_utc"),
                    "counts": counts,
                    "overlay_revision": receipt.get("overlay_revision"),
                    "message": "The durable source-pass receipt is complete."
                    if state == "completed"
                    else "Source retrieval is complete, but retrieved full-text review is incomplete.",
                }
            progress_file = self._source_progress_file()
            if progress_file is None or not progress_file.is_file():
                return absent
            progress = _read_json(progress_file)
            if progress.get("schema") != "source-screening-progress-v1":
                raise ValueError("the source-pass progress schema is unsupported")
            state = progress.get("state")
            if state not in {"prepared", "running", "paused", "error", "completed"}:
                raise ValueError("the source-pass progress state is unsupported")
            updated_text = progress.get("updated_at_utc")
            if not isinstance(updated_text, str):
                raise ValueError("the source-pass progress timestamp is missing")
            updated = datetime.fromisoformat(updated_text.replace("Z", "+00:00"))
            if updated.tzinfo is None:
                raise ValueError("the source-pass progress timestamp has no time zone")
            age = (datetime.now(UTC) - updated.astimezone(UTC)).total_seconds()
            if age > self.process_stale_after_seconds and state != "completed":
                return {
                    **absent,
                    "telemetry": "stale",
                    "state": None,
                    "last_observed_state": state,
                    "run_id": progress.get("run_id"),
                    "policy_id": progress.get("policy_id"),
                    "producer_code_commit": progress.get("producer_code_commit"),
                    "started_at_utc": progress.get("started_at_utc"),
                    "updated_at_utc": updated_text,
                    "counts": progress.get("counts") or {},
                    "message": "The source-pass progress record is stale. Current state is unknown.",
                }
            return {**absent, **progress, "telemetry": "observed"}
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            return {
                **absent,
                "telemetry": "invalid",
                "state": None,
                "message": f"Source-pass record error: {error}",
            }

    def _artifact_rows(self) -> list[dict[str, Any]]:
        paths = [
            ("Protocol", self.protocol_file),
            ("Discovery ledger", self.candidates_file),
            ("Discovery summary", self.summary_file),
            ("Query receipts", self.query_receipts_file),
            ("Screening overlay", self._screening_file()),
            ("Progress record", self.progress_file),
            ("Metadata progress", self._metadata_progress_file()),
            ("Metadata dispositions", self._metadata_dispositions_file()),
            ("Metadata receipt", self._metadata_receipt_file()),
            ("Source-pass progress", self._source_progress_file()),
            ("Source-pass overlay", self._source_pointer_file("overlay-current.json")),
            (
                "Source-pass receipt",
                self._source_pointer_file("run-receipt-current.json"),
            ),
            (
                "Access-readiness progress",
                self.access_run_dir / "progress.json" if self.access_run_dir else None,
            ),
            (
                "Access-readiness overlay",
                self.access_run_dir / "access-overlay.ndjson"
                if self.access_run_dir
                else None,
            ),
            (
                "Gemini progress",
                self.gemini_run_dir / "progress.json" if self.gemini_run_dir else None,
            ),
            (
                "Gemini budget ledger",
                self.gemini_run_dir / "budget-ledger.json"
                if self.gemini_run_dir
                else None,
            ),
        ]
        rows = []
        for label, path in paths:
            if path is None or not path.is_file():
                rows.append({"name": label, "available": False, "updated_at_utc": None})
                continue
            updated = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            rows.append(
                {
                    "name": label,
                    "available": True,
                    "updated_at_utc": updated.replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
        return rows

    def _stage_record(
        self, directory: Path | None, schema: str, absent_message: str
    ) -> dict[str, Any]:
        absent = {
            "telemetry": "absent",
            "state": "not_started",
            "counts": {},
            "message": absent_message,
        }
        if directory is None or not (directory / "progress.json").is_file():
            return absent
        try:
            value = _read_json(directory / "progress.json")
            if value.get("schema") != schema:
                raise ValueError("unsupported progress schema")
            updated = datetime.fromisoformat(
                str(value["updated_at_utc"]).replace("Z", "+00:00")
            )
            age = (datetime.now(UTC) - updated.astimezone(UTC)).total_seconds()
            telemetry = (
                "stale"
                if age > self.process_stale_after_seconds
                and value.get("state") == "running"
                else "observed"
            )
            if telemetry == "stale":
                return {
                    **absent,
                    **value,
                    "telemetry": telemetry,
                    "last_observed_state": value.get("state"),
                    "state": None,
                    "message": "The progress record is stale. Current state is unknown.",
                }
            return {**absent, **value, "telemetry": telemetry}
        except (
            OSError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
            TypeError,
        ) as error:
            return {
                **absent,
                "telemetry": "invalid",
                "state": None,
                "message": f"Progress record error: {error}",
            }

    def state(self) -> dict[str, Any]:
        self.refresh()
        artifacts = self._artifact_rows()
        metadata = self._metadata_progress()
        source_pass = self._source_progress()
        access = self._stage_record(
            self.access_run_dir,
            "article-access-progress-v1",
            "No article-access run is selected.",
        )
        gemini = self._stage_record(
            self.gemini_run_dir,
            "gemini-eligibility-progress-v1",
            "Gemini eligibility screening is not started.",
        )
        process = self._progress()
        if (
            metadata.get("state") == "completed"
            and process.get("stage") == "metadata_prefilter"
        ):
            process = {
                "schema": "corpus-progress-v1",
                "telemetry": "observed",
                "state": "completed",
                "stage": "metadata_prefilter",
                "updated_at_utc": metadata.get("completed_at_utc"),
                "message": "Metadata-only processing is complete under its durable receipt.",
                "run_id": metadata.get("run_id"),
                "policy_id": metadata.get("policy_id"),
                "processed": metadata.get("processed"),
                "total": metadata.get("total"),
                "started_at_utc": metadata.get("started_at_utc"),
                "completed_at_utc": metadata.get("completed_at_utc"),
                "disposition_counts": metadata.get("disposition_counts"),
            }
        if (
            source_pass.get("state") == "completed"
            and process.get("stage") == "source_screening"
        ):
            process = {
                "schema": "corpus-progress-v1",
                "telemetry": "observed",
                "state": "completed",
                "stage": "source_screening",
                "updated_at_utc": source_pass.get("completed_at_utc"),
                "message": "The bounded source pass is complete under its durable receipt.",
                "run_id": source_pass.get("run_id"),
                "policy_id": source_pass.get("policy_id"),
                "processed": (source_pass.get("counts") or {}).get("processed"),
                "total": (source_pass.get("counts") or {}).get("selected"),
                "started_at_utc": source_pass.get("started_at_utc"),
                "completed_at_utc": source_pass.get("completed_at_utc"),
                "source_counts": source_pass.get("counts") or {},
            }
        if access.get("telemetry") == "observed" and access.get("state") in {
            "running",
            "paused",
            "error",
            "completed",
        }:
            process = {
                "schema": "corpus-progress-v1",
                "telemetry": "observed",
                "state": access.get("state"),
                "stage": "article_access_readiness",
                "updated_at_utc": access.get("updated_at_utc"),
                "message": access.get("message"),
                "run_id": access.get("run_id"),
                "policy_id": access.get("policy_id"),
                "processed": (access.get("counts") or {}).get("checked"),
                "total": (access.get("counts") or {}).get("target"),
                "started_at_utc": access.get("started_at_utc"),
                "completed_at_utc": access.get("completed_at_utc"),
            }
        available_dates = [
            row["updated_at_utc"]
            for row in artifacts
            if row["updated_at_utc"] and row["name"] != "Progress record"
        ]
        newest = max(available_dates) if available_dates else None
        if newest:
            age = (
                datetime.now(UTC)
                - datetime.fromisoformat(newest.replace("Z", "+00:00"))
            ).total_seconds()
            freshness = "fresh" if age <= self.stale_after_seconds else "stale"
        else:
            age = None
            freshness = "unavailable"
        payload: dict[str, Any] = {
            "generated_at_utc": _utc_now(),
            "selected_run": self.run_id,
            "availability": "unavailable" if self._last_error else "available",
            "error": self._last_error,
            "freshness": {
                "state": freshness,
                "newest_artifact_at_utc": newest,
                "age_seconds": round(age) if age is not None else None,
                "stale_after_seconds": self.stale_after_seconds,
            },
            "progress": process,
            "metadata_processing": metadata,
            "source_pass": source_pass,
            "access_readiness": access,
            "gemini_screening": gemini,
            "artifacts": artifacts,
            "data_revision": self._small_fingerprint or self._base_fingerprint(),
            "readiness": {
                "metadata_discovery": {
                    "verdict": "not_ready",
                    "completion_condition": "The required discovery artifacts are not available.",
                },
                "metadata_prefilter": {
                    "verdict": "ready"
                    if metadata.get("state") == "completed"
                    else "not_ready",
                    "completion_condition": "Every discovery record has one versioned metadata disposition and queue assignment with reconciled counts.",
                },
                "eligible_corpus": {
                    "verdict": "not_ready",
                    "completion_condition": "Every record has an accepted, excluded, or unresolved disposition under a declared stop rule. A freeze manifest hashes the corpus and inputs.",
                },
            },
        }
        if self._last_error or not self.database.is_file():
            payload["counts"] = None
            payload["protocol"] = None
            payload["stages"] = []
            return payload
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            counts = connection.execute(
                """SELECT COUNT(*) discovered,
                SUM(selected) selected,
                SUM(CASE WHEN access_status='retrieved_original_pdf' OR access_status LIKE 'retrieved_%' THEN 1 ELSE 0 END) retrieved,
                SUM(CASE WHEN eligibility='eligible' THEN 1 ELSE 0 END) eligible,
                SUM(CASE WHEN eligibility='excluded' THEN 1 ELSE 0 END) excluded,
                SUM(CASE WHEN eligibility='pending' THEN 1 ELSE 0 END) pending,
                SUM(CASE WHEN eligibility='unreviewed' THEN 1 ELSE 0 END) unreviewed
                FROM candidates"""
            ).fetchone()
        try:
            summary = _read_json(self.summary_file)
            protocol = _read_json(self.protocol_file)
            receipts = _read_json(self.query_receipts_file)
            if (
                not isinstance(summary, dict)
                or not isinstance(protocol, dict)
                or not isinstance(receipts, list)
            ):
                raise ValueError(
                    "a required corpus artifact has an unsupported JSON shape"
                )
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            payload["availability"] = "unavailable"
            payload["error"] = f"required corpus artifact error: {error}"
            payload["counts"] = None
            payload["protocol"] = None
            payload["stages"] = []
            return payload
        discovered = int(counts["discovered"] or 0)
        payload["counts"] = {key: int(counts[key] or 0) for key in counts.keys()}
        payload["metadata_counts"] = metadata.get("disposition_counts") or {}
        payload["source_pass_counts"] = source_pass.get("counts") or {}
        payload["access_counts"] = access.get("counts") or {}
        payload["gemini_counts"] = gemini.get("counts") or {}
        payload["protocol"] = {
            "protocol_id": protocol.get("protocol_id"),
            "frozen_at_utc": protocol.get("frozen_at_utc"),
        }
        queries_complete = summary.get("primary_queries_complete")
        queries_total = summary.get("primary_query_total")
        terminal_queries = {
            item.get("query_id")
            for item in receipts
            if item.get("service") == "semantic_scholar"
            and item.get("terminal") is True
            and item.get("complete") is True
            and item.get("completion_reason") == "no_continuation_token"
        }
        discovery_ready = bool(
            queries_total
            and queries_complete == queries_total
            and len(terminal_queries) == queries_total
        )
        payload["readiness"]["metadata_discovery"] = {
            "verdict": "ready" if discovery_ready else "not_ready",
            "completion_condition": "Every frozen query has a terminal no-token receipt and the deduplicated ledger is immutable.",
        }
        payload["stages"] = [
            {
                "id": "query_exhaustion",
                "name": "1. Query exhaustion",
                "state": "completed" if discovery_ready else "incomplete",
                "detail": f"{queries_complete or 0} of {queries_total or 0} frozen queries ended without a continuation token.",
            },
            {
                "id": "metadata_prefilter",
                "name": "2. Metadata prefilter",
                "state": metadata.get("state")
                if metadata.get("state") in {"running", "paused", "error", "completed"}
                else "not_started",
                "detail": (
                    f"{metadata.get('processed') or 0} of {metadata.get('total') or discovered} records processed under "
                    f"{metadata.get('policy_id') or 'no selected policy'}. Metadata status does not determine source eligibility."
                ),
            },
            {
                "id": "source_retrieval",
                "name": "3. Source retrieval",
                "state": access.get("state")
                if access.get("state") in {"running", "paused", "error", "completed"}
                else "not_started",
                "detail": (
                    f"Frozen target: {(access.get('counts') or {}).get('target', 0)}. "
                    f"Checked {(access.get('counts') or {}).get('checked', 0)}. "
                    f"Ready {(access.get('counts') or {}).get('full_text_ready', 0)}. "
                    "Missing access remains separate from scientific exclusion."
                ),
            },
            {
                "id": "eligibility_screening",
                "name": "4. Scientific eligibility",
                "state": gemini.get("state")
                if gemini.get("state") in {"running", "paused", "error", "completed"}
                else "not_started",
                "detail": (
                    f"Gemini model: {gemini.get('model') or 'not configured'}. "
                    f"Queued {(gemini.get('counts') or {}).get('queued', 0)}. "
                    "The historical native-agent pass is a pilot, not Gemini output."
                ),
            },
            {
                "id": "corpus_freeze",
                "name": "5. Final corpus freeze",
                "state": "not_started",
                "detail": "No final corpus manifest or freeze command exists for this run.",
            },
        ]
        return payload

    def candidates(self, parameters: dict[str, list[str]]) -> dict[str, Any]:
        self.refresh()
        if self._last_error or not self.database.is_file():
            raise RuntimeError(self._last_error or "the candidate index is unavailable")
        query = (parameters.get("q") or [""])[0].strip()
        if len(query) > 200:
            raise ValueError("search text must contain 200 characters or fewer")
        eligibility = (parameters.get("eligibility") or ["all"])[0]
        pending = (parameters.get("pending_reason") or ["all"])[0]
        metadata_disposition = (parameters.get("metadata_disposition") or ["all"])[0]
        access_readiness = (parameters.get("access_readiness") or ["all"])[0]
        gemini_status = (parameters.get("gemini_status") or ["all"])[0]
        if eligibility not in ELIGIBILITY_FILTERS:
            raise ValueError("unsupported eligibility filter")
        if pending not in PENDING_FILTERS:
            raise ValueError("unsupported pending-reason filter")
        if metadata_disposition not in METADATA_DISPOSITION_FILTERS:
            raise ValueError("unsupported metadata-disposition filter")
        if access_readiness not in ACCESS_FILTERS:
            raise ValueError("unsupported access-readiness filter")
        if gemini_status not in GEMINI_FILTERS:
            raise ValueError("unsupported Gemini-status filter")
        try:
            page = max(1, int((parameters.get("page") or ["1"])[0]))
            page_size = int((parameters.get("page_size") or ["25"])[0])
        except ValueError as error:
            raise ValueError("page and page-size must be integers") from error
        if page_size not in PAGE_SIZES:
            raise ValueError("page-size must be 10, 25, 50, or 100")
        where = []
        values: list[Any] = []
        if query:
            escaped = (
                query.casefold()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            where.append(
                "(title_search LIKE ? ESCAPE '\\' OR lower(COALESCE(doi,'')) LIKE ? ESCAPE '\\')"
            )
            values.extend((f"%{escaped}%", f"%{escaped}%"))
        if eligibility != "all":
            where.append("eligibility=?")
            values.append(eligibility)
        if pending != "all":
            where.append("pending_reason=?")
            values.append(pending)
        if metadata_disposition != "all":
            where.append("metadata_disposition=?")
            values.append(metadata_disposition)
        if access_readiness != "all":
            where.append("access_readiness_state=?")
            values.append(access_readiness)
        if gemini_status != "all":
            where.append("gemini_status=?")
            values.append(gemini_status)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._lock, sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            total = connection.execute(
                f"SELECT COUNT(*) FROM candidates{clause}", values
            ).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = connection.execute(
                f"""SELECT candidate_key,doi,stable_id,title,authors_json,year,venue,
                item_type,landing_url,repository_url,decision,eligibility,pending_reason,
                access_status,reason_code,evidence_locator,evidence_quote,selected,zotero_url
                ,metadata_disposition,metadata_reason_code,metadata_evidence_field,
                metadata_evidence_value,metadata_queue,metadata_flags_json,
                metadata_title_terms_json,metadata_policy_id,metadata_run_id
                ,source_geography,source_pass_run_id,source_pass_position,
                source_decision_method,source_limitations_json
                ,access_readiness_state,access_readiness_reason,access_checked_at,
                access_final_url,gemini_status,gemini_decision
                FROM candidates{clause}
                ORDER BY CASE eligibility WHEN 'eligible' THEN 0 WHEN 'excluded' THEN 1
                WHEN 'pending' THEN 2 ELSE 3 END, title_search, candidate_key
                LIMIT ? OFFSET ?""",
                [*values, page_size, (page - 1) * page_size],
            ).fetchall()
        records = []
        for row in rows:
            item = dict(row)
            item["authors"] = json.loads(item.pop("authors_json"))
            item["selected"] = bool(item["selected"])
            flags = item.pop("metadata_flags_json")
            title_terms = item.pop("metadata_title_terms_json")
            item["metadata_flags"] = json.loads(flags) if flags else []
            item["metadata_title_terms"] = (
                json.loads(title_terms) if title_terms else []
            )
            limitations = item.pop("source_limitations_json")
            item["source_limitations"] = json.loads(limitations) if limitations else []
            records.append(item)
        return {
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "total": total,
            "records": records,
            "filters": {
                "q": query,
                "eligibility": eligibility,
                "pending_reason": pending,
                "metadata_disposition": metadata_disposition,
                "access_readiness": access_readiness,
                "gemini_status": gemini_status,
            },
        }


class CorpusRequestHandler(BaseHTTPRequestHandler):
    server_version = "ArcticCorpusViewer/1"

    @property
    def artifacts(self) -> CorpusArtifacts:
        return self.server.artifacts  # type: ignore[attr-defined]

    def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        self.end_headers()

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self._headers(status, content_type, len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        self._send(status, _safe_json_bytes(payload), "application/json; charset=utf-8")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/":
                body = Path(__file__).with_name("corpus_viewer.html").read_bytes()
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
            elif parsed.path == "/api/state":
                self._json(HTTPStatus.OK, self.artifacts.state())
            elif parsed.path == "/api/candidates":
                self._json(
                    HTTPStatus.OK,
                    self.artifacts.candidates(
                        parse_qs(parsed.query, keep_blank_values=True)
                    ),
                )
            elif parsed.path == "/healthz":
                state = self.artifacts.state()
                status = (
                    HTTPStatus.OK
                    if state["availability"] == "available"
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                self._json(
                    status,
                    {"status": state["availability"], "run_id": self.artifacts.run_id},
                )
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RuntimeError as error:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "the viewer is read-only"})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


class CorpusServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], artifacts: CorpusArtifacts) -> None:
        super().__init__(address, CorpusRequestHandler)
        self.artifacts = artifacts


def serve_corpus_viewer(
    *,
    corpus_root: Path,
    run_id: str,
    runtime_dir: Path,
    progress_file: Path | None,
    zotero_receipts_dir: Path | None,
    metadata_run_dir: Path | None,
    source_run_dir: Path | None,
    access_run_dir: Path | None,
    gemini_run_dir: Path | None,
    host: str,
    port: int,
    stale_after_seconds: int,
    process_stale_after_seconds: int,
) -> None:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    artifacts = CorpusArtifacts(
        corpus_root,
        run_id,
        runtime_dir,
        progress_file=progress_file,
        zotero_receipts_dir=zotero_receipts_dir,
        metadata_run_dir=metadata_run_dir,
        source_run_dir=source_run_dir,
        access_run_dir=access_run_dir,
        gemini_run_dir=gemini_run_dir,
        stale_after_seconds=stale_after_seconds,
        process_stale_after_seconds=process_stale_after_seconds,
    )
    server = CorpusServer((host, port), artifacts)
    actual_host, actual_port = server.server_address[:2]
    print(f"Corpus viewer serving http://{actual_host}:{actual_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the read-only corpus-stage monitor."
    )
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument("--zotero-receipts-dir", type=Path)
    parser.add_argument("--metadata-run-dir", type=Path)
    parser.add_argument("--source-run-dir", type=Path)
    parser.add_argument("--access-run-dir", type=Path)
    parser.add_argument("--gemini-run-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--stale-after-seconds", type=int, default=86400)
    parser.add_argument("--process-stale-after-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    serve_corpus_viewer(
        corpus_root=args.corpus_root,
        run_id=args.run_id,
        runtime_dir=args.runtime_dir,
        progress_file=args.progress_file,
        zotero_receipts_dir=args.zotero_receipts_dir,
        metadata_run_dir=args.metadata_run_dir,
        source_run_dir=args.source_run_dir,
        access_run_dir=args.access_run_dir,
        gemini_run_dir=args.gemini_run_dir,
        host=args.host,
        port=args.port,
        stale_after_seconds=args.stale_after_seconds,
        process_stale_after_seconds=args.process_stale_after_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
