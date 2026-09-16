from __future__ import annotations

import argparse
import fcntl
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .broker_provider import ROLE_STAGES, _provider_result, _request_payload
from .db import Database, now
from .errors import CandidateRejectedError, ProviderError
from .exporting import export_run
from .generation import generate_candidate
from .gemini_eligibility import _config, _decimal, model_config_for_stage
from .model_broker import (
    SharedGeminiBroker,
    _normalized_usage,
    activate_exclusive_batch_mode,
    broker_request_key,
)
from .providers import ProviderResult, provider_prompt_hash
from .publication_export import export_publication_package
from .streaming import (
    _import_source,
    _is_current_contract_candidate,
    _progress_generation,
    _run_eligibility,
    _validate_brokered_eligibility,
    _validate_pair,
)
from .util import (
    atomic_json,
    atomic_write,
    canonical_json,
    jsonl_bytes,
    sha256_bytes,
    sha256_file,
    stable_id,
)
from .validation import validate_candidate


BATCH_SCHEMA = "arctic-gemini-batch-state-v1"
REQUEST_SCHEMA = "arctic-gemini-batch-request-v1"
ROUND_SCHEMA = "arctic-gemini-batch-round-v1"
AUTHORIZATION_SCHEMA = "arctic-gemini-batch-submit-authorization-v1"
DEFAULT_BATCH_ALLOCATION_USD = Decimal("25")
MODEL = "gemini-3.8-flash"
BATCH_INPUT_USD_PER_MILLION = Decimal("0.375")
BATCH_OUTPUT_USD_PER_MILLION = Decimal("1.875")
PRICING_VALID_THROUGH = "2026-12-31"
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
# Batch mode is half the standard price, per PRICING_SOURCE (checked
# 2026-09-15). The agreement judge model is pinned per price config revision:
# flash-lite through v7, the Pro judge from v8 (chapter 3, yield audit 4.5 R5).
BATCH_AGREEMENT_PRICE_RECORDS = {
    "gemini-3.1-flash-lite": ("0.125", "0.75"),
    "gemini-3.1-pro-preview": ("1.00", "6.00"),
}
MODEL_SOURCE = "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash"
BATCH_SOURCE = "https://ai.google.dev/gemini-api/docs/batch-api"
TERMINAL_JOB_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
}
CONTINUATION_SCHEMA = "arctic-gemini-batch-continuation-v1"


class BatchPendingError(ProviderError):
    code = "BATCH_PENDING"


class BatchTransport(Protocol):
    def upload(self, path: Path, display_name: str) -> dict[str, Any]: ...

    def create(
        self, model: str, file_name: str, display_name: str
    ) -> dict[str, Any]: ...

    def get(self, job_name: str) -> dict[str, Any]: ...

    def download(self, file_name: str) -> bytes: ...


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected one JSON object: {path}")
    return value


def _money(value: Any, name: str, *, positive: bool = False) -> Decimal:
    return _decimal(value, name, positive=positive)


def _cost(input_tokens: int, output_tokens: int) -> Decimal:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (input_tokens, output_tokens)
    ):
        raise ValueError("batch token counts must be nonnegative integers")
    return (
        Decimal(input_tokens) * BATCH_INPUT_USD_PER_MILLION
        + Decimal(output_tokens) * BATCH_OUTPUT_USD_PER_MILLION
    ) / Decimal("1000000")


def _batch_pricing(config: dict[str, Any]) -> dict[str, str]:
    return {
        "input_usd_per_million_tokens": str(
            Decimal(config["input_usd_per_million_tokens"]) / 2
        ),
        "output_usd_per_million_tokens_including_thinking": str(
            Decimal(config["output_usd_per_million_tokens_including_thinking"]) / 2
        ),
        "valid_through": str(config["price_valid_through"]),
        "source": str(config["price_source"]),
    }


def _priced_cost(
    pricing: dict[str, Any], input_tokens: int, output_tokens: int
) -> Decimal:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (input_tokens, output_tokens)
    ):
        raise ValueError("batch token counts must be nonnegative integers")
    return (
        Decimal(input_tokens) * Decimal(pricing["input_usd_per_million_tokens"])
        + Decimal(output_tokens)
        * Decimal(pricing["output_usd_per_million_tokens_including_thinking"])
    ) / Decimal("1000000")


def _validate_batch_shared_ledger(
    shared_ledger_file: Path, ledger: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Validate shared paid-call custody before a batch uses its budget.

    The live broker owns the continuation evidence rules. This read-only
    guard reuses those rules and permits only reviewed no-replay holds, whose
    full reservations remain in the shared totals and therefore in all batch
    projections. Every other unresolved request is a submission stop.
    """
    integrity_file = shared_ledger_file.with_name(
        f".{shared_ledger_file.name}.integrity-halt.json"
    )
    if integrity_file.exists():
        raise ValueError("the shared paid-call ledger has an integrity halt")
    try:
        liabilities = SharedGeminiBroker.validate_no_replay_liabilities(
            ledger=ledger,
            receipts_dir=shared_ledger_file.parent / "model-receipts",
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "the shared paid-call ledger has an unaccounted retained liability "
            f"or failed recovery validation: {error}"
        ) from error
    try:
        reserved_expected = sum(
            (
                _money(
                    request.get("reserved_usd"), "retained reservation", positive=True
                )
                for request in ledger["requests"].values()
                if request.get("state") == "orphaned_no_replay"
            ),
            Decimal("0"),
        )
        ambiguous_expected = sum(
            (
                _money(
                    request.get("reserved_usd"), "ambiguous reservation", positive=True
                )
                for request in ledger["requests"].values()
                if request.get("state") == "ambiguous_charge"
            ),
            Decimal("0"),
        )
        reserved_actual = _money(ledger.get("reserved_usd"), "shared ledger reserved")
        ambiguous_actual = _money(
            ledger.get("ambiguous_reserved_usd"), "shared ledger ambiguous"
        )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "the shared paid-call ledger has an unaccounted retained liability "
            f"or failed accounting validation: {error}"
        ) from error
    if reserved_actual != reserved_expected or ambiguous_actual != ambiguous_expected:
        raise ValueError(
            "the shared paid-call ledger has an unaccounted retained liability; "
            "recovery holds must remain fully accounted for"
        )
    if ledger.get("halted") is not False:
        raise ValueError("the shared paid-call ledger is halted")
    inflight = ledger.get("inflight")
    if isinstance(inflight, bool) or not isinstance(inflight, int) or inflight != 0:
        raise ValueError("the shared paid-call producer still has inflight work")
    for request_key, request in ledger["requests"].items():
        state = request.get("state")
        if state == "submitted" or state == "counting":
            raise ValueError(
                "the shared paid-call ledger has an unreviewed submitted liability"
            )
        if (
            state in {"orphaned_no_replay", "ambiguous_charge"}
            and request_key not in liabilities
        ):
            raise ValueError(
                "the shared paid-call ledger has a retained liability without "
                "validated no-replay recovery evidence"
            )
    return liabilities


class BatchStore:
    def __init__(
        self,
        root: Path,
        *,
        price_config_file: Path,
        shared_ledger_file: Path,
        overall_ceiling_usd: Decimal,
        maximum_request_usd: Decimal,
        maximum_paper_usd: Decimal,
    ) -> None:
        self.root = root.resolve()
        self.price_config_file = price_config_file.resolve()
        self.shared_ledger_file = shared_ledger_file.resolve()
        self.overall_ceiling_usd = overall_ceiling_usd
        self.maximum_request_usd = maximum_request_usd
        self.maximum_paper_usd = maximum_paper_usd
        self.config = _config(self.price_config_file)
        if self.config["model"] != MODEL:
            raise ValueError(
                "the configured model does not have batch support evidence"
            )
        if (
            Decimal(self.config["input_usd_per_million_tokens"])
            != BATCH_INPUT_USD_PER_MILLION * 2
        ):
            raise ValueError(
                "the standard input price does not match the batch price record"
            )
        if (
            Decimal(self.config["output_usd_per_million_tokens_including_thinking"])
            != BATCH_OUTPUT_USD_PER_MILLION * 2
        ):
            raise ValueError(
                "the standard output price does not match the batch price record"
            )
        # The reviewed price config pins the agreement model per revision
        # (flash-lite through v7, the Pro judge from v8); the batch evidence
        # is the documented method list bound in that record.
        agreement_config = model_config_for_stage(self.config, "answer_agreement")
        if "batchGenerateContent" not in agreement_config.get(
            "documented_supported_methods", []
        ):
            raise ValueError(
                "the configured answer agreement model lacks batch support evidence"
            )
        agreement_record = BATCH_AGREEMENT_PRICE_RECORDS.get(
            str(agreement_config["model"])
        )
        if agreement_record is None:
            raise ValueError(
                "the configured answer agreement model lacks batch support evidence"
            )
        if _batch_pricing(agreement_config) != {
            "input_usd_per_million_tokens": agreement_record[0],
            "output_usd_per_million_tokens_including_thinking": agreement_record[1],
            "valid_through": agreement_config["price_valid_through"],
            "source": PRICING_SOURCE,
        }:
            raise ValueError("the answer agreement batch price record changed")
        if (
            self.overall_ceiling_usd <= 0
            or self.maximum_request_usd <= 0
            or self.maximum_paper_usd <= 0
        ):
            raise ValueError("batch budget limits must be positive")
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("requests", "receipts", "rounds", "raw"):
            (self.root / name).mkdir(exist_ok=True)
        self._initialize()

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.root / ".state.lock"

    @property
    def batch_identity(self) -> str:
        return sha256_bytes(
            canonical_json(
                {
                    "state_dir": str(self.root),
                    "shared_ledger_file": str(self.shared_ledger_file),
                    "price_config_sha256": sha256_file(self.price_config_file),
                }
            ).encode()
        )

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema": BATCH_SCHEMA,
            "model": MODEL,
            "price_config_file": str(self.price_config_file),
            "price_config_sha256": sha256_file(self.price_config_file),
            "pricing": {
                "input_usd_per_million_tokens": str(BATCH_INPUT_USD_PER_MILLION),
                "output_usd_per_million_tokens_including_thinking": str(
                    BATCH_OUTPUT_USD_PER_MILLION
                ),
                "valid_through": PRICING_VALID_THROUGH,
                "source": PRICING_SOURCE,
            },
            "registered_models": {
                "default": {
                    "model": MODEL,
                    "pricing": _batch_pricing(self.config),
                    "model_source": self.config["model_source"],
                },
                "answer_agreement": {
                    "model": model_config_for_stage(self.config, "answer_agreement")[
                        "model"
                    ],
                    "pricing": _batch_pricing(
                        model_config_for_stage(self.config, "answer_agreement")
                    ),
                    "model_source": model_config_for_stage(
                        self.config, "answer_agreement"
                    )["model_source"],
                },
            },
            "shared_ledger_file": str(self.shared_ledger_file),
            "overall_ceiling_usd": str(self.overall_ceiling_usd),
            "maximum_request_usd": str(self.maximum_request_usd),
            "maximum_paper_usd": str(self.maximum_paper_usd),
            "requests": {},
            "rounds": {},
            "created_at_utc": _utc_now(),
            "updated_at_utc": _utc_now(),
        }

    def _initialize(self) -> None:
        with self.lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.state_file.exists():
                atomic_json(self.state_file, self._initial_state())
            self._validate_state(_read(self.state_file))

    def _validate_state(self, state: dict[str, Any]) -> None:
        if state.get("schema") != BATCH_SCHEMA or state.get("model") != MODEL:
            raise ValueError("the batch state identity changed")
        expected = {
            "price_config_file": str(self.price_config_file),
            "price_config_sha256": sha256_file(self.price_config_file),
            "shared_ledger_file": str(self.shared_ledger_file),
            "overall_ceiling_usd": str(self.overall_ceiling_usd),
            "maximum_request_usd": str(self.maximum_request_usd),
            "maximum_paper_usd": str(self.maximum_paper_usd),
        }
        if any(state.get(key) != value for key, value in expected.items()):
            raise ValueError("the batch state configuration changed")
        if not isinstance(state.get("requests"), dict) or not isinstance(
            state.get("rounds"), dict
        ):
            raise ValueError("the batch state mappings are invalid")
        for key, record in state["requests"].items():
            if (
                not re.fullmatch(r"[a-f0-9]{64}", key)
                or record.get("request_key") != key
            ):
                raise ValueError("a batch request identity changed")
            request_file = self.root / record["request_file"]
            if (
                not request_file.is_file()
                or sha256_file(request_file) != record["request_file_sha256"]
            ):
                raise ValueError("a batch request record changed")
        pricing = state.get("pricing") or {}
        if pricing != self._initial_state()["pricing"]:
            raise ValueError("the batch pricing record changed")
        if state.get("registered_models") != self._initial_state()["registered_models"]:
            raise ValueError("the batch model registry changed")

    def read(self) -> dict[str, Any]:
        with self.lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = _read(self.state_file)
            self._validate_state(state)
            return state

    def update(self, mutate: Any) -> dict[str, Any]:
        with self.lock_file.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = _read(self.state_file)
            self._validate_state(state)
            mutate(state)
            state["updated_at_utc"] = _utc_now()
            self._validate_state(state)
            atomic_json(self.state_file, state)
            return state

    def shared_ledger(self) -> dict[str, Any]:
        ledger = _read(self.shared_ledger_file)
        if ledger.get("schema") != "shared-paid-call-ledger-v1" or not isinstance(
            ledger.get("requests"), dict
        ):
            raise ValueError("the shared paid-call ledger is invalid")
        _validate_batch_shared_ledger(self.shared_ledger_file, ledger)
        for name in (
            "prior_construction_spend_usd",
            "spent_usd",
            "reserved_usd",
            "ambiguous_reserved_usd",
        ):
            _money(ledger.get(name), f"shared ledger {name}")
        return ledger

    def request_path(self, request_key: str) -> Path:
        return self.root / "requests" / f"{request_key}.json"

    def receipt_path(self, request_key: str) -> Path:
        return self.root / "receipts" / f"{request_key}.json"

    def prepare_request(self, record: dict[str, Any]) -> dict[str, Any]:
        key = record["request_key"]
        path = self.request_path(key)
        atomic_json(path, record, immutable=True)

        def mutate(state: dict[str, Any]) -> None:
            existing = state["requests"].get(key)
            summary = {
                "request_key": key,
                "request_file": str(path.relative_to(self.root)),
                "request_file_sha256": sha256_file(path),
                "stage": record["stage"],
                "role": record["role"],
                "paper_id": record["paper_id"],
                "family_id": record["family_id"],
                "source_version_id": record["source_version_id"],
                "attempt": record["attempt"],
                "reserved_usd": record["reserved_usd"],
                "state": "prepared",
                "round_id": None,
                "receipt_sha256": None,
                "actual_cost_usd": None,
            }
            if existing is None:
                state["requests"][key] = summary
            elif any(
                existing.get(name) != summary[name]
                for name in summary
                if name
                not in {"state", "round_id", "receipt_sha256", "actual_cost_usd"}
            ):
                raise ValueError("the existing batch request identity changed")

        return self.update(mutate)["requests"][key]

    def prepared_record(self, request_key: str) -> dict[str, Any]:
        state = self.read()
        summary = state["requests"].get(request_key)
        if summary is None:
            raise KeyError(request_key)
        return _read(self.root / summary["request_file"])

    def make_round(
        self, *, run_identity: dict[str, Any], ordered_inputs: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        state = self.read()
        keys = [
            key
            for key, record in state["requests"].items()
            if record["state"] == "prepared" and record["round_id"] is None
        ]
        if not keys:
            return None
        positions = {
            item["paper_id"]: index for index, item in enumerate(ordered_inputs)
        }
        keys.sort(
            key=lambda key: (
                positions[state["requests"][key]["paper_id"]],
                state["requests"][key]["stage"],
                key,
            )
        )
        first_model = self.prepared_record(keys[0])["model"]
        keys = [
            key for key in keys if self.prepared_record(key)["model"] == first_model
        ]
        lines = []
        total = Decimal("0")
        stages: dict[str, int] = {}
        for key in keys:
            request = self.prepared_record(key)
            lines.append({"key": key, "request": request["request"]})
            total += Decimal(request["reserved_usd"])
            stages[request["stage"]] = stages.get(request["stage"], 0) + 1
        pricing_records = {
            canonical_json(self.prepared_record(key)["batch_pricing"]) for key in keys
        }
        model_sources = {self.prepared_record(key)["model_source"] for key in keys}
        if len(pricing_records) != 1 or len(model_sources) != 1:
            raise ValueError("one batch round must use one model price record")
        round_pricing = json.loads(next(iter(pricing_records)))
        round_id = stable_id("batch-round", run_identity, keys, length=32)
        directory = self.root / "rounds" / round_id
        requests_file = directory / "requests.jsonl"
        atomic_write(requests_file, jsonl_bytes(lines), immutable=True)
        budget_preview = self.budget_preview(keys)
        manifest = {
            "schema": ROUND_SCHEMA,
            "round_id": round_id,
            "run_identity": run_identity,
            "ordered_inputs": ordered_inputs,
            "request_keys": keys,
            "request_count": len(keys),
            "stage_counts": stages,
            "reserved_cost_usd": str(total),
            "budget_preview": budget_preview,
            "shared_ledger_file": str(self.shared_ledger_file),
            "shared_ledger_sha256_at_prepare": sha256_file(self.shared_ledger_file),
            "requests_file": str(requests_file),
            "requests_file_sha256": sha256_file(requests_file),
            "model": first_model,
            "pricing": round_pricing,
            "price_config_sha256": sha256_file(self.price_config_file),
            "pricing_source": PRICING_SOURCE,
            "model_source": next(iter(model_sources)),
            "batch_source": BATCH_SOURCE,
            "submission_enabled": False,
        }
        manifest_path = directory / "manifest.json"
        atomic_json(manifest_path, manifest, immutable=True)

        def mutate(value: dict[str, Any]) -> None:
            current = value["rounds"].get(round_id)
            summary = {
                "round_id": round_id,
                "manifest": str(manifest_path.relative_to(self.root)),
                "manifest_sha256": sha256_file(manifest_path),
                "request_keys": keys,
                "state": "prepared",
                "job_name": None,
                "reserved_usd": "0",
                "actual_cost_usd": "0",
                "missing_keys": keys,
            }
            if current is not None and current != summary:
                raise ValueError("the existing batch round changed")
            value["rounds"][round_id] = summary
            for key in keys:
                value["requests"][key]["round_id"] = round_id

        self.update(mutate)
        return {**manifest, "manifest_path": str(manifest_path)}

    def budget_preview(self, request_keys: list[str]) -> dict[str, Any]:
        state = self.read()
        ledger = self.shared_ledger()
        shared_used = sum(
            _money(ledger[name], name)
            for name in (
                "prior_construction_spend_usd",
                "spent_usd",
                "reserved_usd",
                "ambiguous_reserved_usd",
            )
        )
        batch_actual = sum(
            Decimal(str(row.get("actual_cost_usd") or "0"))
            for row in state["requests"].values()
        )
        batch_liability = sum(
            Decimal(row["reserved_usd"])
            for row in state["requests"].values()
            if row["state"]
            in {"submitted", "submission_ambiguous", "missing", "error_unsettled"}
        )
        new_reservation = sum(
            Decimal(state["requests"][key]["reserved_usd"]) for key in request_keys
        )
        return {
            "shared_used_usd": str(shared_used),
            "batch_actual_usd": str(batch_actual),
            "batch_unsettled_liability_usd": str(batch_liability),
            "new_reservation_usd": str(new_reservation),
            "projected_batch_total_usd": str(
                batch_actual + batch_liability + new_reservation
            ),
            "projected_total_usd": str(
                shared_used + batch_actual + batch_liability + new_reservation
            ),
            "batch_allocation_usd": str(self.overall_ceiling_usd),
            "overall_ceiling_usd": str(self.overall_ceiling_usd),
        }

    def authorize_round(
        self, manifest: dict[str, Any], authorization: dict[str, Any]
    ) -> dict[str, Any]:
        if manifest.get("run_identity", {}).get("continuation_plan_provisional"):
            raise ValueError("a provisional continuation plan cannot be submitted")
        round_id = manifest["round_id"]
        state = self.read()
        round_state = state["rounds"].get(round_id)
        if round_state is None or round_state["state"] != "prepared":
            raise ValueError("the batch round is not ready for submission")
        if authorization.get("schema") != AUTHORIZATION_SCHEMA:
            raise ValueError("the batch submission authorization schema is invalid")
        expected = {
            "round_manifest_sha256": sha256_bytes(
                (canonical_json(manifest) + "\n").encode()
            ),
            "shared_ledger_sha256": sha256_file(self.shared_ledger_file),
            "maximum_reserved_cost_usd": manifest["reserved_cost_usd"],
        }
        if any(authorization.get(key) != value for key, value in expected.items()):
            raise ValueError("the batch submission authorization does not match")
        if not str(authorization.get("authorized_by") or "").strip():
            raise ValueError("the batch submission authorization lacks an author")
        ledger = self.shared_ledger()
        if ledger.get("halted") or ledger.get("inflight") != 0:
            raise ValueError("the shared paid-call producer has not stopped")
        preview = self.budget_preview(manifest["request_keys"])
        if Decimal(preview["projected_batch_total_usd"]) > self.overall_ceiling_usd:
            raise ValueError(
                "the batch reservation exceeds the batch allocation construction ceiling"
            )
        paper_costs: dict[str, Decimal] = {}
        for paper in ledger.get("papers", {}).values():
            family_id = next(
                (key for key, value in ledger["papers"].items() if value is paper), ""
            )
            paper_costs[family_id] = sum(
                _money(paper.get(name, 0), name)
                for name in ("spent_usd", "reserved_usd", "ambiguous_usd")
            )
        for record in state["requests"].values():
            paper_costs[record["family_id"]] = paper_costs.get(
                record["family_id"], Decimal("0")
            ) + Decimal(record.get("actual_cost_usd") or "0")
            if record["state"] in {
                "submitted",
                "submission_ambiguous",
                "missing",
                "error_unsettled",
            }:
                paper_costs[record["family_id"]] += Decimal(record["reserved_usd"])
        for key in manifest["request_keys"]:
            record = state["requests"][key]
            reserved = Decimal(record["reserved_usd"])
            if reserved > self.maximum_request_usd:
                raise ValueError("a batch request exceeds the request cost limit")
            paper_costs[record["family_id"]] = (
                paper_costs.get(record["family_id"], Decimal("0")) + reserved
            )
            if paper_costs[record["family_id"]] > self.maximum_paper_usd:
                raise ValueError("a batch request exceeds the paper cost limit")
        return preview

    def activate_exclusive_mode(self, expected_ledger_sha256: str) -> dict[str, Any]:
        return activate_exclusive_batch_mode(
            self.shared_ledger_file,
            batch_identity=self.batch_identity,
            batch_state_file=self.state_file,
            expected_ledger_sha256=expected_ledger_sha256,
        )

    def mark_submitting(self, round_id: str) -> None:
        def mutate(state: dict[str, Any]) -> None:
            row = state["rounds"][round_id]
            if row["state"] != "prepared":
                raise ValueError("the batch round cannot be submitted again")
            row["state"] = "submitting"
            row["reserved_usd"] = str(
                sum(
                    Decimal(state["requests"][key]["reserved_usd"])
                    for key in row["request_keys"]
                )
            )
            for key in row["request_keys"]:
                state["requests"][key]["state"] = "submission_ambiguous"

        self.update(mutate)

    def mark_submitted(
        self, round_id: str, job_name: str, provider_file_name: str
    ) -> None:
        if not re.fullmatch(r"batches/[A-Za-z0-9._/-]+", job_name):
            raise ValueError("the provider batch job name is invalid")

        def mutate(state: dict[str, Any]) -> None:
            row = state["rounds"][round_id]
            if row["state"] not in {"submitting", "submission_ambiguous"}:
                raise ValueError("the batch round is not awaiting a job identity")
            row["state"] = "submitted"
            row["job_name"] = job_name
            row["provider_file_name"] = provider_file_name
            for key in row["request_keys"]:
                if state["requests"][key]["state"] == "submission_ambiguous":
                    state["requests"][key]["state"] = "submitted"

        self.update(mutate)


@dataclass(frozen=True)
class BatchProvider:
    store: BatchStore
    phase: str
    invocation_run_id: str
    paper_id: str | None = None
    family_id: str | None = None
    source_version_id: str | None = None
    capture_option_requests: bool = False

    name = "gemini"
    externally_metered = True

    @property
    def model(self) -> str:
        return MODEL

    def model_for_role(self, role: str) -> str:
        stage = ROLE_STAGES.get(role)
        if stage is None:
            return self.model
        return str(model_config_for_stage(self.config, stage)["model"])

    @property
    def broker(self) -> BatchProvider:
        return self

    @property
    def config(self) -> dict[str, Any]:
        return self.store.config

    def bind(
        self, *, paper_id: str, family_id: str, source_version_id: str
    ) -> BatchProvider:
        if not all((paper_id, family_id, source_version_id)):
            raise ValueError("the batch provider paper identity is incomplete")
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

    def invoke(
        self,
        role: str,
        system: str,
        prompt: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> ProviderResult:
        if not all((self.paper_id, self.family_id, self.source_version_id)):
            raise ValueError("the batch provider is not bound to a paper")
        stage = ROLE_STAGES.get(role)
        if stage is None:
            raise ValueError(f"the generation role has no batch stage: {role}")
        request_config = model_config_for_stage(self.config, stage)
        model = str(request_config["model"])
        payload = _request_payload(system, prompt, parameters, request_config)
        key = broker_request_key(
            model=model,
            run_id=self.invocation_run_id,
            phase=self.phase,
            stage=stage,
            paper_id=str(self.paper_id),
            family_id=str(self.family_id),
            source_version_id=str(self.source_version_id),
            payload=payload,
        )
        state = self.store.read()
        summary = state["requests"].get(key)
        if summary and summary["state"] == "completed":
            return _provider_result(
                _read(self.store.receipt_path(key)),
                model,
                allow_enum=role == "answer_judge",
            )
        if summary and summary["state"] in {"error_unsettled", "submission_ambiguous"}:
            raise ProviderError(
                f"batch request {key} has unresolved provider liability"
            )
        if summary is None:
            shared = self.store.shared_ledger().get("requests", {}).get(key)
            if shared is not None:
                raise ProviderError(
                    f"batch request {key} already exists in the shared streaming ledger"
                )
            input_bound = len(canonical_json(payload).encode("utf-8"))
            output_bound = int(payload["generationConfig"]["maxOutputTokens"])
            batch_pricing = _batch_pricing(request_config)
            reserved = _priced_cost(batch_pricing, input_bound, output_bound)
            record = {
                "schema": REQUEST_SCHEMA,
                "request_key": key,
                "request_sha256": sha256_bytes(canonical_json(payload).encode()),
                "run_id": self.invocation_run_id,
                "phase": self.phase,
                "stage": stage,
                "role": role,
                "paper_id": self.paper_id,
                "family_id": self.family_id,
                "source_version_id": self.source_version_id,
                "attempt": 1,
                "prompt_sha256": sha256_bytes(prompt.encode()),
                "prompt_identity": provider_prompt_hash(
                    self,
                    system,
                    prompt,
                    "arctic-qa-batch-v1",
                    parameters,
                    role=role,
                ),
                "model": model,
                "model_source": request_config["model_source"],
                "config_id": request_config["config_id"],
                "price_config_sha256": sha256_file(self.store.price_config_file),
                "batch_pricing": batch_pricing,
                "maximum_input_tokens": request_config["maximum_input_tokens"],
                "maximum_output_tokens_including_thinking": output_bound,
                "input_token_bound": input_bound,
                "reserved_usd": str(reserved),
                "request": payload,
            }
            self.store.prepare_request(record)
        if role == "option_verifier" and self.capture_option_requests:
            span_match = re.search(r'"span_id":"([^"]+)"', prompt)
            if span_match is None:
                raise ValueError("the option request has no source span")
            # The capture placeholder rejects the option, so rank-order
            # verification continues and every option request of the round
            # is prepared at once. The whole-set request is prepared in the
            # round after the option receipts are read, because its prompt
            # depends on which options verified.
            return ProviderResult(
                payload={
                    "rationale": "Temporary offline request capture record.",
                    "admitting_interpretation": "",
                    "contradiction_established": False,
                    "option_standalone_interpretable": False,
                    "question_admits_option_as_correct": False,
                    "source_span_id": span_match.group(1),
                },
                returned_model=model,
                request_id=f"capture-{key}",
                input_tokens=0,
                output_tokens=0,
                actual_cost_usd=Decimal("0"),
            )
        raise BatchPendingError(f"batch request {key} is prepared and pending")

    def read_receipt(
        self, *, request_key: str, role: str, request_sha256: str | None = None
    ) -> tuple[dict[str, Any], ProviderResult]:
        receipt = _read(self.store.receipt_path(request_key))
        if (
            receipt.get("state") != "completed"
            or receipt.get("request_key") != request_key
        ):
            raise ValueError("the batch receipt is not completed")
        if (
            request_sha256 is not None
            and receipt.get("request_sha256") != request_sha256
        ):
            raise ValueError("the batch receipt request hash changed")
        if receipt.get("stage") != ROLE_STAGES[role] or receipt.get(
            "model"
        ) != self.model_for_role(role):
            raise ValueError("the batch receipt stage changed")
        return receipt, _provider_result(
            receipt,
            self.model_for_role(role),
            allow_enum=role == "answer_judge",
        )

    def receipt_reference(
        self,
        role: str,
        system: str,
        prompt: str,
        parameters: dict[str, Any],
    ) -> dict[str, str]:
        """Return the immutable batch receipt for an exact completed call."""
        if not all((self.paper_id, self.family_id, self.source_version_id)):
            raise ValueError("the batch provider is not bound to a paper")
        stage = ROLE_STAGES[role]
        request_config = model_config_for_stage(self.config, stage)
        payload = _request_payload(system, prompt, parameters, request_config)
        key = broker_request_key(
            model=str(request_config["model"]),
            run_id=self.invocation_run_id,
            phase=self.phase,
            stage=stage,
            paper_id=str(self.paper_id),
            family_id=str(self.family_id),
            source_version_id=str(self.source_version_id),
            payload=payload,
        )
        path = self.effective_receipt_path(key)
        return {
            "request_key": key,
            "receipt_file": str(path),
            "receipt_sha256": sha256_file(path),
        }

    def effective_receipt_path(self, request_key: str) -> Path:
        path = self.store.receipt_path(request_key)
        if not path.is_file():
            raise ValueError("the batch request has no completed receipt")
        return path

    def effective_receipt(self, request_key: str) -> dict[str, Any]:
        return _read(self.effective_receipt_path(request_key))


def _terminal_candidate(
    db: Database, run_id: str, source_id: str
) -> dict[str, Any] | None:
    return db.one(
        """SELECT item_id,status FROM candidates WHERE run_id=? AND source_id=?
        AND status IN ('rejected','machine_accepted_unverified','incomplete_non_mcq')
        ORDER BY updated_at DESC,item_id DESC LIMIT 1""",
        (run_id, source_id),
    )


def _finish_candidate(
    db: Database, namespace: Path, candidate: dict[str, Any]
) -> dict[str, Any]:
    validation = validate_candidate(db, namespace, candidate).as_dict()
    if (
        validation["final_label"] == "machine_accepted_unverified"
        and not validation["labels"]["mcq_eligible"]
    ):
        with db.transaction():
            db.connection.execute(
                "UPDATE candidates SET status='incomplete_non_mcq',updated_at=? WHERE item_id=?",
                (now(), candidate["item_id"]),
            )
    return validation


def _capture_remaining_options(
    db: Database,
    namespace: Path,
    *,
    source_id: str,
    run_id: str,
    provider: BatchProvider,
    generation_attempt: dict[str, Any] | None = None,
) -> None:
    memory = Database(Path(":memory:"))
    db.connection.backup(memory.connection)
    try:
        capture = replace(provider, capture_option_requests=True)
        try:
            arguments = {
                "source_id": source_id,
                "run_id": run_id,
                "arm": "answer_first",
                "author": capture,
                "verifier": capture,
                "budget_mode": "tokens",
                "budget_limit": Decimal("1000000"),
                "reservation": Decimal("100"),
                "timeout": 30,
                "retries": 0,
                "rate_limit_seconds": 0,
            }
            if generation_attempt is not None:
                arguments["generation_attempt"] = generation_attempt
            generate_candidate(memory, namespace, **arguments)
        except BatchPendingError:
            pass
    finally:
        memory.close()


class _BatchProgress:
    """Provide the generation progress callbacks without a live progress file."""

    def paper(self, **_: Any) -> None:
        return None

    def error(self, *_: Any) -> None:
        return None


def _terminal_dispositions(
    db_file: Path, campaign_id: str
) -> dict[str, dict[str, str]]:
    connection = sqlite3.connect(
        f"file:{db_file.resolve()}?mode=ro", uri=True, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT s.stable_id,s.source_id,c.item_id,c.status,c.candidate_json
            FROM candidates c JOIN sources s ON s.source_id=c.source_id
            WHERE c.run_id=?
            AND c.status IN ('rejected','machine_accepted_unverified','incomplete_non_mcq')
            ORDER BY s.stable_id,c.updated_at DESC,c.item_id DESC""",
            (campaign_id,),
        ).fetchall()
    finally:
        connection.close()
    dispositions: dict[str, dict[str, str]] = {}
    for row in rows:
        if not _is_current_contract_candidate(json.loads(row["candidate_json"])):
            continue
        dispositions.setdefault(
            str(row["stable_id"]),
            {
                "source_id": str(row["source_id"]),
                "item_id": str(row["item_id"]),
                "status": str(row["status"]),
            },
        )
    return dispositions


def select_continuation(
    *,
    access_run_dir: Path,
    db_file: Path,
    campaign_id: str,
    prior_run_id: str,
    eligibility_run_dir: Path,
    shared_ledger_file: Path,
    production_progress_file: Path,
    output_file: Path,
    require_stopped: bool = False,
) -> dict[str, Any]:
    manifest_file = access_run_dir / "run-manifest.json"
    manifest_bytes = manifest_file.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict):
        raise ValueError("the ranked access manifest is not an object")
    selection = manifest.get("selection")
    if not isinstance(selection, list) or manifest.get("target_total") != len(
        selection
    ):
        raise ValueError("the ranked access selection is invalid")
    ranked: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(selection, start=1):
        key = item.get("candidate_key")
        if (
            not isinstance(key, str)
            or key in ranked
            or item.get("position") != index
            or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("source_content_hash")))
        ):
            raise ValueError("the ranked access order or identity changed")
        ranked[key] = item

    ledger_bytes = shared_ledger_file.read_bytes()
    ledger = json.loads(ledger_bytes)
    if ledger.get("schema") != "shared-paid-call-ledger-v1" or not isinstance(
        ledger.get("requests"), dict
    ):
        raise ValueError("the shared paid-call ledger is invalid")
    liabilities = _validate_batch_shared_ledger(shared_ledger_file, ledger)
    receipts: dict[str, list[dict[str, str]]] = {}
    for request_key, request in sorted(ledger["requests"].items()):
        if request.get("run_id") != prior_run_id:
            continue
        paper_id = request.get("paper_id")
        if paper_id not in ranked:
            raise ValueError("a prior-run receipt is outside the ranked input")
        if request.get("source_version_id") != ranked[paper_id]["source_content_hash"]:
            raise ValueError("a prior-run receipt source identity changed")
        receipts.setdefault(paper_id, []).append(
            {
                "request_key": request_key,
                "stage": str(request.get("stage")),
                "state": str(request.get("state")),
            }
        )

    jobs: dict[str, dict[str, Any]] = {}
    for path in sorted((eligibility_run_dir / "jobs").glob("*.json")):
        job = _read(path)
        key = job.get("candidate_key")
        if key not in ranked or key in jobs:
            raise ValueError("an eligibility disposition is outside or repeated")
        request_key = job.get("broker_request_key")
        matching = {row["request_key"] for row in receipts.get(str(key), [])}
        if (
            job.get("execution_authority") != "shared_gemini_broker"
            or job.get("state") not in {"completed", "screening_error"}
            or request_key not in matching
            or job.get("source_content_hash") != ranked[key]["source_content_hash"]
        ):
            raise ValueError("an eligibility disposition lacks receipt custody")
        validation = job.get("validation") or {}
        if validation.get("decision") not in {"eligible", "excluded", "uncertain"}:
            raise ValueError("an eligibility disposition is invalid")
        jobs[str(key)] = {
            "decision": validation["decision"],
            "job_file": str(path.resolve()),
            "job_file_sha256": sha256_file(path),
            "broker_request_key": str(request_key),
            "broker_receipt_sha256": str(job.get("broker_receipt_sha256")),
        }

    terminal = _terminal_dispositions(db_file, campaign_id)
    unknown_terminal = sorted(set(terminal) - set(ranked))
    if unknown_terminal:
        raise ValueError("a campaign terminal disposition is outside the ranked input")
    processed = set(receipts) | set(jobs) | set(terminal)
    excluded: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for item in selection:
        key = item["candidate_key"]
        identity = {
            "position": item["position"],
            "candidate_key": key,
            "family_key": item.get("family_key"),
            "source_content_hash": item["source_content_hash"],
            "extraction_sha256": item.get("extraction_sha256"),
        }
        if key not in processed:
            remaining.append(identity)
            continue
        job = jobs.get(key)
        candidate = terminal.get(key)
        if candidate:
            disposition = f"candidate_{candidate['status']}"
        elif job and job["decision"] in {"excluded", "uncertain"}:
            disposition = f"eligibility_{job['decision']}"
        else:
            disposition = "touched_nonterminal"
        excluded.append(
            {
                **identity,
                "disposition": disposition,
                "eligibility": job,
                "terminal_candidate": candidate,
                "receipts": receipts.get(key, []),
            }
        )

    progress_bytes = production_progress_file.read_bytes()
    progress = json.loads(progress_bytes)
    if not isinstance(progress, dict):
        raise ValueError("the production progress record is not an object")
    unsettled = sorted(
        row["request_key"]
        for rows in receipts.values()
        for row in rows
        if row["state"] != "completed" and row["request_key"] not in liabilities
    )
    provisional = progress.get("state") == "running" or bool(unsettled)
    if require_stopped and provisional:
        raise ValueError("the production campaign has not reached a settled stop")
    identity = {
        "source_access_manifest_sha256": sha256_bytes(manifest_bytes),
        "campaign_id": campaign_id,
        "prior_run_id": prior_run_id,
        "production_progress_sha256": sha256_bytes(progress_bytes),
        "shared_ledger_sha256": sha256_bytes(ledger_bytes),
        "excluded_processed": excluded,
        "remaining_selection": remaining,
    }
    result = {
        "schema": CONTINUATION_SCHEMA,
        "plan_id": stable_id("gemini-batch-continuation", identity, length=32),
        "provisional": provisional,
        "production_progress_state": progress.get("state"),
        "source_access_run_dir": str(access_run_dir.resolve()),
        "source_access_manifest_sha256": identity["source_access_manifest_sha256"],
        "campaign_id": campaign_id,
        "prior_run_id": prior_run_id,
        "production_progress_file": str(production_progress_file.resolve()),
        "production_progress_sha256": identity["production_progress_sha256"],
        "shared_ledger_file": str(shared_ledger_file.resolve()),
        "shared_ledger_sha256": identity["shared_ledger_sha256"],
        "eligibility_run_dir": str(eligibility_run_dir.resolve()),
        "source_count": len(selection),
        "excluded_processed_count": len(excluded),
        "remaining_count": len(remaining),
        "unsettled_request_keys": unsettled,
        "excluded_processed": excluded,
        "remaining_selection": remaining,
    }
    atomic_json(output_file, result, immutable=True)
    return {**result, "output_file": str(output_file.resolve())}


def prepare_pipeline(
    db: Database,
    namespace: Path,
    *,
    store: BatchStore,
    run_id: str,
    campaign_id: str,
    access_run_dir: Path,
    eligibility_run_dir: Path,
    eligibility_prompt_file: Path,
    eligibility_schema_file: Path,
    eligibility_policy_file: Path,
    max_papers: int | None = None,
    continuation_plan_file: Path | None = None,
) -> dict[str, Any]:
    manifest = _read(access_run_dir / "run-manifest.json")
    selection = manifest.get("selection")
    if not isinstance(selection, list) or manifest.get("target_total") != len(
        selection
    ):
        raise ValueError("the ordered access selection is invalid")
    source_selection_count = len(selection)
    continuation_plan_sha256 = None
    continuation_plan_provisional = False
    if continuation_plan_file is not None:
        continuation = _read(continuation_plan_file)
        if continuation.get("schema") != CONTINUATION_SCHEMA or continuation.get(
            "source_access_manifest_sha256"
        ) != sha256_file(access_run_dir / "run-manifest.json"):
            raise ValueError("the batch continuation plan does not match")
        remaining = continuation.get("remaining_selection")
        if not isinstance(remaining, list):
            raise ValueError("the batch continuation selection is invalid")
        by_key = {item["candidate_key"]: item for item in selection}
        filtered = []
        previous_position = 0
        for identity in remaining:
            item = by_key.get(identity.get("candidate_key"))
            expected = {
                "position": item.get("position") if item else None,
                "candidate_key": item.get("candidate_key") if item else None,
                "family_key": item.get("family_key") if item else None,
                "source_content_hash": item.get("source_content_hash")
                if item
                else None,
                "extraction_sha256": item.get("extraction_sha256") if item else None,
            }
            if identity != expected or int(identity["position"]) <= previous_position:
                raise ValueError("the batch continuation order or identity changed")
            previous_position = int(identity["position"])
            filtered.append(item)
        selection = filtered
        continuation_plan_sha256 = sha256_file(continuation_plan_file)
        continuation_plan_provisional = continuation.get("provisional") is True
    limit = len(selection) if max_papers is None else max_papers
    if limit < 1 or limit > len(selection):
        raise ValueError("the batch paper limit is outside the ordered selection")
    access_items = {
        item["candidate_key"]: item
        for item in (
            _read(path) for path in sorted((access_run_dir / "items").glob("*.json"))
        )
    }
    eligibility_jobs = {
        item["candidate_key"]: item
        for item in (
            _read(path)
            for path in sorted((eligibility_run_dir / "jobs").glob("*.json"))
        )
        if item.get("execution_authority") == "shared_gemini_broker"
    }
    provider = BatchProvider(
        store=store, phase="away_production", invocation_run_id=run_id
    )
    papers: list[dict[str, Any]] = []
    counts = {
        "pending": 0,
        "accepted": 0,
        "rejected": 0,
        "incomplete": 0,
        "unavailable": 0,
        "paper_cost_cap_reached": 0,
    }
    for selected in selection[:limit]:
        candidate_key = selected.get("candidate_key")
        access = access_items.get(candidate_key)
        if access is None or access.get("access_state") != "full_text_ready":
            counts["unavailable"] += 1
            continue
        if access.get("position") != selected.get("position"):
            raise ValueError("the ordered access selection changed")
        family_id = str(
            access.get("paper_family_id")
            or stable_id("family", access.get("doi") or candidate_key)
        )
        paper_provider = provider.bind(
            paper_id=str(candidate_key),
            family_id=family_id,
            source_version_id=str(access["source_content_hash"]),
        )
        result = {
            "position": selected["position"],
            "paper_id": str(candidate_key),
            "family_id": family_id,
            "source_version_id": str(access["source_content_hash"]),
        }
        try:
            eligibility = eligibility_jobs.get(str(candidate_key))
            if eligibility is None:
                eligibility = _run_eligibility(
                    db,
                    access,
                    eligibility_run_dir,
                    run_id=campaign_id,
                    provider=paper_provider,
                    prompt_file=eligibility_prompt_file,
                    schema_file=eligibility_schema_file,
                    policy_file=eligibility_policy_file,
                )
                eligibility_jobs[str(candidate_key)] = eligibility
            else:
                eligibility = {
                    **eligibility,
                    "validation": _validate_brokered_eligibility(
                        eligibility,
                        paper_provider,
                        access=access,
                        prompt_file=eligibility_prompt_file,
                        schema_file=eligibility_schema_file,
                        policy_file=eligibility_policy_file,
                    ),
                }
            _validate_pair(access, eligibility)
            if eligibility["validation"]["decision"] != "eligible":
                counts["rejected"] += 1
                papers.append({**result, "state": "eligibility_rejected"})
                continue
            source_id = _import_source(
                db, namespace, access, selected, eligibility, family_id=family_id
            )
            terminal = _terminal_candidate(db, campaign_id, source_id)
            if terminal and terminal["status"] == "machine_accepted_unverified":
                counts["accepted"] += 1
                papers.append({**result, "source_id": source_id, "state": "accepted"})
                continue
            pending_attempt: dict[str, Any] | None = None

            def remember_pending(attempt: dict[str, Any]) -> None:
                nonlocal pending_attempt
                pending_attempt = attempt

            try:
                generation = _progress_generation(
                    db,
                    namespace,
                    _BatchProgress(),
                    campaign_id=campaign_id,
                    candidate_key=str(candidate_key),
                    source_id=source_id,
                    family_id=family_id,
                    selected=selected,
                    title=access.get("title"),
                    author=paper_provider,
                    verifier=paper_provider,
                    pending_handler=remember_pending,
                )
            except BatchPendingError:
                if any(
                    row["paper_id"] == str(candidate_key)
                    and row["stage"] == "option_verification"
                    for row in store.read()["requests"].values()
                ):
                    _capture_remaining_options(
                        db,
                        namespace,
                        source_id=source_id,
                        run_id=campaign_id,
                        provider=paper_provider,
                        generation_attempt=pending_attempt,
                    )
                raise
            state = {
                "accepted": "accepted",
                "incomplete_non_mcq": "incomplete",
                "generation_rejected": "rejected",
                # The per-paper cost cap ends one family, not the batch.
                "paper_cost_cap_reached": "paper_cost_cap_reached",
            }[generation["disposition"]]
            counts[state] += 1
            papers.append(
                {
                    **result,
                    "source_id": source_id,
                    "state": state,
                    "reason": generation["reason_codes"],
                }
            )
        except BatchPendingError:
            counts["pending"] += 1
            papers.append({**result, "state": "pending"})
        except CandidateRejectedError as error:
            counts["rejected"] += 1
            papers.append({**result, "state": "rejected", "reason": error.reason_code})
    ordered_inputs = [
        {
            "position": row["position"],
            "paper_id": row["paper_id"],
            "family_id": row["family_id"],
            "source_version_id": row["source_version_id"],
        }
        for row in papers
    ]
    run_identity = {
        "run_id": run_id,
        "campaign_id": campaign_id,
        "access_manifest_sha256": sha256_file(access_run_dir / "run-manifest.json"),
        "selection_count": limit,
        "source_selection_count": source_selection_count,
        "continuation_plan_sha256": continuation_plan_sha256,
        "continuation_plan_provisional": continuation_plan_provisional,
        "models": sorted(
            {
                MODEL,
                str(model_config_for_stage(store.config, "answer_agreement")["model"]),
            }
        ),
        "price_config_sha256": sha256_file(store.price_config_file),
    }
    round_manifest = store.make_round(
        run_identity=run_identity, ordered_inputs=ordered_inputs
    )
    return {
        "schema": "arctic-gemini-batch-prepare-result-v1",
        "run_identity": run_identity,
        "counts": counts,
        "papers": papers,
        "round": round_manifest,
        "live_call_made": False,
    }


class GeminiBatchHTTP:
    def __init__(self, api_base: str, api_key: str, *, timeout: float = 120) -> None:
        if not api_key or "\n" in api_key:
            raise ValueError("the Gemini API key is invalid")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _json(
        self,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data = canonical_json(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method="POST" if body is not None else "GET",
            headers={
                "x-goog-api-key": self.api_key,
                "content-type": "application/json",
                **(headers or {}),
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            value = json.loads(response.read())
        if not isinstance(value, dict):
            raise ValueError("the Gemini Batch API returned a non-object response")
        return value

    def upload(self, path: Path, display_name: str) -> dict[str, Any]:
        size = path.stat().st_size
        start = urllib.request.Request(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            data=canonical_json({"file": {"display_name": display_name}}).encode(),
            method="POST",
            headers={
                "x-goog-api-key": self.api_key,
                "content-type": "application/json",
                "x-goog-upload-protocol": "resumable",
                "x-goog-upload-command": "start",
                "x-goog-upload-header-content-length": str(size),
                "x-goog-upload-header-content-type": "application/jsonl",
            },
        )
        with urllib.request.urlopen(start, timeout=self.timeout) as response:
            upload_url = response.headers.get("x-goog-upload-url")
        if not upload_url:
            raise ValueError("the Gemini File API did not return an upload URL")
        upload = urllib.request.Request(
            upload_url,
            data=path.read_bytes(),
            method="POST",
            headers={
                "content-length": str(size),
                "x-goog-upload-offset": "0",
                "x-goog-upload-command": "upload, finalize",
            },
        )
        with urllib.request.urlopen(upload, timeout=self.timeout) as response:
            value = json.loads(response.read())
        if not isinstance(value, dict) or not str(
            (value.get("file") or {}).get("name") or ""
        ).startswith("files/"):
            raise ValueError("the Gemini File API upload response is invalid")
        return value

    def create(self, model: str, file_name: str, display_name: str) -> dict[str, Any]:
        return self._json(
            f"{self.api_base}/models/{urllib.parse.quote(model, safe='')}:batchGenerateContent",
            body={
                "batch": {
                    "display_name": display_name,
                    "input_config": {"file_name": file_name},
                }
            },
        )

    def get(self, job_name: str) -> dict[str, Any]:
        return self._json(f"{self.api_base}/{job_name}")

    def download(self, file_name: str) -> bytes:
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/download/v1beta/{file_name}:download?alt=media",
            headers={"x-goog-api-key": self.api_key},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()


def submit_round(
    store: BatchStore,
    manifest_path: Path,
    authorization_path: Path,
    transport: BatchTransport,
) -> dict[str, Any]:
    manifest = _read(manifest_path)
    if (
        manifest.get("schema") != ROUND_SCHEMA
        or manifest_path.resolve()
        != Path(manifest["requests_file"]).resolve().parent / "manifest.json"
    ):
        raise ValueError("the batch round manifest path is invalid")
    authorization = _read(authorization_path)
    preview = store.authorize_round(manifest, authorization)
    store.activate_exclusive_mode(authorization["shared_ledger_sha256"])
    store.mark_submitting(manifest["round_id"])
    try:
        uploaded = transport.upload(
            Path(manifest["requests_file"]), f"arctic-{manifest['round_id']}"
        )
        file_name = str(uploaded["file"]["name"])
        created = transport.create(
            str(manifest["model"]), file_name, f"arctic-{manifest['round_id']}"
        )
    except Exception:
        # The job-creation outcome can be unknown. Keep the full reservation.
        raise
    job_name = str(created.get("name") or "")
    store.mark_submitted(manifest["round_id"], job_name, file_name)
    raw = store.root / "raw" / f"{manifest['round_id']}.created.json"
    atomic_json(raw, created, immutable=True)
    return {
        "round_id": manifest["round_id"],
        "job_name": job_name,
        "budget": preview,
        "created_response": str(raw),
    }


def reconcile_submission(
    store: BatchStore,
    round_id: str,
    job_name: str,
    provider_file_name: str = "reconciled-unknown",
) -> dict[str, Any]:
    state = store.read()
    row = state["rounds"].get(round_id)
    if row is None or row["state"] not in {"submitting", "submission_ambiguous"}:
        raise ValueError("the batch round does not have an ambiguous submission")
    store.mark_submitted(round_id, job_name, provider_file_name)
    return {"round_id": round_id, "job_name": job_name, "resubmitted": False}


def record_status(
    store: BatchStore, round_id: str, status: dict[str, Any]
) -> dict[str, Any]:
    state = store.read()
    row = state["rounds"].get(round_id)
    if row is None or row.get("job_name") != status.get("name"):
        raise ValueError("the batch status does not match the recorded job")
    job_state = status.get("state") or (status.get("metadata") or {}).get("state")
    if not isinstance(job_state, str) or not job_state.startswith("JOB_STATE_"):
        raise ValueError("the batch status has no valid job state")
    raw = (
        store.root
        / "raw"
        / f"{round_id}.status-{sha256_bytes(canonical_json(status).encode())}.json"
    )
    atomic_json(raw, status, immutable=True)

    def mutate(value: dict[str, Any]) -> None:
        current = value["rounds"][round_id]
        current["provider_state"] = job_state
        current["last_status_file"] = str(raw.relative_to(store.root))
        if job_state in TERMINAL_JOB_STATES and job_state != "JOB_STATE_SUCCEEDED":
            current["state"] = "settlement_pending"
            for key in current["request_keys"]:
                if value["requests"][key]["state"] == "submitted":
                    value["requests"][key]["state"] = "missing"
        elif job_state == "JOB_STATE_SUCCEEDED":
            current["state"] = "results_ready"

    store.update(mutate)
    return {"round_id": round_id, "job_state": job_state, "status_file": str(raw)}


def ingest_results(
    store: BatchStore, round_id: str, results_file: Path
) -> dict[str, Any]:
    initial_state = store.read()
    round_state = initial_state["rounds"].get(round_id)
    if round_state is None:
        raise ValueError("the batch round does not exist")
    expected = set(round_state["request_keys"])
    raw_bytes = results_file.read_bytes()
    raw_path = (
        store.root / "raw" / f"{round_id}.results-{sha256_bytes(raw_bytes)}.jsonl"
    )
    atomic_write(raw_path, raw_bytes, immutable=True)
    rows: dict[str, tuple[int, bytes, dict[str, Any]]] = {}
    for line_number, raw_line in enumerate(raw_bytes.splitlines(), start=1):
        if not raw_line.strip():
            continue
        value = json.loads(raw_line)
        if not isinstance(value, dict) or not isinstance(value.get("key"), str):
            raise ValueError("a batch result line has no request key")
        key = value["key"]
        if key in rows:
            raise ValueError(f"the batch results repeat request key {key}")
        if key not in expected:
            raise ValueError(f"the batch results contain unexpected request key {key}")
        rows[key] = (line_number, raw_line, value)
    completed: list[str] = []
    newly_completed: list[str] = []
    for key, (line_number, raw_line, value) in rows.items():
        response = value.get("response")
        if response is None:
            if store.read()["requests"][key]["state"] == "completed":
                raise ValueError("a completed batch response changed to an error")
            error_receipt = {
                "schema": "arctic-gemini-batch-error-receipt-v1",
                "request_key": key,
                "error": value.get("error"),
                "raw_results_file": str(raw_path),
                "raw_results_sha256": sha256_file(raw_path),
                "raw_line_number": line_number,
                "raw_line_sha256": sha256_bytes(raw_line),
            }
            error_path = (
                store.root / "receipts" / f"{key}.error-{sha256_bytes(raw_bytes)}.json"
            )
            atomic_json(error_path, error_receipt, immutable=True)

            def mark_error(
                batch: dict[str, Any], key: str = key, error_path: Path = error_path
            ) -> None:
                if batch["requests"][key]["state"] != "completed":
                    batch["requests"][key]["state"] = "error_unsettled"
                    batch["requests"][key]["error_receipt_sha256"] = sha256_file(
                        error_path
                    )

            store.update(mark_error)
            continue
        if not isinstance(response, dict):
            raise ValueError("a batch result response is not an object")
        request = store.prepared_record(key)
        usage = _normalized_usage(response)
        actual = _priced_cost(
            request["batch_pricing"],
            usage["promptTokenCount"],
            usage["candidatesTokenCount"] + usage["thoughtsTokenCount"],
        )
        if actual > Decimal(request["reserved_usd"]):
            raise ValueError("a batch result exceeds its conservative reservation")
        current = store.read()["requests"][key]
        if current["state"] == "completed":
            existing = _read(store.receipt_path(key))
            if existing.get("response") != response or existing.get("usage") != usage:
                raise ValueError("an already completed batch response changed")
            completed.append(key)
            continue
        receipt = {
            "schema": "arctic-gemini-batch-receipt-v1",
            "request_key": key,
            "request_sha256": request["request_sha256"],
            "run_id": request["run_id"],
            "stage": request["stage"],
            "paper_id": request["paper_id"],
            "family_id": request["family_id"],
            "source_version_id": request["source_version_id"],
            "model": request["model"],
            "state": "completed",
            "reserved_usd": request["reserved_usd"],
            "actual_cost_usd": str(actual),
            "batch_pricing": request["batch_pricing"],
            "usage": usage,
            "response": response,
            "raw_results_file": str(raw_path),
            "raw_results_sha256": sha256_file(raw_path),
            "raw_line_number": line_number,
            "raw_line_sha256": sha256_bytes(raw_line),
        }
        receipt_path = store.receipt_path(key)
        atomic_json(receipt_path, receipt, immutable=True)

        def mark_completed(
            batch: dict[str, Any], key: str = key, actual: Decimal = actual
        ) -> None:
            row = batch["requests"][key]
            if row["state"] == "completed":
                if row["receipt_sha256"] != sha256_file(receipt_path):
                    raise ValueError("the ingested batch receipt changed")
                return
            row["state"] = "completed"
            row["receipt_sha256"] = sha256_file(receipt_path)
            row["actual_cost_usd"] = str(actual)

        store.update(mark_completed)
        completed.append(key)
        newly_completed.append(key)

    def finish(batch: dict[str, Any]) -> None:
        row = batch["rounds"][round_id]
        missing = sorted(
            key
            for key in expected
            if batch["requests"][key]["state"]
            in {"prepared", "submitted", "submission_ambiguous", "missing"}
        )
        errors = sorted(
            key
            for key in expected
            if batch["requests"][key]["state"] == "error_unsettled"
        )
        row["missing_keys"] = missing
        row["error_keys"] = errors
        row["actual_cost_usd"] = str(
            sum(
                Decimal(batch["requests"][key].get("actual_cost_usd") or "0")
                for key in expected
            )
        )
        if missing or errors:
            row["state"] = "partial_results"
            for key in missing:
                if batch["requests"][key]["state"] == "submitted":
                    batch["requests"][key]["state"] = "missing"
        else:
            row["state"] = "completed"
            row["reserved_usd"] = "0"

    final = store.update(finish)
    missing = final["rounds"][round_id]["missing_keys"]
    errors = final["rounds"][round_id]["error_keys"]
    return {
        "round_id": round_id,
        "completed_keys": sorted(completed),
        "error_keys": sorted(errors),
        "missing_keys": missing,
        "results_file_sha256": sha256_file(raw_path),
        "idempotent": bool(rows)
        and not newly_completed
        and all(initial_state["requests"][key]["state"] == "completed" for key in rows),
    }


def _store_from_args(args: argparse.Namespace) -> BatchStore:
    return BatchStore(
        Path(args.state_dir),
        price_config_file=Path(args.price_config_file),
        shared_ledger_file=Path(args.shared_ledger_file),
        overall_ceiling_usd=Decimal(args.overall_ceiling_usd),
        maximum_request_usd=Decimal(args.maximum_request_usd),
        maximum_paper_usd=Decimal(args.maximum_paper_usd),
    )


def _add_store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--price-config-file", required=True)
    parser.add_argument("--shared-ledger-file", required=True)
    parser.add_argument(
        "--overall-ceiling-usd",
        "--batch-allocation-usd",
        dest="overall_ceiling_usd",
        default=str(DEFAULT_BATCH_ALLOCATION_USD),
    )
    parser.add_argument("--maximum-request-usd", default="0.25")
    parser.add_argument("--maximum-paper-usd", default="1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and operate staged Gemini batch continuation."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    _add_store_arguments(prepare)
    prepare.add_argument("--db", required=True)
    prepare.add_argument("--namespace", required=True)
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--campaign-id", required=True)
    prepare.add_argument("--access-run-dir", required=True)
    prepare.add_argument("--eligibility-run-dir", required=True)
    prepare.add_argument("--eligibility-prompt-file", required=True)
    prepare.add_argument("--eligibility-schema-file", required=True)
    prepare.add_argument("--eligibility-policy-file", required=True)
    prepare.add_argument("--continuation-plan")
    prepare.add_argument("--max-papers", type=int)
    resume = commands.add_parser("resume", parents=[], add_help=True)
    for action in prepare._actions[1:]:
        if action.dest != "help":
            resume._add_action(action)
    submit = commands.add_parser("submit")
    _add_store_arguments(submit)
    submit.add_argument("--manifest", required=True)
    submit.add_argument("--authorization", required=True)
    submit.add_argument("--credential-file", required=True)
    status = commands.add_parser("status")
    _add_store_arguments(status)
    status.add_argument("--round-id", required=True)
    status.add_argument("--credential-file", required=True)
    ingest = commands.add_parser("ingest")
    _add_store_arguments(ingest)
    ingest.add_argument("--round-id", required=True)
    ingest.add_argument("--results-file", required=True)
    reconcile = commands.add_parser("reconcile")
    _add_store_arguments(reconcile)
    reconcile.add_argument("--round-id", required=True)
    reconcile.add_argument("--job-name", required=True)
    reconcile.add_argument("--provider-file-name", default="reconciled-unknown")
    export = commands.add_parser("export")
    export.add_argument("--db", required=True)
    export.add_argument("--namespace", required=True)
    export.add_argument("--campaign-id", required=True)
    export.add_argument("--seed", default="streaming-20260912")
    export.add_argument("--publication-output-dir")
    export.add_argument("--prompt-template", action="append", default=[])
    continuation = commands.add_parser("select-continuation")
    continuation.add_argument("--access-run-dir", required=True)
    continuation.add_argument("--db", required=True)
    continuation.add_argument("--campaign-id", required=True)
    continuation.add_argument("--prior-run-id", required=True)
    continuation.add_argument("--eligibility-run-dir", required=True)
    continuation.add_argument("--shared-ledger-file", required=True)
    continuation.add_argument("--production-progress-file", required=True)
    continuation.add_argument("--output-file", required=True)
    continuation.add_argument("--require-stopped", action="store_true")
    return parser


def _credential(path: Path) -> str:
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("the Gemini credential file is absent or not private")
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value:
        raise ValueError("the Gemini credential file must contain one line")
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"prepare", "resume"}:
            store = _store_from_args(args)
            db = Database(Path(args.db))
            try:
                result = prepare_pipeline(
                    db,
                    Path(args.namespace),
                    store=store,
                    run_id=args.run_id,
                    campaign_id=args.campaign_id,
                    access_run_dir=Path(args.access_run_dir),
                    eligibility_run_dir=Path(args.eligibility_run_dir),
                    eligibility_prompt_file=Path(args.eligibility_prompt_file),
                    eligibility_schema_file=Path(args.eligibility_schema_file),
                    eligibility_policy_file=Path(args.eligibility_policy_file),
                    max_papers=args.max_papers,
                    continuation_plan_file=(
                        Path(args.continuation_plan) if args.continuation_plan else None
                    ),
                )
            finally:
                db.close()
        elif args.command == "select-continuation":
            selected = select_continuation(
                access_run_dir=Path(args.access_run_dir),
                db_file=Path(args.db),
                campaign_id=args.campaign_id,
                prior_run_id=args.prior_run_id,
                eligibility_run_dir=Path(args.eligibility_run_dir),
                shared_ledger_file=Path(args.shared_ledger_file),
                production_progress_file=Path(args.production_progress_file),
                output_file=Path(args.output_file),
                require_stopped=args.require_stopped,
            )
            result = {
                "schema": selected["schema"],
                "plan_id": selected["plan_id"],
                "provisional": selected["provisional"],
                "source_count": selected["source_count"],
                "excluded_processed_count": selected["excluded_processed_count"],
                "remaining_count": selected["remaining_count"],
                "first_remaining": (
                    selected["remaining_selection"][0]
                    if selected["remaining_selection"]
                    else None
                ),
                "source_access_manifest_sha256": selected[
                    "source_access_manifest_sha256"
                ],
                "shared_ledger_sha256": selected["shared_ledger_sha256"],
                "output_file": selected["output_file"],
            }
        elif args.command == "submit":
            store = _store_from_args(args)
            transport = GeminiBatchHTTP(
                store.config["api_base"], _credential(Path(args.credential_file))
            )
            result = submit_round(
                store, Path(args.manifest), Path(args.authorization), transport
            )
        elif args.command == "status":
            store = _store_from_args(args)
            state = store.read()
            job_name = state["rounds"][args.round_id].get("job_name")
            if not job_name:
                raise ValueError("the batch round has no reconciled job identity")
            transport = GeminiBatchHTTP(
                store.config["api_base"], _credential(Path(args.credential_file))
            )
            provider_status = transport.get(job_name)
            result = record_status(store, args.round_id, provider_status)
            if result["job_state"] == "JOB_STATE_SUCCEEDED":
                file_name = str(
                    (provider_status.get("dest") or {}).get("fileName")
                    or (provider_status.get("response") or {}).get("responsesFile")
                    or ""
                )
                if file_name:
                    downloaded = store.root / "raw" / f"{args.round_id}.download.jsonl"
                    atomic_write(downloaded, transport.download(file_name))
                    result["downloaded_results"] = str(downloaded)
        elif args.command == "ingest":
            result = ingest_results(
                _store_from_args(args), args.round_id, Path(args.results_file)
            )
        elif args.command == "reconcile":
            result = reconcile_submission(
                _store_from_args(args),
                args.round_id,
                args.job_name,
                args.provider_file_name,
            )
        else:
            db = Database(Path(args.db))
            try:
                result = export_run(
                    db, Path(args.namespace), args.campaign_id, seed=args.seed
                )
            finally:
                db.close()
            if args.publication_output_dir:
                export_manifest = (
                    Path(args.namespace)
                    / "exports"
                    / result["export_id"]
                    / "manifest.json"
                )
                publication = export_publication_package(
                    Path(args.db),
                    Path(args.publication_output_dir),
                    seed=args.seed,
                    prompt_templates=[Path(value) for value in args.prompt_template],
                    export_manifest=export_manifest,
                )
                result = {"accepted_export": result, "publication_export": publication}
        print(canonical_json(result))
        return 0
    except Exception as error:
        print(
            canonical_json({"error": type(error).__name__, "message": str(error)}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
