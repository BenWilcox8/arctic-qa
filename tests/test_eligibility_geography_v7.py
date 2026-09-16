"""Eligibility prompt v7, phrase binding, the bounded repair and the re-screen.

Every test pins a defect the r15 holistic acceptance audit named: section 4.7 and
the eligibility stage findings E3 and E4.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import arctic_qa.gemini_eligibility as eligibility
from arctic_qa.util import canonical_json


ROOT = Path(__file__).parents[1]
PROMPT = (ROOT / "config" / "gemini-eligibility-prompt-v7.txt").read_text(
    encoding="utf-8"
)
RESCREEN_PROMPT = (
    ROOT / "config" / "gemini-eligibility-geography-rescreen-v1.txt"
).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_v7_states_the_ordered_geography_procedure() -> None:
    """Audit 4.7: the prompt never restated the policy failure rule."""
    flat = _flat(PROMPT)
    assert "GEOGRAPHY DECISION PROCEDURE" in flat
    assert "Stop at the first rule that applies" in flat
    assert (
        "fail geography only when all actual study activity is outside the "
        "boundary or the Arctic reference is incidental" in flat
    )
    assert "Absence of evidence is uncertain. Absence of evidence is never failed" in (
        flat
    )
    assert "modeled, simulated, reanalysis, or remote-sensing domain is study" in flat
    for rule in ("1.", "2.", "3.", "4.", "5."):
        assert rule in PROMPT


def test_v7_keeps_the_rules_that_exclude_a_non_arctic_paper() -> None:
    """Rigor: 36 correct exclusions must survive every change."""
    flat = _flat(PROMPT)
    assert (
        "Do not use a title, affiliation, background statement, or citation as "
        "Arctic scope" in flat
    )
    assert "A title can tell you where to look for the activity evidence" in flat
    assert "It cannot stand in for that evidence" in flat
    assert (
        "Span location records provenance only. It does not prove that the "
        "selected text entails the criterion status" in flat
    )
    assert "Use eligible_arctic_scope only for actual study activity" in flat
    assert "Never treat a missing source, access error, or model error" in flat


def test_v7_states_the_separable_component_rule() -> None:
    """Audit 4.7: results on both sides of the boundary are always separable."""
    flat = _flat(PROMPT)
    assert (
        "When the spans show results both inside and outside the boundary, set "
        "separable_arctic_component, never whole_study" in flat
    )
    assert (
        "Use whole_study only when all study activity and results are within "
        "the Arctic boundary" in flat
    )


def test_v7_replaces_the_byte_exact_phrase_demand() -> None:
    """Audit E3: v6 demanded an exact substring of a line fragment."""
    flat = _flat(PROMPT)
    assert "exact substring of the selected finding_span_ids text" not in flat
    assert (
        "Each phrase is checked after whitespace is collapsed, so do not repeat "
        "the line breaks or the spacing of the span" in flat
    )
    assert "When a line break splits a word with a hyphen, write the whole word" in flat
    assert (
        "Do not use a numeric result value, a percentage, a bare unit, or a "
        'vague label such as "In the Arctic" as a phrase' in flat
    )
    assert "Do not use a phrase from a title, activity span, or unselected" in flat


def test_v7_asks_for_sentence_complete_result_spans() -> None:
    """Audit E5: a window that ended mid-clause truncated the writer's source."""
    flat = _flat(PROMPT)
    assert "Select finding spans that are complete sentences" in flat
    assert "Do not end a selection on a partial clause" in flat
    assert "do not build it from a figure or table caption alone" in flat


def test_the_rescreen_prompt_asks_one_criterion_and_keeps_the_rest() -> None:
    flat = _flat(RESCREEN_PROMPT)
    assert "You decide one criterion only: study_geography" in flat
    assert "Every other criterion keeps the status of the first screening" in flat
    assert "Absence of evidence is never failed" in flat
    assert "name the span that places the study activity" in flat


def test_binding_folds_only_presentation_damage() -> None:
    """Audit E3: soft hyphens, ligatures and line wraps blocked every match."""
    damaged = "sampled at Ny-Ålesund (Sval-\nbard Islands) with  runs   of spaces"
    assert eligibility._normalize_for_binding(damaged) == (
        "sampled at Ny-Ålesund (Svalbard Islands) with runs of spaces"
    )
    assert eligibility._normalize_for_binding(
        "South Baf" + chr(0xFB01) + "n Island"
    ) == ("South Baffin Island")


def _scope_case(phrases: list[str]) -> dict[str, object]:
    text = (
        "Methods: We sampled Arctic PM10 at Ny-Ålesund (Sval-\n"
        "bard Islands) at 78.9 N during 2012.\n"
        "Results: At Ny-Ålesund the oxalate share reached 15 percent.\n"
        "Results: At 54.0 N the southern station reached 40 percent.\n"
        "DOI 10.9999/scope-binding identifies this version.\n"
        "This article uses the CC BY 4.0 license.\n"
    )
    extraction = sha256(text.encode()).hexdigest()
    blocks = eligibility._span_blocks_v2(text, extraction)
    spans = [span for block in blocks for span in block["spans"]]
    selected = {
        "published_primary_findings": [spans[2]["span_id"]],
        "stable_identity_version": [spans[4]["span_id"]],
        "study_geography": [spans[0]["span_id"], spans[1]["span_id"]],
        "access_rights_evidence": [spans[5]["span_id"]],
    }
    manifest = eligibility._span_manifest_v2(
        blocks, eligibility.ELIGIBILITY_RESPONSE_V3
    )
    hashes = {
        "policy_sha256": "a" * 64,
        "source_version_sha256": "b" * 64,
        "extracted_text_sha256": extraction,
        "metadata_sha256": "c" * 64,
        "span_manifest_sha256": sha256(canonical_json(manifest).encode()).hexdigest(),
    }
    criteria = []
    for criterion in eligibility.CRITERIA:
        status = (
            "uncertain"
            if criterion == "correction_retraction_coverage"
            else "satisfied"
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
    response = {
        "schema_version": "eligibility-response-v3",
        "status_mapping_version": "eligibility-criterion-status-map-v1",
        "request_id": "scope-binding",
        "criteria": criteria,
        "eligible_arctic_scope": {
            "component": "separable_arctic_component",
            "activity_span_ids": selected["study_geography"],
            "finding_span_ids": selected["published_primary_findings"],
            "question_scope_phrases": phrases,
        },
        "known_missing_context": ["correction_retraction_coverage:unknown"],
        "correction_metadata_used": {
            "provided": False,
            "known_status": "unknown",
            "source": None,
            "as_of": None,
        },
        "input_echo": hashes,
    }
    schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v3.schema.json").read_text()
    )
    return eligibility.validate_response(
        response,
        blocks,
        expected={
            "request_id": "scope-binding",
            "input_echo": hashes,
            "correction_metadata": response["correction_metadata_used"],
            "known_context_gaps": ["correction_retraction_coverage:unknown"],
        },
        response_schema=schema,
    )


def test_a_whole_word_phrase_binds_after_normalization() -> None:
    """Audit E3: a correct phrase could never bind against a line fragment."""
    result = _scope_case(["At Ny-Ålesund the oxalate share"])
    assert result["errors"] == []
    assert result["valid"] is True
    scope = result["resolved_eligible_arctic_scope"]
    assert scope["question_scope_phrases"] == ["At Ny-Ålesund the oxalate share"]
    assert scope["question_scope_phrases_source"] == ["At Ny-Ålesund the oxalate share"]


def test_a_phrase_broken_by_a_line_wrap_is_still_rejected() -> None:
    """Rigor: binding is not relaxed, it is compared through one projection."""
    result = _scope_case(["the oxalate share reached 15 percent in the southern"])
    assert "eligible_arctic_scope_phrase_unbound" in result["errors"]
    assert result["decision"] == "uncertain"


def test_a_formatting_mistake_is_repairable_and_an_envelope_error_is_not() -> None:
    """Audit E3d: one formatting mistake wrote a terminal row and halted the run."""
    assert eligibility.format_repairable(["eligible_arctic_scope_phrase_unbound"])
    assert eligibility.format_repairable(
        ["eligible_arctic_scope_activity_unbound", "eligible_arctic_scope_missing"]
    )
    assert not eligibility.format_repairable([])
    assert not eligibility.format_repairable(["refusal_block_or_malformed_response"])
    assert not eligibility.format_repairable(
        ["candidate_count_or_finish_reason_invalid"]
    )
    assert not eligibility.format_repairable(
        [
            "eligible_arctic_scope_phrase_unbound",
            "evidence_span_unknown:study_geography",
        ]
    )


def test_an_unresolved_paper_is_re_screenable_until_its_attempts_run_out(
    tmp_path: Path,
) -> None:
    """Audit E3d: the paper must not get a terminal screening_error row."""
    run = tmp_path / "run"
    record = {"job_key": "k", "candidate_key": "10.1/x", "parsed_response": None}
    validation = {"errors": ["eligible_arctic_scope_phrase_unbound"]}

    eligibility._record_unresolved(run, "k", record, validation)
    assert not list((run / "jobs").glob("*.json"))
    first = json.loads((run / "unresolved" / "k.json").read_text())
    assert first["state"] == "unresolved_rescreenable"
    assert first["attempts"] == 1
    assert "k" not in eligibility._terminal_index(run)

    eligibility._record_unresolved(run, "k", record, validation)
    second = json.loads((run / "unresolved" / "k.json").read_text())
    assert second["attempts"] == eligibility.MAXIMUM_FORMAT_ATTEMPTS
    assert eligibility._terminal_index(run)["k"] == "unresolved_rescreenable"


def test_the_bounded_repair_freezes_every_criterion_status() -> None:
    """Audit E3: a repair fixes the shape of an answer, never its science."""
    prior = {
        "attempts": 1,
        "format_errors": ["eligible_arctic_scope_phrase_unbound"],
        "parsed_response": {
            "criteria": [
                {"criterion_id": "study_geography", "status": "satisfied"},
                {"criterion_id": "access_rights_evidence", "status": "satisfied"},
            ]
        },
    }
    note = eligibility._repair_note(prior)
    assert note["attempt"] == 2
    assert note["frozen_criterion_statuses"]["study_geography"] == "satisfied"
    assert "Do not change any criterion status" in note["instruction"]

    unchanged = {
        "criteria": [
            {"criterion_id": "study_geography", "status": "satisfied"},
            {"criterion_id": "access_rights_evidence", "status": "satisfied"},
        ]
    }
    assert eligibility._repair_moved_a_status(prior, unchanged) is False

    moved = {
        "criteria": [
            {"criterion_id": "study_geography", "status": "failed"},
            {"criterion_id": "access_rights_evidence", "status": "satisfied"},
        ]
    }
    assert eligibility._repair_moved_a_status(prior, moved) is True
    assert eligibility._repair_moved_a_status(None, moved) is False


def _job(
    run: Path, key: str, decision: str, statuses: dict[str, str], state="completed"
) -> None:
    directory = run / ("jobs" if state == "completed" else "unresolved")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{key}.json").write_text(
        canonical_json(
            {
                "state": state,
                "candidate_key": key,
                "validation": {"decision": decision},
                "parsed_response": {
                    "criteria": [
                        {"criterion_id": name, "status": status}
                        for name, status in statuses.items()
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def test_the_geography_rescreen_takes_only_the_geography_unresolved_papers(
    tmp_path: Path,
) -> None:
    """Audit 4.7: uncertain is terminal today, so the bounded re-screen is paired."""
    run = tmp_path / "prior"
    satisfied = dict.fromkeys(eligibility.CRITERIA, "satisfied")
    _job(
        run,
        "only-geography",
        "uncertain",
        {**satisfied, "study_geography": ("uncertain")},
    )
    _job(
        run, "geography-failed", "excluded", {**satisfied, "study_geography": "failed"}
    )
    _job(
        run,
        "another-criterion",
        "uncertain",
        {
            **satisfied,
            "study_geography": "uncertain",
            "access_rights_evidence": "uncertain",
        },
    )
    _job(run, "already-eligible", "eligible", satisfied)
    _job(
        run,
        "format-unresolved",
        "uncertain",
        {**satisfied, "study_geography": "uncertain"},
        state="unresolved_rescreenable",
    )

    keys = eligibility.geography_rescreen_keys(run)
    assert keys == {"only-geography", "format-unresolved"}


def test_the_rescreen_action_is_offered_by_the_command_line() -> None:
    from arctic_qa.cli import parser

    action = next(
        row
        for row in parser()
        ._subparsers._group_actions[0]
        .choices["gemini-eligibility"]
        ._actions
        if row.dest == "action"
    )
    assert "geography-rescreen" in action.choices
    assert "geography-rescreen-dry-run" in action.choices


def test_the_status_reports_an_exhausted_paper_as_unresolved_not_queued(
    tmp_path: Path,
) -> None:
    """A spent paper must not show as queued for the rest of the run."""
    run = tmp_path / "run"
    (run / "unresolved").mkdir(parents=True)
    (run / "unresolved" / "k.json").write_text(
        canonical_json(
            {
                "state": eligibility.UNRESOLVED_STATE,
                "job_key": "k",
                "candidate_key": "10.1/x",
                "attempts": eligibility.MAXIMUM_FORMAT_ATTEMPTS,
                "format_errors": ["eligible_arctic_scope_phrase_unbound"],
            }
        ),
        encoding="utf-8",
    )
    (run / "budget-ledger.json").write_text(canonical_json({}), encoding="utf-8")

    status = eligibility._status(
        run,
        config={"model": "test-model"},
        sources=[{"candidate_key": "10.1/x"}],
        key_present=True,
        enabled=True,
        state="paused",
        live_call_made=False,
        estimated=0,
    )
    assert status["counts"]["unresolved_rescreenable"] == 1
    assert status["counts"]["screening_error"] == 0
    assert status["counts"]["queued"] == 0
    rows = [
        json.loads(line)
        for line in (run / "gemini-overlay.ndjson")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert [row["gemini_status"] for row in rows] == ["unresolved_rescreenable"]
    assert rows[0]["gemini_decision"] is None


def test_the_corpus_viewer_knows_the_unresolved_state() -> None:
    """The read-only monitor refuses an overlay status it does not know."""
    from arctic_qa.corpus_viewer import GEMINI_FILTERS

    assert eligibility.UNRESOLVED_STATE in GEMINI_FILTERS
    page = (ROOT / "src" / "arctic_qa" / "corpus_viewer.html").read_text(
        encoding="utf-8"
    )
    assert f'value="{eligibility.UNRESOLVED_STATE}"' in page
