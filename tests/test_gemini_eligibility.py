from __future__ import annotations

import json
import urllib.error
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import pytest

from arctic_qa.gemini_eligibility import (
    CRITERIA,
    _authorize_submission,
    _config,
    _request_payload,
    _segments,
    _settle_submission,
    _strict_json_loads,
    init_budget,
    reserve_budget,
    run_gemini_eligibility,
    validate_response,
)


ROOT = Path(__file__).parents[1]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def safety(live: bool) -> dict:
    return {
        "schema": "captain-gemini-initial-safety-policy-v1",
        "live_generation_enabled": live,
        "project_lifetime_ceiling_usd": "1000.00",
        "initial_phase_ceiling_usd": "1.00",
        "maximum_run_allocation_usd": "1.00",
        "maximum_request_reserved_cost_usd": "0.25",
        "maximum_generation_submissions_initial_phase": 3,
        "maximum_concurrent_generation_requests": 1,
        "minimum_seconds_between_generation_submissions": 60,
        "maximum_count_requests_initial_phase": 10,
        "maximum_run_wall_seconds": 1200,
        "maximum_output_tokens_including_thinking": 8192,
        "automatic_generation_retries": 0,
        "stop_on_first_error_or_ambiguous_charge": True,
        "automatic_model_fallback": False,
        "automatic_budget_rearm": False,
        "automatic_next_run": False,
        "key_presence_authorizes_generation": False,
    }


def fixture(tmp_path: Path, *, live: bool = True) -> dict[str, Path]:
    access = tmp_path / "access"
    run = tmp_path / "gemini"
    text = "Arctic samples were collected at 71 north. " + "Evidence text. " * 180
    extraction = access / "extracted" / "text.txt"
    extraction.parent.mkdir(parents=True)
    extraction.write_text(text, encoding="utf-8")
    source_hash = sha256(b"source object").hexdigest()
    row = {
        "schema": "article-access-item-v1",
        "run_id": "access-fixture",
        "position": 1,
        "candidate_key": "10.1234/arctic",
        "subgroup": "retained_article_type",
        "title": "A fixture article about Arctic samples",
        "doi": "10.1234/arctic",
        "authors": ["A. Researcher"],
        "year": 2026,
        "access_state": "full_text_ready",
        "identity_verified": True,
        "source_content_hash": source_hash,
        "extraction_path": str(extraction),
        "extraction_sha256": sha256(text.encode()).hexdigest(),
        "extraction_coverage": {"article_body_recognized": True},
        "media_type": "application/xml",
        "final_url": "https://example.org/article.xml",
    }
    write_json(access / "items" / "item-000001.json", row)
    write_json(
        access / "run-manifest.json",
        {
            "run_id": "access-fixture",
            "target_total": 1,
            "selection": [
                {
                    "position": 1,
                    "candidate_key": "10.1234/arctic",
                    "subgroup": "retained_article_type",
                }
            ],
        },
    )
    write_json(access / "progress.json", {"state": "completed"})
    write_json(access / "run-receipt.json", {"state": "completed"})
    safety_file = tmp_path / "safety.json"
    write_json(safety_file, safety(live))
    policy_file = tmp_path / "policy.json"
    write_json(policy_file, {"protocol_id": "fixture-frozen-policy"})
    return {
        "access": access,
        "run": run,
        "config": ROOT / "config" / "gemini-eligibility-v1.json",
        "prompt": ROOT / "config" / "gemini-eligibility-prompt-v3.txt",
        "schema": ROOT / "schemas" / "gemini-eligibility.v1.schema.json",
        "policy": policy_file,
        "safety": safety_file,
        "ledger": tmp_path / "project-budget.json",
        "credential": tmp_path / "private" / "key",
    }


def call(paths: dict[str, Path], action: str, transport=None) -> dict:
    return run_gemini_eligibility(
        action=action,
        access_run_dir=paths["access"],
        run_dir=paths["run"],
        config_file=paths["config"],
        prompt_file=paths["prompt"],
        schema_file=paths["schema"],
        policy_file=paths["policy"],
        safety_policy_file=paths["safety"],
        project_ledger_file=paths["ledger"],
        max_cost_usd=Decimal("1.00"),
        credential_file=paths["credential"],
        transport=transport,
    )


def test_standalone_live_actions_require_shared_broker(tmp_path: Path) -> None:
    paths = fixture(tmp_path, live=True)
    with pytest.raises(ValueError, match="shared streaming broker"):
        call(paths, "run")
    with pytest.raises(ValueError, match="shared streaming broker"):
        call(paths, "resume")


def response_value(
    request_id: str, hashes: dict, quote: str, known_gaps: list[str] | None = None
) -> dict:
    criteria = []
    for name in CRITERIA:
        status = (
            "uncertain" if name == "correction_retraction_coverage" else "satisfied"
        )
        criteria.append(
            {
                "criterion_id": name,
                "status": status,
                "reason_codes": ["fixture_evidence"],
                "evidence": []
                if status == "uncertain"
                else [
                    {
                        "quote": quote,
                        "locator": {
                            "source_block_id": "text-block-00001",
                            "page_id": None,
                            "section_id": "extracted-text",
                        },
                    }
                ],
                "missing_context": ["No correction registry metadata was supplied."]
                if status == "uncertain"
                else [],
            }
        )
    return {
        "schema_version": "eligibility-response-v1",
        "request_id": request_id,
        "overall": "eligible",
        "overall_reason_codes": ["all_required_criteria_satisfied"],
        "criteria": criteria,
        "known_missing_context": known_gaps
        if known_gaps is not None
        else ["correction_retraction_coverage:unknown"],
        "correction_metadata_used": {
            "provided": False,
            "known_status": "unknown",
            "source": None,
            "as_of": None,
        },
        "input_echo": hashes,
    }


class GoodTransport:
    def __init__(self) -> None:
        self.methods: list[str] = []

    def post(self, model: str, method: str, payload: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 120}
        content = payload["contents"][0]["parts"][0]["text"]
        request_id = content.split("request_id: ", 1)[1].splitlines()[0]
        hashes = json.loads(content.split("input_hashes: ", 1)[1].splitlines()[0])
        metadata = json.loads(content.split("metadata: ", 1)[1].splitlines()[0])
        value = response_value(
            request_id,
            hashes,
            "Arctic samples were collected at 71 north.",
            metadata["known_context_gaps"],
        )
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(value)}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 120,
                "candidatesTokenCount": 40,
                "thoughtsTokenCount": 10,
                "totalTokenCount": 170,
            },
            "modelVersion": "gemini-3.8-flash",
            "responseId": "fixture-response",
        }


def test_exact_quotes_and_policy_mapping() -> None:
    segments = _segments("prefix unique evidence suffix")
    hashes = {
        "policy_sha256": "a",
        "source_version_sha256": "b",
        "extracted_text_sha256": "c",
        "metadata_sha256": "d",
    }
    value = response_value("request", hashes, "unique evidence")
    expected = {
        "request_id": "request",
        "input_echo": hashes,
        "correction_metadata": value["correction_metadata_used"],
        "known_context_gaps": ["correction_retraction_coverage:unknown"],
    }
    response_schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v1.schema.json").read_text()
    )
    result = validate_response(
        value, segments, expected=expected, response_schema=response_schema
    )
    assert result["valid"] is True
    assert result["resolved_evidence"][0]["start"] == 7

    value["criteria"][0]["status"] = "failed"
    value["overall"] = "eligible"
    contradictory = validate_response(
        value, segments, expected=expected, response_schema=response_schema
    )
    assert contradictory["valid"] is False
    assert "overall_contradicts_criteria" in contradictory["errors"]


def test_local_schema_and_known_context_gaps_fail_closed() -> None:
    segments = _segments("prefix unique evidence suffix")
    hashes = {
        "policy_sha256": "a",
        "source_version_sha256": "b",
        "extracted_text_sha256": "c",
        "metadata_sha256": "d",
    }
    value = response_value("request", hashes, "unique evidence", [])
    expected = {
        "request_id": "request",
        "input_echo": hashes,
        "correction_metadata": value["correction_metadata_used"],
        "known_context_gaps": [
            "figures:unknown_not_extracted",
            "correction_retraction_coverage:unknown",
        ],
    }
    schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v1.schema.json").read_text()
    )
    hidden = validate_response(
        value, segments, expected=expected, response_schema=schema
    )
    assert hidden["valid"] is False
    assert "known_context_gap_hidden" in hidden["errors"]

    value["known_missing_context"] = list(expected["known_context_gaps"])
    value["criteria"][0]["evidence"][0]["locator"]["extra"] = "hidden"
    malformed = validate_response(
        value, segments, expected=expected, response_schema=schema
    )
    assert malformed["valid"] is False
    assert "schema_additional_field" in malformed["errors"]


def test_strict_json_rejects_duplicate_fields() -> None:
    with pytest.raises(ValueError, match="duplicate JSON field"):
        _strict_json_loads('{"overall":"eligible","overall":"excluded"}')


def test_prompt_keeps_full_text_and_disables_tools(tmp_path: Path) -> None:
    paths = fixture(tmp_path)
    source = json.loads((paths["access"] / "items" / "item-000001.json").read_text())
    text = Path(source["extraction_path"]).read_text()
    payload, hashes = _request_payload(
        source=source,
        policy={"frozen": True},
        text=text,
        prompt=paths["prompt"].read_text(encoding="utf-8"),
        schema={"type": "object"},
        config=_config(paths["config"]),
        request_id="request",
        policy_sha256="f" * 64,
    )
    serialized = json.dumps(payload)
    assert text in serialized
    assert "ARTICLE_TEXT_BEGIN" in serialized
    assert "source_block_id=text-block-00001" in serialized
    assert "page_id=None" in serialized
    assert "text-page" not in serialized
    user_text = payload["contents"][0]["parts"][0]["text"]
    metadata = json.loads(user_text.split("metadata: ", 1)[1].splitlines()[0])
    assert set(("authors", "year", "media_type", "final_url")) <= set(metadata)
    assert metadata["authors"] == ["A. Researcher"]
    assert metadata["year"] == 2026
    assert "tools" not in payload
    assert payload["store"] is False
    assert payload["generationConfig"]["candidateCount"] == 1
    assert (
        "Set schema_version to the exact string eligibility-response-v1."
        in payload["systemInstruction"]["parts"][0]["text"]
    )
    prompt = payload["systemInstruction"]["parts"][0]["text"]
    assert "Preserve every whitespace and Unicode character exactly" in prompt
    assert "occur exactly once in its cited source block" in prompt
    assert "extend the quote with adjacent exact text until it is unique" in prompt
    assert hashes["extracted_text_sha256"] == source["extraction_sha256"]


def test_saved_canary_prompt_remains_content_addressable() -> None:
    saved_prompt = ROOT / "config" / "gemini-eligibility-prompt-v1.txt"
    prior_prompt = ROOT / "config" / "gemini-eligibility-prompt-v2.txt"
    current_prompt = ROOT / "config" / "gemini-eligibility-prompt-v3.txt"

    assert sha256(saved_prompt.read_bytes()).hexdigest() == (
        "426d3fb8fa7cfdd41700a8b054c749b5934cd596fa5204ea5c9217338dc227a0"
    )
    assert sha256(prior_prompt.read_bytes()).hexdigest() == (
        "dc7d430383ede2f3f094d203a727845f85b2c64811c9a2716a9488e08456b996"
    )
    assert sha256(current_prompt.read_bytes()).hexdigest() == (
        "2b613a7d9e95aa300485624774c6a411293daa90a5a74ee8635a61ca632854e6"
    )


def test_pdf_indentation_evidence_requires_exact_whitespace() -> None:
    text = (ROOT / "fixtures" / "eligibility-pdf-indentation.txt").read_text()
    segments = _segments(text)
    hashes = {
        "policy_sha256": "a",
        "source_version_sha256": "b",
        "extracted_text_sha256": "c",
        "metadata_sha256": "d",
    }
    collapsed = (
        "Here we report Arctic observations from 71°N to "
        "the central Arctic Ocean at 87°N."
    )
    value = response_value("request", hashes, collapsed)
    expected = {
        "request_id": "request",
        "input_echo": hashes,
        "correction_metadata": value["correction_metadata_used"],
        "known_context_gaps": ["correction_retraction_coverage:unknown"],
    }
    schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v1.schema.json").read_text()
    )

    invalid = validate_response(
        value, segments, expected=expected, response_schema=schema
    )
    assert invalid["valid"] is False
    assert set(invalid["errors"]) == {
        f"evidence_unmatched_or_ambiguous:{criterion}"
        for criterion in CRITERIA
        if criterion != "correction_retraction_coverage"
    }

    exact = text.rstrip("\n")
    valid = validate_response(
        response_value("request", hashes, exact),
        segments,
        expected=expected,
        response_schema=schema,
    )
    assert valid["valid"] is True


def test_repeated_footer_evidence_requires_a_unique_extension() -> None:
    text = (ROOT / "fixtures" / "eligibility-repeated-footer.txt").read_text()
    segments = _segments(text)
    hashes = {
        "policy_sha256": "a",
        "source_version_sha256": "b",
        "extracted_text_sha256": "c",
        "metadata_sha256": "d",
    }
    footer = "Scientific Reports | 5:13760 | DOI: 10.1038/srep13760"
    repeated = response_value("request", hashes, footer)
    expected = {
        "request_id": "request",
        "input_echo": hashes,
        "correction_metadata": repeated["correction_metadata_used"],
        "known_context_gaps": ["correction_retraction_coverage:unknown"],
    }
    schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v1.schema.json").read_text()
    )

    invalid = validate_response(
        repeated, segments, expected=expected, response_schema=schema
    )
    assert invalid["valid"] is False

    unique = f"First page context.\n{footer}"
    valid = validate_response(
        response_value("request", hashes, unique),
        segments,
        expected=expected,
        response_schema=schema,
    )
    assert valid["valid"] is True


def test_phase_budget_is_shared_across_run_directories(tmp_path: Path) -> None:
    config = _config(ROOT / "config" / "gemini-eligibility-v1.json")
    policy = safety(True)
    ledger = tmp_path / "project.json"
    first = tmp_path / "first"
    second = tmp_path / "second"
    init_budget(first, config, Decimal("1"), ledger, policy)
    init_budget(second, config, Decimal("1"), ledger, policy)
    reserve_budget(first, Decimal("0.25"), ledger, policy)
    reserve_budget(first, Decimal("0.25"), ledger, policy)
    reserve_budget(second, Decimal("0.25"), ledger, policy)
    reserve_budget(second, Decimal("0.25"), ledger, policy)
    with pytest.raises(ValueError, match="project cap or run allocation"):
        reserve_budget(second, Decimal("0.01"), ledger, policy)
    with pytest.raises(ValueError, match="positive"):
        reserve_budget(second, Decimal("-1"), ledger, policy)


def test_submission_count_is_shared_and_atomic(tmp_path: Path) -> None:
    config = _config(ROOT / "config" / "gemini-eligibility-v1.json")
    policy = safety(True)
    policy["minimum_seconds_between_generation_submissions"] = 0
    ledger = tmp_path / "project.json"
    run = tmp_path / "run"
    init_budget(run, config, Decimal("1"), ledger, policy)
    for number in range(3):
        key = f"job-{number}"
        _authorize_submission(
            run,
            key,
            Decimal("0.01"),
            policy,
            ledger,
            {"job_key": key, "candidate_key": key},
        )
        _settle_submission(run, key, Decimal("0"), ledger)
    with pytest.raises(ValueError, match="submission limit"):
        _authorize_submission(
            run,
            "job-4",
            Decimal("0.01"),
            policy,
            ledger,
            {"job_key": "job-4", "candidate_key": "job-4"},
        )


def test_doctor_does_not_read_private_key_when_policy_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = fixture(tmp_path, live=False)
    paths["credential"].parent.mkdir(mode=0o700)
    paths["credential"].write_text("private fixture value", encoding="utf-8")
    paths["credential"].chmod(0o600)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with patch.object(Path, "read_text", side_effect=AssertionError("key read")):
        result = call(paths, "doctor")
    assert result["state"] == "disabled_by_policy"
    assert result["credential_source"] == "private_file"
    assert result["live_call_made"] is False


def test_completed_job_updates_durable_progress(tmp_path: Path) -> None:
    paths = fixture(tmp_path)
    transport = GoodTransport()
    result = call(paths, "run", transport)
    assert transport.methods == ["countTokens", "generateContent"]
    assert result["state"] == "completed"
    assert result["counts"]["queued"] == 0
    assert result["counts"]["completed"] == 1
    assert result["counts"]["eligible"] == 1
    overlay = [
        json.loads(line)
        for line in (paths["run"] / "gemini-overlay.ndjson").read_text().splitlines()
    ]
    assert overlay[0]["gemini_status"] == "eligible"


def test_envelope_error_is_billed_and_not_completed(tmp_path: Path) -> None:
    paths = fixture(tmp_path)

    class BadEnvelope(GoodTransport):
        def post(self, model: str, method: str, payload: dict) -> dict:
            if method == "countTokens":
                return super().post(model, method, payload)
            self.methods.append(method)
            return {
                "candidates": [
                    {"finishReason": "MAX_TOKENS"},
                    {"finishReason": "STOP"},
                ],
                "usageMetadata": {
                    "promptTokenCount": 120,
                    "candidatesTokenCount": 40,
                    "thoughtsTokenCount": 10,
                    "totalTokenCount": 170,
                },
            }

    result = call(paths, "run", BadEnvelope())
    job = json.loads(next((paths["run"] / "jobs").glob("*.json")).read_text())
    assert job["state"] == "screening_error"
    assert result["counts"]["completed"] == 0
    assert result["counts"]["screening_error"] == 1
    assert Decimal(result["budget"]["spent_usd"]) > 0


def test_count_error_and_exact_too_large_are_durable(tmp_path: Path) -> None:
    paths = fixture(tmp_path / "error")

    class CountError:
        def __init__(self) -> None:
            self.methods = []

        def post(self, model: str, method: str, payload: dict) -> dict:
            self.methods.append(method)
            raise RuntimeError("controlled count error")

    failed = CountError()
    result = call(paths, "run", failed)
    assert failed.methods == ["countTokens"]
    assert result["counts"]["screening_error"] == 1
    assert len(list((paths["run"] / "errors").glob("*.json"))) == 1

    large_paths = fixture(tmp_path / "large")

    class TooLarge:
        def __init__(self) -> None:
            self.methods = []

        def post(self, model: str, method: str, payload: dict) -> dict:
            self.methods.append(method)
            return {"totalTokens": 1_048_577}

    large = TooLarge()
    result = call(large_paths, "run", large)
    assert large.methods == ["countTokens"]
    assert result["counts"]["too_large_not_ready"] == 1
    assert len(list((large_paths["run"] / "too-large").glob("*.json"))) == 1


def test_ambiguous_generation_is_never_reposted(tmp_path: Path) -> None:
    paths = fixture(tmp_path)

    class Ambiguous(GoodTransport):
        def post(self, model: str, method: str, payload: dict) -> dict:
            if method == "countTokens":
                return super().post(model, method, payload)
            self.methods.append(method)
            raise TimeoutError("controlled post-send timeout")

    first = Ambiguous()
    result = call(paths, "run", first)
    assert first.methods == ["countTokens", "generateContent"]
    assert result["counts"]["ambiguous_charge"] == 1

    second = GoodTransport()
    resumed = call(paths, "resume", second)
    assert second.methods == []
    assert resumed["state"] == "paused"
    assert resumed["counts"]["ambiguous_charge"] == 1


def test_process_crash_after_authorization_recovers_without_repost(
    tmp_path: Path,
) -> None:
    paths = fixture(tmp_path)

    class ControlledCrash(BaseException):
        pass

    class CrashTransport(GoodTransport):
        def post(self, model: str, method: str, payload: dict) -> dict:
            if method == "countTokens":
                return super().post(model, method, payload)
            self.methods.append(method)
            raise ControlledCrash()

    with pytest.raises(ControlledCrash):
        call(paths, "run", CrashTransport())
    second = GoodTransport()
    resumed = call(paths, "resume", second)
    assert second.methods == []
    assert resumed["counts"]["ambiguous_charge"] == 1


def test_known_http_generation_error_is_not_ambiguous(tmp_path: Path) -> None:
    paths = fixture(tmp_path)

    class KnownHTTP(GoodTransport):
        def post(self, model: str, method: str, payload: dict) -> dict:
            if method == "countTokens":
                return super().post(model, method, payload)
            self.methods.append(method)
            raise urllib.error.HTTPError(
                "https://example.invalid", 429, "rate", {"Retry-After": "60"}, None
            )

    result = call(paths, "run", KnownHTTP())
    assert result["counts"]["screening_error"] == 1
    assert result["counts"]["ambiguous_charge"] == 0
    error = json.loads(next((paths["run"] / "errors").glob("*.json")).read_text())
    assert error["http_status"] == 429
    assert error["retry_after"] == "60"


def test_invalid_config_fails_closed(tmp_path: Path) -> None:
    value = json.loads((ROOT / "config" / "gemini-eligibility-v1.json").read_text())
    value["model"] = "gemini-does-not-exist"
    path = tmp_path / "config.json"
    write_json(path, value)
    with pytest.raises(ValueError, match="no verified price record"):
        _config(path)


def test_config_requires_low_thinking_for_bounded_structured_output(
    tmp_path: Path,
) -> None:
    config_path = ROOT / "config" / "gemini-eligibility-v1.json"
    value = json.loads(config_path.read_text())

    config = _config(config_path)
    assert config["thinking_level"] == "low"
    assert config["config_id"] == "arctic-gemini-eligibility-r1-config-v5"
    assert config["stage_models"]["answer_agreement"]["maximum_output_tokens"] == 128
    assert value["maximum_output_tokens"] == 8192

    legacy = json.loads(config_path.read_text())
    legacy["config_id"] = "arctic-gemini-eligibility-r1-config-v4"
    legacy["stage_models"]["answer_agreement"]["maximum_output_tokens"] = 4
    legacy_path = tmp_path / "legacy-four-token-answer-judge.json"
    write_json(legacy_path, legacy)
    assert _config(legacy_path)["config_id"] == (
        "arctic-gemini-eligibility-r1-config-v4"
    )

    value["stage_models"]["answer_agreement"]["maximum_output_tokens"] = 4
    changed_path = tmp_path / "four-token-answer-judge.json"
    write_json(changed_path, value)
    with pytest.raises(
        ValueError, match="answer agreement model configuration changed"
    ):
        _config(changed_path)

    value["stage_models"]["answer_agreement"]["maximum_output_tokens"] = 128
    value["thinking_level"] = "medium"
    changed_path = tmp_path / "medium-thinking.json"
    write_json(changed_path, value)
    with pytest.raises(ValueError, match="thinking level must be low"):
        _config(changed_path)

    value["thinking_level"] = "low"
    value["config_id"] = "arctic-gemini-eligibility-r1-config-v1"
    changed_path = tmp_path / "old-config-revision.json"
    write_json(changed_path, value)
    with pytest.raises(ValueError, match="config revision is not approved"):
        _config(changed_path)
