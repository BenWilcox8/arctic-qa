"""Regressions for the r15 holistic audit phase 0 defects.

Each test names the audit finding it closes. Every fix here either moves an
existing rejection earlier, records why a kill happened, or removes a
presentation artifact from a containment test. None of them admits an item.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctic_qa import generation
from arctic_qa.db import Database, now
from arctic_qa.errors import CandidateRejectedError
from arctic_qa.gemini_eligibility import normalize_for_phrase_binding
from arctic_qa.util import canonical_json, sha256_bytes, stable_id
from arctic_qa.validation import (
    option_display_issue,
    rejection_diagnostic_detail,
    scope_is_evidence_bound,
    validate_candidate,
)


CHUNK_TEXT = (
    "The reported water depth was 2.0 m with a source-grounded tolerance of 0.1 m."
)


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate(tmp_path / "backups")
    return database


def _chunk() -> dict[str, object]:
    return {"chunk_id": "chunk-1", "text": CHUNK_TEXT}


def _answer(**overrides: object) -> dict[str, object]:
    start = CHUNK_TEXT.index("The reported")
    answer = {
        "text": "2.0 m",
        "evidence_quote": CHUNK_TEXT,
        "locator": {
            "chunk_id": "chunk-1",
            "start_offset": start,
            "end_offset": start + len(CHUNK_TEXT),
        },
        "required_question_phrases": ["reported water depth"],
        "scope": {"method": "reported water depth"},
        "claim_type": "observation",
        "selection_rationale": "one bounded observed depth",
    }
    answer.update(overrides)
    return answer


# FS-3 / section 4.5 fix 2: the evidence quote must contain the finding.


def test_freeze_time_admits_a_finding_whose_quote_contains_it() -> None:
    assert generation.finding_admission_reason(_answer(), _chunk()) is None


def test_freeze_time_rejects_a_quote_that_excludes_the_finding() -> None:
    header = "Journal of Arctic Research, Volume 12"
    answer = _answer(
        evidence_quote=header,
        locator={"chunk_id": "chunk-1", "start_offset": 0, "end_offset": len(header)},
    )
    chunk = {"chunk_id": "chunk-1", "text": header + CHUNK_TEXT}

    reason = generation.finding_admission_reason(answer, chunk)

    assert reason is not None
    assert reason[0] == "finding_evidence_quote_excludes_finding"


def test_freeze_time_admits_a_finding_bound_by_its_deterministic_source_value() -> None:
    answer = _answer(
        text="deeper than the shelf average",
        deterministic_rule={"kind": "directional_relation", "source_value": "2.0 m"},
    )

    assert generation.finding_admission_reason(answer, _chunk()) is None


# FS-1 / section 4.5 fix 1: the answer-leak check runs before the finding freezes.


def test_freeze_time_rejects_a_required_phrase_that_carries_the_answer() -> None:
    answer = _answer(required_question_phrases=["water depth was 2.0 m"])

    reason = generation.finding_admission_reason(answer, _chunk())

    assert reason is not None
    assert reason[0] == "finding_answer_phrase_in_required_question_phrases"


def test_the_freeze_time_gate_allows_one_free_re_ask() -> None:
    assert generation.FINDING_ADMISSION_PASSES == 2


# E3 / eligibility: the doubled-newline join and the presentation fold.


def test_phrase_binding_folds_only_presentation_differences() -> None:
    spans = [
        " soluble fraction of Arctic PM10 sam-\n",
        " ples collected at Ny-Ålesund (Svalbard Islands) during 2012.\n",
    ]
    joined = "".join(spans)

    haystack = normalize_for_phrase_binding(joined)

    assert normalize_for_phrase_binding("PM10 samples") in haystack
    assert normalize_for_phrase_binding("Ny-Ålesund (Svalbard Islands)") in haystack
    assert normalize_for_phrase_binding("Beaufort Sea") not in haystack


def test_phrase_binding_still_fails_a_phrase_the_spans_do_not_state() -> None:
    joined = " results from the Chukchi Sea shelf\n"

    haystack = normalize_for_phrase_binding(joined)

    assert normalize_for_phrase_binding("Beaufort Sea shelf") not in haystack


def test_a_doubled_newline_join_no_longer_breaks_a_cross_line_phrase() -> None:
    spans = ["Great skuas from Bjørn-\n", "øya were sampled in 2018.\n"]

    joined = "".join(spans)
    doubled = "\n".join(spans)

    assert normalize_for_phrase_binding(
        "Great skuas from Bjørnøya"
    ) in normalize_for_phrase_binding(joined)
    assert "Bjørnøya" not in doubled


# Section 4.2 fix 6 / 4.6 fix 6: every kill carries the judge's own reasons.


def test_rejection_detail_carries_every_judge_rationale() -> None:
    candidate = {
        "standalone_verification": {
            "unresolved_phrases": ["the ice cores"],
            "missing_detail_types": ["location"],
            "review_rationale": "the referent is not resolved",
        },
        "answer_verification": {
            "verification_rationale": "the span does not state the site",
            "residual_error": "geography missing",
            "question_context_missing_detail": "study location",
        },
    }

    detail = rejection_diagnostic_detail(candidate)

    assert detail["unresolved_phrases"] == ["the ice cores"]
    assert detail["missing_detail_types"] == ["location"]
    assert detail["review_rationale"] == "the referent is not resolved"
    assert detail["verification_rationale"] == "the span does not state the site"
    assert detail["residual_error"] == "geography missing"


def test_rejection_detail_is_empty_but_typed_for_a_candidate_without_reviews() -> None:
    detail = rejection_diagnostic_detail({})

    assert detail["unresolved_phrases"] == []
    assert detail["review_rationale"] == ""
    assert detail["contract_version"]


def test_the_rejection_ledger_stores_the_diagnostic_detail(tmp_path: Path) -> None:
    database = _database(tmp_path)
    candidate = {"schema_version": "1.0.0", "item_id": "aqa-legacy"}

    result = validate_candidate(database, tmp_path, candidate, persist=True)

    assert result.final_label == "rejected"
    row = database.one(
        "SELECT detail_json FROM rejection_ledger WHERE item_id=?", ("aqa-legacy",)
    )
    assert row is not None
    assert json.loads(row["detail_json"])["contract_version"]


# Section 4.8 item 5: a schema without a standalone contract can never validate.


@pytest.mark.parametrize(
    "schema_version", ["2.0.0", "2.1.0", "2.2.0", "2.3.0", "2.4.0"]
)
def test_a_schema_without_a_standalone_contract_is_refused(
    tmp_path: Path, schema_version: str
) -> None:
    database = _database(tmp_path)
    candidate = {
        "schema_version": schema_version,
        "item_id": f"aqa-{schema_version}",
        "question": "What changed?",
        "answer": {"text": "more"},
    }

    result = validate_candidate(database, tmp_path, candidate, persist=False)

    assert result.final_label == "rejected"
    assert result.reasons == ["unsafe_legacy_candidate_schema"]


def test_the_current_schema_still_declares_a_standalone_contract() -> None:
    from arctic_qa.validation import expected_standalone_contract

    assert expected_standalone_contract(generation.CANDIDATE_SCHEMA_VERSION)


# RECON-2 / section 4.4 fix 5: an honest empty reconstructor scope is not a kill.


def test_an_empty_reconstruction_scope_is_accepted_only_where_it_is_allowed() -> None:
    record = {"evidence_quote": CHUNK_TEXT}
    empty = {"geography": None, "period": None, "method": None}

    assert scope_is_evidence_bound(empty, record, allow_empty=True) is True
    assert scope_is_evidence_bound(empty, record) is False


def test_an_unsupported_reconstruction_scope_value_still_fails() -> None:
    record = {"evidence_quote": CHUNK_TEXT}
    invented = {"geography": "Beaufort Sea", "period": None}

    assert scope_is_evidence_bound(invented, record, allow_empty=True) is False


# DW-3 / section 4.6 fix 5: the display rules run before the paid verifier.


def test_the_option_prefilter_names_the_rule_that_rejects_an_option() -> None:
    answer = _answer()

    assert option_display_issue(answer, {"text": "3.0 m"}) is None
    assert (
        option_display_issue(answer, {"text": "3.0 m; 4.0 m"})
        == "displayed_assertion_compound"
    )
    assert (
        option_display_issue(answer, {"text": "not 2.0 m"})
        == "displayed_assertion_negated"
    )


def test_the_option_prefilter_uses_the_numeric_rule_for_a_numeric_option() -> None:
    answer = _answer()
    option = {
        "text": "3.0 m or deeper",
        "numeric": {"canonical_value": "3.0", "unit": "m"},
    }

    assert option_display_issue(answer, option) is not None


def _budget(database: Database) -> None:
    from decimal import Decimal

    from arctic_qa.providers import ensure_budget

    ensure_budget(database, "run", "tokens", Decimal("1000000"))


def _stored_candidate(database: Database, candidate: dict[str, object]) -> None:
    with database.transaction():
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                candidate["item_id"],
                "run",
                "source",
                "family",
                "answer_first",
                canonical_json(candidate),
                candidate.get("status", "candidate"),
                now(),
                now(),
            ),
        )


# R2 / section 4.6 fix 2: the writer can report a demand its source cannot meet.


def test_the_writer_schema_carries_the_context_gap_escape_hatch() -> None:
    schema = generation.ROLE_SCHEMAS["question_writer"]

    assert "context_gap" in schema["properties"]
    assert "context_gap" not in schema["required"]


# R4 / section 4.6 fix 4: the surgical correction no longer bypasses the gates.


def test_the_surgical_correction_returns_a_replacement_and_persists_nothing(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _budget(database)
    calls: list[str] = []

    class Recorder:
        name = "fake"
        model = "fake-model"

        def invoke(self, role, system, prompt, parameters, timeout):
            calls.append(prompt)
            from arctic_qa.providers import ProviderResult

            return ProviderResult(
                {"component": "question", "replacement": "What depth was reported?"},
                self.model,
                "request-1",
                1,
                1,
                None,
            )

    from decimal import Decimal

    replacement = generation.correct_one_component(
        database,
        Recorder(),
        run_id="run",
        entity_id="entity",
        component="question",
        candidate_record={"question": "What?"},
        context="SOURCE_DATA\n" + CHUNK_TEXT,
        reason_codes=["question_context_missing"],
        defect={"unresolved_phrases": ["the ice cores"]},
        parameters={"temperature": 0, "max_tokens": 128},
        reservation=Decimal("1000"),
        timeout=5,
        retries=0,
        rate_limit_seconds=0,
    )

    assert replacement == "What depth was reported?"
    assert "question_context_missing" in calls[0]
    assert "the ice cores" in calls[0]
    assert CHUNK_TEXT in calls[0]
    assert database.one("SELECT COUNT(*) AS count FROM candidates")["count"] == 0


def test_the_surgical_correction_refuses_another_component(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _budget(database)

    class Wrong:
        name = "fake"
        model = "fake-model"

        def invoke(self, role, system, prompt, parameters, timeout):
            from arctic_qa.providers import ProviderResult

            return ProviderResult(
                {"component": "distractors", "replacement": []},
                self.model,
                "request-1",
                1,
                1,
                None,
            )

    from decimal import Decimal

    with pytest.raises(CandidateRejectedError):
        generation.correct_one_component(
            database,
            Wrong(),
            run_id="run",
            entity_id="entity",
            component="question",
            candidate_record={"question": "What?"},
            context=CHUNK_TEXT,
            reason_codes=["question_context_missing"],
            defect={},
            parameters={"temperature": 0, "max_tokens": 128},
            reservation=Decimal("1000"),
            timeout=5,
            retries=0,
            rate_limit_seconds=0,
        )


# Section 4.1: the study-setting spans the classifier already located.


def test_activity_spans_are_read_only_with_matching_hashes() -> None:
    quote = "The study site was the Villum Research Station in Greenland."
    source = {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "resolved_eligible_arctic_scope": {
                    "activity_spans": [
                        {
                            "quote": quote,
                            "source_bytes_sha256": sha256_bytes(quote.encode()),
                        },
                        {"quote": "tampered", "source_bytes_sha256": "0" * 64},
                    ]
                }
            }
        ),
    }

    assert generation.eligible_activity_spans(source) == [quote]


def test_the_context_only_block_drops_a_span_that_states_the_answer() -> None:
    answer = _answer()
    leaking = "The depth of 2.0 m was measured at the station."
    source = {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "resolved_eligible_arctic_scope": {
                    "activity_spans": [
                        {
                            "quote": leaking,
                            "source_bytes_sha256": sha256_bytes(leaking.encode()),
                        }
                    ]
                }
            }
        ),
    }

    assert generation.activity_context_block(source, answer) == ""


def test_the_context_only_block_marks_its_spans_as_unselectable() -> None:
    quote = "Sampling ran at Hornsund, Svalbard, through the 2019 melt season."
    source = {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "resolved_eligible_arctic_scope": {
                    "activity_spans": [
                        {
                            "quote": quote,
                            "source_bytes_sha256": sha256_bytes(quote.encode()),
                        }
                    ]
                }
            }
        ),
    }

    block = generation.activity_context_block(source, _answer())

    assert "CONTEXT_ONLY_SOURCE" in block
    assert "Never select a CONTEXT_ONLY_SOURCE span as answer evidence" in block
    assert quote in block


def test_stable_identifiers_stay_bound_to_the_new_routing_contract() -> None:
    assert generation.GENERATION_ATTEMPT_CONTRACT_VERSION == (
        "bounded-failure-routing-v4"
    )
    assert stable_id("probe", generation.GENERATION_ATTEMPT_CONTRACT_VERSION)


def test_the_context_only_block_drops_a_span_that_states_an_answer_variant() -> None:
    answer = _answer(variants=["200 cm"])
    leaking = "Depths of 200 cm were recorded at the mooring."
    source = {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "resolved_eligible_arctic_scope": {
                    "activity_spans": [
                        {
                            "quote": leaking,
                            "source_bytes_sha256": sha256_bytes(leaking.encode()),
                        }
                    ]
                }
            }
        ),
    }

    assert generation.activity_context_block(source, answer) == ""
