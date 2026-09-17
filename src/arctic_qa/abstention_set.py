"""Build and load a frozen abstention evaluation set from the state database.

The builder reads the state database read-only, selects accepted benchmark
items by population, orders each item's accepted distractors by the item's
fixed random distractor order, keeps the first k, and freezes the result as
``items.jsonl`` plus a ``manifest.json`` with hashes. An item that lacks k
accepted distractors is excluded and listed with its reason.

Populations:

- ``current``: the candidates that match the live dataset contract file
  (candidate schema, generation prompt, scope contract).
- ``production``: every accepted candidate whose run id starts with the
  production campaign prefix, across contracts.
- ``list``: an explicit item id list.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import db

from .abstention_render import (
    DEFAULT_CONTENT_OPTION_COUNT,
    PROMPT_VERSION,
    prompt_contract,
    prompt_sha256,
)
from .distractor_order import (
    DISTRACTOR_ORDER_CONTRACT_VERSION,
    ASSIGNED_SEED_LABEL,
    apply_order,
    resolve_order,
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


EVAL_SET_SCHEMA = "abstention-eval-set-v1"
ITEMS_FILENAME = "items.jsonl"
MANIFEST_FILENAME = "manifest.json"
ACCEPTED_STATUS = "machine_accepted_unverified"
DEFAULT_PRODUCTION_RUN_PREFIX = "arctic-qa-production-campaign-"
POPULATIONS = ("current", "production", "list")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else default
    except json.JSONDecodeError:
        return default


def load_contract(path: Path) -> dict[str, str]:
    """Read the live dataset contract that defines the ``current`` population."""
    value = json.loads(path.read_text(encoding="utf-8"))
    required = (
        "candidate_schema_version",
        "generation_prompt_version",
        "scope_contract_version",
    )
    contract = {name: value.get(name) for name in required}
    if any(not isinstance(item, str) or not item for item in contract.values()):
        raise ValueError("the population contract lacks a contract version")
    return contract


def _matches_contract(candidate: dict[str, Any], contract: dict[str, str]) -> bool:
    provenance = candidate.get("provenance")
    return bool(
        isinstance(provenance, dict)
        and candidate.get("schema_version") == contract["candidate_schema_version"]
        and provenance.get("prompt_version") == contract["generation_prompt_version"]
        and provenance.get("scope_contract_version")
        == contract["scope_contract_version"]
    )


def _script(text: str) -> str:
    has_cyrillic = bool(_CYRILLIC.search(text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if has_cyrillic and has_latin:
        return "mixed"
    if has_cyrillic:
        return "cyrillic"
    return "latin"


def construction_roles(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return which model wrote or judged the item (contamination record)."""
    provenance = candidate.get("provenance") or {}
    writer = provenance.get("author_model")
    verifier = provenance.get("verifier_model")
    judges: set[str] = set()
    calls = provenance.get("verification_calls")
    if isinstance(calls, dict):
        for record in calls.values():
            if isinstance(record, dict) and record.get("requested_model"):
                judges.add(str(record["requested_model"]))
    elif isinstance(calls, list):
        for record in calls:
            if isinstance(record, dict) and record.get("requested_model"):
                judges.add(str(record["requested_model"]))
    option_verifiers: set[str] = set()
    for verdict in candidate.get("option_verdicts") or []:
        if not isinstance(verdict, dict):
            continue
        model = (verdict.get("provenance") or {}).get("requested_model")
        if model:
            option_verifiers.add(str(model))
    if verifier:
        judges.add(str(verifier))
    models = {str(writer)} if writer else set()
    models |= judges | option_verifiers
    return {
        "writer_model": writer,
        "verifier_model": verifier,
        "judge_models": sorted(judges),
        "option_verifier_models": sorted(option_verifiers),
        "models": sorted(models),
    }


def role_of_model(roles: dict[str, Any], model: str) -> str:
    """Return ``writer``, ``judge``, ``writer_and_judge`` or ``none`` for one model."""
    wrote = roles.get("writer_model") == model
    judged = model in set(roles.get("judge_models") or []) | set(
        roles.get("option_verifier_models") or []
    )
    if wrote and judged:
        return "writer_and_judge"
    if wrote:
        return "writer"
    if judged:
        return "judge"
    return "none"


def _item_row(
    row: sqlite3.Row,
    candidate: dict[str, Any],
    details: dict[str, Any],
    *,
    k: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (item, None) or (None, exclusion) for one accepted candidate."""
    accepted = [
        item
        for item in details.get("distractors", [])
        if isinstance(item, dict) and item.get("accepted") is True
    ]
    item_id = str(candidate["item_id"])
    if len(accepted) < k:
        return None, {
            "item_id": item_id,
            "reason": "insufficient_accepted_distractors",
            "accepted_distractor_count": len(accepted),
            "required": k,
        }
    try:
        order = resolve_order(candidate)
        ordered = apply_order(order, accepted)
    except ValueError as error:
        return None, {
            "item_id": item_id,
            "reason": "invalid_distractor_order",
            "detail": str(error),
        }
    candidate_hash = stable_id("candidate-payload", row["candidate_json"])
    answer = candidate.get("answer") or {}
    question = str(candidate["question"])
    question_context = str(candidate.get("question_context") or "")
    provenance = candidate.get("provenance") or {}
    item = {
        "item_id": item_id,
        "candidate_hash": candidate_hash,
        "family_id": str(row["paper_family_id"]),
        "run_id": str(row["run_id"]),
        "source": candidate.get("source"),
        "schema_version": candidate.get("schema_version"),
        "generation_prompt_version": provenance.get("prompt_version"),
        "question": question,
        "question_context": question_context,
        "gold_text": str(answer["text"]),
        "distractors": [
            {
                "text": str(entry["text"]),
                "type": entry.get("type"),
                "verification_label": entry.get("label"),
                "order_rank": rank,
            }
            for rank, entry in enumerate(ordered[:k])
        ],
        "accepted_distractor_count": len(accepted),
        "distractor_order": order,
        "strata": {
            "numeric": answer.get("numeric_rule") is not None,
            "has_context": bool(question_context.strip()),
            "script": _script(question + " " + question_context),
            "schema_version": candidate.get("schema_version"),
            "generation_prompt_version": provenance.get("prompt_version"),
        },
        "construction_roles": construction_roles(candidate),
    }
    return item, None


def build_eval_set(
    *,
    state_db: Path,
    output_dir: Path,
    population: str,
    k: int = DEFAULT_CONTENT_OPTION_COUNT,
    contract_file: Path | None = None,
    production_run_prefix: str = DEFAULT_PRODUCTION_RUN_PREFIX,
    item_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Freeze one evaluation set and return its manifest."""
    if population not in POPULATIONS:
        raise ValueError(f"unsupported population: {population}")
    if isinstance(k, bool) or not isinstance(k, int) or k < 2:
        raise ValueError("the content option count k must be an integer of at least 2")
    contract: dict[str, str] | None = None
    if population == "current":
        if contract_file is None:
            raise ValueError("the current population requires a contract file")
        contract = load_contract(contract_file)
    if population == "list" and not item_ids:
        raise ValueError("the list population requires at least one item id")
    wanted = set(item_ids or [])
    population_record: dict[str, Any] = {"population": population, "k": k}
    if contract is not None:
        population_record["contract"] = contract
        population_record["contract_file_sha256"] = sha256_file(contract_file)  # type: ignore[arg-type]
    if population == "production":
        population_record["production_run_prefix"] = production_run_prefix
    if population == "list":
        population_record["item_ids"] = sorted(wanted)

    connection = db.connect_read_only(state_db)
    connection.row_factory = sqlite3.Row
    items: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    try:
        rows = connection.execute(
            "SELECT item_id,run_id,paper_family_id,candidate_json,status,updated_at "
            "FROM candidates WHERE status=? ORDER BY paper_family_id,updated_at,item_id",
            (ACCEPTED_STATUS,),
        ).fetchall()
        by_family: dict[str, tuple[sqlite3.Row, dict[str, Any], dict[str, Any]]] = {}
        seen_wanted: set[str] = set()
        for row in rows:
            item_id = str(row["item_id"])
            candidate = _json(row["candidate_json"], None)
            if not isinstance(candidate, dict):
                excluded.append({"item_id": item_id, "reason": "unreadable_candidate"})
                continue
            if population == "current" and not _matches_contract(candidate, contract):  # type: ignore[arg-type]
                continue
            if population == "production" and not str(row["run_id"]).startswith(
                production_run_prefix
            ):
                continue
            if population == "list":
                if item_id not in wanted:
                    continue
                seen_wanted.add(item_id)
            validation = connection.execute(
                "SELECT label,details_json FROM validation_events WHERE item_id=? "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            details = _json(validation["details_json"], {}) if validation else {}
            labels = details.get("labels", {}) if isinstance(details, dict) else {}
            if not (
                validation
                and validation["label"] == ACCEPTED_STATUS
                and isinstance(details, dict)
                and details.get("candidate_hash")
                == stable_id("candidate-payload", row["candidate_json"])
                and isinstance(labels, dict)
                and labels.get("mcq_eligible") is True
            ):
                excluded.append(
                    {"item_id": item_id, "reason": "invalid_or_unbound_validation"}
                )
                continue
            family_id = str(row["paper_family_id"])
            previous = by_family.get(family_id)
            if previous is not None:
                excluded.append(
                    {
                        "item_id": str(previous[0]["item_id"]),
                        "reason": "duplicate_family_superseded",
                        "family_id": family_id,
                        "superseded_by": item_id,
                    }
                )
            by_family[family_id] = (row, candidate, details)
        for item_id in sorted(wanted - seen_wanted):
            excluded.append({"item_id": item_id, "reason": "not_an_accepted_item"})
        for row, candidate, details in by_family.values():
            item, exclusion = _item_row(row, candidate, details, k=k)
            if item is not None:
                items.append(item)
            else:
                excluded.append(exclusion)  # type: ignore[arg-type]
    finally:
        connection.close()
    items.sort(key=lambda item: item["item_id"])
    excluded.sort(key=lambda entry: (entry["reason"], entry["item_id"]))
    return write_eval_set(
        items,
        excluded=excluded,
        output_dir=output_dir,
        population_record=population_record,
        k=k,
        state_db=state_db,
    )


def write_eval_set(
    items: list[dict[str, Any]],
    *,
    excluded: list[dict[str, Any]],
    output_dir: Path,
    population_record: dict[str, Any],
    k: int,
    state_db: Path | None,
) -> dict[str, Any]:
    """Write the frozen set directory and return the manifest."""
    for item in items:
        if len(item["distractors"]) != k:
            raise ValueError("every frozen item must carry exactly k distractors")
    items_bytes = jsonl_bytes(items)
    items_sha256 = sha256_bytes(items_bytes)
    # The prompt contract is part of the set identity. A changed contract, for
    # example the per-call option order of v2, therefore freezes a new set
    # directory instead of reusing an immutable manifest of the old contract.
    contract = prompt_contract()
    eval_set_id = stable_id(
        "abstention-eval-set",
        population_record,
        k,
        items_sha256,
        contract["prompt_sha256"],
        length=16,
    )
    set_dir = output_dir / eval_set_id
    atomic_write(set_dir / ITEMS_FILENAME, items_bytes, immutable=True)
    counts: dict[str, int] = {}
    for entry in excluded:
        counts[entry["reason"]] = counts.get(entry["reason"], 0) + 1
    assigned = [
        item["item_id"]
        for item in items
        if item["distractor_order"].get("assignment")
        == "assigned_by_evaluation_set_builder"
    ]
    manifest_path = set_dir / MANIFEST_FILENAME
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = {
        "schema": EVAL_SET_SCHEMA,
        "eval_set_id": eval_set_id,
        "created_at_utc": _utc_now(),
        "state_db": str(state_db) if state_db else None,
        "population": population_record,
        "k": k,
        "displayed_option_count": k + 1,
        "item_count": len(items),
        "items_file": ITEMS_FILENAME,
        "items_sha256": items_sha256,
        "item_hashes": {
            item["item_id"]: sha256_bytes(canonical_json(item).encode())
            for item in items
        },
        "distractor_order": {
            "contract_version": DISTRACTOR_ORDER_CONTRACT_VERSION,
            "drop_rule": "gold-present drops the last distractor of the order",
            "assigned_seed_rule": f"stable_id({ASSIGNED_SEED_LABEL!r}, item_id)",
            "assigned_item_ids": assigned,
            "recorded_item_ids": [
                item["item_id"] for item in items if item["item_id"] not in assigned
            ],
        },
        "prompt_contract": contract,
        "excluded": excluded,
        "exclusion_counts": counts,
        "construction_models": sorted(
            {model for item in items for model in item["construction_roles"]["models"]}
        ),
    }
    atomic_json(manifest_path, manifest, immutable=True)
    return manifest


def load_eval_set(set_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read one frozen set and verify its hashes."""
    manifest_path = set_dir / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != EVAL_SET_SCHEMA:
        raise ValueError("the evaluation set manifest schema is unsupported")
    items_path = set_dir / str(manifest["items_file"])
    data = items_path.read_bytes()
    if sha256_bytes(data) != manifest["items_sha256"]:
        raise ValueError("the evaluation set items changed after they were frozen")
    items = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
    if len(items) != manifest["item_count"]:
        raise ValueError("the evaluation set item count changed")
    for item in items:
        expected = manifest["item_hashes"].get(item["item_id"])
        if expected != sha256_bytes(canonical_json(item).encode()):
            raise ValueError("an evaluation set item changed after it was frozen")
    return manifest, items


def require_current_prompt_contract(manifest: dict[str, Any]) -> None:
    """Refuse to start a run on a set that another prompt contract froze.

    The evaluation set manifest is immutable and records the prompt contract
    of its build. A run renders its trials with the contract of the running
    code, so the two must agree. An older set stays readable, which keeps the
    scores of its own runs reproducible.
    """
    contract = manifest.get("prompt_contract") or {}
    if (
        contract.get("prompt_version") != PROMPT_VERSION
        or contract.get("prompt_sha256") != prompt_sha256()
    ):
        raise ValueError(
            "the evaluation set was frozen under another prompt contract: "
            f"{contract.get('prompt_version')} against {PROMPT_VERSION}"
        )


def manifest_sha256(set_dir: Path) -> str:
    return sha256_file(set_dir / MANIFEST_FILENAME)


def evaluation_identity(item: dict[str, Any]) -> dict[str, str]:
    """Return the ledger identity of one evaluated item.

    Evaluation calls bind their own paper family per item, so construction
    family rows and the per-paper construction cap never see evaluation spend.
    """
    item_id = str(item["item_id"])
    return {
        "paper_id": item_id,
        "family_id": f"evaluation-item:{item_id}",
        "source_version_id": f"evaluation-item:{item_id}:{item['candidate_hash']}",
    }
