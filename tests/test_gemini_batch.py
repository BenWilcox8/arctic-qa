from __future__ import annotations

import json
import re
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from arctic_qa.db import Database
from arctic_qa.exporting import export_run
from arctic_qa.gemini_batch import (
    AUTHORIZATION_SCHEMA,
    BatchPendingError,
    BatchProvider,
    BatchStore,
    ingest_results,
    prepare_pipeline,
    select_continuation,
    submit_round,
)
from arctic_qa.model_broker import SharedGeminiBroker, exclusive_batch_marker_path
from arctic_qa.paths import DataPaths
from arctic_qa.util import canonical_json, sha256_file


REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


class NoCallTransport:
    def post(self, model: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("no interactive provider call is permitted")


def shared_ledger(tmp_path: Path) -> tuple[SharedGeminiBroker, Path]:
    review = tmp_path / "review.txt"
    review.write_text("offline test review\n", encoding="utf-8")
    gate = tmp_path / "gate.json"
    write_json(
        gate,
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": True,
            "allowed_phase": "away_production",
            "independent_review_verdict": "pass",
            "integrated_code_commit": "test-only",
            "review_record": str(review),
            "review_record_sha256": sha256_file(review),
        },
    )
    credential = tmp_path / "credential"
    credential.write_text("test-only\n", encoding="utf-8")
    credential.chmod(0o600)
    ledger = tmp_path / "shared-ledger.json"
    broker = SharedGeminiBroker(
        policy_file=REPO / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=REPO / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=gate,
        ledger_file=ledger,
        receipts_dir=tmp_path / "shared-receipts",
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=NoCallTransport(),
    )
    return broker, ledger


def batch_store(tmp_path: Path, ledger: Path, *, ceiling: str = "250") -> BatchStore:
    return BatchStore(
        tmp_path / f"batch-{ceiling.replace('.', '-')}",
        price_config_file=REPO / "config" / "gemini-eligibility-v1.json",
        shared_ledger_file=ledger,
        overall_ceiling_usd=Decimal(ceiling),
        maximum_request_usd=Decimal("0.25"),
        maximum_paper_usd=Decimal("1"),
    )


class FakeBatchTransport:
    def __init__(self, *, fail_create: bool = False) -> None:
        self.fail_create = fail_create
        self.calls: list[str] = []

    def upload(self, path: Path, display_name: str) -> dict[str, Any]:
        self.calls.append("upload")
        assert path.is_file()
        return {"file": {"name": "files/test-input"}}

    def create(self, model: str, file_name: str, display_name: str) -> dict[str, Any]:
        self.calls.append("create")
        if self.fail_create:
            raise TimeoutError("unknown create outcome")
        return {"name": "batches/test-job", "state": "JOB_STATE_PENDING"}

    def get(self, job_name: str) -> dict[str, Any]:
        return {"name": job_name, "state": "JOB_STATE_PENDING"}

    def download(self, file_name: str) -> bytes:
        return b""


def authorization(manifest_path: Path, ledger: Path, destination: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    write_json(
        destination,
        {
            "schema": AUTHORIZATION_SCHEMA,
            "round_manifest_sha256": sha256_file(manifest_path),
            "shared_ledger_sha256": sha256_file(ledger),
            "maximum_reserved_cost_usd": manifest["reserved_cost_usd"],
            "authorized_by": "offline-test",
        },
    )
    return destination


def test_budget_refusal_precedes_transport_and_uncertain_submit_is_not_replayed(
    tmp_path: Path,
) -> None:
    broker, ledger = shared_ledger(tmp_path)
    original_ledger_hash = sha256_file(ledger)
    limited = batch_store(tmp_path, ledger, ceiling="0.000001")
    provider = BatchProvider(
        store=limited,
        phase="away_production",
        invocation_run_id="future-batch-run",
    ).bind(
        paper_id="paper-one",
        family_id="family-one",
        source_version_id="a" * 64,
    )
    with pytest.raises(BatchPendingError):
        provider.invoke(
            "question_writer",
            "system",
            "prompt",
            {
                "temperature": 0,
                "max_tokens": 2048,
                "json_schema": {"type": "object"},
            },
            30,
        )
    round_manifest = limited.make_round(
        run_identity={"run_id": "future-batch-run"},
        ordered_inputs=[
            {
                "position": 1,
                "paper_id": "paper-one",
                "family_id": "family-one",
                "source_version_id": "a" * 64,
            }
        ],
    )
    assert round_manifest is not None
    manifest_path = Path(round_manifest["manifest_path"])
    auth = authorization(manifest_path, ledger, tmp_path / "limited-auth.json")
    transport = FakeBatchTransport()

    provisional_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    provisional_manifest["run_identity"]["continuation_plan_provisional"] = True
    provisional_path = tmp_path / "provisional-manifest.json"
    write_json(provisional_path, provisional_manifest)
    provisional_auth = authorization(
        provisional_path, ledger, tmp_path / "provisional-auth.json"
    )
    with pytest.raises(ValueError, match="provisional continuation"):
        limited.authorize_round(
            provisional_manifest,
            json.loads(provisional_auth.read_text(encoding="utf-8")),
        )
    assert transport.calls == []

    with pytest.raises(ValueError, match="construction ceiling"):
        submit_round(limited, manifest_path, auth, transport)

    assert transport.calls == []
    assert sha256_file(ledger) == original_ledger_hash

    store = batch_store(tmp_path, ledger)
    provider = replace_store(provider, store)
    with pytest.raises(BatchPendingError):
        provider.invoke(
            "question_writer",
            "system",
            "prompt",
            {
                "temperature": 0,
                "max_tokens": 2048,
                "json_schema": {"type": "object"},
            },
            30,
        )
    round_manifest = store.make_round(
        run_identity={"run_id": "future-batch-run"},
        ordered_inputs=[
            {
                "position": 1,
                "paper_id": "paper-one",
                "family_id": "family-one",
                "source_version_id": "a" * 64,
            }
        ],
    )
    assert round_manifest is not None
    manifest_path = Path(round_manifest["manifest_path"])
    auth = authorization(manifest_path, ledger, tmp_path / "auth.json")
    uncertain = FakeBatchTransport(fail_create=True)

    with pytest.raises(TimeoutError, match="unknown create outcome"):
        submit_round(store, manifest_path, auth, uncertain)

    marker = json.loads(exclusive_batch_marker_path(ledger).read_text(encoding="utf-8"))
    assert marker["batch_identity"] == store.batch_identity
    key = round_manifest["request_keys"][0]
    request = store.prepared_record(key)
    with pytest.raises(ValueError, match="exclusive Gemini batch mode"):
        broker.execute(
            phase=request["phase"],
            run_id=request["run_id"],
            stage=request["stage"],
            paper_id=request["paper_id"],
            family_id=request["family_id"],
            source_version_id=request["source_version_id"],
            request_key=key,
            payload=request["request"],
        )
    assert store.read()["rounds"][round_manifest["round_id"]]["state"] == "submitting"
    with pytest.raises(ValueError, match="not ready"):
        submit_round(store, manifest_path, auth, uncertain)
    assert uncertain.calls == ["upload", "create"]


def replace_store(provider: BatchProvider, store: BatchStore) -> BatchProvider:
    return BatchProvider(
        store=store,
        phase=provider.phase,
        invocation_run_id=provider.invocation_run_id,
        paper_id=provider.paper_id,
        family_id=provider.family_id,
        source_version_id=provider.source_version_id,
    )


def test_shuffled_partial_results_keep_missing_and_error_liability_after_restart(
    tmp_path: Path,
) -> None:
    _, ledger = shared_ledger(tmp_path)
    store = batch_store(tmp_path, ledger)
    ordered_inputs = []
    for position in range(1, 4):
        paper_id = f"paper-{position}"
        family_id = f"family-{position}"
        source_version_id = str(position) * 64
        ordered_inputs.append(
            {
                "position": position,
                "paper_id": paper_id,
                "family_id": family_id,
                "source_version_id": source_version_id,
            }
        )
        provider = BatchProvider(
            store=store,
            phase="away_production",
            invocation_run_id="future-batch-run",
        ).bind(
            paper_id=paper_id,
            family_id=family_id,
            source_version_id=source_version_id,
        )
        with pytest.raises(BatchPendingError):
            provider.invoke(
                "question_writer",
                "system",
                f"prompt-{position}",
                {
                    "temperature": 0,
                    "max_tokens": 2048,
                    "json_schema": {"type": "object"},
                },
                30,
            )
    round_manifest = store.make_round(
        run_identity={"run_id": "future-batch-run"},
        ordered_inputs=ordered_inputs,
    )
    assert round_manifest is not None
    manifest_path = Path(round_manifest["manifest_path"])
    auth = authorization(manifest_path, ledger, tmp_path / "partial-auth.json")
    submit_round(store, manifest_path, auth, FakeBatchTransport())
    keys = round_manifest["request_keys"]
    success = store.prepared_record(keys[2])
    success_response = {
        "responseId": "partial-success",
        "modelVersion": "gemini-3.8-flash",
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"parts": [{"text": canonical_json({"ok": True})}]},
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 2,
            "thoughtsTokenCount": 1,
            "totalTokenCount": 13,
        },
    }
    results = tmp_path / "partial-results.jsonl"
    results.write_text(
        canonical_json({"key": success["request_key"], "response": success_response})
        + "\n"
        + canonical_json(
            {"key": keys[0], "error": {"code": 500, "message": "test error"}}
        )
        + "\n",
        encoding="utf-8",
    )

    ingested = ingest_results(store, round_manifest["round_id"], results)

    assert ingested["completed_keys"] == [keys[2]]
    assert ingested["error_keys"] == [keys[0]]
    assert ingested["missing_keys"] == [keys[1]]
    restarted = batch_store(tmp_path, ledger)
    preview = restarted.budget_preview([])
    assert Decimal(preview["batch_actual_usd"]) > 0
    assert Decimal(preview["batch_unsettled_liability_usd"]) == sum(
        Decimal(restarted.read()["requests"][key]["reserved_usd"])
        for key in (keys[0], keys[1])
    )

    later = tmp_path / "later-results.jsonl"
    later.write_text(
        canonical_json({"key": keys[2], "response": success_response})
        + "\n"
        + canonical_json({"key": keys[1], "response": success_response})
        + "\n",
        encoding="utf-8",
    )
    later_ingested = ingest_results(restarted, round_manifest["round_id"], later)

    assert later_ingested["completed_keys"] == sorted((keys[1], keys[2]))
    assert later_ingested["error_keys"] == [keys[0]]
    assert later_ingested["missing_keys"] == []
    assert not later_ingested["idempotent"]
    second_restart = batch_store(tmp_path, ledger)
    second_preview = second_restart.budget_preview([])
    assert Decimal(second_preview["batch_unsettled_liability_usd"]) == Decimal(
        second_restart.read()["requests"][keys[0]]["reserved_usd"]
    )


def test_continuation_selector_preserves_ranked_order_and_logs_processed_ids(
    tmp_path: Path,
) -> None:
    access = tmp_path / "ranked-access"
    selection = [
        {
            "position": position,
            "candidate_key": f"paper-{position}",
            "family_key": f"family-{position}",
            "source_content_hash": str(position) * 64,
            "extraction_sha256": chr(96 + position) * 64,
        }
        for position in range(1, 4)
    ]
    write_json(
        access / "run-manifest.json",
        {
            "schema": "article-access-manifest-v1",
            "run_id": "ranked-top-three",
            "target_total": 3,
            "selection": selection,
        },
    )
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    database.close()
    _, ledger_path = shared_ledger(tmp_path)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    request_key = "f" * 64
    ledger["requests"][request_key] = {
        "request_key": request_key,
        "run_id": "first-production",
        "stage": "eligibility",
        "paper_id": "paper-2",
        "family_id": "family-2",
        "source_version_id": "2" * 64,
        "state": "completed",
    }
    write_json(ledger_path, ledger)
    eligibility = tmp_path / "eligibility"
    write_json(
        eligibility / "jobs" / "paper-2.json",
        {
            "candidate_key": "paper-2",
            "execution_authority": "shared_gemini_broker",
            "state": "completed",
            "broker_request_key": request_key,
            "broker_receipt_sha256": "e" * 64,
            "source_content_hash": "2" * 64,
            "validation": {"decision": "excluded"},
        },
    )
    progress = tmp_path / "progress.json"
    write_json(progress, {"state": "running"})

    provisional = select_continuation(
        access_run_dir=access,
        db_file=paths.database,
        campaign_id="scientific-campaign",
        prior_run_id="first-production",
        eligibility_run_dir=eligibility,
        shared_ledger_file=ledger_path,
        production_progress_file=progress,
        output_file=tmp_path / "provisional.json",
    )

    assert provisional["provisional"] is True
    assert [row["position"] for row in provisional["remaining_selection"]] == [1, 3]
    assert provisional["excluded_processed"] == [
        {
            **selection[1],
            "disposition": "eligibility_excluded",
            "eligibility": {
                "decision": "excluded",
                "job_file": str((eligibility / "jobs" / "paper-2.json").resolve()),
                "job_file_sha256": sha256_file(eligibility / "jobs" / "paper-2.json"),
                "broker_request_key": request_key,
                "broker_receipt_sha256": "e" * 64,
            },
            "terminal_candidate": None,
            "receipts": [
                {
                    "request_key": request_key,
                    "stage": "eligibility",
                    "state": "completed",
                }
            ],
        }
    ]
    with pytest.raises(ValueError, match="settled stop"):
        select_continuation(
            access_run_dir=access,
            db_file=paths.database,
            campaign_id="scientific-campaign",
            prior_run_id="first-production",
            eligibility_run_dir=eligibility,
            shared_ledger_file=ledger_path,
            production_progress_file=progress,
            output_file=tmp_path / "must-stop.json",
            require_stopped=True,
        )

    write_json(progress, {"state": "error"})
    final = select_continuation(
        access_run_dir=access,
        db_file=paths.database,
        campaign_id="scientific-campaign",
        prior_run_id="first-production",
        eligibility_run_dir=eligibility,
        shared_ledger_file=ledger_path,
        production_progress_file=progress,
        output_file=tmp_path / "final.json",
        require_stopped=True,
    )
    assert final["provisional"] is False


def access_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    access = tmp_path / "access"
    eligibility = tmp_path / "eligibility"
    source = access / "originals" / "source.html"
    source.parent.mkdir(parents=True)
    source.write_bytes((FIXTURES / "public-source.html").read_bytes())
    extracted = access / "extracted" / "text.txt"
    extracted.parent.mkdir(parents=True)
    extracted.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    candidate_key = "test-only:batch-paper"
    source_hash = sha256(source.read_bytes()).hexdigest()
    extraction_hash = sha256(extracted.read_bytes()).hexdigest()
    write_json(
        access / "items" / "item-000001.json",
        {
            "schema": "article-access-item-v1",
            "run_id": "access-fixture",
            "position": 1,
            "candidate_key": candidate_key,
            "subgroup": "test_only",
            "title": "Synthetic public Arctic extraction fixture",
            "doi": None,
            "access_state": "full_text_ready",
            "identity_verified": True,
            "source_path": str(source),
            "source_content_hash": source_hash,
            "extraction_path": str(extracted),
            "extraction_sha256": extraction_hash,
            "extraction_coverage": {"article_body_recognized": True},
            "media_type": "text/html",
            "final_url": "https://example.invalid/public-source.html",
            "license": "CC0-1.0",
        },
    )
    write_json(
        access / "run-manifest.json",
        {
            "run_id": "access-fixture",
            "target_total": 1,
            "selection": [
                {
                    "position": 1,
                    "candidate_key": candidate_key,
                    "subgroup": "test_only",
                    "authors": ["Arctic QA tests"],
                    "year": 2026,
                }
            ],
        },
    )
    policy = tmp_path / "eligibility-policy.json"
    write_json(policy, {"protocol_id": "test-only-policy"})
    return access, eligibility, policy


def replace(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {key: replace(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [replace(item, old, new) for item in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def scripted_payload(record: dict[str, Any]) -> dict[str, Any]:
    prompt = record["request"]["contents"][0]["parts"][0]["text"]
    if record["role"] == "eligibility":
        request_id = re.search(r"^request_id: (.+)$", prompt, re.M).group(1)
        input_hashes = json.loads(
            re.search(r"^input_hashes: (.+)$", prompt, re.M).group(1)
        )
        correction = json.loads(
            re.search(r"^correction_metadata: (.+)$", prompt, re.M).group(1)
        )
        metadata = json.loads(re.search(r"^metadata: (.+)$", prompt, re.M).group(1))
        evidence = [
            {
                "quote": "The complete study site was at 71.3 N.",
                "locator": {
                    "source_block_id": "text-block-00001",
                    "page_id": None,
                    "section_id": "extracted-text",
                },
            }
        ]
        return {
            "schema_version": "eligibility-response-v1",
            "request_id": request_id,
            "overall": "eligible",
            "overall_reason_codes": ["all_required_criteria_satisfied"],
            "criteria": [
                {
                    "criterion_id": criterion,
                    "status": "satisfied",
                    "reason_codes": ["test_only_evidence"],
                    "evidence": evidence,
                    "missing_context": [],
                }
                for criterion in (
                    "published_primary_findings",
                    "stable_identity_version",
                    "study_geography",
                    "access_rights_evidence",
                )
            ]
            + [
                {
                    "criterion_id": "correction_retraction_coverage",
                    "status": "uncertain",
                    "reason_codes": ["coverage_unknown"],
                    "evidence": [],
                    "missing_context": metadata["known_context_gaps"],
                }
            ],
            "known_missing_context": metadata["known_context_gaps"],
            "correction_metadata_used": correction,
            "input_echo": input_hashes,
        }
    events = []
    for fixture in ("fake-author.jsonl", "fake-verifier.jsonl"):
        events.extend(
            json.loads(line)
            for line in (FIXTURES / fixture).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    event = next(
        item
        for item in events
        if item["role"] == record["role"]
        and all(marker in prompt for marker in item.get("require_prompt_contains", []))
        and all(
            marker not in prompt for marker in item.get("forbid_prompt_contains", [])
        )
    )
    payload = json.loads(canonical_json(event["response"]))
    span = re.search(r'"span_id":"([^"]+)"', prompt)
    if span:
        payload = replace(payload, "{{span_id}}", span.group(1))
    chunk = re.search(r'"chunk_id":"([^"]+)"', prompt)
    if chunk:
        payload = replace(payload, "{{chunk_id}}", chunk.group(1))
    return payload


def provider_response(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "responseId": f"batch-{record['request_key'][:12]}",
        "modelVersion": "gemini-3.8-flash",
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [{"text": canonical_json(scripted_payload(record))}]
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 10,
            "thoughtsTokenCount": 5,
            "totalTokenCount": 115,
        },
    }


def test_staged_batch_pipeline_uses_real_prompts_and_exports_accepted_output(
    tmp_path: Path,
) -> None:
    access, eligibility, policy = access_fixture(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    _, ledger = shared_ledger(tmp_path)
    store = batch_store(tmp_path, ledger)
    original_ledger_hash = sha256_file(ledger)
    seen_stage_counts: dict[str, int] = {}

    for _ in range(12):
        prepared = prepare_pipeline(
            database,
            paths.namespace,
            store=store,
            run_id="batch-invocation-r1",
            campaign_id="scientific-campaign-r1",
            access_run_dir=access,
            eligibility_run_dir=eligibility,
            eligibility_prompt_file=REPO
            / "config"
            / "gemini-eligibility-prompt-v3.txt",
            eligibility_schema_file=REPO
            / "schemas"
            / "gemini-eligibility.v1.schema.json",
            eligibility_policy_file=policy,
        )
        if prepared["counts"]["accepted"] == 1:
            break
        round_manifest = prepared["round"]
        assert round_manifest is not None
        request_records = [
            store.prepared_record(key) for key in round_manifest["request_keys"]
        ]
        for record in request_records:
            seen_stage_counts[record["stage"]] = (
                seen_stage_counts.get(record["stage"], 0) + 1
            )
        results = tmp_path / f"{round_manifest['round_id']}.results.jsonl"
        rows = [
            {"key": record["request_key"], "response": provider_response(record)}
            for record in reversed(request_records)
        ]
        results.write_text(
            "".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8"
        )
        first = ingest_results(store, round_manifest["round_id"], results)
        second = ingest_results(store, round_manifest["round_id"], results)
        assert first["error_keys"] == []
        assert second["completed_keys"] == sorted(round_manifest["request_keys"])
    else:
        pytest.fail("the staged batch pipeline did not finish")

    exported = export_run(
        database, paths.namespace, "scientific-campaign-r1", seed="streaming-20260912"
    )
    assert exported["short_answer_count"] == 1
    assert exported["mcq_count"] == 2
    assert seen_stage_counts == {
        "eligibility": 1,
        "finding_answer_extraction": 1,
        "question_generation": 1,
        "blinded_reconstruction": 1,
        "answer_verification": 1,
        "distractor_generation": 1,
        "option_verification": 4,
    }
    assert sha256_file(ledger) == original_ledger_hash
    option_requests = [
        store.prepared_record(key)
        for key, row in store.read()["requests"].items()
        if row["stage"] == "option_verification"
    ]
    assert len(option_requests) == 4
    assert all(
        "VERIFICATION_BINDING" in row["request"]["contents"][0]["parts"][0]["text"]
        for row in option_requests
    )
    candidate = database.one("SELECT candidate_json FROM candidates")
    value = json.loads(candidate["candidate_json"])
    assert value["question_rationale"]
    assert all(item["generation_rationale"] for item in value["distractors"])
