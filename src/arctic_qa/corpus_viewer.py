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
    zotero_url TEXT
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
        stale_after_seconds: int = 86400,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", run_id):
            raise ValueError("run-id contains unsupported characters")
        if stale_after_seconds < 1:
            raise ValueError("stale-after-seconds must be positive")
        self.corpus_root = corpus_root.resolve()
        self.run_id = run_id
        self.runtime_dir = runtime_dir.resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.progress_file = progress_file.resolve() if progress_file else None
        self.zotero_receipts_dir = (
            zotero_receipts_dir.resolve() if zotero_receipts_dir else None
        )
        self.stale_after_seconds = stale_after_seconds
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

    def _base_fingerprint(self) -> str:
        return _file_fingerprint(self.candidates_file)

    def _overlay_fingerprint(self) -> str:
        parts = [
            _file_fingerprint(self._screening_file()),
            _file_fingerprint(self.summary_file),
            _file_fingerprint(self.query_receipts_file),
            _file_fingerprint(self.protocol_file),
            _file_fingerprint(self.progress_file),
            _directory_fingerprint(self.zotero_receipts_dir),
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

    def _apply_overlays(self, fingerprint: str) -> None:
        screening_file = self._screening_file()
        screening = _read_json(screening_file) if screening_file else []
        links = self._zotero_links()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """UPDATE candidates SET decision='pending', eligibility='unreviewed',
                pending_reason='unreviewed', selected=0, evidence_locator=NULL,
                evidence_quote=NULL, zotero_url=NULL"""
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
            connection.execute(
                "INSERT OR REPLACE INTO cache_meta (key,value) VALUES ('overlay_fingerprint',?)",
                (fingerprint,),
            )
            connection.commit()

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
            if age > self.stale_after_seconds:
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
            }
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            return {
                **absent,
                "telemetry": "invalid",
                "message": f"Progress record error: {error}",
            }

    def _artifact_rows(self) -> list[dict[str, Any]]:
        paths = [
            ("Protocol", self.protocol_file),
            ("Discovery ledger", self.candidates_file),
            ("Discovery summary", self.summary_file),
            ("Query receipts", self.query_receipts_file),
            ("Screening overlay", self._screening_file()),
            ("Progress record", self.progress_file),
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

    def state(self) -> dict[str, Any]:
        self.refresh()
        artifacts = self._artifact_rows()
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
            "progress": self._progress(),
            "artifacts": artifacts,
            "data_revision": self._small_fingerprint or self._base_fingerprint(),
            "readiness": {
                "metadata_discovery": {
                    "verdict": "not_ready",
                    "completion_condition": "The required discovery artifacts are not available.",
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
                SUM(CASE WHEN access_status='retrieved_original_pdf' THEN 1 ELSE 0 END) retrieved,
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
        selected = int(counts["selected"] or 0)
        payload["counts"] = {key: int(counts[key] or 0) for key in counts.keys()}
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
                "state": "partial",
                "detail": f"The {selected}-source cap is a historical initial batch, not a full prefilter of {discovered} records.",
            },
            {
                "id": "source_retrieval",
                "name": "3. Source retrieval",
                "state": "partial",
                "detail": f"{int(counts['retrieved'] or 0)} selected sources have verified original PDFs. Missing access remains pending.",
            },
            {
                "id": "eligibility_screening",
                "name": "4. Scientific eligibility",
                "state": "partial",
                "detail": f"{int(counts['eligible'] or 0)} eligible, {int(counts['excluded'] or 0)} excluded, {int(counts['pending'] or 0)} pending, and {int(counts['unreviewed'] or 0)} unreviewed.",
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
        if eligibility not in ELIGIBILITY_FILTERS:
            raise ValueError("unsupported eligibility filter")
        if pending not in PENDING_FILTERS:
            raise ValueError("unsupported pending-reason filter")
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
    host: str,
    port: int,
    stale_after_seconds: int,
) -> None:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    artifacts = CorpusArtifacts(
        corpus_root,
        run_id,
        runtime_dir,
        progress_file=progress_file,
        zotero_receipts_dir=zotero_receipts_dir,
        stale_after_seconds=stale_after_seconds,
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--stale-after-seconds", type=int, default=86400)
    args = parser.parse_args(argv)
    serve_corpus_viewer(
        corpus_root=args.corpus_root,
        run_id=args.run_id,
        runtime_dir=args.runtime_dir,
        progress_file=args.progress_file,
        zotero_receipts_dir=args.zotero_receipts_dir,
        host=args.host,
        port=args.port,
        stale_after_seconds=args.stale_after_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
