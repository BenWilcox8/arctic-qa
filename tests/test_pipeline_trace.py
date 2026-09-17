from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from arctic_qa.db import Database, now
from arctic_qa.pipeline_trace import (
    LIST_SCHEMA,
    PAPER_SCHEMA,
    STAGE_SCHEMA,
    PipelineTraceStore,
    _plain_reason,
    record_model_request_trace,
)
from arctic_qa.util import canonical_json, sha256_bytes, stable_id


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
            (
                source_id,
                family_id,
                canonical_json(candidate),
                "2026-09-13T00:00:06Z",
                "2026-09-13T00:00:06Z",
            ),
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


def set_active_invocation(
    namespace: Path, invocation_run_id: str, recent_papers: list[dict[str, object]]
) -> None:
    progress_path = namespace / "streaming-dataset-r1" / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["invocation_run_id"] = invocation_run_id
    progress["recent_papers"] = recent_papers
    write_json(progress_path, progress)


def test_two_threads_that_miss_together_build_once(tmp_path: Path) -> None:
    """A cache that costs the whole history to fill is filled by one thread.

    The viewer's background refresher and a paper-detail request missed
    together on 2026-09-17 and each built the per-family records in full,
    which took the pair past 90 seconds where one build takes 25.
    """
    namespace, _, _ = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    jobs = store._all_eligibility_jobs()
    store._records_version = None
    store._records_cache = None
    builds = 0
    original = store._build_paper_records
    # The version is frozen, so only the lock can hold the build to one. An
    # input that moves while the threads run is a real second build and would
    # otherwise make this test say the lock failed when it held.
    store._path_version = staticmethod(lambda path: ("frozen",))  # type: ignore[assignment]
    store._receipt_events = lambda: []  # type: ignore[method-assign]

    def counted(eligibility_jobs: object) -> object:
        nonlocal builds
        builds += 1
        # Long enough that every other thread is certainly waiting by now.
        time.sleep(0.3)
        return original(eligibility_jobs)

    store._build_paper_records = counted  # type: ignore[method-assign]
    threads = [
        threading.Thread(target=lambda: store._paper_records(jobs)) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert builds == 1
    assert store._records_cache is not None


def test_a_new_receipt_rereads_itself_and_not_the_whole_directory(
    tmp_path: Path,
) -> None:
    """One receipt arriving must not cost the reader every other receipt.

    A receipt is immutable once written, and the shared ledger has two live
    writers, so one lands every few seconds. Re-deriving all of them each time
    cost 77 seconds against the 48,938 receipts of 2026-09-17 and put that on
    every request of the viewer.
    """
    namespace, _, _ = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    first = store._receipt_events()
    assert first
    reads: list[Path] = []
    original = store._read_json
    store._read_json = lambda path: (  # type: ignore[method-assign]
        reads.append(Path(path)),
        original(path),
    )[1]

    # Nothing moved: the listing alone answers, and nothing is read again.
    assert store._receipt_events() == first
    assert reads == []

    arrival = store.receipts_dir / f"{'b' * 64}.json"
    write_json(
        arrival,
        {
            "request_key": "b" * 64,
            "run_id": "run-new",
            "family_id": "family-fixture",
            "stage": "question_generation",
        },
    )
    after = store._receipt_events()

    assert len(after) == len(first) + 1
    # Only the receipt that arrived was opened.
    assert reads == [arrival]


def test_a_removed_receipt_leaves_the_derived_events(tmp_path: Path) -> None:
    """A receipt that leaves the directory leaves the reader's map with it."""
    namespace, _, _ = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    arrival = store.receipts_dir / f"{'c' * 64}.json"
    write_json(
        arrival,
        {
            "request_key": "c" * 64,
            "run_id": "run-new",
            "family_id": "family-fixture",
            "stage": "question_generation",
        },
    )
    with_arrival = store._receipt_events()
    assert any(row["request_key"] == "c" * 64 for row in with_arrival)

    arrival.unlink()
    without = store._receipt_events()

    assert not any(row["request_key"] == "c" * 64 for row in without)
    assert "c" * 64 not in store._receipt_derived


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
    assert searched["items"][0]["state_entered_at_utc"] == ("2026-09-13T00:00:06Z")
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


def test_trace_counts_calls_candidates_and_findings_separately(
    tmp_path: Path,
) -> None:
    namespace, old_key, new_key = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    paper = store.list_papers(query="10.1234/fixture")["items"][0]

    assert paper["attempt_count"] == 2
    assert paper["model_call_count"] == 2
    assert paper["qa_candidate_count"] == 1
    assert paper["finding_attempt_count"] == 1

    detail = store.paper_detail(paper["paper_key"])
    assert [stage["request_key"] for stage in detail["stages"]] == [
        old_key,
        new_key,
    ]
    assert [stage["transport_attempt"] for stage in detail["stages"]] == [1, 1]
    assert (
        store.stage_payload(paper["paper_key"], new_key)["receipt"]["request_key"]
        == new_key
    )

    database = Database(namespace / "state.sqlite3")
    with database.transaction():
        database.connection.execute(
            """INSERT INTO findings
            (finding_id,run_id,source_id,paper_family_id,chunk_id,
             selection_policy_version,answer_json,status,created_at)
            VALUES ('finding-history','history-run',?,?,?,
                    'finding-policy-history',?,'frozen',?)""",
            (
                "src-fixture",
                "family-fixture",
                "chunk-fixture",
                canonical_json({"text": "The result increased earlier."}),
                "2026-09-13T00:00:07Z",
            ),
        )
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES ('aqa-history','history-run',?,?,'answer_first',?,
                    'rejected',?,?)""",
            (
                "src-fixture",
                "family-fixture",
                canonical_json(
                    {"item_id": "aqa-history", "question": "What changed earlier?"}
                ),
                "2026-09-13T00:00:08Z",
                "2026-09-13T00:00:08Z",
            ),
        )
    database.close()

    refreshed = PipelineTraceStore(namespace)
    paper = refreshed.list_papers(query="10.1234/fixture")["items"][0]
    assert paper["model_call_count"] == 2
    assert paper["qa_candidate_count"] == 2
    assert paper["finding_attempt_count"] == 2
    runs = {
        run["run_id"]: run for run in refreshed.paper_detail(paper["paper_key"])["runs"]
    }
    assert runs["campaign-fixture"]["model_call_count"] == 2
    assert runs["campaign-fixture"]["qa_candidate_count"] == 1
    assert runs["history-run"]["model_call_count"] == 0
    assert runs["history-run"]["qa_candidate_count"] == 1
    assert runs["history-run"]["finding_attempt_count"] == 1


def test_paper_detail_scans_eligibility_jobs_once(tmp_path: Path) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    paper_key = store.list_papers(query="10.1234/fixture")["items"][0]["paper_key"]
    original_job_paths = store._job_paths
    scan_count = 0

    def counting_job_paths() -> list[Path]:
        nonlocal scan_count
        scan_count += 1
        return original_job_paths()

    store._job_paths = counting_job_paths  # type: ignore[method-assign]

    detail = store.paper_detail(paper_key)

    assert detail["eligibility"][0]["validation"]["decision"] == "eligible"
    assert scan_count == 1


def test_paper_detail_refreshes_changed_eligibility_job(tmp_path: Path) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    paper_key = store.list_papers(query="10.1234/fixture")["items"][0]["paper_key"]
    job_path = (
        namespace
        / "gemini-eligibility-r1"
        / "run-fixture"
        / "jobs"
        / f"{'3' * 64}.json"
    )

    assert (
        store.paper_detail(paper_key)["eligibility"][0]["validation"]["decision"]
        == "eligible"
    )
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job["validation"]["decision"] = "excluded"
    write_json(job_path, job)

    assert (
        store.paper_detail(paper_key)["eligibility"][0]["validation"]["decision"]
        == "excluded"
    )


def test_candidate_reasons_bind_to_current_payload_and_keep_exits_separate(
    tmp_path: Path,
) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    database = Database(namespace / "state.sqlite3")
    stored = database.one(
        "SELECT candidate_json FROM candidates WHERE item_id='aqa-fixture'"
    )
    assert stored is not None
    candidate = json.loads(stored["candidate_json"])
    current_hash = stable_id("candidate-payload", canonical_json(candidate))
    with database.transaction():
        database.connection.execute(
            "UPDATE candidates SET status='rejected' WHERE item_id='aqa-fixture'"
        )
        database.connection.execute(
            """INSERT INTO validation_events
            (event_id,item_id,stage,label,reason_codes_json,details_json,created_at)
            VALUES ('validation-old','aqa-fixture','automated_acceptance','rejected',?,?,?)""",
            (
                canonical_json(["stale_rejection"]),
                canonical_json({"candidate_hash": "old-payload-hash"}),
                now(),
            ),
        )
        database.connection.execute(
            """INSERT INTO validation_events
            (event_id,item_id,stage,label,reason_codes_json,details_json,created_at)
            VALUES ('validation-current','aqa-fixture','automated_acceptance','rejected',?,?,?)""",
            (
                canonical_json(["answer_verifier_scope_not_source_bound"]),
                canonical_json(
                    {
                        "candidate_hash": current_hash,
                        "distractors": [
                            {
                                "text": "It stayed unchanged.",
                                "type": "contradiction",
                                "accepted": False,
                                "reasons": ["distractor_not_false"],
                            }
                        ],
                    }
                ),
                now(),
            ),
        )
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES ('rejection-current','aqa-fixture','src-fixture',
                    'automated_acceptance','answer_verifier_scope_not_source_bound',?,?)""",
            (canonical_json({"recorded": "current payload"}), now()),
        )
    database.close()

    store = PipelineTraceStore(namespace)
    paper_key = store.list_papers(query="10.1234/fixture")["items"][0]["paper_key"]
    detail = store.paper_detail(paper_key)
    wrapper = detail["candidates"][0]

    assert wrapper["rejection_reason_status"] == "recorded"
    assert [row["reason_code"] for row in wrapper["rejection_reasons"]] == [
        "answer_verifier_scope_not_source_bound"
    ]
    assert wrapper["rejection_reasons"][0]["stage"] == "automated_acceptance"
    assert (
        "scope was not fully bound"
        in wrapper["rejection_reasons"][0]["plain_reason"]["summary"]
    )
    assert wrapper["distractor_rejections"][0]["reason_codes"] == [
        "distractor_not_false"
    ]
    assert any(
        row["reason_code"] == "fixture_rejection" for row in detail["rejections"]
    )

    database = Database(namespace / "state.sqlite3")
    with database.transaction():
        database.connection.execute(
            """UPDATE candidates SET status='machine_accepted_unverified'
            WHERE item_id='aqa-fixture'"""
        )
    database.close()
    accepted = PipelineTraceStore(namespace).paper_detail(paper_key)["candidates"][0]
    assert accepted["rejection_reason_status"] == "not_rejected"
    assert accepted["rejection_reasons"] == []


def test_rejected_candidate_without_a_current_payload_reason_is_unrecorded(
    tmp_path: Path,
) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    candidates = [
        {
            "item_id": "missing-reason",
            "status": "rejected",
            "candidate": {"item_id": "missing-reason", "question": "What changed?"},
        }
    ]

    store._project_candidate_reasons(candidates, [], [])

    assert candidates[0]["rejection_reason_status"] == "unrecorded"
    assert candidates[0]["rejection_reasons"] == []


def test_progress_reason_overlays_list_and_builds_plain_evidence(
    tmp_path: Path,
) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    progress_path = namespace / "streaming-dataset-r1" / "progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    progress["invocation_run_id"] = "campaign-fixture"
    progress["recent_papers"] = [
        {
            "paper_id": "src-fixture",
            "title": "Arctic fixture paper",
            "current_stage": "completed",
            "final_state": "generation_rejected",
            "final_reason": "answer_verifier_scope_not_source_bound",
        }
    ]
    write_json(progress_path, progress)
    database = Database(namespace / "state.sqlite3")
    with database.transaction():
        database.connection.execute(
            "UPDATE candidates SET status='rejected' WHERE item_id='aqa-fixture'"
        )
    database.close()
    store = PipelineTraceStore(namespace)

    item = store.list_papers(run_id="campaign-fixture", query="Arctic fixture")[
        "items"
    ][0]
    assert item["state"] == "generation_rejected"
    assert item["reason"]["summary"].startswith("The paper stayed eligible")
    assert item["final_reason"] == "answer_verifier_scope_not_source_bound"

    detail = store.paper_detail(item["paper_key"])
    assert detail["plain_reason"]["failed_stage"] == "automated_acceptance"
    assert detail["plain_reason"]["reason_codes"] == [
        "answer_verifier_scope_not_source_bound",
        "fixture_rejection",
    ]
    assert detail["plain_reason"]["evidence"][0]["quote"].startswith(
        "The retained Arctic result"
    )
    assert detail["plain_reason"]["comparisons"][0] == {
        "label": "Proposed answer compared with independent reconstruction",
        "proposed_answer": "It increased.",
        "reconstructed_answer": "It increased.",
    }


@pytest.mark.parametrize(
    ("reason", "state", "category", "stage"),
    [
        (
            "criterion_failed:published_primary_findings",
            "eligibility_rejected",
            "eligibility_exclusion",
            "scientific_eligibility",
        ),
        (
            "criterion_evidence_missing:study_geography",
            "eligibility_unresolved",
            "eligibility_unresolved",
            "scientific_eligibility",
        ),
        (
            "evidence_span_unknown:study_geography",
            "eligibility_unresolved",
            "eligibility_unresolved",
            "scientific_eligibility",
        ),
        (
            "provider_type_missing",
            "unresolved",
            "provider_output_unresolved",
            "retrieval",
        ),
        (
            "reconstruction_disagreement",
            "generation_rejected",
            "qa_rejection",
            "automated_acceptance",
        ),
        (
            "reconstructor_response_invalid",
            "generation_rejected",
            "invalid_model_response",
            "blinded_reconstruction",
        ),
        ("ValueError", "error", "processing_error", "generation"),
        (
            "source_unavailable",
            "unresolved",
            "source_or_access_problem",
            "retrieval",
        ),
        (
            "distractor_not_false",
            "generation_rejected",
            "distractor_rejection",
            "option_verification",
        ),
        (
            "ambiguous_charge",
            "ambiguous_charge",
            "infrastructure_or_accounting_stop",
            "generation",
        ),
    ],
)
def test_plain_reason_distinguishes_exit_categories(
    reason: str, state: str, category: str, stage: str
) -> None:
    result = _plain_reason(reason, state, stage)

    assert result is not None
    assert result["category"] == category
    assert result["failed_stage"] == stage
    assert result["reason_code"] == reason


def test_plain_eligibility_statement_does_not_require_decoding_reason_code(
    tmp_path: Path,
) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    store = PipelineTraceStore(namespace)
    detail = store._plain_reason_detail(
        {
            "reason": _plain_reason(
                "criterion_failed:study_geography",
                "eligibility_rejected",
                "completed",
            ),
            "state": "eligibility_rejected",
            "receipts": [],
        },
        [
            {
                "parsed_response": {
                    "criteria": [
                        {
                            "criterion_id": "study_geography",
                            "status": "failed",
                            "reason_codes": ["marine_mixed_setting"],
                        }
                    ]
                },
                "validation": {
                    "overall_reason_codes": ["criterion_failed:study_geography"],
                    "resolved_evidence": [
                        {
                            "criterion": "study_geography",
                            "spans": [
                                {
                                    "span_id": "span-geo-1",
                                    "quote": "  Arctic study area\n  continued here.",
                                    "start_byte": 120,
                                    "end_byte": 158,
                                    "locator": {
                                        "section_id": "methods",
                                        "source_block_id": "block-1",
                                    },
                                }
                            ],
                        }
                    ],
                },
            }
        ],
        [],
        [],
        [],
    )

    assert detail is not None
    assert "marine mixed setting" in detail["model_statements"][0]["text"]
    assert "marine_mixed_setting" not in detail["model_statements"][0]["text"]
    assert "marine_mixed_setting" in detail["reason_codes"]
    assert detail["evidence"] == [
        {
            "quote": "  Arctic study area\n  continued here.",
            "locator": {
                "section_id": "methods",
                "source_block_id": "block-1",
            },
            "span_id": "span-geo-1",
            "start_byte": 120,
            "end_byte": 158,
        }
    ]


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


def test_new_submitted_run_does_not_inherit_historical_acceptance(
    tmp_path: Path,
) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    request_key = "b" * 64
    write_json(
        namespace
        / "streaming-dataset-r1"
        / "model-receipts"
        / f"{request_key}.submitted.json",
        {
            "request_key": request_key,
            "request_sha256": "c" * 64,
            "run_id": "future-run",
            "stage": "finding_answer_extraction",
            "paper_id": "10.1234/fixture",
            "family_id": "family-fixture",
            "source_version_id": "a" * 64,
            "model": "gemini-fixture",
            "state": "submitted",
            "submitted_at_utc": "2026-09-13T00:00:10Z",
            "reserved_usd": "0.01",
        },
    )
    store = PipelineTraceStore(namespace)

    aggregate = store.list_papers(query="10.1234/fixture")["items"][0]
    historical = store.list_papers(run_id="campaign-fixture")["items"][0]
    current = store.list_papers(run_id="future-run")["items"][0]

    assert aggregate["state"] == "in_progress"
    assert aggregate["current_stage"] == "finding_answer_extraction"
    assert historical["state"] == "machine_accepted_unverified"
    assert current["state"] == "in_progress"
    assert current["run_ids"] == ["future-run"]
    assert current["current_stage"] == "finding_answer_extraction"
    assert current["attempt_count"] == 1
    assert current["state_entered_at_utc"] == "2026-09-13T00:00:10Z"
    assert (
        store.list_papers(run_id="future-run", state="machine_accepted_unverified")[
            "items"
        ]
        == []
    )

    detail = store.paper_detail(aggregate["paper_key"])
    runs = {run["run_id"]: run for run in detail["runs"]}
    assert runs["campaign-fixture"]["state"] == "machine_accepted_unverified"
    assert runs["campaign-fixture"]["candidate_item_ids"] == ["aqa-fixture"]
    assert runs["future-run"]["state"] == "in_progress"
    assert runs["future-run"]["stages"] == [request_key]
    assert {stage["run_id"] for stage in detail["stages"]} == {
        "campaign-fixture",
        "future-run",
    }


def test_latest_invocation_hides_historical_acceptance_and_keeps_current_evidence(
    tmp_path: Path,
) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    candidate = {
        "item_id": "aqa-latest-rejected",
        "finding_id": "finding-fixture",
        "question": "What happened in the latest attempt?",
        "answer": {"text": "It was rejected."},
        "source": {"source_id": "src-fixture"},
    }
    candidate_hash = stable_id("candidate-payload", canonical_json(candidate))
    database = Database(namespace / "state.sqlite3")
    with database.transaction():
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES ('aqa-latest-rejected','latest-rejected-run',?,?,'answer_first',?,
                    'rejected',?,?)""",
            (
                "src-fixture",
                "family-fixture",
                canonical_json(candidate),
                "2026-09-14T00:00:08Z",
                "2026-09-14T00:00:08Z",
            ),
        )
        database.connection.execute(
            """INSERT INTO validation_events
            (event_id,item_id,stage,label,reason_codes_json,details_json,created_at)
            VALUES ('validation-latest','aqa-latest-rejected','automated_acceptance',
                    'rejected',?,?,?)""",
            (
                canonical_json(["latest_attempt_rejected"]),
                canonical_json({"candidate_hash": candidate_hash}),
                "2026-09-14T00:00:08Z",
            ),
        )
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES ('rejection-latest','aqa-latest-rejected',?,
                    'automated_acceptance','latest_attempt_rejected',?,?)""",
            (
                "src-fixture",
                canonical_json({"attempt": "latest"}),
                "2026-09-14T00:00:08Z",
            ),
        )
    database.close()
    set_active_invocation(
        namespace,
        "latest-rejected-run",
        [
            {
                "paper_id": "src-fixture",
                "title": "Arctic fixture paper",
                "current_stage": "completed",
                "final_state": "generation_rejected",
                "final_reason": "latest_attempt_rejected",
            }
        ],
    )

    store = PipelineTraceStore(namespace)
    items = store.list_papers()["items"]

    assert len(items) == 1
    assert items[0]["state"] == "generation_rejected"
    assert items[0]["run_ids"] == ["latest-rejected-run"]
    assert store.latest_run_counts() == {
        "accepted_qa": 0,
        "generation_rejected": 1,
        "incomplete_non_mcq": 0,
        "in_progress": 0,
        "eligibility_rejected": 0,
        "eligibility_unresolved": 0,
        "eligible": 0,
    }

    detail = store.paper_detail(items[0]["paper_key"])
    assert [row["item_id"] for row in detail["candidates"]] == ["aqa-latest-rejected"]
    assert [row["finding_id"] for row in detail["findings"]] == ["finding-fixture"]
    assert detail["candidates"][0]["rejection_reasons"][0]["reason_code"] == (
        "latest_attempt_rejected"
    )


def test_same_invocation_acceptance_wins_over_later_rejection(tmp_path: Path) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    accepted = {
        "item_id": "aqa-same-accepted",
        "finding_id": "finding-fixture",
        "question": "What happened?",
        "answer": {"text": "It increased."},
        "source": {"source_id": "src-fixture"},
    }
    rejected = {
        "item_id": "aqa-same-rejected",
        "finding_id": "finding-fixture",
        "question": "What happened in the revision?",
        "answer": {"text": "It decreased."},
        "source": {"source_id": "src-fixture"},
    }
    database = Database(namespace / "state.sqlite3")
    with database.transaction():
        for item_id, candidate, status in (
            ("aqa-same-accepted", accepted, "machine_accepted_unverified"),
            ("aqa-same-rejected", rejected, "rejected"),
        ):
            database.connection.execute(
                """INSERT INTO candidates
                (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
                 status,created_at,updated_at)
                    VALUES (?, 'same-run', ?, ?, 'answer_first', ?, ?, ?, ?)""",
                (
                    item_id,
                    "src-fixture",
                    "family-fixture",
                    canonical_json(candidate),
                    status,
                    "2026-09-14T00:00:08Z"
                    if status == "machine_accepted_unverified"
                    else "2026-09-14T00:00:09Z",
                    "2026-09-14T00:00:08Z"
                    if status == "machine_accepted_unverified"
                    else "2026-09-14T00:00:09Z",
                ),
            )
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES ('rejection-same','aqa-same-rejected',?,
                    'automated_acceptance','revision_rejected',?,?)""",
            (
                "src-fixture",
                canonical_json({"attempt": "revision"}),
                "2026-09-14T00:00:09Z",
            ),
        )
    database.close()
    set_active_invocation(
        namespace,
        "same-run",
        [
            {
                "paper_id": "src-fixture",
                "title": "Arctic fixture paper",
                "current_stage": "completed",
                "final_state": "generation_rejected",
                "final_reason": "revision_rejected",
            }
        ],
    )

    store = PipelineTraceStore(namespace)
    item = store.list_papers()["items"][0]

    assert item["state"] == "machine_accepted_unverified"
    detail = store.paper_detail(item["paper_key"])
    assert {row["item_id"] for row in detail["candidates"]} == {
        "aqa-same-accepted",
        "aqa-same-rejected",
    }
    assert detail["rejections"][0]["reason_code"] == "revision_rejected"


def test_latest_scope_links_campaign_candidates_through_receipts(
    tmp_path: Path,
) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    receipts = namespace / "streaming-dataset-r1" / "model-receipts"
    accepted_key = "8" * 64
    rejected_key = "9" * 64
    for request_key, response_id, completed in (
        (accepted_key, "latest-accepted-response", "2026-09-14T00:00:08Z"),
        (rejected_key, "latest-rejected-response", "2026-09-14T00:00:09Z"),
    ):
        write_json(
            receipts / f"{request_key}.json",
            {
                "request_key": request_key,
                "run_id": "latest-real-shape-run",
                "stage": "option_verification",
                "paper_id": "10.1234/fixture",
                "family_id": "family-fixture",
                "state": "completed",
                "submitted_at_utc": completed,
                "completed_at_utc": completed,
                "response": {"responseId": response_id},
            },
        )
    database = Database(namespace / "state.sqlite3")
    with database.transaction():
        for item_id, status, response_id, updated_at in (
            (
                "aqa-real-accepted",
                "machine_accepted_unverified",
                "latest-accepted-response",
                "2026-09-14T00:00:08Z",
            ),
            (
                "aqa-real-rejected",
                "rejected",
                "latest-rejected-response",
                "2026-09-14T00:00:09Z",
            ),
        ):
            candidate = {
                "item_id": item_id,
                "finding_id": "finding-fixture",
                "question": item_id,
                "answer": {"text": "It increased."},
                "source": {"source_id": "src-fixture"},
                "provenance": {"verification_calls": [{"request_id": response_id}]},
            }
            database.connection.execute(
                """INSERT INTO candidates
                (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
                 status,created_at,updated_at)
                    VALUES (?, 'campaign-fixture', ?, ?, 'answer_first', ?, ?, ?, ?)""",
                (
                    item_id,
                    "src-fixture",
                    "family-fixture",
                    canonical_json(candidate),
                    status,
                    updated_at,
                    updated_at,
                ),
            )
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES ('rejection-real-shape','aqa-real-rejected',?,
                    'automated_acceptance','real_shape_revision_rejected',?,?)""",
            (
                "src-fixture",
                canonical_json({"attempt": "latest"}),
                "2026-09-14T00:00:09Z",
            ),
        )
    database.close()
    set_active_invocation(
        namespace,
        "latest-real-shape-run",
        [
            {
                "paper_id": "src-fixture",
                "title": "Arctic fixture paper",
                "current_stage": "completed",
                "final_state": "generation_rejected",
                "final_reason": "real_shape_revision_rejected",
            }
        ],
    )

    store = PipelineTraceStore(namespace)
    item = store.list_papers()["items"][0]
    detail = store.paper_detail(item["paper_key"])

    assert item["state"] == "machine_accepted_unverified"
    assert item["run_ids"] == ["latest-real-shape-run"]
    assert {row["item_id"] for row in detail["candidates"]} == {
        "aqa-real-accepted",
        "aqa-real-rejected",
    }
    assert [row["finding_id"] for row in detail["findings"]] == ["finding-fixture"]
    assert [stage["request_key"] for stage in detail["stages"]] == [
        accepted_key,
        rejected_key,
    ]
    assert detail["rejections"][0]["reason_code"] == ("real_shape_revision_rejected")


def test_empty_newest_invocation_does_not_fall_back_to_history(tmp_path: Path) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    set_active_invocation(namespace, "empty-newest-run", [])
    store = PipelineTraceStore(namespace)

    assert store.list_papers()["items"] == []
    with pytest.raises(KeyError, match="unknown pipeline paper key"):
        store.paper_detail(stable_id("pipeline-paper", "family-fixture"))


def test_progress_acceptance_without_final_candidate_is_not_accepted(
    tmp_path: Path,
) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    set_active_invocation(
        namespace,
        "accepted-label-without-candidate",
        [
            {
                "paper_id": "src-second",
                "title": "Second searchable paper",
                "current_stage": "completed",
                "final_state": "accepted",
            }
        ],
    )

    item = next(
        item
        for item in PipelineTraceStore(namespace).list_papers()["items"]
        if item["title"] == "Second searchable paper"
    )
    assert item["state"] != "machine_accepted_unverified"


@pytest.mark.parametrize(
    ("decision", "expected"),
    (("excluded", "eligibility_rejected"), ("uncertain", "eligibility_unresolved")),
)
def test_eligibility_only_state_uses_retained_decision(
    tmp_path: Path, decision: str, expected: str
) -> None:
    namespace, _, _ = fixture_namespace(tmp_path)
    request_key = "7" * 64
    response = {
        "responseId": "eligibility-only-response",
        "modelVersion": "gemini-fixture-v1",
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"parts": [{"text": canonical_json({"criteria": []})}]},
            }
        ],
    }
    write_json(
        namespace / "streaming-dataset-r1" / "model-receipts" / f"{request_key}.json",
        {
            "request_key": request_key,
            "request_sha256": "8" * 64,
            "run_id": "eligibility-only-run",
            "stage": "eligibility",
            "paper_id": "10.1234/second",
            "family_id": "family-second",
            "source_version_id": "9" * 64,
            "model": "gemini-fixture",
            "state": "completed",
            "submitted_at_utc": "2026-09-13T00:00:06Z",
            "completed_at_utc": "2026-09-13T00:00:07Z",
            "response": response,
        },
    )
    job_key = "a" * 64
    write_json(
        namespace
        / "gemini-eligibility-r1"
        / "run-eligibility-only"
        / "jobs"
        / f"{job_key}.json",
        {
            "schema": "gemini-eligibility-job-v1",
            "job_key": job_key,
            "candidate_key": "10.1234/second",
            "broker_request_key": request_key,
            "completed_at_utc": "2026-09-13T00:00:07Z",
            "validation": {
                "valid": True,
                "decision": decision,
                "overall_reason_codes": [f"fixture_{decision}"],
            },
        },
    )

    item = PipelineTraceStore(namespace).list_papers(query="10.1234/second")["items"][0]

    assert item["state"] == expected
