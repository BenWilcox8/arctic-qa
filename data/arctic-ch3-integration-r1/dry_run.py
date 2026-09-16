"""Mocked end-to-end dry run of the integrated chapter 3 call plan.

Every provider is the scripted fake provider. No paid call is made. The run
drives ``generate_candidate`` over five candidate families and the streaming
eligibility pass over two papers, and records what the integrated plan did:
the pre-judge free checks, the standalone call on every candidate, the Pro
call skips, the shadow cohort, the option call plan and the re-screen path.

Usage (from the repository root, inside ``nix develop``):
    PYTHONPATH=src:tests python data/arctic-ch3-integration-r1/dry_run.py <out.json>

The event builders of two test modules are inlined here, so the script needs
no pytest import.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import arctic_qa.generation as generation
import arctic_qa.gemini_eligibility as eligibility
import arctic_qa.streaming as streaming
from cost_plan_harness import Seeded, fixture_events

OUT = Path(sys.argv[1])
SATISFIED = (
    "published_primary_findings",
    "stable_identity_version",
    "access_rights_evidence",
)


class _Scripts:
    """The event builders of tests/test_cost_call_plan.py, inlined."""

    @staticmethod
    def _author_events(**writer_overrides: object) -> list[dict]:
        events = fixture_events("fake-author.jsonl")
        events[1]["response"].update(writer_overrides)
        return events

    @staticmethod
    def _verifier_events(*, leaking_question: bool = False) -> list[dict]:
        events = fixture_events("fake-verifier.jsonl")
        if leaking_question:
            events[0]["forbid_prompt_contains"] = [
                marker
                for marker in events[0]["forbid_prompt_contains"]
                if marker != "2.0 m"
            ]
        return events

    @staticmethod
    def _unavailable(events: list[dict], slot: str) -> list[dict]:
        for row in events[1]["response"]["referent_slots"]:
            if row["slot"] == slot:
                row["state"] = "unavailable_in_source"
        return events


cost_tests = _Scripts


class _Eligibility:
    """The helpers of tests/test_eligibility_streaming_recovery.py, inlined."""

    class _Recorder:
        def __init__(self, answers: list[dict]) -> None:
            self.answers = answers
            self.calls: list[dict] = []

        def __call__(self, db, access, run_dir, **kwargs):
            self.calls.append(kwargs)
            return self.answers[len(self.calls) - 1]

    @staticmethod
    def _parsed(
        *, geography: str = "satisfied", phrases: list[str] | None = None
    ) -> dict:
        statuses = {name: "satisfied" for name in SATISFIED}
        statuses["correction_retraction_coverage"] = "uncertain"
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

    @classmethod
    def _job(
        cls,
        key: str,
        *,
        valid: bool,
        errors: list[str] | None = None,
        kind: str = "initial",
        attempt: int = 1,
        parsed: dict | None = None,
    ) -> dict:
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
            "parsed_response": parsed if parsed is not None else cls._parsed(),
            "validation": {
                "valid": valid,
                "errors": list(errors or []),
                "decision": "eligible" if valid else "uncertain",
            },
        }

    @staticmethod
    def _run(root: Path, recorder, *, rescreen: bool = True):
        extraction = root / "extraction.txt"
        extraction.write_text("Sampled at 78.9 N in March 2012.\n", encoding="utf-8")
        access = {"candidate_key": "10.1/x", "extraction_path": str(extraction)}
        provider = type("Provider", (), {"broker": object()})()
        return streaming._run_eligibility(
            None,
            access,
            root / "run",
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


eligibility_tests = _Eligibility


def _plan(candidate: dict) -> dict:
    provenance = candidate.get("provenance") or {}
    plan = provenance.get("judge_call_plan") or {}
    option_plan = provenance.get("option_verification_call_plan")
    return {
        "status": candidate.get("status"),
        "qa_gate_reasons": candidate.get("qa_gate_reasons"),
        "pre_judge_gate_reasons": plan.get("pre_judge_gate_reasons"),
        "skip_reason": plan.get("skip_reason"),
        "skipped_calls": plan.get("skipped_calls"),
        "shadow_cohort": plan.get("shadow_cohort"),
        "calls_made_for_shadow": plan.get("calls_made_for_shadow"),
        "shadow_gate_reasons": plan.get("shadow_gate_reasons"),
        "standalone_pass": (candidate.get("standalone_verification") or {}).get("pass"),
        "standalone_reask": provenance.get("standalone_verification_reask"),
        "reconstruction_called": candidate.get("reconstruction") is not None,
        "answer_verification_called": candidate.get("answer_verification") is not None,
        "option_verification_call_plan": option_plan,
        "option_verification_deferred": provenance.get("option_verification_deferred"),
        "option_set_verdict": (
            {
                key: (candidate.get("option_set_verdict") or {}).get(key)
                for key in (
                    "options_mutually_exclusive",
                    "answer_choosable_from_displayed_text",
                )
            }
            if candidate.get("option_set_verdict")
            else None
        ),
        "distractors": [row.get("text") for row in candidate.get("distractors") or []],
    }


def _family(
    name: str, author: list[dict], verifier: list[dict], *, shadow: bool = False
) -> dict:
    original = generation._in_shadow_cohort
    if shadow:
        generation._in_shadow_cohort = lambda run_id, entity: True
    else:
        generation._in_shadow_cohort = lambda run_id, entity: False
    try:
        with tempfile.TemporaryDirectory() as root:
            seeded = Seeded(Path(root))
            candidate = seeded.generate(name, author, verifier)
            return {
                "family": name,
                "call_roles": seeded.call_roles(name),
                **_plan(candidate),
            }
    finally:
        generation._in_shadow_cohort = original


def _standalone_failing_verifier() -> list[dict]:
    events = cost_tests._verifier_events()
    events[0]["response"].update(
        {
            "pass": False,
            "reasons": ["undefined_location"],
            "missing_detail_types": ["location"],
            "unresolved_phrases": ["the study site"],
            "review_rationale": "The displayed task names no place.",
        }
    )
    return events


def _unevidenced_then_evidenced_verifier() -> list[dict]:
    """A first verdict with a code and no phrase, re-asked once (audit 4.2 SG-3)."""
    events = cost_tests._verifier_events()
    first = json.loads(json.dumps(events[0]))
    first["response"].update(
        {
            "pass": False,
            "reasons": ["undefined_location"],
            "missing_detail_types": ["location"],
            "unresolved_phrases": [],
            "review_rationale": "No place is named.",
        }
    )
    second = json.loads(json.dumps(events[0]))
    second["require_prompt_contains"] = list(second["require_prompt_contains"]) + [
        "CONTRACT_VIOLATION"
    ]
    events[0:1] = [first, second]
    return events


def _eligibility(name: str, answers: list[dict], *, rescreen: bool = True) -> dict:
    recorder = eligibility_tests._Recorder(answers)
    original = streaming._eligibility_attempt
    streaming._eligibility_attempt = recorder
    try:
        with tempfile.TemporaryDirectory() as root:
            job = eligibility_tests._run(Path(root), recorder, rescreen=rescreen)
    finally:
        streaming._eligibility_attempt = original
    return {
        "paper": name,
        "attempt_kinds": [call["kind"] for call in recorder.calls],
        "attempt_notes": [
            {
                key: value
                for key, value in (call.get("attempt_note") or {}).items()
                if key in {"kind", "format_errors", "frozen_criterion_statuses"}
            }
            for call in recorder.calls
        ],
        "state": job.get("state"),
        "decision": (job.get("validation") or {}).get("decision"),
        "geography_rescreen": job.get("geography_rescreen"),
        "rescreened_from": job.get("rescreened_from"),
    }


families = [
    _family(
        "clean-full-suite", cost_tests._author_events(), cost_tests._verifier_events()
    ),
    _family(
        "free-check-failure",
        cost_tests._author_events(
            question="What reported water depth was documented as 2.0 m?"
        ),
        cost_tests._verifier_events(leaking_question=True),
    ),
    _family(
        "standalone-failure",
        cost_tests._author_events(),
        _standalone_failing_verifier(),
    ),
    _family(
        "standalone-unevidenced-reask",
        cost_tests._author_events(),
        _unevidenced_then_evidenced_verifier(),
    ),
    _family(
        "unavailable-slot",
        cost_tests._unavailable(cost_tests._author_events(), "location"),
        cost_tests._verifier_events(),
    ),
    _family(
        "shadow-cohort-member",
        cost_tests._author_events(
            question="What reported water depth was documented as 2.0 m?"
        ),
        cost_tests._verifier_events(leaking_question=True),
        shadow=True,
    ),
]
papers = [
    _eligibility(
        "geography-rescreen",
        [
            eligibility_tests._job(
                "a", valid=True, parsed=eligibility_tests._parsed(geography="uncertain")
            ),
            eligibility_tests._job("b", valid=True, kind="geography_rescreen"),
        ],
    ),
    _eligibility(
        "format-reask",
        [
            eligibility_tests._job(
                "a", valid=False, errors=["eligible_arctic_scope_phrase_unbound"]
            ),
            eligibility_tests._job("b", valid=True, kind="format_repair", attempt=2),
        ],
        rescreen=False,
    ),
    _eligibility(
        "failed-geography-never-rescreened",
        [
            {
                **eligibility_tests._job(
                    "a",
                    valid=True,
                    parsed=eligibility_tests._parsed(geography="failed"),
                ),
                "validation": {"valid": True, "errors": [], "decision": "excluded"},
            }
        ],
    ),
]
report = {
    "schema": "arctic-ch3-integration-dry-run-v1",
    "note": "Scripted fake provider on the public fixture source; no paid call.",
    "judge_call_plan_contract": generation.JUDGE_CALL_PLAN_CONTRACT_VERSION,
    "option_call_plan_contract": generation.OPTION_VERIFICATION_CALL_PLAN_VERSION,
    "option_proposal_count": generation.OPTION_PROPOSAL_COUNT,
    "option_verified_target": generation.OPTION_VERIFIED_TARGET,
    "families": families,
    "eligibility": papers,
}
OUT.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
for row in families:
    print(row["family"], row["call_roles"], row["skip_reason"], row["shadow_cohort"])
for row in papers:
    print(
        row["paper"],
        row["attempt_kinds"],
        row["decision"],
        row.get("geography_rescreen"),
    )
