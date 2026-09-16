"""Eligibility prompt v8, schema v4 and the chapter 3 contract corrections.

Every test pins a defect the chapter 2 yield audit named in section 4.7 and in
the eligibility stage findings E1, E2, E6 and E8.
"""

from __future__ import annotations

import itertools
import json
from hashlib import sha256
from pathlib import Path

import arctic_qa.gemini_eligibility as eligibility
from arctic_qa.util import canonical_json


ROOT = Path(__file__).parents[1]
PROMPT = (ROOT / "config" / "gemini-eligibility-prompt-v8.txt").read_text(
    encoding="utf-8"
)
RESCREEN_PROMPT = (
    ROOT / "config" / "gemini-eligibility-geography-rescreen-v2.txt"
).read_text(encoding="utf-8")
SCHEMA = json.loads(
    (ROOT / "schemas" / "gemini-eligibility.v4.schema.json").read_text(encoding="utf-8")
)

SOURCE_LINES = (
    "Methods: We sampled Arctic PM10 at Ny-Ålesund (Svalbard Islands) at 78.9 N.\n",
    "Methods: Collection ran from March 2012 to September 2013 at that station.\n",
    "Methods: We collected 48 filter units over the two field seasons.\n",
    "Methods: An ion chromatograph measured the water-soluble ion fraction.\n",
    "Methods: We report particulate matter (PM10) over the whole record.\n",
    "Results: At Ny-Ålesund the oxalate share reached 15 percent.\n",
    "Results: At 54.0 N the southern station reached 40 percent.\n",
    "DOI 10.9999/scope-binding identifies this version.\n",
    "This article uses the CC BY 4.0 license.\n",
)
GEOGRAPHY, PERIOD, SAMPLE, METHOD, DEFINITION = range(5)
FINDING, SOUTHERN, IDENTIFIER, LICENSE = range(5, 9)


def _flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------- prompt v8


def test_v8_states_the_missing_context_rule_the_validator_enforces() -> None:
    """Audit 4.7 E1: 22 papers died on a rule no prompt and no schema stated."""
    flat = _flat(PROMPT)
    assert (
        "When a criterion status is uncertain, write at least one value in that "
        "criterion's missing_context. Name what you could not read." in flat
    )
    description = SCHEMA["$defs"]["criterion"]["properties"]["missing_context"][
        "description"
    ]
    assert "Required when status is uncertain" in description


def test_v8_asks_for_the_study_setting_by_dimension() -> None:
    """Audit 4.7 phase B: the writer needs the setting, not only the proof."""
    flat = _flat(PROMPT)
    assert "Select activity spans for two purposes" in flat
    assert (
        "At least one activity span must also appear in the selected "
        "study_geography evidence" in flat
    )
    assert "Every activity_span_ids value must also appear" not in flat
    assert "Write each activity span as one object with a span_id and a dimension" in (
        flat
    )
    for dimension in eligibility.SCOPE_DIMENSIONS:
        assert f"- {dimension} for " in PROMPT
    assert "A span with no date is not a period span" in flat


def test_v8_binds_the_reason_codes_to_the_schema_enum() -> None:
    """Audit 4.7 E8: 79 papers produced 60 distinct geography reason codes."""
    flat = _flat(PROMPT)
    assert "Use only the reason codes in the schema enum for that criterion" in flat
    assert "Use other when no listed code states your reason" in flat
    assert "Use short reason codes and missing-context values" not in flat
    enum = set(
        SCHEMA["$defs"]["criterion"]["properties"]["reason_codes"]["items"]["enum"]
    )
    assert enum == set(eligibility.ELIGIBILITY_REASON_CODES)
    assert "other" in enum


def test_v8_keeps_every_rule_the_audit_ordered_kept() -> None:
    """Rigor: the ordered geography procedure and the separable rule survive."""
    flat = _flat(PROMPT)
    assert "GEOGRAPHY DECISION PROCEDURE" in flat
    assert "Stop at the first rule that applies" in flat
    assert "Absence of evidence is uncertain. Absence of evidence is never failed" in (
        flat
    )
    assert (
        "fail geography only when all actual study activity is outside the "
        "boundary or the Arctic reference is incidental" in flat
    )
    assert (
        "When the spans show results both inside and outside the boundary, set "
        "separable_arctic_component, never whole_study" in flat
    )
    assert (
        "Span location records provenance only. It does not prove that the "
        "selected text entails the criterion status" in flat
    )
    assert (
        "Do not use a title, affiliation, background statement, or citation as "
        "Arctic scope" in flat
    )
    assert "Use eligible_arctic_scope only for actual study activity" in flat


def test_the_rescreen_prompt_v2_carries_the_v8_span_and_phrase_blocks() -> None:
    """Audit 4.7: a recovered paper must enter with the same phrase discipline."""
    flat = _flat(RESCREEN_PROMPT)
    assert "You decide one criterion only: study_geography" in flat
    assert "Every other criterion keeps the status of the first screening" in flat
    assert "ARCTIC SCOPE SPANS" in RESCREEN_PROMPT
    assert "QUESTION SCOPE PHRASES" in RESCREEN_PROMPT
    assert "Write each activity span as one object with a span_id and a dimension" in (
        flat
    )
    assert "Do not use a numeric result value, a percentage, a bare unit" in flat
    assert "Absence of evidence is never failed" in flat


def test_schema_v4_carries_dimension_labelled_activity_spans() -> None:
    scope = SCHEMA["$defs"]["arctic_scope"]
    assert set(scope["required"]) == {
        "component",
        "activity_spans",
        "finding_span_ids",
        "question_scope_phrases",
    }
    spans = scope["properties"]["activity_spans"]
    assert spans["maxItems"] == 12
    assert set(spans["items"]["required"]) == {"span_id", "dimension"}
    assert set(spans["items"]["properties"]["dimension"]["enum"]) == set(
        eligibility.SCOPE_DIMENSIONS
    )


# ------------------------------------------------------- the response builder


def _case(
    *,
    statuses: dict[str, str] | None = None,
    missing_context: dict[str, list[str]] | None = None,
    reason_codes: dict[str, list[str]] | None = None,
    evidence: dict[str, list[list[str]]] | None = None,
    activity_spans: list[dict[str, str]] | None = None,
    finding_spans: list[str] | None = None,
    phrases: list[str] | None = None,
    component: str = "separable_arctic_component",
    version: str = eligibility.ELIGIBILITY_RESPONSE_V4,
) -> dict[str, object]:
    text = "".join(SOURCE_LINES)
    extraction = sha256(text.encode()).hexdigest()
    blocks = eligibility._span_blocks_v2(text, extraction)
    spans = [span["span_id"] for block in blocks for span in block["spans"]]
    default_evidence = {
        "published_primary_findings": [[spans[FINDING]]],
        "stable_identity_version": [[spans[IDENTIFIER]]],
        "study_geography": [[spans[GEOGRAPHY]]],
        "access_rights_evidence": [[spans[LICENSE]]],
        "correction_retraction_coverage": [],
    }
    default_statuses = {
        criterion: (
            "uncertain"
            if criterion == "correction_retraction_coverage"
            else "satisfied"
        )
        for criterion in eligibility.CRITERIA
    }
    default_reasons = {
        "published_primary_findings": ["primary_research_reported"],
        "stable_identity_version": ["identifier_present"],
        "study_geography": ["lat_ge_66_56"],
        "access_rights_evidence": ["access_open"],
        "correction_retraction_coverage": ["coverage_unknown"],
    }
    default_missing = {"correction_retraction_coverage": ["correction registry"]}
    statuses = {**default_statuses, **(statuses or {})}
    evidence = {**default_evidence, **(evidence or {})}
    reason_codes = {**default_reasons, **(reason_codes or {})}
    missing_context = {**default_missing, **(missing_context or {})}
    manifest = eligibility._span_manifest_v2(blocks, version)
    hashes = {
        "policy_sha256": "a" * 64,
        "source_version_sha256": "b" * 64,
        "extracted_text_sha256": extraction,
        "metadata_sha256": "c" * 64,
        "span_manifest_sha256": sha256(canonical_json(manifest).encode()).hexdigest(),
    }
    criteria = [
        {
            "criterion_id": criterion,
            "status": statuses[criterion],
            "reason_codes": reason_codes[criterion],
            "evidence": [{"span_ids": ids} for ids in evidence[criterion]],
            "missing_context": missing_context.get(criterion, []),
        }
        for criterion in eligibility.CRITERIA
    ]
    if activity_spans is None:
        activity_spans = [
            {"span_id": spans[GEOGRAPHY], "dimension": "geography"},
            {"span_id": spans[PERIOD], "dimension": "period"},
            {"span_id": spans[SAMPLE], "dimension": "sample"},
            {"span_id": spans[METHOD], "dimension": "method"},
            {"span_id": spans[DEFINITION], "dimension": "definition"},
        ]
    scope_key = (
        "activity_spans"
        if version == eligibility.ELIGIBILITY_RESPONSE_V4
        else "activity_span_ids"
    )
    scope_value: object = (
        activity_spans
        if version == eligibility.ELIGIBILITY_RESPONSE_V4
        else [record["span_id"] for record in activity_spans]
    )
    response = {
        "schema_version": version,
        "status_mapping_version": "eligibility-criterion-status-map-v1",
        "request_id": "contract-v8",
        "criteria": criteria,
        "eligible_arctic_scope": {
            "component": component,
            scope_key: scope_value,
            "finding_span_ids": (
                [spans[FINDING]] if finding_spans is None else finding_spans
            ),
            "question_scope_phrases": (
                ["At Ny-Ålesund the oxalate share"] if phrases is None else phrases
            ),
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
    schema = (
        SCHEMA
        if version == eligibility.ELIGIBILITY_RESPONSE_V4
        else json.loads(
            (ROOT / "schemas" / "gemini-eligibility.v3.schema.json").read_text()
        )
    )
    result = eligibility.validate_response(
        response,
        blocks,
        expected={
            "request_id": "contract-v8",
            "input_echo": hashes,
            "correction_metadata": response["correction_metadata_used"],
            "known_context_gaps": ["correction_retraction_coverage:unknown"],
        },
        response_schema=schema,
    )
    return {"result": result, "spans": spans, "response": response}


def test_a_clean_v4_response_validates_and_keeps_every_dimension() -> None:
    result = _case()["result"]
    assert result["errors"] == []
    assert result["valid"] is True
    assert result["decision"] == "eligible"
    scope = result["resolved_eligible_arctic_scope"]
    assert [row["dimension"] for row in scope["activity_spans"]] == list(
        eligibility.SCOPE_DIMENSIONS
    )
    for row in scope["activity_spans"]:
        assert row["source_bytes_sha256"] == sha256(row["quote"].encode()).hexdigest()


# ------------------------------------------- E1, the non-fatal missing_context


def test_an_empty_missing_context_is_recorded_and_no_longer_kills_the_paper() -> None:
    """Audit 4.7 E1: this code alone lost 22 of 200 papers and paused the run."""
    result = _case(missing_context={"correction_retraction_coverage": []})["result"]
    assert result["errors"] == []
    assert result["valid"] is True
    assert result["decision"] == "eligible"
    assert result["contract_notes"] == [
        "criterion_missing_context_absent:correction_retraction_coverage"
    ]


def test_an_uncertain_geography_keeps_its_decision_with_no_missing_context() -> None:
    """The recovered decision is the real one: uncertain, never eligible."""
    result = _case(
        statuses={"study_geography": "uncertain"},
        missing_context={"study_geography": [], "correction_retraction_coverage": []},
        evidence={"study_geography": []},
        reason_codes={"study_geography": ["activity_not_located"]},
        component="none",
        activity_spans=[],
        finding_spans=[],
        phrases=[],
    )["result"]
    assert result["errors"] == []
    assert result["decision"] == "uncertain"
    assert (
        "criterion_missing_context_absent:study_geography" in (result["contract_notes"])
    )


def test_the_code_stays_fatal_when_that_criterion_record_is_broken() -> None:
    """The check is non-fatal only when the criterion evidence is complete."""
    result = _case(
        statuses={"correction_retraction_coverage": "uncertain"},
        missing_context={"correction_retraction_coverage": []},
        evidence={"correction_retraction_coverage": [["s999999"]]},
    )["result"]
    assert (
        "criterion_missing_context_absent:correction_retraction_coverage"
        in result["errors"]
    )
    assert "evidence_span_unknown:correction_retraction_coverage" in result["errors"]
    assert result["valid"] is False
    assert result["decision"] == "uncertain"


def test_the_residual_case_is_re_askable_rather_than_terminal() -> None:
    """Audit 4.7 E6: a residual case must not end the paper."""
    assert "criterion_missing_context_absent" in eligibility.FORMAT_ERROR_CODES
    assert eligibility.format_repairable(["criterion_missing_context_absent"])


# -------------------------------------------------- E2, the re-screen alignment


def test_the_rescreen_and_the_status_mapping_agree_on_every_status_grid() -> None:
    """Audit 4.7 E2: the two definitions drifted and the re-screen took zero."""
    others = [
        "published_primary_findings",
        "stable_identity_version",
        "access_rights_evidence",
        "correction_retraction_coverage",
    ]
    values = ("satisfied", "failed", "uncertain")
    selected = 0
    for combination in itertools.product(values, repeat=len(others)):
        statuses = dict(zip(others, combination))
        statuses["study_geography"] = "uncertain"
        by_id = {name: {"status": status} for name, status in statuses.items()}
        assert eligibility._status_mapping_v2(by_id)[0] != "eligible"
        satisfied_geography = {
            name: {"status": status} for name, status in statuses.items()
        }
        satisfied_geography["study_geography"] = {"status": "satisfied"}
        would_be_eligible = (
            eligibility._status_mapping_v2(satisfied_geography)[0] == "eligible"
        )
        assert eligibility.geography_rescreen_eligible(statuses) is would_be_eligible
        selected += would_be_eligible
    assert selected > 0


def test_a_failed_geography_never_returns_to_the_re_screen() -> None:
    """Rigor: a failed geography is a decision, not an unresolved criterion."""
    satisfied = dict.fromkeys(eligibility.CRITERIA, "satisfied")
    assert not eligibility.geography_rescreen_eligible(
        {**satisfied, "study_geography": "failed"}
    )
    assert not eligibility.geography_rescreen_eligible(satisfied)
    assert eligibility.geography_rescreen_eligible(
        {
            **satisfied,
            "study_geography": "uncertain",
            "correction_retraction_coverage": "uncertain",
        }
    )


def test_the_chapter_2_correction_criterion_no_longer_blocks_every_paper() -> None:
    """Audit 4.7 E2: this criterion is uncertain on 79 of 79 non-eligible papers."""
    statuses = {
        "published_primary_findings": "satisfied",
        "stable_identity_version": "satisfied",
        "access_rights_evidence": "satisfied",
        "study_geography": "uncertain",
        "correction_retraction_coverage": "uncertain",
    }
    assert eligibility.geography_rescreen_eligible(statuses) is True
    assert (
        eligibility.unsatisfied_required_criteria(
            statuses, ignore=frozenset({"study_geography"})
        )
        == []
    )


# ------------------------------------------ phase B, the intersection and labels


def test_the_intersection_test_replaces_the_subset_test() -> None:
    """Audit 4.7 phase B: the subset test made setting spans a geography subset."""
    v4 = _case()["result"]
    assert "eligible_arctic_scope_activity_unbound" not in v4["errors"]
    v3 = _case(version=eligibility.ELIGIBILITY_RESPONSE_V3)["result"]
    assert "eligible_arctic_scope_activity_unbound" in v3["errors"]


def test_no_geography_bearing_activity_span_still_breaks_the_custody_chain() -> None:
    """Rigor: at least one activity span must prove the geography."""
    case = _case(activity_spans=[])
    spans = case["spans"]
    result = _case(activity_spans=[{"span_id": spans[PERIOD], "dimension": "period"}])[
        "result"
    ]
    assert "eligible_arctic_scope_activity_unbound" in result["errors"]


def test_a_dimension_label_the_span_text_cannot_support_is_refused() -> None:
    """Audit 4.7 phase B: the label is a claim, so it is checked against the text."""
    spans = _case()["spans"]
    result = _case(
        activity_spans=[
            {"span_id": spans[GEOGRAPHY], "dimension": "geography"},
            {"span_id": spans[METHOD], "dimension": "period"},
        ]
    )["result"]
    assert "eligible_arctic_scope_dimension_unsupported" in result["errors"]
    assert result["format_repair_detail"]["mislabelled_dimensions"] == [
        {"span_id": spans[METHOD], "dimension": "period"}
    ]
    assert eligibility.format_repairable(result["errors"])


def test_each_dimension_marker_reads_its_own_span_text() -> None:
    assert eligibility._dimension_supported(SOURCE_LINES[GEOGRAPHY], "geography")
    assert eligibility._dimension_supported(SOURCE_LINES[PERIOD], "period")
    assert eligibility._dimension_supported(SOURCE_LINES[SAMPLE], "sample")
    assert eligibility._dimension_supported(SOURCE_LINES[METHOD], "method")
    assert eligibility._dimension_supported(SOURCE_LINES[DEFINITION], "definition")
    assert not eligibility._dimension_supported(SOURCE_LINES[METHOD], "period")
    assert not eligibility._dimension_supported("◦\n", "geography")
    assert not eligibility._dimension_supported(SOURCE_LINES[GEOGRAPHY], "unknown")


def test_a_malformed_activity_span_record_is_a_scope_error() -> None:
    spans = _case()["spans"]
    result = _case(
        activity_spans=[{"span_id": spans[GEOGRAPHY], "dimension": "instrument"}]
    )["result"]
    assert "eligible_arctic_scope_invalid" in result["errors"]


# -------------------------------------------------------- E8, the reason codes


def test_a_reason_code_outside_the_enum_is_measured_and_never_decides() -> None:
    """Audit 4.7 E8: measurement only, never a prune of the re-screen pool."""
    result = _case(reason_codes={"study_geography": ["all_sites_outside_boundary"]})[
        "result"
    ]
    assert result["errors"] == []
    assert result["valid"] is True
    assert result["decision"] == "eligible"
    assert "reason_code_out_of_enum:study_geography" in result["contract_notes"]


# ----------------------------------------------- E6, the specific repair note


def test_the_repair_note_names_the_phrase_that_failed_and_the_text_it_missed() -> None:
    """Audit 4.7 E6: the chapter 2 note carried the code and nothing else."""
    result = _case(phrases=["the oxalate share reached 15 percent in the south"])[
        "result"
    ]
    assert "eligible_arctic_scope_phrase_unbound" in result["errors"]
    detail = result["format_repair_detail"]
    assert detail["unbound_phrases"] == [
        "the oxalate share reached 15 percent in the south"
    ]
    assert (
        "At Ny-Ålesund the oxalate share reached 15 percent"
        in (detail["finding_span_text"])
    )
    note = eligibility._repair_note(
        {
            "attempts": 1,
            "format_errors": result["errors"],
            "parsed_response": {
                "criteria": [{"criterion_id": "study_geography", "status": "satisfied"}]
            },
            "validation": result,
        }
    )
    assert note["unbound_phrases"] == detail["unbound_phrases"]
    assert note["finding_span_text"] == detail["finding_span_text"]
    assert (
        "unbound_phrases lists each question_scope_phrases value"
        in (note["instruction"])
    )
    assert "Do not change any criterion status" in note["instruction"]


def test_a_repaired_phrase_must_still_name_a_station_region_or_population() -> None:
    """Audit 4.7 phase D: a bound phrase that identifies nothing is not a fix."""
    assert eligibility.phrase_is_specific("Ny-Ålesund station")
    assert eligibility.phrase_is_specific("the Svalbard Archipelago")
    assert not eligibility.phrase_is_specific("15 percent")
    assert not eligibility.phrase_is_specific("35 %")
    assert not eligibility.phrase_is_specific("In the Arctic")
    assert not eligibility.phrase_is_specific("the study area")
    assert not eligibility.phrase_is_specific("  ")
    assert eligibility.repaired_phrases_are_specific(
        {"eligible_arctic_scope": {"question_scope_phrases": ["Ny-Ålesund station"]}}
    )
    assert not eligibility.repaired_phrases_are_specific(
        {"eligible_arctic_scope": {"question_scope_phrases": ["In the Arctic"]}}
    )
    assert not eligibility.repaired_phrases_are_specific(None)


def test_a_repair_that_moves_a_criterion_status_is_still_refused() -> None:
    """Rigor: `_repair_moved_a_status` is unchanged and still in force."""
    prior = {
        "attempts": 1,
        "format_errors": ["eligible_arctic_scope_phrase_unbound"],
        "parsed_response": {
            "criteria": [{"criterion_id": "study_geography", "status": "uncertain"}]
        },
    }
    assert eligibility._repair_moved_a_status(
        prior,
        {"criteria": [{"criterion_id": "study_geography", "status": "satisfied"}]},
    )


# ----------------------------------------------- the two-pass shadow at USD 0


def test_the_two_pass_screen_is_a_shadow_measurement_booked_at_zero() -> None:
    """Audit 4.10: refuted as a saving, kept as a measurement only."""
    text = "".join(SOURCE_LINES)
    scope = _case()["result"]["resolved_eligible_arctic_scope"]
    record = eligibility.shadow_two_pass_measurement(text, scope)
    assert record["schema"] == eligibility.SHADOW_TWO_PASS_VERSION
    assert record["applied"] is False
    assert record["decision_path"] is False
    assert record["booked_usd"] == "0"
    assert record["selected_activity_spans"] == 5
    assert record["measurement"] in {
        "short_view_covers_selected_spans",
        "defer_to_full_text",
    }
    assert eligibility.shadow_two_pass_measurement(text, None)["measurement"] == (
        "defer_to_full_text"
    )
