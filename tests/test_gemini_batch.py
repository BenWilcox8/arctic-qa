from __future__ import annotations

import json
import re
import urllib.error
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from arctic_qa.db import Database
from arctic_qa.discovery import manual_record
from arctic_qa.exporting import export_run
from arctic_qa import generation as generation_contract
from arctic_qa.gemini_batch import (
    AUTHORIZATION_SCHEMA,
    BatchPendingError,
    BatchProvider,
    BatchStore,
    ingest_results,
    prepare_pipeline,
    select_continuation,
    submit_round,
    _terminal_dispositions,
)
from arctic_qa.model_broker import (
    SharedGeminiBroker,
    broker_request_key,
    exclusive_batch_marker_path,
)
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


def shared_ledger(
    tmp_path: Path, transport: object | None = None
) -> tuple[SharedGeminiBroker, Path]:
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
            "authorized_new_run_id": "run-current",
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
        receipts_dir=tmp_path / "model-receipts",
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=transport or NoCallTransport(),
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
        self.created_models: list[str] = []

    def upload(self, path: Path, display_name: str) -> dict[str, Any]:
        self.calls.append("upload")
        assert path.is_file()
        return {"file": {"name": "files/test-input"}}

    def create(self, model: str, file_name: str, display_name: str) -> dict[str, Any]:
        self.calls.append("create")
        self.created_models.append(model)
        if self.fail_create:
            raise TimeoutError("unknown create outcome")
        return {"name": "batches/test-job", "state": "JOB_STATE_PENDING"}

    def get(self, job_name: str) -> dict[str, Any]:
        return {"name": job_name, "state": "JOB_STATE_PENDING"}

    def download(self, file_name: str) -> bytes:
        return b""


def test_batch_answer_judge_uses_flash_lite_price_and_payload(
    tmp_path: Path,
) -> None:
    _, ledger = shared_ledger(tmp_path)
    store = batch_store(tmp_path, ledger)
    provider = BatchProvider(
        store=store,
        phase="away_production",
        invocation_run_id="judge-batch-run",
    ).bind(
        paper_id="paper-one",
        family_id="family-one",
        source_version_id="a" * 64,
    )
    parameters = {
        "temperature": 0,
        "max_tokens": 4,
        "response_mime_type": "text/x.enum",
        "json_schema": {"type": "string", "enum": ["yes", "no"]},
    }

    with pytest.raises(BatchPendingError):
        provider.invoke("answer_judge", "System", "DATA\n{}", parameters, 30)

    state = store.read()
    key = next(iter(state["requests"]))
    request = store.prepared_record(key)
    assert request["stage"] == "answer_agreement"
    assert request["model"] == "gemini-2.5-flash-lite"
    assert request["batch_pricing"] == {
        "input_usd_per_million_tokens": "0.05",
        "output_usd_per_million_tokens_including_thinking": "0.20",
        "valid_through": "2026-12-31",
        "source": "https://ai.google.dev/gemini-api/docs/pricing",
    }
    generation = request["request"]["generationConfig"]
    assert generation["responseMimeType"] == "text/x.enum"
    assert generation["thinkingConfig"] == {"thinkingBudget": 0}
    manifest = store.make_round(
        run_identity={"run_id": "judge-batch-run"},
        ordered_inputs=[
            {
                "position": 1,
                "paper_id": "paper-one",
                "family_id": "family-one",
                "source_version_id": "a" * 64,
            }
        ],
    )
    assert manifest is not None
    assert manifest["model"] == "gemini-2.5-flash-lite"
    assert manifest["pricing"] == request["batch_pricing"]


class Http500Transport:
    def post(self, model: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
        if method == "countTokens":
            return {"totalTokens": 100}
        raise urllib.error.HTTPError(
            "https://fake.invalid", 500, "upstream failure", {}, None
        )


def ambiguous_hold(
    tmp_path: Path,
    *,
    paper_id: str = "paper-ambiguous",
    family_id: str = "family-ambiguous",
    source_version_id: str = "source-ambiguous",
) -> tuple[SharedGeminiBroker, Path, dict[str, Any]]:
    broker, ledger = shared_ledger(tmp_path, Http500Transport())
    payload = {
        "systemInstruction": {"parts": [{"text": "Return JSON."}]},
        "contents": [{"role": "user", "parts": [{"text": "Paper text."}]}],
        "generationConfig": {
            "candidateCount": 1,
            "responseMimeType": "application/json",
            "responseJsonSchema": {"type": "object"},
            "maxOutputTokens": 1000,
            "thinkingConfig": {"thinkingLevel": "medium"},
        },
        "store": False,
    }
    request_key = broker_request_key(
        model=broker.config["model"],
        run_id="run-current",
        phase="away_production",
        stage="question_generation",
        paper_id=paper_id,
        family_id=family_id,
        source_version_id=source_version_id,
        payload=payload,
    )
    receipt = broker.execute(
        phase="away_production",
        run_id="run-current",
        stage="question_generation",
        paper_id=paper_id,
        family_id=family_id,
        source_version_id=source_version_id,
        request_key=request_key,
        payload=payload,
    )
    evidence = tmp_path / "ambiguous-evidence.json"
    write_json(
        evidence,
        {
            "schema": "shared-paid-call-ambiguous-continuation-evidence-v1",
            "request_key": request_key,
            "error_class": "known_http_response_unknown_charge",
            "http_status": 500,
            "live_call_made": True,
            "received_receipt_absent": True,
            "actual_cost_known": False,
            "replay_prohibited": True,
            "affected_family_id": family_id,
            "authorized_run_id": "run-current",
        },
    )
    review = tmp_path / "review.md"
    review.write_text("Reviewed no-replay continuation.\n", encoding="utf-8")
    broker.authorize_ambiguous_continuation(
        request_key=request_key,
        expected_ledger_sha256=sha256_file(ledger),
        review_file=review,
        evidence_file=evidence,
        authorized_run_id="run-current",
        operator_id="batch-test",
    )
    return broker, ledger, receipt


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


def test_continuation_ignores_legacy_terminal_candidates(
    tmp_path: Path,
) -> None:
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")

    current_provenance = {
        "prompt_version": generation_contract.PROMPT_VERSION,
        "question_verification_contract_version": (
            generation_contract.QUESTION_VERIFICATION_CONTRACT_VERSION
        ),
        "numeric_rule_contract_version": generation_contract.NUMERIC_RULE_CONTRACT_VERSION,
        "direct_value_contract_version": (
            generation_contract.DIRECT_SOURCE_VALUE_CONTRACT_VERSION
        ),
        "scope_contract_version": generation_contract.SCOPE_CONTRACT_VERSION,
        "scope_role_semantics_version": generation_contract.SCOPE_ROLE_SEMANTICS_VERSION,
        "scope_role_binding_contract_version": (
            generation_contract.SCOPE_ROLE_BINDING_CONTRACT_VERSION
        ),
    }
    for stable_id, candidate in (
        (
            "legacy-paper",
            {"schema_version": "1.0", "provenance": {}},
        ),
        (
            "current-paper",
            {
                "schema_version": generation_contract.CANDIDATE_SCHEMA_VERSION,
                "finding_policy_version": (
                    generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION
                ),
                "provenance": current_provenance,
            },
        ),
    ):
        source = manual_record(
            {
                "stable_id": stable_id,
                "title": stable_id,
                "year": 2026,
                "discipline": "test",
                "paper_family_id": stable_id,
            },
            "test-only",
        )
        database.upsert_source(source)
        with database.transaction():
            database.connection.execute(
                """INSERT INTO candidates
                (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    f"item-{stable_id}",
                    "scientific-campaign",
                    source["source_id"],
                    stable_id,
                    "answer_first",
                    canonical_json(candidate),
                    "rejected",
                    "2026-09-14T00:00:00+00:00",
                    "2026-09-14T00:00:00+00:00",
                ),
            )

    dispositions = _terminal_dispositions(paths.database, "scientific-campaign")
    assert set(dispositions) == {"current-paper"}
    assert dispositions["current-paper"]["status"] == "rejected"
    database.close()


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


def test_batch_pipeline_reuses_bounded_question_revision_contract(
    tmp_path: Path,
) -> None:
    access, eligibility, policy = access_fixture(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    _, ledger = shared_ledger(tmp_path)
    store = batch_store(tmp_path, ledger)
    seen_stage_counts: dict[str, int] = {}

    def revised_response(record: dict[str, Any]) -> dict[str, Any]:
        payload = scripted_payload(record)
        prompt = record["request"]["contents"][0]["parts"][0]["text"]
        if record["role"] == "question_writer" and "QUESTION_REVISION" not in prompt:
            payload["question"] = (
                "What reported water depth was documented as 2.0 m?"
            )
        return {
            "responseId": f"batch-{record['request_key'][:12]}",
            "modelVersion": "gemini-3.8-flash",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": canonical_json(payload)}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 115,
            },
        }

    for _ in range(16):
        prepared = prepare_pipeline(
            database,
            paths.namespace,
            store=store,
            run_id="batch-invocation-revision-r1",
            campaign_id="scientific-campaign-revision-r1",
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
        results = tmp_path / f"{round_manifest['round_id']}.revision-results.jsonl"
        results.write_text(
            "".join(
                canonical_json(
                    {"key": record["request_key"], "response": revised_response(record)}
                )
                + "\n"
                for record in reversed(request_records)
            ),
            encoding="utf-8",
        )
        ingest_results(store, round_manifest["round_id"], results)
    else:
        pytest.fail("the batch revision pipeline did not finish")

    assert seen_stage_counts == {
        "eligibility": 1,
        "question_generation": 2,
        "finding_answer_extraction": 1,
        "blinded_reconstruction": 2,
        "answer_verification": 2,
        "distractor_generation": 1,
        "option_verification": 4,
    }
    assert database.one(
        "SELECT COUNT(*) AS count FROM candidates "
        "WHERE run_id=? AND status='machine_accepted_unverified'",
        ("scientific-campaign-revision-r1",),
    )["count"] == 1


def test_batch_allocation_is_separate_from_shared_live_spend(tmp_path: Path) -> None:
    _, ledger_path = shared_ledger(tmp_path)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["prior_construction_spend_usd"] = "24.99"
    write_json(ledger_path, ledger)
    store = batch_store(tmp_path, ledger_path, ceiling="25")
    provider = BatchProvider(
        store=store,
        phase="away_production",
        invocation_run_id="separate-allocation-run",
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
            {"temperature": 0, "max_tokens": 2048, "json_schema": {"type": "object"}},
            30,
        )
    round_manifest = store.make_round(
        run_identity={"run_id": "separate-allocation-run"},
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
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    auth_path = authorization(manifest_path, ledger_path, tmp_path / "auth.json")
    authorized = store.authorize_round(
        manifest, json.loads(auth_path.read_text(encoding="utf-8"))
    )
    preview = store.budget_preview(manifest["request_keys"])
    assert Decimal(preview["shared_used_usd"]) == Decimal("24.99")
    assert Decimal(preview["batch_allocation_usd"]) == Decimal("25")
    assert Decimal(preview["projected_batch_total_usd"]) == Decimal(
        manifest["reserved_cost_usd"]
    )
    assert authorized["batch_allocation_usd"] == "25"


def test_batch_allows_covered_recovery_liability_and_retains_its_cost(
    tmp_path: Path,
) -> None:
    _, ledger_path, ambiguous = ambiguous_hold(tmp_path)
    store = batch_store(tmp_path, ledger_path, ceiling="25")
    provider = BatchProvider(
        store=store,
        phase="away_production",
        invocation_run_id="batch-with-recovery-hold",
    ).bind(
        paper_id="paper-batch",
        family_id="family-batch",
        source_version_id="b" * 64,
    )
    with pytest.raises(BatchPendingError):
        provider.invoke(
            "question_writer",
            "system",
            "prompt",
            {"temperature": 0, "max_tokens": 2048, "json_schema": {"type": "object"}},
            30,
        )
    manifest = store.make_round(
        run_identity={"run_id": "batch-with-recovery-hold"},
        ordered_inputs=[
            {
                "position": 1,
                "paper_id": "paper-batch",
                "family_id": "family-batch",
                "source_version_id": "b" * 64,
            }
        ],
    )
    assert manifest is not None
    manifest_path = Path(manifest["manifest_path"])
    authorization_path = authorization(manifest_path, ledger_path, tmp_path / "auth.json")
    preview = store.budget_preview(manifest["request_keys"])
    expected_shared = Decimal(ambiguous["reserved_usd"])
    assert Decimal(preview["shared_used_usd"]) == expected_shared
    assert Decimal(preview["projected_total_usd"]) == (
        expected_shared + Decimal(preview["new_reservation_usd"])
    )
    authorized = store.authorize_round(
        json.loads(manifest_path.read_text(encoding="utf-8")),
        json.loads(authorization_path.read_text(encoding="utf-8")),
    )
    assert authorized["shared_used_usd"] == str(expected_shared)


def test_batch_rejects_an_uncovered_retained_liability(tmp_path: Path) -> None:
    _, ledger_path = shared_ledger(tmp_path)
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["reserved_usd"] = "0.010000"
    write_json(ledger_path, ledger)
    with pytest.raises(ValueError, match="no-replay|recovery|liability"):
        batch_store(tmp_path, ledger_path, ceiling="25").budget_preview([])


def test_batch_rejects_integrity_halt_even_with_recovery_receipt(
    tmp_path: Path,
) -> None:
    _, ledger_path, _ = ambiguous_hold(tmp_path)
    write_json(
        ledger_path.with_name(f".{ledger_path.name}.integrity-halt.json"),
        {"schema": "shared-paid-call-integrity-halt-v1", "reason": "test"},
    )
    with pytest.raises(ValueError, match="integrity"):
        batch_store(tmp_path, ledger_path, ceiling="25").budget_preview([])


def test_continuation_selector_accepts_a_validated_recovery_hold(
    tmp_path: Path,
) -> None:
    _, ledger_path, _ = ambiguous_hold(
        tmp_path,
        paper_id="paper-1",
        family_id="family-1",
        source_version_id="a" * 64,
    )
    access = tmp_path / "ranked-access"
    write_json(
        access / "run-manifest.json",
        {
            "schema": "article-access-manifest-v1",
            "run_id": "ranked-one",
            "target_total": 1,
            "selection": [
                {
                    "position": 1,
                    "candidate_key": "paper-1",
                    "family_key": "family-1",
                    "source_content_hash": "a" * 64,
                    "extraction_sha256": "b" * 64,
                }
            ],
        },
    )
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    database.close()
    progress = tmp_path / "progress.json"
    write_json(progress, {"state": "error"})

    selected = select_continuation(
        access_run_dir=access,
        db_file=paths.database,
        campaign_id="scientific-campaign",
        prior_run_id="run-current",
        eligibility_run_dir=tmp_path / "eligibility",
        shared_ledger_file=ledger_path,
        production_progress_file=progress,
        output_file=tmp_path / "continuation.json",
        require_stopped=True,
    )

    assert selected["provisional"] is False
    assert selected["remaining_selection"] == []
