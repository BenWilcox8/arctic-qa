from __future__ import annotations

import csv
import json
from hashlib import sha256
from pathlib import Path

import pytest

import arctic_qa.gemini_eligibility as eligibility
from arctic_qa.generation import (
    CandidateRejectedError,
    _eligible_generation_scope,
    _finding_context,
    _require_arctic_scope_custody,
)
from arctic_qa.geography_correction import write_geography_correction_overlay
from arctic_qa.util import canonical_json, sha256_bytes


ROOT = Path(__file__).parents[1]


def _response(
    *,
    request_id: str,
    hashes: dict[str, str],
    selected: dict[str, list[str]],
    geography_status: str = "satisfied",
    scope: dict[str, object],
) -> dict[str, object]:
    criteria = []
    for criterion in eligibility.CRITERIA:
        status = (
            geography_status
            if criterion == "study_geography"
            else "uncertain"
            if criterion == "correction_retraction_coverage"
            else "satisfied"
        )
        criteria.append(
            {
                "criterion_id": criterion,
                "status": status,
                "reason_codes": [f"test_{status}"],
                "evidence": (
                    [{"span_ids": selected[criterion]}]
                    if status != "uncertain"
                    else []
                ),
                "missing_context": (
                    ["No correction registry metadata was supplied."]
                    if criterion == "correction_retraction_coverage"
                    else ["The available source does not locate the study activity."]
                    if status == "uncertain"
                    else []
                ),
            }
        )
    return {
        "schema_version": "eligibility-response-v3",
        "status_mapping_version": "eligibility-criterion-status-map-v1",
        "request_id": request_id,
        "criteria": criteria,
        "eligible_arctic_scope": scope,
        "known_missing_context": ["correction_retraction_coverage:unknown"],
        "correction_metadata_used": {
            "provided": False,
            "known_status": "unknown",
            "source": None,
            "as_of": None,
        },
        "input_echo": hashes,
    }


def _validate_v3(
    text: str,
    selected: dict[str, list[str]],
    *,
    geography_status: str = "satisfied",
    scope: dict[str, object],
) -> dict[str, object]:
    extraction_sha256 = sha256(text.encode()).hexdigest()
    blocks = eligibility._span_blocks_v2(text, extraction_sha256)
    manifest = eligibility._span_manifest_v2(
        blocks, eligibility.ELIGIBILITY_RESPONSE_V3
    )
    hashes = {
        "policy_sha256": "a" * 64,
        "source_version_sha256": "b" * 64,
        "extracted_text_sha256": extraction_sha256,
        "metadata_sha256": "c" * 64,
        "span_manifest_sha256": sha256(canonical_json(manifest).encode()).hexdigest(),
    }
    schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v3.schema.json").read_text()
    )
    return eligibility.validate_response(
        _response(
            request_id="geography-v3",
            hashes=hashes,
            selected=selected,
            geography_status=geography_status,
            scope=scope,
        ),
        blocks,
        expected={
            "request_id": "geography-v3",
            "input_echo": hashes,
            "correction_metadata": {
                "provided": False,
                "known_status": "unknown",
                "source": None,
                "as_of": None,
            },
            "known_context_gaps": ["correction_retraction_coverage:unknown"],
        },
        response_schema=schema,
    )


def test_explicit_northern_marine_latitude_qualifies_without_a_name_allowlist() -> None:
    text = (
        "Methods: We measured seawater at 82.6 N in the Eurasian Basin.\n"
        "Results: At 82.6 N, nitrate declined by 15 percent.\n"
        "DOI 10.9999/northern-marine identifies this version.\n"
        "This article uses the CC BY 4.0 license.\n"
    )
    blocks = eligibility._span_blocks_v2(text, sha256(text.encode()).hexdigest())
    spans = [span for block in blocks for span in block["spans"]]
    selected = {
        "published_primary_findings": [spans[1]["span_id"]],
        "stable_identity_version": [spans[2]["span_id"]],
        "study_geography": [spans[0]["span_id"]],
        "access_rights_evidence": [spans[3]["span_id"]],
    }
    result = _validate_v3(
        text,
        selected,
        scope={
            "component": "whole_study",
            "activity_span_ids": selected["study_geography"],
            "finding_span_ids": selected["published_primary_findings"],
            "question_scope_phrases": [],
        },
    )

    assert result["valid"] is True
    assert result["decision"] == "eligible"
    assert result["resolved_eligible_arctic_scope"]["component"] == "whole_study"


def test_separable_arctic_scope_limits_finding_context_and_requires_custody() -> None:
    arctic_quote = "At 76.2 N, Arctic station nitrate declined by 15 percent."
    southern_quote = "At 54.0 N, the southern station nitrate increased by 20 percent."
    chunks = [
        {
            "chunk_id": "chunk-1",
            "section_id": "results",
            "heading": "Results",
            "chunk_index": 0,
            "text": arctic_quote + "\n" + southern_quote,
        }
    ]
    source = {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "eligibility_job_key": "job-v3",
                "resolved_eligible_arctic_scope": {
                    "component": "separable_arctic_component",
                    "finding_spans": [
                        {
                            "quote": arctic_quote,
                            "source_bytes_sha256": sha256_bytes(
                                arctic_quote.encode()
                            ),
                        }
                    ],
                    "question_scope_phrases": ["Arctic station"],
                },
            }
        ),
    }

    scope, spans = _eligible_generation_scope(source, chunks)
    context, _ = _finding_context(chunks, spans)

    assert scope is not None
    assert arctic_quote in context
    assert southern_quote not in context
    _require_arctic_scope_custody(
        {
            "evidence_quote": arctic_quote,
            "required_question_phrases": ["Arctic station"],
        },
        scope,
    )
    with pytest.raises(CandidateRejectedError, match="scope"):
        _require_arctic_scope_custody(
            {
                "evidence_quote": arctic_quote,
                "required_question_phrases": [],
            },
            scope,
        )


def test_scope_finding_span_allows_only_whitespace_equivalent_chunk_text() -> None:
    eligibility_quote = "At 76.2 N, Arctic station nitrate declined by 15 percent.\n"
    chunk_quote = eligibility_quote.rstrip()
    source = {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "eligibility_job_key": "job-v3-whitespace",
                "resolved_eligible_arctic_scope": {
                    "component": "whole_study",
                    "finding_spans": [
                        {
                            "quote": eligibility_quote,
                            "source_bytes_sha256": sha256_bytes(
                                eligibility_quote.encode()
                            ),
                        }
                    ],
                    "question_scope_phrases": [],
                },
            }
        ),
    }

    _, spans = _eligible_generation_scope(
        source, [{"chunk_id": "chunk-1", "text": chunk_quote}]
    )

    assert spans is not None
    assert spans[0]["text"] == chunk_quote
    assert spans[0]["text_sha256"] == sha256_bytes(chunk_quote.encode())
    assert spans[0]["eligibility_quote_sha256"] == sha256_bytes(
        eligibility_quote.encode()
    )
    assert spans[0]["eligibility_match_kind"] == "whitespace_equivalent"


@pytest.mark.parametrize(
    ("eligibility_quote", "chunk_quote"),
    [
        (
            "At 76.2 N, Arctic station nitrate declined by 15 percent.",
            "At 76.2 N, Arctic station nitrate increased by 15 percent.",
        ),
        (
            "At 76.2 N, Arctic station nitrate declined by 15",
            "At 76.2 N, Arctic\nstation nitrate declined by 150",
        ),
        ("The concentration was 4.2 m", "The concentration\nwas 4.2 mg"),
        ("The sample was not retained", "The sample\nwas not retainedly"),
    ],
)
def test_scope_finding_span_word_difference_is_paper_local_rejection(
    eligibility_quote: str, chunk_quote: str
) -> None:
    source = {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "eligibility_job_key": "job-v3-word-mismatch",
                "resolved_eligible_arctic_scope": {
                    "component": "whole_study",
                    "finding_spans": [
                        {
                            "quote": eligibility_quote,
                            "source_bytes_sha256": sha256_bytes(
                                eligibility_quote.encode()
                            ),
                        }
                    ],
                    "question_scope_phrases": [],
                },
            }
        ),
    }

    with pytest.raises(CandidateRejectedError) as error:
        _eligible_generation_scope(
            source,
            [
                {
                    "chunk_id": "chunk-1",
                    "text": chunk_quote,
                }
            ],
        )

    assert error.value.reason_code == "eligible_arctic_scope_finding_unbound"


@pytest.mark.parametrize(
    "text,geography_status",
    [
        ("Methods: Sampling occurred in a boundary-crossing named region.\n", "uncertain"),
        ("Methods: Observations were made at 69.0 S in Antarctica.\n", "failed"),
        ("Title: Arctic change. Methods: Sampling occurred at 54.0 N.\n", "failed"),
    ],
)
def test_insufficient_southern_and_incidental_geography_cannot_create_scope(
    text: str, geography_status: str
) -> None:
    full_text = (
        text
        + "Results: This is a primary finding.\n"
        + "DOI 10.9999/control identifies this version.\n"
        + "This article uses the CC BY 4.0 license.\n"
    )
    blocks = eligibility._span_blocks_v2(
        full_text, sha256(full_text.encode()).hexdigest()
    )
    spans = [span for block in blocks for span in block["spans"]]
    selected = {
        "published_primary_findings": [spans[1]["span_id"]],
        "stable_identity_version": [spans[2]["span_id"]],
        "study_geography": [spans[0]["span_id"]],
        "access_rights_evidence": [spans[3]["span_id"]],
    }
    result = _validate_v3(
        full_text,
        selected,
        geography_status=geography_status,
        scope={
            "component": "none",
            "activity_span_ids": [],
            "finding_span_ids": [],
            "question_scope_phrases": [],
        },
    )

    assert result["valid"] is True
    assert result["decision"] in {"excluded", "uncertain"}


def test_correction_overlay_preserves_old_jobs_as_reviewed_proposals(
    tmp_path: Path,
) -> None:
    old_policy = tmp_path / "protocol-v2.json"
    new_policy = tmp_path / "protocol-v3.json"
    old_policy.write_text('{"version":"v2"}\n', encoding="utf-8")
    new_policy.write_text('{"version":"v3"}\n', encoding="utf-8")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    affected = tmp_path / "affected.csv"
    fields = [
        "doi",
        "evidence_locator",
        "evidence_summary",
        "recommended_disposition",
    ]
    with affected.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for number in range(16):
            doi = f"10.9999/reviewed-{number}"
            writer.writerow(
                {
                    "doi": doi,
                    "evidence_locator": f"text-block-00001:s{number:06d}",
                    "evidence_summary": "A reviewed Arctic study result.",
                    "recommended_disposition": "retain",
                }
            )
            (jobs / f"job-{number}.json").write_text(
                json.dumps(
                    {
                        "job_key": f"job-{number}",
                        "candidate_key": doi,
                        "policy_sha256": sha256_bytes(old_policy.read_bytes()),
                        "prompt_sha256": "p" * 64,
                        "schema_sha256": "s" * 64,
                        "source_content_hash": f"source-{number}",
                        "extraction_sha256": f"extract-{number}",
                        "validation": {"decision": "excluded"},
                    }
                ),
                encoding="utf-8",
            )

    result = write_geography_correction_overlay(
        affected_papers_file=affected,
        historical_jobs_dir=jobs,
        old_policy_file=old_policy,
        new_policy_file=new_policy,
        output_dir=tmp_path / "overlay",
        decision_source="test review",
        decision_at_utc="2026-09-14T00:00:00Z",
    )

    rows = (tmp_path / "overlay" / "geography-correction-overlay.ndjson").read_text(
        encoding="utf-8"
    ).splitlines()
    assert result["rows"] == 16
    assert len(rows) == 16
    first = json.loads(rows[0])
    assert first["authority"] == "reviewed_correction_proposal"
    assert first["model_decision"] is False
