from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arctic_qa import generation as generation_contract
from arctic_qa import streaming as streaming_module
from arctic_qa.db import Database, now
from arctic_qa.errors import BudgetError
from arctic_qa.streaming import _Progress, _progress_generation
from arctic_qa.util import canonical_json, stable_id


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate(tmp_path / "backups")
    return database


def _candidate(
    *, attempt: dict, item_id: str, source_id: str, family_id: str
) -> dict:
    return {
        "schema_version": generation_contract.CANDIDATE_SCHEMA_VERSION,
        "item_id": item_id,
        "finding_id": stable_id("finding", item_id),
        "finding_policy_version": attempt["finding_policy_version"],
        "source": {
            "source_id": source_id,
            "paper_family_id": family_id,
            "content_hash": "c" * 64,
            "chunk_id": "chunk-1",
            "section_id": "results",
        },
        "question": "What changed?",
        "answer": {"source_span_id": f"span-{item_id}"},
        "reconstruction": {},
        "answer_verification": {},
        "option_verdicts": [],
        "provenance": {
            "prompt_version": generation_contract.PROMPT_VERSION,
            "question_verification_contract_version": (
                generation_contract.QUESTION_VERIFICATION_CONTRACT_VERSION
            ),
            "numeric_rule_contract_version": (
                generation_contract.NUMERIC_RULE_CONTRACT_VERSION
            ),
            "direct_value_contract_version": (
                generation_contract.DIRECT_SOURCE_VALUE_CONTRACT_VERSION
            ),
            "scope_contract_version": generation_contract.SCOPE_CONTRACT_VERSION,
            "scope_role_semantics_version": (
                generation_contract.SCOPE_ROLE_SEMANTICS_VERSION
            ),
            "scope_role_binding_contract_version": (
                generation_contract.SCOPE_ROLE_BINDING_CONTRACT_VERSION
            ),
            "generation_attempt": attempt,
        },
    }


def _insert_candidate(
    database: Database, candidate: dict, *, run_id: str, family_id: str, status: str
) -> None:
    with database.transaction():
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?, ?,?)""",
            (
                candidate["item_id"],
                run_id,
                candidate["source"]["source_id"],
                family_id,
                "answer_first",
                canonical_json(candidate),
                status,
                now(),
                now(),
            ),
        )


def test_generation_counts_include_both_question_revisions(tmp_path: Path) -> None:
    database = _database(tmp_path)
    parent_attempt_id: str | None = None
    parent_item_id: str | None = None
    for revision_index in range(3):
        attempt = streaming_module._generation_attempt(
            campaign_id="campaign",
            family_id="family",
            finding_attempt_index=1,
            question_revision_index=revision_index,
            attempt_kind="primary" if revision_index == 0 else "question_revision",
            parent_attempt_id=parent_attempt_id,
            parent_item_id=parent_item_id,
            trigger_reason_code=("question_context_missing" if revision_index else None),
            excluded_finding_span_ids=[],
        )
        item_id = f"item-{revision_index}"
        candidate = _candidate(
            attempt=attempt,
            item_id=item_id,
            source_id="source",
            family_id="family",
        )
        _insert_candidate(
            database,
            candidate,
            run_id="campaign",
            family_id="family",
            status="rejected",
        )
        parent_attempt_id = attempt["attempt_id"]
        parent_item_id = item_id

    metrics = streaming_module._generation_counts(database, "campaign")

    assert metrics["question_revision_count"] == 2


def test_fallback_allows_two_revisions_before_an_alternative_finding() -> None:
    primary = streaming_module._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )
    path = {
        "attempt": primary,
        "candidate": {
            "item_id": "item-primary",
            "candidate_json": canonical_json(
                {"answer": {"source_span_id": "span-primary"}}
            ),
        },
    }
    paths = {(1, 0): path}

    revision = streaming_module._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths=paths,
        failed_path=path,
        reason_codes=["question_context_missing"],
    )

    assert revision is not None
    assert revision["attempt_kind"] == "question_revision"
    assert revision["finding_attempt_index"] == 1
    assert revision["question_revision_index"] == 1
    paths[(1, 1)] = {
        "attempt": revision,
        "candidate": {
            "item_id": "item-revision",
            "candidate_json": canonical_json(
                {"answer": {"source_span_id": "span-primary"}}
            ),
        },
    }

    second_revision = streaming_module._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths=paths,
        failed_path=paths[(1, 1)],
        reason_codes=["question_context_missing"],
    )

    assert second_revision is not None
    assert second_revision["attempt_kind"] == "question_revision"
    assert second_revision["finding_attempt_index"] == 1
    assert second_revision["question_revision_index"] == 2
    assert second_revision["parent_attempt_id"] == revision["attempt_id"]
    paths[(1, 2)] = {
        "attempt": second_revision,
        "candidate": {
            "item_id": "item-second-revision",
            "candidate_json": canonical_json(
                {"answer": {"source_span_id": "span-primary"}}
            ),
        },
    }

    alternative = streaming_module._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths=paths,
        failed_path=paths[(1, 2)],
        reason_codes=["reconstruction_disagreement"],
    )

    assert alternative is not None
    assert alternative["attempt_kind"] == "alternative_finding"
    assert alternative["finding_attempt_index"] == 2
    assert alternative["question_revision_index"] == 0
    assert alternative["parent_attempt_id"] == second_revision["attempt_id"]

    assert len({
        primary["attempt_id"],
        revision["attempt_id"],
        second_revision["attempt_id"],
        alternative["attempt_id"],
    }) == 4


def test_answer_bearing_required_phrase_routes_immediately_to_alternative() -> None:
    primary = streaming_module._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )
    path = {
        "attempt": primary,
        "candidate": {
            "item_id": "item-primary",
            "candidate_json": canonical_json(
                {"answer": {"source_span_id": "span-primary"}}
            ),
        },
    }

    alternative = streaming_module._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths={(1, 0): path},
        failed_path=path,
        reason_codes=["finding_answer_phrase_in_required_question_phrases"],
    )

    assert alternative is not None
    assert alternative["attempt_kind"] == "alternative_finding"
    assert alternative["finding_attempt_index"] == 2
    assert alternative["question_revision_index"] == 0
    assert alternative["excluded_finding_span_ids"] == ["span-primary"]


def test_generation_lineage_allows_six_bounded_paths() -> None:
    paths: dict[tuple[int, int], dict] = {}
    parent_attempt_id: str | None = None
    parent_item_id: str | None = None
    for finding_index in (1, 2):
        for revision_index in range(3):
            kind = (
                "primary"
                if (finding_index, revision_index) == (1, 0)
                else "alternative_finding"
                if (finding_index, revision_index) == (2, 0)
                else "question_revision"
            )
            attempt = streaming_module._generation_attempt(
                campaign_id="campaign",
                family_id="family",
                finding_attempt_index=finding_index,
                question_revision_index=revision_index,
                attempt_kind=kind,
                parent_attempt_id=parent_attempt_id,
                parent_item_id=parent_item_id,
                trigger_reason_code="reconstruction_disagreement"
                if parent_attempt_id
                else None,
                excluded_finding_span_ids=(
                    ["span-primary"]
                    if (finding_index, revision_index) == (2, 0)
                    else []
                ),
            )
            item_id = f"item-{finding_index}-{revision_index}"
            paths[(finding_index, revision_index)] = {
                "attempt": attempt,
                "candidate": {"item_id": item_id, "candidate_json": "{}"},
            }
            parent_attempt_id = attempt["attempt_id"]
            parent_item_id = item_id

    streaming_module._validate_generation_lineage(paths)
    assert len(paths) == streaming_module.MAX_CANDIDATE_PATHS == 6
    with pytest.raises(ValueError, match="bounded generation path limit"):
        streaming_module._validate_generation_lineage({**paths, (3, 0): paths[(1, 0)]})


def test_fallback_does_not_progress_from_a_contract_mismatch() -> None:
    primary = streaming_module._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )
    path = {
        "attempt": primary,
        "candidate": {
            "item_id": "item-primary",
            "candidate_json": canonical_json(
                {"answer": {"source_span_id": "span-primary"}}
            ),
        },
    }

    assert (
        streaming_module._next_generation_attempt(
            campaign_id="campaign",
            family_id="family",
            paths={(1, 0): path},
            failed_path=path,
            reason_codes=["generation_contract_version_mismatch"],
        )
        is None
    )


def test_fallback_stops_after_two_revisions_or_multiple_reasons() -> None:
    primary = streaming_module._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )
    paths = {
        (1, 0): {
            "attempt": primary,
            "candidate": {"item_id": "item-primary", "candidate_json": "{}"},
        }
    }
    first_revision = streaming_module._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths=paths,
        failed_path=paths[(1, 0)],
        reason_codes=["reconstruction_disagreement"],
    )
    assert first_revision is not None
    paths[(1, 1)] = {
        "attempt": first_revision,
        "candidate": {"item_id": "item-first", "candidate_json": "{}"},
    }
    second_revision = streaming_module._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths=paths,
        failed_path=paths[(1, 1)],
        reason_codes=["reconstruction_disagreement"],
    )
    assert second_revision is not None
    paths[(1, 2)] = {
        "attempt": second_revision,
        "candidate": {"item_id": "item-second", "candidate_json": "{}"},
    }

    assert (
        streaming_module._next_generation_attempt(
            campaign_id="campaign",
            family_id="family",
            paths=paths,
            failed_path=paths[(1, 2)],
            reason_codes=["reconstruction_disagreement"],
        )
        is not None
    )
    assert (
        streaming_module._next_generation_attempt(
            campaign_id="campaign",
            family_id="family",
            paths=paths,
            failed_path=paths[(1, 0)],
            reason_codes=["reconstruction_disagreement", "answer_ambiguous"],
        )
        is None
    )
    assert (
        streaming_module._next_generation_attempt(
            campaign_id="campaign",
            family_id="family",
            paths=paths,
            failed_path=paths[(1, 2)],
            reason_codes=["provider_response_ambiguous"],
        )
        is None
    )


@pytest.mark.parametrize(
    ("failure_reason", "accept_on", "expected_kinds"),
    [
        ("question_context_missing", 2, ["primary", "question_revision"]),
        (
            "reconstruction_disagreement",
            3,
            ["primary", "question_revision", "question_revision"],
        ),
    ],
)
def test_progress_generation_continues_with_a_fresh_path_after_rejection(
    tmp_path: Path,
    monkeypatch,
    failure_reason: str,
    accept_on: int,
    expected_kinds: list[str],
) -> None:
    database = _database(tmp_path)
    namespace = tmp_path / "namespace"
    namespace.mkdir()
    manifest = namespace / "run-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    progress = _Progress(
        namespace / "progress.json",
        run_id="campaign",
        invocation_run_id="invocation",
        run_manifest_file=manifest,
        counts={},
    )
    calls: list[dict] = []
    source_id = "source"
    family_id = "family"

    def fake_generate(database, namespace, **kwargs):
        attempt = kwargs["generation_attempt"]
        calls.append(attempt)
        item_id = stable_id("candidate", attempt["attempt_id"])
        candidate = _candidate(
            attempt=attempt,
            item_id=item_id,
            source_id=source_id,
            family_id=family_id,
        )
        _insert_candidate(
            database,
            candidate,
            run_id="campaign",
            family_id=family_id,
            status="candidate",
        )
        return candidate

    def fake_validate(database, namespace, candidate):
        accepted = len(calls) == accept_on
        status = "machine_accepted_unverified" if accepted else "rejected"
        reasons = [] if accepted else [failure_reason]
        labels = {"mcq_eligible": accepted}
        with database.transaction():
            database.connection.execute(
                "UPDATE candidates SET status=? WHERE item_id=?",
                (status, candidate["item_id"]),
            )
            database.connection.execute(
                """INSERT INTO validation_events
                (event_id,item_id,stage,label,reason_codes_json,details_json,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    stable_id("validation", candidate["item_id"]),
                    candidate["item_id"],
                    "automated_acceptance",
                    status,
                    canonical_json(reasons),
                    canonical_json(
                        {
                            "candidate_hash": stable_id(
                                "candidate-payload", canonical_json(candidate)
                            ),
                            "labels": labels,
                        }
                    ),
                    now(),
                ),
            )
        return SimpleNamespace(
            as_dict=lambda: {
                "final_label": status,
                "labels": labels,
                "reasons": reasons,
            }
        )

    monkeypatch.setattr(streaming_module, "generate_candidate", fake_generate)
    monkeypatch.setattr(streaming_module, "validate_candidate", fake_validate)

    result = _progress_generation(
        database,
        namespace,
        progress,
        campaign_id="campaign",
        candidate_key="candidate-key",
        source_id=source_id,
        family_id=family_id,
        selected={},
        title="Fixture",
        author=object(),
        verifier=object(),
    )

    assert result["disposition"] == "accepted"
    assert [attempt["attempt_kind"] for attempt in calls] == expected_kinds
    assert all(
        attempt["parent_attempt_id"] == previous["attempt_id"]
        for previous, attempt in zip(calls, calls[1:])
    )
    assert all(not attempt["excluded_finding_span_ids"] for attempt in calls)
    assert database.one(
        "SELECT COUNT(*) AS count FROM candidates WHERE run_id=?", ("campaign",)
    )["count"] == accept_on


def test_existing_findings_seed_alternative_exclusions(tmp_path: Path) -> None:
    database = _database(tmp_path)
    campaign_id = "campaign"
    family_id = "family"
    source_id = "source"
    with database.transaction():
        for finding_index, span_id in ((1, "frozen-primary"), (2, "frozen-alt")):
            database.connection.execute(
                """INSERT INTO findings
                (finding_id,run_id,source_id,paper_family_id,chunk_id,
                 selection_policy_version,answer_json,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    f"finding-{finding_index}",
                    campaign_id,
                    source_id,
                    family_id,
                    "chunk-1",
                    f"{generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION}:finding-"
                    f"{finding_index}",
                    canonical_json({"source_span_id": span_id}),
                    "frozen",
                    now(),
                ),
            )

    paths = streaming_module._generation_paths(
        database,
        campaign_id=campaign_id,
        source_id=source_id,
        family_id=family_id,
    )

    assert paths[(2, 0)]["attempt"]["excluded_finding_span_ids"] == [
        "frozen-primary"
    ]


def test_existing_findings_restore_primary_before_alternative_parent(
    tmp_path: Path, monkeypatch
) -> None:
    database = _database(tmp_path)
    campaign_id = "arctic-qa-production-campaign-001"
    family_id = "family-17e0f0a4909fdaed6d59"
    source_id = "src-17e0f0a4909fdaed6d59"
    with database.transaction():
        for finding_id, finding_index, span_id in (
            ("finding-909ca59694ed5085a475", 1, "frozen-primary"),
            ("finding-6ad815d8ce5278267308", 2, "frozen-alternative"),
        ):
            database.connection.execute(
                """INSERT INTO findings
                (finding_id,run_id,source_id,paper_family_id,chunk_id,
                 selection_policy_version,answer_json,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    finding_id,
                    campaign_id,
                    source_id,
                    family_id,
                    "chunk-1",
                    f"{generation_contract.SCOPE_ROLE_FINDING_POLICY_VERSION}:finding-"
                    f"{finding_index}",
                    canonical_json({"source_span_id": span_id}),
                    "frozen",
                    now(),
                ),
            )

    paths = streaming_module._generation_paths(
        database,
        campaign_id=campaign_id,
        source_id=source_id,
        family_id=family_id,
    )

    assert paths[(2, 0)]["attempt"]["parent_attempt_id"] == paths[(1, 0)][
        "attempt"
    ]["attempt_id"]

    namespace = tmp_path / "namespace"
    namespace.mkdir()
    manifest = namespace / "run-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    progress = _Progress(
        namespace / "progress.json",
        run_id=campaign_id,
        invocation_run_id="saved-transition-offline",
        run_manifest_file=manifest,
        counts={},
    )

    attempted: list[dict] = []

    def stop_before_provider_call(*args, **kwargs):
        attempted.append(kwargs["attempt"])
        raise RuntimeError("offline provider boundary")

    monkeypatch.setattr(
        streaming_module, "_generate_candidate_attempt", stop_before_provider_call
    )

    with pytest.raises(RuntimeError, match="offline provider boundary"):
        _progress_generation(
            database,
            namespace,
            progress,
            campaign_id=campaign_id,
            candidate_key="10.1007/s00382-018-4279-z",
            source_id=source_id,
            family_id=family_id,
            selected={},
            title="Summers with low Arctic sea ice",
            author=object(),
            verifier=object(),
        )

    assert attempted == [paths[(1, 0)]["attempt"]]


def test_generation_path_restore_error_stops_running_progress(
    tmp_path: Path, monkeypatch
) -> None:
    database = _database(tmp_path)
    namespace = tmp_path / "namespace"
    namespace.mkdir()
    manifest = namespace / "run-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    progress_file = namespace / "progress.json"
    progress = _Progress(
        progress_file,
        run_id="campaign",
        invocation_run_id="invocation",
        run_manifest_file=manifest,
        counts={},
    )
    progress.write("running", "eligibility", "Streaming pipeline started.")

    def fail_to_restore_paths(*args, **kwargs):
        raise ValueError("the generation attempt parent is missing")

    monkeypatch.setattr(
        streaming_module, "_generation_paths", fail_to_restore_paths
    )

    with pytest.raises(ValueError, match="generation attempt parent is missing"):
        _progress_generation(
            database,
            namespace,
            progress,
            campaign_id="campaign",
            candidate_key="candidate-key",
            source_id="source",
            family_id="family",
            selected={},
            title="Fixture",
            author=object(),
            verifier=object(),
        )

    persisted = json.loads(progress_file.read_text(encoding="utf-8"))
    assert persisted["state"] == "error"
    assert persisted["current_stage"] == "generation"
    assert persisted["recent_papers"][-1]["final_state"] == "error"


def test_malformed_alternative_state_becomes_a_paper_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    database = _database(tmp_path)
    namespace = tmp_path / "namespace"
    namespace.mkdir()
    manifest = namespace / "run-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    progress = _Progress(
        namespace / "progress.json",
        run_id="campaign",
        invocation_run_id="invocation",
        run_manifest_file=manifest,
        counts={},
    )
    primary = streaming_module._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )
    alternative = streaming_module._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=2,
        question_revision_index=0,
        attempt_kind="alternative_finding",
        parent_attempt_id=primary["attempt_id"],
        parent_item_id=None,
        trigger_reason_code="generation_rejected",
        excluded_finding_span_ids=[],
    )
    paths = {
        (1, 0): {"attempt": primary, "candidate": None},
        (2, 0): {
            "attempt": alternative,
            "candidate": None,
            "partial_finding": True,
        },
    }
    monkeypatch.setattr(
        streaming_module,
        "_generation_paths",
        lambda *args, **kwargs: paths,
    )

    def raise_invalid_state(*args, **kwargs):
        raise ValueError("alternative finding state is invalid")

    monkeypatch.setattr(
        streaming_module, "_generate_candidate_attempt", raise_invalid_state
    )

    result = _progress_generation(
        database,
        namespace,
        progress,
        campaign_id="campaign",
        candidate_key="candidate-key",
        source_id="source",
        family_id="family",
        selected={},
        title="Fixture",
        author=object(),
        verifier=object(),
    )

    assert result["disposition"] == "generation_rejected"
    assert result["reason_codes"] == ["alternative_finding_state_invalid"]
    assert database.one(
        "SELECT reason_code FROM rejection_ledger WHERE source_id=?",
        ("source",),
    ) == {"reason_code": "alternative_finding_state_invalid"}


def test_budget_stop_is_terminal_when_generation_resumes(
    tmp_path: Path, monkeypatch
) -> None:
    database = _database(tmp_path)
    namespace = tmp_path / "namespace"
    namespace.mkdir()
    manifest = namespace / "run-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    progress = _Progress(
        namespace / "progress.json",
        run_id="campaign",
        invocation_run_id="invocation",
        run_manifest_file=manifest,
        counts={},
    )
    calls = 0

    def fake_budget_generate(database, namespace, **kwargs):
        nonlocal calls
        calls += 1
        raise BudgetError(streaming_module.PER_REQUEST_CAP_REASON)

    monkeypatch.setattr(streaming_module, "generate_candidate", fake_budget_generate)

    arguments = {
        "campaign_id": "campaign",
        "candidate_key": "candidate-key",
        "source_id": "source",
        "family_id": "family",
        "selected": {},
        "title": "Fixture",
        "author": object(),
        "verifier": object(),
    }
    first = _progress_generation(database, namespace, progress, **arguments)
    second = _progress_generation(database, namespace, progress, **arguments)

    assert first == {
        "disposition": "generation_rejected",
        "reason_codes": ["request_cost_bound_exceeded"],
        "resumed": False,
    }
    assert second == {
        "disposition": "generation_rejected",
        "reason_codes": ["request_cost_bound_exceeded"],
        "resumed": True,
    }
    assert calls == 1
    assert database.one(
        "SELECT stage FROM rejection_ledger WHERE source_id=?", ("source",)
    )["stage"] == "generation_budget"
