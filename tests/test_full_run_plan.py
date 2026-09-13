from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from arctic_qa.full_run_plan import build_plan


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_plan_is_deterministic_and_never_changes_its_inputs(tmp_path: Path) -> None:
    access = tmp_path / "access"
    _write_json(
        access / "run-manifest.json",
        {
            "run_id": "access-r1",
            "target_total": 2,
            "selection": [
                {
                    "candidate_key": "doi:one",
                    "doi": "doi:one",
                    "position": 1,
                    "subgroup": "a",
                    "source_content_hash": "source-one",
                    "extraction_sha256": "extract-1",
                },
                {
                    "candidate_key": "doi:two",
                    "doi": "doi:two",
                    "position": 2,
                    "subgroup": "b",
                    "source_content_hash": "source-two",
                    "extraction_sha256": "extract-2",
                },
            ],
        },
    )
    _write_json(access / "progress.json", {"state": "completed"})
    _write_json(access / "run-receipt.json", {"state": "completed"})
    eligibility = tmp_path / "eligibility"
    eligibility.mkdir()
    ledger = tmp_path / "shared-ledger.json"
    _write_json(
        ledger,
        {
            "spent_usd": "1.25",
            "reserved_usd": "0.25",
            "prior_construction_spend_usd": "0",
        },
    )
    status = ledger.with_name("shared-ledger.status.json")
    _write_json(status, {"state": "ready"})
    config = tmp_path / "config"
    for name in ("budget", "price", "gate", "prompt", "schema", "policy"):
        (config / name).parent.mkdir(parents=True, exist_ok=True)
        (config / name).write_text(name, encoding="utf-8")

    inputs = [path for path in tmp_path.rglob("*") if path.is_file()]
    before = {path: path.read_bytes() for path in inputs}
    kwargs = {
        "data_root": tmp_path / "data-root",
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "run_id": "future-scientific-r1",
        "campaign_id": "future-campaign-r1",
        "credential_file": tmp_path / "private" / "gemini.key",
        "ledger_file": ledger,
        "model_receipts_dir": tmp_path / "receipts",
        "budget_policy_file": config / "budget",
        "price_config_file": config / "price",
        "execution_gate_file": config / "gate",
        "eligibility_prompt_file": config / "prompt",
        "eligibility_schema_file": config / "schema",
        "eligibility_policy_file": config / "policy",
        "planning_cumulative_budget_usd": Decimal("20"),
    }

    first = build_plan(**kwargs)
    second = build_plan(**kwargs)

    assert first == second
    assert {path: path.read_bytes() for path in inputs} == before
    assert first["planning_only"] is True
    assert first["activation"]["money_spent_by_plan_usd"] == "0.000000"
    assert first["budget_and_ledger"]["ledger_status"]["path"] == str(status)
    assert first["budget_and_ledger"]["ledger_status"]["sha256"] is not None
    assert [source["source_id"] for source in first["input"]["ordered_sources"]] == [
        "doi:one",
        "doi:two",
    ]
    assert first["stream_command_argv"][0:4] == ["python", "-m", "arctic_qa", "--json"]
