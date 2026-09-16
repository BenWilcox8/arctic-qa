"""The per-paper completion label of a streaming run.

Three guards. The label rule, on a fixture state with one paper per outcome
class and a paper of every mid-family shape, which takes no label. The startup
skip, which reads no receipt and replays no call of a labelled paper. The
self-labelling, which writes the label the moment a paper reaches a terminal
outcome and writes none for a paper left mid-family.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arctic_qa import paper_completion, streaming
from arctic_qa.db import Database
from arctic_qa.paths import DataPaths
from arctic_qa.providers import FakeProvider
from arctic_qa.streaming import run_stream
from arctic_qa.util import canonical_json

from test_streaming import FIXTURES, streaming_fixture, write_json


RUN_ID = "completion-fixture-r1"
CAMPAIGN_ID = "completion-fixture-campaign"
CODE_COMMIT = "abc1234"


def _open_database(tmp_path: Path) -> tuple[DataPaths, Database]:
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    return paths, database


def _stream_arguments(tmp_path: Path) -> dict[str, Any]:
    access, eligibility = streaming_fixture(tmp_path)
    paths, database = _open_database(tmp_path)
    return {
        "db": database,
        "namespace": paths.namespace,
        "run_id": RUN_ID,
        "campaign_id": CAMPAIGN_ID,
        "access_run_dir": access,
        "eligibility_run_dir": eligibility,
        "author": FakeProvider("fake-author", FIXTURES / "fake-author.jsonl"),
        "verifier": FakeProvider("fake-verifier", FIXTURES / "fake-verifier.jsonl"),
        "max_papers": 1,
        "code_commit": CODE_COMMIT,
    }


def _labels(database: Database) -> dict[str, dict[str, Any]]:
    return paper_completion.load_completions(database, run_id=RUN_ID)


def _access(candidate_key: str, family_id: str) -> dict[str, Any]:
    return {
        "candidate_key": candidate_key,
        "paper_family_id": family_id,
        "access_state": "full_text_ready",
        "source_content_hash": "0" * 64,
    }


def _job(decision: str, *, valid: bool = True, errors: list[str] | None = None):
    return {
        "candidate_key": "unused",
        "execution_authority": "shared_gemini_broker",
        "state": "completed" if valid else "screening_error",
        "validation": {
            "decision": decision,
            "valid": valid,
            "errors": errors or [],
            "overall_reason_codes": [f"test_only_{decision}"],
        },
        "parsed_response": {"overall_reason_codes": [f"parsed_{decision}"]},
    }


def _insert_rejection(
    database: Database, *, stage: str, reason_code: str, detail: dict[str, Any]
) -> None:
    with database.transaction():
        database.connection.execute(
            """INSERT INTO rejection_ledger
            (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
            VALUES (?,NULL,NULL,?,?,?,'2026-09-16T00:00:00+00:00')""",
            (
                f"rejection-{stage}-{detail['candidate_key']}",
                stage,
                reason_code,
                canonical_json(detail),
            ),
        )


def _insert_source(database: Database, *, candidate_key: str, family_id: str) -> str:
    source_id = f"src-{family_id}"
    with database.transaction():
        database.connection.execute(
            """INSERT INTO sources
            (source_id,stable_id,doi,title,authors_json,access_state,provenance_json,
             corrections_json,zotero_json,paper_family_id,geography_state,
             geography_confidence,scope_evidence_json,eligibility_state,
             metadata_json,created_at,updated_at)
            VALUES (?,?,NULL,'t','[]','full_text_ready','[]','[]','{}',?,
                    'core_arctic','test','{}','eligible','{}',
                    '2026-09-16T00:00:00+00:00','2026-09-16T00:00:00+00:00')""",
            (source_id, candidate_key, family_id),
        )
    return source_id


def _classify(
    database: Database,
    *,
    candidate_key: str,
    family_id: str,
    eligibility: dict[str, Any] | None,
    access: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return paper_completion.classify_paper(
        database,
        campaign_id=CAMPAIGN_ID,
        candidate_key=candidate_key,
        family_id=family_id,
        access=access if access is not None else _access(candidate_key, family_id),
        eligibility=eligibility,
    )


# ---------------------------------------------------------------------------
# The label rule.


def test_the_rule_labels_one_paper_per_outcome_class_and_no_mid_family_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, database = _open_database(tmp_path)

    # Eligibility outcomes, from the stored job alone.
    excluded = _classify(
        database,
        candidate_key="p-excluded",
        family_id="f-excluded",
        eligibility=_job("excluded"),
    )
    assert excluded["complete"] is True
    assert excluded["outcome_class"] == "eligibility_excluded"
    assert excluded["reason_code"] == "test_only_excluded"
    assert excluded["eligibility_decision"] == "excluded"

    uncertain = _classify(
        database,
        candidate_key="p-uncertain",
        family_id="f-uncertain",
        eligibility=_job("uncertain"),
    )
    assert uncertain["outcome_class"] == "eligibility_unresolved"

    # An invalid answer stays uncertain and is final for this prompt version,
    # whatever the job's state says about a later re-screen.
    invalid = _classify(
        database,
        candidate_key="p-invalid",
        family_id="f-invalid",
        eligibility=_job(
            "uncertain", valid=False, errors=["criterion_evidence_missing:x"]
        ),
    )
    assert invalid["complete"] is True
    assert invalid["outcome_class"] == "eligibility_unresolved"
    assert invalid["reason_code"] == "criterion_evidence_missing:x"

    # The per-paper cost cap, recorded before or after screening.
    _insert_rejection(
        database,
        stage="paper_cost_cap",
        reason_code=streaming.PAPER_COST_CAP_REASON_CODE,
        detail={"campaign_id": CAMPAIGN_ID, "candidate_key": "p-capped"},
    )
    capped = _classify(
        database, candidate_key="p-capped", family_id="f-capped", eligibility=None
    )
    assert capped["outcome_class"] == "paper_cost_cap_reached"
    assert capped["eligibility_decision"] is None

    # The generation outcomes, through the producer's own ladder.
    def terminal(disposition: str, reasons: list[str]):
        def outcome(*_arguments: Any, **_keywords: Any) -> dict[str, Any]:
            return {
                "kind": streaming.STORED_OUTCOME_TERMINAL,
                "disposition": disposition,
                "reason_codes": reasons,
            }

        return outcome

    for candidate_key, disposition, outcome_class in (
        ("p-rejected", "generation_rejected", "generation_rejected"),
        ("p-non-mcq", "incomplete_non_mcq", "incomplete_non_mcq"),
        ("p-accepted", "accepted", "generation_accepted"),
    ):
        family_id = candidate_key.replace("p-", "f-")
        source_id = _insert_source(
            database, candidate_key=candidate_key, family_id=family_id
        )
        monkeypatch.setattr(
            streaming,
            "_stored_generation_outcome",
            terminal(disposition, ["reason_a", "reason_b"]),
        )
        row = _classify(
            database,
            candidate_key=candidate_key,
            family_id=family_id,
            eligibility=_job("eligible"),
        )
        assert row["complete"] is True, candidate_key
        assert row["outcome_class"] == outcome_class
        assert row["reason_code"] == "reason_a"
        assert row["source_id"] == source_id
        assert row["eligibility_decision"] == "eligible"

    # A paper mid-family takes no label, whatever shape it has.
    def pending(kind: str):
        def outcome(*_arguments: Any, **_keywords: Any) -> dict[str, Any]:
            return {"kind": kind, "attempt": {}, "unmet": ["slot"]}

        return outcome

    for kind, reason in (
        (streaming.STORED_OUTCOME_ATTEMPT, "generation_pending"),
        (streaming.STORED_OUTCOME_UNVALIDATED, "candidate_unvalidated"),
        (streaming.STORED_OUTCOME_SLOT_LOOKUP_REQUIRED, "slot_lookup_pending"),
    ):
        _insert_source(database, candidate_key=f"p-{reason}", family_id=f"f-{reason}")
        monkeypatch.setattr(streaming, "_stored_generation_outcome", pending(kind))
        row = _classify(
            database,
            candidate_key=f"p-{reason}",
            family_id=f"f-{reason}",
            eligibility=_job("eligible"),
        )
        assert row["complete"] is False, reason
        assert row["incomplete_reason"] == reason

    not_imported = _classify(
        database,
        candidate_key="p-no-source",
        family_id="f-no-source",
        eligibility=_job("eligible"),
    )
    assert not_imported == {
        "candidate_key": "p-no-source",
        "paper_family_id": "f-no-source",
        "complete": False,
        "incomplete_reason": "source_not_imported",
        "eligibility_decision": "eligible",
    }

    not_screened = _classify(
        database,
        candidate_key="p-unscreened",
        family_id="f-unscreened",
        eligibility=None,
    )
    assert not_screened["incomplete_reason"] == "not_screened"

    other_authority = _classify(
        database,
        candidate_key="p-other",
        family_id="f-other",
        eligibility={**_job("excluded"), "execution_authority": "direct"},
    )
    assert other_authority["incomplete_reason"] == "not_screened"

    # An unsettled or ambiguous request leaves the family mid-flight.
    with database.transaction():
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES ('call-open',?,'src-f-open','f-open','answer_first','{}',
                    'incomplete_infra','2026-09-16T00:00:00+00:00',
                    '2026-09-16T00:00:00+00:00')""",
            (CAMPAIGN_ID,),
        )
    open_call = _classify(
        database,
        candidate_key="p-open",
        family_id="f-open",
        eligibility=_job("excluded"),
    )
    assert open_call["incomplete_reason"] == "incomplete_infra_call"

    _insert_rejection(
        database,
        stage="generation",
        reason_code="ambiguous_charge_unresolved",
        detail={
            "campaign_id": CAMPAIGN_ID,
            "candidate_key": "p-ambiguous",
            "operational_unresolved": True,
        },
    )
    ambiguous = _classify(
        database,
        candidate_key="p-ambiguous",
        family_id="f-ambiguous",
        eligibility=_job("excluded"),
    )
    assert ambiguous["incomplete_reason"] == "operational_unresolved"
    assert ambiguous["reason_code"] == "ambiguous_charge_unresolved"

    pending_access = _classify(
        database,
        candidate_key="p-pending",
        family_id="f-pending",
        eligibility=_job("excluded"),
        access={"candidate_key": "p-pending", "access_state": "access_pending"},
    )
    assert pending_access["incomplete_reason"] == "not_full_text_ready"


def test_the_batch_labels_every_finished_paper_once_in_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, database = _open_database(tmp_path)
    _insert_rejection(
        database,
        stage="paper_cost_cap",
        reason_code=streaming.PAPER_COST_CAP_REASON_CODE,
        detail={"campaign_id": CAMPAIGN_ID, "candidate_key": "p-capped"},
    )
    selection = [
        {"position": 1, "candidate_key": "p-excluded"},
        {"position": 2, "candidate_key": "p-pending"},
        {"position": 3, "candidate_key": "p-capped"},
        {"position": 4, "candidate_key": "p-unscreened"},
        {"position": 5, "candidate_key": "p-eligible"},
    ]
    access_items = {
        key: _access(key, key.replace("p-", "f-"))
        for key in ("p-excluded", "p-capped", "p-unscreened", "p-eligible")
    }
    access_items["p-pending"] = {"candidate_key": "p-pending", "access_state": "x"}
    eligibility_jobs = {
        "p-excluded": _job("excluded"),
        "p-capped": _job("eligible"),
        "p-eligible": _job("eligible"),
    }
    _insert_source(database, candidate_key="p-eligible", family_id="f-eligible")
    monkeypatch.setattr(
        streaming,
        "_stored_generation_outcome",
        lambda *a, **k: {"kind": streaming.STORED_OUTCOME_ATTEMPT, "attempt": {}},
    )
    arguments = {
        "run_id": RUN_ID,
        "campaign_id": CAMPAIGN_ID,
        "selection": selection,
        "access_items": access_items,
        "eligibility_jobs": eligibility_jobs,
        "code_commit": CODE_COMMIT,
    }

    dry = paper_completion.label_run(database, dry_run=True, **arguments)

    assert dry["dry_run"] is True
    assert dry["written"] == 0
    assert dry["counts"] == {
        "walked": 4,
        "complete": 2,
        "already_labelled": 0,
        "to_label": 2,
        "incomplete": 2,
        "per_outcome_class": {
            "eligibility_excluded": 1,
            "eligibility_unresolved": 0,
            "generation_accepted": 0,
            "generation_rejected": 0,
            "incomplete_non_mcq": 0,
            "paper_cost_cap_reached": 1,
        },
        "per_incomplete_reason": {
            "not_full_text_ready": 0,
            "not_screened": 1,
            "eligibility_incomplete": 0,
            "operational_unresolved": 0,
            "incomplete_infra_call": 0,
            "source_not_imported": 0,
            "candidate_unvalidated": 0,
            "generation_pending": 1,
            "slot_lookup_pending": 0,
        },
    }
    assert [row["candidate_key"] for row in dry["papers"]] == [
        "p-excluded",
        "p-capped",
        "p-unscreened",
        "p-eligible",
    ]
    assert _labels(database) == {}

    applied = paper_completion.label_run(database, dry_run=False, **arguments)

    assert applied["written"] == 2
    labels = _labels(database)
    assert set(labels) == {"p-excluded", "p-capped"}
    assert labels["p-excluded"]["schema"] == paper_completion.COMPLETION_SCHEMA
    assert labels["p-excluded"]["run_id"] == RUN_ID
    assert labels["p-excluded"]["campaign_id"] == CAMPAIGN_ID
    assert labels["p-excluded"]["outcome_class"] == "eligibility_excluded"
    assert labels["p-excluded"]["labelled_by_commit"] == CODE_COMMIT
    assert labels["p-excluded"]["labelled_at_utc"]
    assert json.loads(labels["p-excluded"]["detail_json"]) == {"labelled_by": "batch"}

    again = paper_completion.label_run(database, dry_run=False, **arguments)

    assert again["written"] == 0
    assert again["counts"]["already_labelled"] == 2
    assert again["counts"]["to_label"] == 0
    assert _labels(database) == labels


def test_a_batch_that_fails_midway_writes_no_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, database = _open_database(tmp_path)
    rows = [
        paper_completion.completion_row(
            run_id=RUN_ID,
            campaign_id=CAMPAIGN_ID,
            candidate_key=key,
            family_id=f"f-{key}",
            source_id=None,
            outcome_class="eligibility_excluded",
            eligibility_decision="excluded",
            reason_code=None,
            code_commit=CODE_COMMIT,
        )
        for key in ("one", "two")
    ]
    rows[1]["reason_code"] = {"unbindable": True}  # the second insert fails

    with pytest.raises(Exception):
        paper_completion.record_completions(database, rows)

    assert _labels(database) == {}


# ---------------------------------------------------------------------------
# The self-labelling.


def test_the_producer_labels_a_paper_the_moment_it_finishes(tmp_path: Path) -> None:
    arguments = _stream_arguments(tmp_path)

    result = run_stream(**arguments)

    assert result["counts"]["accepted_base_questions"] == 1
    assert result["counts"]["completion_labelled_skipped"] == 0
    labels = _labels(arguments["db"])
    assert list(labels) == ["test-only:streaming-paper"]
    label = labels["test-only:streaming-paper"]
    assert label["schema"] == paper_completion.COMPLETION_SCHEMA
    assert label["run_id"] == RUN_ID
    assert label["campaign_id"] == CAMPAIGN_ID
    assert label["outcome_class"] == "generation_accepted"
    assert label["eligibility_decision"] == "eligible"
    assert label["source_id"] == result["paper_results"][0]["source_id"]
    assert label["labelled_by_commit"] == CODE_COMMIT
    assert json.loads(label["detail_json"])["labelled_by"] == "producer"


def test_the_producer_labels_an_excluded_paper(tmp_path: Path) -> None:
    arguments = _stream_arguments(tmp_path)
    job_path = arguments["eligibility_run_dir"] / "jobs" / "fixture-job.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job["parsed_response"]["overall"] = "excluded"
    job["validation"]["decision"] = "excluded"
    write_json(job_path, job)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    arguments["author"] = FakeProvider("fake-author", empty)
    arguments["verifier"] = FakeProvider("fake-verifier", empty)

    result = run_stream(**arguments)

    assert result["counts"]["eligibility_rejected"] == 1
    label = _labels(arguments["db"])["test-only:streaming-paper"]
    assert label["outcome_class"] == "eligibility_excluded"
    assert label["eligibility_decision"] == "excluded"
    assert label["source_id"] is None
    assert label["reason_code"] == result["paper_results"][0]["reason_codes"][0]


@pytest.mark.parametrize(
    "disposition",
    [
        "operational_unresolved",
        "candidate_processing_fault",
        "count_tokens_unavailable",
    ],
)
def test_a_paper_left_mid_family_takes_no_label(disposition: str) -> None:
    assert (
        paper_completion.label_from_disposition(
            run_id=RUN_ID,
            campaign_id=CAMPAIGN_ID,
            candidate_key="p",
            family_id="f",
            source_id=None,
            disposition=disposition,
            reason_codes=[disposition],
            eligibility_decision=None,
            code_commit=CODE_COMMIT,
        )
        is None
    )


def test_every_terminal_disposition_has_one_outcome_class_and_one_count() -> None:
    assert set(paper_completion.DISPOSITION_OUTCOME_CLASSES.values()) == set(
        paper_completion.OUTCOME_CLASSES
    )
    assert set(streaming.COMPLETION_CLASS_DISPOSITIONS) == set(
        paper_completion.OUTCOME_CLASSES
    )
    assert set(streaming.COMPLETION_CLASS_COUNTS) == set(
        paper_completion.OUTCOME_CLASSES
    )


# ---------------------------------------------------------------------------
# The startup skip.


def _refuse(name: str):
    def refused(*_arguments: Any, **_keywords: Any) -> Any:
        raise AssertionError(f"{name} was called for a labelled paper")

    return refused


def test_a_labelled_paper_is_skipped_before_any_receipt_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _stream_arguments(tmp_path)
    first = run_stream(**arguments)
    assert _labels(arguments["db"])

    for name in ("_progress_generation", "_import_source", "_validate_pair"):
        monkeypatch.setattr(streaming, name, _refuse(name))

    resumed = run_stream(**arguments)

    assert resumed["state"] == "completed"
    assert resumed["resumed_papers"] == 0
    assert resumed["counts"] == {
        **first["counts"],
        "completion_labelled_skipped": 1,
    }
    assert resumed["paper_results"] == [
        {
            "candidate_key": "test-only:streaming-paper",
            "disposition": "accepted",
            "reason_codes": first["paper_results"][0]["reason_codes"][:1],
            "source_id": first["paper_results"][0]["source_id"],
            "completion_label": {
                "outcome_class": "generation_accepted",
                "labelled_at_utc": _labels(arguments["db"])[
                    "test-only:streaming-paper"
                ]["labelled_at_utc"],
                "labelled_by_commit": CODE_COMMIT,
            },
        }
    ]
    # The label is a note about finished work: the run's own state is untouched.
    assert resumed["export"]["mcq_count"] == first["export"]["mcq_count"]


def test_a_paper_without_a_label_keeps_the_receipt_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _stream_arguments(tmp_path)
    run_stream(**arguments)
    with arguments["db"].transaction():
        arguments["db"].connection.execute("DELETE FROM paper_completions")

    walked: list[str] = []
    real = streaming._progress_generation

    def spy(*positional: Any, **keywords: Any) -> Any:
        walked.append(keywords["candidate_key"])
        return real(*positional, **keywords)

    monkeypatch.setattr(streaming, "_progress_generation", spy)

    resumed = run_stream(**arguments)

    assert walked == ["test-only:streaming-paper"]
    assert resumed["resumed_papers"] == 1
    assert resumed["counts"]["completion_labelled_skipped"] == 0
    # The walk labels the paper again, so the next start skips it.
    assert _labels(arguments["db"])["test-only:streaming-paper"]["outcome_class"] == (
        "generation_accepted"
    )


def test_the_eligibility_trust_walk_reads_no_receipt_of_a_labelled_paper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    access, _ = streaming_fixture(tmp_path)
    manifest = json.loads((access / "run-manifest.json").read_text(encoding="utf-8"))
    access_items = {
        item["candidate_key"]: item
        for item in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in (access / "items").glob("*.json")
        )
    }
    monkeypatch.setattr(
        streaming, "_validate_brokered_eligibility", _refuse("receipt validation")
    )
    monkeypatch.setattr(streaming, "_bind_provider", _refuse("provider binding"))
    completions = {
        "test-only:streaming-paper": {
            "outcome_class": "eligibility_unresolved",
            "eligibility_decision": "uncertain",
        }
    }

    decisions = streaming._trusted_brokered_eligibility_decisions(
        selection=manifest["selection"],
        access_items=access_items,
        eligibility_jobs={
            "test-only:streaming-paper": {"execution_authority": "shared_gemini_broker"}
        },
        verifier=object(),
        max_papers=1,
        prompt_file=Path("unused"),
        schema_file=Path("unused"),
        policy_file=Path("unused"),
        completions=completions,
    )

    assert decisions == {"test-only:streaming-paper": "uncertain"}

    # A cost-capped paper that was never screened carries no decision.
    completions["test-only:streaming-paper"]["eligibility_decision"] = None
    assert (
        streaming._trusted_brokered_eligibility_decisions(
            selection=manifest["selection"],
            access_items=access_items,
            eligibility_jobs={},
            verifier=object(),
            max_papers=1,
            prompt_file=Path("unused"),
            schema_file=Path("unused"),
            policy_file=Path("unused"),
            completions=completions,
        )
        == {}
    )
