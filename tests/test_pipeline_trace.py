from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctic_qa.db import Database, now
from arctic_qa.pipeline_trace import (
    LIST_SCHEMA,
    PAPER_SCHEMA,
    STAGE_SCHEMA,
    PipelineTraceStore,
    record_model_request_trace,
)
from arctic_qa.util import canonical_json, sha256_bytes


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture_namespace(tmp_path: Path) -> tuple[Path, str, str]:
    namespace = tmp_path / "arctic-qa"
    namespace.mkdir()
    database = Database(namespace / "state.sqlite3")
    database.migrate(namespace / "backups")
    source_id = "src-fixture"
    family_id = "family-fixture"
    database.upsert_source(
        {
            "source_id": source_id,
            "stable_id": "10.1234/fixture",
            "doi": "10.1234/fixture",
            "title": "Arctic fixture paper",
            "authors": ["A. Scientist"],
            "published_date": "2026",
            "content_hash": "a" * 64,
            "paper_family_id": family_id,
            "eligibility_state": "eligible",
            "geography_state": "core_arctic",
            "geography_confidence": "model_reviewed_unverified",
            "provenance": {"adapter": "fixture"},
        }
    )
    database.upsert_source(
        {
            "source_id": "src-second",
            "stable_id": "10.1234/second",
            "doi": "10.1234/second",
            "title": "Second searchable paper",
            "authors": [],
            "paper_family_id": "family-second",
            "provenance": {"adapter": "fixture"},
        }
    )
    chunks = namespace / "chunks" / "aa" / "chunks.jsonl"
    chunks.parent.mkdir(parents=True)
    chunk = {
        "chunk_id": "chunk-fixture",
        "section_id": "results",
        "text": "The retained Arctic result increased after 1927.",
        "start_offset": 0,
        "end_offset": 48,
    }
    chunks.write_text(canonical_json(chunk) + "\n", encoding="utf-8")
    with database.transaction():
        database.connection.execute(
            """INSERT INTO artifacts
            (artifact_id,source_id,kind,content_hash,relative_path,media_type,created_at,metadata_json)
            VALUES ('artifact-chunks',?,'chunks',?,?, 'application/x-ndjson',?,?)""",
            (
                source_id,
                "b" * 64,
                str(chunks.relative_to(namespace)),
                now(),
                canonical_json({"private_path": "/private/source"}),
            ),
        )
        database.connection.execute(
            """INSERT INTO findings
            (finding_id,run_id,source_id,paper_family_id,chunk_id,
             selection_policy_version,answer_json,status,created_at)
            VALUES ('finding-fixture','campaign-fixture',?,?,?,
                    'finding-policy-fixture',?,'frozen',?)""",
            (
                source_id,
                family_id,
                chunk["chunk_id"],
                canonical_json(
                    {
                        "text": "The result increased.",
                        "evidence_quote": chunk["text"],
                        "locator": {
                            "chunk_id": chunk["chunk_id"],
                            "start_offset": 0,
                            "end_offset": 48,
                        },
                    }
                ),
                now(),
            ),
        )
        candidate = {
            "item_id": "aqa-fixture",
            "question": "What happened to the result?",
            "answer": {
                "text": "It increased.",
                "evidence_quote": chunk["text"],
                "locator": {
                    "chunk_id": chunk["chunk_id"],
                    "start_offset": 0,
                    "end_offset": 48,
                },
            },
            "reconstruction": {"answer": "It increased."},
            "distractors": [{"text": "It decreased."}],
            "option_verdicts": [{"contradiction_established": True}],
            "source": {"source_id": source_id},
        }
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES ('aqa-fixture','campaign-fixture',?,?,'answer_first',?,
                    'machine_accepted_unverified',?,?)""",
            (source_id, family_id, canonical_json(candidate), now(), now()),
        )
        database.connection.execute(
            """INSERT INTO validation_events
            (event_id,item_id,stage,label,reason_codes_json,details_json,created_at)
            VALUES ('validation-fixture','aqa-fixture','automated_acceptance',
                    'machine_accepted_unverified',?, ?, ?)""",
            (
                canonical_json(["model_only_distractor_verification"]),
                canonical_json({"labels": {"mcq_eligible": True}}),
                now(),
            ),
        )
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES ('rejection-fixture','aqa-fixture',?,'option_validation',
                    'fixture_rejection',?,?)""",
            (source_id, canonical_json({"option": "old option"}), now()),
        )
    receipts = namespace / "streaming-dataset-r1" / "model-receipts"
    old_key = "1" * 64
    new_key = "2" * 64
    for request_key, response_id, stage, completed in (
        (old_key, "response-old", "question_generation", "2026-09-13T00:00:02Z"),
        (new_key, "response-new", "option_verification", "2026-09-13T00:00:05Z"),
    ):
        response_payload = (
            {"question": "What happened to the result?"}
            if request_key == old_key
            else {"contradiction_established": True, "rationale": "Source conflict."}
        )
        response = {
            "responseId": response_id,
            "modelVersion": "gemini-fixture-v1",
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": canonical_json(response_payload),
                                "thoughtSignature": "must-not-leak",
                            }
                        ]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 3,
                "thoughtsTokenCount": 0,
                "totalTokenCount": 13,
            },
        }
        receipt = {
            "request_key": request_key,
            "request_sha256": "c" * 64,
            "run_id": "campaign-fixture",
            "stage": stage,
            "paper_id": "10.1234/fixture",
            "family_id": family_id,
            "source_version_id": "a" * 64,
            "model": "gemini-fixture",
            "state": "completed",
            "submitted_at_utc": "2026-09-13T00:00:00Z",
            "completed_at_utc": completed,
            "reserved_usd": "0.01",
            "actual_cost_usd": "0.001",
            "usage": response["usageMetadata"],
            "response": response,
        }
        write_json(receipts / f"{request_key}.json", receipt)
        with database.transaction():
            database.connection.execute(
                """INSERT INTO calls
                (call_id,run_id,entity_id,role,provider,requested_model,returned_model,
                 prompt_version,prompt_hash,parameters_json,request_id,attempt,status,
                 input_tokens,output_tokens,actual_cost_usd,started_at,completed_at,response_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1,'completed',10,3,'0.001',?,?,?)""",
                (
                    f"call-{request_key[:20]}",
                    "campaign-fixture",
                    f"entity-{request_key[:8]}",
                    "question_writer" if request_key == old_key else "option_verifier",
                    "gemini",
                    "gemini-fixture",
                    "gemini-fixture-v1",
                    "prompt-v1",
                    f"prompt-{request_key[:20]}",
                    canonical_json({"max_tokens": 20}),
                    response_id,
                    "2026-09-13T00:00:00Z",
                    completed,
                    canonical_json(response_payload),
                ),
            )
    request_payload = {
        "systemInstruction": {"parts": [{"text": "Return JSON."}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": "Full retained prompt and Arctic context."}],
            }
        ],
        "generationConfig": {"responseMimeType": "application/json"},
        "store": False,
    }
    assert record_model_request_trace(
        receipts,
        identity={
            "request_key": new_key,
            "request_sha256": sha256_bytes(canonical_json(request_payload).encode()),
            "run_id": "campaign-fixture",
            "stage": "option_verification",
            "paper_id": "10.1234/fixture",
            "family_id": family_id,
            "source_version_id": "a" * 64,
            "model": "gemini-fixture",
        },
        payload=request_payload,
        submitted_at_utc="2026-09-13T00:00:00Z",
    )
    eligibility = namespace / "gemini-eligibility-r1" / "run-fixture"
    job_key = "3" * 64
    write_json(
        eligibility / "jobs" / f"{job_key}.json",
        {
            "schema": "gemini-eligibility-job-v1",
            "job_key": job_key,
            "candidate_key": "10.1234/fixture",
            "broker_request_key": old_key,
            "span_manifest_path": "/private/must-not-leak.json",
            "completed_at_utc": "2026-09-13T00:00:02Z",
            "parsed_response": {"overall": "eligible"},
            "validation": {
                "decision": "eligible",
                "overall_reason_codes": ["all_required_criteria_satisfied"],
            },
        },
    )
    write_json(
        eligibility / "span-manifests" / f"{job_key}.json",
        {
            "schema": "eligibility-span-manifest-v2",
            "blocks": [{"span_id": "span-1", "quote": chunk["text"]}],
        },
    )
    write_json(
        namespace / "streaming-dataset-r1" / "progress.json",
        {
            "schema": "streaming-dataset-progress-v1",
            "state": "completed",
            "run_id": "campaign-fixture",
            "current_stage": "completed",
            "updated_at_utc": "2026-09-13T00:00:05Z",
            "recent_papers": [],
        },
    )
    write_json(
        namespace / "streaming-dataset-r1" / "shared-paid-call-ledger.json",
        {
            "halted": False,
            "inflight": 0,
            "updated_at_utc": "2026-09-13T00:00:05Z",
        },
    )
    export_dir = namespace / "exports" / "export-fixture"
    write_json(
        export_dir / "manifest.json",
        {"export_id": "export-fixture", "run_id": "campaign-fixture"},
    )
    export_dir.joinpath("short_answer.jsonl").write_text(
        canonical_json({"item_id": "aqa-fixture", "question": candidate["question"]})
        + "\n",
        encoding="utf-8",
    )
    for name in ("incomplete_short_answer", "mcq", "rejections"):
        export_dir.joinpath(f"{name}.jsonl").write_text("", encoding="utf-8")
    database.close()
    return namespace, old_key, new_key


def test_trace_list_filters_and_pages_without_exposing_paths(tmp_path: Path) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)

    first = store.list_papers(limit=1)

    assert first["schema"] == LIST_SCHEMA
    assert len(first["items"]) == 1
    assert first["next_cursor"]
    second = store.list_papers(limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["paper_key"] != first["items"][0]["paper_key"]
    searched = store.list_papers(
        query="Arctic fixture", state="machine_accepted_unverified"
    )
    assert searched["items"][0]["doi"] == "10.1234/fixture"
    assert store.list_papers(stage="option_verification")["items"] == searched["items"]
    with pytest.raises(ValueError, match="cursor"):
        store.list_papers(cursor="../../private")


def test_detail_exposes_retained_scientific_records_and_separate_attempts(
    tmp_path: Path,
) -> None:
    namespace, old_key, new_key = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    paper_key = store.list_papers(query="10.1234/fixture")["items"][0]["paper_key"]

    detail = store.paper_detail(paper_key)

    assert detail["schema"] == PAPER_SCHEMA
    assert detail["findings"][0]["answer"]["text"] == "The result increased."
    assert detail["candidates"][0]["candidate"]["reconstruction"]["answer"] == (
        "It increased."
    )
    assert detail["validation_events"][0]["label"] == ("machine_accepted_unverified")
    assert detail["rejections"][0]["reason_code"] == "fixture_rejection"
    assert detail["eligibility"][0]["span_manifest"]["blocks"][0]["span_id"] == (
        "span-1"
    )
    assert detail["exports"][0]["kind"] == "short_answer"
    assert [stage["stage_key"] for stage in detail["stages"]] == [old_key, new_key]
    encoded = json.dumps(detail)
    assert "/private/must-not-leak" not in encoded
    assert "thoughtSignature" not in encoded


def test_stage_payload_is_full_on_demand_and_historical_absence_is_honest(
    tmp_path: Path,
) -> None:
    namespace, old_key, new_key = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    paper_key = store.list_papers(query="10.1234/fixture")["items"][0]["paper_key"]

    old = store.stage_payload(paper_key, old_key)
    current = store.stage_payload(paper_key, new_key)

    assert old["schema"] == STAGE_SCHEMA
    assert old["request"] == {
        "availability": "not_retained",
        "reason": (
            "The historical request body was hash-bound but was not persisted verbatim."
        ),
        "payload": None,
    }
    assert current["request"]["availability"] == "retained"
    assert current["request"]["payload"]["contents"][0]["parts"][0]["text"] == (
        "Full retained prompt and Arctic context."
    )
    assert current["response"]["model_text"] == canonical_json(
        {"contradiction_established": True, "rationale": "Source conflict."}
    )
    assert current["response"]["parsed_response"]["contradiction_established"] is True
    assert current["source_context"][0]["chunks"][0]["text"].startswith(
        "The retained Arctic result"
    )
    assert "thoughtSignature" not in json.dumps(current)
    with pytest.raises(KeyError, match="stage"):
        store.stage_payload(paper_key, "../../private/secret")


def test_configured_inputs_cannot_escape_namespace(tmp_path: Path) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    with pytest.raises(ValueError, match="inside the namespace"):
        PipelineTraceStore(namespace, receipts_dir=tmp_path.parent / "elsewhere")


def test_submitted_only_stage_appears_on_refresh(tmp_path: Path) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    assert store.list_papers(query="10.1234/second")["items"][0]["state"] == (
        "not_started"
    )
    request_key = "4" * 64
    write_json(
        namespace
        / "streaming-dataset-r1"
        / "model-receipts"
        / f"{request_key}.submitted.json",
        {
            "request_key": request_key,
            "request_sha256": "5" * 64,
            "run_id": "active-run",
            "stage": "finding_answer_extraction",
            "paper_id": "10.1234/second",
            "family_id": "family-second",
            "source_version_id": "6" * 64,
            "model": "gemini-fixture",
            "state": "submitted",
            "submitted_at_utc": "2026-09-13T00:00:06Z",
            "reserved_usd": "0.01",
        },
    )

    item = store.list_papers(query="10.1234/second")["items"][0]

    assert item["state"] == "in_progress"
    assert item["current_stage"] == "finding_answer_extraction"
    stage = store.stage_payload(item["paper_key"], request_key)
    assert stage["stage"]["state"] == "submitted"
    assert stage["response"]["availability"] == "not_retained"
