from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import re
import sqlite3
import threading
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from . import db

from . import ledger_store
from urllib.parse import parse_qs, urlsplit

from .benchmark_guard import benchmark_report
from .live_papers import live_papers_report, read_shared_ledger, read_state_facts
from .metadata_prefilter import DISPOSITIONS
from .util import sha256_file


# The page polls its four routes every 15 seconds. One background thread
# builds the three parameter-free ones on the same cadence, and a request only
# copies out the bytes that thread left. See `LiveSnapshot`.
SNAPSHOT_REFRESH_SECONDS = 15
# One fixed pool of request threads, and a bounded backlog. See `CorpusServer`.
WORKER_THREADS = 8
REQUEST_QUEUE_LIMIT = 64
_TRACE_POLL_FIELDS = {"q", "state", "stage", "run_id", "cursor", "limit"}

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
    "ambiguous_charge",
    "unresolved_rescreenable",
}
PROJECT_PROGRESS_STATUSES = {"completed", "in_progress", "not_finished"}
PROJECT_PROGRESS_SCHEMA = "project-progress-overview-v1"
PROJECT_PROGRESS_MAX_BYTES = 131_072
RESEARCH_TIMELINE_SCHEMA = "research-fleet-timeline-v1"
RESEARCH_TIMELINE_MAX_BYTES = 262_144
PUBLICATION_MANIFEST_MAX_BYTES = 131_072
PUBLICATION_FILE_MAX_BYTES = 2_000_000
LIVE_DATASET_POINTER_SCHEMA = "arctic-qa-live-dataset-pointer-v1"
LIVE_DATASET_SNAPSHOT_SCHEMA = "arctic-qa-live-dataset-snapshot-v1"
LIVE_DATASET_MANIFEST_MAX_BYTES = 131_072
LIVE_DATASET_FILE_MAX_BYTES = 64_000_000
LIVE_DATASET_FILES = {
    "benchmark": "accepted-benchmark.jsonl",
    "reviewer": "accepted-reviewer.jsonl",
}
PUBLICATION_DATA_FILES = {
    "benchmark_csv": "text/csv; charset=utf-8",
    "benchmark_jsonl": "application/x-ndjson; charset=utf-8",
    "reviewer_csv": "text/csv; charset=utf-8",
    "reviewer_jsonl": "application/x-ndjson; charset=utf-8",
    "scoring_csv": "text/csv; charset=utf-8",
    "scoring_jsonl": "application/x-ndjson; charset=utf-8",
}
RESEARCH_TIMELINE_KINDS = {
    "code_change",
    "review",
    "test",
    "live_execution",
    "outcome",
    "blocker",
    "restart",
    "monitoring",
}
RESEARCH_TIMELINE_STATUSES = {
    "completed",
    "in_progress",
    "blocked",
    "resolved",
}
RESEARCH_TIMELINE_ARTIFACT_STATES = {
    "committed",
    "deployed",
    "recorded",
    "in_progress",
    "blocked",
    "resolved",
}
PROJECT_PROGRESS_STAGE_IDS = {
    "scientific": (
        "research-design",
        "paper-discovery",
        "working-full-text",
        "scientific-eligibility",
        "qa-answer",
        "answer-verification",
        "distractor-verification",
        "usable-dataset",
        "model-evaluation",
    ),
    "engineering": (
        "api-accounting",
        "batch-progression",
        "export-counts",
        "source-span-evidence",
        "live-end-to-end-proof",
    ),
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
        gemini_connection_file: Path | None = None,
        shared_ledger_file: Path | None = None,
        streaming_budget_policy_file: Path | None = None,
        streaming_progress_file: Path | None = None,
        dataset_metadata_file: Path | None = None,
        production_plan_file: Path | None = None,
        publication_package_dir: Path | None = None,
        live_dataset_dir: Path | None = None,
        project_overview_file: Path | None = None,
        research_timeline_file: Path | None = None,
        benchmark_journal_dir: Path | None = None,
        benchmark_guard_state_file: Path | None = None,
        pipeline_trace_store: Any | None = None,
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
        self.gemini_connection_file = (
            gemini_connection_file.resolve() if gemini_connection_file else None
        )
        self.shared_ledger_file = (
            shared_ledger_file.resolve() if shared_ledger_file else None
        )
        self.streaming_budget_policy_file = (
            streaming_budget_policy_file.resolve()
            if streaming_budget_policy_file
            else None
        )
        self.streaming_progress_file = (
            streaming_progress_file.resolve() if streaming_progress_file else None
        )
        self.dataset_metadata_file = (
            dataset_metadata_file.resolve() if dataset_metadata_file else None
        )
        self.production_plan_file = (
            production_plan_file.resolve() if production_plan_file else None
        )
        self.publication_package_dir = (
            publication_package_dir.resolve() if publication_package_dir else None
        )
        self.live_dataset_dir = live_dataset_dir.resolve() if live_dataset_dir else None
        self.project_overview_file = (
            project_overview_file.resolve() if project_overview_file else None
        )
        self.research_timeline_file = (
            research_timeline_file.resolve() if research_timeline_file else None
        )
        self.benchmark_journal_dir = (
            benchmark_journal_dir.resolve() if benchmark_journal_dir else None
        )
        self.benchmark_guard_state_file = (
            benchmark_guard_state_file.resolve() if benchmark_guard_state_file else None
        )
        self.pipeline_trace_store = pipeline_trace_store
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
        # The live-papers section polls every 15 seconds, and the shared ledger
        # is several megabytes. Both caches are keyed by the file identity, so a
        # poll that finds no new bytes parses nothing and never holds a lock.
        self._live_lock = threading.Lock()
        self._ledger_cache: tuple[tuple[int, int, int], dict[str, Any]] | None = None
        self._facts_cache: (
            tuple[tuple[int, int, int, str, str], dict[str, Any]] | None
        ) = None
        self._titles_cache: tuple[tuple[int, int, int], dict[str, str]] | None = None
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
                self.access_run_dir / "access-overlay.ndjson"
                if self.access_run_dir
                else None
            ),
            _file_fingerprint(
                self.access_run_dir / "quality-notice-r1.json"
                if self.access_run_dir
                else None
            ),
            _file_fingerprint(
                self.gemini_run_dir / "gemini-overlay.ndjson"
                if self.gemini_run_dir
                else None
            ),
            _file_fingerprint(self.gemini_connection_file),
            _file_fingerprint(self.shared_ledger_file),
            _file_fingerprint(self.streaming_budget_policy_file),
            _file_fingerprint(self.streaming_progress_file),
            _file_fingerprint(self.dataset_metadata_file),
            _file_fingerprint(self.production_plan_file),
            _file_fingerprint(
                self.publication_package_dir / "manifest.json"
                if self.publication_package_dir
                else None
            ),
            _file_fingerprint(
                self.live_dataset_dir / "current.json"
                if self.live_dataset_dir
                else None
            ),
            _file_fingerprint(self.project_overview_file),
            _file_fingerprint(self.research_timeline_file),
        ]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    def _cached_base_fingerprint(self) -> str | None:
        if not self.database.is_file():
            return None
        try:
            with sqlite3.connect(self.database, timeout=db.BUSY_TIMEOUT_SECONDS) as connection:
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
        with sqlite3.connect(self.database, timeout=db.BUSY_TIMEOUT_SECONDS) as connection:
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
        overlay = (
            self.gemini_run_dir / "gemini-overlay.ndjson"
            if self.gemini_run_dir
            else None
        )
        if overlay is None or not overlay.is_file():
            return
        progress = _read_json(self.gemini_run_dir / "progress.json")
        descriptor = progress.get("overlay") or {}
        if descriptor.get("file") != overlay.name or descriptor.get(
            "sha256"
        ) != sha256_file(overlay):
            raise ValueError("the Gemini overlay does not match its progress record")
        seen: set[str] = set()
        observed: Counter[str] = Counter()
        with overlay.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                item = json.loads(line)
                key = str(item.get("candidate_key") or "")
                status = item.get("gemini_status")
                decision = item.get("gemini_decision")
                if (
                    item.get("schema") != "gemini-eligibility-overlay-row-v1"
                    or item.get("run_id") != self.gemini_run_dir.name
                    or item.get("ready_source_keys_sha256")
                    != descriptor.get("ready_source_keys_sha256")
                    or status not in GEMINI_FILTERS - {"all", "not_started"}
                    or decision not in {None, "eligible", "excluded", "uncertain"}
                    or key in seen
                ):
                    raise ValueError(
                        f"the Gemini overlay row is invalid at line {line_number}"
                    )
                seen.add(key)
                observed[str(status)] += 1
                cursor = connection.execute(
                    "UPDATE candidates SET gemini_status=?,gemini_decision=? WHERE candidate_key=?",
                    (status, decision, key),
                )
                if cursor.rowcount != 1:
                    raise ValueError("a Gemini overlay key does not match discovery")
        if len(seen) != descriptor.get("rows"):
            raise ValueError("the Gemini overlay row count does not match progress")
        counts = progress.get("counts") or {}
        for status in GEMINI_FILTERS - {"all", "not_started"}:
            if int(counts.get(status, 0)) != observed[status]:
                raise ValueError(
                    "the Gemini overlay state counts do not match progress"
                )

    def _gemini_connection(self) -> dict[str, Any]:
        absent = {
            "state": "not_checked",
            "message": "No read-only Gemini connection receipt is selected.",
        }
        path = self.gemini_connection_file
        if path is None or not path.is_file():
            return absent
        try:
            value = _read_json(path)
            if (
                value.get("schema") != "gemini-readonly-connection-check-v1"
                or value.get("method") != "GET"
                or value.get("generation_requests") != 0
                or value.get("article_uploads") != 0
            ):
                raise ValueError("unsupported connection receipt")
            authenticated = bool(
                value.get("authentication_verified") is True
                and value.get("http_status") == 200
                and value.get("returned_model") == f"models/{value.get('model')}"
            )
            return {
                "state": "authenticated_read_only" if authenticated else "error",
                "message": (
                    "The read-only model check authenticated. Paid generation remains disabled."
                    if authenticated
                    else "The read-only model check did not authenticate."
                ),
                "checked_at_utc": value.get("checked_at"),
                "model": value.get("model"),
                "input_token_limit": value.get("input_token_limit"),
                "output_token_limit": value.get("output_token_limit"),
                "generation_requests": 0,
                "article_uploads": 0,
            }
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            return {
                "state": "invalid",
                "message": f"Read-only Gemini connection record error: {error}",
            }

    def _streaming_custody_hashes(self) -> tuple[tuple[str, str], ...] | None:
        status_file = (
            self.shared_ledger_file.with_name(
                f"{self.shared_ledger_file.stem}.status.json"
            )
            if self.shared_ledger_file
            else None
        )
        paths = (
            self.streaming_progress_file,
            self.shared_ledger_file,
            status_file,
            self.streaming_budget_policy_file,
            self.dataset_metadata_file,
        )
        try:
            return tuple(
                (str(path), sha256_file(path))
                for path in paths
                if path is not None and path.is_file()
            )
        except OSError:
            return None

    def _streaming_state(
        self, *, retry_inconsistent_custody: bool = True
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "telemetry": "absent",
            "state": "not_started",
            "message": "No streaming-pipeline progress record is selected.",
            "counts": {},
            "recent_papers": [],
        }
        custody_hashes = self._streaming_custody_hashes()
        try:
            progress = None
            if self.streaming_progress_file and self.streaming_progress_file.is_file():
                progress = _read_json(self.streaming_progress_file)
                if (
                    progress.get("schema") != "streaming-dataset-progress-v1"
                    or progress.get("state") not in PROGRESS_STATES
                    or not isinstance(progress.get("counts"), dict)
                    or not isinstance(progress.get("recent_papers", []), list)
                    or len(progress.get("recent_papers", [])) > 100
                ):
                    raise ValueError("the streaming progress record is invalid")
                counts = dict(progress["counts"])
                if (
                    "rejected" in counts
                    and not {
                        "excluded",
                        "unresolved",
                    }
                    <= counts.keys()
                ):
                    legacy_count = int(counts.pop("rejected"))
                    recent = progress.get("recent_papers", [])
                    excluded = sum(
                        row.get("final_state") == "rejected" for row in recent
                    )
                    unresolved = sum(
                        row.get("final_state") == "unresolved" for row in recent
                    )
                    generation_rejected = sum(
                        row.get("final_state") == "generation_rejected"
                        for row in recent
                    )
                    if excluded + unresolved + generation_rejected == legacy_count:
                        counts["eligibility_completed"] = (
                            int(counts.get("eligible", 0)) + excluded + unresolved
                        )
                        counts["excluded"] = excluded
                        counts["unresolved"] = unresolved
                        counts["generation_rejected"] = generation_rejected
                    else:
                        counts["legacy_rejected_or_unresolved"] = legacy_count
                        counts.setdefault("excluded", 0)
                        counts.setdefault("unresolved", 0)
                        counts.setdefault("generation_rejected", 0)
                progress = {**progress, "counts": counts}
                result = {**progress, "telemetry": "observed"}
            policy = None
            if (
                self.streaming_budget_policy_file
                and self.streaming_budget_policy_file.is_file()
            ):
                policy = _read_json(self.streaming_budget_policy_file)
                if policy.get("schema") != "streaming-dataset-budget-policy-v1":
                    raise ValueError("the streaming budget policy schema is invalid")
                result["budget_policy"] = policy
            if self.shared_ledger_file and self.shared_ledger_file.is_file():
                status_file = self.shared_ledger_file.with_name(
                    f"{self.shared_ledger_file.stem}.status.json"
                )
                if not status_file.is_file():
                    raise ValueError("the invariant-checked broker status is absent")
                status = _read_json(status_file)
                if (
                    status.get("schema") != "shared-gemini-broker-status-v2"
                    or status.get("ledger_file") != str(self.shared_ledger_file)
                    or status.get("ledger_sha256")
                    != sha256_file(self.shared_ledger_file)
                    or not isinstance(status.get("stages"), dict)
                    or not isinstance(status.get("papers"), dict)
                    or not isinstance(status.get("limits"), dict)
                    or not isinstance(status.get("usage"), dict)
                    or not isinstance(status.get("remaining"), dict)
                ):
                    raise ValueError("the invariant-checked broker status is invalid")
                if policy is None or status.get("policy_sha256") != sha256_file(
                    self.streaming_budget_policy_file
                ):
                    raise ValueError("the broker status and budget policy do not match")
                if (
                    progress is None
                    or progress.get("broker_status_sha256") != sha256_file(status_file)
                    or progress.get("budget_policy_sha256")
                    != sha256_file(self.streaming_budget_policy_file)
                ):
                    raise ValueError(
                        "the streaming progress and broker custody records do not match"
                    )
                result["broker"] = {
                    **status,
                    "papers": [
                        {"family_id": family_id, **row}
                        for family_id, row in list(status["papers"].items())[-100:]
                    ],
                }
            dataset_available = bool(
                self.dataset_metadata_file and self.dataset_metadata_file.is_file()
            )
            if dataset_available:
                metadata = _read_json(self.dataset_metadata_file)
                if (
                    progress is None
                    or metadata.get("run_id") != progress.get("run_id")
                    or progress.get("dataset_metadata_sha256")
                    != sha256_file(self.dataset_metadata_file)
                ):
                    raise ValueError(
                        "the streaming progress and dataset metadata do not match"
                    )
            result["dataset_metadata_available"] = dataset_available
            if self.production_plan_file and self.production_plan_file.is_file():
                plan = _read_json(self.production_plan_file)
                future_run = plan.get("future_scientific_run") or {}
                plan_budget = plan.get("budget_and_ledger") or {}
                policy_ref = (plan.get("generation_configuration") or {}).get(
                    "budget_policy"
                ) or {}
                plan_schema = plan.get("schema")
                if (
                    plan_schema
                    not in {
                        "arctic-qa-full-run-plan-v1",
                        "arctic-qa-full-run-plan-v2",
                    }
                    or not isinstance(future_run, dict)
                    or not isinstance(plan_budget, dict)
                    or not isinstance(policy_ref, dict)
                ):
                    raise ValueError("the production plan is invalid")
                active_segment: dict[str, Any] = {}
                predecessor_segments: list[dict[str, str]] = []
                methodology: dict[str, str] = {}
                if plan_schema == "arctic-qa-full-run-plan-v2":
                    lineage = plan.get("run_segment_lineage")
                    if not isinstance(lineage, dict):
                        raise ValueError("the production plan lineage is invalid")
                    active_segment = lineage.get("active_segment") or {}
                    predecessor_values = lineage.get("predecessor_segments")
                    methodology = plan.get("methodology") or {}
                    if (
                        not isinstance(active_segment, dict)
                        or not isinstance(predecessor_values, list)
                        or not predecessor_values
                        or not isinstance(methodology, dict)
                        or active_segment.get("campaign_id")
                        != future_run.get("campaign_id")
                        or active_segment.get("run_id") != future_run.get("run_id")
                        or not isinstance(active_segment.get("segment_id"), str)
                        or not active_segment["segment_id"]
                        or not isinstance(
                            active_segment.get("execution_gate_sha256"), str
                        )
                        or not re.fullmatch(
                            r"[0-9a-f]{64}", active_segment["execution_gate_sha256"]
                        )
                        or any(
                            not isinstance(methodology.get(key), str)
                            or not methodology[key]
                            for key in (
                                "eligibility_policy_version",
                                "eligibility_prompt_version",
                                "eligibility_schema_version",
                                "generation_prompt_version",
                            )
                        )
                    ):
                        raise ValueError("the production plan lineage is invalid")
                    for predecessor in predecessor_values:
                        if (
                            not isinstance(predecessor, dict)
                            or predecessor.get("campaign_id")
                            != future_run.get("campaign_id")
                            or not isinstance(predecessor.get("segment_id"), str)
                            or not predecessor["segment_id"]
                            or not isinstance(predecessor.get("run_id"), str)
                            or not predecessor["run_id"]
                            or predecessor["run_id"] == future_run.get("run_id")
                            or not isinstance(
                                predecessor.get("run_manifest_sha256"), str
                            )
                            or not re.fullmatch(
                                r"[0-9a-f]{64}", predecessor["run_manifest_sha256"]
                            )
                        ):
                            raise ValueError("the production plan lineage is invalid")
                        predecessor_segments.append(
                            {
                                "segment_id": predecessor["segment_id"],
                                "run_id": predecessor["run_id"],
                            }
                        )
                if policy is not None and policy_ref.get("sha256") != sha256_file(
                    self.streaming_budget_policy_file
                ):
                    raise ValueError(
                        "the production plan and budget policy do not match"
                    )
                campaign_matches = bool(
                    progress
                    and progress.get("run_id") == future_run.get("campaign_id")
                    and progress.get("invocation_run_id") == future_run.get("run_id")
                )
                if (
                    progress
                    and progress.get("state")
                    in {
                        "running",
                        "paused",
                        "completed",
                    }
                    and not campaign_matches
                ):
                    raise ValueError(
                        "the production plan and progress run do not match"
                    )
                result["production_campaign"] = {
                    "campaign_id": future_run.get("campaign_id"),
                    "invocation_run_id": future_run.get("run_id"),
                    "phase": future_run.get("phase"),
                    "state": progress.get("state")
                    if campaign_matches
                    else "not_observed",
                    "incremental_ceiling_usd": plan_budget.get(
                        "remaining_to_planning_cap_usd"
                    ),
                    "prior_test_spend_usd": plan_budget.get("spent_usd"),
                    "cumulative_ceiling_usd": plan_budget.get(
                        "planning_cumulative_cap_usd"
                    ),
                    **(
                        {
                            "segment_id": active_segment["segment_id"],
                            "predecessor_segments": predecessor_segments,
                            "methodology": methodology,
                        }
                        if plan_schema == "arctic-qa-full-run-plan-v2"
                        else {}
                    ),
                }
            if self.pipeline_trace_store and result.get("telemetry") == "observed":
                latest_counts = self.pipeline_trace_store.latest_run_counts()
                if latest_counts is not None:
                    result["counts"] = {
                        **dict(result.get("counts") or {}),
                        "accepted_qa": latest_counts["accepted_qa"],
                        "generation_rejected": latest_counts["generation_rejected"],
                    }
                    result["accepted_qa_scope"] = "current_incremental_invocation"
            return result
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            if (
                retry_inconsistent_custody
                and str(error)
                in {
                    "the invariant-checked broker status is invalid",
                    "the broker status and budget policy do not match",
                    "the streaming progress and broker custody records do not match",
                    "the streaming progress and dataset metadata do not match",
                }
                and custody_hashes is not None
                and custody_hashes != self._streaming_custody_hashes()
            ):
                return self._streaming_state(retry_inconsistent_custody=False)
            return {
                **result,
                "telemetry": "invalid",
                "state": "error",
                "message": f"Streaming-pipeline record error: {error}",
            }

    def dataset_metadata(self) -> bytes:
        path = self.dataset_metadata_file
        if path is None or not path.is_file():
            raise RuntimeError("validated dataset metadata is not available")
        value = _read_json(path)
        if not isinstance(value, dict) or not {
            "schema_version",
            "export_id",
            "run_id",
            "files",
        } <= set(value):
            raise RuntimeError("validated dataset metadata has an unsupported shape")
        return _safe_json_bytes(value)

    def _publication_package(self, *, include_paths: bool = False) -> dict[str, Any]:
        root = self.publication_package_dir
        if root is None:
            return {
                "state": "not_selected",
                "trial_example": True,
                "message": "No trial publication package is selected.",
                "files": [],
            }
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("the trial publication manifest is unavailable")
        if manifest_path.stat().st_size > PUBLICATION_MANIFEST_MAX_BYTES:
            raise RuntimeError("the trial publication manifest is too large")
        manifest = _read_json(manifest_path)
        if (
            manifest.get("schema_version") != "arctic-qa-publication-review-v1"
            or manifest.get("benchmark_item_count") != 2
            or manifest.get("reviewer_item_count") != 2
        ):
            raise RuntimeError("the trial publication manifest is invalid")

        files: list[dict[str, Any]] = []

        def add_file(
            key: str, label: str, relative: Any, expected_hash: Any, content_type: str
        ) -> None:
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                raise RuntimeError("the trial publication file record is invalid")
            path = (root / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise RuntimeError(
                    "the trial publication path is outside its package"
                ) from error
            if not path.is_file() or path.stat().st_size > PUBLICATION_FILE_MAX_BYTES:
                raise RuntimeError(
                    "a trial publication file is unavailable or too large"
                )
            if sha256_file(path) != expected_hash:
                raise RuntimeError("a trial publication file hash does not match")
            files.append(
                {
                    "key": key,
                    "label": label,
                    "filename": path.name,
                    "size_bytes": path.stat().st_size,
                    "content_type": content_type,
                    "path": path,
                }
            )

        manifest_files = manifest.get("files")
        if not isinstance(manifest_files, dict):
            raise RuntimeError("the trial publication file list is invalid")
        for key, content_type in PUBLICATION_DATA_FILES.items():
            record = manifest_files.get(key)
            if not isinstance(record, dict):
                raise RuntimeError("the trial publication data files are incomplete")
            add_file(
                key,
                key.replace("_", " "),
                record.get("path"),
                record.get("sha256"),
                content_type,
            )

        templates = manifest.get("historical_prompt_templates")
        if not isinstance(templates, list) or len(templates) != 2:
            raise RuntimeError("the historical prompt companions are incomplete")
        for record in templates:
            if not isinstance(record, dict) or record.get("historical") is not True:
                raise RuntimeError("a historical prompt companion is invalid")
            relative = record.get("path")
            name = Path(relative).name if isinstance(relative, str) else ""
            if not re.fullmatch(r"(generation|validation)-v10-[A-Za-z0-9]+\.py", name):
                raise RuntimeError("a historical prompt companion name is invalid")
            role = name.split("-", 1)[0]
            add_file(
                f"historical_{role}_renderer",
                f"historical {role} renderer",
                relative,
                record.get("sha256"),
                "text/x-python; charset=utf-8",
            )
        files.insert(
            0,
            {
                "key": "manifest",
                "label": "package manifest",
                "filename": manifest_path.name,
                "size_bytes": manifest_path.stat().st_size,
                "content_type": "application/json; charset=utf-8",
                "path": manifest_path,
            },
        )
        return {
            "state": "available",
            "trial_example": True,
            "message": "Trial example only: one question with two variants. These are not production results.",
            "benchmark_item_count": 2,
            "reviewer_item_count": 2,
            "files": files
            if include_paths
            else [
                {key: value for key, value in row.items() if key != "path"}
                for row in files
            ],
        }

    def publication_download(self, key: str) -> tuple[bytes, str]:
        package = self._publication_package(include_paths=True)
        allowed = {row["key"]: row for row in package.get("files", [])}
        if key not in allowed:
            raise KeyError("unknown trial publication download")
        return allowed[key]["path"].read_bytes(), allowed[key]["content_type"]

    def _live_dataset_snapshot(self, *, include_paths: bool = False) -> dict[str, Any]:
        root = self.live_dataset_dir
        if root is None:
            return {
                "state": "not_selected",
                "message": "No live dataset snapshot is selected.",
                "item_count": 0,
                "files": [],
            }
        pointer_path = root / "current.json"
        if not pointer_path.is_file():
            raise RuntimeError("the live dataset pointer is unavailable")
        if pointer_path.stat().st_size > LIVE_DATASET_MANIFEST_MAX_BYTES:
            raise RuntimeError("the live dataset pointer is too large")
        pointer = _read_json(pointer_path)
        if pointer.get("schema") != LIVE_DATASET_POINTER_SCHEMA:
            raise RuntimeError("the live dataset pointer is invalid")
        snapshot_id = pointer.get("snapshot_id")
        expected_manifest = f"snapshots/{snapshot_id}/manifest.json"
        if (
            not isinstance(snapshot_id, str)
            or not re.fullmatch(r"live-dataset-snapshot-[a-f0-9]{32}", snapshot_id)
            or pointer.get("manifest") != expected_manifest
        ):
            raise RuntimeError("the live dataset pointer target is invalid")
        snapshot_dir = (root / "snapshots" / snapshot_id).resolve()
        try:
            snapshot_dir.relative_to(root)
        except ValueError as error:
            raise RuntimeError(
                "the live dataset snapshot is outside its root"
            ) from error
        manifest_path = snapshot_dir / "manifest.json"
        if (
            not manifest_path.is_file()
            or manifest_path.stat().st_size > LIVE_DATASET_MANIFEST_MAX_BYTES
            or sha256_file(manifest_path) != pointer.get("manifest_sha256")
        ):
            raise RuntimeError("the live dataset manifest does not match its pointer")
        manifest = _read_json(manifest_path)
        if (
            manifest.get("schema") != LIVE_DATASET_SNAPSHOT_SCHEMA
            or manifest.get("snapshot_id") != snapshot_id
            or manifest.get("item_count") != pointer.get("item_count")
            or manifest.get("updated_at_utc") != pointer.get("updated_at_utc")
        ):
            raise RuntimeError("the live dataset manifest is invalid")
        preview = manifest.get("preview")
        if preview is not None and (
            not isinstance(preview, dict)
            or preview.get("label") != "Machine-validated preview"
            or not isinstance(preview.get("notice"), str)
            or not preview["notice"]
        ):
            raise RuntimeError("the live dataset preview is invalid")
        files = []
        records = manifest.get("files")
        if not isinstance(records, dict):
            raise RuntimeError("the live dataset file list is invalid")
        for key, filename in LIVE_DATASET_FILES.items():
            record = records.get(key)
            if not isinstance(record, dict) or record.get("path") != filename:
                raise RuntimeError("the live dataset file record is invalid")
            path = snapshot_dir / filename
            if (
                not path.is_file()
                or path.stat().st_size > LIVE_DATASET_FILE_MAX_BYTES
                or path.stat().st_size != record.get("size_bytes")
                or sha256_file(path) != record.get("sha256")
            ):
                raise RuntimeError("a live dataset file is unavailable or invalid")
            files.append(
                {
                    "key": key,
                    "filename": filename,
                    "size_bytes": path.stat().st_size,
                    "content_type": "application/x-ndjson; charset=utf-8",
                    "path": path,
                }
            )
        return {
            "state": "available",
            "message": "Current-contract accepted question families.",
            "snapshot_id": snapshot_id,
            "updated_at_utc": manifest["updated_at_utc"],
            "item_count": manifest["item_count"],
            "selection": manifest.get("selection"),
            "preview": preview,
            "files": files
            if include_paths
            else [
                {name: value for name, value in row.items() if name != "path"}
                for row in files
            ],
        }

    def live_dataset_records(self, parameters: dict[str, list[str]]) -> dict[str, Any]:
        snapshot = self._live_dataset_snapshot(include_paths=True)
        files = {row["key"]: row["path"] for row in snapshot["files"]}

        def rows(path: Path) -> list[dict[str, Any]]:
            result = []
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise RuntimeError("a live dataset row is invalid")
                        result.append(value)
            return result

        benchmark = rows(files["benchmark"])
        reviewer = rows(files["reviewer"])
        review_by_id = {row.get("item_id"): row for row in reviewer}
        benchmark_ids = [row.get("item_id") for row in benchmark]
        if (
            len(benchmark) != snapshot["item_count"]
            or len(reviewer) != snapshot["item_count"]
            or len(set(benchmark_ids)) != len(benchmark_ids)
            or set(benchmark_ids) != set(review_by_id)
        ):
            raise RuntimeError("the live benchmark and reviewer rows do not join")
        query = (parameters.get("q") or [""])[0].strip().casefold()
        if len(query) > 200:
            raise ValueError("q is too long")
        joined = [
            {"benchmark": row, "reviewer": review_by_id[row["item_id"]]}
            for row in benchmark
        ]
        if query:
            joined = [
                row
                for row in joined
                if query
                in json.dumps(row, ensure_ascii=False, sort_keys=True).casefold()
            ]
        try:
            page = int((parameters.get("page") or ["1"])[0])
            page_size = int((parameters.get("page_size") or ["10"])[0])
        except ValueError as error:
            raise ValueError("page and page_size must be integers") from error
        if page < 1 or page_size not in PAGE_SIZES:
            raise ValueError("the live dataset page is invalid")
        total = len(joined)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        start = (page - 1) * page_size
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "updated_at_utc": snapshot["updated_at_utc"],
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "records": joined[start : start + page_size],
        }

    def live_dataset_download(self, key: str) -> bytes:
        snapshot = self._live_dataset_snapshot(include_paths=True)
        allowed = {row["key"]: row for row in snapshot["files"]}
        if key not in allowed:
            raise KeyError("unknown live dataset download")
        return allowed[key]["path"].read_bytes()

    @staticmethod
    def _trace_parameter(
        parameters: dict[str, list[str]], name: str, *, required: bool = False
    ) -> str | None:
        value = (parameters.get(name) or [""])[0].strip()
        if not value:
            if required:
                raise ValueError(f"{name} is required")
            return None
        if len(value) > 500 or any(ord(character) < 32 for character in value):
            raise ValueError(f"{name} contains unsupported characters")
        return value

    def pipeline_trace_list(self, parameters: dict[str, list[str]]) -> dict[str, Any]:
        if self.pipeline_trace_store is None:
            return {
                "schema": "pipeline-trace-list-v1",
                "generated_at_utc": None,
                "freshness": {"state": "absent"},
                "items": [],
                "next_cursor": None,
            }
        try:
            limit = int((parameters.get("limit") or ["25"])[0])
        except ValueError as error:
            raise ValueError("limit must be an integer") from error
        if limit not in PAGE_SIZES:
            raise ValueError("limit must be 10, 25, 50, or 100")
        result = self.pipeline_trace_store.list_papers(
            query=self._trace_parameter(parameters, "q"),
            run_id=self._trace_parameter(parameters, "run_id"),
            state=self._trace_parameter(parameters, "state"),
            stage=self._trace_parameter(parameters, "stage"),
            limit=limit,
            cursor=self._trace_parameter(parameters, "cursor"),
        )
        if (
            not isinstance(result, dict)
            or result.get("schema") != "pipeline-trace-list-v1"
        ):
            raise RuntimeError("the pipeline trace adapter returned an invalid list")
        return result

    def pipeline_trace_paper(self, parameters: dict[str, list[str]]) -> dict[str, Any]:
        if self.pipeline_trace_store is None:
            raise RuntimeError("pipeline trace data is not configured")
        paper_key = self._trace_parameter(parameters, "paper_key", required=True)
        run_id = self._trace_parameter(parameters, "run_id")
        result = (
            self.pipeline_trace_store.paper_detail(paper_key, run_id=run_id)
            if run_id
            else self.pipeline_trace_store.paper_detail(paper_key)
        )
        if (
            not isinstance(result, dict)
            or result.get("schema") != "pipeline-trace-paper-v1"
        ):
            raise RuntimeError("the pipeline trace adapter returned an invalid paper")
        return result

    def pipeline_trace_stage(self, parameters: dict[str, list[str]]) -> dict[str, Any]:
        if self.pipeline_trace_store is None:
            raise RuntimeError("pipeline trace data is not configured")
        paper_key = self._trace_parameter(parameters, "paper_key", required=True)
        stage_key = self._trace_parameter(parameters, "stage_key", required=True)
        run_id = self._trace_parameter(parameters, "run_id")
        result = (
            self.pipeline_trace_store.stage_payload(paper_key, stage_key, run_id=run_id)
            if run_id
            else self.pipeline_trace_store.stage_payload(paper_key, stage_key)
        )
        if (
            not isinstance(result, dict)
            or result.get("schema") != "pipeline-trace-stage-v1"
        ):
            raise RuntimeError("the pipeline trace adapter returned an invalid stage")
        return result

    def _project_overview(self, streaming: dict[str, Any]) -> dict[str, Any]:
        unavailable: dict[str, Any] = {
            "schema": PROJECT_PROGRESS_SCHEMA,
            "telemetry": "absent",
            "state": "unavailable",
            "updated_at_utc": None,
            "summary": "Project overview is unavailable.",
            "distinction": "Scientific-stage status is not available.",
            "scientific_stages": [],
            "engineering_stages": [],
            "notes": [],
            "live_metrics": self._project_live_metrics(streaming),
        }
        path = self.project_overview_file
        if path is None or not path.is_file():
            return unavailable
        try:
            if path.stat().st_size > PROJECT_PROGRESS_MAX_BYTES:
                raise ValueError("the project overview is too large")
            value = _read_json(path)
            if (
                not isinstance(value, dict)
                or value.get("schema") != PROJECT_PROGRESS_SCHEMA
            ):
                raise ValueError("the project overview schema is invalid")
            updated_at = self._project_overview_timestamp(value.get("updated_at_utc"))
            summary = self._project_overview_text(value.get("summary"), "summary", 600)
            distinction = self._project_overview_text(
                value.get("distinction"), "distinction", 1_200
            )
            scientific = self._project_overview_stages(
                value.get("scientific_stages"), "scientific"
            )
            engineering = self._project_overview_stages(
                value.get("engineering_stages"), "engineering"
            )
            notes_value = value.get("notes", [])
            if not isinstance(notes_value, list) or len(notes_value) > 20:
                raise ValueError("the project overview notes are invalid")
            notes = [
                self._project_overview_text(note, "note", 600) for note in notes_value
            ]
            age = (datetime.now(UTC) - updated_at).total_seconds()
            telemetry = "stale" if age > self.stale_after_seconds else "observed"
            return {
                "schema": PROJECT_PROGRESS_SCHEMA,
                "telemetry": telemetry,
                "state": "available" if telemetry == "observed" else "stale",
                "updated_at_utc": updated_at.replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "summary": summary,
                "distinction": distinction,
                "scientific_stages": scientific,
                "engineering_stages": engineering,
                "notes": notes,
                "live_metrics": self._project_live_metrics(streaming),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {
                **unavailable,
                "telemetry": "invalid",
                "summary": "Project overview is unavailable because its status record is invalid.",
            }

    @staticmethod
    def _project_overview_timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("the project overview timestamp is invalid")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("the project overview timestamp has no time zone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _project_overview_text(value: Any, name: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"the project overview {name} is invalid")
        return value.strip()

    def _project_overview_stages(
        self, value: Any, diagram: str
    ) -> list[dict[str, str]]:
        expected_ids = PROJECT_PROGRESS_STAGE_IDS[diagram]
        if not isinstance(value, list) or len(value) != len(expected_ids):
            raise ValueError(f"the {diagram} project stages are invalid")
        stages: list[dict[str, str]] = []
        seen: set[str] = set()
        for stage in value:
            if not isinstance(stage, dict):
                raise ValueError(f"a {diagram} project stage is invalid")
            stage_id = stage.get("id")
            status = stage.get("status")
            if (
                not isinstance(stage_id, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,47}", stage_id)
                or stage_id in seen
                or status not in PROJECT_PROGRESS_STATUSES
            ):
                raise ValueError(f"a {diagram} project stage identity is invalid")
            seen.add(stage_id)
            stages.append(
                {
                    "id": stage_id,
                    "label": self._project_overview_text(
                        stage.get("label"), "stage label", 80
                    ),
                    "status": status,
                    "explanation": self._project_overview_text(
                        stage.get("explanation"), "stage explanation", 1_200
                    ),
                    "next_action": self._project_overview_text(
                        stage.get("next_action"), "stage next action", 600
                    ),
                }
            )
        if tuple(stage["id"] for stage in stages) != expected_ids:
            raise ValueError(f"the {diagram} project stage sequence is invalid")
        return stages

    def _research_timeline(self) -> dict[str, Any]:
        unavailable: dict[str, Any] = {
            "schema": RESEARCH_TIMELINE_SCHEMA,
            "telemetry": "absent",
            "state": "unavailable",
            "updated_at_utc": None,
            "window_start_utc": None,
            "window_end_utc": None,
            "summary": "Research fleet timeline is unavailable.",
            "coverage_note": "No curated timeline record is configured.",
            "source_types": [],
            "entries": [],
        }
        path = self.research_timeline_file
        if path is None or not path.is_file():
            return unavailable
        try:
            if path.stat().st_size > RESEARCH_TIMELINE_MAX_BYTES:
                raise ValueError("the research timeline is too large")
            value = _read_json(path)
            if (
                not isinstance(value, dict)
                or value.get("schema") != RESEARCH_TIMELINE_SCHEMA
            ):
                raise ValueError("the research timeline schema is invalid")
            updated_at = self._project_overview_timestamp(value.get("updated_at_utc"))
            window_start = self._project_overview_timestamp(
                value.get("window_start_utc")
            )
            window_end = self._project_overview_timestamp(value.get("window_end_utc"))
            if window_end < window_start:
                raise ValueError("the research timeline window is invalid")
            sources_value = value.get("source_types", [])
            if not isinstance(sources_value, list) or len(sources_value) > 20:
                raise ValueError("the research timeline source types are invalid")
            source_types = [
                self._project_overview_text(source, "source type", 120)
                for source in sources_value
            ]
            entries_value = value.get("entries")
            if (
                not isinstance(entries_value, list)
                or not entries_value
                or len(entries_value) > 200
            ):
                raise ValueError("the research timeline entries are invalid")
            entries: list[dict[str, Any]] = []
            seen: set[str] = set()
            previous_at: datetime | None = None
            for entry in entries_value:
                if not isinstance(entry, dict):
                    raise ValueError("a research timeline entry is invalid")
                entry_id = entry.get("id")
                agent = entry.get("agent")
                stage = entry.get("stage")
                kind = entry.get("kind")
                status = entry.get("status")
                artifact_state = entry.get("artifact_state")
                if (
                    not isinstance(entry_id, str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", entry_id)
                    or entry_id in seen
                    or not isinstance(agent, str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,47}", agent)
                    or not isinstance(stage, str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,47}", stage)
                    or kind not in RESEARCH_TIMELINE_KINDS
                    or status not in RESEARCH_TIMELINE_STATUSES
                    or artifact_state not in RESEARCH_TIMELINE_ARTIFACT_STATES
                ):
                    raise ValueError("a research timeline entry identity is invalid")
                at = self._project_overview_timestamp(entry.get("at_utc"))
                if (
                    at < window_start
                    or at > window_end
                    or (previous_at is not None and at < previous_at)
                ):
                    raise ValueError("the research timeline entry order is invalid")
                evidence_url_value = entry.get("evidence_url")
                evidence_url = _safe_external_url(evidence_url_value)
                if evidence_url_value is not None and evidence_url is None:
                    raise ValueError("a research timeline evidence URL is invalid")
                seen.add(entry_id)
                previous_at = at
                entries.append(
                    {
                        "id": entry_id,
                        "at_utc": at.replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "agent": agent,
                        "agent_label": self._project_overview_text(
                            entry.get("agent_label"), "agent label", 80
                        ),
                        "stage": stage,
                        "stage_label": self._project_overview_text(
                            entry.get("stage_label"), "stage label", 80
                        ),
                        "kind": kind,
                        "status": status,
                        "artifact_state": artifact_state,
                        "title": self._project_overview_text(
                            entry.get("title"), "entry title", 160
                        ),
                        "detail": self._project_overview_text(
                            entry.get("detail"), "entry detail", 1_500
                        ),
                        "version": self._project_overview_text(
                            entry.get("version"), "entry version", 160
                        ),
                        "evidence_ref": self._project_overview_text(
                            entry.get("evidence_ref"), "evidence reference", 300
                        ),
                        "evidence_url": evidence_url,
                    }
                )
            age = (datetime.now(UTC) - updated_at).total_seconds()
            telemetry = "stale" if age > self.stale_after_seconds else "observed"

            def timestamp(item: datetime) -> str:
                return item.replace(microsecond=0).isoformat().replace("+00:00", "Z")

            return {
                "schema": RESEARCH_TIMELINE_SCHEMA,
                "telemetry": telemetry,
                "state": "available" if telemetry == "observed" else "stale",
                "updated_at_utc": timestamp(updated_at),
                "window_start_utc": timestamp(window_start),
                "window_end_utc": timestamp(window_end),
                "summary": self._project_overview_text(
                    value.get("summary"), "timeline summary", 800
                ),
                "coverage_note": self._project_overview_text(
                    value.get("coverage_note"), "coverage note", 1_200
                ),
                "source_types": source_types,
                "entries": entries,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {
                **unavailable,
                "telemetry": "invalid",
                "summary": "Research fleet timeline is unavailable because its record is invalid.",
                "coverage_note": "No timeline event is inferred from an invalid record.",
            }

    @staticmethod
    def _project_live_metrics(streaming: dict[str, Any]) -> dict[str, Any]:
        observed = streaming.get("telemetry") == "observed"
        counts = streaming.get("counts") or {}
        broker = streaming.get("broker") or {}

        def integer(mapping: dict[str, Any], key: str) -> int | None:
            if not observed or key not in mapping:
                return None
            value = mapping[key]
            if isinstance(value, bool):
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed >= 0 else None

        return {
            "source": "viewer_validated_pipeline_records",
            "telemetry": streaming.get("telemetry", "absent"),
            "scientific_count_scope": "current_incremental_invocation",
            # The progress file carries the campaign total. Only the pipeline
            # trace store can narrow it to the latest invocation, so the label
            # follows the data instead of claiming a scope the count lacks.
            "accepted_qa_scope": streaming.get(
                "accepted_qa_scope", "shared_ledger_cumulative"
            ),
            "spent_usd": broker.get("spent_usd") if observed else None,
            "reserved_usd": broker.get("reserved_usd") if observed else None,
            "ambiguous_reserved_usd": (
                broker.get("ambiguous_reserved_usd") if observed else None
            ),
            "generation_submissions": integer(broker, "generation_submissions"),
            "inflight": integer(broker, "inflight"),
            "eligible": integer(counts, "eligible"),
            "excluded": integer(counts, "excluded"),
            "unresolved": integer(counts, "unresolved"),
            "accepted_qa": integer(counts, "accepted_qa"),
        }

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
            ("Gemini read-only connection", self.gemini_connection_file),
            ("Shared paid-call ledger", self.shared_ledger_file),
            ("Streaming budget policy", self.streaming_budget_policy_file),
            ("Streaming progress", self.streaming_progress_file),
            ("Validated dataset metadata", self.dataset_metadata_file),
            ("Production campaign plan", self.production_plan_file),
            (
                "Trial publication package",
                self.publication_package_dir / "manifest.json"
                if self.publication_package_dir
                else None,
            ),
            ("Project progress overview", self.project_overview_file),
            ("Research fleet timeline", self.research_timeline_file),
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

    def _access_quality_notice(self) -> dict[str, Any] | None:
        path = (
            self.access_run_dir / "quality-notice-r1.json"
            if self.access_run_dir
            else None
        )
        if path is None or not path.is_file():
            return None
        value = _read_json(path)
        if (
            value.get("schema") != "article-access-quality-notice-v1"
            or value.get("run_id") != self.access_run_dir.name.removeprefix("run-")
            or value.get("status") != "superseded_quarantined"
        ):
            raise ValueError("the article-access quality notice is invalid")
        return {
            "status": value["status"],
            "message": value.get("message"),
            "reported_full_text_ready": value.get("reported_full_text_ready"),
            "authoritative_full_text_ready": value.get("authoritative_full_text_ready"),
            "recorded_at_utc": value.get("recorded_at_utc"),
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
        quality_notice = self._access_quality_notice()
        if quality_notice:
            access["quality_notice"] = quality_notice
            access["message"] = quality_notice["message"]
        gemini = self._stage_record(
            self.gemini_run_dir,
            "gemini-eligibility-progress-v1",
            "Gemini eligibility screening is not started.",
        )
        connection = self._gemini_connection()
        gemini["connection"] = connection
        streaming = self._streaming_state()
        project_overview = self._project_overview(streaming)
        research_timeline = self._research_timeline()
        historical_gemini = {
            "state": gemini.get("state"),
            "updated_at_utc": gemini.get("updated_at_utc"),
            "counts": gemini.get("counts") or {},
            "budget": gemini.get("budget") or {},
            "message": "This setup record predates the authenticated connection and shared budget policy.",
        }
        current_counts = streaming.get("counts") or {}
        current_broker = streaming.get("broker") or {}
        current_policy = streaming.get("budget_policy") or {}
        current_state = (
            streaming.get("state")
            if streaming.get("telemetry") == "observed"
            else "not_started"
        )
        authenticated = connection.get("state") == "authenticated_read_only"
        gemini = {
            "telemetry": streaming.get("telemetry", "absent"),
            "state": current_state,
            "model": connection.get("model") or gemini.get("model"),
            "updated_at_utc": streaming.get("updated_at_utc"),
            "message": (
                "The Gemini connection is authenticated. Paid generation is disabled until the integrated review passes."
                if authenticated and current_state == "not_running"
                else streaming.get("message")
                or "Current Gemini eligibility processing is not started."
            ),
            "counts": {
                "queued": int(current_counts.get("queued", 0)),
                "completed": int(current_counts.get("eligibility_completed", 0)),
                "eligible": int(current_counts.get("eligible", 0)),
                "excluded": int(current_counts.get("excluded", 0)),
                "uncertain": int(current_counts.get("unresolved", 0)),
                "screening_error": int(current_counts.get("screening_error", 0)),
                "too_large_not_ready": int(
                    current_counts.get("too_large_not_ready", 0)
                ),
                "ambiguous_charge": int(current_counts.get("ambiguous_charge", 0)),
            },
            "budget": {
                "away_session_total_ceiling_usd": current_policy.get(
                    "away_session_total_ceiling_usd"
                ),
                "live_test_suballocation_usd": current_policy.get(
                    "live_test_suballocation_usd"
                ),
                "reserved_usd": current_broker.get("reserved_usd", "0"),
                "spent_usd": current_broker.get("spent_usd", "0"),
                "ambiguous_reserved_usd": current_broker.get(
                    "ambiguous_reserved_usd", "0"
                ),
            },
            "connection": connection,
            "historical_setup": historical_gemini,
        }
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
        elif access.get("telemetry") in {"stale", "invalid"} and access.get("run_id"):
            process = {
                "schema": "corpus-progress-v1",
                "telemetry": access.get("telemetry"),
                "state": None,
                "last_observed_state": access.get("last_observed_state"),
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
        if gemini.get("telemetry") == "observed" and gemini.get("state") in {
            "running",
            "paused",
            "error",
            "completed",
        }:
            process = {
                "schema": "corpus-progress-v1",
                "telemetry": "observed",
                "state": gemini.get("state"),
                "stage": "scientific_eligibility",
                "updated_at_utc": gemini.get("updated_at_utc"),
                "message": gemini.get("message"),
                "run_id": self.gemini_run_dir.name if self.gemini_run_dir else None,
                "processed": sum(
                    int((gemini.get("counts") or {}).get(name, 0))
                    for name in (
                        "completed",
                        "screening_error",
                        "too_large_not_ready",
                        "ambiguous_charge",
                    )
                ),
                "total": (gemini.get("counts") or {}).get("full_text_ready"),
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
        try:
            publication_package = self._publication_package()
        except RuntimeError as error:
            publication_package = {
                "state": "invalid",
                "trial_example": True,
                "message": f"Trial publication package error: {error}",
                "files": [],
            }
        try:
            live_dataset = self._live_dataset_snapshot()
        except RuntimeError as error:
            live_dataset = {
                "state": "invalid",
                "message": f"Live dataset error: {error}",
                "item_count": 0,
                "files": [],
            }
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
            "streaming_pipeline": streaming,
            "publication_package": publication_package,
            "live_dataset": live_dataset,
            "project_overview": project_overview,
            "research_timeline": research_timeline,
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
        with sqlite3.connect(self.database, timeout=db.BUSY_TIMEOUT_SECONDS) as connection:
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
                else (
                    "unknown"
                    if access.get("telemetry") in {"stale", "invalid"}
                    and access.get("run_id")
                    else "not_started"
                ),
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
                    f"Current queued {(gemini.get('counts') or {}).get('queued', 0)}. "
                    "The prior disabled setup record is historical."
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

    def live_benchmark(self) -> dict[str, Any]:
        """Return the live-benchmarking view, rebuilt from the files each request.

        The per-model table and the per-question cost rows come from the
        streaming evaluator's cost journal. The budget, the extrapolation, the
        quota readings and the pause state come from the guard-state file that
        `arctic_qa.benchmark_guard` writes. This route reads files only: it
        never makes a paid call and never writes.
        """
        return benchmark_report(
            journal_dir=self.benchmark_journal_dir,
            guard_state_file=self.benchmark_guard_state_file,
        )

    def _cached_ledger(self) -> dict[str, Any] | None:
        """Return the shared paid-call ledger, parsed once per written version."""
        path = self.shared_ledger_file
        if path is None or not path.is_file():
            return None
        status = path.stat()
        journal = ledger_store.journal_file(path)
        try:
            journal_status = journal.stat()
            journal_key = (journal_status.st_mtime_ns, journal_status.st_size)
        except OSError:
            journal_key = (0, 0)
        # The parallel bookkeeping store moves the journal on every commit and
        # the snapshot only on a compaction, so the cache is keyed on both.
        key = (status.st_mtime_ns, status.st_size, status.st_ino, journal_key)
        with self._live_lock:
            cached = self._ledger_cache
            if cached is not None and cached[0] == key:
                return cached[1]
        ledger = read_shared_ledger(path)
        with self._live_lock:
            self._ledger_cache = (key, ledger)
        return ledger

    def _cached_state_facts(
        self, *, campaign_id: str, run_id: str
    ) -> dict[str, Any] | None:
        """Return the state-database half of the live view, cached per version."""
        configured = getattr(self.pipeline_trace_store, "db_file", None)
        if configured is None:
            return None
        path = Path(configured)
        if not path.is_file():
            return None
        status = path.stat()
        key = (
            status.st_mtime_ns,
            status.st_size,
            status.st_ino,
            campaign_id,
            run_id,
        )
        with self._live_lock:
            cached = self._facts_cache
            if cached is not None and cached[0] == key:
                return cached[1]
        facts = read_state_facts(path, campaign_id=campaign_id, run_id=run_id)
        with self._live_lock:
            self._facts_cache = (key, facts)
        return facts

    def _corpus_titles(self) -> dict[str, str]:
        """Return the corpus title of every candidate, keyed by folded DOI.

        The title of a paper the state database has not imported yet comes from
        this index, so a paper in its eligibility call still shows its title.

        The titles come from the immutable discovery ledger, which is the base
        of the index. No screening overlay touches them, so this reader never
        asks for a rebuild and never waits for one.
        """
        if not self.database.is_file():
            return {}
        status = self.database.stat()
        key = (status.st_mtime_ns, status.st_size, status.st_ino)
        with self._live_lock:
            cached = self._titles_cache
            if cached is not None and cached[0] == key:
                return cached[1]
        connection = db.connect_read_only(self.database)
        try:
            titles = {
                str(doi).casefold(): str(title)
                for doi, title in connection.execute(
                    "SELECT doi,title FROM candidates WHERE doi IS NOT NULL"
                )
            }
        except sqlite3.Error:
            return {}
        finally:
            connection.close()
        with self._live_lock:
            self._titles_cache = (key, titles)
        return titles

    def live_papers(self) -> dict[str, Any]:
        """Return the papers in analysis now and the papers that finished last.

        The route reads three records and writes nothing: the shared paid-call
        ledger under the shared form of its lock, the streaming progress record,
        and the pipeline state database read-only. It never starts, stops or
        signals the producer, and it never reads the evaluator.

        The route does not rebuild the query index. A live producer writes the
        eligibility run directory on every paper, so the index fingerprint moves
        on every poll, and a rebuild of it took about 50 seconds on 2026-09-16.
        A section that refreshes every 15 seconds must not wait for that work,
        and it needs nothing the rebuild produces.
        """
        progress: dict[str, Any] | None = None
        error: str | None = None
        try:
            if self.streaming_progress_file and self.streaming_progress_file.is_file():
                value = _read_json(self.streaming_progress_file)
                if value.get("schema") != "streaming-dataset-progress-v1":
                    raise ValueError("the streaming progress record is invalid")
                progress = value
            ledger = self._cached_ledger()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as failure:
            return live_papers_report(
                ledger=None,
                progress=None,
                error=f"The live-paper records are not readable: {failure}",
            )
        facts: dict[str, Any] | None = None
        run_id = str((progress or {}).get("invocation_run_id") or "")
        campaign_id = str((progress or {}).get("run_id") or "")
        if run_id:
            try:
                facts = self._cached_state_facts(campaign_id=campaign_id, run_id=run_id)
            except sqlite3.Error as failure:
                error = f"The pipeline state database is not readable: {failure}"
        return live_papers_report(
            ledger=ledger,
            progress=progress,
            facts=facts,
            titles=self._corpus_titles(),
            process_stale_after_seconds=self.process_stale_after_seconds,
            error=error,
        )

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
        with self._lock, sqlite3.connect(self.database, timeout=db.BUSY_TIMEOUT_SECONDS) as connection:
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


def _release_free_memory() -> None:
    """Hand glibc's free pages back to the kernel after a refresh cycle.

    The refresher parses the 23 MB ledger and the query index on every cycle
    and drops both again. Without this the freed pages stay in the arena of
    the refresher thread, and the process keeps a working set it no longer
    uses. The call is best effort: a C library without `malloc_trim` is not an
    error, and nothing on the serving path depends on it.
    """
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError, ValueError):
        return


class LiveSnapshot:
    """Serve every polled route from bytes that one background thread builds.

    Each of these routes reads the whole of some large record: the 23 MB
    shared ledger, the 110 MB query index, the evaluator's cost journal. Doing
    that inside the request thread cost the viewer its life twice on
    2026-09-17. `/api/state` answered in 6 seconds, so a page that polls every
    15 seconds queued its own requests behind each other; and each request ran
    on a thread of its own, which glibc gives a 64 MB malloc arena and never
    takes back, so the process walked to the arena cap (eight per core) and
    3.8 GB over six hours, and then answered with empty responses.

    One thread now does that work, on a fixed cadence, whatever the number of
    clients. A request copies out the bytes of the last cycle and answers in
    microseconds. The payload keeps its own `generated_at_utc`, so the page
    always shows the moment the reading was taken and never the moment it was
    served, and `X-Snapshot-Age-Seconds` states the difference.
    """

    # The four routes the page polls every 15 seconds. The trace list is here
    # with its own default query: that is the one the poll asks for, and
    # building it reads the whole state database.
    ROUTES = (
        "/api/state",
        "/api/live-benchmark",
        "/api/live-papers",
        "/api/pipeline-trace",
    )
    TRACE_POLL_QUERY = "limit=25"

    @classmethod
    def is_trace_poll(cls, query: str) -> bool:
        """True when this trace query is the page's own unfiltered poll.

        The page builds its query from the filter form, so an empty form still
        sends every field: `q=&state=&stage=&limit=25`. The match is therefore
        on what the query means and never on its spelling.
        """
        parameters = parse_qs(query, keep_blank_values=True)
        if any(
            (parameters.get(name) or [""])[0].strip()
            for name in ("q", "state", "stage", "run_id", "cursor")
        ):
            return False
        limit = (parameters.get("limit") or ["25"])[0].strip() or "25"
        return limit == "25" and not (set(parameters) - _TRACE_POLL_FIELDS)

    def __init__(
        self,
        artifacts: CorpusArtifacts,
        *,
        interval_seconds: int = SNAPSHOT_REFRESH_SECONDS,
    ) -> None:
        self._artifacts = artifacts
        self._interval = max(int(interval_seconds), 1)
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _build(self, route: str) -> dict[str, Any]:
        if route == "/api/state":
            return self._artifacts.state()
        if route == "/api/live-benchmark":
            return self._artifacts.live_benchmark()
        if route == "/api/live-papers":
            return self._artifacts.live_papers()
        if route == "/api/pipeline-trace":
            return self._artifacts.pipeline_trace_list(
                parse_qs(self.TRACE_POLL_QUERY, keep_blank_values=True)
            )
        raise KeyError(f"route not found: {route}")

    def refresh_once(self) -> None:
        """Build every route once, keeping the last good bytes of a failure."""
        for route in self.ROUTES:
            try:
                payload = self._build(route)
                entry = {
                    "body": _safe_json_bytes(payload),
                    "availability": payload.get("availability"),
                    "built_at": time.monotonic(),
                    "built_at_utc": _utc_now(),
                    "error": None,
                }
            except Exception as failure:  # noqa: BLE001 - see the note below.
                # A refresher that dies freezes every route it owns, so one
                # route's failure is recorded and the cycle goes on. The last
                # good bytes stay served, with the failure beside them, and a
                # route that never built answers 503 with the reason.
                detail = f"{type(failure).__name__}: {failure}"
                print(
                    f"corpus viewer: {route} refresh failed: {detail}\n"
                    f"{traceback.format_exc()}",
                    flush=True,
                )
                with self._lock:
                    kept = self._entries.get(route)
                if kept is None:
                    continue
                entry = {**kept, "error": detail}
            with self._lock:
                self._entries[route] = entry
        _release_free_memory()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.refresh_once()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="corpus-viewer-refresh", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def entry(self, route: str) -> dict[str, Any]:
        with self._lock:
            entry = self._entries.get(route)
        if entry is None:
            raise RuntimeError(
                f"the background refresher has not built {route} yet; "
                "retry in a moment"
            )
        return entry


class CorpusRequestHandler(BaseHTTPRequestHandler):
    server_version = "ArcticCorpusViewer/1"

    @property
    def artifacts(self) -> CorpusArtifacts:
        return self.server.artifacts  # type: ignore[attr-defined]

    @property
    def snapshot(self) -> LiveSnapshot:
        return self.server.snapshot  # type: ignore[attr-defined]

    def _cached(self, route: str) -> None:
        """Answer a polled route from the background refresher's last bytes."""
        entry = self.snapshot.entry(route)
        self._age = int(max(time.monotonic() - entry["built_at"], 0))
        self._refresh_error = entry["error"]
        self._send(
            HTTPStatus.OK, entry["body"], "application/json; charset=utf-8"
        )

    def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        age = getattr(self, "_age", None)
        if age is not None:
            self.send_header("X-Snapshot-Age-Seconds", str(age))
        refresh_error = getattr(self, "_refresh_error", None)
        if refresh_error:
            self.send_header("X-Snapshot-Refresh-Error", refresh_error[:200])
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
                self._cached("/api/state")
            elif parsed.path == "/api/candidates":
                self._json(
                    HTTPStatus.OK,
                    self.artifacts.candidates(
                        parse_qs(parsed.query, keep_blank_values=True)
                    ),
                )
            elif parsed.path == "/api/live-dataset":
                self._json(
                    HTTPStatus.OK,
                    self.artifacts.live_dataset_records(
                        parse_qs(parsed.query, keep_blank_values=True)
                    ),
                )
            elif parsed.path == "/api/live-benchmark":
                self._cached("/api/live-benchmark")
            elif parsed.path == "/api/live-papers":
                self._cached("/api/live-papers")
            elif parsed.path == "/api/pipeline-trace":
                # The page's own poll is the default query and comes from the
                # snapshot. A filter or a page the operator typed is built on
                # demand, and the store's caches keep that cheap.
                if LiveSnapshot.is_trace_poll(parsed.query):
                    self._cached("/api/pipeline-trace")
                else:
                    self._json(
                        HTTPStatus.OK,
                        self.artifacts.pipeline_trace_list(
                            parse_qs(parsed.query, keep_blank_values=True)
                        ),
                    )
            elif parsed.path == "/api/pipeline-trace/paper":
                self._json(
                    HTTPStatus.OK,
                    self.artifacts.pipeline_trace_paper(
                        parse_qs(parsed.query, keep_blank_values=True)
                    ),
                )
            elif parsed.path == "/api/pipeline-trace/stage":
                self._json(
                    HTTPStatus.OK,
                    self.artifacts.pipeline_trace_stage(
                        parse_qs(parsed.query, keep_blank_values=True)
                    ),
                )
            elif parsed.path == "/downloads/dataset-metadata.json":
                self._send(
                    HTTPStatus.OK,
                    self.artifacts.dataset_metadata(),
                    "application/json; charset=utf-8",
                )
            elif parsed.path.startswith("/downloads/trial-publication/"):
                key = parsed.path.removeprefix("/downloads/trial-publication/")
                if not re.fullmatch(r"[a-z_]{3,60}", key):
                    raise KeyError("unknown trial publication download")
                body, content_type = self.artifacts.publication_download(key)
                self._send(HTTPStatus.OK, body, content_type)
            elif parsed.path.startswith("/downloads/live-dataset/"):
                key = parsed.path.removeprefix("/downloads/live-dataset/")
                if key not in LIVE_DATASET_FILES:
                    raise KeyError("unknown live dataset download")
                self._send(
                    HTTPStatus.OK,
                    self.artifacts.live_dataset_download(key),
                    "application/x-ndjson; charset=utf-8",
                )
            elif parsed.path == "/healthz":
                # The health check reads the snapshot like every other poll.
                # Rebuilding the whole state here made the one call that is
                # meant to be cheap the most expensive call of the viewer.
                entry = self.snapshot.entry("/api/state")
                availability = entry["availability"]
                age = int(max(time.monotonic() - entry["built_at"], 0))
                status = (
                    HTTPStatus.OK
                    if availability == "available"
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                self._json(
                    status,
                    {
                        "status": availability,
                        "run_id": self.artifacts.run_id,
                        "snapshot_age_seconds": age,
                        "snapshot_built_at_utc": entry["built_at_utc"],
                        "refresh_error": entry["error"],
                    },
                )
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "route not found"})
        except KeyError as error:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(error).strip("'")})
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


class CorpusServer(HTTPServer):
    """Serve the viewer from a fixed pool of threads and a bounded backlog.

    `ThreadingHTTPServer` starts one thread per request. glibc gives each new
    thread a malloc arena of its own, 64 MB on this machine, and never returns
    those pages, so a page that polls four routes every 15 seconds walked the
    viewer to the arena cap of eight per core and about 4 GB over six hours.
    The pages the arenas hold are not a cache: nothing reads them again.

    A fixed pool holds the arena count at the size of the pool, and the
    bounded backlog answers a flood with HTTP 503 instead of an unbounded
    thread count. An unanswered request is what the captain saw on
    2026-09-17, so the refusal says so in the body rather than dropping the
    connection.
    """

    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        artifacts: CorpusArtifacts,
        snapshot: LiveSnapshot | None = None,
        *,
        workers: int = WORKER_THREADS,
        queue_limit: int = REQUEST_QUEUE_LIMIT,
        refresh_interval_seconds: int = SNAPSHOT_REFRESH_SECONDS,
    ) -> None:
        super().__init__(address, CorpusRequestHandler)
        self.artifacts = artifacts
        # The server owns the refresher: one build of every polled route
        # before the port answers, and one thread keeping them current.
        self.snapshot = snapshot or LiveSnapshot(
            artifacts, interval_seconds=refresh_interval_seconds
        )
        self.snapshot.refresh_once()
        self.snapshot.start()
        self._requests: queue.Queue[tuple[Any, Any]] = queue.Queue(
            maxsize=max(int(queue_limit), 1)
        )
        self._workers = [
            threading.Thread(
                target=self._serve_queued,
                name=f"corpus-viewer-worker-{index}",
                daemon=True,
            )
            for index in range(max(int(workers), 1))
        ]
        for worker in self._workers:
            worker.start()

    def process_request(self, request: Any, client_address: Any) -> None:
        try:
            self._requests.put_nowait((request, client_address))
        except queue.Full:
            self._refuse(request)

    def _refuse(self, request: Any) -> None:
        body = b'{"error": "the viewer backlog is full; retry in a moment"}'
        head = (
            "HTTP/1.1 503 Service Unavailable\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        try:
            request.sendall(head + body)
        except OSError:
            pass
        self.shutdown_request(request)

    def server_close(self) -> None:
        self.snapshot.stop()
        super().server_close()

    def _serve_queued(self) -> None:
        while True:
            request, client_address = self._requests.get()
            try:
                self.finish_request(request, client_address)
            except Exception:  # noqa: BLE001 - a worker must outlive a request.
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)
                self._requests.task_done()


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
    gemini_connection_file: Path | None,
    shared_ledger_file: Path | None,
    streaming_budget_policy_file: Path | None,
    streaming_progress_file: Path | None,
    dataset_metadata_file: Path | None,
    production_plan_file: Path | None,
    publication_package_dir: Path | None,
    live_dataset_dir: Path | None,
    project_overview_file: Path | None,
    research_timeline_file: Path | None,
    host: str,
    port: int,
    stale_after_seconds: int,
    process_stale_after_seconds: int,
    pipeline_namespace: Path | None = None,
    pipeline_db_file: Path | None = None,
    pipeline_receipts_dir: Path | None = None,
    pipeline_eligibility_roots: tuple[Path, ...] = (),
    benchmark_journal_dir: Path | None = None,
    benchmark_guard_state_file: Path | None = None,
    refresh_interval_seconds: int = SNAPSHOT_REFRESH_SECONDS,
    worker_threads: int = WORKER_THREADS,
) -> None:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if refresh_interval_seconds < 1:
        raise ValueError("refresh-interval-seconds must be positive")
    if worker_threads < 1:
        raise ValueError("worker-threads must be positive")
    pipeline_trace_store = None
    if pipeline_namespace is not None:
        from .pipeline_trace import PipelineTraceStore

        pipeline_trace_store = PipelineTraceStore(
            pipeline_namespace,
            db_file=pipeline_db_file,
            receipts_dir=pipeline_receipts_dir,
            ledger_file=shared_ledger_file,
            eligibility_roots=pipeline_eligibility_roots,
        )
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
        gemini_connection_file=gemini_connection_file,
        shared_ledger_file=shared_ledger_file,
        streaming_budget_policy_file=streaming_budget_policy_file,
        streaming_progress_file=streaming_progress_file,
        dataset_metadata_file=dataset_metadata_file,
        production_plan_file=production_plan_file,
        publication_package_dir=publication_package_dir,
        live_dataset_dir=live_dataset_dir,
        project_overview_file=project_overview_file,
        research_timeline_file=research_timeline_file,
        benchmark_journal_dir=benchmark_journal_dir,
        benchmark_guard_state_file=benchmark_guard_state_file,
        pipeline_trace_store=pipeline_trace_store,
        stale_after_seconds=stale_after_seconds,
        process_stale_after_seconds=process_stale_after_seconds,
    )
    server = CorpusServer(
        (host, port),
        artifacts,
        workers=worker_threads,
        refresh_interval_seconds=refresh_interval_seconds,
    )
    actual_host, actual_port = server.server_address[:2]
    print(
        f"Corpus viewer serving http://{actual_host}:{actual_port}/ "
        f"({worker_threads} request threads, "
        f"{refresh_interval_seconds}s snapshot refresh)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the read-only corpus and pipeline monitor."
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
    parser.add_argument("--gemini-connection-file", type=Path)
    parser.add_argument("--shared-ledger-file", type=Path)
    parser.add_argument("--streaming-budget-policy-file", type=Path)
    parser.add_argument("--streaming-progress-file", type=Path)
    parser.add_argument("--dataset-metadata-file", type=Path)
    parser.add_argument("--production-plan-file", type=Path)
    parser.add_argument("--publication-package-dir", type=Path)
    parser.add_argument("--live-dataset-dir", type=Path)
    parser.add_argument("--project-overview-file", type=Path)
    parser.add_argument("--research-timeline-file", type=Path)
    parser.add_argument("--benchmark-journal-dir", type=Path)
    parser.add_argument("--benchmark-guard-state-file", type=Path)
    parser.add_argument("--pipeline-namespace", type=Path)
    parser.add_argument("--pipeline-db-file", type=Path)
    parser.add_argument("--pipeline-receipts-dir", type=Path)
    parser.add_argument(
        "--pipeline-eligibility-root", type=Path, action="append", default=[]
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--stale-after-seconds", type=int, default=86400)
    parser.add_argument("--process-stale-after-seconds", type=int, default=300)
    parser.add_argument(
        "--refresh-interval-seconds",
        type=int,
        default=SNAPSHOT_REFRESH_SECONDS,
        help="how often the background thread rebuilds the polled routes",
    )
    parser.add_argument(
        "--worker-threads",
        type=int,
        default=WORKER_THREADS,
        help="the fixed number of request threads",
    )
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
        gemini_connection_file=args.gemini_connection_file,
        shared_ledger_file=args.shared_ledger_file,
        streaming_budget_policy_file=args.streaming_budget_policy_file,
        streaming_progress_file=args.streaming_progress_file,
        dataset_metadata_file=args.dataset_metadata_file,
        production_plan_file=args.production_plan_file,
        publication_package_dir=args.publication_package_dir,
        live_dataset_dir=args.live_dataset_dir,
        project_overview_file=args.project_overview_file,
        research_timeline_file=args.research_timeline_file,
        benchmark_journal_dir=args.benchmark_journal_dir,
        benchmark_guard_state_file=args.benchmark_guard_state_file,
        pipeline_namespace=args.pipeline_namespace,
        pipeline_db_file=args.pipeline_db_file,
        pipeline_receipts_dir=args.pipeline_receipts_dir,
        pipeline_eligibility_roots=tuple(args.pipeline_eligibility_root),
        host=args.host,
        port=args.port,
        stale_after_seconds=args.stale_after_seconds,
        process_stale_after_seconds=args.process_stale_after_seconds,
        refresh_interval_seconds=args.refresh_interval_seconds,
        worker_threads=args.worker_threads,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
