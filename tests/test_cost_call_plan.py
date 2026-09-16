"""Chapter 2 yield audit, sections 4.4 and 4.9: the judge call plan.

The free checks run right after the writer, the standalone call is kept for
every candidate, the two later Pro calls are skipped after a failure, every
Pro call is skipped on a writer-declared unavailable slot, and a seeded
shadow cohort still runs the full suite. Every role receives the evidence
once, and the static instructions ride in the system instruction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from arctic_qa import generation, model_roles, streaming  # noqa: E402
from arctic_qa.errors import CandidateRejectedError  # noqa: E402
from arctic_qa.providers import provider_prompt_hash  # noqa: E402
from cost_plan_harness import Seeded, fixture_events  # noqa: E402


def _author_events(**writer_overrides: object) -> list[dict]:
    events = fixture_events("fake-author.jsonl")
    writer = events[1]
    writer["response"].update(writer_overrides)
    return events


def _verifier_events(*, leaking_question: bool = False) -> list[dict]:
    events = fixture_events("fake-verifier.jsonl")
    if leaking_question:
        # The scripted standalone judge forbids the answer in its prompt; a
        # leak test displays it on purpose.
        events[0]["forbid_prompt_contains"] = [
            marker
            for marker in events[0]["forbid_prompt_contains"]
            if marker != "2.0 m"
        ]
    return events


def _unavailable(events: list[dict], slot: str) -> list[dict]:
    for row in events[1]["response"]["referent_slots"]:
        if row["slot"] == slot:
            row["state"] = "unavailable_in_source"
    return events


# Skip rules.


def test_a_clean_candidate_runs_the_full_suite(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    candidate = seeded.generate("clean", _author_events(), _verifier_events())

    assert candidate["status"] == "candidate"
    assert seeded.call_roles("clean") == [
        "extractor",
        "question_writer",
        "standalone_verifier",
        "reconstructor",
        "answer_verifier",
        "distractor_writer",
        *["option_verifier"] * 4,
    ]
    plan = candidate["provenance"]["judge_call_plan"]
    assert plan["contract_version"] == "judge-call-plan-v1"
    assert plan["skip_reason"] is None
    assert plan["skipped_calls"] == []
    assert plan["pre_judge_gate_reasons"] == []
    assert plan["shadow_gate_reasons"] is None


def test_a_free_check_failure_keeps_the_standalone_call_and_skips_the_rest(
    tmp_path: Path,
) -> None:
    seeded = Seeded(tmp_path)
    leaking = _author_events(
        question="What reported water depth was documented as 2.0 m?"
    )
    candidate = seeded.generate(
        "free-fail", leaking, _verifier_events(leaking_question=True)
    )

    assert candidate["status"] == "qa_gate_failed"
    assert seeded.call_roles("free-fail") == [
        "extractor",
        "question_writer",
        "standalone_verifier",
    ]
    assert candidate["qa_gate_reasons"][0] == "question_answer_leakage"
    assert candidate["reconstruction"] is None
    assert candidate["answer_verification"] is None
    assert candidate["answer_agreement"] is None
    assert candidate["standalone_verification"]["pass"] is True
    plan = candidate["provenance"]["judge_call_plan"]
    assert plan["skip_reason"] == "skipped_after_free_check_failure"
    assert plan["skipped_calls"] == ["reconstructor", "answer_verifier"]
    assert plan["pre_judge_gate_reasons"] == ["question_answer_leakage"]
    assert "standalone_verifier" in candidate["provenance"]["verification_calls"]
    assert "reconstructor" not in candidate["provenance"]["verification_calls"]
    # A skipped judge never contributes a code: the persisted list carries
    # only the codes that routing must read.
    assert not any(
        reason.startswith(("reconstruction_", "answer_verifier_"))
        or reason in {"source_entailment_not_verified", "relation_scope_mismatch"}
        for reason in candidate["qa_gate_reasons"]
    )


def test_a_standalone_failure_skips_reconstruction_and_verification(
    tmp_path: Path,
) -> None:
    seeded = Seeded(tmp_path)
    failing = _verifier_events()
    failing[0]["response"].update(
        {
            "pass": False,
            "reasons": ["undefined_location"],
            "missing_detail_types": ["location"],
            "unresolved_phrases": ["the study site"],
            "review_rationale": "The displayed task names no place.",
        }
    )
    candidate = seeded.generate("standalone-fail", _author_events(), failing)

    assert candidate["status"] == "qa_gate_failed"
    assert seeded.call_roles("standalone-fail") == [
        "extractor",
        "question_writer",
        "standalone_verifier",
    ]
    assert "standalone_undefined_location" in candidate["qa_gate_reasons"]
    assert candidate["reconstruction"] is None
    plan = candidate["provenance"]["judge_call_plan"]
    assert plan["skip_reason"] == "skipped_after_standalone_failure"
    assert plan["pre_judge_gate_reasons"] == []


def test_an_unavailable_slot_skips_every_pro_call(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    candidate = seeded.generate(
        "slot", _unavailable(_author_events(), "location"), _verifier_events()
    )

    assert candidate["status"] == "qa_gate_failed"
    assert seeded.call_roles("slot") == ["extractor", "question_writer"]
    assert candidate["qa_gate_reasons"][0] == "writer_slot_unavailable_location"
    assert candidate["standalone_verification"] is None
    assert candidate["reconstruction"] is None
    assert candidate["question_claim_type"] is None
    assert candidate["provenance"]["direct_value_request_id"] is None
    plan = candidate["provenance"]["judge_call_plan"]
    assert plan["skip_reason"] == "skipped_on_unavailable_slot"
    assert plan["unavailable_slots"] == ["location"]
    assert plan["skipped_calls"] == [
        "standalone_verifier",
        "reconstructor",
        "answer_verifier",
    ]


def test_the_writer_slot_codes_are_registered_for_routing() -> None:
    codes = generation.WRITER_SLOT_UNAVAILABLE_REASONS
    assert len(codes) == len(generation.REFERENT_SLOT_NAMES)
    assert codes <= streaming.REPAIRABLE_QUESTION_REASONS
    assert streaming._SLOT_REASON_TYPES["writer_slot_unavailable_location"] == "place"
    assert streaming._failure_layer("writer_slot_unavailable_period_or_event") == (
        "context"
    )
    assert streaming._reason_family("writer_slot_unavailable_acronym") == (
        "referent_slot"
    )
    assert streaming._failure_layer("no_admissible_finding") == "finding"
    assert "no_admissible_finding" in streaming.IMMEDIATE_ALTERNATIVE_FINDING_REASONS
    assert streaming._failure_layer("finding_context_over_budget") == "contract"


def test_the_shadow_cohort_runs_the_skipped_calls_and_keeps_the_routing_input(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(generation, "_in_shadow_cohort", lambda run_id, entity: True)
    seeded = Seeded(tmp_path)
    leaking = _author_events(
        question="What reported water depth was documented as 2.0 m?"
    )
    candidate = seeded.generate(
        "shadow", leaking, _verifier_events(leaking_question=True)
    )

    assert seeded.call_roles("shadow") == [
        "extractor",
        "question_writer",
        "standalone_verifier",
        "reconstructor",
        "answer_verifier",
    ]
    assert candidate["status"] == "qa_gate_failed"
    assert candidate["reconstruction"] is not None
    assert candidate["answer_verification"] is not None
    plan = candidate["provenance"]["judge_call_plan"]
    assert plan["shadow_cohort"] is True
    assert plan["skip_reason"] == "skipped_after_free_check_failure"
    assert plan["calls_made_for_shadow"] == ["reconstructor", "answer_verifier"]
    # The persisted list is the short-circuit list, so routing reads the same
    # input for a cohort member and for every other candidate.
    assert candidate["qa_gate_reasons"] == ["question_answer_leakage"]
    assert isinstance(plan["shadow_gate_reasons"], list)
    assert "question_answer_leakage" in plan["shadow_gate_reasons"]


def test_the_shadow_cohort_selection_is_seeded_and_near_five_percent() -> None:
    members = [
        entity
        for entity in (f"unit-{index}" for index in range(20000))
        if generation._in_shadow_cohort("run-a", entity)
    ]
    rate = len(members) / 20000
    assert 0.045 <= rate <= 0.055
    assert all(generation._in_shadow_cohort("run-a", entity) for entity in members)
    other = {
        entity
        for entity in (f"unit-{index}" for index in range(20000))
        if generation._in_shadow_cohort("run-b", entity)
    }
    assert other != set(members)


def test_the_pre_judge_subset_emits_only_free_codes() -> None:
    chunk = {
        "chunk_id": "chunk-1",
        "section_id": "s",
        "heading": "Results",
        "text": "Nitrate declined by 15 percent at the Arctic station in 2019.",
    }
    quote = "Nitrate declined by 15 percent at the Arctic station in 2019."
    answer = {
        "text": "15 percent",
        "evidence_quote": quote,
        "locator": {"chunk_id": "chunk-1", "start_offset": 0, "end_offset": len(quote)},
        "scope": {"geography": "Arctic station", "period": "2019"},
        "required_question_phrases": ["Arctic station"],
        "claim_type": "observation",
        "numeric_rule": {"canonical_value": "15", "unit": "percent", "tolerance": "0"},
    }
    reasons = generation._pre_judge_gate_reasons(
        chunk, "By how much did nitrate decline?", answer, ""
    )
    # The displayed text omits the required phrase and the displayed scope.
    assert "scope_qualifier_missing" in reasons
    assert "scope_qualifier_not_displayed" in reasons
    # Judge-only codes never come from the free pass, including the numeric
    # binding that reads the verifier's request id.
    assert "source_bound_numeric_rule_missing" not in reasons
    assert not any(
        reason.startswith(("reconstruction_", "answer_verifier_", "standalone_"))
        for reason in reasons
    )
    full = generation._qa_gate_reasons(
        chunk,
        "By how much did nitrate decline?",
        answer,
        {},
        {},
        "",
        None,
        standalone_verification={"pass": True, "reasons": []},
    )
    assert set(reasons) <= set(full)


# Payloads and roles.


def test_the_evidence_is_emitted_once_with_the_same_spans_and_hashes() -> None:
    chunk = {
        "chunk_id": "chunk-1",
        "section_id": "s",
        "heading": "Results",
        "page": 1,
        "text": ("A first sentence. " * 200).strip(),
    }
    spans = generation._finding_spans(chunk)
    payload = json.loads(
        generation._context(chunk)
        .removeprefix("SOURCE_DATA_BEGIN\n")
        .removesuffix("\nSOURCE_DATA_END")
    )
    assert "text" not in payload
    assert [
        (span["span_id"], span["text"], span["text_sha256"])
        for span in payload["evidence_spans"]
    ] == [(span["span_id"], span["text"], span["text_sha256"]) for span in spans]
    # The overlapping tiles carry every byte of the chunk at least once.
    assert all(
        chunk["text"][span["start_offset"] : span["end_offset"]] == span["text"]
        for span in payload["evidence_spans"]
    )
    assert min(span["start_offset"] for span in spans) == 0
    assert max(span["end_offset"] for span in spans) == len(chunk["text"])
    finding_context, resolver = generation._finding_context([chunk])
    rendered = json.loads(
        finding_context.removeprefix("SOURCE_DATA_BEGIN\n").removesuffix(
            "\nSOURCE_DATA_END"
        )
    )
    assert all("text" not in row for row in rendered["chunks"])
    assert set(resolver) == {span["span_id"] for span in spans}


def test_static_instructions_ride_in_the_system_instruction_once() -> None:
    duplicated = "alternatives, evidence, and the question claim type. "
    assert generation.ANSWER_VERIFIER_SYSTEM.count(duplicated) == 1
    for system in (
        generation.EXTRACTOR_SYSTEM,
        generation.RECONSTRUCTOR_SYSTEM,
        generation.ANSWER_VERIFIER_SYSTEM,
        generation.OPTION_VERIFIER_SYSTEM,
    ):
        assert system.startswith(generation.SYSTEM + "\n")
        assert generation.BENCHMARK_STANDALONE_INSTRUCTIONS[:40] in system or (
            system is generation.EXTRACTOR_SYSTEM
        )
    assert "ranked best first" in generation.EXTRACTOR_SYSTEM
    assert "study_internal_index" in generation.EXTRACTOR_SYSTEM


def test_the_prompt_hash_binds_the_role_system_instruction(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    candidate = seeded.generate("hash", _author_events(), _verifier_events())
    call = candidate["provenance"]["verification_calls"]["reconstructor"]
    row = seeded.db.one(
        "SELECT prompt_hash,parameters_json FROM calls WHERE run_id=? AND role=?",
        ("hash", "reconstructor"),
    )
    assert row["prompt_hash"] == call["prompt_hash"]
    verifier = SimpleNamespace(name="fake", model="gemini-3.1-pro-preview")
    stored = seeded.db.rows(
        "SELECT prompt_hash FROM calls WHERE run_id=? AND role='answer_verifier'",
        ("hash",),
    )
    assert stored
    # The user prompt of every judge holds no static instruction sentence.
    assert generation.RECONSTRUCTOR_INSTRUCTIONS[:60] not in json.dumps(
        candidate["provenance"]["verification_calls"]
    )
    assert provider_prompt_hash(
        verifier, generation.SYSTEM, "x", "v", {}, role="reconstructor"
    ) != provider_prompt_hash(
        verifier, generation.RECONSTRUCTOR_SYSTEM, "x", "v", {}, role="reconstructor"
    )


def test_the_finding_context_budget_rejects_before_any_paid_call(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(generation, "MAX_FINDING_CONTEXT_CHARS", 10)
    seeded = Seeded(tmp_path)
    with pytest.raises(CandidateRejectedError) as error:
        seeded.generate("budget", _author_events(), _verifier_events())
    assert error.value.reason_code == "finding_context_over_budget"
    assert seeded.call_roles("budget") == []


def test_the_budget_is_measured_from_the_chapter_2_payloads() -> None:
    # The largest chapter 2 extractor payload was 393,094 characters with the
    # evidence twice and 257,607 characters once; the budget keeps headroom.
    assert 257_607 < generation.MAX_FINDING_CONTEXT_CHARS < 1_000_000


def test_the_cost_aware_profile_is_refused_for_a_production_run() -> None:
    model_roles.assert_profile_allowed_for_phase("cost_aware", "offline")
    model_roles.assert_profile_allowed_for_phase("gemini_separated", "away_production")
    with pytest.raises(ValueError, match="not allowed for a production run"):
        model_roles.assert_profile_allowed_for_phase("cost_aware", "away_production")
    author = SimpleNamespace(
        name="gemini", model="gemini-3.8-flash", phase="away_production"
    )
    verifier = SimpleNamespace(name="gemini", model="gemini-3.1-pro-preview")
    with pytest.raises(ValueError, match="not allowed for a production run"):
        streaming._resolve_model_roles(
            author=author,
            verifier=verifier,
            roles_file=REPO / "config" / "roles.v1.json",
            role_profile="cost_aware",
        )


# Option verifier call plan.


def _distractor_events(count: int) -> tuple[list[dict], list[dict]]:
    author = fixture_events("fake-author.jsonl")
    template = author[2]["response"]["distractors"][0]
    author[2]["response"]["distractors"] = [
        {
            **template,
            "text": f"{value} m",
            "numeric": {"canonical_value": str(value), "unit": "m"},
        }
        for value in ("2.5", "3.0", "4.0", "5.0", "6.0", "7.0", "8.0")[:count]
    ]
    verifier = fixture_events("fake-verifier.jsonl")
    option_template = verifier[3]
    verifier = verifier[:3] + [
        {
            **json.loads(json.dumps(option_template)),
            "require_prompt_contains": [
                marker
                for marker in option_template["require_prompt_contains"]
                if marker != "2.5 m"
            ]
            + [f"{value} m"],
        }
        for value in ("2.5", "3.0", "4.0", "5.0", "6.0", "7.0", "8.0")[:count]
    ]
    return author, verifier


def test_option_verification_stops_at_the_export_need_and_keeps_a_reserve(
    tmp_path: Path,
) -> None:
    author, verifier = _distractor_events(6)
    seeded = Seeded(tmp_path)
    candidate = seeded.generate("options", author, verifier)

    assert seeded.call_roles("options").count("option_verifier") == 4
    assert [row["text"] for row in candidate["distractors"]] == [
        "2.5 m",
        "3.0 m",
        "4.0 m",
        "5.0 m",
    ]
    plan = candidate["provenance"]["option_verification_call_plan"]
    assert plan["contract_version"] == "rank-order-option-verification-v1"
    assert plan["verified_target"] == generation.OPTION_VERIFIED_TARGET == 4
    assert plan["proposed"] == 6
    assert len(plan["verified_option_hashes"]) == 4
    assert [row["option_text"] for row in plan["reserve"]] == ["6.0 m", "7.0 m"]


def test_a_failed_verdict_does_not_count_toward_the_target(tmp_path: Path) -> None:
    author, verifier = _distractor_events(6)
    verifier[3]["response"]["contradiction_established"] = False
    seeded = Seeded(tmp_path)
    candidate = seeded.generate("options-fail", author, verifier)

    assert seeded.call_roles("options-fail").count("option_verifier") == 5
    plan = candidate["provenance"]["option_verification_call_plan"]
    assert len(plan["verified_option_hashes"]) == 4
    assert [row["option_text"] for row in plan["reserve"]] == ["7.0 m"]
    assert len(candidate["option_verdicts"]) == 5


def test_free_option_checks_run_before_any_paid_call(tmp_path: Path) -> None:
    author, verifier = _distractor_events(5)
    author[2]["response"]["distractors"][1]["text"] = "None of the above"
    author[2]["response"]["distractors"][1].pop("numeric", None)
    seeded = Seeded(tmp_path)
    candidate = seeded.generate("options-free", author, verifier[:4] + verifier[5:])

    assert seeded.call_roles("options-free").count("option_verifier") == 4
    prefiltered = candidate["provenance"]["option_display_prefilter"]
    assert {
        "option_text": "None of the above",
        "reason_code": "forbidden_meta_option",
    } in (prefiltered)
    assert "None of the above" not in [row["text"] for row in candidate["distractors"]]


def test_the_verified_predicate_mirrors_the_validator_preconditions() -> None:
    verdict = {
        "contradiction_established": True,
        "alternative_answer_search_passed": True,
        "question_admits_option_as_correct": False,
    }
    atomic = {"text": "5.0 m"}
    compound = {"text": "5.0 m and 6.0 m"}
    assert generation._option_verdict_verified(atomic, verdict, independent=False)
    assert not generation._option_verdict_verified(compound, verdict, independent=False)
    assert generation._option_verdict_verified(compound, verdict, independent=True)
    assert not generation._option_verdict_verified(
        atomic, {**verdict, "question_admits_option_as_correct": True}, independent=True
    )
