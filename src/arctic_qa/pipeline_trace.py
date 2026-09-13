from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util import atomic_json, canonical_json, stable_id


TRACE_SCHEMA = "pipeline-model-request-trace-v1"
LIST_SCHEMA = "pipeline-trace-list-v1"
PAPER_SCHEMA = "pipeline-trace-paper-v1"
STAGE_SCHEMA = "pipeline-trace-stage-v1"

_REQUEST_KEY = re.compile(r"[a-f0-9]{64}")
_RECEIPT_STEM = re.compile(r"([a-f0-9]{64})(?:\.resume-[a-f0-9]{64})?")
_RECEIPT_EVENT = re.compile(
    r"(?P<stem>[a-f0-9]{64}(?:\.resume-[a-f0-9]{64})?)"
    r"(?:\.(?P<event>submitted|received))?\.json"
)
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credential_file",
    "thoughtsignature",
    "x-api-key",
    "x-goog-api-key",
}
_PATH_KEYS = {
    "file",
    "path",
    "relative_path",
    "replay_path",
    "span_manifest_path",
}
_ROLE_BY_STAGE = {
    "eligibility": "eligibility",
    "finding_answer_extraction": "extractor",
    "question_generation": "question_writer",
    "blinded_reconstruction": "reconstructor",
    "answer_verification": "answer_verifier",
    "distractor_generation": "distractor_writer",
    "option_verification": "option_verifier",
    "repair": "correction",
}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def record_model_request_trace(
    receipts_dir: Path,
    *,
    identity: dict[str, Any],
    payload: dict[str, Any],
    submitted_at_utc: str,
) -> bool:
    """Best-effort retention of the exact safe generateContent request.

    The broker calls this only after it has validated the request payload and
    durably recorded the paid submission. A trace write must never participate
    in accounting or change whether the provider request proceeds.
    """

    try:
        request_key = str(identity["request_key"])
        if not _REQUEST_KEY.fullmatch(request_key):
            return False
        trace = {
            "schema": TRACE_SCHEMA,
            "request_key": request_key,
            "request_sha256": identity.get("request_sha256"),
            "run_id": identity.get("run_id"),
            "stage": identity.get("stage"),
            "paper_id": identity.get("paper_id"),
            "family_id": identity.get("family_id"),
            "source_version_id": identity.get("source_version_id"),
            "model": identity.get("model"),
            "submitted_at_utc": submitted_at_utc,
            "provider_method": "generateContent",
            "payload": json.loads(canonical_json(payload)),
        }
        return atomic_json(
            receipts_dir / f"{request_key}.request-trace.json",
            trace,
            immutable=True,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


class PipelineTraceStore:
    """Read-only, identifier-addressed view of retained pipeline evidence."""

    def __init__(
        self,
        namespace: Path,
        *,
        db_file: Path | None = None,
        receipts_dir: Path | None = None,
        ledger_file: Path | None = None,
        eligibility_roots: tuple[Path, ...] = (),
    ) -> None:
        self.namespace = namespace.resolve()
        self.db_file = (db_file or self.namespace / "state.sqlite3").resolve()
        self.receipts_dir = (
            receipts_dir or self.namespace / "streaming-dataset-r1" / "model-receipts"
        ).resolve()
        self.ledger_file = (
            ledger_file
            or self.namespace / "streaming-dataset-r1" / "shared-paid-call-ledger.json"
        ).resolve()
        self.eligibility_roots = tuple(
            path.resolve()
            for path in (
                eligibility_roots or (self.namespace / "gemini-eligibility-r1",)
            )
        )
        configured = (
            self.db_file,
            self.receipts_dir,
            self.ledger_file,
            *self.eligibility_roots,
        )
        if any(not self._inside_namespace(path) for path in configured):
            raise ValueError("pipeline trace inputs must stay inside the namespace")
        self._receipt_cache_fingerprint: tuple[tuple[str, int, int], ...] = ()
        self._receipt_cache: list[dict[str, Any]] = []
        self._job_cache_fingerprint: tuple[tuple[str, int, int], ...] = ()
        self._job_cache: list[tuple[Path, dict[str, Any]]] = []

    def list_papers(
        self,
        query: str | None = None,
        run_id: str | None = None,
        state: str | None = None,
        stage: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        records = self._paper_records()
        needle = (query or "").strip().casefold()
        filtered = []
        for record in records.values():
            searchable = " ".join(
                str(record.get(key) or "")
                for key in ("paper_id", "source_id", "doi", "title")
            ).casefold()
            if needle and needle not in searchable:
                continue
            if run_id and run_id not in record["run_ids"]:
                continue
            if state and state != record["state"]:
                continue
            if stage and stage not in record["stages"]:
                continue
            filtered.append(record)
        filtered.sort(
            key=lambda item: (str(item.get("latest_at_utc") or ""), item["paper_key"]),
            reverse=True,
        )
        offset = self._decode_cursor(cursor)
        page = filtered[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "schema": LIST_SCHEMA,
            "generated_at_utc": _now(),
            "freshness": self._freshness(),
            "items": [self._list_item(row) for row in page],
            "next_cursor": (
                self._encode_cursor(next_offset)
                if next_offset < len(filtered)
                else None
            ),
        }

    def paper_detail(self, paper_key: str) -> dict[str, Any]:
        record = self._paper_records().get(paper_key)
        if record is None:
            raise KeyError("unknown pipeline paper key")
        source_ids = record["source_ids"]
        sources = [self._source_detail(source_id) for source_id in source_ids]
        stages = self._stage_summaries(record)
        return {
            "schema": PAPER_SCHEMA,
            "generated_at_utc": _now(),
            "identity": self._list_item(record),
            "runs": self._runs(record, stages),
            "sources": sources,
            "findings": self._table_json_rows("findings", "source_id", source_ids),
            "candidates": self._table_json_rows("candidates", "source_id", source_ids),
            "validation_events": self._validation_rows(source_ids),
            "rejections": self._rejection_rows(source_ids, record),
            "eligibility": self._eligibility_jobs(record),
            "exports": self._export_rows(record, source_ids),
            "stages": stages,
        }

    def stage_payload(self, paper_key: str, stage_key: str) -> dict[str, Any]:
        record = self._paper_records().get(paper_key)
        if record is None:
            raise KeyError("unknown pipeline paper key")
        stage = next(
            (
                item
                for item in self._stage_summaries(record)
                if item["stage_key"] == stage_key
            ),
            None,
        )
        if stage is None:
            raise KeyError("unknown pipeline stage key")
        receipt = None
        request_trace = None
        if stage.get("receipt_stem"):
            receipt = self._read_receipt_stem(stage["receipt_stem"])
            request_trace = self._read_request_trace(stage["request_key"])
        call = self._call_by_id(stage.get("call_id"))
        parsed_response = None
        if call and call.get("response_json"):
            parsed_response = self._json_value(call["response_json"])
        raw_response = (receipt or {}).get("response")
        if parsed_response is None:
            parsed_response = self._parse_model_text(raw_response)
        request = (
            {
                "availability": "retained",
                "retention_schema": request_trace.get("schema"),
                "provider_method": request_trace.get("provider_method"),
                "payload": request_trace.get("payload"),
            }
            if request_trace
            else {
                "availability": "not_retained",
                "reason": (
                    "The historical request body was hash-bound but was not "
                    "persisted verbatim."
                ),
                "payload": None,
            }
        )
        return {
            "schema": STAGE_SCHEMA,
            "generated_at_utc": _now(),
            "paper_key": paper_key,
            "stage": {
                key: value for key, value in stage.items() if key != "receipt_stem"
            },
            "request": request,
            "response": {
                "availability": "retained"
                if raw_response is not None
                else "not_retained",
                "raw_provider_response": self._safe_value(raw_response),
                "model_text": self._model_text(raw_response),
                "parsed_response": self._safe_value(parsed_response),
            },
            "journal": self._safe_call(call),
            "receipt": self._safe_receipt(receipt),
            "eligibility": self._eligibility_for_request(stage.get("request_key")),
            "source_context": [
                self._source_context(source_id) for source_id in record["source_ids"]
            ],
            "candidates": self._table_json_rows(
                "candidates", "source_id", record["source_ids"]
            ),
        }

    def _inside_namespace(self, path: Path) -> bool:
        return path == self.namespace or self.namespace in path.parents

    def _connect(self) -> sqlite3.Connection:
        if not self.db_file.is_file():
            raise FileNotFoundError("pipeline state database is unavailable")
        connection = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _paper_records(self) -> dict[str, dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        with self._connect() as connection:
            sources = [dict(row) for row in connection.execute("SELECT * FROM sources")]
            candidates = [
                dict(row)
                for row in connection.execute(
                    "SELECT source_id,run_id,status,updated_at FROM candidates"
                )
            ]
            findings = [
                dict(row)
                for row in connection.execute(
                    "SELECT source_id,run_id,status,created_at FROM findings"
                )
            ]
        source_by_family: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            source_by_family.setdefault(source["paper_family_id"], []).append(source)
        receipts = self._receipt_events()
        families = set(source_by_family) | {
            str(event.get("family_id")) for event in receipts if event.get("family_id")
        }
        progress_titles = self._progress_titles()
        for family_id in families:
            family_sources = source_by_family.get(family_id, [])
            family_receipts = [
                event for event in receipts if event.get("family_id") == family_id
            ]
            receipt_ids = [
                str(event.get("paper_id"))
                for event in family_receipts
                if event.get("paper_id")
            ]
            source = family_sources[0] if family_sources else {}
            paper_id = str(
                source.get("doi")
                or source.get("stable_id")
                or (
                    receipt_ids[-1]
                    if receipt_ids
                    else source.get("source_id") or family_id
                )
            )
            source_ids = [str(item["source_id"]) for item in family_sources]
            relevant_candidates = [
                row for row in candidates if row["source_id"] in source_ids
            ]
            relevant_findings = [
                row for row in findings if row["source_id"] in source_ids
            ]
            run_ids = sorted(
                {
                    str(value)
                    for value in (
                        *[item.get("run_id") for item in family_receipts],
                        *[item.get("run_id") for item in relevant_candidates],
                        *[item.get("run_id") for item in relevant_findings],
                    )
                    if value
                }
            )
            latest_values = [
                str(value)
                for value in (
                    *[
                        item.get("completed_at_utc") or item.get("submitted_at_utc")
                        for item in family_receipts
                    ],
                    *[item.get("updated_at") for item in relevant_candidates],
                    *[item.get("created_at") for item in relevant_findings],
                )
                if value
            ]
            stages = sorted(
                {
                    str(item.get("stage"))
                    for item in family_receipts
                    if item.get("stage")
                }
            )
            state = self._paper_state(
                relevant_candidates,
                family_receipts,
                self._eligibility_state(
                    {
                        str(item.get("request_key"))
                        for item in family_receipts
                        if item.get("request_key")
                    },
                    {str(value) for value in (*receipt_ids, *source_ids) if value},
                ),
            )
            title = source.get("title")
            if not title:
                title = next(
                    (
                        progress_titles.get(identifier)
                        for identifier in (*receipt_ids, *source_ids)
                        if progress_titles.get(identifier)
                    ),
                    None,
                )
            key = stable_id("pipeline-paper", family_id)
            latest_receipt = max(
                family_receipts,
                key=lambda item: str(
                    item.get("completed_at_utc") or item.get("submitted_at_utc") or ""
                ),
                default={},
            )
            groups[key] = {
                "paper_key": key,
                "paper_id": paper_id,
                "source_id": source_ids[0] if source_ids else None,
                "source_ids": source_ids,
                "family_id": family_id,
                "doi": source.get("doi"),
                "title": title,
                "run_ids": run_ids,
                "state": state,
                "current_stage": latest_receipt.get("stage", "not_started"),
                "stages": stages,
                "attempt_count": len(family_receipts),
                "latest_at_utc": max(latest_values, default=None),
                "receipts": family_receipts,
            }
        return groups

    def _receipt_events(self) -> list[dict[str, Any]]:
        if not self.receipts_dir.is_dir():
            return []
        paths: dict[str, dict[str, Path]] = {}
        fingerprint_rows = []
        for path in sorted(self.receipts_dir.glob("*.json")):
            match = _RECEIPT_EVENT.fullmatch(path.name)
            if not match:
                continue
            event = match.group("event") or "final"
            paths.setdefault(match.group("stem"), {})[event] = path
            stat = path.stat()
            fingerprint_rows.append((path.name, stat.st_size, stat.st_mtime_ns))
        fingerprint = tuple(fingerprint_rows)
        if fingerprint == self._receipt_cache_fingerprint:
            return self._receipt_cache
        events = []
        for stem, candidates in sorted(paths.items()):
            event_name = next(
                name
                for name in ("final", "received", "submitted")
                if name in candidates
            )
            value = self._read_json(candidates[event_name])
            if not isinstance(value, dict) or not value.get("request_key"):
                continue
            events.append({**value, "receipt_stem": stem, "receipt_event": event_name})
        self._receipt_cache_fingerprint = fingerprint
        self._receipt_cache = events
        return self._receipt_cache

    def _stage_summaries(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        calls = self._calls(record["run_ids"])
        by_request_id = {
            str(call["request_id"]): call for call in calls if call.get("request_id")
        }
        summaries = []
        attempts: dict[str, int] = {}
        for receipt in sorted(
            record["receipts"],
            key=lambda item: str(
                item.get("submitted_at_utc") or item.get("completed_at_utc") or ""
            ),
        ):
            request_key = str(receipt["request_key"])
            attempts[request_key] = attempts.get(request_key, 0) + 1
            response_id = (receipt.get("response") or {}).get("responseId")
            call = by_request_id.get(str(response_id)) if response_id else None
            started = receipt.get("submitted_at_utc") or (call or {}).get("started_at")
            completed = receipt.get("completed_at_utc") or (call or {}).get(
                "completed_at"
            )
            summaries.append(
                {
                    "stage_key": receipt["receipt_stem"],
                    "receipt_stem": receipt["receipt_stem"],
                    "request_key": request_key,
                    "call_id": (call or {}).get("call_id"),
                    "run_id": receipt.get("run_id"),
                    "stage": receipt.get("stage"),
                    "role": (call or {}).get("role")
                    or _ROLE_BY_STAGE.get(str(receipt.get("stage"))),
                    "state": receipt.get("state"),
                    "attempt": (call or {}).get("attempt") or attempts[request_key],
                    "timing": {
                        "started_at_utc": started,
                        "completed_at_utc": completed,
                        "duration_ms": self._duration_ms(started, completed),
                    },
                    "model": {
                        "requested": receipt.get("model"),
                        "returned": (
                            (receipt.get("response") or {}).get("modelVersion")
                        ),
                        "prompt_version": (call or {}).get("prompt_version"),
                        "prompt_hash": (call or {}).get("prompt_hash"),
                    },
                    "cost": {
                        "reserved_usd": receipt.get("reserved_usd"),
                        "actual_usd": receipt.get("actual_cost_usd"),
                    },
                    "usage": receipt.get("usage"),
                    "payload_availability": (
                        "retained"
                        if self._request_trace_path(request_key).is_file()
                        else "not_retained"
                    ),
                }
            )
        return sorted(
            summaries,
            key=lambda item: str(item["timing"].get("started_at_utc") or ""),
        )

    def _calls(self, run_ids: list[str]) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM calls WHERE run_id IN ({placeholders}) ORDER BY started_at,call_id",
                    tuple(run_ids),
                )
            ]

    def _source_detail(self, source_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            source_row = connection.execute(
                "SELECT * FROM sources WHERE source_id=?", (source_id,)
            ).fetchone()
            artifact_rows = connection.execute(
                "SELECT * FROM artifacts WHERE source_id=? ORDER BY kind,created_at",
                (source_id,),
            ).fetchall()
        if source_row is None:
            return {"source_id": source_id, "availability": "not_retained"}
        source = dict(source_row)
        for key in tuple(source):
            if key.endswith("_json"):
                source[key.removesuffix("_json")] = self._json_value(source.pop(key))
        source["artifacts"] = []
        for artifact_row in artifact_rows:
            artifact = dict(artifact_row)
            artifact.pop("relative_path", None)
            artifact["metadata"] = self._json_value(artifact.pop("metadata_json"))
            source["artifacts"].append(self._safe_value(artifact))
        return self._safe_value(source)

    def _source_context(self, source_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            artifact = connection.execute(
                "SELECT relative_path,content_hash FROM artifacts WHERE source_id=? AND kind='chunks' ORDER BY created_at DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        if artifact is None:
            return {
                "source_id": source_id,
                "availability": "not_retained",
                "chunks": [],
            }
        path = (self.namespace / artifact["relative_path"]).resolve()
        if not self._inside_namespace(path) or not path.is_file():
            return {"source_id": source_id, "availability": "unavailable", "chunks": []}
        chunks = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    chunks.append(json.loads(line))
        return {
            "source_id": source_id,
            "availability": "retained",
            "content_hash": artifact["content_hash"],
            "chunks": self._safe_value(chunks),
        }

    def _table_json_rows(
        self, table: str, source_column: str, source_ids: list[str]
    ) -> list[dict[str, Any]]:
        if table not in {"findings", "candidates"} or source_column != "source_id":
            raise ValueError("unsupported pipeline table")
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        with self._connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} WHERE source_id IN ({placeholders}) ORDER BY created_at",
                    tuple(source_ids),
                )
            ]
        for row in rows:
            for key in tuple(row):
                if key.endswith("_json"):
                    row[key.removesuffix("_json")] = self._json_value(row.pop(key))
        return self._safe_value(rows)

    def _validation_rows(self, source_ids: list[str]) -> list[dict[str, Any]]:
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        with self._connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    f"SELECT v.* FROM validation_events v JOIN candidates c ON c.item_id=v.item_id WHERE c.source_id IN ({placeholders}) ORDER BY v.created_at,v.event_id",
                    tuple(source_ids),
                )
            ]
        for row in rows:
            row["reason_codes"] = self._json_value(row.pop("reason_codes_json"))
            row["details"] = self._json_value(row.pop("details_json"))
        return self._safe_value(rows)

    def _rejection_rows(
        self, source_ids: list[str], record: dict[str, Any]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            with self._connect() as connection:
                rows = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM rejection_ledger WHERE source_id IN ({placeholders}) OR item_id IN (SELECT item_id FROM candidates WHERE source_id IN ({placeholders})) ORDER BY created_at,rejection_id",
                        (*source_ids, *source_ids),
                    )
                ]
        for row in rows:
            row["detail"] = self._json_value(row.pop("detail_json"))
        for job in self._eligibility_jobs(record):
            validation = job.get("validation") or {}
            if validation.get("decision") not in {None, "eligible"}:
                rows.append(
                    {
                        "stage": "scientific_eligibility",
                        "reason_code": (
                            validation.get("overall_reason_codes")
                            or ["eligibility_unresolved"]
                        )[0],
                        "detail": validation,
                        "created_at": job.get("completed_at_utc"),
                    }
                )
        return self._safe_value(rows)

    def _eligibility_jobs(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        request_keys = {
            str(item.get("request_key"))
            for item in record["receipts"]
            if item.get("request_key")
        }
        candidate_ids = {
            str(value)
            for value in (
                record.get("paper_id"),
                record.get("doi"),
                *record.get("source_ids", []),
            )
            if value
        }
        jobs = []
        for path, job in self._all_eligibility_jobs():
            if (
                job.get("broker_request_key") not in request_keys
                and str(job.get("candidate_key")) not in candidate_ids
            ):
                continue
            safe = self._safe_value(job)
            manifest = (
                path.parent.parent / "span-manifests" / f"{job.get('job_key')}.json"
            )
            safe["span_manifest"] = (
                self._safe_value(self._read_json(manifest))
                if manifest.is_file()
                else None
            )
            jobs.append(safe)
        return jobs

    def _eligibility_for_request(
        self, request_key: str | None
    ) -> dict[str, Any] | None:
        if not request_key:
            return None
        for path, job in self._all_eligibility_jobs():
            if job.get("broker_request_key") == request_key:
                safe = self._safe_value(job)
                manifest = (
                    path.parent.parent / "span-manifests" / f"{job.get('job_key')}.json"
                )
                safe["span_manifest"] = (
                    self._safe_value(self._read_json(manifest))
                    if manifest.is_file()
                    else None
                )
                return safe
        return None

    def _job_paths(self) -> list[Path]:
        return sorted(
            path
            for root in self.eligibility_roots
            if root.is_dir()
            for path in root.glob("*/jobs/*.json")
        )

    def _all_eligibility_jobs(self) -> list[tuple[Path, dict[str, Any]]]:
        paths = self._job_paths()
        fingerprint = tuple(
            (str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths
        )
        if fingerprint == self._job_cache_fingerprint:
            return self._job_cache
        jobs = []
        for path in paths:
            value = self._read_json(path)
            if isinstance(value, dict):
                jobs.append((path, value))
        self._job_cache_fingerprint = fingerprint
        self._job_cache = jobs
        return self._job_cache

    def _eligibility_state(
        self, request_keys: set[str], candidate_ids: set[str]
    ) -> str | None:
        matching = [
            job
            for _, job in self._all_eligibility_jobs()
            if job.get("broker_request_key") in request_keys
            or str(job.get("candidate_key")) in candidate_ids
        ]
        if not matching:
            return None
        latest = max(matching, key=lambda job: str(job.get("completed_at_utc") or ""))
        validation = latest.get("validation") or {}
        decision = validation.get("decision")
        if validation.get("valid") is not True and decision != "excluded":
            return "eligibility_unresolved"
        if decision == "uncertain":
            return "eligibility_unresolved"
        if decision == "excluded":
            return "eligibility_rejected"
        if decision == "eligible":
            return "eligible"
        return "eligibility_completed"

    def _export_rows(
        self, record: dict[str, Any], source_ids: list[str]
    ) -> list[dict[str, Any]]:
        exports_dir = self.namespace / "exports"
        if not exports_dir.is_dir():
            return []
        item_ids = {
            row["item_id"]
            for row in self._table_json_rows("candidates", "source_id", source_ids)
        }
        rows = []
        for manifest_path in sorted(exports_dir.glob("*/manifest.json")):
            manifest = self._read_json(manifest_path)
            if (
                not isinstance(manifest, dict)
                or manifest.get("run_id") not in record["run_ids"]
            ):
                continue
            for kind in (
                "short_answer",
                "incomplete_short_answer",
                "mcq",
                "rejections",
            ):
                data_path = manifest_path.parent / f"{kind}.jsonl"
                if not data_path.is_file():
                    continue
                with data_path.open(encoding="utf-8") as handle:
                    for line in handle:
                        value = json.loads(line)
                        if (
                            value.get("item_id") in item_ids
                            or value.get("paired_item_id") in item_ids
                            or value.get("source_id") in source_ids
                        ):
                            rows.append(
                                {
                                    "export_id": manifest.get("export_id"),
                                    "kind": kind,
                                    "record": self._safe_value(value),
                                }
                            )
        return rows

    def _runs(
        self, record: dict[str, Any], stages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "run_id": run_id,
                "stages": [
                    item["stage_key"] for item in stages if item["run_id"] == run_id
                ],
            }
            for run_id in record["run_ids"]
        ]

    def _paper_state(
        self,
        candidates: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
        eligibility_state: str | None,
    ) -> str:
        candidate_states = {str(row.get("status")) for row in candidates}
        if "machine_accepted_unverified" in candidate_states:
            return "machine_accepted_unverified"
        if "incomplete_non_mcq" in candidate_states:
            return "incomplete_non_mcq"
        if "rejected" in candidate_states:
            return "generation_rejected"
        states = {str(row.get("state")) for row in receipts}
        if "submitted" in states or "response_received" in states:
            return "in_progress"
        if "ambiguous_charge" in states:
            return "ambiguous_charge"
        if eligibility_state is not None:
            return eligibility_state
        if receipts:
            latest = max(
                receipts,
                key=lambda row: str(
                    row.get("completed_at_utc") or row.get("submitted_at_utc") or ""
                ),
            )
            if (
                latest.get("stage") == "eligibility"
                and latest.get("state") == "completed"
            ):
                parsed = self._parse_model_text(latest.get("response")) or {}
                return str(parsed.get("overall") or "eligibility_completed")
            return str(latest.get("state") or "unknown")
        return "not_started"

    def _progress_titles(self) -> dict[str, str]:
        path = self.namespace / "streaming-dataset-r1" / "progress.json"
        value = self._read_json(path) if path.is_file() else {}
        return {
            str(row.get("paper_id")): str(row.get("title"))
            for row in (value.get("recent_papers") or [])
            if isinstance(row, dict) and row.get("paper_id") and row.get("title")
        }

    def _freshness(self) -> dict[str, Any]:
        progress_path = self.namespace / "streaming-dataset-r1" / "progress.json"
        progress = self._read_json(progress_path) if progress_path.is_file() else None
        ledger = (
            self._read_json(self.ledger_file) if self.ledger_file.is_file() else None
        )
        return {
            "pipeline": (
                {
                    "availability": "observed",
                    "state": progress.get("state"),
                    "run_id": progress.get("run_id"),
                    "current_stage": progress.get("current_stage"),
                    "updated_at_utc": progress.get("updated_at_utc"),
                }
                if isinstance(progress, dict)
                else {"availability": "not_retained"}
            ),
            "ledger": (
                {
                    "availability": "observed",
                    "state": "halted" if ledger.get("halted") else "idle",
                    "updated_at_utc": ledger.get("updated_at_utc"),
                    "inflight": ledger.get("inflight"),
                }
                if isinstance(ledger, dict)
                else {"availability": "not_retained"}
            ),
        }

    @staticmethod
    def _list_item(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "paper_key",
                "paper_id",
                "source_id",
                "family_id",
                "doi",
                "title",
                "run_ids",
                "state",
                "current_stage",
                "attempt_count",
                "latest_at_utc",
            )
        }

    def _call_by_id(self, call_id: str | None) -> dict[str, Any] | None:
        if not call_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return dict(row) if row else None

    def _safe_call(self, call: dict[str, Any] | None) -> dict[str, Any] | None:
        if call is None:
            return None
        safe = dict(call)
        safe["parameters"] = self._json_value(safe.pop("parameters_json"))
        safe["parsed_response"] = self._json_value(safe.pop("response_json"))
        return self._safe_value(safe)

    def _read_receipt_stem(self, stem: str) -> dict[str, Any] | None:
        if not _RECEIPT_STEM.fullmatch(stem):
            return None
        for suffix in (".json", ".received.json", ".submitted.json"):
            path = self.receipts_dir / f"{stem}{suffix}"
            if path.is_file():
                value = self._read_json(path)
                return value if isinstance(value, dict) else None
        return None

    def _request_trace_path(self, request_key: str) -> Path:
        return self.receipts_dir / f"{request_key}.request-trace.json"

    def _read_request_trace(self, request_key: str | None) -> dict[str, Any] | None:
        if not request_key or not _REQUEST_KEY.fullmatch(request_key):
            return None
        path = self._request_trace_path(request_key)
        value = self._read_json(path) if path.is_file() else None
        if (
            not isinstance(value, dict)
            or value.get("schema") != TRACE_SCHEMA
            or value.get("request_key") != request_key
        ):
            return None
        return value

    def _safe_receipt(self, receipt: dict[str, Any] | None) -> dict[str, Any] | None:
        if receipt is None:
            return None
        return self._safe_value(
            {key: value for key, value in receipt.items() if key != "response"}
        )

    @classmethod
    def _safe_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in _SENSITIVE_KEYS or normalized in _PATH_KEYS:
                    continue
                result[key] = cls._safe_value(item)
            return result
        if isinstance(value, list):
            return [cls._safe_value(item) for item in value]
        return value

    @staticmethod
    def _json_value(value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    @staticmethod
    def _model_text(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        candidates = response.get("candidates")
        if not isinstance(candidates, list):
            return None
        texts = []
        for candidate in candidates:
            parts = ((candidate or {}).get("content") or {}).get("parts") or []
            texts.extend(
                part["text"]
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        return "".join(texts) if texts else None

    @classmethod
    def _parse_model_text(cls, response: Any) -> Any:
        text = cls._model_text(response)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _duration_ms(started: Any, completed: Any) -> int | None:
        if not isinstance(started, str) or not isinstance(completed, str):
            return None
        try:
            start = datetime.fromisoformat(started.replace("Z", "+00:00"))
            end = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(0, round((end - start).total_seconds() * 1000))

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            padding = "=" * (-len(cursor) % 4)
            value = int(base64.urlsafe_b64decode(cursor + padding).decode())
        except (binascii.Error, ValueError, UnicodeDecodeError) as error:
            raise ValueError("invalid pipeline trace cursor") from error
        if value < 0:
            raise ValueError("invalid pipeline trace cursor")
        return value

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
