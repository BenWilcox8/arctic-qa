from __future__ import annotations

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


def test_fallback_prefers_one_question_revision_then_alternative() -> None:
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

    alternative = streaming_module._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths=paths,
        failed_path=paths[(1, 1)],
        reason_codes=["reconstruction_disagreement"],
    )

    assert alternative is not None
    assert alternative["attempt_kind"] == "alternative_finding"
    assert alternative["finding_attempt_index"] == 2
    assert alternative["question_revision_index"] == 0
    assert alternative["parent_attempt_id"] == revision["attempt_id"]


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


@pytest.mark.parametrize(
    ("failure_reason", "expected_kind"),
    [
        ("reconstruction_disagreement", "alternative_finding"),
        ("question_context_missing", "question_revision"),
    ],
)
def test_progress_generation_continues_with_a_fresh_path_after_rejection(
    tmp_path: Path, monkeypatch, failure_reason: str, expected_kind: str
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
        accepted = len(calls) == 2
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
    assert [attempt["attempt_kind"] for attempt in calls] == ["primary", expected_kind]
    assert calls[1]["excluded_finding_span_ids"] == (
        [f"span-{stable_id('candidate', calls[0]['attempt_id'])}"]
        if expected_kind == "alternative_finding"
        else []
    )
    assert calls[1]["parent_attempt_id"] == calls[0]["attempt_id"]
    assert database.one(
        "SELECT COUNT(*) AS count FROM candidates WHERE run_id=?", ("campaign",)
    )["count"] == 2


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
