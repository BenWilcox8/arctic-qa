"""Write a deterministic, non-activating plan for a future streaming run."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .util import sha256_file


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
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
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
