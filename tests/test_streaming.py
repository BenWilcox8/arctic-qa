from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from arctic_qa import cli as cli_module
from arctic_qa.broker_provider import BrokerProvider
from arctic_qa.db import Database
from arctic_qa.errors import AmbiguousChargeError
from arctic_qa.generation import ROLE_SCHEMAS
from arctic_qa.model_broker import SharedGeminiBroker
from arctic_qa.paths import DataPaths
from arctic_qa.providers import FakeProvider
from arctic_qa.streaming import run_stream
from arctic_qa.util import sha256_file, stable_id


REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def run_cli(root: Path, *arguments: str, expected: int = 0) -> dict:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "arctic_qa",
            "--data-root",
            str(root),
            "--test-mode",
            "--json",
            *arguments,
        ],
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected, result.stderr or result.stdout
    return json.loads(result.stdout if expected == 0 else result.stderr)


def streaming_fixture(tmp_path: Path) -> tuple[Path, Path]:
    access = tmp_path / "access"
    eligibility = tmp_path / "eligibility"
    source = access / "originals" / "source.html"
    source.parent.mkdir(parents=True)
    source.write_bytes((FIXTURES / "public-source.html").read_bytes())
    extracted = access / "extracted" / "text.txt"
    extracted.parent.mkdir(parents=True)
    extracted.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    candidate_key = "test-only:streaming-paper"
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
                    "authors": ["Arctic QA test suite"],
                    "year": 2026,
                }
            ],
        },
    )
    write_json(access / "progress.json", {"state": "completed"})
    write_json(access / "run-receipt.json", {"state": "completed"})
    write_json(
        eligibility / "jobs" / "fixture-job.json",
        {
            "schema": "gemini-eligibility-job-v1",
            "job_key": "fixture-job",
            "candidate_key": candidate_key,
            "model": "gemini-3.8-flash",
            "state": "completed",
            "source_content_hash": source_hash,
            "extraction_sha256": extraction_hash,
            "parsed_response": {
                "schema_version": "eligibility-response-v1",
                "request_id": "fixture-job",
                "overall": "eligible",
                "overall_reason_codes": ["all_required_criteria_satisfied"],
                "criteria": [
                    {
                        "criterion_id": criterion,
                        "status": "satisfied",
                        "reason_codes": ["test_only_evidence"],
                        "evidence": [
                            {
                                "quote": "The complete study site was at 71.3 N.",
                                "locator": {
                                    "source_block_id": "text-block-00001",
                                    "page_id": None,
                                    "section_id": "extracted-text",
                                },
                            }
                        ],
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
                        "missing_context": ["correction_retraction_coverage:unknown"],
                    }
                ],
                "known_missing_context": ["correction_retraction_coverage:unknown"],
                "correction_metadata_used": {
                    "provided": False,
                    "source": None,
                    "as_of": None,
                    "known_status": "unknown",
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
                "decision": "eligible",
                "resolved_evidence": [
                    {
                        "criterion": "study_geography",
                        "locator": {
                            "source_block_id": "text-block-00001",
                            "page_id": None,
                            "section_id": "extracted-text",
                        },
                        "quote": "The complete study site was at 71.3 N.",
                        "start": 0,
                        "end": 39,
                    }
                ],
            },
            "actual_cost_usd": "0.01",
        },
    )
    return access, eligibility


class ScriptedBrokerTransport:
    def __init__(self, *, verifier_script: Path | None = None) -> None:
        self.author = FakeProvider("gemini-3.8-flash", FIXTURES / "fake-author.jsonl")
        self.verifier = FakeProvider(
            "gemini-3.8-flash", verifier_script or FIXTURES / "fake-verifier.jsonl"
        )
        self.methods: list[str] = []
        self.custody_paths: tuple[Path, Path, Path] | None = None
        self.custody_checks = 0

    def post(self, model: str, method: str, body: dict) -> dict:
        if self.custody_paths is not None:
            progress_path, status_path, policy_path = self.custody_paths
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            assert progress["broker_status_sha256"] == sha256_file(status_path)
            assert progress["budget_policy_sha256"] == sha256_file(policy_path)
            self.custody_checks += 1
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 100}
        generation = body["generationConfig"]
        schema = generation["responseJsonSchema"]
        if "criteria" in schema.get("properties", {}):
            user_text = body["contents"][0]["parts"][0]["text"]
            request_id = re.search(r"^request_id: (.+)$", user_text, re.M).group(1)
            input_hashes = json.loads(
                re.search(r"^input_hashes: (.+)$", user_text, re.M).group(1)
            )
            correction = json.loads(
                re.search(r"^correction_metadata: (.+)$", user_text, re.M).group(1)
            )
            metadata = json.loads(
                re.search(r"^metadata: (.+)$", user_text, re.M).group(1)
            )
            missing = metadata["known_context_gaps"]
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
            payload = {
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
                        "missing_context": missing,
                    }
                ],
                "known_missing_context": missing,
                "correction_metadata_used": correction,
                "input_echo": input_hashes,
            }
            return self._response(model, payload, "fixture-eligibility")
        role = next(
            name for name, expected in ROLE_SCHEMAS.items() if schema == expected
        )
        provider = (
            self.author
            if role
            in {
                "extractor",
                "question_writer",
                "direct_joint",
                "distractor_writer",
                "correction",
            }
            else self.verifier
        )
        result = provider.invoke(
            role,
            body["systemInstruction"]["parts"][0]["text"],
            body["contents"][0]["parts"][0]["text"],
            {
                "temperature": generation.get("temperature", 0),
                "max_tokens": generation["maxOutputTokens"],
                "json_schema": schema,
            },
            timeout=30,
        )
        return self._response(model, result.payload, result.request_id)

    def _response(self, model: str, payload: dict, request_id: str | None) -> dict:
        return {
            "responseId": request_id,
            "modelVersion": model,
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(payload)}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 115,
            },
        }


class InvalidEligibilityEvidenceTransport(ScriptedBrokerTransport):
    def post(self, model: str, method: str, body: dict) -> dict:
        response = super().post(model, method, body)
        schema = body.get("generationConfig", {}).get("responseJsonSchema", {})
        if method == "generateContent" and "criteria" in schema.get("properties", {}):
            payload = json.loads(
                response["candidates"][0]["content"]["parts"][0]["text"]
            )
            geography = next(
                row
                for row in payload["criteria"]
                if row["criterion_id"] == "study_geography"
            )
            geography["evidence"][0]["quote"] = (
                "quote absent from the synthetic extraction"
            )
            response["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(
                payload
            )
        return response


class FirstInvalidEligibilityEvidenceTransport(ScriptedBrokerTransport):
    def __init__(self) -> None:
        super().__init__()
        self.eligibility_responses = 0

    def post(self, model: str, method: str, body: dict) -> dict:
        response = super().post(model, method, body)
        schema = body.get("generationConfig", {}).get("responseJsonSchema", {})
        if method == "generateContent" and "criteria" in schema.get("properties", {}):
            self.eligibility_responses += 1
            if self.eligibility_responses == 1:
                payload = json.loads(
                    response["candidates"][0]["content"]["parts"][0]["text"]
                )
                geography = next(
                    row
                    for row in payload["criteria"]
                    if row["criterion_id"] == "study_geography"
                )
                geography["evidence"][0]["quote"] = (
                    "quote absent from the synthetic extraction"
                )
                response["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(
                    payload
                )
            else:
                payload = json.loads(
                    response["candidates"][0]["content"]["parts"][0]["text"]
                )
                payload["overall"] = "excluded"
                payload["overall_reason_codes"] = ["test_only_excluded"]
                geography = next(
                    row
                    for row in payload["criteria"]
                    if row["criterion_id"] == "study_geography"
                )
                geography["status"] = "failed"
                geography["reason_codes"] = ["test_only_excluded"]
                response["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(
                    payload
                )
        return response


class LowThinkingStructuredTransport(ScriptedBrokerTransport):
    def __init__(self) -> None:
        super().__init__()
        self.generation_configs: list[dict] = []

    def post(self, model: str, method: str, body: dict) -> dict:
        if method == "generateContent":
            self.generation_configs.append(body["generationConfig"])
        return super().post(model, method, body)


class OmittedZeroThoughtStreamingTransport(ScriptedBrokerTransport):
    def _response(self, model: str, payload: dict, request_id: str | None) -> dict:
        response = super()._response(model, payload, request_id)
        usage = response["usageMetadata"]
        del usage["thoughtsTokenCount"]
        usage["totalTokenCount"] = 110
        return response


def shared_broker(tmp_path: Path, transport: ScriptedBrokerTransport):
    gate = tmp_path / "broker-gate.json"
    write_json(
        gate,
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": True,
            "allowed_phase": "live_test",
            "integrated_code_commit": "test-only-commit",
            "independent_review_verdict": "pass",
            "review_record": "test-only-review",
        },
    )
    credential = tmp_path / "private" / "gemini.key"
    credential.parent.mkdir(mode=0o700)
    credential.write_text("test-only-unused-key", encoding="utf-8")
    credential.chmod(0o600)
    return SharedGeminiBroker(
        policy_file=REPO / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=REPO / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=gate,
        ledger_file=tmp_path / "shared-ledger.json",
        receipts_dir=tmp_path / "model-receipts",
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
    )


def broker_eligibility_inputs(tmp_path: Path) -> dict[str, Path]:
    policy = tmp_path / "eligibility-policy.json"
    write_json(policy, {"protocol_id": "test-only-policy"})
    return {
        "eligibility_prompt_file": REPO / "config" / "gemini-eligibility-prompt-v3.txt",
        "eligibility_schema_file": REPO
        / "schemas"
        / "gemini-eligibility.v1.schema.json",
        "eligibility_policy_file": policy,
    }


def test_streaming_cli_moves_one_eligible_paper_to_validated_export(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-fixture",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
        "--max-papers",
        "1",
    )

    assert result["state"] == "completed"
    assert result["counts"] == {
        "accepted_base_questions": 1,
        "eligibility_rejected": 0,
        "eligibility_unresolved": 0,
        "generation_rejected": 0,
        "incomplete_non_mcq": 0,
        "processed": 1,
    }
    assert result["export"]["short_answer_count"] == 1
    assert result["export"]["mcq_count"] == 2
    assert result["provider_policy"] == {
        "model": "fake-gemini-3.8-flash",
        "same_model_roles": True,
        "correlated_error_disclosed": True,
        "live_provider": False,
    }
    progress = json.loads(
        (tmp_path / "arctic-qa" / "streaming-dataset-r1" / "progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["schema"] == "streaming-dataset-progress-v1"
    assert progress["state"] == "completed"
    assert progress["run_id"] == "stream-fixture"
    run_manifest = next(
        (tmp_path / "arctic-qa" / "streaming-dataset-r1" / "runs").glob(
            "*/run-manifest.json"
        )
    )
    assert progress["run_manifest_sha256"] == sha256_file(run_manifest)
    assert progress["counts"] == {
        "full_text_ready": 1,
        "eligible": 1,
        "rejected": 0,
        "accepted_qa": 1,
    }
    assert progress["recent_papers"] == [
        {
            "paper_id": result["paper_results"][0]["source_id"],
            "title": "Synthetic public Arctic extraction fixture",
            "current_stage": "completed",
            "final_state": "accepted",
            "final_reason": "machine_accepted_unverified",
        }
    ]


def test_streaming_run_manifest_rejects_changed_resume_inputs(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    arguments = (
        "stream",
        "--run-id",
        "stream-frozen-inputs",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
    )
    run_cli(tmp_path, *arguments)
    access_manifest = json.loads((access / "run-manifest.json").read_text())
    access_manifest["changed_after_first_run"] = True
    write_json(access / "run-manifest.json", access_manifest)

    result = run_cli(tmp_path, *arguments, expected=2)

    assert result["code"] == "VALUEERROR"
    assert result["message"] == "the immutable streaming run inputs changed"


def test_streaming_revalidates_brokered_eligibility_on_resume(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = InvalidEligibilityEvidenceTransport()
    broker = shared_broker(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="eligibility-tamper",
    )
    arguments = {
        "db": database,
        "namespace": paths.namespace,
        "run_id": "eligibility-tamper",
        "campaign_id": "eligibility-tamper-campaign",
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "author": provider,
        "verifier": provider,
        "max_papers": 1,
        **broker_eligibility_inputs(tmp_path),
    }

    result = run_stream(**arguments)
    assert result["paper_results"] == [
        {
            "candidate_key": "test-only:streaming-paper",
            "disposition": "eligibility_unresolved",
            "reason_codes": ["evidence_unmatched_or_ambiguous:study_geography"],
            "source_id": None,
        }
    ]
    job_path = next(
        path
        for path in (eligibility / "jobs").glob("*.json")
        if json.loads(path.read_text()).get("execution_authority")
        == "shared_gemini_broker"
    )
    job = json.loads(job_path.read_text())
    job["state"] = "completed"
    job["validation"] = {
        "valid": True,
        "errors": [],
        "decision": "eligible",
        "resolved_evidence": [],
    }
    write_json(job_path, job)

    resumed = run_stream(**arguments)

    assert resumed["paper_results"] == result["paper_results"]
    assert transport.methods.count("generateContent") == 1
    assert broker.status()["generation_submissions"] == 1


def test_unresolved_eligibility_resume_rejects_changed_source(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = InvalidEligibilityEvidenceTransport()
    broker = shared_broker(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="unresolved-source-integrity",
    )
    arguments = {
        "db": database,
        "namespace": paths.namespace,
        "run_id": "unresolved-source-integrity",
        "campaign_id": "unresolved-source-integrity-campaign",
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "author": provider,
        "verifier": provider,
        "max_papers": 1,
        **broker_eligibility_inputs(tmp_path),
    }

    first = run_stream(**arguments)
    assert first["paper_results"][0]["disposition"] == "eligibility_unresolved"
    assert transport.methods.count("generateContent") == 1

    access_item = json.loads(
        (access / "items" / "item-000001.json").read_text(encoding="utf-8")
    )
    source_path = Path(access_item["source_path"])
    source_path.write_bytes(source_path.read_bytes() + b"\nchanged after receipt\n")

    with pytest.raises(ValueError, match="ready source object is missing or changed"):
        run_stream(**arguments)

    assert transport.methods.count("generateContent") == 1
    assert broker.status()["generation_submissions"] == 1


def test_streaming_advances_after_uncertain_brokered_eligibility(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    first_item_path = access / "items" / "item-000001.json"
    second_item_path = access / "items" / "item-000002.json"
    second_item = json.loads(first_item_path.read_text(encoding="utf-8"))
    second_source = access / "originals" / "source-2.html"
    second_source.write_bytes(
        Path(second_item["source_path"]).read_bytes()
        + b"\n<!-- second test-only source -->\n"
    )
    second_extraction = access / "extracted" / "text-2.txt"
    second_extraction.write_text(
        second_source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    second_item.update(
        {
            "position": 2,
            "candidate_key": "test-only:streaming-paper-2",
            "title": "Second synthetic Arctic extraction fixture",
            "source_path": str(second_source),
            "source_content_hash": sha256(second_source.read_bytes()).hexdigest(),
            "extraction_path": str(second_extraction),
            "extraction_sha256": sha256(second_extraction.read_bytes()).hexdigest(),
        }
    )
    write_json(second_item_path, second_item)
    manifest_path = access / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["target_total"] = 2
    manifest["selection"].append(
        {
            "position": 2,
            "candidate_key": second_item["candidate_key"],
            "subgroup": "test_only",
            "authors": ["Arctic QA test suite"],
            "year": 2026,
        }
    )
    write_json(manifest_path, manifest)

    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = FirstInvalidEligibilityEvidenceTransport()
    broker = shared_broker(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="uncertain-then-next",
    )
    arguments = {
        "db": database,
        "namespace": paths.namespace,
        "run_id": "uncertain-then-next",
        "campaign_id": "uncertain-then-next-campaign",
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "author": provider,
        "verifier": provider,
        "max_papers": 2,
        **broker_eligibility_inputs(tmp_path),
    }

    result = run_stream(**arguments)

    assert result["state"] == "completed"
    assert result["counts"] == {
        "accepted_base_questions": 0,
        "eligibility_rejected": 1,
        "eligibility_unresolved": 1,
        "generation_rejected": 0,
        "incomplete_non_mcq": 0,
        "processed": 2,
    }
    assert result["paper_results"][0] == {
        "candidate_key": "test-only:streaming-paper",
        "disposition": "eligibility_unresolved",
        "reason_codes": ["evidence_unmatched_or_ambiguous:study_geography"],
        "source_id": None,
    }
    assert result["paper_results"][1]["candidate_key"] == second_item["candidate_key"]
    assert result["paper_results"][1]["disposition"] == "eligibility_rejected"
    progress = json.loads(
        (paths.namespace / "streaming-dataset-r1" / "progress.json").read_text()
    )
    assert progress["recent_papers"][0]["final_state"] == "unresolved"
    assert progress["recent_papers"][0]["final_reason"] == (
        "evidence_unmatched_or_ambiguous:study_geography"
    )

    status = broker.status()
    assert status["generation_submissions"] == 2
    assert status["stages"]["eligibility"]["submissions"] == 2
    first_family = stable_id("family", "test-only:streaming-paper")
    second_family = stable_id("family", second_item["candidate_key"])
    ledger = json.loads((tmp_path / "shared-ledger.json").read_text())
    assert (
        sum(row["family_id"] == first_family for row in ledger["requests"].values())
        == 1
    )
    assert (
        sum(row["family_id"] == second_family for row in ledger["requests"].values())
        == 1
    )
    request_keys = set(ledger["requests"])
    receipt_hashes = {
        path.name: sha256_file(path)
        for path in (tmp_path / "model-receipts").glob("*.json")
    }

    resumed = run_stream(**arguments)

    assert resumed["paper_results"] == result["paper_results"]
    resumed_status = broker.status()
    assert resumed_status["generation_submissions"] == 2
    assert (
        set(json.loads((tmp_path / "shared-ledger.json").read_text())["requests"])
        == request_keys
    )
    assert {
        path.name: sha256_file(path)
        for path in (tmp_path / "model-receipts").glob("*.json")
    } == receipt_hashes
    assert transport.methods.count("generateContent") == 2
    rejection = database.one(
        "SELECT reason_code,detail_json FROM rejection_ledger "
        "WHERE stage='scientific_eligibility'"
    )
    assert rejection["reason_code"] == (
        "evidence_unmatched_or_ambiguous:study_geography"
    )
    assert json.loads(rejection["detail_json"])["decision"] == "uncertain"


def test_streaming_uses_one_shared_broker_for_all_ten_stages(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    eligibility_inputs = broker_eligibility_inputs(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = ScriptedBrokerTransport()
    broker = shared_broker(tmp_path, transport)
    progress_path = paths.namespace / "streaming-dataset-r1" / "progress.json"
    status_path = tmp_path / "shared-ledger.status.json"
    policy_path = REPO / "config" / "streaming-dataset-budget-policy-v1.json"
    transport.custody_paths = (progress_path, status_path, policy_path)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    )

    result = run_stream(
        database,
        paths.namespace,
        run_id="live-test-r1",
        campaign_id="streaming-commission",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=provider,
        verifier=provider,
        max_papers=1,
        **eligibility_inputs,
    )

    assert result["counts"]["accepted_base_questions"] == 1
    assert result["provider_policy"] == {
        "model": "gemini-3.8-flash",
        "same_model_roles": True,
        "correlated_error_disclosed": True,
        "live_provider": True,
    }
    status = broker.status()
    assert status["generation_submissions"] == 10
    assert status["accepted_question_count"] == 1
    assert transport.custody_checks == 20
    access_item = json.loads(next((access / "items").glob("*.json")).read_text())
    family_id = stable_id("family", access_item["candidate_key"])
    assert set(status["papers"]) == {family_id}
    ledger = json.loads((tmp_path / "shared-ledger.json").read_text())
    assert ledger["family_bindings"] == {
        family_id: {
            "paper_id": access_item["candidate_key"],
            "source_version_id": access_item["source_content_hash"],
        }
    }
    assert {name: row["submissions"] for name, row in status["stages"].items()} == {
        "eligibility": 1,
        "finding_answer_extraction": 1,
        "question_generation": 1,
        "blinded_reconstruction": 1,
        "answer_verification": 1,
        "distractor_generation": 1,
        "option_verification": 4,
    }
    assert (
        database.one("SELECT * FROM budgets WHERE run_id='streaming-commission'")
        is None
    )
    eligibility_call = database.one(
        "SELECT prompt_version FROM calls WHERE role='eligibility'"
    )
    assert eligibility_call["prompt_version"] == "gemini-eligibility-prompt-v3"
    source = database.one("SELECT * FROM sources")
    assert source["geography_confidence"] == "model_reviewed_unverified"
    assert source["year"] == 2026
    assert source["discipline"] == "unclassified"
    assert source["source_version"] == access_item["source_content_hash"]
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    dataset_metadata = (
        paths.namespace / "exports" / result["export"]["export_id"] / "manifest.json"
    )
    assert progress["run_id"] == "streaming-commission"
    assert progress["invocation_run_id"] == "live-test-r1"
    assert progress["broker_status_sha256"] == sha256_file(status_path)
    assert progress["budget_policy_sha256"] == sha256_file(policy_path)
    assert progress["dataset_metadata_sha256"] == sha256_file(dataset_metadata)
    scope_evidence = json.loads(source["scope_evidence_json"])
    eligibility_job_key = scope_evidence["eligibility_job_key"]
    assert re.fullmatch(r"[a-f0-9]{64}", eligibility_job_key)
    assert (eligibility / "jobs" / f"{eligibility_job_key}.json").is_file()
    assert scope_evidence["study_geography"]["evidence"][0]["locator"] == {
        "source_block_id": "text-block-00001",
        "page_id": None,
        "section_id": "extracted-text",
    }
    assert transport.methods.count("generateContent") == 10

    resumed = run_stream(
        database,
        paths.namespace,
        run_id="live-test-r1",
        campaign_id="streaming-commission",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=provider,
        verifier=provider,
        max_papers=1,
        **eligibility_inputs,
    )

    assert resumed["resumed_papers"] == 1
    assert resumed["counts"]["accepted_base_questions"] == 1
    assert broker.status()["generation_submissions"] == 10
    assert transport.methods.count("generateContent") == 10
    jobs = [
        json.loads(path.read_text()) for path in (eligibility / "jobs").glob("*.json")
    ]
    assert len(jobs) == 2
    assert (
        sum(job.get("execution_authority") == "shared_gemini_broker" for job in jobs)
        == 1
    )


def test_streaming_resumes_reconciled_eligibility_after_process_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    (eligibility / "jobs" / "fixture-job.json").unlink()
    inputs = broker_eligibility_inputs(tmp_path)
    inputs["eligibility_prompt_file"] = (
        REPO / "config" / "gemini-eligibility-prompt-v1.txt"
    )
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = OmittedZeroThoughtStreamingTransport()
    broker = shared_broker(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="reconciled-eligibility",
    )
    arguments = {
        "db": database,
        "namespace": paths.namespace,
        "run_id": "reconciled-eligibility",
        "campaign_id": "reconciled-eligibility-campaign",
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "max_papers": 1,
        **inputs,
    }

    def legacy_classification(submitted: dict, response: dict):
        return (
            {
                **submitted,
                "state": "ambiguous_charge",
                "error": "KeyError: 'thoughtsTokenCount'",
                "response": response,
                "live_call_made": True,
                "completed_at_utc": "2026-09-12T17:24:41Z",
            },
            None,
            None,
        )

    monkeypatch.setattr(broker, "_completed_receipt", legacy_classification)
    with pytest.raises(AmbiguousChargeError):
        run_stream(author=provider, verifier=provider, **arguments)
    assert transport.methods == ["countTokens", "generateContent"]

    ledger = json.loads((tmp_path / "shared-ledger.json").read_text())
    request_key = next(iter(ledger["requests"]))
    review = tmp_path / "usage-review.md"
    review.write_text("The usage repair passed independent review.\n", encoding="utf-8")
    gate_path = tmp_path / "broker-gate.json"
    gate = json.loads(gate_path.read_text())
    gate.update(
        {
            "integrated_code_commit": "usage-repair-commit",
            "review_record": str(review),
            "review_record_sha256": sha256_file(review),
        }
    )
    write_json(gate_path, gate)
    broker.reconcile_omitted_thought_usage(request_key)

    restarted_broker = SharedGeminiBroker(
        policy_file=REPO / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=REPO / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=gate_path,
        ledger_file=tmp_path / "shared-ledger.json",
        receipts_dir=tmp_path / "model-receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
    )
    restarted_provider = BrokerProvider(
        broker=restarted_broker,
        phase="live_test",
        invocation_run_id="reconciled-eligibility",
    )
    result = run_stream(
        author=restarted_provider,
        verifier=restarted_provider,
        **arguments,
    )

    assert result["counts"]["accepted_base_questions"] == 1
    assert transport.methods.count("generateContent") == 10
    status = restarted_broker.status()
    assert status["generation_submissions"] == 10
    assert status["stages"]["eligibility"]["submissions"] == 1
    assert database.one(
        "SELECT status,error_code,error_text FROM calls WHERE role='eligibility'"
    ) == {"status": "completed", "error_code": None, "error_text": None}


def test_low_thinking_counterfactual_completes_the_structured_stream(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = LowThinkingStructuredTransport()
    broker = shared_broker(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="low-thinking-counterfactual",
    )

    result = run_stream(
        database,
        paths.namespace,
        run_id="low-thinking-counterfactual",
        campaign_id="streaming-commission",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=provider,
        verifier=provider,
        max_papers=1,
        **broker_eligibility_inputs(tmp_path),
    )

    assert result["counts"]["accepted_base_questions"] == 1
    assert len(transport.generation_configs) == 10
    assert all(
        config["thinkingConfig"] == {"thinkingLevel": "low"}
        for config in transport.generation_configs
    )
    assert transport.generation_configs[0]["maxOutputTokens"] == 8192
    assert all(
        config["maxOutputTokens"] == 2048 for config in transport.generation_configs[1:]
    )


def test_live_stream_cli_runs_inline_eligibility_and_qa(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    (eligibility / "jobs" / "fixture-job.json").unlink()
    eligibility_policy = tmp_path / "eligibility-policy.json"
    write_json(eligibility_policy, {"protocol_id": "test-only-policy"})
    transport = ScriptedBrokerTransport()
    broker = shared_broker(tmp_path, transport)
    monkeypatch.setattr(cli_module, "SharedGeminiBroker", lambda **kwargs: broker)

    exit_code = cli_module.main(
        [
            "--data-root",
            str(tmp_path),
            "--test-mode",
            "--json",
            "stream",
            "--phase",
            "live_test",
            "--run-id",
            "live-test-r1",
            "--campaign-id",
            "streaming-commission",
            "--access-run-dir",
            str(access),
            "--eligibility-run-dir",
            str(eligibility),
            "--eligibility-policy-file",
            str(eligibility_policy),
            "--credential-file",
            str(tmp_path / "unused-private-key"),
            "--prior-construction-spend-usd",
            "0",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"]["accepted_base_questions"] == 1
    assert broker.status()["generation_submissions"] == 10
    assert transport.methods.count("generateContent") == 10


def test_streaming_live_cli_obeys_disabled_broker_gate_before_credentials(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    eligibility_inputs = broker_eligibility_inputs(tmp_path)
    gate = tmp_path / "disabled-gate.json"
    write_json(
        gate,
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": False,
            "allowed_phase": None,
            "integrated_code_commit": None,
            "independent_review_verdict": "pending",
            "review_record": None,
        },
    )

    result = run_cli(
        tmp_path,
        "stream",
        "--phase",
        "live_test",
        "--run-id",
        "disabled-live-test",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--eligibility-policy-file",
        str(eligibility_inputs["eligibility_policy_file"]),
        "--execution-gate-file",
        str(gate),
        "--credential-file",
        str(tmp_path / "missing-private-key"),
        "--prior-construction-spend-usd",
        "0",
        expected=2,
    )

    assert result["code"] == "VALUEERROR"
    assert result["message"] == "streaming live generation is disabled"
    assert not (tmp_path / "missing-private-key").exists()
    progress = json.loads(
        (tmp_path / "arctic-qa" / "streaming-dataset-r1" / "progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["state"] == "error"
    assert progress["current_stage"] == "eligibility"
    assert progress["recent_papers"][-1]["final_state"] == "error"


def test_legacy_generate_cli_cannot_bypass_the_shared_broker(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "generate",
        "--source-id",
        "unused-source",
        "--run-id",
        "legacy-live-bypass",
        "--arm",
        "answer_first",
        "--author-provider",
        "claude",
        "--author-model",
        "claude-opus-5",
        "--verifier-provider",
        "gemini",
        "--verifier-model",
        "gemini-3.8-flash",
        "--budget-limit",
        "1",
        expected=2,
    )

    assert result["code"] == "VALUEERROR"
    assert result["message"] == (
        "legacy live generation is disabled; use stream with the shared broker"
    )


def test_failed_reconstruction_never_reaches_distractor_generation(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    verifier_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-verifier.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    verifier_events[0]["response"]["ambiguity_label"] = "multiple_answers"
    verifier_events[0]["response"]["alternatives"] = ["1.9 m"]
    verifier_script = tmp_path / "ambiguous-verifier.jsonl"
    verifier_script.write_text(
        "\n".join(json.dumps(event) for event in verifier_events) + "\n",
        encoding="utf-8",
    )
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = ScriptedBrokerTransport(verifier_script=verifier_script)
    broker = shared_broker(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    )

    result = run_stream(
        database,
        paths.namespace,
        run_id="live-test-r1",
        campaign_id="streaming-commission",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=provider,
        verifier=provider,
        max_papers=1,
        **broker_eligibility_inputs(tmp_path),
    )

    assert result["counts"]["accepted_base_questions"] == 0
    assert result["counts"]["generation_rejected"] == 1
    status = broker.status()
    assert status["generation_submissions"] == 5
    assert "distractor_generation" not in status["stages"]
    assert "option_verification" not in status["stages"]


def test_true_distractor_is_removed_before_streaming_export(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    verifier_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-verifier.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    verifier_events[2]["response"]["contradiction_established"] = False
    verifier_events[2]["response"]["question_admits_option_as_correct"] = True
    verifier_script = tmp_path / "true-option-verifier.jsonl"
    verifier_script.write_text(
        "\n".join(json.dumps(event) for event in verifier_events) + "\n",
        encoding="utf-8",
    )
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    broker = shared_broker(
        tmp_path, ScriptedBrokerTransport(verifier_script=verifier_script)
    )
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    )

    result = run_stream(
        database,
        paths.namespace,
        run_id="live-test-r1",
        campaign_id="streaming-commission",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=provider,
        verifier=provider,
        max_papers=1,
        **broker_eligibility_inputs(tmp_path),
    )

    assert result["counts"]["accepted_base_questions"] == 1
    assert result["export"]["mcq_count"] == 1
    mcq_path = paths.namespace / result["export"]["files"]["mcq"]
    records = [json.loads(line) for line in mcq_path.read_text().splitlines()]
    assert all(
        option["text"] != "2.5 m" for record in records for option in record["options"]
    )


def test_short_answer_without_three_distractors_is_not_counted_as_accepted(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    author_events[2]["response"]["distractors"] = author_events[2]["response"][
        "distractors"
    ][:2]
    author_script = tmp_path / "two-distractor-author.jsonl"
    author_script.write_text(
        "\n".join(json.dumps(event) for event in author_events) + "\n",
        encoding="utf-8",
    )
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = ScriptedBrokerTransport()
    transport.author = FakeProvider("gemini-3.8-flash", author_script)
    broker = shared_broker(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="live-test-r1",
    )

    result = run_stream(
        database,
        paths.namespace,
        run_id="live-test-r1",
        campaign_id="streaming-commission",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=provider,
        verifier=provider,
        max_papers=1,
        **broker_eligibility_inputs(tmp_path),
    )

    assert result["counts"]["accepted_base_questions"] == 0
    assert result["counts"]["incomplete_non_mcq"] == 1
    assert broker.status()["accepted_question_count"] == 0
    assert result["export"]["short_answer_count"] == 0
    assert result["export"]["incomplete_short_answer_count"] == 1
    incomplete_path = (
        paths.namespace / result["export"]["files"]["incomplete_short_answer"]
    )
    record = json.loads(incomplete_path.read_text(encoding="utf-8"))
    assert record["release_label"] == "incomplete_non_mcq"
    progress = json.loads(
        (paths.namespace / "streaming-dataset-r1" / "progress.json").read_text()
    )
    assert progress["counts"]["accepted_qa"] == 0


def test_streaming_cli_stops_after_eligibility_rejection(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    receipt_path = eligibility / "jobs" / "fixture-job.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["parsed_response"]["overall"] = "excluded"
    receipt["validation"]["decision"] = "excluded"
    write_json(receipt_path, receipt)
    empty_author = tmp_path / "empty-author.jsonl"
    empty_verifier = tmp_path / "empty-verifier.jsonl"
    empty_author.write_text("", encoding="utf-8")
    empty_verifier.write_text("", encoding="utf-8")

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-rejected",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(empty_author),
        "--verifier-script",
        str(empty_verifier),
        "--max-papers",
        "1",
    )

    assert result["state"] == "completed"
    assert result["counts"] == {
        "accepted_base_questions": 0,
        "eligibility_rejected": 1,
        "eligibility_unresolved": 0,
        "generation_rejected": 0,
        "incomplete_non_mcq": 0,
        "processed": 1,
    }
    assert result["export"]["short_answer_count"] == 0
    assert result["export"]["mcq_count"] == 0


def test_streaming_skips_pending_access_before_next_ready_paper(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    ready_path = access / "items" / "item-000001.json"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    ready["position"] = 2
    write_json(ready_path, ready)
    pending = {
        "schema": "article-access-item-v1",
        "run_id": "access-fixture",
        "position": 1,
        "candidate_key": "test-only:pending-paper",
        "subgroup": "test_only",
        "title": "Pending source",
        "access_state": "access_pending",
        "identity_verified": False,
    }
    write_json(access / "items" / "item-000000.json", pending)
    manifest = json.loads((access / "run-manifest.json").read_text())
    manifest["target_total"] = 2
    manifest["selection"] = [
        {
            "position": 1,
            "candidate_key": pending["candidate_key"],
            "subgroup": "test_only",
        },
        {
            "position": 2,
            "candidate_key": ready["candidate_key"],
            "subgroup": "test_only",
            "authors": ["Arctic QA test suite"],
            "year": 2026,
        },
    ]
    write_json(access / "run-manifest.json", manifest)

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-skip-pending",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
        "--max-papers",
        "1",
    )

    assert result["counts"]["accepted_base_questions"] == 1
    assert result["counts"]["processed"] == 1
    progress = json.loads(
        (tmp_path / "arctic-qa" / "streaming-dataset-r1" / "progress.json").read_text()
    )
    assert progress["counts"]["full_text_ready"] == 1


def test_streaming_cli_reports_the_eligibility_rejection_reason(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    receipt_path = eligibility / "jobs" / "fixture-job.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["parsed_response"]["overall"] = "excluded"
    receipt["parsed_response"]["overall_reason_codes"] = ["study_geography_not_arctic"]
    receipt["validation"]["decision"] = "excluded"
    write_json(receipt_path, receipt)
    empty_author = tmp_path / "unused-author.jsonl"
    empty_verifier = tmp_path / "unused-verifier.jsonl"
    empty_author.write_text("", encoding="utf-8")
    empty_verifier.write_text("", encoding="utf-8")

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-rejection-reason",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(empty_author),
        "--verifier-script",
        str(empty_verifier),
    )

    assert result["paper_results"] == [
        {
            "candidate_key": "test-only:streaming-paper",
            "disposition": "eligibility_rejected",
            "reason_codes": ["study_geography_not_arctic"],
            "source_id": None,
        }
    ]


def test_streaming_cli_resumes_without_a_duplicate_model_call(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    arguments = (
        "stream",
        "--run-id",
        "stream-resume",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
    )
    first = run_cli(tmp_path, *arguments)
    empty_author = tmp_path / "empty-resume-author.jsonl"
    empty_verifier = tmp_path / "empty-resume-verifier.jsonl"
    empty_author.write_text("", encoding="utf-8")
    empty_verifier.write_text("", encoding="utf-8")
    resumed_arguments = list(arguments)
    resumed_arguments[resumed_arguments.index(str(FIXTURES / "fake-author.jsonl"))] = (
        str(empty_author)
    )
    resumed_arguments[
        resumed_arguments.index(str(FIXTURES / "fake-verifier.jsonl"))
    ] = str(empty_verifier)

    second = run_cli(tmp_path, *resumed_arguments)

    assert first["counts"]["accepted_base_questions"] == 1
    assert second["counts"]["accepted_base_questions"] == 1
    assert second["resumed_papers"] == 1
    status = run_cli(tmp_path, "status", "--run-id", "stream-resume")
    assert status["calls"] == [{"count": 9, "status": "completed"}]


def test_streaming_cli_fails_closed_on_an_unbound_selection(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    manifest_path = access / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["selection"][0]["candidate_key"] = "different-paper"
    write_json(manifest_path, manifest)

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-unbound-selection",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
        expected=2,
    )

    assert result["code"] == "VALUEERROR"
    assert result["message"] == "the ordered selection does not match its access item"


def test_streaming_finding_selection_reads_results_not_only_the_longest_chunk(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    source_path = access / "originals" / "source.html"
    source_path.write_text(
        "<html><body><h1>Synthetic public Arctic extraction fixture</h1>"
        "<h2>Introduction</h2><p>"
        + ("Background material without a reported finding. " * 140)
        + "</p><h2>Results</h2><p>"
        "The complete study site was at 71.3 N. "
        "The reported water depth was 2.0 m with a source-grounded tolerance of 0.1 m."
        "</p></body></html>",
        encoding="utf-8",
    )
    extracted_path = access / "extracted" / "text.txt"
    extracted_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    source_hash = sha256(source_path.read_bytes()).hexdigest()
    extraction_hash = sha256(extracted_path.read_bytes()).hexdigest()
    access_path = access / "items" / "item-000001.json"
    access_item = json.loads(access_path.read_text(encoding="utf-8"))
    access_item["source_content_hash"] = source_hash
    access_item["extraction_sha256"] = extraction_hash
    write_json(access_path, access_item)
    eligibility_path = eligibility / "jobs" / "fixture-job.json"
    eligibility_item = json.loads(eligibility_path.read_text(encoding="utf-8"))
    eligibility_item["source_content_hash"] = source_hash
    eligibility_item["extraction_sha256"] = extraction_hash
    eligibility_item["parsed_response"]["input_echo"]["source_version_sha256"] = (
        source_hash
    )
    eligibility_item["parsed_response"]["input_echo"]["extracted_text_sha256"] = (
        extraction_hash
    )
    write_json(eligibility_path, eligibility_item)
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    author_events[0]["require_prompt_contains"] = [
        '"heading":"Results"',
        "The reported water depth was 2.0 m",
    ]
    author_script = tmp_path / "results-author.jsonl"
    author_script.write_text(
        "\n".join(json.dumps(event) for event in author_events) + "\n",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-results",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(author_script),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
    )

    assert result["counts"]["accepted_base_questions"] == 1


def test_streaming_family_freeze_spans_test_and_production_phases(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    shared = (
        "--campaign-id",
        "streaming-commission",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
    )
    first = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "live-test-phase",
        *shared,
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
    )
    empty_author = tmp_path / "unused-production-author.jsonl"
    empty_verifier = tmp_path / "unused-production-verifier.jsonl"
    empty_author.write_text("", encoding="utf-8")
    empty_verifier.write_text("", encoding="utf-8")

    second = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "production-phase",
        *shared,
        "--author-script",
        str(empty_author),
        "--verifier-script",
        str(empty_verifier),
    )

    assert first["campaign_id"] == "streaming-commission"
    assert second["campaign_id"] == "streaming-commission"
    assert second["resumed_papers"] == 1
    status = run_cli(tmp_path, "status", "--run-id", "streaming-commission")
    assert status["calls"] == [{"count": 9, "status": "completed"}]


def test_streaming_export_discloses_same_model_correlated_error(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-disclosure",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(FIXTURES / "fake-author.jsonl"),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
    )
    short_answer_path = (
        tmp_path / "arctic-qa" / result["export"]["files"]["short_answer"]
    )
    exported = json.loads(short_answer_path.read_text(encoding="utf-8"))

    assert exported["provenance"]["construction_role_policy"] == {
        "author_model": "fake-gemini-3.8-flash",
        "verifier_model": "fake-gemini-3.8-flash",
        "same_provider_family": True,
        "separate_blinded_calls": True,
        "independent_error_evidence": False,
    }
