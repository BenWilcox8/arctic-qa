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
    "standalone_verification": "standalone_verifier",
    "blinded_reconstruction": "reconstructor",
    "answer_agreement": "answer_judge",
    "answer_verification": "answer_verifier",
    "distractor_generation": "distractor_writer",
    "option_verification": "option_verifier",
    "repair": "correction",
}


def _stored_ids(rows: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(row[key]) for row in rows if row.get(key)})


def _request_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold()
            if normalized == "request_id" or normalized.endswith("_request_id"):
                if nested:
                    found.add(str(nested))
            found.update(_request_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_request_ids(nested))
    return found


def _plain_reason(
    final_reason: str | None, state: str, current_stage: str
) -> dict[str, Any] | None:
    if not final_reason:
        return None
    reason = str(final_reason)
    check = reason.split(":", 1)[1] if ":" in reason else reason
    plain_check = check.replace("_", " ")
    if reason == "criterion_failed:published_primary_findings":
        return {
            "category": "eligibility_exclusion",
            "summary": "Excluded because the source was classified as a review or synthesis, not a primary research report.",
            "explanation": "The scientific eligibility check failed the primary-findings criterion. This is a retained model decision, not a new scientific judgment by the viewer.",
            "failed_stage": "scientific_eligibility",
            "failed_check": "published_primary_findings",
            "reason_code": reason,
        }
    if reason.startswith("criterion_failed:"):
        return {
            "category": "eligibility_exclusion",
            "summary": f"Excluded because the {plain_check} eligibility check failed.",
            "explanation": "The retained eligibility result marked this required criterion as failed.",
            "failed_stage": "scientific_eligibility",
            "failed_check": check,
            "reason_code": reason,
        }
    if reason.startswith(
        ("criterion_evidence_missing:", "criterion_missing_context_absent:")
    ):
        return {
            "category": "eligibility_unresolved",
            "summary": f"Eligibility is unresolved because evidence for {plain_check} is missing from the retained context.",
            "explanation": "Missing evidence does not prove that the study is out of scope. The pipeline kept this paper unresolved.",
            "failed_stage": "scientific_eligibility",
            "failed_check": check,
            "reason_code": reason,
        }
    if reason.startswith("criterion_unresolved:"):
        return {
            "category": "eligibility_unresolved",
            "summary": f"Eligibility is unresolved for the {plain_check} check.",
            "explanation": "The retained evidence did not support a final eligibility decision. This is not an exclusion.",
            "failed_stage": "scientific_eligibility",
            "failed_check": check,
            "reason_code": reason,
        }
    if state == "eligibility_unresolved":
        return {
            "category": "eligibility_unresolved",
            "summary": "Eligibility is unresolved because the retained evidence could not support a final decision.",
            "explanation": "This is not an exclusion. The recorded eligibility check needs more evidence or correction.",
            "failed_stage": "scientific_eligibility",
            "failed_check": reason,
            "reason_code": reason,
        }
    if "provider" in reason.casefold() and any(
        token in reason.casefold()
        for token in ("missing", "unavailable", "not_received", "no_output")
    ):
        return {
            "category": "provider_output_unresolved",
            "summary": "Processing is unresolved because the provider did not return a usable output.",
            "explanation": "This record does not show that the paper or QA item was rejected.",
            "failed_stage": current_stage,
            "failed_check": reason,
            "reason_code": reason,
        }
    if reason == "reconstruction_disagreement":
        return {
            "category": "qa_rejection",
            "summary": "The paper stayed eligible, but its generated question was rejected because the independent reconstruction did not agree with the proposed answer.",
            "explanation": "This rejection applies to the generated QA candidate. It does not exclude the source paper from the corpus.",
            "failed_stage": "automated_acceptance",
            "failed_check": reason,
            "reason_code": reason,
        }
    if "scope_not_source_bound" in reason or reason == "relation_scope_mismatch":
        return {
            "category": "qa_rejection",
            "summary": "The paper stayed eligible, but its generated question was rejected because the stated scope was not fully bound to the selected source passage.",
            "explanation": "This rejection applies to the generated QA candidate. It does not exclude the source paper from the corpus.",
            "failed_stage": "automated_acceptance",
            "failed_check": reason,
            "reason_code": reason,
        }
    if reason.endswith("_response_invalid") or any(
        token in reason.casefold()
        for token in ("malformed", "schema_invalid", "parse_error")
    ):
        invalid_stage = {
            "reconstructor": "blinded_reconstruction",
            "question_writer": "question_generation",
            "answer_verifier": "answer_verification",
            "distractor_writer": "distractor_generation",
            "option_verifier": "option_verification",
        }.get(reason.removesuffix("_response_invalid"), current_stage)
        return {
            "category": "invalid_model_response",
            "summary": "The paper stayed eligible, but this QA attempt was rejected because the model response was invalid.",
            "explanation": "The response failed the required structured-output contract. The viewer preserves the settled response for inspection.",
            "failed_stage": invalid_stage,
            "failed_check": reason,
            "reason_code": reason,
        }
    if any(
        token in reason.casefold() for token in ("distractor", "option_verification")
    ):
        return {
            "category": "distractor_rejection",
            "summary": "The paper stayed eligible, but the generated distractor set failed validation.",
            "explanation": "This rejection applies to the generated QA options. It does not exclude the source paper from the corpus.",
            "failed_stage": "option_verification",
            "failed_check": reason,
            "reason_code": reason,
        }
    if any(
        token in reason.casefold()
        for token in ("access", "source_unavailable", "source_missing")
    ):
        return {
            "category": "source_or_access_problem",
            "summary": "Processing could not continue because the required source or access evidence was unavailable.",
            "explanation": "Unavailable evidence is not an eligibility exclusion. The record remains separate until the source problem is resolved.",
            "failed_stage": current_stage,
            "failed_check": reason,
            "reason_code": reason,
        }
    if any(
        token in reason.casefold()
        for token in ("accounting", "budget", "ambiguous_charge", "infrastructure")
    ):
        return {
            "category": "infrastructure_or_accounting_stop",
            "summary": "Processing stopped because an infrastructure, budget, or accounting guard did not allow continuation.",
            "explanation": "This operational stop is not an eligibility or QA-quality decision.",
            "failed_stage": current_stage,
            "failed_check": reason,
            "reason_code": reason,
        }
    if state == "error" or reason in {"ValueError", "RuntimeError"}:
        return {
            "category": "processing_error",
            "summary": f"Processing stopped with {reason} during {current_stage.replace('_', ' ')}.",
            "explanation": "The progress record does not retain a more specific error message. This is a processing stop, not a scientific decision.",
            "failed_stage": current_stage,
            "failed_check": reason,
            "reason_code": reason,
        }
    return {
        "category": "qa_rejection"
        if state == "generation_rejected"
        else "recorded_exit",
        "summary": f"Processing ended with the retained reason: {plain_check}.",
        "explanation": "The exact reason code is preserved below.",
        "failed_stage": current_stage,
        "failed_check": reason,
        "reason_code": reason,
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
        eligibility_jobs = self._all_eligibility_jobs()
        records = self._paper_records(eligibility_jobs)
        scope_run_id = run_id or self.active_run_id()
        needle = (query or "").strip().casefold()
        filtered = []
        for record in records.values():
            if scope_run_id and scope_run_id not in record["run_ids"]:
                continue
            visible = (
                self._project_run(record, scope_run_id, eligibility_jobs)
                if scope_run_id
                else record
            )
            searchable = " ".join(
                str(visible.get(key) or "")
                for key in ("paper_id", "source_id", "doi", "title")
            ).casefold()
            if needle and needle not in searchable:
                continue
            if state and state != visible["state"]:
                continue
            if stage and stage not in visible["stages"]:
                continue
            filtered.append(visible)
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

    def paper_detail(self, paper_key: str, run_id: str | None = None) -> dict[str, Any]:
        eligibility_jobs = self._all_eligibility_jobs()
        record, scope_run_id = self._scoped_record(paper_key, run_id, eligibility_jobs)
        source_ids = record["source_ids"]
        sources = [self._source_detail(source_id) for source_id in source_ids]
        stages = self._stage_summaries(record)
        all_findings = self._table_json_rows("findings", "source_id", source_ids)
        all_candidates = self._table_json_rows("candidates", "source_id", source_ids)
        candidate_ids = {
            str(value) for value in _stored_ids(record["candidate_rows"], "item_id")
        }
        finding_ids = {
            str(value) for value in _stored_ids(record["finding_rows"], "finding_id")
        }
        candidates = [
            row for row in all_candidates if str(row.get("item_id")) in candidate_ids
        ]
        findings = [
            row for row in all_findings if str(row.get("finding_id")) in finding_ids
        ]
        rejections = self._rejection_rows(
            source_ids, record, eligibility_jobs, run_id=scope_run_id
        )
        eligibility = self._eligibility_jobs(
            record, eligibility_jobs, run_id=scope_run_id
        )
        validation_events = self._validation_rows(source_ids)
        if scope_run_id:
            validation_events = [
                event
                for event in validation_events
                if str(event.get("item_id")) in candidate_ids
            ]
        self._project_candidate_reasons(candidates, validation_events, rejections)
        self._project_exit_reasons(rejections)
        return {
            "schema": PAPER_SCHEMA,
            "generated_at_utc": _now(),
            "identity": self._list_item(record),
            "runs": self._runs(record, stages, eligibility_jobs),
            "sources": sources,
            "plain_reason": self._plain_reason_detail(
                record, eligibility, candidates, findings, rejections
            ),
            "findings": findings,
            "candidates": candidates,
            "validation_events": validation_events,
            "rejections": rejections,
            "eligibility": eligibility,
            "exports": self._export_rows(record, source_ids),
            "stages": stages,
        }

    def stage_payload(
        self, paper_key: str, stage_key: str, run_id: str | None = None
    ) -> dict[str, Any]:
        eligibility_jobs = self._all_eligibility_jobs()
        record, scope_run_id = self._scoped_record(paper_key, run_id, eligibility_jobs)
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
        candidate_ids = {
            str(value) for value in _stored_ids(record["candidate_rows"], "item_id")
        }
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
            "eligibility": self._eligibility_for_request(
                stage.get("request_key"), eligibility_jobs, run_id=scope_run_id
            ),
            "source_context": [
                self._source_context(source_id) for source_id in record["source_ids"]
            ],
            "candidates": [
                row
                for row in self._table_json_rows(
                    "candidates", "source_id", record["source_ids"]
                )
                if str(row.get("item_id")) in candidate_ids
            ],
        }

    def active_run_id(self) -> str | None:
        value = self._progress_snapshot().get("invocation_run_id")
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @staticmethod
    def _candidate_run_ids(
        candidate: dict[str, Any], receipts: list[dict[str, Any]]
    ) -> set[str]:
        run_ids = {str(candidate["run_id"])} if candidate.get("run_id") else set()
        request_ids = _request_ids(candidate.get("candidate"))
        for receipt in receipts:
            response_id = (receipt.get("response") or {}).get("responseId")
            if (
                response_id
                and str(response_id) in request_ids
                and receipt.get("run_id")
            ):
                run_ids.add(str(receipt["run_id"]))
        return run_ids

    def latest_run_counts(self) -> dict[str, int] | None:
        """Return paper-state counts for the active invocation, if one exists."""

        run_id = self.active_run_id()
        if not run_id:
            return None
        jobs = self._all_eligibility_jobs()
        records = self._paper_records(jobs)
        counts = {
            "accepted_qa": 0,
            "generation_rejected": 0,
            "incomplete_non_mcq": 0,
            "in_progress": 0,
            "eligibility_rejected": 0,
            "eligibility_unresolved": 0,
            "eligible": 0,
        }
        for record in records.values():
            if run_id not in record["run_ids"]:
                continue
            state = self._project_run(record, run_id, jobs)["state"]
            if state == "machine_accepted_unverified":
                counts["accepted_qa"] += 1
            elif state in counts:
                counts[state] += 1
        return counts

    def _scoped_record(
        self,
        paper_key: str,
        requested_run_id: str | None,
        eligibility_jobs: list[tuple[Path, dict[str, Any]]],
    ) -> tuple[dict[str, Any], str | None]:
        record = self._paper_records(eligibility_jobs).get(paper_key)
        if record is None:
            raise KeyError("unknown pipeline paper key")
        scope_run_id = requested_run_id or self.active_run_id()
        if scope_run_id:
            if scope_run_id not in record["run_ids"]:
                raise KeyError("unknown pipeline paper key")
            return self._project_run(
                record, scope_run_id, eligibility_jobs
            ), scope_run_id
        return record, None

    def _inside_namespace(self, path: Path) -> bool:
        return path == self.namespace or self.namespace in path.parents

    def _connect(self) -> sqlite3.Connection:
        if not self.db_file.is_file():
            raise FileNotFoundError("pipeline state database is unavailable")
        connection = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _paper_records(
        self, eligibility_jobs: list[tuple[Path, dict[str, Any]]]
    ) -> dict[str, dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        with self._connect() as connection:
            sources = [dict(row) for row in connection.execute("SELECT * FROM sources")]
            candidates = [
                dict(row)
                for row in connection.execute(
                    "SELECT item_id,source_id,run_id,status,updated_at,candidate_json FROM candidates"
                )
            ]
            findings = [
                dict(row)
                for row in connection.execute(
                    "SELECT finding_id,source_id,run_id,status,created_at FROM findings"
                )
            ]
        for row in candidates:
            row["candidate"] = self._json_value(row.pop("candidate_json"))
        source_by_family: dict[str, list[dict[str, Any]]] = {}
        for source in sources:
            source_by_family.setdefault(source["paper_family_id"], []).append(source)
        receipts = self._receipt_events()
        families = set(source_by_family) | {
            str(event.get("family_id")) for event in receipts if event.get("family_id")
        }
        progress = self._progress_snapshot()
        progress_titles = {
            str(row.get("paper_id")): str(row.get("title"))
            for row in progress.get("recent_papers", [])
            if row.get("paper_id") and row.get("title")
        }
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
            candidate_run_ids = {
                str(row["item_id"]): self._candidate_run_ids(row, family_receipts)
                for row in relevant_candidates
                if row.get("item_id")
            }
            finding_run_ids = {
                str(row["finding_id"]): {str(row["run_id"])}
                for row in relevant_findings
                if row.get("finding_id") and row.get("run_id")
            }
            for candidate in relevant_candidates:
                finding_id = (candidate.get("candidate") or {}).get("finding_id")
                if not finding_id:
                    continue
                finding_run_ids.setdefault(str(finding_id), set()).update(
                    candidate_run_ids.get(str(candidate.get("item_id")), set())
                )
            candidate_item_ids = _stored_ids(relevant_candidates, "item_id")
            finding_ids = _stored_ids(relevant_findings, "finding_id")
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
                    eligibility_jobs,
                ),
            )
            state_entered_at_utc = self._state_entered_at(
                state,
                relevant_candidates,
                family_receipts,
                {
                    str(item.get("request_key"))
                    for item in family_receipts
                    if item.get("request_key")
                },
                {str(value) for value in (*receipt_ids, *source_ids) if value},
                eligibility_jobs,
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
            record = {
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
                # Keep this legacy field as the retained-receipt count.
                "attempt_count": len(family_receipts),
                "model_call_count": len(family_receipts),
                "qa_candidate_count": len(candidate_item_ids),
                "finding_attempt_count": len(finding_ids),
                "latest_at_utc": max(latest_values, default=None),
                "state_entered_at_utc": state_entered_at_utc,
                "receipts": family_receipts,
                "candidate_rows": relevant_candidates,
                "finding_rows": relevant_findings,
                "_candidate_run_ids": candidate_run_ids,
                "_finding_run_ids": finding_run_ids,
            }
            progress_row = self._matching_progress_row(record, progress)
            if progress_row:
                invocation_run_id = progress.get("invocation_run_id")
                if invocation_run_id:
                    record["run_ids"] = sorted(
                        {*record["run_ids"], str(invocation_run_id)}
                    )
                record["progress_row"] = progress_row
                record["progress_snapshot"] = {
                    "run_id": progress.get("run_id"),
                    "invocation_run_id": progress.get("invocation_run_id"),
                }
                projection = self._progress_projection(progress_row)
                if projection["state"] != record["state"]:
                    projection["state_entered_at_utc"] = progress_row.get(
                        "state_changed_at_utc"
                    )
                elif progress_row.get("state_changed_at_utc"):
                    projection["state_entered_at_utc"] = progress_row[
                        "state_changed_at_utc"
                    ]
                record.update(projection)
            groups[key] = record
        return groups

    def _project_run(
        self,
        record: dict[str, Any],
        run_id: str,
        eligibility_jobs: list[tuple[Path, dict[str, Any]]],
    ) -> dict[str, Any]:
        receipts = [item for item in record["receipts"] if item.get("run_id") == run_id]
        candidates = [
            item
            for item in record["candidate_rows"]
            if run_id
            in record.get("_candidate_run_ids", {}).get(str(item.get("item_id")), set())
        ]
        findings = [
            item
            for item in record["finding_rows"]
            if run_id
            in record.get("_finding_run_ids", {}).get(
                str(item.get("finding_id")),
                {str(item.get("run_id"))} if item.get("run_id") else set(),
            )
        ]
        stages = sorted(
            {str(item.get("stage")) for item in receipts if item.get("stage")}
        )
        latest_receipt = max(
            receipts,
            key=lambda item: str(
                item.get("completed_at_utc") or item.get("submitted_at_utc") or ""
            ),
            default={},
        )
        latest_values = [
            str(value)
            for value in (
                *[
                    item.get("completed_at_utc") or item.get("submitted_at_utc")
                    for item in receipts
                ],
                *[item.get("updated_at") for item in candidates],
            )
            if value
        ]
        request_keys = {
            str(item.get("request_key")) for item in receipts if item.get("request_key")
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
        projected = {
            **record,
            "run_ids": [run_id],
            "state": self._paper_state(
                candidates,
                receipts,
                self._eligibility_state(
                    request_keys, candidate_ids, eligibility_jobs, run_id=run_id
                ),
                accepted_wins=True,
            ),
            "current_stage": (
                latest_receipt.get("stage")
                if latest_receipt
                else "completed"
                if candidates
                else "not_started"
            ),
            "stages": stages,
            # Keep this legacy field as the retained-receipt count.
            "attempt_count": len(receipts),
            "model_call_count": len(receipts),
            "qa_candidate_count": len(_stored_ids(candidates, "item_id")),
            "finding_attempt_count": len(_stored_ids(findings, "finding_id")),
            "latest_at_utc": max(latest_values, default=None),
            "state_entered_at_utc": self._state_entered_at(
                self._paper_state(
                    candidates,
                    receipts,
                    self._eligibility_state(
                        request_keys,
                        candidate_ids,
                        eligibility_jobs,
                        run_id=run_id,
                    ),
                    accepted_wins=True,
                ),
                candidates,
                receipts,
                request_keys,
                candidate_ids,
                eligibility_jobs,
                run_id=run_id,
            ),
            "receipts": receipts,
            "candidate_rows": candidates,
            "finding_rows": findings,
        }
        progress = record.get("progress_row")
        snapshot = record.get("progress_snapshot") or {}
        if progress and run_id in {
            snapshot.get("run_id"),
            snapshot.get("invocation_run_id"),
        }:
            projection = self._progress_projection(progress)
            if projected["state"] != "machine_accepted_unverified":
                if projection["state"] != "machine_accepted_unverified":
                    if projection["state"] != projected["state"]:
                        projection["state_entered_at_utc"] = progress.get(
                            "state_changed_at_utc"
                        )
                    elif progress.get("state_changed_at_utc"):
                        projection["state_entered_at_utc"] = progress[
                            "state_changed_at_utc"
                        ]
                    projected.update(projection)
            else:
                projected.pop("final_reason", None)
                projected.pop("reason", None)
        return projected

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
            transport_attempt = call.get("attempt") if call else None
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
                    # Keep attempt for API compatibility. It is a transport
                    # attempt only when the matching call journal is present.
                    "attempt": (
                        transport_attempt
                        if transport_attempt is not None
                        else attempts[request_key]
                    ),
                    "transport_attempt": transport_attempt,
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
        self,
        source_ids: list[str],
        record: dict[str, Any],
        eligibility_jobs: list[tuple[Path, dict[str, Any]]],
        *,
        run_id: str | None = None,
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
        if run_id:
            candidate_ids = {
                str(value) for value in _stored_ids(record["candidate_rows"], "item_id")
            }
            rows = [row for row in rows if str(row.get("item_id")) in candidate_ids]
        for row in rows:
            row["detail"] = self._json_value(row.pop("detail_json"))
        for job in self._eligibility_jobs(record, eligibility_jobs, run_id=run_id):
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

    @staticmethod
    def _reason_record(
        reason_code: Any,
        stage: Any,
        *,
        state: str = "generation_rejected",
        detail: Any = None,
        rejection_id: Any = None,
        created_at: Any = None,
    ) -> dict[str, Any]:
        code = str(reason_code) if reason_code else None
        recorded_stage = str(stage) if stage else "not_recorded"
        plain_reason = _plain_reason(code, state, recorded_stage) if code else None
        return {
            "stage": recorded_stage,
            "reason_code": code,
            "plain_reason": plain_reason,
            "detail": detail if isinstance(detail, dict) else {},
            "rejection_id": rejection_id,
            "created_at": created_at,
        }

    def _project_exit_reasons(self, rejections: list[dict[str, Any]]) -> None:
        """Add readable reasons to retained exits without changing their raw records."""

        for rejection in rejections:
            detail = rejection.get("detail")
            decision = detail.get("decision") if isinstance(detail, dict) else None
            state = (
                "eligibility_unresolved"
                if decision == "uncertain"
                else "generation_rejected"
            )
            rejection["reason"] = self._reason_record(
                rejection.get("reason_code"),
                rejection.get("stage"),
                state=state,
                detail=detail,
                rejection_id=rejection.get("rejection_id"),
                created_at=rejection.get("created_at"),
            )

    def _project_candidate_reasons(
        self,
        candidates: list[dict[str, Any]],
        validation_events: list[dict[str, Any]],
        rejections: list[dict[str, Any]],
    ) -> None:
        """Attach only current-payload validation results to their QA candidate."""

        ledger_by_item: dict[str, list[dict[str, Any]]] = {}
        for rejection in rejections:
            item_id = rejection.get("item_id")
            if item_id:
                ledger_by_item.setdefault(str(item_id), []).append(rejection)

        for wrapper in candidates:
            candidate = wrapper.get("candidate")
            item_id = wrapper.get("item_id")
            if not isinstance(candidate, dict) or not item_id:
                continue
            current_hash = stable_id("candidate-payload", canonical_json(candidate))
            bound_events = []
            for event in validation_events:
                if str(event.get("item_id")) != str(item_id):
                    continue
                details = event.get("details")
                if (
                    not isinstance(details, dict)
                    or details.get("candidate_hash") != current_hash
                ):
                    continue
                bound_events.append(event)

            wrapper["current_candidate_hash"] = current_hash
            wrapper["bound_validation_events"] = bound_events
            wrapper["rejection_reasons"] = []
            wrapper["rejection_reason_status"] = "not_rejected"
            if wrapper.get("status") == "rejected":
                wrapper["rejection_reason_status"] = "unrecorded"
                for event in bound_events:
                    if event.get("label") != "rejected":
                        continue
                    for code in event.get("reason_codes") or []:
                        ledger = next(
                            (
                                row
                                for row in ledger_by_item.get(str(item_id), [])
                                if row.get("stage") == event.get("stage")
                                and row.get("reason_code") == code
                            ),
                            None,
                        )
                        wrapper["rejection_reasons"].append(
                            self._reason_record(
                                code,
                                event.get("stage"),
                                detail=(ledger or {}).get(
                                    "detail", event.get("details")
                                ),
                                rejection_id=(ledger or {}).get("rejection_id"),
                                created_at=(ledger or {}).get(
                                    "created_at", event.get("created_at")
                                ),
                            )
                        )
                if wrapper["rejection_reasons"]:
                    wrapper["rejection_reason_status"] = "recorded"

            option_rejections = []
            for event in bound_events:
                details = event.get("details") or {}
                for distractor in details.get("distractors") or []:
                    if not isinstance(distractor, dict) or distractor.get("accepted"):
                        continue
                    option_rejections.append(
                        {
                            "option_text": distractor.get("text"),
                            "option_type": distractor.get("type"),
                            "stage": "option_validation",
                            "reason_codes": list(distractor.get("reasons") or []),
                            "detail": distractor,
                        }
                    )
            wrapper["distractor_rejections"] = option_rejections

    def _eligibility_jobs(
        self,
        record: dict[str, Any],
        eligibility_jobs: list[tuple[Path, dict[str, Any]]],
        *,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
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
        for path, job in eligibility_jobs:
            if run_id and not self._eligibility_job_belongs_to_run(
                path, job, run_id, request_keys
            ):
                continue
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
        self,
        request_key: str | None,
        eligibility_jobs: list[tuple[Path, dict[str, Any]]],
        *,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not request_key:
            return None
        for path, job in eligibility_jobs:
            if job.get("broker_request_key") == request_key and (
                not run_id
                or self._eligibility_job_belongs_to_run(
                    path, job, run_id, {request_key}
                )
            ):
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

    @staticmethod
    def _eligibility_job_belongs_to_run(
        path: Path,
        job: dict[str, Any],
        run_id: str,
        request_keys: set[str],
    ) -> bool:
        if str(job.get("run_id") or "") == run_id:
            return True
        if str(job.get("invocation_run_id") or "") == run_id:
            return True
        if str(job.get("broker_request_key") or "") in request_keys:
            return True
        return path.parent.parent.name == run_id

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
        self,
        request_keys: set[str],
        candidate_ids: set[str],
        eligibility_jobs: list[tuple[Path, dict[str, Any]]],
        *,
        run_id: str | None = None,
    ) -> str | None:
        latest = self._latest_eligibility_job(
            request_keys, candidate_ids, eligibility_jobs, run_id=run_id
        )
        if latest is None:
            return None
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

    def _latest_eligibility_job(
        self,
        request_keys: set[str],
        candidate_ids: set[str],
        eligibility_jobs: list[tuple[Path, dict[str, Any]]],
        *,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        matching = [
            (path, job)
            for path, job in eligibility_jobs
            if (
                job.get("broker_request_key") in request_keys
                if request_keys
                else str(job.get("candidate_key")) in candidate_ids
            )
            and (
                not run_id
                or self._eligibility_job_belongs_to_run(path, job, run_id, request_keys)
            )
        ]
        if not matching:
            return None
        return max(
            (job for _, job in matching),
            key=lambda job: str(job.get("completed_at_utc") or ""),
        )

    def _state_entered_at(
        self,
        state: str,
        candidates: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
        request_keys: set[str],
        candidate_ids: set[str],
        eligibility_jobs: list[tuple[Path, dict[str, Any]]],
        *,
        run_id: str | None = None,
    ) -> str | None:
        candidate_status = {
            "machine_accepted_unverified": "machine_accepted_unverified",
            "incomplete_non_mcq": "incomplete_non_mcq",
            "generation_rejected": "rejected",
        }.get(state)
        if candidate_status:
            return max(
                (
                    str(row.get("updated_at"))
                    for row in candidates
                    if row.get("status") == candidate_status and row.get("updated_at")
                ),
                default=None,
            )
        if state == "in_progress":
            return max(
                (
                    str(row.get("submitted_at_utc"))
                    for row in receipts
                    if row.get("state") in {"submitted", "response_received"}
                    and row.get("submitted_at_utc")
                ),
                default=None,
            )
        if state == "ambiguous_charge":
            return max(
                (
                    str(row.get("completed_at_utc") or row.get("submitted_at_utc"))
                    for row in receipts
                    if row.get("state") == "ambiguous_charge"
                ),
                default=None,
            )
        if state.startswith("eligibility_") or state == "eligible":
            job = self._latest_eligibility_job(
                request_keys, candidate_ids, eligibility_jobs, run_id=run_id
            )
            return (
                str(job.get("completed_at_utc"))
                if job and job.get("completed_at_utc")
                else None
            )
        if receipts:
            latest = max(
                receipts,
                key=lambda row: str(
                    row.get("completed_at_utc") or row.get("submitted_at_utc") or ""
                ),
            )
            value = latest.get("completed_at_utc") or latest.get("submitted_at_utc")
            return str(value) if value else None
        return None

    def _export_rows(
        self, record: dict[str, Any], source_ids: list[str]
    ) -> list[dict[str, Any]]:
        exports_dir = self.namespace / "exports"
        if not exports_dir.is_dir():
            return []
        item_ids = {
            row["item_id"]
            for row in record.get("candidate_rows", [])
            if row.get("item_id")
        }
        export_run_ids = set(record.get("run_ids", []))
        export_run_ids.update(
            str(row["run_id"])
            for row in record.get("candidate_rows", [])
            if row.get("run_id")
        )
        rows = []
        for manifest_path in sorted(exports_dir.glob("*/manifest.json")):
            manifest = self._read_json(manifest_path)
            if (
                not isinstance(manifest, dict)
                or manifest.get("run_id") not in export_run_ids
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
        self,
        record: dict[str, Any],
        stages: list[dict[str, Any]],
        eligibility_jobs: list[tuple[Path, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        runs = []
        for run_id in record["run_ids"]:
            projected = self._project_run(record, run_id, eligibility_jobs)
            runs.append(
                {
                    "run_id": run_id,
                    "state": projected["state"],
                    "current_stage": projected["current_stage"],
                    "attempt_count": projected["attempt_count"],
                    "model_call_count": projected["model_call_count"],
                    "qa_candidate_count": projected["qa_candidate_count"],
                    "finding_attempt_count": projected["finding_attempt_count"],
                    "latest_at_utc": projected["latest_at_utc"],
                    "candidate_item_ids": _stored_ids(
                        projected["candidate_rows"], "item_id"
                    ),
                    "finding_ids": _stored_ids(projected["finding_rows"], "finding_id"),
                    "stages": [
                        item["stage_key"] for item in stages if item["run_id"] == run_id
                    ],
                }
            )
        return runs

    def _plain_reason_detail(
        self,
        record: dict[str, Any],
        eligibility: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        rejections: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        reason = record.get("reason")
        if not reason:
            return None
        detail = dict(reason)
        codes = [str(reason["reason_code"])]
        codes.extend(
            str(row.get("reason_code")) for row in rejections if row.get("reason_code")
        )
        model_statements: list[dict[str, str]] = []
        evidence: list[dict[str, Any]] = []
        comparisons: list[dict[str, Any]] = []

        failed_check = reason.get("failed_check")
        for job in eligibility:
            validation = job.get("validation") or {}
            if str(reason.get("category", "")).startswith("eligibility_"):
                codes.extend(
                    str(value) for value in validation.get("overall_reason_codes") or []
                )
            criteria = (job.get("parsed_response") or {}).get("criteria") or []
            for criterion in criteria:
                if criterion.get("criterion_id") != failed_check:
                    continue
                criterion_codes = [
                    str(value) for value in criterion.get("reason_codes") or []
                ]
                codes.extend(criterion_codes)
                statement = (
                    f"The model marked {failed_check.replace('_', ' ')} as "
                    f"{criterion.get('status') or 'not resolved'}"
                )
                if criterion_codes:
                    plain_codes = [value.replace("_", " ") for value in criterion_codes]
                    statement += f". Model reason: {', '.join(plain_codes)}."
                else:
                    statement += "."
                model_statements.append(
                    {"label": "Eligibility model result", "text": statement}
                )
                missing = [
                    str(value) for value in criterion.get("missing_context") or []
                ]
                if missing:
                    model_statements.append(
                        {
                            "label": "Missing context reported by the model",
                            "text": ", ".join(missing),
                        }
                    )
            for group in validation.get("resolved_evidence") or []:
                if group.get("criterion") == failed_check:
                    evidence.extend(
                        {
                            "quote": span.get("quote"),
                            "locator": span.get("locator"),
                            "span_id": span.get("span_id"),
                            "start_byte": span.get("start_byte"),
                            "end_byte": span.get("end_byte"),
                        }
                        for span in group.get("spans") or []
                        if span.get("quote")
                    )

        for row in candidates:
            candidate = row.get("candidate") or {}
            codes.extend(str(value) for value in candidate.get("qa_gate_reasons") or [])
            answer = candidate.get("answer") or {}
            reconstruction = candidate.get("reconstruction") or {}
            verification = candidate.get("answer_verification") or {}
            standalone = candidate.get("standalone_verification") or {}
            agreement = candidate.get("answer_agreement") or {}
            if standalone.get("review_rationale"):
                model_statements.append(
                    {
                        "label": "Source-blind standalone verifier statement",
                        "text": str(standalone["review_rationale"]),
                    }
                )
            if standalone.get("unresolved_phrases"):
                model_statements.append(
                    {
                        "label": "Unresolved displayed phrases",
                        "text": ", ".join(
                            str(value) for value in standalone["unresolved_phrases"]
                        ),
                    }
                )
            if verification.get("verification_rationale"):
                model_statements.append(
                    {
                        "label": "Answer verifier statement",
                        "text": str(verification["verification_rationale"]),
                    }
                )
            if verification.get("residual_error"):
                model_statements.append(
                    {
                        "label": "Verifier error statement",
                        "text": str(verification["residual_error"]),
                    }
                )
            if answer.get("text") or reconstruction.get("answer"):
                comparison = {
                    "label": "Proposed answer compared with independent reconstruction",
                    "proposed_answer": answer.get("text"),
                    "reconstructed_answer": reconstruction.get("answer"),
                }
                if agreement:
                    comparison.update(
                        {
                            "agreement_method": agreement.get("method"),
                            "agreement_confidence_category": agreement.get(
                                "confidence_category"
                            ),
                            "deterministic_match": agreement.get("deterministic_match"),
                            "judge_result": (agreement.get("judge") or {}).get(
                                "verdict"
                            ),
                            "judge_model": (agreement.get("judge") or {}).get(
                                "requested_model"
                            ),
                        }
                    )
                comparisons.append(comparison)
            decision_evidence = candidate.get("decision_evidence") or []
            if decision_evidence:
                evidence.extend(
                    {
                        "quote": source.get("evidence_quote"),
                        "locator": source.get("locator"),
                        "span_id": source.get("source_span_id"),
                        "component_span_ids": source.get("source_span_ids"),
                        "roles": source.get("roles"),
                    }
                    for source in decision_evidence
                    if isinstance(source, dict) and source.get("evidence_quote")
                )
            else:
                source = answer if answer.get("evidence_quote") else reconstruction
                if source.get("evidence_quote"):
                    evidence.append(
                        {
                            "quote": source.get("evidence_quote"),
                            "locator": source.get("locator"),
                            "span_id": source.get("source_span_id"),
                        }
                    )

        if not model_statements and reason.get("category") in {
            "invalid_model_response",
            "processing_error",
        }:
            latest = max(
                record.get("receipts") or [],
                key=lambda row: str(
                    row.get("completed_at_utc") or row.get("submitted_at_utc") or ""
                ),
                default={},
            )
            model_text = self._model_text(latest.get("response"))
            if model_text:
                model_statements.append(
                    {"label": "Last retained model response", "text": model_text}
                )
                parsed = self._parse_model_text(latest.get("response")) or {}
                source_span_id = parsed.get("source_span_id")
                for finding in findings:
                    answer = finding.get("answer") or {}
                    if (
                        source_span_id
                        and answer.get("source_span_id") != source_span_id
                    ):
                        continue
                    if answer.get("evidence_quote"):
                        evidence.append(
                            {
                                "quote": answer.get("evidence_quote"),
                                "locator": answer.get("locator"),
                                "span_id": answer.get("source_span_id"),
                            }
                        )
                        break

        detail["reason_codes"] = list(dict.fromkeys(codes))
        detail["model_statements"] = model_statements
        detail["comparisons"] = comparisons
        detail["evidence"] = evidence
        if not evidence:
            detail["evidence_note"] = (
                "No source quote is retained for this failed check. "
                "Missing evidence is not proof that the paper is out of scope."
            )
        return self._safe_value(detail)

    def _paper_state(
        self,
        candidates: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
        eligibility_state: str | None,
        *,
        accepted_wins: bool = False,
    ) -> str:
        states = {str(row.get("state")) for row in receipts}
        candidate_states = {str(row.get("status")) for row in candidates}
        if accepted_wins and "machine_accepted_unverified" in candidate_states:
            return "machine_accepted_unverified"
        if "submitted" in states or "response_received" in states:
            return "in_progress"
        if "ambiguous_charge" in states:
            return "ambiguous_charge"
        if "machine_accepted_unverified" in candidate_states:
            return "machine_accepted_unverified"
        if "incomplete_non_mcq" in candidate_states:
            return "incomplete_non_mcq"
        if "rejected" in candidate_states:
            return "generation_rejected"
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

    def _progress_snapshot(self) -> dict[str, Any]:
        path = self.namespace / "streaming-dataset-r1" / "progress.json"
        value = self._read_json(path) if path.is_file() else {}
        if not isinstance(value, dict):
            return {}
        value["recent_papers"] = [
            row for row in (value.get("recent_papers") or []) if isinstance(row, dict)
        ]
        return value

    @staticmethod
    def _matching_progress_row(
        record: dict[str, Any], progress: dict[str, Any]
    ) -> dict[str, Any] | None:
        identifiers = {
            str(value).casefold()
            for value in (
                record.get("paper_id"),
                record.get("doi"),
                *record.get("source_ids", []),
            )
            if value
        }
        title = str(record.get("title") or "").strip().casefold()
        for row in reversed(progress.get("recent_papers", [])):
            paper_id = str(row.get("paper_id") or "").casefold()
            row_title = str(row.get("title") or "").strip().casefold()
            if paper_id in identifiers or (title and row_title == title):
                return dict(row)
        return None

    @staticmethod
    def _progress_projection(row: dict[str, Any]) -> dict[str, Any]:
        final_state = str(row.get("final_state") or "unknown")
        if final_state == "accepted":
            final_state = "machine_accepted_unverified"
        if final_state == "rejected" and str(row.get("final_reason") or "").startswith(
            "criterion_"
        ):
            final_state = "eligibility_rejected"
        if final_state == "unresolved":
            final_state = "eligibility_unresolved"
        current_stage = str(row.get("current_stage") or "not_started")
        reason = _plain_reason(row.get("final_reason"), final_state, current_stage)
        return {
            "state": final_state,
            "current_stage": current_stage,
            "final_reason": row.get("final_reason"),
            "reason": reason,
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
                    "invocation_run_id": progress.get("invocation_run_id"),
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
                "model_call_count",
                "qa_candidate_count",
                "finding_attempt_count",
                "latest_at_utc",
                "state_entered_at_utc",
                "final_reason",
                "reason",
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
