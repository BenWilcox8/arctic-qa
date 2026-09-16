"""Chapter 2 yield audit, section 4.5: the ranked finding bank.

The ranked candidates of one extraction are persisted per family and served
to the alternative-finding rung, a banked candidate passes the same admission
path as a fresh one, ranking puts a study-internal index last, and the
freeze-time checks re-derived from the frozen chapter 2 text only reject.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from arctic_qa import generation  # noqa: E402
from arctic_qa.errors import CandidateRejectedError  # noqa: E402
from arctic_qa.streaming import _generation_attempt  # noqa: E402
from cost_plan_harness import Seeded, fixture_events  # noqa: E402

FINDING_SENTENCE = "Sulfate aerosol mass declined by 15 percent at the Arctic station."


def _second_candidate() -> dict:
    return {
        "rank": 2,
        "admissible": True,
        "answer_basis_class": "category_identity",
        "source_blind_answer_basis": (
            "A substrate class is a category a reader can bound from the site."
        ),
        "answer": {
            "text": "gravel",
            "variants": [],
            "claim_type": "observation",
            "selection_rationale": "The result names the only reported substrate.",
            "source_span_id": "{{span_id}}",
            "scope": {
                "geography": None,
                "population": None,
                "period": None,
                "method": "reported substrate category",
                "comparison": None,
                "uncertainty": None,
            },
            "required_question_phrases": ["reported substrate category"],
        },
        "ranking_rationale": "A categorical result from a second sentence.",
    }


def _two_candidate_author() -> list[dict]:
    events = fixture_events("fake-author.jsonl")
    events[0]["response"]["candidate_findings"].append(_second_candidate())
    return events


def _slot_unavailable_writer(text: str) -> dict:
    writer = fixture_events("fake-author.jsonl")[1]
    writer.pop("require_prompt_contains", None)
    writer["response"]["question"] = text
    for row in writer["response"]["referent_slots"]:
        if row["slot"] == "location":
            row["state"] = "unavailable_in_source"
    return writer


def _alternative_attempt(seeded: Seeded, run_id: str, primary: dict) -> dict:
    primary_attempt = primary["provenance"]["generation_attempt"]
    return _generation_attempt(
        campaign_id=run_id,
        family_id=seeded.source["paper_family_id"],
        finding_attempt_index=2,
        question_revision_index=0,
        attempt_kind="alternative_finding",
        parent_attempt_id=primary_attempt["attempt_id"],
        parent_item_id=primary["item_id"],
        trigger_reason_code="writer_slot_unavailable_location",
        excluded_finding_span_ids=[
            primary["answer"]["source_span_id"],
            *primary["answer"].get("source_span_ids", []),
        ],
    )


def _primary_attempt(seeded: Seeded, run_id: str) -> dict:
    return _generation_attempt(
        campaign_id=run_id,
        family_id=seeded.source["paper_family_id"],
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )


def _bank_rows(seeded: Seeded, run_id: str) -> list[dict]:
    return [
        dict(row)
        for row in seeded.db.rows(
            "SELECT * FROM finding_bank WHERE run_id=? ORDER BY rowid",
            (run_id,),
        )
    ]


def test_the_bank_serves_the_alternative_finding_without_an_extractor_call(
    tmp_path: Path,
) -> None:
    seeded = Seeded(tmp_path, char_cap=120, overlap_chars=0)
    run_id = "bank"
    author = _two_candidate_author()
    author[1] = _slot_unavailable_writer("What reported water depth was documented?")
    primary = seeded.generate(
        run_id, author[:2], [], generation_attempt=_primary_attempt(seeded, run_id)
    )
    assert primary["answer"]["text"] == "2.0 m"
    admission = primary["provenance"]["finding_admission"]
    assert admission["served_from_bank"] is False
    assert admission["candidate_count"] == 2
    assert [row["status"] for row in admission["candidate_evaluations"]] == [
        "admissible",
        "admissible",
    ]
    rows = _bank_rows(seeded, run_id)
    assert [row["admission_status"] for row in rows] == ["frozen", "admissible"]
    assert rows[0]["frozen_finding_id"] == primary["finding_id"]

    alternative = seeded.generate(
        run_id,
        [_slot_unavailable_writer("Which reported substrate category was documented?")],
        [],
        generation_attempt=_alternative_attempt(seeded, run_id, primary),
    )
    assert alternative["answer"]["text"] == "gravel"
    assert alternative["finding_id"] != primary["finding_id"]
    served = alternative["provenance"]["finding_admission"]
    assert served["served_from_bank"] is True
    assert served["bank_row_id"] == rows[1]["bank_row_id"]
    assert seeded.call_roles(run_id).count("extractor") == 1
    assert [row["admission_status"] for row in _bank_rows(seeded, run_id)] == [
        "frozen",
        "frozen",
    ]


def test_an_empty_bank_calls_the_extractor(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path, char_cap=120, overlap_chars=0)
    run_id = "bank-empty"
    author = fixture_events("fake-author.jsonl")
    author[1] = _slot_unavailable_writer("What reported water depth was documented?")
    primary = seeded.generate(
        run_id, author[:2], [], generation_attempt=_primary_attempt(seeded, run_id)
    )
    assert [row["admission_status"] for row in _bank_rows(seeded, run_id)] == ["frozen"]

    second_extraction = fixture_events("fake-author.jsonl")[0]
    second_extraction["response"]["candidate_findings"] = [_second_candidate()]
    alternative = seeded.generate(
        run_id,
        [
            second_extraction,
            _slot_unavailable_writer("Which reported substrate category was documented?"),
        ],
        [],
        generation_attempt=_alternative_attempt(seeded, run_id, primary),
    )
    assert alternative["answer"]["text"] == "gravel"
    assert seeded.call_roles(run_id).count("extractor") == 2


def test_a_row_under_another_bank_key_is_never_served(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path, char_cap=120, overlap_chars=0)
    run_id = "bank-key"
    author = _two_candidate_author()
    author[1] = _slot_unavailable_writer("What reported water depth was documented?")
    primary = seeded.generate(
        run_id, author[:2], [], generation_attempt=_primary_attempt(seeded, run_id)
    )
    with seeded.db.transaction():
        seeded.db.connection.execute(
            "UPDATE finding_bank SET bank_key='stale-contract' WHERE run_id=?",
            (run_id,),
        )
    second_extraction = fixture_events("fake-author.jsonl")[0]
    second_extraction["response"]["candidate_findings"] = [_second_candidate()]
    seeded.generate(
        run_id,
        [
            second_extraction,
            _slot_unavailable_writer("Which reported substrate category was documented?"),
        ],
        [],
        generation_attempt=_alternative_attempt(seeded, run_id, primary),
    )
    assert seeded.call_roles(run_id).count("extractor") == 2


def test_a_banked_candidate_is_revalidated_before_it_is_served(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path, char_cap=120, overlap_chars=0)
    run_id = "bank-revalidate"
    author = _two_candidate_author()
    author[1] = _slot_unavailable_writer("What reported water depth was documented?")
    primary = seeded.generate(
        run_id, author[:2], [], generation_attempt=_primary_attempt(seeded, run_id)
    )
    rows = _bank_rows(seeded, run_id)
    banked = json.loads(rows[1]["candidate_json"])
    banked["answer"]["source_span_id"] = "finding-evidence-span-v3-forged"
    with seeded.db.transaction():
        seeded.db.connection.execute(
            "UPDATE finding_bank SET candidate_json=? WHERE bank_row_id=?",
            (json.dumps(banked), rows[1]["bank_row_id"]),
        )
    second_extraction = fixture_events("fake-author.jsonl")[0]
    second_extraction["response"]["candidate_findings"] = [_second_candidate()]
    alternative = seeded.generate(
        run_id,
        [
            second_extraction,
            _slot_unavailable_writer("Which reported substrate category was documented?"),
        ],
        [],
        generation_attempt=_alternative_attempt(seeded, run_id, primary),
    )
    assert alternative["answer"]["text"] == "gravel"
    assert seeded.call_roles(run_id).count("extractor") == 2
    statuses = {
        row["bank_row_id"]: (row["admission_status"], row["admission_reason_code"])
        for row in _bank_rows(seeded, run_id)
    }
    assert statuses[rows[1]["bank_row_id"]] == (
        "rejected",
        "finding_evidence_span_not_found",
    )


def test_a_study_internal_bank_serves_no_admissible_finding_and_buys_nothing(
    tmp_path: Path,
) -> None:
    seeded = Seeded(tmp_path, char_cap=120, overlap_chars=0)
    run_id = "bank-internal"
    author = _two_candidate_author()
    author[0]["response"]["candidate_findings"][1]["answer_basis_class"] = (
        "study_internal_index"
    )
    author[1] = _slot_unavailable_writer("What reported water depth was documented?")
    primary = seeded.generate(
        run_id, author[:2], [], generation_attempt=_primary_attempt(seeded, run_id)
    )
    with pytest.raises(CandidateRejectedError) as error:
        seeded.generate(
            run_id,
            [fixture_events("fake-author.jsonl")[0]],
            [],
            generation_attempt=_alternative_attempt(seeded, run_id, primary),
        )
    assert error.value.reason_code == "no_admissible_finding"
    assert seeded.call_roles(run_id).count("extractor") == 1


# Ranking and the admission checks, at the unit level.


def _chunks(text: str) -> list[dict]:
    return [
        {
            "chunk_id": "chunk-1",
            "section_id": "section-1",
            "heading": "Results",
            "page": 1,
            "text": text,
        }
    ]


def _candidate(rank: int, span_id: str, basis: str, **extra: object) -> dict:
    return {
        "rank": rank,
        "admissible": True,
        "answer_basis_class": basis,
        "source_blind_answer_basis": "A reader can bound it from domain knowledge.",
        "answer": {
            "text": "15 percent",
            "source_span_id": span_id,
            "scope": {"method": "sulfate aerosol mass"},
            "required_question_phrases": ["sulfate aerosol mass"],
            "claim_type": "observation",
            "selection_rationale": "prose sentence",
        },
        "ranking_rationale": f"rank {rank}",
        **extra,
    }


def test_a_study_internal_index_is_ranked_last_whatever_its_rank() -> None:
    chunks = _chunks(FINDING_SENTENCE)
    spans = {span["span_id"]: span for span in generation._finding_spans(chunks[0])}
    span_id = next(iter(spans))
    candidates = [
        _candidate(1, span_id, "study_internal_index"),
        _candidate(2, span_id, "physical_magnitude"),
    ]
    answer, _, admission = generation._admit_ranked_finding(
        candidates, spans, chunks, None, [], excluded_span_ids=[]
    )
    assert admission["admitted_rank"] == 1
    assert admission["answer_basis_class"] == "physical_magnitude"
    assert [row["model_rank"] for row in admission["candidate_evaluations"]] == [2, 1]
    # The ranking fields never enter the frozen answer record.
    assert "answer_basis_class" not in answer
    assert "source_blind_answer_basis" not in answer


def test_only_a_study_internal_index_left_is_no_admissible_finding() -> None:
    chunks = _chunks(FINDING_SENTENCE)
    spans = {span["span_id"]: span for span in generation._finding_spans(chunks[0])}
    span_id = next(iter(spans))
    with pytest.raises(CandidateRejectedError) as error:
        generation._admit_ranked_finding(
            [_candidate(1, span_id, "study_internal_index")],
            spans,
            chunks,
            None,
            [],
            excluded_span_ids=[],
        )
    assert error.value.reason_code == "no_admissible_finding"
    assert "no_admissible_finding" in generation.FINDING_ADMISSION_REASK_REASONS


def test_a_self_declared_inadmissible_candidate_goes_behind_the_admissible() -> None:
    chunks = _chunks(FINDING_SENTENCE)
    spans = {span["span_id"]: span for span in generation._finding_spans(chunks[0])}
    span_id = next(iter(spans))
    ordered = generation._ordered_candidate_findings(
        [
            _candidate(1, span_id, "physical_magnitude", admissible=False),
            _candidate(2, span_id, "physical_magnitude"),
        ]
    )
    assert [row["rank"] for row in ordered] == [2, 1]


def test_the_extractor_schema_carries_the_ranking_fields_outside_the_answer() -> None:
    schema = generation.ROLE_SCHEMAS["extractor"]["properties"]["candidate_findings"][
        "items"
    ]
    assert {"admissible", "answer_basis_class", "source_blind_answer_basis"} <= set(
        schema["required"]
    )
    assert schema["properties"]["answer_basis_class"]["enum"] == list(
        generation.ANSWER_BASIS_CLASSES
    )
    assert "answer_basis_class" not in schema["properties"]["answer"]["properties"]


def test_the_second_admission_pass_is_skipped_when_every_span_is_excluded(
    tmp_path: Path, monkeypatch
) -> None:
    seeded = Seeded(tmp_path)
    author = fixture_events("fake-author.jsonl")
    author[0]["response"]["candidate_findings"][0]["answer"]["required_question_phrases"] = [
        "SMLcoupled depth"
    ]
    monkeypatch.setattr(
        generation, "_unexcluded_finding_spans", lambda spans, *exclusions: False
    )
    with pytest.raises(CandidateRejectedError) as error:
        seeded.generate("no-reask", author[:1], [])
    assert error.value.reason_code == "finding_required_phrase_artifact"
    assert "second admission pass was skipped" in str(error.value)
    assert seeded.call_roles("no-reask") == ["extractor"]


def test_the_second_admission_pass_still_runs_when_a_span_remains(
    tmp_path: Path,
) -> None:
    seeded = Seeded(tmp_path, char_cap=120, overlap_chars=0)
    author = fixture_events("fake-author.jsonl")
    first = json.loads(json.dumps(author[0]))
    first["response"]["candidate_findings"][0]["answer"]["required_question_phrases"] = [
        "SMLcoupled depth"
    ]
    second = json.loads(json.dumps(author[0]))
    second["response"]["candidate_findings"] = [_second_candidate()]
    candidate = seeded.generate(
        "reask",
        [
            first,
            second,
            _slot_unavailable_writer("Which reported substrate category was documented?"),
        ],
        [],
    )
    assert candidate["answer"]["text"] == "gravel"
    assert seeded.call_roles("reask") == ["extractor", "extractor", "question_writer"]
    rows = [
        (row["admission_status"], row["admission_reason_code"])
        for row in seeded.db.rows(
            "SELECT admission_status,admission_reason_code FROM finding_bank "
            "WHERE run_id=? ORDER BY rowid",
            ("reask",),
        )
    ]
    assert rows == [("rejected", "finding_required_phrase_artifact"), ("frozen", None)]


@pytest.mark.parametrize(
    ("quote", "phrases", "labelled", "expected"),
    [
        ("Iqaluit, Nanisivik Prochromadora sp. 3", ["Nanisivik"], False, "finding_span_is_table_or_caption"),
        ("thalma\nIqaluit, Nanisivik, Resolute Geomonhystera sp. 1", ["Resolute"], False, "finding_span_is_table_or_caption"),
        ("Iqaluit, Nanisivik Prochromadora sp. 3", ["Nanisivik"], True, None),
        (
            "Увеличение площади однолетних толстых льдов в северо-восточном районе "
            "Баренцева моря в апреле происходит вследствие их приноса через пролив "
            "Макарова из Арктического бассейна несколькими месяцами ранее.",
            ["однолетних толстых льдов"],
            False,
            None,
        ),
        (FINDING_SENTENCE, ["SMLcoupled clouds"], False, "finding_required_phrase_artifact"),
        (FINDING_SENTENCE, ["CHINARE 2010", "DBO3 in the Chukchi Sea"], False, None),
        (FINDING_SENTENCE, ["sulfate aerosol mass"], False, None),
    ],
)
def test_freeze_time_checks_re_derived_from_the_frozen_text(
    quote: str, phrases: list[str], labelled: bool, expected: str | None
) -> None:
    answer = {"evidence_quote": quote, "required_question_phrases": phrases}
    assert (
        generation._finding_admission_reason(answer, ["span-1"] if labelled else [])
        == expected
    )
    assert "finding_required_phrase_artifact" in generation.FINDING_ADMISSION_REASK_REASONS


def test_the_prescreen_records_a_shadow_verdict_and_never_blocks(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.generate(
        "prescreen", fixture_events("fake-author.jsonl"), fixture_events("fake-verifier.jsonl")
    )
    row = seeded.db.one(
        "SELECT * FROM finding_prescreen_shadow WHERE run_id=?", ("prescreen",)
    )
    verdict = json.loads(row["verdict_json"])
    assert verdict["contract_version"] == "structural-finding-prescreen-shadow-v1"
    assert verdict["mode"] == "shadow"
    assert verdict["would_reject"] is False
    assert verdict["qualifying_spans"] >= 1

    empty = generation._structural_prescreen(
        {"s1": {"span_id": "s1", "chunk_id": "c", "text": "Table 1. Site list."}},
        [{"chunk_id": "c", "heading": "Results"}],
    )
    assert empty["would_reject"] is True
    assert empty["qualifying_spans"] == 0
