from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from arctic_qa import cli as cli_module
from arctic_qa import generation as generation_module
from arctic_qa import streaming as streaming_module
from arctic_qa.broker_provider import BrokerProvider
from arctic_qa.db import Database
from arctic_qa.errors import AmbiguousChargeError, BudgetError
from arctic_qa.exporting import export_run
from arctic_qa.generation import ROLE_SCHEMAS, resume_candidate_distractors
from arctic_qa.model_broker import (
    AUTHORIZED_CAP_REASON,
    PER_REQUEST_CAP_REASON,
    SharedGeminiBroker,
)
from arctic_qa.paths import DataPaths
from arctic_qa.providers import FakeProvider
from arctic_qa.streaming import run_stream
from arctic_qa.util import canonical_json, sha256_file, stable_id
from arctic_qa import validation as validation_module


REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_exact_integer_count_is_source_bound_without_a_written_zero_tolerance() -> None:
    answer = {
        "text": "three ramping experiments",
        "evidence_quote": "we could run three ramping experiments",
        "numeric_rule": {
            "canonical_value": "3",
            "unit": "experiments",
            "tolerance": "0",
            "tolerance_basis": "count",
            "reported_precision": "exact integer",
            "rounding_rule": "none",
            "conversion_rule": "Direct count of the specified ramping experiments",
        },
    }

    assert validation_module.numeric_rule_is_source_bound(answer) is True


def test_exact_integer_count_with_compound_unit_is_source_bound() -> None:
    answer = {
        "text": "250 fungal OTUs",
        "evidence_quote": (
            "250 fungal OTUs of 76,691 reads were included in the final matrix."
        ),
        "numeric_rule": {
            "canonical_value": "250",
            "unit": "fungal OTUs",
            "tolerance": "0",
            "tolerance_basis": "count",
            "reported_precision": "exact integer",
            "rounding_rule": "none",
            "conversion_rule": "direct count of fungal OTUs",
        },
    }

    assert validation_module.numeric_rule_is_source_bound(answer) is True


@pytest.mark.parametrize(
    ("displayed", "evidence", "canonical_value", "unit"),
    [
        (
            "250 fungal OTUs",
            "250 bacterial OTUs of 76,691 reads were included in the final matrix.",
            "250",
            "fungal OTUs",
        ),
        (
            "250 fungal OTUs",
            "250 fungal OTUs of 76,691 reads were included in the final matrix.",
            "251",
            "fungal OTUs",
        ),
    ],
)
def test_exact_integer_count_compound_unit_rejects_unit_or_cardinality_near_miss(
    displayed: str, evidence: str, canonical_value: str, unit: str
) -> None:
    answer = {
        "text": displayed,
        "evidence_quote": evidence,
        "numeric_rule": {
            "canonical_value": canonical_value,
            "unit": unit,
            "tolerance": "0",
            "tolerance_basis": "count",
            "reported_precision": "exact integer",
            "rounding_rule": "none",
            "conversion_rule": "direct count of fungal OTUs",
        },
    }

    assert validation_module.numeric_rule_is_source_bound(answer) is False


def test_unqualified_exact_percentage_remains_not_source_bound() -> None:
    answer = {
        "text": "10.5 %",
        "evidence_quote": "The cost function was reduced by 10.5 %.",
        "numeric_rule": {
            "canonical_value": "10.5",
            "unit": "%",
            "tolerance": "0",
            "tolerance_basis": "exact percentage reported",
            "reported_precision": "10.5",
            "rounding_rule": "exact_match",
            "conversion_rule": "Directly reported percentage value.",
        },
    }

    assert validation_module.numeric_rule_is_source_bound(answer) is False


@pytest.mark.parametrize(
    ("displayed", "evidence", "canonical_value", "unit", "tolerance", "basis"),
    [
        (
            "4.2 mg/L",
            "The concentration was 4.2 mg/L with a tolerance of 0.1 mg/L.",
            "4.2",
            "mg/L",
            "0.1",
            "0.1 mg/L",
        ),
        (
            "1.2e-3 m",
            "The displacement was 1.2e-3 m with a tolerance of 1e-4 m.",
            "0.0012",
            "m",
            "0.0001",
            "1e-4 m",
        ),
        (
            "1,000 m",
            "The transect length was 1,000 m ± 10 m.",
            "1000",
            "m",
            "10",
            "± 10 m",
        ),
    ],
)
def test_source_bound_numeric_rule_accepts_supported_literal_formats(
    displayed: str,
    evidence: str,
    canonical_value: str,
    unit: str,
    tolerance: str,
    basis: str,
) -> None:
    answer = {
        "text": displayed,
        "evidence_quote": evidence,
        "numeric_rule": {
            "canonical_value": canonical_value,
            "unit": unit,
            "tolerance": tolerance,
            "tolerance_basis": basis,
            "reported_precision": basis.removeprefix("± "),
            "rounding_rule": "none",
            "conversion_rule": "direct source literal",
        },
    }

    assert validation_module.numeric_rule_is_source_bound(answer) is True


def test_compound_unit_rule_without_source_tolerance_remains_rejected() -> None:
    answer = {
        "text": "4.2 mg/L",
        "evidence_quote": "The concentration was 4.2 mg/L.",
        "numeric_rule": {
            "canonical_value": "4.2",
            "unit": "mg/L",
            "tolerance": "0.1",
            "tolerance_basis": "0.1 mg/L",
            "reported_precision": "0.1 mg/L",
            "rounding_rule": "none",
            "conversion_rule": "direct source literal",
        },
    }

    assert validation_module.numeric_rule_is_source_bound(answer) is False


def test_numeric_rule_schema_describes_source_support_and_omission() -> None:
    properties = generation_module.NUMERIC_RULE_SCHEMA["properties"]

    assert generation_module.PROMPT_VERSION == "arctic-qa-generation-v23"
    assert (
        generation_module.NUMERIC_RULE_CONTRACT_VERSION
        == "numeric-rule-source-support-v3"
    )
    assert (
        generation_module.SCOPE_CONTRACT_VERSION == "selected-evidence-literal-scope-v4"
    )
    distractor_array = generation_module.ROLE_SCHEMAS["distractor_writer"][
        "properties"
    ]["distractors"]
    assert distractor_array["minItems"] == 4
    assert distractor_array["maxItems"] == 6
    assert "Avoid explicit negation" in generation_module.DISTRACTOR_WRITER_INSTRUCTIONS
    assert "exactly one displayed number and unit" in (
        generation_module.DISTRACTOR_WRITER_INSTRUCTIONS
    )
    assert (
        generation_module.ANSWER_SCHEMA["properties"]["required_question_phrases"][
            "minItems"
        ]
        == 1
    )
    assert (
        "Exact selected-span text"
        in generation_module.SCOPE_SCHEMA["properties"]["method"]["description"]
    )
    assert "selected source span" in properties["canonical_value"]["description"]
    assert "selected source span" in properties["tolerance"]["description"]
    assert "Exact source text" in properties["tolerance_basis"]["description"]
    assert "Do not invent another wording" in properties["rounding_rule"]["description"]
    # One vocabulary: the strings the rule enforces are the strings the schema
    # and the prompt state.
    assert "same unit as the unit field" in properties["tolerance_basis"]["description"]
    assert "decimal increment" in properties["reported_precision"]["description"]
    assert "'<N> decimal places'" in properties["rounding_rule"]["description"]
    assert "'direct source literal'" in properties["conversion_rule"]["description"]


def test_generation_prompt_requires_atomic_answers_and_aligned_questions() -> None:
    answer_instructions = generation_module.ANSWER_FORMAT_INSTRUCTIONS
    question_instructions = generation_module.QUESTION_ALIGNMENT_INSTRUCTIONS
    answer_text_schema = generation_module.ANSWER_SCHEMA["properties"]["text"]

    assert generation_module.FINDING_POLICY_VERSION.endswith("-v6")
    assert "only the concise answer" in answer_instructions
    assert "Do not restate the question" in answer_instructions
    assert "unrelated values" in answer_instructions
    assert "12 cases" in answer_instructions
    assert "higher at Site A" in answer_instructions
    assert "necessary unit" in answer_instructions
    assert "matching numeric metadata" in answer_instructions
    assert "complete source-supported count noun phrase" in answer_instructions
    assert "exactly the content of answer.text" in question_instructions
    assert "one component of a multi-value answer" in question_instructions
    assert "multiple values" in question_instructions
    assert "concise answer" in answer_text_schema["description"]
    assert "necessary units" in answer_text_schema["description"]


def test_generation_schemas_require_concise_review_justifications() -> None:
    assert generation_module.PROMPT_VERSION == "arctic-qa-generation-v23"
    assert (
        generation_module.MODEL_JUSTIFICATION_CONTRACT_VERSION
        == "model-justification-v1"
    )

    required_by_role = {
        "question_writer": "question_rationale",
        "direct_joint": "question_rationale",
        "reconstructor": "reconstruction_rationale",
        "answer_verifier": "verification_rationale",
        "option_verifier": "rationale",
    }
    for role, field in required_by_role.items():
        assert field in generation_module.ROLE_SCHEMAS[role]["required"]
        assert (
            "concise"
            in generation_module.ROLE_SCHEMAS[role]["properties"][field][
                "description"
            ].lower()
        )

    candidate_schema = generation_module.ROLE_SCHEMAS["extractor"]["properties"][
        "candidate_findings"
    ]["items"]
    assert "ranking_rationale" in candidate_schema["required"]
    assert (
        "concise"
        in candidate_schema["properties"]["ranking_rationale"]["description"].lower()
    )
    answer_schema = candidate_schema["properties"]["answer"]
    assert "selection_rationale" in answer_schema["required"]
    assert (
        "concise"
        in answer_schema["properties"]["selection_rationale"]["description"].lower()
    )

    distractor_schema = generation_module.ROLE_SCHEMAS["distractor_writer"][
        "properties"
    ]["distractors"]["items"]
    assert "generation_rationale" in distractor_schema["required"]
    assert (
        "concise"
        in distractor_schema["properties"]["generation_rationale"][
            "description"
        ].lower()
    )


def test_reconstruction_schema_has_no_frozen_answer_or_selection_rationale() -> None:
    schema_text = json.dumps(generation_module.ROLE_SCHEMAS["reconstructor"])

    assert "selection_rationale" not in schema_text
    assert "reference_answer" not in schema_text


def test_numeric_format_alias_is_not_a_competing_reconstruction_answer() -> None:
    answer = {
        "text": "three ramping experiments",
        "variants": ["3 ramping experiments", "three"],
        "numeric_rule": {
            "canonical_value": "3",
            "unit": "experiments",
            "tolerance": "0",
            "tolerance_basis": "count",
            "reported_precision": "exact integer",
            "rounding_rule": "none",
            "conversion_rule": "Direct count of the specified ramping experiments",
        },
    }
    reconstruction = {
        "answer": "three",
        "numeric": {"canonical_value": "3", "unit": "experiments"},
        "alternatives": ["3"],
    }

    assert (
        validation_module.reconstruction_has_competing_alternatives(
            answer, reconstruction
        )
        is False
    )
    reconstruction["alternatives"] = ["four"]
    assert (
        validation_module.reconstruction_has_competing_alternatives(
            answer, reconstruction
        )
        is True
    )


def test_reconstruction_match_accepts_only_exact_normalized_form() -> None:
    answer = {
        "text": "All wetland sequences lacked this loop.",
        "variants": [],
    }

    assert validation_module.reconstruction_matches(
        answer,
        {
            "answer": "Yes, all wetland sequences lacked this loop.",
        },
    )
    assert not validation_module.reconstruction_matches(
        answer,
        {
            "answer": "Yes, all wetland sequences lacked this loop corresponding "
            "to the conserved motif.",
        },
    )
    assert not validation_module.reconstruction_matches(
        {"text": "The organic mass component was 24 % over February-May."},
        {"answer": "24 %"},
    )
    assert not validation_module.reconstruction_matches(
        answer,
        {"answer": "No, wetland sequences did not lack this loop."},
    )


def test_position_1043_counterfactual_keeps_scope_rejection() -> None:
    quote = "proposed approach, we could run three ramping experiments"
    chunk = {"chunk_id": "retained-1043", "text": quote}
    answer_scope = {
        "comparison": None,
        "geography": None,
        "method": "proposed approach simulation",
        "period": None,
        "population": "ramping experiments",
        "uncertainty": None,
    }
    verifier_scope = {
        **answer_scope,
        "method": "proposed approach",
        "population": "ramping experiments with fast to intermediate rates",
    }
    locator = {"chunk_id": "retained-1043", "start_offset": 0, "end_offset": len(quote)}
    answer = {
        "text": "three ramping experiments",
        "variants": ["3 ramping experiments", "three"],
        "claim_type": "observation",
        "evidence_quote": quote,
        "locator": locator,
        "required_question_phrases": [],
        "scope": answer_scope,
        "numeric_rule": {
            "canonical_value": "3",
            "unit": "experiments",
            "tolerance": "0",
            "tolerance_basis": "count",
            "reported_precision": "exact integer",
            "rounding_rule": "none",
            "conversion_rule": "Direct count of the specified ramping experiments",
        },
    }
    reconstruction = {
        "answer": "three",
        "numeric": {"canonical_value": "3", "unit": "experiments"},
        "alternatives": ["3"],
        "ambiguity_label": "one_answer",
        "question_claim_type": "observation",
        "evidence_quote": quote,
        "locator": locator,
        "scope": verifier_scope,
    }
    verification = {
        "source_entailment_model_verified": True,
        "relation_scope_match": True,
        "scope_value_contradicted_by_source": False,
        "contradicted_scope_field": "",
        "scope_representation_note": "",
        "ambiguity_resolved": True,
        "alternative_answer_search_passed": True,
        "question_context_required": False,
        "question_context_source_supported": True,
        "question_context_answer_leakage_absent": True,
        "question_verification_contract_version": generation_module.QUESTION_VERIFICATION_CONTRACT_VERSION,
        "question_context_referent_resolved": True,
        "question_context_missing_detail": "",
        "question_answer_leakage_absent": True,
        "question_claim_type": "observation",
        "evidence_quote": quote,
        "locator": locator,
        "scope": verifier_scope,
    }

    reasons = generation_module._qa_gate_reasons(
        chunk, "How many ramping experiments?", answer, reconstruction, verification
    )

    assert "reconstruction_alternative_answer_present" not in reasons
    assert "source_bound_numeric_rule_missing" not in reasons
    assert "answer_scope_not_source_bound" in reasons
    assert "reconstruction_scope_not_source_bound" in reasons
    assert "answer_verifier_scope_not_source_bound" in reasons
    assert "reconstruction_scope_mismatch" not in reasons
    assert "answer_verifier_scope_mismatch" not in reasons


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


def test_export_identity_changes_with_rejection_content(tmp_path: Path) -> None:
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")

    with database.transaction():
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES ('rejection-one',NULL,NULL,'scientific_eligibility',
                    'first_reason','{}','2026-09-12T00:00:00Z')"""
        )
    first = export_run(database, paths.namespace, "campaign", seed="fixed")
    first_rejections = Path(paths.namespace / first["files"]["rejections"]).read_bytes()

    with database.transaction():
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES ('rejection-two',NULL,NULL,'scientific_eligibility',
                    'second_reason','{}','2026-09-12T00:00:01Z')"""
        )
    second = export_run(database, paths.namespace, "campaign", seed="fixed")

    assert second["export_id"] != first["export_id"]
    assert first["rejection_count"] == 1
    assert second["rejection_count"] == 2
    assert first["file_sha256"]["rejections"] == sha256(first_rejections).hexdigest()
    assert Path(paths.namespace / first["files"]["rejections"]).read_bytes() == (
        first_rejections
    )


def test_streaming_export_excludes_predecessor_contract_candidates(
    tmp_path: Path,
) -> None:
    result = run_cli(
        tmp_path,
        "smoke",
        "--fixture-dir",
        str(FIXTURES),
        "--run-id",
        "current-export",
    )
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    current = database.one(
        "SELECT candidate_json FROM candidates WHERE item_id=?", (result["item_id"],)
    )
    predecessor = json.loads(current["candidate_json"])
    predecessor["item_id"] = "historical-v20-item"
    predecessor["schema_version"] = "2.5.0"
    predecessor["provenance"]["prompt_version"] = "arctic-qa-generation-v20"
    predecessor_json = canonical_json(predecessor)
    current_validation = database.one(
        "SELECT * FROM validation_events WHERE item_id=? ORDER BY rowid DESC LIMIT 1",
        (result["item_id"],),
    )
    details = json.loads(current_validation["details_json"])
    details["candidate_hash"] = stable_id("candidate-payload", predecessor_json)
    with database.transaction():
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            SELECT ?,run_id,source_id,?,generation_arm,?,status,created_at,updated_at
            FROM candidates WHERE item_id=?""",
            (
                predecessor["item_id"],
                "historical-v20-family",
                predecessor_json,
                result["item_id"],
            ),
        )
        database.connection.execute(
            """INSERT INTO validation_events
            (event_id,item_id,stage,label,reason_codes_json,details_json,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (
                "historical-v20-validation",
                predecessor["item_id"],
                current_validation["stage"],
                current_validation["label"],
                current_validation["reason_codes_json"],
                canonical_json(details),
                current_validation["created_at"],
            ),
        )

    exported = export_run(
        database,
        paths.namespace,
        "current-export",
        seed="current-contract-only",
        candidate_schema_version=generation_module.CANDIDATE_SCHEMA_VERSION,
        generation_prompt_version=generation_module.PROMPT_VERSION,
    )
    records = [
        json.loads(line)
        for line in (paths.namespace / exported["files"]["short_answer"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert [record["item_id"] for record in records] == [result["item_id"]]


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
    def __init__(
        self,
        *,
        author_script: Path | None = None,
        verifier_script: Path | None = None,
    ) -> None:
        self.author = FakeProvider(
            "gemini-3.8-flash", author_script or FIXTURES / "fake-author.jsonl"
        )
        self.verifier = FakeProvider(
            "gemini-3.8-flash", verifier_script or FIXTURES / "fake-verifier.jsonl"
        )
        self.methods: list[str] = []
        self.role_prompts: list[tuple[str, str]] = []
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
        self.role_prompts.append((role, body["contents"][0]["parts"][0]["text"]))
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


def test_finding_prompt_requires_one_exact_source_span(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    author_events[0]["require_prompt_contains"] = [
        "Select one source_span_id for each candidate.",
        "one exact selectable interval",
        "Do not combine span IDs yourself.",
    ]
    author_script = tmp_path / "exact-finding-prompt-author.jsonl"
    author_script.write_text(
        "\n".join(json.dumps(event) for event in author_events) + "\n",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-exact-finding-prompt",
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
    assert result["export"]["short_answer_count"] == 1
    assert result["export"]["mcq_count"] == 2
    assert {
        key: value
        for key, value in result["provider_policy"].items()
        if key != "model_roles"
    } == {
        "model": "fake-gemini-3.8-flash",
        "same_model_roles": True,
        "correlated_error_disclosed": True,
        "live_provider": False,
    }
    assert result["provider_policy"]["model_roles"]["enforced"] is False
    assert result["provider_policy"]["model_roles"]["same_model_roles"] is True
    progress = json.loads(
        (tmp_path / "arctic-qa" / "streaming-dataset-r1" / "progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["schema"] == "streaming-dataset-progress-v1"
    assert progress["state"] == "completed"
    assert progress["run_id"] == "stream-exact-finding-prompt"
    run_manifest = next(
        (tmp_path / "arctic-qa" / "streaming-dataset-r1" / "runs").glob(
            "*/run-manifest.json"
        )
    )
    assert progress["run_manifest_sha256"] == sha256_file(run_manifest)
    assert progress["counts"] == {
        "full_text_ready": 1,
        "eligibility_completed": 1,
        "eligible": 1,
        "excluded": 0,
        "unresolved": 0,
        "generation_rejected": 0,
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


def test_finding_span_id_resolves_to_exact_source_evidence(tmp_path: Path) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    answer = author_events[0]["response"]["candidate_findings"][0]["answer"]
    answer.pop("evidence_quote", None)
    answer.pop("locator", None)
    answer["source_span_id"] = "{{span_id}}"
    author_events[0]["require_prompt_contains"] = [
        '"evidence_spans"',
        '"span_id"',
        '"span_contract_version":"finding-evidence-span-v3"',
        '"text_sha256"',
        "Select one source_span_id for each candidate.",
    ]
    author_script = tmp_path / "finding-span-author.jsonl"
    author_script.write_text(
        "\n".join(json.dumps(event) for event in author_events) + "\n",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-finding-span",
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
    short_answer_path = (
        tmp_path / "arctic-qa" / result["export"]["files"]["short_answer"]
    )
    record = json.loads(short_answer_path.read_text(encoding="utf-8"))
    assert (
        "The reported water depth was 2.0 m with a source-grounded tolerance of 0.1 m."
        in record["evidence"]["quote"]
    )
    assert (
        record["evidence"]["locator"]["end_offset"]
        > record["evidence"]["locator"]["start_offset"]
    )
    assert record["evidence"]["span_contract_version"] == "finding-evidence-span-v3"
    assert record["evidence"]["source_span_id"].startswith("finding-evidence-span-v3-")
    assert len(record["evidence"]["text_sha256"]) == 64


def test_finding_spans_keep_wrapped_prose_sentence_together() -> None:
    text = (
        "Quantitatively, the RMS error in reconstruction of ITP 103 is\n"
        "0.030 km compared to 0.078 km for ITP 104. This is due to\n"
        "the larger motion magnitude.\n\nNEXT SECTION"
    )
    chunk = {"chunk_id": "chunk-wrapped", "text": text}

    spans = generation_module._finding_spans(chunk)

    expected = text[: text.index("\n\n")]
    assert any(span["text"] == expected for span in spans)
    span = next(span for span in spans if span["text"] == expected)
    assert text[span["start_offset"] : span["end_offset"]] == expected


def test_streaming_resolves_every_role_evidence_from_source_spans(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    for distractor in author_events[2]["response"]["distractors"]:
        distractor.pop("evidence_quote", None)
        distractor.pop("locator", None)
        distractor["source_span_id"] = "{{span_id}}"
    author_events[2]["require_prompt_contains"] = [
        '"span_contract_version":"finding-evidence-span-v3"',
        "Select source_span_id for each evidence record.",
    ]
    verifier_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-verifier.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    for event in verifier_events:
        if event["role"] == "standalone_verifier":
            continue
        response = event["response"]
        response.pop("evidence_quote", None)
        response.pop("locator", None)
        response["source_span_id"] = "{{span_id}}"
        event.setdefault("require_prompt_contains", []).extend(
            [
                '"evidence_spans"',
                "Select one source_span_id for the evidence.",
            ]
        )
    author_script = tmp_path / "all-spans-author.jsonl"
    author_script.write_text(
        "\n".join(json.dumps(event) for event in author_events) + "\n",
        encoding="utf-8",
    )
    verifier_script = tmp_path / "all-spans-verifier.jsonl"
    verifier_script.write_text(
        "\n".join(json.dumps(event) for event in verifier_events) + "\n",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-all-evidence-spans",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(author_script),
        "--verifier-script",
        str(verifier_script),
    )

    assert result["counts"]["accepted_base_questions"] == 1
    mcq_path = tmp_path / "arctic-qa" / result["export"]["files"]["mcq"]
    records = [json.loads(line) for line in mcq_path.read_text().splitlines()]
    answer_present = next(
        row for row in records if row["task_type"] == "answer_present_mcq"
    )
    for option in answer_present["options"]:
        if option["is_correct"]:
            continue
        assert (
            "The reported water depth was 2.0 m with a source-grounded tolerance of 0.1 m."
            in option["falsity_evidence"]["quote"]
        )


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        "max_papers": 1,
        **broker_eligibility_inputs(tmp_path),
    }

    first = run_stream(**arguments)

    assert first["counts"]["processed"] == 1
    assert first["paper_results"][0]["candidate_key"] == ("test-only:streaming-paper")
    assert broker.status()["generation_submissions"] == 1
    first_ledger = json.loads((tmp_path / "shared-ledger.json").read_text())
    first_request_keys = set(first_ledger["requests"])
    first_receipt_hashes = {
        path.name: sha256_file(path)
        for path in (tmp_path / "model-receipts").glob("*.json")
    }

    progress_path = paths.namespace / "streaming-dataset-r1" / "progress.json"
    foreign_progress = json.loads(progress_path.read_text(encoding="utf-8"))
    foreign_progress["invocation_run_id"] = "different-invocation"
    foreign_progress["counts"]["eligibility_completed"] = 999
    write_json(progress_path, foreign_progress)
    observed_progress_counts: list[dict[str, int]] = []
    original_atomic_json = streaming_module.atomic_json

    def observe_atomic_json(path: Path, value: Any, **kwargs: Any) -> None:
        if path.resolve() == progress_path.resolve():
            observed_progress_counts.append(dict(value["counts"]))
        original_atomic_json(path, value, **kwargs)

    monkeypatch.setattr(streaming_module, "atomic_json", observe_atomic_json)
    arguments["max_papers"] = 2
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
    assert progress["counts"] == {
        "full_text_ready": 2,
        "eligibility_completed": 2,
        "eligible": 0,
        "excluded": 1,
        "unresolved": 1,
        "generation_rejected": 0,
        "accepted_qa": 0,
    }
    assert observed_progress_counts
    assert observed_progress_counts[0]["eligibility_completed"] == 1
    assert all(
        1 <= counts["eligibility_completed"] <= 2 for counts in observed_progress_counts
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
    assert first_request_keys < set(ledger["requests"])
    assert len(set(ledger["requests"]) - first_request_keys) == 1
    assert all(
        sha256_file(tmp_path / "model-receipts" / name) == digest
        for name, digest in first_receipt_hashes.items()
    )
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


def test_streaming_maximum_comes_from_immutable_input_count(tmp_path: Path) -> None:
    access = tmp_path / "access-501"
    eligibility = tmp_path / "eligibility-501"
    selection = []
    for position in range(1, 502):
        candidate_key = f"test-only:unavailable-{position:03d}"
        selection.append(
            {
                "position": position,
                "candidate_key": candidate_key,
                "subgroup": "test_only_unavailable",
            }
        )
        write_json(
            access / "items" / f"item-{position:06d}.json",
            {
                "run_id": "access-501",
                "position": position,
                "candidate_key": candidate_key,
                "subgroup": "test_only_unavailable",
                "access_state": "unavailable",
            },
        )
    write_json(
        access / "run-manifest.json",
        {
            "run_id": "access-501",
            "target_total": len(selection),
            "selection": selection,
        },
    )
    write_json(access / "progress.json", {"state": "completed"})
    write_json(access / "run-receipt.json", {"state": "completed"})
    empty_script = tmp_path / "empty-provider.jsonl"
    empty_script.write_text("", encoding="utf-8")
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    arguments = {
        "db": database,
        "namespace": paths.namespace,
        "run_id": "input-bounded-501",
        "campaign_id": "input-bounded-501",
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "author": FakeProvider("fake-gemini", empty_script),
        "verifier": FakeProvider("fake-gemini", empty_script),
        "max_papers": 501,
    }

    result = run_stream(**arguments)

    assert result["state"] == "completed"
    assert result["counts"]["processed"] == 0
    with pytest.raises(
        ValueError, match="max papers cannot exceed the ordered selection count"
    ):
        run_stream(**{**arguments, "max_papers": 502})


def test_streaming_uses_one_shared_broker_for_all_eleven_calls(
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
    assert {
        key: value
        for key, value in result["provider_policy"].items()
        if key != "model_roles"
    } == {
        "model": "gemini-3.8-flash",
        "same_model_roles": False,
        "correlated_error_disclosed": False,
        "live_provider": True,
    }
    # A live test phase discloses the effective per-role models without a
    # profile. Only the production phase must name a separated role profile.
    # The chapter 2 price config meters every judge stage on a different
    # model from the writer, so the disclosure reports separated roles.
    roles = result["provider_policy"]["model_roles"]
    assert roles["enforced"] is False
    assert roles["same_model_roles"] is False
    assert roles["effective_role_models"]["question_writer"] == "gemini-3.8-flash"
    assert roles["effective_role_models"]["standalone_verifier"] == (
        "gemini-3.1-pro-preview"
    )
    status = broker.status()
    assert status["generation_submissions"] == 11
    assert status["accepted_question_count"] == 1
    assert transport.custody_checks == 22
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
        "standalone_verification": 1,
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
    assert transport.methods.count("generateContent") == 11
    prompts = dict(transport.role_prompts)
    assert (
        "Select one atomic claim from a complete prose finding sentence"
        in prompts["extractor"]
    )
    assert "Do not select a title, heading, caption" in prompts["extractor"]
    assert "SOURCE_DATA" not in prompts["standalone_verifier"]
    assert "ANSWER_RECORD" not in prompts["standalone_verifier"]
    assert "RECONSTRUCTION" not in prompts["standalone_verifier"]
    assert (
        "Populate every scope qualifier that a reader without the paper needs to "
        "interpret the result" in prompts["extractor"]
    )
    assert (
        "A scope value that comes from an interpretation span belongs in "
        "question_context" in prompts["extractor"]
    )
    assert (
        "CONTEXT_ONLY_SOURCE supports question_context statements only."
        in (prompts["question_writer"])
    )
    assert (
        "CONTEXT_ONLY_SOURCE supports question_context statements only."
        in (prompts["reconstructor"])
    )
    assert (
        "CONTEXT_ONLY_SOURCE supports question_context statements only."
        in (prompts["answer_verifier"])
    )
    assert (
        "CONTEXT_ONLY_SOURCE supports question_context statements only."
        in (prompts["option_verifier"])
    )
    assert (
        "Populate only scope qualifiers stated verbatim in the QUESTION"
        in prompts["reconstructor"]
    )
    assert (
        "Independently verify every non-null ANSWER_RECORD.scope value"
        in prompts["answer_verifier"]
    )
    assert (
        "Do not assume any proposed scope value is true" in prompts["answer_verifier"]
    )

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
    assert broker.status()["generation_submissions"] == 11
    assert transport.methods.count("generateContent") == 11
    jobs = [
        json.loads(path.read_text()) for path in (eligibility / "jobs").glob("*.json")
    ]
    assert len(jobs) == 2
    assert (
        sum(job.get("execution_authority") == "shared_gemini_broker" for job in jobs)
        == 1
    )


def test_same_campaign_regenerates_a_stale_terminal_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    first = run_stream(
        database,
        paths.namespace,
        run_id="historical-invocation",
        campaign_id="same-campaign",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=FakeProvider("fake-gemini", FIXTURES / "fake-author.jsonl"),
        verifier=FakeProvider("fake-gemini", FIXTURES / "fake-verifier.jsonl"),
        max_papers=1,
    )
    old_item = first["paper_results"][0]
    assert old_item["disposition"] == "accepted"
    monkeypatch.setattr(
        generation_module, "PROMPT_VERSION", "arctic-qa-generation-test-next"
    )
    rerun_author = tmp_path / "rerun-author.jsonl"
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl").read_text().splitlines()
    ]
    rerun_author.write_text(
        "\n".join(
            json.dumps(event)
            for event in author_events
            if event.get("role") != "extractor"
        )
        + "\n",
        encoding="utf-8",
    )

    second = run_stream(
        database,
        paths.namespace,
        run_id="rerun-invocation",
        campaign_id="same-campaign",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=FakeProvider("fake-gemini", rerun_author),
        verifier=FakeProvider("fake-gemini", FIXTURES / "fake-verifier.jsonl"),
        max_papers=1,
    )

    candidates = database.rows(
        "SELECT item_id,candidate_json FROM candidates "
        "WHERE run_id='same-campaign' ORDER BY created_at,item_id"
    )
    assert second["resumed_papers"] == 1
    assert second["counts"]["generation_rejected"] == 1
    assert len(candidates) == 2
    assert {
        json.loads(row["candidate_json"])["provenance"]["prompt_version"]
        for row in candidates
    } == {
        "arctic-qa-generation-v23",
        "arctic-qa-generation-test-next",
    }


def test_new_campaign_regenerates_a_paper_with_historical_accepted_output(
    tmp_path: Path,
) -> None:
    access, historical_eligibility = streaming_fixture(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")

    historical = run_stream(
        database,
        paths.namespace,
        run_id="historical-trial-invocation",
        campaign_id="historical-trial-campaign",
        access_run_dir=access,
        eligibility_run_dir=historical_eligibility,
        author=FakeProvider("fake-gemini", FIXTURES / "fake-author.jsonl"),
        verifier=FakeProvider("fake-gemini", FIXTURES / "fake-verifier.jsonl"),
        max_papers=1,
    )
    historical_item = database.one(
        "SELECT item_id FROM candidates WHERE run_id='historical-trial-campaign'"
    )["item_id"]
    assert historical["counts"]["accepted_base_questions"] == 1

    production_eligibility = tmp_path / "production-eligibility"
    transport = ScriptedBrokerTransport()
    broker = shared_broker(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="new-production-invocation",
    )
    production = run_stream(
        database,
        paths.namespace,
        run_id="new-production-invocation",
        campaign_id="new-production-campaign",
        access_run_dir=access,
        eligibility_run_dir=production_eligibility,
        author=provider,
        verifier=provider,
        max_papers=1,
        **broker_eligibility_inputs(tmp_path),
    )

    production_item = database.one(
        "SELECT item_id FROM candidates WHERE run_id='new-production-campaign'"
    )["item_id"]
    assert production["counts"]["accepted_base_questions"] == 1
    assert production["resumed_papers"] == 0
    assert production_item != historical_item
    assert broker.status()["generation_submissions"] == 11
    assert transport.methods.count("generateContent") == 11
    assert (
        database.one(
            "SELECT COUNT(*) AS count FROM calls WHERE run_id='new-production-campaign'"
        )["count"]
        == 11
    )
    jobs = list((production_eligibility / "jobs").glob("*.json"))
    assert len(jobs) == 1
    assert (
        json.loads(jobs[0].read_text(encoding="utf-8"))["execution_authority"]
        == "shared_gemini_broker"
    )


def test_streaming_live_gate_binds_reviewed_access_input_before_transport(
    tmp_path: Path,
) -> None:
    reviewed_access, reviewed_eligibility = streaming_fixture(tmp_path / "reviewed")
    substituted_access, _ = streaming_fixture(tmp_path / "substituted")
    manifest_path = reviewed_access / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "schema": "article-access-manifest-v1",
            "frozen_manifest_sha256": "test-only-frozen-manifest",
            "remaining_order_sha256": "test-only-frozen-order",
        }
    )
    write_json(manifest_path, manifest)
    receipt_path = reviewed_access / "run-receipt.json"
    write_json(
        receipt_path,
        {
            "schema": "article-access-run-receipt-v1",
            "state": "completed",
            "run_id": manifest["run_id"],
            "run_manifest_sha256": sha256_file(manifest_path),
            "remaining_order_sha256": manifest["remaining_order_sha256"],
            "counts": {"target": manifest["target_total"]},
        },
    )
    run_root = tmp_path / "run"
    run_root.mkdir()
    paths = DataPaths.open(run_root, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = ScriptedBrokerTransport()
    broker = shared_broker(run_root, transport)
    eligibility_inputs = broker_eligibility_inputs(run_root)
    gate_path = run_root / "broker-gate.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate.update(
        {
            "continuation_input_binding_version": "stream-input-binding-v1",
            "continuation_artifact": str(reviewed_access.resolve()),
            "continuation_access_run_id": manifest["run_id"],
            "continuation_run_manifest_sha256": sha256_file(manifest_path),
            "continuation_run_receipt_sha256": sha256_file(receipt_path),
            "continuation_frozen_manifest_sha256": manifest["frozen_manifest_sha256"],
            "continuation_order_sha256": manifest["remaining_order_sha256"],
            "continuation_family_count": manifest["target_total"],
            "authorized_new_run_id": "input-bound-live-test",
            "authorized_campaign_id": "input-bound-live-test",
            "eligibility_prompt_sha256": sha256_file(
                eligibility_inputs["eligibility_prompt_file"]
            ),
            "eligibility_schema_sha256": sha256_file(
                eligibility_inputs["eligibility_schema_file"]
            ),
            "eligibility_policy_sha256": sha256_file(
                eligibility_inputs["eligibility_policy_file"]
            ),
        }
    )
    write_json(gate_path, gate)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="input-bound-live-test",
    )
    arguments = {
        "db": database,
        "namespace": paths.namespace,
        "run_id": "input-bound-live-test",
        "campaign_id": "input-bound-live-test",
        "eligibility_run_dir": reviewed_eligibility,
        "author": provider,
        "verifier": provider,
        "max_papers": 1,
        **eligibility_inputs,
    }

    with pytest.raises(ValueError, match="reviewed continuation input"):
        run_stream(**arguments, access_run_dir=substituted_access)
    assert transport.methods == []
    substituted_prompt = run_root / "substituted-eligibility-prompt.txt"
    substituted_prompt.write_text(
        eligibility_inputs["eligibility_prompt_file"].read_text(encoding="utf-8")
        + "\nsubstituted\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reviewed continuation eligibility inputs"):
        run_stream(
            **{
                **arguments,
                "access_run_dir": reviewed_access,
                "eligibility_prompt_file": substituted_prompt,
            }
        )
    with pytest.raises(ValueError, match="reviewed continuation run identity"):
        run_stream(
            **{
                **arguments,
                "access_run_dir": reviewed_access,
                "run_id": "substituted-run",
            }
        )
    with pytest.raises(ValueError, match="reviewed continuation run identity"):
        run_stream(
            **{
                **arguments,
                "access_run_dir": reviewed_access,
                "campaign_id": "substituted-campaign",
            }
        )
    assert transport.methods == []

    result = run_stream(**arguments, access_run_dir=reviewed_access)

    assert result["counts"]["accepted_base_questions"] == 1
    assert transport.methods.count("generateContent") == 11


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
    assert transport.methods.count("generateContent") == 11
    status = restarted_broker.status()
    assert status["generation_submissions"] == 11
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
    assert len(transport.generation_configs) == 11
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
    assert broker.status()["generation_submissions"] == 11
    assert transport.methods.count("generateContent") == 11


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
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    alternative_author_script = tmp_path / "ambiguous-alternative-author.jsonl"
    alternative_author_script.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                author_events[0],
                author_events[1],
                author_events[1],
                author_events[0],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    verifier_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-verifier.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    verifier_events[1]["response"]["ambiguity_label"] = "multiple_answers"
    verifier_events[1]["response"]["alternatives"] = ["1.9 m"]
    verifier_script = tmp_path / "ambiguous-verifier.jsonl"
    verifier_script.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                *verifier_events[:3],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = ScriptedBrokerTransport(
        author_script=alternative_author_script,
        verifier_script=verifier_script,
    )
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
    assert status["generation_submissions"] == 8
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
    verifier_events[3]["response"]["contradiction_established"] = False
    verifier_events[3]["response"]["question_admits_option_as_correct"] = True
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def clear_test_rate_window(broker: SharedGeminiBroker) -> None:
        ledger = json.loads(broker.ledger_file.read_text(encoding="utf-8"))
        ledger["recent_submission_times_utc"] = []
        write_json(broker.ledger_file, ledger)

    monkeypatch.setattr(SharedGeminiBroker, "_pace", clear_test_rate_window)
    access, eligibility = streaming_fixture(tmp_path)
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    alternative_author_script = tmp_path / "incomplete-alternative-author.jsonl"
    alternative_author_script.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                author_events[0],
                author_events[1],
                author_events[2],
                author_events[2],
                author_events[2],
                author_events[0],
                author_events[2],
                author_events[2],
                author_events[2],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    verifier_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-verifier.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    verifier_events[3]["response"]["question_admits_option_as_correct"] = True
    verifier_events[4]["response"]["question_admits_option_as_correct"] = True
    verifier_script = tmp_path / "two-accepted-distractor-verifier.jsonl"
    verifier_script.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                *verifier_events,
                *json.loads(json.dumps(verifier_events[3:])),
                *json.loads(json.dumps(verifier_events[3:])),
                *json.loads(json.dumps(verifier_events[3:])),
                *json.loads(json.dumps(verifier_events[3:])),
                *json.loads(json.dumps(verifier_events[3:])),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = ScriptedBrokerTransport(
        author_script=alternative_author_script,
        verifier_script=verifier_script,
    )
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

    attempts = [
        json.loads(row["candidate_json"])
        for row in database.rows("SELECT candidate_json FROM candidates")
    ]
    assert {attempt["question"] for attempt in attempts} == {
        "What reported water depth was documented?"
    }
    assert broker.status()["stages"]["question_generation"]["submissions"] == 1
    assert broker.status()["stages"]["distractor_generation"]["submissions"] == 3
    assert (
        sum(
            bool(attempt["provenance"].get("distractor_only_retry"))
            for attempt in attempts
        )
        == 2
    )

    base = database.one("SELECT * FROM candidates WHERE status='incomplete_non_mcq'")
    targeted_author = FakeProvider("gemini-3.8-flash", FIXTURES / "fake-author.jsonl")
    targeted_verifier = FakeProvider(
        "gemini-3.8-flash", FIXTURES / "fake-verifier.jsonl"
    )
    targeted_author.position = 2
    targeted_verifier.position = 3
    resumed = resume_candidate_distractors(
        database,
        paths.namespace,
        item_id=base["item_id"],
        author=targeted_author,
        verifier=targeted_verifier,
    )
    resumed_validation = validation_module.validate_candidate(
        database, paths.namespace, resumed
    ).as_dict()
    assert resumed["item_id"] != base["item_id"]
    assert resumed["provenance"]["targeted_regression"] is True
    assert resumed_validation["labels"]["mcq_eligible"] is True
    assert (
        database.one(
            "SELECT status FROM candidates WHERE item_id=?", (base["item_id"],)
        )["status"]
        == "incomplete_non_mcq"
    )


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


def test_streaming_records_invalid_finding_and_advances_to_next_paper(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    ready_path = access / "items" / "item-000001.json"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    ready["position"] = 2
    write_json(ready_path, ready)
    rejected = {
        **ready,
        "position": 1,
        "candidate_key": "test-only:invalid-finding",
        "title": "Synthetic source with an invalid finding response",
    }
    write_json(access / "items" / "item-000000.json", rejected)
    manifest_path = access / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["target_total"] = 2
    manifest["selection"] = [
        {
            "position": 1,
            "candidate_key": rejected["candidate_key"],
            "subgroup": "test_only",
            "authors": ["Arctic QA test suite"],
            "year": 2026,
        },
        {
            "position": 2,
            "candidate_key": ready["candidate_key"],
            "subgroup": "test_only",
            "authors": ["Arctic QA test suite"],
            "year": 2026,
        },
    ]
    write_json(manifest_path, manifest)

    eligibility_path = eligibility / "jobs" / "fixture-job.json"
    rejected_eligibility = json.loads(eligibility_path.read_text(encoding="utf-8"))
    rejected_eligibility["job_key"] = "fixture-invalid-finding"
    rejected_eligibility["candidate_key"] = rejected["candidate_key"]
    rejected_eligibility["parsed_response"]["request_id"] = rejected_eligibility[
        "job_key"
    ]
    write_json(
        eligibility / "jobs" / "fixture-invalid-finding.json",
        rejected_eligibility,
    )

    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    invalid_extractor = json.loads(json.dumps(author_events[0]))
    invalid_extractor["response"]["candidate_findings"][0]["answer"][
        "source_span_id"
    ] = "evidence-span-does-not-exist"
    author_script = tmp_path / "invalid-finding-then-valid-author.jsonl"
    author_script.write_text(
        "\n".join(json.dumps(event) for event in [invalid_extractor, *author_events])
        + "\n",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        "stream-invalid-finding-continues",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(author_script),
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
        "--max-papers",
        "2",
    )

    assert result["counts"]["processed"] == 2
    assert result["counts"]["generation_rejected"] == 1
    assert result["counts"]["accepted_base_questions"] == 1
    assert result["paper_results"][0] == {
        "candidate_key": "test-only:invalid-finding",
        "disposition": "generation_rejected",
        "reason_codes": ["finding_evidence_span_not_found"],
        "source_id": stable_id("src", "test-only:invalid-finding"),
    }
    rejection_path = tmp_path / "arctic-qa" / result["export"]["files"]["rejections"]
    rejections = [json.loads(line) for line in rejection_path.read_text().splitlines()]
    assert [row["reason_code"] for row in rejections] == [
        "finding_evidence_span_not_found"
    ]


def test_live_stream_records_schema_invalid_reconstruction_and_advances(
    tmp_path: Path,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    ready_path = access / "items" / "item-000001.json"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    ready["position"] = 2
    write_json(ready_path, ready)
    invalid_source = access / "originals" / "invalid-reconstruction.html"
    invalid_source.write_bytes(
        Path(ready["source_path"]).read_bytes()
        + b"\n<!-- distinct invalid reconstruction fixture -->\n"
    )
    invalid_extraction = access / "extracted" / "invalid-reconstruction.txt"
    invalid_extraction.write_bytes(
        Path(ready["extraction_path"]).read_bytes()
        + b"\nDistinct invalid reconstruction fixture.\n"
    )
    invalid = {
        **ready,
        "position": 1,
        "candidate_key": "test-only:invalid-reconstruction-schema",
        "title": "Synthetic source with invalid reconstruction numeric metadata",
        "source_path": str(invalid_source),
        "source_content_hash": sha256(invalid_source.read_bytes()).hexdigest(),
        "extraction_path": str(invalid_extraction),
        "extraction_sha256": sha256(invalid_extraction.read_bytes()).hexdigest(),
        "final_url": "https://example.invalid/invalid-reconstruction.html",
    }
    write_json(access / "items" / "item-000000.json", invalid)
    manifest_path = access / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["target_total"] = 2
    manifest["selection"] = [
        {
            "position": 1,
            "candidate_key": invalid["candidate_key"],
            "subgroup": "test_only",
            "authors": ["Arctic QA test suite"],
            "year": 2026,
        },
        {
            "position": 2,
            "candidate_key": ready["candidate_key"],
            "subgroup": "test_only",
            "authors": ["Arctic QA test suite"],
            "year": 2026,
        },
    ]
    write_json(manifest_path, manifest)

    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    verifier_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-verifier.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    invalid_reconstruction = json.loads(json.dumps(verifier_events[1]))
    invalid_reconstruction["response"]["numeric"] = {
        "canonical_value": "",
        "unit": "",
    }
    author_script = tmp_path / "schema-invalid-then-valid-author.jsonl"
    verifier_script = tmp_path / "schema-invalid-then-valid-verifier.jsonl"
    author_script.write_text(
        "\n".join(json.dumps(event) for event in [*author_events[:2], *author_events])
        + "\n",
        encoding="utf-8",
    )
    verifier_script.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                verifier_events[0],
                invalid_reconstruction,
                *verifier_events,
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    transport = ScriptedBrokerTransport(
        author_script=author_script,
        verifier_script=verifier_script,
    )
    broker = shared_broker(tmp_path, transport)
    provider = BrokerProvider(
        broker=broker,
        phase="live_test",
        invocation_run_id="schema-invalid-reconstruction-r1",
    )

    result = run_stream(
        database,
        paths.namespace,
        run_id="schema-invalid-reconstruction-r1",
        campaign_id="streaming-commission",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=provider,
        verifier=provider,
        max_papers=2,
        **broker_eligibility_inputs(tmp_path),
    )

    assert result["counts"]["processed"] == 2
    assert result["counts"]["generation_rejected"] == 1
    assert result["counts"]["accepted_base_questions"] == 1
    assert result["paper_results"][0]["reason_codes"] == [
        "reconstructor_response_invalid"
    ]
    assert transport.methods.count("generateContent") == 16
    status = broker.status()
    assert status["generation_submissions"] == 16
    assert Decimal(status["reserved_usd"]) == 0
    assert Decimal(status["ambiguous_reserved_usd"]) == 0
    invalid_receipts = [
        row
        for row in json.loads((tmp_path / "shared-ledger.json").read_text())[
            "requests"
        ].values()
        if row["paper_id"] == invalid["candidate_key"]
        and row["stage"] == "blinded_reconstruction"
    ]
    assert len(invalid_receipts) == 1
    assert invalid_receipts[0]["state"] == "completed"

    resumed = run_stream(
        database,
        paths.namespace,
        run_id="schema-invalid-reconstruction-r1",
        campaign_id="streaming-commission",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=provider,
        verifier=provider,
        max_papers=2,
        **broker_eligibility_inputs(tmp_path),
    )

    assert resumed["counts"]["processed"] == 2
    assert resumed["counts"]["generation_rejected"] == 1
    assert resumed["counts"]["accepted_base_questions"] == 1
    assert transport.methods.count("generateContent") == 16
    assert broker.status()["generation_submissions"] == 16


@pytest.mark.parametrize(
    ("role", "event_index", "reason_code"),
    [
        ("reconstructor", 1, "reconstruction_evidence_span_not_found"),
        ("answer_verifier", 2, "answer_verifier_evidence_span_not_found"),
        ("distractor_writer", 2, "distractor_evidence_span_not_found"),
        ("option_verifier", 3, "option_verifier_evidence_span_not_found"),
    ],
)
def test_streaming_rejects_invalid_downstream_source_span_selection(
    tmp_path: Path, role: str, event_index: int, reason_code: str
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    author_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    verifier_events = [
        json.loads(line)
        for line in (FIXTURES / "fake-verifier.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    events = author_events if role == "distractor_writer" else verifier_events
    response = events[event_index]["response"]
    if role == "distractor_writer":
        response["distractors"][0]["source_span_id"] = "unknown-source-span"
    else:
        response["source_span_id"] = "unknown-source-span"
    author_script = tmp_path / f"invalid-{role}-author.jsonl"
    verifier_script = tmp_path / f"invalid-{role}-verifier.jsonl"
    author_script.write_text(
        "\n".join(json.dumps(event) for event in author_events) + "\n",
        encoding="utf-8",
    )
    verifier_script.write_text(
        "\n".join(json.dumps(event) for event in verifier_events) + "\n",
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "stream",
        "--run-id",
        f"stream-invalid-{role}",
        "--access-run-dir",
        str(access),
        "--eligibility-run-dir",
        str(eligibility),
        "--author-script",
        str(author_script),
        "--verifier-script",
        str(verifier_script),
    )

    assert result["counts"]["accepted_base_questions"] == 0
    assert result["counts"]["generation_rejected"] == 1
    assert result["paper_results"] == [
        {
            "candidate_key": "test-only:streaming-paper",
            "disposition": "generation_rejected",
            "reason_codes": [reason_code],
            "source_id": stable_id("src", "test-only:streaming-paper"),
        }
    ]
    rejection_path = tmp_path / "arctic-qa" / result["export"]["files"]["rejections"]
    rejections = [json.loads(line) for line in rejection_path.read_text().splitlines()]
    assert [row["reason_code"] for row in rejections] == [reason_code]


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


@pytest.mark.parametrize(
    ("message", "skipped"),
    [(PER_REQUEST_CAP_REASON, True), (AUTHORIZED_CAP_REASON, False)],
)
def test_streaming_skips_only_a_request_above_the_per_request_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    skipped: bool,
) -> None:
    access, eligibility = streaming_fixture(tmp_path)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")

    def budget_stop(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise BudgetError(message)

    monkeypatch.setattr(streaming_module, "generate_candidate", budget_stop)
    arguments = {
        "db": database,
        "namespace": paths.namespace,
        "run_id": "request-bound",
        "campaign_id": "request-bound",
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "author": FakeProvider("fake-gemini", FIXTURES / "fake-author.jsonl"),
        "verifier": FakeProvider("fake-gemini", FIXTURES / "fake-verifier.jsonl"),
        "max_papers": 1,
    }
    if not skipped:
        with pytest.raises(BudgetError, match=re.escape(message)):
            run_stream(**arguments)
        return

    result = run_stream(**arguments)

    assert result["paper_results"][0]["disposition"] == "generation_rejected"
    assert result["paper_results"][0]["reason_codes"] == ["request_cost_bound_exceeded"]


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
    assert status["calls"] == [{"count": 10, "status": "completed"}]


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
    assert status["calls"] == [{"count": 10, "status": "completed"}]


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
