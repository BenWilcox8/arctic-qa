from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from arctic_qa.db import Database
from arctic_qa.rerun_selection import build_rerun_selection


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def test_rerun_selection_puts_every_prior_paper_before_ranked_unseen(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    frozen_rows = []
    for position in range(1, 5):
        source = source_dir / f"source-{position}.pdf"
        extraction = source_dir / f"source-{position}.txt"
        source.write_bytes(f"source {position}".encode())
        extraction.write_text(f"finding {position}", encoding="utf-8")
        frozen_rows.append(
            {
                "schema": "full-text-ready-manifest-item-v1",
                "manifest_position": position,
                "candidate_key": f"paper-{position}",
                "doi": f"10.1234/paper-{position}",
                "family_key": f"paper-{position}",
                "title": f"Paper {position}",
                "authors": ["A. Author"],
                "year": 2025,
                "access_receipt": {
                    "access_state": "full_text_ready",
                    "identity_verified": True,
                    "source_path": str(source),
                    "source_sha256": _hash(source),
                    "source_bytes": source.stat().st_size,
                    "extraction_path": str(extraction),
                    "extraction_sha256": _hash(extraction),
                    "extraction_bytes": extraction.stat().st_size,
                    "media_type": "application/pdf",
                    "item_sha256": "a" * 64,
                },
            }
        )
    frozen = tmp_path / "freeze" / "manifest.jsonl"
    frozen.parent.mkdir()
    frozen.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in frozen_rows),
        encoding="utf-8",
    )
    descriptor = frozen.with_name("descriptor.json")
    _write_json(
        descriptor,
        {
            "schema": "full-text-ready-freeze-descriptor-v1",
            "state": "frozen_offline",
            "freeze_id": "freeze-r1",
            "counts": {"manifest_records": 4, "unique_paper_families": 4},
        },
    )
    quality = tmp_path / "quality.json"
    _write_json(
        quality,
        {
            "source_manifest_sha256": _hash(frozen),
            "records": [
                {"candidate_key": f"paper-{position}", "rank": position}
                for position in range(1, 5)
            ],
        },
    )
    bindings = {
        "paper-1": {
            "family_id": "family-prior-one",
            "source_version_id": frozen_rows[0]["access_receipt"]["source_sha256"],
        },
        "paper-3": {
            "family_id": "family-prior-three",
            "source_version_id": frozen_rows[2]["access_receipt"]["source_sha256"],
        },
    }
    ledger = tmp_path / "ledger.json"
    _write_json(
        ledger,
        {
            "paper_bindings": bindings,
            "family_bindings": {
                binding["family_id"]: {
                    "paper_id": paper_id,
                    "source_version_id": binding["source_version_id"],
                }
                for paper_id, binding in bindings.items()
            },
            "requests": {
                "request-one": {
                    "paper_id": "paper-1",
                    "run_id": "old-r1",
                    "stage": "eligibility",
                    "state": "completed",
                },
                "request-three": {
                    "paper_id": "paper-3",
                    "run_id": "old-r2",
                    "stage": "question_generation",
                    "state": "submitted",
                },
            },
            "spent_usd": "4.000000",
            "reserved_usd": "0.009047",
            "ambiguous_reserved_usd": "0.000000",
            "generation_submissions": 2,
        },
    )
    database_file = tmp_path / "state.sqlite3"
    database = Database(database_file)
    database.migrate(tmp_path / "backups")
    with database.transaction():
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "old-item",
                "old-r1",
                "old-source",
                "family-prior-one",
                "answer_first",
                json.dumps(
                    {
                        "provenance": {"prompt_version": "generation-prompt-v1"},
                        "finding_policy_version": "finding-v1",
                    }
                ),
                "rejected",
                "2026-09-01T00:00:00Z",
                "2026-09-01T00:00:00Z",
            ),
        )
    database.close()
    contract_dir = tmp_path / "contract"
    contract_dir.mkdir()
    prompt = contract_dir / "prompt.txt"
    schema = contract_dir / "schema.json"
    policy = contract_dir / "policy.json"
    prompt.write_text("prompt", encoding="utf-8")
    schema.write_text("schema", encoding="utf-8")
    policy.write_text("policy", encoding="utf-8")
    arguments = {
        "source_manifest_file": frozen,
        "descriptor_file": descriptor,
        "quality_order_file": quality,
        "ledger_file": ledger,
        "state_db_file": database_file,
        "output_dir": tmp_path / "release",
        "eligibility_run_dir": tmp_path / "eligibility-new",
        "eligibility_prompt_file": prompt,
        "eligibility_schema_file": schema,
        "eligibility_policy_file": policy,
        "producer_commit": "f" * 40,
        "invocation_run_id": "production-rerun-r1",
        "campaign_id": "production-campaign-r1",
        "limit": 3,
    }

    first = build_rerun_selection(**arguments)
    second = build_rerun_selection(**arguments)

    assert first == second
    selected = [
        json.loads(line)
        for line in Path(first["manifest"])
        .with_name("rerun-first-3.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["candidate_key"] for row in selected] == [
        "paper-1",
        "paper-3",
        "paper-2",
    ]
    assert [row["paper_family_id"] for row in selected[:2]] == [
        "family-prior-one",
        "family-prior-three",
    ]
    manifest = json.loads(Path(first["manifest"]).read_text(encoding="utf-8"))
    assert manifest["selection"] == {
        **manifest["selection"],
        "prior_evaluated_count": 2,
        "ranked_unseen_count": 1,
        "all_prior_evaluated_included": True,
        "prior_set_precedes_unseen": True,
    }
    assert manifest["accounting_before"]["unresolved_request_keys"] == [
        "request-three"
    ]
    assert manifest["prior_papers"][0]["prior_candidates"][0]["item_id"] == (
        "old-item"
    )
    assert manifest["prior_papers"][0]["new_attempt"]["state"] == (
        "selected_pending"
    )
    access_dir = Path(first["access_run"]["access_run_dir"])
    access_manifest = json.loads(
        (access_dir / "run-manifest.json").read_text(encoding="utf-8")
    )
    assert access_manifest["selection"][0]["paper_family_id"] == (
        "family-prior-one"
    )
