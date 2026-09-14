"""Write a deterministic, non-activating plan for a future streaming run."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .util import atomic_json, canonical_json, sha256_bytes, sha256_file


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _file_identity(path: Path, *, required: bool = True) -> dict[str, str | None]:
    resolved = path.resolve()
    if not resolved.is_file():
        if required:
            raise ValueError(f"required file is missing: {resolved}")
        return {"path": str(resolved), "sha256": None}
    return {"path": str(resolved), "sha256": sha256_file(resolved)}


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def _ledger_summary(
    ledger: dict[str, Any], planning_cap: Decimal
) -> dict[str, str | None]:
    spent = Decimal(str(ledger.get("spent_usd", "0")))
    reserved = Decimal(str(ledger.get("reserved_usd", "0")))
    return {
        "spent_usd": _decimal_text(spent),
        "reserved_usd": _decimal_text(reserved),
        "prior_construction_spend_usd": str(
            ledger.get("prior_construction_spend_usd", "unknown")
        ),
        "remaining_to_planning_cap_usd": _decimal_text(
            max(Decimal("0"), planning_cap - spent - reserved)
        ),
    }


def _source_records(
    manifest: dict[str, Any],
    *,
    selected_count: int,
) -> list[dict[str, Any]]:
    selection = manifest.get("selection")
    if not isinstance(selection, list):
        raise ValueError("the article-access selection is missing")
    target_total = manifest.get("target_total")
    if not isinstance(target_total, int) or len(selection) != target_total:
        raise ValueError("the ordered selection count is inconsistent")

    records: list[dict[str, Any]] = []
    for expected_position, selected in enumerate(selection[:selected_count], start=1):
        if not isinstance(selected, dict):
            raise ValueError("the ordered selection has an invalid record")
        candidate_key = selected.get("candidate_key")
        if not isinstance(candidate_key, str) or not candidate_key:
            raise ValueError("the ordered selection has no candidate key")
        if selected.get("position") != expected_position:
            raise ValueError("the ordered selection position is inconsistent")
        source_id = selected.get("source_id") or selected.get("doi") or candidate_key
        records.append(
            {
                "position": expected_position,
                "source_id": str(source_id),
                "candidate_key": candidate_key,
                "source_content_hash": selected.get("source_content_hash"),
                "extraction_sha256": selected.get("extraction_sha256"),
            }
        )
    return records


def _frozen_source_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for expected_position, line in enumerate(handle, start=1):
            selected = json.loads(line)
            if (
                not isinstance(selected, dict)
                or selected.get("manifest_position") != expected_position
            ):
                raise ValueError("the frozen source manifest position is inconsistent")
            candidate_key = selected.get("candidate_key")
            if not isinstance(candidate_key, str) or not candidate_key:
                raise ValueError("the frozen source manifest has no candidate key")
            receipt = selected.get("access_receipt")
            if not isinstance(receipt, dict):
                raise ValueError("the frozen source manifest has no access receipt")
            records.append(
                {
                    "position": expected_position,
                    "source_id": str(selected.get("doi") or candidate_key),
                    "candidate_key": candidate_key,
                    "source_content_hash": receipt.get("source_sha256"),
                    "extraction_sha256": receipt.get("extraction_sha256"),
                }
            )
    if not records:
        raise ValueError("the frozen source manifest is empty")
    return records


def build_frozen_manifest_draft(
    *,
    source_manifest_file: Path,
    descriptor_file: Path,
    run_id: str,
    campaign_id: str,
    ledger_file: Path,
    budget_policy_file: Path,
    price_config_file: Path,
    execution_gate_file: Path,
    planning_cumulative_budget_usd: Decimal,
    phase: str = "away_production",
) -> dict[str, Any]:
    """Return a planning-only draft for an immutable JSONL source freeze."""
    if not run_id or not campaign_id or planning_cumulative_budget_usd <= 0:
        raise ValueError("future IDs and a positive planning budget are required")
    if phase not in {"live_test", "away_production"}:
        raise ValueError("the planned phase must be live_test or away_production")
    descriptor = _read_json(descriptor_file)
    sources = _frozen_source_records(source_manifest_file)
    expected_total = (descriptor.get("counts") or {}).get("manifest_records")
    if expected_total != len(sources):
        raise ValueError(
            "the frozen descriptor count does not match its source manifest"
        )
    ledger = _read_json(ledger_file)
    return {
        "schema": "arctic-qa-full-run-plan-v1",
        "planning_only": True,
        "activation": {"state": "not_activated", "money_spent_by_plan_usd": "0.000000"},
        "future_scientific_run": {
            "run_id": run_id,
            "campaign_id": campaign_id,
            "phase": phase,
        },
        "input": {
            "frozen_source_manifest": _file_identity(source_manifest_file),
            "frozen_manifest_descriptor": _file_identity(descriptor_file),
            "freeze_id": descriptor.get("freeze_id"),
            "target_total": len(sources),
            "ordered_sources": sources,
        },
        "selection": {
            "method": "frozen_manifest_order",
            "seed": None,
            "explicit_smaller_selection": False,
            "planned_source_count": len(sources),
        },
        "generation_configuration": {
            "generation_arm": "answer_first",
            "provider": "shared_gemini_broker",
            "price_config": _file_identity(price_config_file),
            "budget_policy": _file_identity(budget_policy_file),
            "execution_gate": _file_identity(execution_gate_file),
        },
        "budget_and_ledger": {
            "planning_cumulative_cap_usd": _decimal_text(
                planning_cumulative_budget_usd
            ),
            "funding_state": "not_authorized_by_plan",
            "ledger": _file_identity(ledger_file),
            "ledger_status": _file_identity(
                ledger_file.with_name(f"{ledger_file.stem}.status.json"), required=False
            ),
            **_ledger_summary(ledger, planning_cumulative_budget_usd),
        },
        "stream_command_argv": None,
        "stream_command_status": "Builder must materialize this frozen JSONL input as a supported access run before stream execution.",
    }


def materialize_frozen_access_run(
    *,
    source_manifest_file: Path,
    descriptor_file: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Write a supported access run that references an immutable source freeze."""
    descriptor = _read_json(descriptor_file)
    if (
        descriptor.get("schema") != "full-text-ready-freeze-descriptor-v1"
        or descriptor.get("state") != "frozen_offline"
        or not descriptor.get("freeze_id")
    ):
        raise ValueError("the frozen manifest descriptor is invalid")
    frozen_rows = _frozen_source_records(source_manifest_file)
    expected_total = (descriptor.get("counts") or {}).get("manifest_records")
    if expected_total != len(frozen_rows):
        raise ValueError(
            "the frozen descriptor count does not match its source manifest"
        )

    source_manifest_sha256 = sha256_file(source_manifest_file)
    descriptor_sha256 = sha256_file(descriptor_file)
    run_id = output_dir.resolve().name
    if not run_id:
        raise ValueError("the materialized access run has no run ID")
    selection: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    candidate_keys: set[str] = set()
    family_keys: set[str] = set()
    with source_manifest_file.open(encoding="utf-8") as handle:
        for position, line in enumerate(handle, start=1):
            frozen = json.loads(line)
            candidate_key = frozen["candidate_key"]
            family_key = str(frozen.get("family_key") or candidate_key)
            if candidate_key in candidate_keys:
                raise ValueError("the frozen source manifest has a duplicate candidate")
            candidate_keys.add(candidate_key)
            family_keys.add(family_key)
            receipt = frozen["access_receipt"]
            source_path = Path(str(receipt.get("source_path") or ""))
            extraction_path = Path(str(receipt.get("extraction_path") or ""))
            source_sha256 = receipt.get("source_sha256")
            extraction_sha256 = receipt.get("extraction_sha256")
            if (
                receipt.get("access_state") != "full_text_ready"
                or receipt.get("identity_verified") is not True
                or not source_path.is_absolute()
                or not source_path.is_file()
                or not extraction_path.is_absolute()
                or not extraction_path.is_file()
                or not isinstance(source_sha256, str)
                or len(source_sha256) != 64
                or not isinstance(extraction_sha256, str)
                or len(extraction_sha256) != 64
            ):
                raise ValueError("the frozen source receipt is not ready")
            subgroup = "frozen_full_text_manifest"
            selected = {
                "authors": frozen.get("authors") or [],
                "candidate_key": candidate_key,
                "doi": frozen.get("doi"),
                "extraction_sha256": extraction_sha256,
                "family_key": family_key,
                "frozen_manifest_position": position,
                "position": position,
                "priority_tier": frozen.get("priority_tier"),
                "priority_tier_position": frozen.get("tier_position"),
                "scientific_eligibility": frozen.get(
                    "scientific_eligibility", "unresolved"
                ),
                "source_content_hash": source_sha256,
                "subgroup": subgroup,
                "title": frozen.get("title"),
                "year": frozen.get("year"),
            }
            item = {
                **selected,
                "access_state": "full_text_ready",
                "extraction_bytes": receipt.get("extraction_bytes"),
                "extraction_coverage": {
                    "article_body_recognized": True,
                    "figures": "unknown_not_extracted",
                    "ocr": "unknown",
                    "supplements": "unknown_not_extracted",
                    "tables": "unknown_not_extracted",
                },
                "extraction_path": str(extraction_path),
                "final_url": (
                    f"https://doi.org/{frozen['doi']}"
                    if frozen.get("doi")
                    else source_path.as_uri()
                ),
                "identity_verified": True,
                "license": None,
                "media_type": receipt.get("media_type"),
                "run_id": run_id,
                "schema": "article-access-item-v1",
                "source_bytes": receipt.get("source_bytes"),
                "source_path": str(source_path),
                "upstream_access_position": frozen.get("upstream_access_position"),
                "upstream_item_sha256": receipt.get("item_sha256"),
            }
            selection.append(selected)
            items.append(item)

    expected_families = (descriptor.get("counts") or {}).get("unique_paper_families")
    if expected_families != len(family_keys):
        raise ValueError("the frozen descriptor family count is inconsistent")
    selection_keys_sha256 = sha256_bytes(
        canonical_json([row["candidate_key"] for row in selection]).encode()
    )
    manifest = {
        "schema": "article-access-manifest-v1",
        "run_id": run_id,
        "target_total": len(selection),
        "selection_keys_sha256": selection_keys_sha256,
        "remaining_order_sha256": selection_keys_sha256,
        "frozen_manifest_sha256": source_manifest_sha256,
        "frozen_manifest_descriptor_sha256": descriptor_sha256,
        "freeze_id": descriptor["freeze_id"],
        "purpose": "Full scientific-run planning from the frozen full-text manifest.",
        "scientific_eligibility_effect": "none",
        "selection": selection,
    }
    output_dir = output_dir.resolve()
    atomic_json(output_dir / "run-manifest.json", manifest, immutable=True)
    for position, item in enumerate(items, start=1):
        atomic_json(
            output_dir / "items" / f"item-{position:06d}.json",
            item,
            immutable=True,
        )
    counts = {
        "checked": len(items),
        "full_text_ready": len(items),
        "ready_for_eligibility": len(items),
        "target": len(items),
    }
    progress = {
        "schema": "article-access-progress-v1",
        "state": "completed",
        "run_id": run_id,
        "current_stage": "materialized_from_frozen_manifest",
        "counts": counts,
        "model_calls": 0,
        "paid_calls": 0,
    }
    atomic_json(output_dir / "progress.json", progress, immutable=True)
    receipt = {
        "schema": "article-access-run-receipt-v1",
        "state": "completed",
        "run_id": run_id,
        "run_manifest_sha256": sha256_file(output_dir / "run-manifest.json"),
        "frozen_manifest_sha256": source_manifest_sha256,
        "frozen_manifest_descriptor_sha256": descriptor_sha256,
        "selection_keys_sha256": selection_keys_sha256,
        "remaining_order_sha256": selection_keys_sha256,
        "counts": counts,
        "model_calls": 0,
        "paid_calls": 0,
        "scientific_eligibility_effect": "none",
    }
    atomic_json(output_dir / "run-receipt.json", receipt, immutable=True)
    return {
        "access_run_dir": str(output_dir),
        "run_id": run_id,
        "target_total": len(items),
        "run_manifest_sha256": receipt["run_manifest_sha256"],
        "frozen_manifest_sha256": source_manifest_sha256,
        "selection_keys_sha256": selection_keys_sha256,
    }


def build_plan(
    *,
    data_root: Path,
    access_run_dir: Path,
    eligibility_run_dir: Path,
    run_id: str,
    campaign_id: str,
    credential_file: Path,
    ledger_file: Path,
    model_receipts_dir: Path,
    budget_policy_file: Path,
    price_config_file: Path,
    execution_gate_file: Path,
    eligibility_prompt_file: Path,
    eligibility_schema_file: Path,
    eligibility_policy_file: Path,
    planning_cumulative_budget_usd: Decimal,
    config_transition_file: Path | None = None,
    max_papers: int | None = None,
    selection_seed: str | None = None,
    phase: str = "away_production",
) -> dict[str, Any]:
    """Read immutable inputs and return a plan without activating the stream."""
    if not run_id or not campaign_id:
        raise ValueError("run and campaign IDs are required")
    if planning_cumulative_budget_usd <= 0:
        raise ValueError("the planning cumulative budget must be positive")
    if phase not in {"live_test", "away_production"}:
        raise ValueError("the planned phase must be live_test or away_production")

    access_run_dir = access_run_dir.resolve()
    eligibility_run_dir = eligibility_run_dir.resolve()
    access_manifest_file = access_run_dir / "run-manifest.json"
    completion_receipt_file = access_run_dir / "run-receipt.json"
    progress_file = access_run_dir / "progress.json"
    access_manifest = _read_json(access_manifest_file)
    if _read_json(progress_file).get("state") != "completed":
        raise ValueError("the article-access run is not complete")
    _file_identity(completion_receipt_file)
    access_run_id = access_manifest.get("run_id")
    if run_id == access_run_id or campaign_id == access_run_id:
        raise ValueError(
            "the future scientific run identity must differ from the access run"
        )

    target_total = access_manifest.get("target_total")
    if not isinstance(target_total, int) or target_total < 1:
        raise ValueError("the article-access target total is invalid")
    if max_papers is None:
        selected_count = target_total
        selection = {
            "method": "manifest_order",
            "seed": None,
            "explicit_smaller_selection": False,
        }
    else:
        if max_papers < 1 or max_papers > target_total:
            raise ValueError("the selected paper count is outside the manifest range")
        if max_papers < target_total and not selection_seed:
            raise ValueError("a smaller selection requires an explicit selection seed")
        selected_count = max_papers
        selection = {
            "method": "manifest_order_prefix",
            "seed": selection_seed,
            "explicit_smaller_selection": max_papers < target_total,
        }

    sources = _source_records(access_manifest, selected_count=selected_count)
    ledger_identity = _file_identity(ledger_file)
    ledger = _read_json(ledger_file)
    status_identity = _file_identity(
        ledger_file.with_name(f"{ledger_file.stem}.status.json"), required=False
    )
    configuration = {
        "generation_arm": "answer_first",
        "provider": "shared_gemini_broker",
        "price_config": _file_identity(price_config_file),
        "budget_policy": _file_identity(budget_policy_file),
        "execution_gate": _file_identity(execution_gate_file),
        "eligibility_prompt": _file_identity(eligibility_prompt_file),
        "eligibility_schema": _file_identity(eligibility_schema_file),
        "eligibility_policy": _file_identity(eligibility_policy_file),
        "config_transition": (
            _file_identity(config_transition_file)
            if config_transition_file is not None
            else None
        ),
    }
    stream_command = [
        "python",
        "-m",
        "arctic_qa",
        "--json",
        "--data-root",
        str(data_root.resolve()),
        "stream",
        "--phase",
        phase,
        "--run-id",
        run_id,
        "--campaign-id",
        campaign_id,
        "--access-run-dir",
        str(access_run_dir),
        "--eligibility-run-dir",
        str(eligibility_run_dir),
        "--eligibility-prompt-file",
        str(eligibility_prompt_file.resolve()),
        "--eligibility-schema-file",
        str(eligibility_schema_file.resolve()),
        "--eligibility-policy-file",
        str(eligibility_policy_file.resolve()),
        "--credential-file",
        str(credential_file.resolve()),
        "--prior-construction-spend-usd",
        str(ledger.get("prior_construction_spend_usd", "0")),
        "--shared-ledger-file",
        str(ledger_file.resolve()),
        "--model-receipts-dir",
        str(model_receipts_dir.resolve()),
        "--streaming-budget-policy-file",
        str(budget_policy_file.resolve()),
        "--price-config-file",
        str(price_config_file.resolve()),
        "--execution-gate-file",
        str(execution_gate_file.resolve()),
        "--max-papers",
        str(selected_count),
    ]
    if config_transition_file is not None:
        stream_command.extend(
            [
                "--ledger-config-transition-file",
                str(config_transition_file.resolve()),
            ]
        )
    return {
        "schema": "arctic-qa-full-run-plan-v1",
        "planning_only": True,
        "activation": {
            "state": "not_activated",
            "money_spent_by_plan_usd": "0.000000",
            "warning": "This plan does not authorize or start provider requests.",
        },
        "future_scientific_run": {
            "run_id": run_id,
            "campaign_id": campaign_id,
            "phase": phase,
        },
        "input": {
            "access_manifest": _file_identity(access_manifest_file),
            "access_completion_receipt": _file_identity(completion_receipt_file),
            "access_run_id": access_run_id,
            "target_total": target_total,
            "eligibility_run_dir": str(eligibility_run_dir),
            "eligibility_run_manifest": _file_identity(
                eligibility_run_dir / "run-manifest.json", required=False
            ),
            "ordered_sources": sources,
        },
        "selection": {**selection, "planned_source_count": selected_count},
        "generation_configuration": configuration,
        "budget_and_ledger": {
            "planning_cumulative_cap_usd": _decimal_text(
                planning_cumulative_budget_usd
            ),
            "funding_state": "not_authorized_by_plan",
            "ledger": ledger_identity,
            "ledger_status": status_identity,
            "model_receipts_dir": str(model_receipts_dir.resolve()),
            **_ledger_summary(ledger, planning_cumulative_budget_usd),
        },
        "stream_command_argv": stream_command,
        "quality_summary_handoff": {
            "interface": "python -m arctic_qa.quality_summary",
            "required_inputs": [
                "selected export manifest",
                "shared ledger",
                "ledger status",
            ],
            "warning": "Run the report only after the stream writes an accepted export manifest.",
        },
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Write an offline Arctic QA full-run plan."
    )
    result.add_argument("--json-out", type=Path, required=True)
    result.add_argument("--data-root", type=Path, required=True)
    result.add_argument("--access-run-dir", type=Path, required=True)
    result.add_argument("--eligibility-run-dir", type=Path, required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--campaign-id", required=True)
    result.add_argument("--credential-file", type=Path, required=True)
    result.add_argument("--shared-ledger-file", type=Path, required=True)
    result.add_argument("--model-receipts-dir", type=Path, required=True)
    result.add_argument("--streaming-budget-policy-file", type=Path, required=True)
    result.add_argument("--price-config-file", type=Path, required=True)
    result.add_argument("--execution-gate-file", type=Path, required=True)
    result.add_argument("--ledger-config-transition-file", type=Path)
    result.add_argument("--eligibility-prompt-file", type=Path, required=True)
    result.add_argument("--eligibility-schema-file", type=Path, required=True)
    result.add_argument("--eligibility-policy-file", type=Path, required=True)
    result.add_argument(
        "--planning-cumulative-budget-usd", type=Decimal, default=Decimal("20")
    )
    result.add_argument("--max-papers", type=int)
    result.add_argument("--selection-seed")
    result.add_argument(
        "--phase", choices=("live_test", "away_production"), default="away_production"
    )
    result.add_argument("--frozen-source-manifest-file", type=Path)
    result.add_argument("--frozen-manifest-descriptor-file", type=Path)
    result.add_argument("--materialized-access-run-dir", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.materialized_access_run_dir and not args.frozen_source_manifest_file:
        raise ValueError("access materialization requires both frozen manifest files")
    if args.frozen_source_manifest_file or args.frozen_manifest_descriptor_file:
        if (
            not args.frozen_source_manifest_file
            or not args.frozen_manifest_descriptor_file
        ):
            raise ValueError("both frozen manifest files are required")
        if args.materialized_access_run_dir:
            materialization = materialize_frozen_access_run(
                source_manifest_file=args.frozen_source_manifest_file,
                descriptor_file=args.frozen_manifest_descriptor_file,
                output_dir=args.materialized_access_run_dir,
            )
            plan = build_plan(
                data_root=args.data_root,
                access_run_dir=args.materialized_access_run_dir,
                eligibility_run_dir=args.eligibility_run_dir,
                run_id=args.run_id,
                campaign_id=args.campaign_id,
                credential_file=args.credential_file,
                ledger_file=args.shared_ledger_file,
                model_receipts_dir=args.model_receipts_dir,
                budget_policy_file=args.streaming_budget_policy_file,
                price_config_file=args.price_config_file,
                execution_gate_file=args.execution_gate_file,
                eligibility_prompt_file=args.eligibility_prompt_file,
                eligibility_schema_file=args.eligibility_schema_file,
                eligibility_policy_file=args.eligibility_policy_file,
                planning_cumulative_budget_usd=args.planning_cumulative_budget_usd,
                config_transition_file=args.ledger_config_transition_file,
                max_papers=args.max_papers,
                selection_seed=args.selection_seed,
                phase=args.phase,
            )
            descriptor = _read_json(args.frozen_manifest_descriptor_file)
            plan["input"].update(
                {
                    "freeze_id": descriptor["freeze_id"],
                    "frozen_source_manifest": _file_identity(
                        args.frozen_source_manifest_file
                    ),
                    "frozen_manifest_descriptor": _file_identity(
                        args.frozen_manifest_descriptor_file
                    ),
                    "materialization": materialization,
                }
            )
            plan["selection"]["method"] = "frozen_manifest_order"
            plan["stream_command_status"] = "supported_not_activated"
        else:
            plan = build_frozen_manifest_draft(
                source_manifest_file=args.frozen_source_manifest_file,
                descriptor_file=args.frozen_manifest_descriptor_file,
                run_id=args.run_id,
                campaign_id=args.campaign_id,
                ledger_file=args.shared_ledger_file,
                budget_policy_file=args.streaming_budget_policy_file,
                price_config_file=args.price_config_file,
                execution_gate_file=args.execution_gate_file,
                planning_cumulative_budget_usd=args.planning_cumulative_budget_usd,
                phase=args.phase,
            )
    else:
        plan = build_plan(
            data_root=args.data_root,
            access_run_dir=args.access_run_dir,
            eligibility_run_dir=args.eligibility_run_dir,
            run_id=args.run_id,
            campaign_id=args.campaign_id,
            credential_file=args.credential_file,
            ledger_file=args.shared_ledger_file,
            model_receipts_dir=args.model_receipts_dir,
            budget_policy_file=args.streaming_budget_policy_file,
            price_config_file=args.price_config_file,
            execution_gate_file=args.execution_gate_file,
            eligibility_prompt_file=args.eligibility_prompt_file,
            eligibility_schema_file=args.eligibility_schema_file,
            eligibility_policy_file=args.eligibility_policy_file,
            planning_cumulative_budget_usd=args.planning_cumulative_budget_usd,
            config_transition_file=args.ledger_config_transition_file,
            max_papers=args.max_papers,
            selection_seed=args.selection_seed,
            phase=args.phase,
        )
    args.json_out.write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
