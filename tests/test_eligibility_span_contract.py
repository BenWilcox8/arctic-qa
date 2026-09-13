from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import arctic_qa.gemini_eligibility as eligibility
from arctic_qa.streaming import _validate_pair


ROOT = Path(__file__).parents[1]
MAPPING_VERSION = "eligibility-criterion-status-map-v1"


def _hashes(extraction_sha256: str, manifest_sha256: str) -> dict[str, str]:
    return {
        "policy_sha256": "a" * 64,
        "source_version_sha256": "b" * 64,
        "extracted_text_sha256": extraction_sha256,
        "metadata_sha256": "c" * 64,
        "span_manifest_sha256": manifest_sha256,
    }


def _response(
    request_id: str,
    hashes: dict[str, str],
    selected: dict[str, list[str]],
    *,
    statuses: dict[str, str] | None = None,
) -> dict[str, object]:
    statuses = statuses or {}
    criteria = []
    for criterion in eligibility.CRITERIA:
        status = statuses.get(
            criterion,
            "uncertain"
            if criterion == "correction_retraction_coverage"
            else "satisfied",
        )
        criteria.append(
            {
                "criterion_id": criterion,
                "status": status,
                "reason_codes": [f"test_{status}"],
                "evidence": (
                    [{"span_ids": selected[criterion]}] if status != "uncertain" else []
                ),
                "missing_context": (
                    ["No correction registry metadata was supplied."]
                    if status == "uncertain"
                    else []
                ),
            }
        )
    return {
        "schema_version": "eligibility-response-v2",
        "status_mapping_version": MAPPING_VERSION,
        "request_id": request_id,
        "criteria": criteria,
        "known_missing_context": ["correction_retraction_coverage:unknown"],
        "correction_metadata_used": {
            "provided": False,
            "known_status": "unknown",
            "source": None,
            "as_of": None,
        },
        "input_echo": hashes,
    }


def _case() -> tuple[str, list[dict[str, object]], dict[str, list[str]]]:
    text = (
        "Findings begin in the left column.\n"
        "RIGHT COLUMN INTERRUPTION\n"
        "Findings continue in the left column.\n"
        "DOI 10.1234/example identifies this version.\n"
        "The study sampled the Arctic Ocean.\n"
        "This article uses the CC BY 4.0 license.\n"
    )
    extraction_sha256 = sha256(text.encode()).hexdigest()
    blocks = eligibility._span_blocks_v2(text, extraction_sha256)
    spans = [span for block in blocks for span in block["spans"]]
    selected = {
        "published_primary_findings": [spans[0]["span_id"], spans[2]["span_id"]],
        "stable_identity_version": [spans[3]["span_id"]],
        "study_geography": [spans[4]["span_id"]],
        "access_rights_evidence": [spans[5]["span_id"]],
    }
    return text, blocks, selected


def _validate(
    value: dict[str, object], blocks: list[dict[str, object]]
) -> dict[str, object]:
    schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v2.schema.json").read_text()
    )
    return eligibility.validate_response(
        value,
        blocks,
        expected={
            "request_id": "request-v2",
            "input_echo": value["input_echo"],
            "correction_metadata": value["correction_metadata_used"],
            "known_context_gaps": ["correction_retraction_coverage:unknown"],
        },
        response_schema=schema,
    )


def test_v2_selects_exact_source_spans_across_two_column_interruption() -> None:
    text, blocks, selected = _case()
    manifest = eligibility._span_manifest_v2(blocks)
    hashes = _hashes(
        sha256(text.encode()).hexdigest(),
        sha256(eligibility.canonical_json(manifest).encode()).hexdigest(),
    )
    result = _validate(_response("request-v2", hashes, selected), blocks)

    assert result["valid"] is True
    assert result["decision"] == "eligible"
    assert result["mapping_version"] == MAPPING_VERSION
    findings = next(
        row
        for row in result["resolved_evidence"]
        if row["criterion"] == "published_primary_findings"
    )
    assert [part["quote"] for part in findings["spans"]] == [
        "Findings begin in the left column.\n",
        "Findings continue in the left column.\n",
    ]
    assert (
        "quote"
        not in _response("request-v2", hashes, selected)["criteria"][0]["evidence"][0]
    )


def test_v2_rejects_unknown_and_duplicate_selected_span_ids() -> None:
    text, blocks, selected = _case()
    manifest = eligibility._span_manifest_v2(blocks)
    hashes = _hashes(
        sha256(text.encode()).hexdigest(),
        sha256(eligibility.canonical_json(manifest).encode()).hexdigest(),
    )
    unknown = json.loads(json.dumps(selected))
    unknown["study_geography"] = ["s999999"]
    unknown_result = _validate(_response("request-v2", hashes, unknown), blocks)
    assert unknown_result["valid"] is False
    assert "evidence_span_unknown:study_geography" in unknown_result["errors"]

    repeated = json.loads(json.dumps(selected))
    repeated["study_geography"] *= 2
    repeated_result = _validate(_response("request-v2", hashes, repeated), blocks)
    assert repeated_result["valid"] is False
    assert "evidence_span_duplicate:study_geography" in repeated_result["errors"]


def test_v2_allows_one_verified_span_to_support_separate_criteria() -> None:
    text, blocks, selected = _case()
    manifest = eligibility._span_manifest_v2(blocks)
    hashes = _hashes(
        sha256(text.encode()).hexdigest(),
        sha256(eligibility.canonical_json(manifest).encode()).hexdigest(),
    )
    selected["study_geography"] = selected["stable_identity_version"]

    result = _validate(_response("request-v2", hashes, selected), blocks)

    assert result["valid"] is True
    identity = next(
        row
        for row in result["resolved_evidence"]
        if row["criterion"] == "stable_identity_version"
    )
    geography = next(
        row
        for row in result["resolved_evidence"]
        if row["criterion"] == "study_geography"
    )
    assert geography["span_ids"] == identity["span_ids"]


def test_v2_rejects_changed_and_out_of_bounds_internal_bindings() -> None:
    text, blocks, selected = _case()
    manifest = eligibility._span_manifest_v2(blocks)
    hashes = _hashes(
        sha256(text.encode()).hexdigest(),
        sha256(eligibility.canonical_json(manifest).encode()).hexdigest(),
    )
    value = _response("request-v2", hashes, selected)

    changed = json.loads(json.dumps(blocks))
    changed[0]["spans"][0]["text"] = "changed\n"
    changed_result = _validate(value, changed)
    assert changed_result["valid"] is False
    assert "evidence_catalog_changed" in changed_result["errors"]

    out_of_bounds = json.loads(json.dumps(blocks))
    out_of_bounds[0]["spans"][0]["end_byte"] = blocks[0]["end_byte"] + 1
    bounds_result = _validate(value, out_of_bounds)
    assert bounds_result["valid"] is False
    assert "evidence_span_out_of_bounds" in bounds_result["errors"]


def test_v2_schema_removes_generated_overall_and_is_version_isolated() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v2.schema.json").read_text()
    )
    assert schema["properties"]["schema_version"]["const"] == (
        "eligibility-response-v2"
    )
    assert "overall" not in schema["properties"]
    assert "overall_reason_codes" not in schema["properties"]

    text, blocks, selected = _case()
    manifest = eligibility._span_manifest_v2(blocks)
    hashes = _hashes(
        sha256(text.encode()).hexdigest(),
        sha256(eligibility.canonical_json(manifest).encode()).hexdigest(),
    )
    value = _response("request-v2", hashes, selected)
    value["schema_version"] = "eligibility-response-v1"
    result = _validate(value, blocks)
    assert result["valid"] is False
    assert "response_identity_mismatch" in result["errors"]


def test_v2_derives_frozen_status_mapping_without_model_overall() -> None:
    text, blocks, selected = _case()
    manifest = eligibility._span_manifest_v2(blocks)
    hashes = _hashes(
        sha256(text.encode()).hexdigest(),
        sha256(eligibility.canonical_json(manifest).encode()).hexdigest(),
    )

    eligible = _validate(_response("request-v2", hashes, selected), blocks)
    assert eligible["decision"] == "eligible"
    assert eligible["overall_reason_codes"] == ["all_required_criteria_satisfied"]

    excluded = _validate(
        _response(
            "request-v2",
            hashes,
            selected,
            statuses={"study_geography": "failed"},
        ),
        blocks,
    )
    assert excluded["valid"] is True
    assert excluded["decision"] == "excluded"
    assert excluded["overall_reason_codes"] == ["criterion_failed:study_geography"]

    uncertain_selected = dict(selected)
    uncertain_selected.pop("study_geography")
    uncertain = _validate(
        _response(
            "request-v2",
            hashes,
            uncertain_selected,
            statuses={"study_geography": "uncertain"},
        ),
        blocks,
    )
    assert uncertain["valid"] is True
    assert uncertain["decision"] == "uncertain"
    assert uncertain["overall_reason_codes"] == ["criterion_unresolved:study_geography"]


def test_v2_request_renders_source_once_with_short_ids_and_manifest_hash() -> None:
    text, blocks, _ = _case()
    extraction_sha256 = sha256(text.encode()).hexdigest()
    source = {
        "candidate_key": "test",
        "title": "Test",
        "doi": "10.1234/test",
        "authors": ["Researcher"],
        "year": 2026,
        "subgroup": "test-only",
        "source_content_hash": "b" * 64,
        "extraction_sha256": extraction_sha256,
        "extraction_coverage": {"article_body_recognized": True},
        "media_type": "application/pdf",
        "final_url": "https://example.invalid/test.pdf",
    }
    schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v2.schema.json").read_text()
    )
    config = eligibility._config(ROOT / "config" / "gemini-eligibility-v1.json")
    payload, hashes = eligibility._request_payload(
        source=source,
        policy={"protocol_id": "test-only"},
        text=text,
        prompt=(ROOT / "config" / "gemini-eligibility-prompt-v4.txt").read_text(),
        schema=schema,
        config=config,
        request_id="request-v2",
        policy_sha256="a" * 64,
    )
    rendered = payload["contents"][0]["parts"][0]["text"]
    article = rendered.split("ARTICLE_SPANS_BEGIN\n", 1)[1].split(
        "ARTICLE_SPANS_END", 1
    )[0]
    for span in (span for block in blocks for span in block["spans"]):
        assert rendered.count(span["text"]) == 1
        assert len(span["span_id"]) <= 8
        assert span["block_sha256"] not in article
        assert span["span_sha256"] not in article
    assert hashes["span_manifest_sha256"] in rendered
    assert "ELIGIBILITY_SCREEN_REQUEST_V2" in rendered
    assert "ARTICLE_SPANS_BEGIN" in rendered
    assert "overall:" not in rendered


def test_v2_manifest_and_derived_decision_pass_one_finding_pair_validation(
    tmp_path: Path,
) -> None:
    text, blocks, selected = _case()
    extraction_sha256 = sha256(text.encode()).hexdigest()
    manifest = eligibility._span_manifest_v2(blocks)
    hashes = _hashes(
        extraction_sha256,
        sha256(eligibility.canonical_json(manifest).encode()).hexdigest(),
    )
    value = _response("request-v2", hashes, selected)
    source_path = tmp_path / "source.pdf"
    extraction_path = tmp_path / "text.txt"
    source_path.write_bytes(b"test-only source")
    extraction_path.write_text(text, encoding="utf-8")
    source_sha256 = sha256(source_path.read_bytes()).hexdigest()
    access = {
        "schema": "article-access-item-v1",
        "access_state": "full_text_ready",
        "identity_verified": True,
        "candidate_key": "test",
        "source_content_hash": source_sha256,
        "extraction_sha256": extraction_sha256,
        "source_path": str(source_path),
        "extraction_path": str(extraction_path),
    }
    value["input_echo"]["source_version_sha256"] = source_sha256
    validation = _validate(value, blocks)
    job = {
        "schema": "gemini-eligibility-job-v1",
        "state": "completed",
        "job_key": "request-v2",
        "candidate_key": "test",
        "source_content_hash": source_sha256,
        "extraction_sha256": extraction_sha256,
        "parsed_response": value,
        "validation": validation,
    }

    manifest_reference = eligibility._persist_span_manifest_v2(
        tmp_path / "run",
        "request-v2",
        text,
        extraction_sha256,
        json.loads(
            (ROOT / "schemas" / "gemini-eligibility.v2.schema.json").read_text()
        ),
    )
    saved_manifest = json.loads(
        Path(manifest_reference["span_manifest_path"]).read_text()
    )
    assert saved_manifest == manifest
    assert all("text" not in block for block in saved_manifest["blocks"])
    assert all(
        "text" not in span
        for block in saved_manifest["blocks"]
        for span in block["spans"]
    )
    _validate_pair(access, job)
