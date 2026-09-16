"""The bounded re-ask and the bounded geography re-screen in the streaming path.

Chapter 2 ran 202 eligibility calls for 200 papers, so at most two re-asks, and
the geography re-screen selected zero papers by construction. Both recoveries now
run inside the streaming pass (audit 4.7, findings E2 and E6).

These tests drive `_run_eligibility` with the provider call replaced, so the
orchestration is tested without a paid call and without a broker fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import arctic_qa.gemini_eligibility as eligibility
import arctic_qa.streaming as streaming


SATISFIED = (
    "published_primary_findings",
    "stable_identity_version",
    "access_rights_evidence",
)


def _parsed(
    *,
    geography: str = "satisfied",
    others: dict[str, str] | None = None,
    phrases: list[str] | None = None,
) -> dict[str, Any]:
    statuses = {name: "satisfied" for name in SATISFIED}
    statuses["correction_retraction_coverage"] = "uncertain"
    statuses.update(others or {})
    statuses["study_geography"] = geography
    return {
        "schema_version": eligibility.ELIGIBILITY_RESPONSE_V4,
        "criteria": [
            {"criterion_id": name, "status": status}
            for name, status in statuses.items()
        ],
        "eligible_arctic_scope": {
            "question_scope_phrases": ["Ny-Ålesund station"]
            if phrases is None
            else phrases
        },
    }


def _job(
    key: str,
    *,
    valid: bool,
    errors: list[str] | None = None,
    kind: str = "initial",
    attempt: int = 1,
    parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "gemini-eligibility-job-v1",
        "job_key": key,
        "candidate_key": "10.1/x",
        "state": "completed" if valid else "screening_error",
        "eligibility_attempt": {
            "kind": kind,
            "attempt": attempt,
            "attempt_note": None,
        },
        "parsed_response": parsed if parsed is not None else _parsed(),
        "validation": {
            "valid": valid,
            "errors": list(errors or []),
            "decision": "eligible" if valid else "uncertain",
        },
    }


class _Recorder:
    """Replace `_eligibility_attempt` with a scripted sequence of answers."""

    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = answers
        self.calls: list[dict[str, Any]] = []

    def __call__(self, db, access, run_dir, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.answers[len(self.calls) - 1]


def _access(tmp_path: Path) -> dict[str, Any]:
    extraction = tmp_path / "extraction.txt"
    extraction.write_text("Sampled at 78.9 N in March 2012.\n", encoding="utf-8")
    return {"candidate_key": "10.1/x", "extraction_path": str(extraction)}


def _run(tmp_path: Path, recorder: _Recorder, *, rescreen: bool = True):
    provider = type("Provider", (), {"broker": object()})()
    return streaming._run_eligibility(
        None,
        _access(tmp_path),
        tmp_path / "run",
        run_id="run",
        provider=provider,
        prompt_file=Path("config/gemini-eligibility-prompt-v8.txt"),
        schema_file=Path("schemas/gemini-eligibility.v4.schema.json"),
        policy_file=Path("config/arctic-eligibility-policy-v3.json"),
        rescreen_prompt_file=(
            Path("config/gemini-eligibility-geography-rescreen-v2.txt")
            if rescreen
            else None
        ),
    )


# --------------------------------------------------------- the format re-ask


def test_a_formatting_mistake_is_re_asked_inside_the_same_pass(
    tmp_path: Path, monkeypatch
) -> None:
    """Audit 4.7 E6: the re-ask lived in the batch module and never ran."""
    recorder = _Recorder(
        [
            _job("a", valid=False, errors=["eligible_arctic_scope_phrase_unbound"]),
            _job("b", valid=True, kind="format_repair", attempt=2),
        ]
    )
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder, rescreen=False)

    assert len(recorder.calls) == 2
    assert recorder.calls[0]["kind"] == "initial"
    assert recorder.calls[1]["kind"] == "format_repair"
    note = recorder.calls[1]["attempt_note"]
    assert note["format_errors"] == ["eligible_arctic_scope_phrase_unbound"]
    assert "Do not change any criterion status" in note["instruction"]
    assert job["state"] == "completed"
    assert job["validation"]["valid"] is True


def test_the_re_ask_is_bounded_and_leaves_the_paper_re_screenable(
    tmp_path: Path, monkeypatch
) -> None:
    """A spent paper is unresolved, never a terminal screening error."""
    failure = _job("a", valid=False, errors=["eligible_arctic_scope_phrase_unbound"])
    recorder = _Recorder([failure, dict(failure, job_key="b")])
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder, rescreen=False)

    assert len(recorder.calls) == eligibility.MAXIMUM_FORMAT_ATTEMPTS
    assert job["state"] == eligibility.UNRESOLVED_STATE
    assert streaming._brokered_eligibility_state(job, job["validation"]) == (
        eligibility.UNRESOLVED_STATE
    )


def test_an_envelope_error_is_never_re_asked(tmp_path: Path, monkeypatch) -> None:
    """Rigor: only a scope-shape mistake is a formatting mistake."""
    recorder = _Recorder(
        [_job("a", valid=False, errors=["refusal_block_or_malformed_response"])]
    )
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder, rescreen=False)

    assert len(recorder.calls) == 1
    assert job["state"] == "screening_error"


def test_a_repair_that_moves_a_criterion_status_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """Rigor: a repair corrects a shape, never a scientific judgment."""
    recorder = _Recorder(
        [
            _job(
                "a",
                valid=False,
                errors=["eligible_arctic_scope_phrase_unbound"],
                parsed=_parsed(geography="uncertain"),
            ),
            _job(
                "b",
                valid=True,
                kind="format_repair",
                attempt=2,
                parsed=_parsed(geography="satisfied"),
            ),
        ]
    )
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder, rescreen=False)

    assert job["validation"]["errors"] == ["repair_changed_criterion_status"]
    assert job["validation"]["valid"] is False
    assert job["state"] == eligibility.UNRESOLVED_STATE


def test_a_repaired_phrase_that_names_nothing_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """Audit 4.7 phase D: re-check that a repaired phrase still names a scope."""
    recorder = _Recorder(
        [
            _job("a", valid=False, errors=["eligible_arctic_scope_phrase_unbound"]),
            _job(
                "b",
                valid=True,
                kind="format_repair",
                attempt=2,
                parsed=_parsed(phrases=["In the Arctic"]),
            ),
        ]
    )
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder, rescreen=False)

    assert job["validation"]["errors"] == ["eligible_arctic_scope_phrase_not_specific"]
    assert job["state"] == eligibility.UNRESOLVED_STATE


# ---------------------------------------------------- the geography re-screen


def test_an_unresolved_geography_is_re_screened_once_in_the_same_pass(
    tmp_path: Path, monkeypatch
) -> None:
    """Audit 4.7 E2: the r15 re-screen mechanism never fired in production."""
    recorder = _Recorder(
        [
            _job("a", valid=True, parsed=_parsed(geography="uncertain")),
            _job("b", valid=True, kind="geography_rescreen"),
        ]
    )
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder)

    assert [call["kind"] for call in recorder.calls] == [
        "initial",
        "geography_rescreen",
    ]
    note = recorder.calls[1]["attempt_note"]
    assert note["kind"] == "geography_rescreen"
    assert "study_geography" not in note["frozen_criterion_statuses"]
    assert note["frozen_criterion_statuses"]["published_primary_findings"] == (
        "satisfied"
    )
    assert job["geography_rescreen"] == "applied"
    assert job["rescreened_from"] == "a"
    assert job["validation"]["decision"] == "eligible"


def test_a_failed_geography_is_never_re_screened(tmp_path: Path, monkeypatch) -> None:
    """Rigor: a failed geography is a decision and never returns."""
    recorder = _Recorder([_job("a", valid=True, parsed=_parsed(geography="failed"))])
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder)

    assert len(recorder.calls) == 1
    assert "geography_rescreen" not in job


def test_a_second_unresolved_criterion_blocks_the_re_screen(
    tmp_path: Path, monkeypatch
) -> None:
    """The re-screen decides one criterion, so it cannot free such a paper."""
    recorder = _Recorder(
        [
            _job(
                "a",
                valid=True,
                parsed=_parsed(
                    geography="uncertain",
                    others={"access_rights_evidence": "uncertain"},
                ),
            )
        ]
    )
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    _run(tmp_path, recorder)

    assert len(recorder.calls) == 1


def test_a_re_screen_that_moves_a_frozen_status_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """Rigor: the re-screen decides one criterion and freezes the other four."""
    recorder = _Recorder(
        [
            _job("a", valid=True, parsed=_parsed(geography="uncertain")),
            _job(
                "b",
                valid=True,
                kind="geography_rescreen",
                parsed=_parsed(others={"stable_identity_version": "failed"}),
            ),
        ]
    )
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder)

    assert job["geography_rescreen"] == "refused_moved_frozen_status"
    assert job["job_key"] == "a"


def test_an_invalid_re_screen_answer_leaves_the_first_screening_in_place(
    tmp_path: Path, monkeypatch
) -> None:
    recorder = _Recorder(
        [
            _job("a", valid=True, parsed=_parsed(geography="uncertain")),
            _job(
                "b",
                valid=False,
                errors=["eligible_arctic_scope_span_unknown"],
                kind="geography_rescreen",
            ),
        ]
    )
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder)

    assert job["geography_rescreen"] == "unresolved_invalid_response"
    assert job["job_key"] == "a"


def test_no_re_screen_prompt_means_no_re_screen_call(
    tmp_path: Path, monkeypatch
) -> None:
    recorder = _Recorder([_job("a", valid=True, parsed=_parsed(geography="uncertain"))])
    monkeypatch.setattr(streaming, "_eligibility_attempt", recorder)

    job = _run(tmp_path, recorder, rescreen=False)

    assert len(recorder.calls) == 1
    assert job["geography_rescreen"] == "skipped_no_prompt"


# --------------------------------------------------------- the identity binding


def test_each_attempt_binds_its_own_receipt_identity() -> None:
    """A re-ask and a re-screen send other bytes, so each takes its own key."""
    access = {
        "candidate_key": "10.1/x",
        "source_content_hash": "a" * 64,
        "extraction_sha256": "b" * 64,
    }
    config = {"model": "gemini-3.8-flash"}
    provider = type(
        "Provider",
        (),
        {
            "model": "gemini-3.8-flash",
            "request_identity": lambda self: {"paper_id": "10.1/x"},
        },
    )()
    prompt = Path("config/gemini-eligibility-prompt-v8.txt")
    schema = Path("schemas/gemini-eligibility.v4.schema.json")
    policy = Path("config/arctic-eligibility-policy-v3.json")
    initial = streaming._eligibility_job_key(
        access, config, prompt, schema, policy, provider
    )
    repaired = streaming._eligibility_job_key(
        access,
        config,
        prompt,
        schema,
        policy,
        provider,
        attempt_note={"attempt": 2, "format_errors": ["x"]},
    )
    rescreened = streaming._eligibility_job_key(
        access,
        config,
        Path("config/gemini-eligibility-geography-rescreen-v2.txt"),
        schema,
        policy,
        provider,
        attempt_note={"kind": "geography_rescreen"},
    )
    assert len({initial, repaired, rescreened}) == 3
    assert (
        streaming._eligibility_job_key(access, config, prompt, schema, policy, provider)
        == initial
    )


def test_the_loader_takes_the_last_attempt_of_one_candidate(tmp_path: Path) -> None:
    """A resumed run must read the re-screened answer, not the first screening."""
    jobs = tmp_path / "jobs"
    jobs.mkdir(parents=True)
    for name, kind, attempt in (
        ("a", "initial", 1),
        ("b", "format_repair", 2),
        ("c", "geography_rescreen", 1),
    ):
        (jobs / f"{name}.json").write_text(
            __import__("json").dumps(
                _job(name, valid=True, kind=kind, attempt=attempt)
            ),
            encoding="utf-8",
        )

    loaded = streaming._load_eligibility_jobs(
        tmp_path, prompt_file=None, schema_file=None, policy_file=None
    )

    assert loaded["10.1/x"]["job_key"] == "c"


def test_the_loader_still_refuses_two_jobs_of_the_same_attempt(
    tmp_path: Path,
) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir(parents=True)
    for name in ("a", "b"):
        (jobs / f"{name}.json").write_text(
            __import__("json").dumps(_job(name, valid=True)), encoding="utf-8"
        )

    try:
        streaming._load_eligibility_jobs(
            tmp_path, prompt_file=None, schema_file=None, policy_file=None
        )
    except ValueError as error:
        assert "more than one eligibility job" in str(error)
    else:
        raise AssertionError("two initial jobs for one candidate must be refused")


# ------------------------------------------------------ the routing registration


def test_every_new_eligibility_code_has_one_routing_entry() -> None:
    """The routing layer owns one entry per reason code the stage can emit."""
    new_codes = {
        "eligible_arctic_scope_dimension_unsupported",
        "eligible_arctic_scope_phrase_not_specific",
    }
    assert new_codes <= streaming.ELIGIBILITY_CONTRACT_REASONS
    # A wrong dimension label is no longer an error at all. It filters one span
    # and is recorded as that span's reason, so it can never be re-asked
    # (chapter 3 production run, the rule 5 correction of 2026-09-16).
    assert (
        "eligible_arctic_scope_dimension_unsupported"
        not in eligibility.FORMAT_ERROR_CODES
    )
    assert "eligible_arctic_scope_phrase_not_specific" in eligibility.FORMAT_ERROR_CODES
    # These codes end a screening attempt, so no candidate-level rung may claim
    # them. Routing must never spend a revision on a paper that has no candidate.
    for reasons in (
        streaming.REPAIRABLE_QUESTION_REASONS,
        streaming.ALTERNATIVE_FINDING_REASONS,
        streaming.IMMEDIATE_ALTERNATIVE_FINDING_REASONS,
        streaming.OPTION_REPAIR_REASONS,
        streaming.SURGICAL_CORRECTION_REASONS,
    ):
        assert not new_codes & reasons
