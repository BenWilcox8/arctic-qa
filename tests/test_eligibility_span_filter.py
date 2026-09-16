"""The span filter and the geography re-screen, after the rule 5 interrupt.

The chapter 3 production run stopped at 22 papers with 9 of the first 19 in
`screening_error`. Two chapter 3 code paths caused it, and chapter 2 ran
neither. The dimension label test refused correct spans and ended whole
papers. The geography re-screen, which recorded no row at all in chapter 2,
failed every time it ran. These tests pin both corrections against the spans
and the responses the run recorded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import arctic_qa.gemini_eligibility as eligibility
import arctic_qa.streaming as streaming

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(Path(__file__).parent))

from test_eligibility_contract_v8 import (  # noqa: E402
    GEOGRAPHY,
    METHOD,
    SAMPLE,
    _case,
)

RECORDED = json.loads(
    (ROOT / "fixtures" / "eligibility-rescreen-recorded-r2.json").read_text(
        encoding="utf-8"
    )
)
# Span text the run recorded, with the paper each one came from. Every line
# here states the dimension the classifier gave it.
REFUSED_IN_THE_RUN = (
    ("geography", "(68.5833 o N, 149.7167 o W). It is bounded on the east by the\n"),
    ("geography", "the Itkillik River, extending approximately 65 km from North\n"),
    ("geography", "70 -78 N\n"),
    ("geography", "Bounded from the north by the parallels of the Vilkitsky\n"),
    ("sample", "The airborne surveys show the mean ice thickness of the floe.\n"),
    ("sample", "Nuclear icebreakers pr.22220 LK-60Ya, flag of Russia, 7 units\n"),
    ("sample", "S. polaris is a mat-forming prostrate dwarf shrub species.\n"),
    ("method", "on the eddy-covariance technique, which measured the movement\n"),
    ("method", "on PDA (Potato Dextrose, Agar, Biocorp) growth medium with acid\n"),
    ("method", "The ECS device was deployed on the undeformed part of each floe\n"),
    ("definition", "moist acidic tundra (MAT; soil pH <5.5), 15% as moist tundra\n"),
)
CYRILLIC = "Район исследования расположен в Арктической зоне России.\n"


# ------------------------------------------------- the widened marker patterns


def test_every_span_the_run_refused_as_wrong_now_reads_as_right() -> None:
    """Each line is a span the run dropped a paper for, and each is correct."""
    for dimension, text in REFUSED_IN_THE_RUN:
        assert eligibility._dimension_supported(text, dimension), (dimension, text)


def test_the_widened_geography_pattern_still_refuses_a_span_with_no_place() -> None:
    for text in ("An ion chromatograph measured the ion fraction.\n", "◦\n", "10 on\n"):
        assert not eligibility._dimension_supported(text, "geography")


def test_a_span_in_another_script_cannot_be_judged_by_english_markers() -> None:
    assert eligibility._dimension_verifiable("Ny-Alesund at 78.9 N\n") is True
    assert eligibility._dimension_verifiable(CYRILLIC) is False
    # A span with no letters carries no script, so it stays judgeable.
    assert eligibility._dimension_verifiable("78.9 N, 11.9 E\n") is True


# ------------------------------------------------------------ the span filter


def test_a_span_in_another_script_is_forwarded_and_flagged_never_dropped() -> None:
    """A non-English paper must not lose its whole context to an English list."""
    case = _case(extra_lines=(CYRILLIC,))
    spans = case["spans"]
    result = _case(
        extra_lines=(CYRILLIC,),
        activity_spans=[
            {"span_id": spans[GEOGRAPHY], "dimension": "geography"},
            {"span_id": spans[-1], "dimension": "geography"},
        ],
    )["result"]
    assert result["errors"] == []
    assert result["dimension_span_filter"]["dropped"] == []
    assert result["dimension_span_filter"]["unverified"] == [
        {
            "span_id": spans[-1],
            "dimension": "geography",
            "reason": "dimension_marker_not_latin_script",
        }
    ]
    forwarded = result["resolved_eligible_arctic_scope"]["activity_spans"]
    assert [span["span_id"] for span in forwarded] == [spans[GEOGRAPHY], spans[-1]]
    # The flag travels with the span, so nothing unverified reads as verified.
    assert "dimension_verified" not in forwarded[0]
    assert forwarded[1]["dimension_verified"] is False


def test_a_wrong_label_costs_no_re_ask_and_no_paper() -> None:
    """The code is not an error any more, so it cannot spend a bounded re-ask."""
    assert (
        "eligible_arctic_scope_dimension_unsupported"
        not in eligibility.FORMAT_ERROR_CODES
    )
    spans = _case()["spans"]
    result = _case(
        activity_spans=[
            {"span_id": spans[GEOGRAPHY], "dimension": "geography"},
            {"span_id": spans[SAMPLE], "dimension": "definition"},
        ]
    )["result"]
    assert result["errors"] == []
    assert eligibility.format_repairable(result["errors"]) is False


def test_losing_the_custody_span_to_the_filter_is_recorded() -> None:
    """A forwarded context that no longer proves the geography says so."""
    spans = _case()["spans"]
    result = _case(
        activity_spans=[
            {"span_id": spans[GEOGRAPHY], "dimension": "period"},
            {"span_id": spans[METHOD], "dimension": "method"},
        ]
    )["result"]
    assert [row["span_id"] for row in result["dimension_span_filter"]["dropped"]] == [
        spans[GEOGRAPHY]
    ]
    assert "scope_activity_custody_dropped" in result["contract_notes"]
    # The paper keeps its decision. The note is measurement, never a verdict.
    assert result["errors"] == []
    assert result["decision"] == "eligible"


# ------------------------------------------------- the geography re-screen


def _rescreen_criteria(recorded: dict, spans: list[str]):
    """Replay one recorded answer against the synthetic span catalog."""

    def factory(catalog_spans: list[str]) -> list[dict]:
        rows = []
        for row in recorded["criteria"]:
            row = dict(row)
            if row["criterion_id"] == "study_geography" and row["evidence"]:
                row["evidence"] = [{"span_ids": [catalog_spans[GEOGRAPHY]]}]
            rows.append(row)
        return rows

    return factory


def test_the_run_recorded_three_re_screens_and_every_one_of_them_failed() -> None:
    assert len(RECORDED["responses"]) == 3
    for recorded in RECORDED["responses"]:
        assert recorded["state"] == "screening_error"
        assert [
            code
            for code in recorded["errors"]
            if code.startswith("criterion_evidence_missing:")
        ] == [
            "criterion_evidence_missing:access_rights_evidence",
            "criterion_evidence_missing:published_primary_findings",
            "criterion_evidence_missing:stable_identity_version",
        ]


def _scope_of(recorded: dict) -> dict:
    """A scope is empty unless the re-screen satisfied the geography."""
    geography = next(
        row for row in recorded["criteria"] if row["criterion_id"] == "study_geography"
    )
    if geography["status"] == "satisfied":
        return {}
    return {
        "component": "none",
        "activity_spans": [],
        "finding_spans": [],
        "phrases": [],
    }


def test_each_recorded_re_screen_still_fails_when_it_is_read_as_a_screening() -> None:
    """The recorded defect reproduces: the restated rows carry no evidence."""
    for recorded in RECORDED["responses"]:
        result = _case(
            criteria_factory=_rescreen_criteria(recorded, []),
            **_scope_of(recorded),
        )["result"]
        assert "criterion_evidence_missing:access_rights_evidence" in result["errors"]
        assert (
            "criterion_evidence_missing:published_primary_findings"
            in (result["errors"])
        )
        assert "criterion_evidence_missing:stable_identity_version" in result["errors"]


def test_each_recorded_re_screen_validates_when_the_frozen_rows_are_ignored() -> None:
    """The re-screen decides one criterion, so only that one is read."""
    for recorded in RECORDED["responses"]:
        frozen = recorded["frozen_criterion_statuses"]
        result = _case(
            criteria_factory=_rescreen_criteria(recorded, []),
            frozen=frozen,
            **_scope_of(recorded),
        )["result"]
        assert [
            code
            for code in result["errors"]
            if code.startswith("criterion_evidence_missing:")
        ] == []
        assert result["errors"] == []
        assert result["valid"] is True


def test_the_schema_is_why_the_model_had_to_restate_the_frozen_rows() -> None:
    """A one-row answer is not an option: the schema refuses it before reading."""
    recorded = RECORDED["responses"][0]

    def factory(spans: list[str]) -> list[dict]:
        return [
            row
            for row in _rescreen_criteria(recorded, [])(spans)
            if row["criterion_id"] == "study_geography"
        ]

    result = _case(
        criteria_factory=factory,
        frozen=recorded["frozen_criterion_statuses"],
        **_scope_of(recorded),
    )["result"]
    assert "schema_array_too_short" in result["errors"]
    assert "criterion_set_invalid" in result["errors"]


def test_the_re_screen_prompt_asks_for_the_five_rows_the_schema_needs() -> None:
    """The prompt, the schema and the validator must state the same thing."""
    schema = json.loads(
        (ROOT / "schemas" / "gemini-eligibility.v4.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["properties"]["criteria"]["minItems"] == 5
    prompt = (
        ROOT / "config" / "gemini-eligibility-geography-rescreen-v2.txt"
    ).read_text(encoding="utf-8")
    flat = " ".join(prompt.split())
    assert (
        "The schema needs all five criteria, so write each other criterion with "
        "its frozen status, an empty evidence list and an empty missing_context "
        "list." in flat
    )
    assert "Do not restate it and do not revise it" not in flat


def test_only_a_re_screen_attempt_carries_frozen_statuses_into_validation() -> None:
    """The wiring reads the note the row recorded, so a replay reads the same."""
    note = {
        "kind": "geography_rescreen",
        "frozen_criterion_statuses": {"access_rights_evidence": "satisfied"},
    }
    assert streaming._frozen_rescreen_statuses(note) == {
        "access_rights_evidence": "satisfied"
    }
    assert streaming._frozen_rescreen_statuses(None) is None
    assert streaming._frozen_rescreen_statuses({"kind": "format_repair"}) is None
    assert streaming._frozen_rescreen_statuses({"kind": "geography_rescreen"}) is None


def test_the_re_screen_note_freezes_the_four_criteria_it_may_not_touch() -> None:
    job = {
        "parsed_response": {
            "criteria": [
                {"criterion_id": name, "status": "satisfied"}
                for name in eligibility.CRITERIA
            ]
        }
    }
    note = streaming._rescreen_note(job)
    assert note["kind"] == "geography_rescreen"
    assert "study_geography" not in note["frozen_criterion_statuses"]
    assert set(note["frozen_criterion_statuses"]) == set(eligibility.CRITERIA) - {
        "study_geography"
    }
