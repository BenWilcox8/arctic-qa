"""Behavioural calibration of the source-blind standalone gate.

The r15 audit built ``fixtures/standalone-calibration-v1.jsonl`` and declared
a release rule, but nothing ever ran the judge prompt against it
(chapter 2 yield audit, stage standalone_gate, F6). This module wires the set
to the live judge behind a replay cassette:

- ``record`` calls the configured ``standalone_verifier`` provider once per
  labelled row and writes every response into a cassette whose header binds
  the SHA-256 of ``STANDALONE_SYSTEM``. Recording is a paid operation and
  never runs in tests.
- ``replay`` evaluates a cassette against the calibration set with
  ``standalone_gate_decision``, the same composed decision the pipeline
  applies, and fails the release rule when any ``must_fail`` row passes or
  fewer than ``STANDALONE_CALIBRATION_MUST_PASS_RATE`` of the ``must_pass``
  rows pass. A cassette recorded under another prompt text is stale and is
  refused, so a prompt change always forces a fresh live run.

Only rows both labelers agreed on gate. A ``disputed`` row is kept in the file
for the record and is neither recorded nor scored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .generation import ROLE_SCHEMAS, STANDALONE_SYSTEM
from .providers import Provider, ProviderResult, _validate_schema, provider_model
from .util import canonical_json, sha256_bytes
from .validation import (
    STANDALONE_CALIBRATION_MUST_PASS_RATE,
    STANDALONE_CALIBRATION_SET_VERSION,
    STANDALONE_VERIFICATION_CONTRACT_VERSION,
    standalone_gate_decision,
)

CASSETTE_CONTRACT_VERSION = "standalone-calibration-cassette-v1"
CALIBRATION_ROLE = "standalone_verifier"
GATING_LABELS = frozenset({"must_pass", "must_fail"})
DEFAULT_CALIBRATION_SET = (
    Path(__file__).parents[2] / "fixtures" / "standalone-calibration-v2.jsonl"
)
# The judge call parameters of generate_candidate, so a recorded verdict is
# the verdict the pipeline would have received.
CALIBRATION_PARAMETERS: dict[str, Any] = {
    "temperature": 0,
    "max_tokens": 2048,
    "reasoning_token_cap": 2048,
    "billable_token_overhead": 1024,
}


class CalibrationError(ValueError):
    """The calibration set or the cassette does not satisfy its contract."""


@dataclass(frozen=True)
class CalibrationSet:
    header: dict[str, Any]
    rows: list[dict[str, Any]]

    @property
    def gating_rows(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if row.get("label") in GATING_LABELS]


def standalone_system_sha256() -> str:
    return sha256_bytes(STANDALONE_SYSTEM.encode("utf-8"))


def calibration_prompt(row: dict[str, Any]) -> str:
    """The exact DISPLAYED_TASK prompt generate_candidate sends to the judge."""
    return "DISPLAYED_TASK\n" + canonical_json(
        {
            "question": str(row["question"]),
            "question_context": str(row.get("question_context", "") or ""),
        }
    )


def load_calibration_set(path: Path) -> CalibrationSet:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records or records[0].get("record") != "calibration_set_header":
        raise CalibrationError("the calibration set has no header record")
    header = records[0]
    if header.get("calibration_set_version") != STANDALONE_CALIBRATION_SET_VERSION:
        raise CalibrationError(
            "the calibration set version is not current: "
            + str(header.get("calibration_set_version"))
        )
    if header.get("contract_version") != STANDALONE_VERIFICATION_CONTRACT_VERSION:
        raise CalibrationError("the calibration set names another standalone contract")
    rows = records[1:]
    seen: set[str] = set()
    for row in rows:
        if row.get("record") != "calibration_row":
            raise CalibrationError("a calibration record is not a calibration_row")
        item_id = str(row.get("item_id", ""))
        if not item_id or item_id in seen:
            raise CalibrationError(f"duplicate or missing item_id: {item_id!r}")
        seen.add(item_id)
        _validate_labels(row)
    return CalibrationSet(header, rows)


def _validate_labels(row: dict[str, Any]) -> None:
    """Two labelers, recorded as fields; the gating label is their agreement."""
    labels = row.get("labels")
    if not isinstance(labels, dict) or set(labels) != {"labeler_1", "labeler_2"}:
        raise CalibrationError(f"row {row.get('item_id')} does not carry two labelers")
    verdicts = []
    for key in ("labeler_1", "labeler_2"):
        entry = labels[key]
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("labeler"), str)
            or not entry["labeler"]
            or entry.get("label") not in GATING_LABELS
            or not isinstance(entry.get("labeled_on"), str)
        ):
            raise CalibrationError(f"row {row.get('item_id')} has an invalid {key}")
        verdicts.append(entry["label"])
    expected = verdicts[0] if verdicts[0] == verdicts[1] else "disputed"
    if row.get("label") != expected:
        raise CalibrationError(
            f"row {row.get('item_id')} label {row.get('label')!r} does not equal "
            f"the labelers' agreement {expected!r}"
        )


def _row_decision(row: dict[str, Any], response: dict[str, Any]) -> list[str]:
    return standalone_gate_decision(
        str(row["question"]),
        str(row.get("question_context", "") or ""),
        row.get("answer"),
        model_reasons=list(response.get("reasons") or []),
        model_answer_leakage_absent=response.get("answer_leakage_absent"),
    )


def record_cassette(
    calibration: CalibrationSet,
    provider: Provider,
    cassette_path: Path,
    *,
    timeout: float = 300.0,
    bind_row: Any = None,
) -> dict[str, Any]:
    """Call the judge once per gating row and write the cassette.

    ``bind_row`` optionally maps a row to a bound provider (the shared broker
    needs a paper identity per request). The cassette is written only after
    every call succeeded, so a partial recording never gates anything.
    """
    parameters = {**CALIBRATION_PARAMETERS, "json_schema": ROLE_SCHEMAS[CALIBRATION_ROLE]}
    rows_out: list[dict[str, Any]] = []
    for row in calibration.gating_rows:
        bound = bind_row(provider, row) if bind_row is not None else provider
        prompt = calibration_prompt(row)
        result: ProviderResult = bound.invoke(
            CALIBRATION_ROLE, STANDALONE_SYSTEM, prompt, parameters, timeout
        )
        response = result.payload
        _validate_schema(response, ROLE_SCHEMAS[CALIBRATION_ROLE])
        rows_out.append(
            {
                "record": "cassette_row",
                "item_id": row["item_id"],
                "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
                "response": response,
                "returned_model": result.returned_model,
                "request_id": result.request_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "actual_cost_usd": (
                    str(result.actual_cost_usd) if result.actual_cost_usd is not None else None
                ),
            }
        )
    header = {
        "record": "cassette_header",
        "cassette_contract_version": CASSETTE_CONTRACT_VERSION,
        "calibration_set_version": STANDALONE_CALIBRATION_SET_VERSION,
        "contract_version": STANDALONE_VERIFICATION_CONTRACT_VERSION,
        "standalone_system_sha256": standalone_system_sha256(),
        "provider": provider.name,
        "requested_model": provider_model(provider, CALIBRATION_ROLE),
        "parameters": CALIBRATION_PARAMETERS,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows": len(rows_out),
    }
    cassette_path.parent.mkdir(parents=True, exist_ok=True)
    cassette_path.write_text(
        "\n".join(canonical_json(record) for record in [header, *rows_out]) + "\n",
        encoding="utf-8",
    )
    return {"cassette": str(cassette_path), **header}


def load_cassette(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records or records[0].get("record") != "cassette_header":
        raise CalibrationError("the cassette has no header record")
    header = records[0]
    if header.get("cassette_contract_version") != CASSETTE_CONTRACT_VERSION:
        raise CalibrationError("the cassette contract version is not current")
    if header.get("standalone_system_sha256") != standalone_system_sha256():
        raise CalibrationError(
            "the cassette was recorded under another STANDALONE_SYSTEM text; "
            "record a fresh cassette against the current prompt"
        )
    if header.get("calibration_set_version") != STANDALONE_CALIBRATION_SET_VERSION:
        raise CalibrationError("the cassette was recorded against another calibration set")
    rows: dict[str, dict[str, Any]] = {}
    for record in records[1:]:
        if record.get("record") != "cassette_row":
            raise CalibrationError("a cassette record is not a cassette_row")
        rows[str(record["item_id"])] = record
    return header, rows


def evaluate_cassette(calibration: CalibrationSet, cassette_path: Path) -> dict[str, Any]:
    """Apply the release rule to a recorded cassette. Makes no call."""
    header, recorded = load_cassette(cassette_path)
    results: list[dict[str, Any]] = []
    must_pass_total = 0
    must_pass_passed = 0
    must_fail_violations: list[str] = []
    for row in calibration.gating_rows:
        item_id = str(row["item_id"])
        record = recorded.get(item_id)
        if record is None:
            raise CalibrationError(f"the cassette holds no response for {item_id}")
        if record.get("prompt_sha256") != sha256_bytes(
            calibration_prompt(row).encode("utf-8")
        ):
            raise CalibrationError(f"the cassette prompt for {item_id} differs from the row")
        response = record["response"]
        _validate_schema(response, ROLE_SCHEMAS[CALIBRATION_ROLE])
        reasons = _row_decision(row, response)
        passed = not reasons
        if row["label"] == "must_pass":
            must_pass_total += 1
            must_pass_passed += int(passed)
        elif passed:
            must_fail_violations.append(item_id)
        results.append(
            {
                "item_id": item_id,
                "label": row["label"],
                "slice": row.get("slice", "core"),
                "passed": passed,
                "reasons": reasons,
                "judge_reasons": list(response.get("reasons") or []),
                "judge_pass": response.get("pass"),
                "unresolved_phrases": list(response.get("unresolved_phrases") or []),
                "review_rationale": str(response.get("review_rationale") or ""),
            }
        )
    rate = (
        Decimal(must_pass_passed) / Decimal(must_pass_total)
        if must_pass_total
        else Decimal(0)
    )
    passed_rule = not must_fail_violations and rate >= STANDALONE_CALIBRATION_MUST_PASS_RATE
    return {
        "calibration_set_version": STANDALONE_CALIBRATION_SET_VERSION,
        "cassette": str(cassette_path),
        "cassette_recorded_at_utc": header.get("recorded_at_utc"),
        "requested_model": header.get("requested_model"),
        "standalone_system_sha256": header.get("standalone_system_sha256"),
        "must_pass_total": must_pass_total,
        "must_pass_passed": must_pass_passed,
        "must_pass_rate": str(rate),
        "must_pass_rate_required": str(STANDALONE_CALIBRATION_MUST_PASS_RATE),
        "must_fail_violations": must_fail_violations,
        "passed": passed_rule,
        "rows": results,
    }
