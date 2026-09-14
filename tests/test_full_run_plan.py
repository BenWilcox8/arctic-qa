from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from arctic_qa.full_run_plan import build_plan


REPO = Path(__file__).resolve().parents[1]


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
    for name in (
        "budget",
        "price",
        "gate",
        "prompt",
        "schema",
        "policy",
        "transition",
    ):
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
        "config_transition_file": config / "transition",
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
    transition_arg = first["stream_command_argv"].index(
        "--ledger-config-transition-file"
    )
    assert first["stream_command_argv"][transition_arg + 1] == str(
        (config / "transition").resolve()
    )
    assert first["generation_configuration"]["config_transition"]["sha256"]


def test_cli_materializes_frozen_jsonl_as_supported_access_run(tmp_path: Path) -> None:
    originals = tmp_path / "originals"
    source = originals / "source.pdf"
    extraction = originals / "text.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-1.4\nfixture\n")
    extraction.write_text("A source-supported Arctic finding.\n", encoding="utf-8")
    source_hash = sha256(source.read_bytes()).hexdigest()
    extraction_hash = sha256(extraction.read_bytes()).hexdigest()
    frozen = tmp_path / "freeze" / "manifest.jsonl"
    frozen.parent.mkdir(parents=True)
    frozen.write_text(
        json.dumps(
            {
                "schema": "full-text-ready-manifest-item-v1",
                "manifest_position": 1,
                "candidate_key": "10.1234/frozen-one",
                "doi": "10.1234/frozen-one",
                "family_key": "10.1234/frozen-one",
                "title": "Frozen Arctic source",
                "authors": ["A. Author"],
                "year": 2024,
                "priority_tier": "positive_arctic_or_marine_cue",
                "tier_position": 1,
                "scientific_eligibility": "unresolved",
                "access_receipt": {
                    "access_state": "full_text_ready",
                    "identity_verified": True,
                    "source_path": str(source),
                    "source_sha256": source_hash,
                    "source_bytes": source.stat().st_size,
                    "extraction_path": str(extraction),
                    "extraction_sha256": extraction_hash,
                    "extraction_bytes": extraction.stat().st_size,
                    "media_type": "application/pdf",
                    "item_sha256": "a" * 64,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    descriptor = tmp_path / "freeze" / "descriptor.json"
    _write_json(
        descriptor,
        {
            "schema": "full-text-ready-freeze-descriptor-v1",
            "state": "frozen_offline",
            "freeze_id": "frozen-one-r1",
            "counts": {"manifest_records": 1, "unique_paper_families": 1},
        },
    )
    ledger = tmp_path / "shared-ledger.json"
    _write_json(
        ledger,
        {
            "spent_usd": "1.25",
            "reserved_usd": "0",
            "prior_construction_spend_usd": "0",
        },
    )
    _write_json(ledger.with_name("shared-ledger.status.json"), {"state": "ready"})
    config = tmp_path / "config"
    for name in ("budget", "price", "gate", "prompt", "schema", "policy"):
        (config / name).parent.mkdir(parents=True, exist_ok=True)
        (config / name).write_text(name, encoding="utf-8")
    access = tmp_path / "materialized-access"
    plan_path = tmp_path / "plan.json"
    command = [
        sys.executable,
        "-m",
        "arctic_qa.full_run_plan",
        "--json-out",
        str(plan_path),
        "--data-root",
        str(tmp_path / "data-root"),
        "--access-run-dir",
        str(tmp_path / "unused-access"),
        "--eligibility-run-dir",
        str(tmp_path / "eligibility"),
        "--run-id",
        "future-scientific-r1",
        "--campaign-id",
        "future-campaign-r1",
        "--credential-file",
        str(tmp_path / "private" / "gemini.key"),
        "--shared-ledger-file",
        str(ledger),
        "--model-receipts-dir",
        str(tmp_path / "receipts"),
        "--streaming-budget-policy-file",
        str(config / "budget"),
        "--price-config-file",
        str(config / "price"),
        "--execution-gate-file",
        str(config / "gate"),
        "--eligibility-prompt-file",
        str(config / "prompt"),
        "--eligibility-schema-file",
        str(config / "schema"),
        "--eligibility-policy-file",
        str(config / "policy"),
        "--planning-cumulative-budget-usd",
        "20",
        "--frozen-source-manifest-file",
        str(frozen),
        "--frozen-manifest-descriptor-file",
        str(descriptor),
        "--materialized-access-run-dir",
        str(access),
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO / "src")
    before = {
        path: path.read_bytes() for path in (source, extraction, frozen, descriptor)
    }

    first = subprocess.run(
        command,
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert first.returncode == 0, first.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    manifest = json.loads((access / "run-manifest.json").read_text(encoding="utf-8"))
    item = json.loads((access / "items" / "item-000001.json").read_text())
    assert plan["stream_command_argv"] is not None
    access_arg = plan["stream_command_argv"].index("--access-run-dir")
    assert plan["stream_command_argv"][access_arg + 1] == str(access.resolve())
    assert plan["input"]["target_total"] == 1
    assert manifest["schema"] == "article-access-manifest-v1"
    assert manifest["target_total"] == 1
    assert manifest["selection"][0]["candidate_key"] == "10.1234/frozen-one"
    assert item["schema"] == "article-access-item-v1"
    assert item["position"] == 1
    assert item["source_content_hash"] == source_hash
    assert item["extraction_sha256"] == extraction_hash
    assert item["source_path"] == str(source)
    assert item["extraction_path"] == str(extraction)
    assert not (access / "originals").exists()
    assert {path: path.read_bytes() for path in before} == before

    second = subprocess.run(
        command,
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr

    eligibility = tmp_path / "eligibility"
    criteria = [
        {
            "criterion_id": criterion,
            "status": "satisfied",
            "reason_codes": ["test_only_evidence"],
            "evidence": [],
            "missing_context": [],
        }
        for criterion in (
            "published_primary_findings",
            "stable_identity_version",
            "access_rights_evidence",
        )
    ]
    criteria.extend(
        [
            {
                "criterion_id": "study_geography",
                "status": "failed",
                "reason_codes": ["outside_arctic_boundary"],
                "evidence": [],
                "missing_context": [],
            },
            {
                "criterion_id": "correction_retraction_coverage",
                "status": "uncertain",
                "reason_codes": ["coverage_unknown"],
                "evidence": [],
                "missing_context": ["correction_retraction_coverage:unknown"],
            },
        ]
    )
    _write_json(
        eligibility / "jobs" / "fixture.json",
        {
            "schema": "gemini-eligibility-job-v1",
            "job_key": "fixture-job",
            "candidate_key": "10.1234/frozen-one",
            "model": "fake-gemini-3.8-flash",
            "state": "completed",
            "source_content_hash": source_hash,
            "extraction_sha256": extraction_hash,
            "parsed_response": {
                "schema_version": "eligibility-response-v1",
                "request_id": "fixture-job",
                "overall": "excluded",
                "overall_reason_codes": ["criterion_failed:study_geography"],
                "criteria": criteria,
                "known_missing_context": ["correction_retraction_coverage:unknown"],
                "correction_metadata_used": {
                    "provided": False,
                    "known_status": "unknown",
                    "source": None,
                    "as_of": None,
                },
                "input_echo": {
                    "policy_sha256": "test-only",
                    "source_version_sha256": source_hash,
                    "extracted_text_sha256": extraction_hash,
                    "metadata_sha256": "test-only",
                },
            },
            "validation": {
                "valid": True,
                "errors": [],
                "decision": "excluded",
                "resolved_evidence": [],
            },
        },
    )
    stream_root = tmp_path / "stream-data"
    stream_root.mkdir()
    stream = subprocess.run(
        [
            sys.executable,
            "-m",
            "arctic_qa",
            "--data-root",
            str(stream_root),
            "--test-mode",
            "--json",
            "stream",
            "--phase",
            "offline",
            "--run-id",
            "materialized-stream-smoke-r1",
            "--campaign-id",
            "materialized-stream-smoke-campaign-r1",
            "--access-run-dir",
            str(access),
            "--eligibility-run-dir",
            str(eligibility),
            "--author-script",
            str(REPO / "fixtures" / "fake-author.jsonl"),
            "--verifier-script",
            str(REPO / "fixtures" / "fake-verifier.jsonl"),
            "--max-papers",
            "1",
        ],
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert stream.returncode == 0, stream.stderr
    stream_result = json.loads(stream.stdout)
    assert stream_result["counts"]["processed"] == 1
    assert stream_result["counts"]["eligibility_rejected"] == 1
    assert stream_result["counts"]["accepted_base_questions"] == 0
