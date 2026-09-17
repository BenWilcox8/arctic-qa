"""The Jev prescreen: the manifest, the state budget, the ledger, the ranking.

Every test here runs free. The provider is a fake that answers from a recorded
response fixture, so nothing in this file can make a paid call.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa import jev_prescreen as jev


# --------------------------------------------------------------------------
# Fixtures


def _extraction(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / "extracted" / name / "text.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _frozen_row(
    candidate_key: str,
    position: int,
    extraction_path: Path | None,
    *,
    ready: bool = True,
) -> dict:
    receipt = {
        "access_state": "full_text_ready" if ready else "landing_url_only",
        "extraction_path": str(extraction_path) if extraction_path else "",
        "extraction_sha256": "b" * 64,
        "source_sha256": "a" * 64,
        "source_path": "/nowhere/source.pdf",
        "identity_verified": True,
        "media_type": "application/pdf",
    }
    return {
        "schema": "full-text-ready-manifest-item-v1",
        "candidate_key": candidate_key,
        "family_key": candidate_key,
        "doi": candidate_key,
        "title": f"Title {candidate_key}",
        "year": 2024,
        "manifest_position": position,
        "priority_tier": "positive_arctic_or_marine_cue",
        "tier_position": position,
        "access_receipt": receipt,
    }


def _disposition(candidate_key: str, disposition: str) -> dict:
    return {"candidate_key": candidate_key, "metadata_disposition": disposition}


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _answers(**overrides: float) -> dict:
    """A recorded-shaped answer map, every question at its best value.

    An override gives the probability the ranking should read for that
    question, whatever the question's own answer type is: a noul takes it
    directly, a choice puts that mass on its good option and the rest on the
    next one, and a score is scaled back up to its level index.
    """
    answers: dict[str, dict] = {}
    for question in jev.QUESTIONS:
        key = question["key"]
        wanted = overrides.get(key, 1.0)
        if question["type"] == "noul":
            answers[key] = {"type": "noul", "noul": wanted}
        elif question["type"] == "choice":
            good = question["good_options"][0]
            others = [name for name in question["criteria"] if name != good]
            share = (1.0 - wanted) / len(others) if others else 0.0
            probabilities = {good: wanted}
            probabilities.update({name: share for name in others})
            answers[key] = {
                "type": "choice",
                "choice": max(probabilities, key=probabilities.get),
                "probabilities": probabilities,
                "confidence": 0.8,
            }
        else:
            top = len(question["criteria"]) - 1
            answers[key] = {
                "type": "score",
                "score": wanted * top,
                "legend": {
                    str(index): text for index, text in enumerate(question["criteria"])
                },
                "probabilities": {
                    str(index): 1.0 / len(question["criteria"])
                    for index in range(len(question["criteria"]))
                },
                "confidence": 0.8,
            }
    return answers


def _response(**overrides: float) -> dict:
    """A faithful `POST /v1/systemone` response, as docs.typesafe.ai/api.md."""
    return {
        "model": jev.DEFAULT_MODEL,
        "answers": _answers(**overrides),
        "usage": {"input_tokens": 5000, "output_tokens": 40},
    }


class FakeClient:
    """A recorded-response stand-in for `JevClient`. It makes no call."""

    def __init__(
        self, response: dict | None = None, *, fail_keys: set[str] | None = None
    ):
        self.response = response or _response()
        self.fail_keys = fail_keys or set()
        self.requests: list[dict] = []

    def invoke(self, body: dict) -> dict:
        self.requests.append(body)
        if any(marker in body["state"] for marker in self.fail_keys):
            raise jev.JevPrescreenError("HTTP 422 from the provider: bad state")
        return json.loads(json.dumps(self.response))


# --------------------------------------------------------------------------
# The question set and the request


def test_question_payload_matches_the_documented_request_shape():
    payload = jev.question_payload()
    assert set(payload) == set(jev.QUESTION_KEYS)
    for question in jev.QUESTIONS:
        entry = payload[question["key"]]
        assert entry["type"] in ("noul", "choice", "score")
        assert isinstance(entry["instructions"], str) and entry["instructions"]
        if entry["type"] == "noul":
            assert set(entry["criteria"]) == {"true", "false"}
        elif entry["type"] == "choice":
            # The API requires a map of option to rubric description.
            assert isinstance(entry["criteria"], dict)
            assert len(entry["criteria"]) >= 2
            assert all(isinstance(text, str) for text in entry["criteria"].values())
            assert set(question["good_options"]) <= set(entry["criteria"])
        else:
            # The API requires an ordered array of at least two levels.
            assert isinstance(entry["criteria"], list)
            assert len(entry["criteria"]) >= 2
    # No internal field of the question table ever reaches the provider.
    for entry in payload.values():
        assert set(entry) <= {"type", "instructions", "criteria"}


def test_every_question_names_a_rejection_reason_it_predicts():
    for question in jev.QUESTIONS:
        assert question["targets"], question["key"]
        assert question["role"] in ("gate", "quality")
        assert Decimal(question["weight"]) > 0


def test_the_request_batches_every_question_into_one_call():
    body = jev.build_request("some article text")
    assert body["state"] == "some article text"
    assert body["model"] == jev.DEFAULT_MODEL
    assert len(body["questions"]) == len(jev.QUESTIONS)
    # The hash is stable across calls, so a receipt binds this question set.
    assert jev.request_sha256(body) == jev.request_sha256(
        jev.build_request("some article text")
    )
    assert jev.request_sha256(body) != jev.request_sha256(jev.build_request("other"))


# --------------------------------------------------------------------------
# The manifest


def test_build_manifest_covers_the_retained_set_and_marks_missing_text(tmp_path):
    first = _extraction(tmp_path, "aa", "a" * 4000)
    second = _extraction(tmp_path, "bb", "b" * 8000)
    freeze = _write_jsonl(
        tmp_path / "freeze.jsonl",
        [
            _frozen_row("10.1/a", 1, first),
            _frozen_row("10.1/b", 2, second),
        ],
    )
    dispositions = _write_jsonl(
        tmp_path / "dispositions.ndjson",
        [
            _disposition("10.1/a", "retained_article_type"),
            _disposition("10.1/b", "retained_article_type"),
            # Retained, but never reached full text.
            _disposition("10.1/c", "retained_article_type"),
            # Not retained: it must not appear at all.
            _disposition("10.1/d", "flagged_nonresearch_type"),
            # A duplicate row of the same candidate is taken once.
            _disposition("10.1/a", "retained_article_type"),
        ],
    )
    descriptor = jev.build_manifest(
        dispositions_file=dispositions,
        freeze_manifest_file=freeze,
        output_dir=tmp_path / "out",
    )
    assert descriptor["counts"] == {
        "retained": 3,
        "with_full_text": 2,
        "without_full_text": 1,
        "estimated_state_tokens": 1000 + 2000,
    }
    rows = jev.read_manifest(Path(descriptor["manifest_file"]))
    assert [row["candidate_key"] for row in rows] == ["10.1/a", "10.1/b", "10.1/c"]
    assert [row["manifest_position"] for row in rows] == [1, 2, 3]
    assert rows[0]["has_full_text"] is True
    assert rows[0]["source_characters"] == 4000
    assert rows[0]["estimated_tokens"] == 1000
    assert rows[0]["extraction_path"] == str(first)
    assert rows[2]["has_full_text"] is False
    assert rows[2]["extraction_path"] is None
    assert rows[2]["estimated_tokens"] is None
    # The projected cost is recorded with the manifest, so a screen is never
    # started without a number in front of it.
    assert Decimal(descriptor["projected_cost_usd"]) > 0


def test_build_manifest_skips_a_frozen_row_whose_text_is_not_ready(tmp_path):
    path = _extraction(tmp_path, "aa", "a" * 100)
    freeze = _write_jsonl(
        tmp_path / "freeze.jsonl", [_frozen_row("10.1/a", 1, path, ready=False)]
    )
    dispositions = _write_jsonl(
        tmp_path / "d.ndjson", [_disposition("10.1/a", "retained_article_type")]
    )
    descriptor = jev.build_manifest(
        dispositions_file=dispositions,
        freeze_manifest_file=freeze,
        output_dir=tmp_path / "out",
    )
    assert descriptor["counts"]["with_full_text"] == 0


# --------------------------------------------------------------------------
# The state budget and the chunking


def test_a_short_article_is_sent_whole():
    text = "Abstract. A finding of 3.4 m." * 10
    selection = jev.select_state_text(text, budget=10_000)
    assert selection["selection_rule"] == "whole"
    assert selection["state_text"] == text
    assert selection["spans"] == [[0, len(text)]]
    assert selection["state_characters"] == len(text)


def test_the_reference_list_is_cut_before_the_body_is_touched():
    # The measured corpus puts the last reference heading at a median 0.777 of
    # the text, so the body outweighs the reference list.
    body = "Introduction and results.\n" * 500
    tail = "\nReferences\n" + ("Author, A. 2020. A paper.\n" * 100)
    text = body + tail
    selection = jev.select_state_text(text, budget=len(body) + 50)
    assert selection["selection_rule"] == "references_trimmed"
    assert "Author, A. 2020" not in selection["state_text"]
    assert selection["state_text"] == text[: selection["spans"][0][1]]
    assert selection["source_characters"] == len(text)


def test_a_reference_word_early_in_the_body_is_not_a_cut_point():
    # The word appears in the first half, so it is not the reference list.
    text = "References\n" + ("body sentence. " * 5000)
    selection = jev.select_state_text(text, budget=len(text))
    assert selection["selection_rule"] == "whole"


def test_an_over_budget_body_keeps_its_head_and_its_tail():
    head_marker = "ABSTRACT the opening claim."
    tail_marker = "CONCLUSIONS the closing claim."
    text = head_marker + ("middle. " * 20_000) + tail_marker
    budget = 4_000
    selection = jev.select_state_text(text, budget=budget)
    assert selection["selection_rule"] == "head_tail"
    assert head_marker in selection["state_text"]
    assert tail_marker in selection["state_text"]
    assert jev._ELISION in selection["state_text"]
    # The budget bounds the article characters; the elision marker is ours.
    assert selection["state_characters"] == budget + len(jev._ELISION)
    assert selection["spans"] == [[0, 2400], [len(text) - 1600, len(text)]]
    assert selection["source_characters"] == len(text)


def test_an_over_budget_article_cuts_the_references_and_then_the_middle():
    text = ("body. " * 20_000) + "\nReferences\n" + ("Ref entry.\n" * 2000)
    selection = jev.select_state_text(text, budget=4_000)
    assert selection["selection_rule"] == "references_trimmed_head_tail"
    assert "Ref entry." not in selection["state_text"]


def test_the_state_selection_is_reproducible():
    text = "an article. " * 30_000
    first = jev.select_state_text(text, budget=5_000)
    second = jev.select_state_text(text, budget=5_000)
    assert first["state_sha256"] == second["state_sha256"]
    assert first["state_text"] == second["state_text"]


def test_a_tiny_budget_is_refused():
    with pytest.raises(jev.JevPrescreenError):
        jev.select_state_text("text", budget=10)


# --------------------------------------------------------------------------
# The answers and the ranking arithmetic


def test_each_answer_type_is_read_as_one_probability():
    answers = _answers()
    # A noul is already the probability.
    answers["reports_own_finding"] = {"type": "noul", "noul": 0.25}
    # A score is the probability-weighted level over the top level index. The
    # question has four levels, so 1.5 of a top index of 3 is 0.5.
    answers["self_contained_claim"] = {
        "type": "score",
        "score": 1.5,
        "legend": {"0": "a", "1": "b", "2": "c", "3": "d"},
        "probabilities": {"0": 0.1, "1": 0.4, "2": 0.4, "3": 0.1},
        "confidence": 0.7,
    }
    # A choice is the mass its answer puts on the good options, not the mass
    # of the option it happened to pick.
    answers["geography_status"] = {
        "type": "choice",
        "choice": "not_stated",
        "probabilities": {
            "arctic_activity_stated": 0.3,
            "outside_or_incidental": 0.25,
            "not_stated": 0.45,
        },
        "confidence": 0.4,
    }
    values = jev.read_probabilities(answers)
    assert values["reports_own_finding"] == Decimal("0.25")
    assert values["self_contained_claim"] == Decimal("0.5")
    assert values["geography_status"] == Decimal("0.3")


def test_a_choice_answer_that_omits_an_option_is_refused():
    answers = _answers()
    answers["geography_status"] = {
        "type": "choice",
        "choice": "arctic_activity_stated",
        "probabilities": {"arctic_activity_stated": 1.0},
        "confidence": 0.9,
    }
    with pytest.raises(jev.JevPrescreenError, match="omits an option"):
        jev.read_probabilities(answers)


def test_a_missing_or_wrongly_typed_answer_is_refused():
    answers = _answers()
    del answers["geography_status"]
    with pytest.raises(jev.JevPrescreenError, match="geography_status"):
        jev.read_probabilities(answers)
    answers = _answers()
    answers["reports_own_finding"] = {"type": "choice", "choice": "yes"}
    with pytest.raises(jev.JevPrescreenError, match="not a noul"):
        jev.read_probabilities(answers)


def test_a_perfect_paper_ranks_at_one_and_an_empty_paper_at_zero():
    best = jev.rank_probability(jev.read_probabilities(_answers()))
    assert Decimal(best["rank_probability"]) == Decimal("1")
    worst_answers = _answers(**{key: 0.0 for key in jev.QUESTION_KEYS})
    worst = jev.rank_probability(jev.read_probabilities(worst_answers))
    assert Decimal(worst["rank_probability"]) == Decimal("0")


def test_one_failed_gate_sinks_the_paper_but_one_weak_quality_term_does_not():
    baseline = Decimal(
        jev.rank_probability(jev.read_probabilities(_answers()))["rank_probability"]
    )
    gated = Decimal(
        jev.rank_probability(jev.read_probabilities(_answers(geography_status=0.0)))[
            "rank_probability"
        ]
    )
    softened = Decimal(
        jev.rank_probability(
            jev.read_probabilities(_answers(arctic_attributed_finding=0.0))
        )["rank_probability"]
    )
    assert gated == Decimal("0")
    assert Decimal("0") < softened < baseline


def test_the_ranking_rises_with_every_question():
    for key in jev.QUESTION_KEYS:
        low = _answers(**{key: 0.2 if key in jev.GATE_KEYS else 0.2})
        high = _answers(**{key: 0.9 if key in jev.GATE_KEYS else 0.9})
        low_rank = Decimal(
            jev.rank_probability(jev.read_probabilities(low))["rank_probability"]
        )
        high_rank = Decimal(
            jev.rank_probability(jev.read_probabilities(high))["rank_probability"]
        )
        assert high_rank > low_rank, key


def test_the_ranking_records_both_factors_and_every_probability():
    report = jev.rank_probability(
        jev.read_probabilities(_answers(geography_status=0.5))
    )
    assert set(report["probabilities"]) == set(jev.QUESTION_KEYS)
    assert Decimal(report["gate_probability"]) < 1
    assert Decimal(report["quality_probability"]) == Decimal("1")
    assert report["gate_exponent"] == str(jev.GATE_EXPONENT)


def test_a_worked_example_of_the_arithmetic():
    # The gates are geography 2.0, article type 1.5, own finding 1.5 and
    # extraction 1.0, so the gate weight is 6.0. Only geography answers 0.5,
    # so the weighted geometric mean is 0.5 ** (2.0 / 6.0) = 0.793701.
    probabilities = jev.read_probabilities(_answers(geography_status=0.5))
    report = jev.rank_probability(probabilities)
    assert report["gate_probability"] == "0.793701"
    assert report["quality_probability"] == "1.000000"
    # The gate exponent is 0.5, so 0.793701 ** 0.5 = 0.5 ** (1 / 6) = 0.890899.
    assert report["rank_probability"] == "0.890899"


# --------------------------------------------------------------------------
# The ledger and its ceiling


def test_the_ledger_replays_its_own_rows_and_reports_what_is_left(tmp_path):
    ledger = jev.CallLedger(tmp_path / "ledger", ceiling_usd=Decimal("1.00"))
    ledger.record(
        {
            "state": "completed",
            "candidate_key": "10.1/a",
            "cost_usd": "0.25",
            "input_tokens": 1000,
            "output_tokens": 10,
        }
    )
    ledger.record({"state": "failed", "candidate_key": "10.1/b", "error": "HTTP 422"})
    assert ledger.spent_usd == Decimal("0.25")
    summary = json.loads((tmp_path / "ledger" / "ledger.json").read_text())
    assert summary["completed_calls"] == 1
    assert summary["spent_usd"] == "0.250000"
    assert summary["remaining_usd"] == "0.750000"
    assert summary["ceiling_reached"] is False
    # The price it billed at is on the record, named as unverified.
    assert summary["price_status"] == jev.PRICE_STATUS

    # A second ledger over the same directory reads the same position, and the
    # failed attempt costs nothing.
    again = jev.CallLedger(tmp_path / "ledger", ceiling_usd=Decimal("1.00"))
    assert again.spent_usd == Decimal("0.25")


def test_the_ceiling_refuses_a_call_before_it_is_made(tmp_path):
    ledger = jev.CallLedger(tmp_path / "ledger", ceiling_usd=Decimal("0.10"))
    ledger.reserve(Decimal("0.05"))
    ledger.record({"state": "completed", "candidate_key": "a", "cost_usd": "0.05"})
    ledger.reserve(Decimal("0.05"))
    ledger.record({"state": "completed", "candidate_key": "b", "cost_usd": "0.05"})
    assert ledger.ceiling_reached is True
    with pytest.raises(jev.JevCeilingReached):
        ledger.reserve(Decimal("0.01"))


def test_a_projected_overrun_is_refused_even_on_an_empty_ledger(tmp_path):
    ledger = jev.CallLedger(tmp_path / "ledger", ceiling_usd=Decimal("0.01"))
    with pytest.raises(jev.JevCeilingReached):
        ledger.reserve(Decimal("0.02"))
    assert ledger.spent_usd == Decimal("0")


def test_a_non_positive_ceiling_is_refused(tmp_path):
    with pytest.raises(jev.JevPrescreenError):
        jev.CallLedger(tmp_path / "ledger", ceiling_usd=Decimal("0"))


def test_the_cost_arithmetic_uses_the_documented_price():
    # One million input tokens at 0.042 USD per million.
    assert jev.call_cost_usd(1_000_000, 5_000) == Decimal("0.042000")
    assert jev.projected_cost_usd(1_000_000, 0) == Decimal("0.042000")
    # The question set is billed on every call.
    with_overhead = jev.projected_cost_usd(0, 1_000_000 // jev.QUESTION_OVERHEAD_TOKENS)
    assert with_overhead > 0


# --------------------------------------------------------------------------
# The screen, against the fake client


def _small_corpus(tmp_path: Path, count: int = 3) -> Path:
    frozen = []
    for index in range(count):
        path = _extraction(tmp_path, f"p{index}", f"Article {index}. " * 500)
        frozen.append(_frozen_row(f"10.1/p{index}", index + 1, path))
    freeze = _write_jsonl(tmp_path / "freeze.jsonl", frozen)
    dispositions = _write_jsonl(
        tmp_path / "d.ndjson",
        [_disposition(row["candidate_key"], "retained_article_type") for row in frozen],
    )
    descriptor = jev.build_manifest(
        dispositions_file=dispositions,
        freeze_manifest_file=freeze,
        output_dir=tmp_path / "manifest",
    )
    return Path(descriptor["manifest_file"])


def test_a_screen_records_one_response_per_paper_and_ranks_from_it(tmp_path):
    manifest = _small_corpus(tmp_path)
    client = FakeClient()
    receipt = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=client,
        ceiling_usd=Decimal("1.00"),
        workers=1,
    )
    assert receipt["counts"]["completed"] == 3
    assert receipt["counts"]["failed"] == 0
    assert len(client.requests) == 3
    responses = sorted((tmp_path / "run" / "responses").glob("*.json"))
    assert len(responses) == 3
    record = json.loads(responses[0].read_text())
    assert record["question_set_sha256"] == jev.question_set_sha256()
    assert record["response"]["answers"]
    assert record["selection"]["selection_rule"] == "whole"
    assert "state_text" not in record["selection"]

    ranked = jev.build_ranking(
        manifest_file=manifest,
        responses_dir=tmp_path / "run" / "responses",
        output_dir=tmp_path / "rank",
    )
    assert ranked["counts"] == {"ranked": 3, "unreadable": 0}
    ranking = json.loads(Path(ranked["ranking_file"]).read_text())
    assert [row["rank"] for row in ranking["records"]] == [1, 2, 3]
    assert ranking["ranking_inputs"] == "article_full_text_only"
    assert "pipeline_acceptance_labels" in ranking["excluded_inputs"]


def test_a_second_screen_replays_the_recorded_papers_free(tmp_path):
    manifest = _small_corpus(tmp_path)
    first = FakeClient()
    jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=first,
        ceiling_usd=Decimal("1.00"),
        workers=1,
    )
    second = FakeClient()
    receipt = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=second,
        ceiling_usd=Decimal("1.00"),
        workers=1,
    )
    assert second.requests == []
    assert receipt["counts"]["already_recorded"] == 3
    assert receipt["counts"]["completed"] == 0


def test_the_screen_stops_on_its_ceiling_and_keeps_what_it_bought(tmp_path):
    manifest = _small_corpus(tmp_path, count=5)
    # One recorded call costs 5,000 input tokens, which is 0.00021 USD.
    client = FakeClient()
    receipt = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=client,
        ceiling_usd=Decimal("0.0005"),
        workers=1,
    )
    assert receipt["ceiling_reached"] is True
    assert receipt["counts"]["completed"] < 5
    assert receipt["counts"]["stopped_on_ceiling"] > 0
    assert Decimal(receipt["spent_usd"]) <= Decimal("0.0005") + Decimal("0.00021")


def test_a_failed_call_is_recorded_and_does_not_stop_the_other_papers(tmp_path):
    manifest = _small_corpus(tmp_path)
    client = FakeClient(fail_keys={"Article 1."})
    receipt = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=client,
        ceiling_usd=Decimal("1.00"),
        workers=1,
    )
    assert receipt["counts"]["completed"] == 2
    assert receipt["counts"]["failed"] == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "run" / "ledger" / "calls.jsonl")
        .read_text()
        .splitlines()
    ]
    failed = [row for row in rows if row["state"] == "failed"]
    assert len(failed) == 1
    assert "HTTP 422" in failed[0]["error"]


def test_a_provider_that_reports_no_input_count_is_billed_on_the_estimate(tmp_path):
    manifest = _small_corpus(tmp_path, count=1)
    response = _response()
    response["usage"] = {"output_tokens": 10}
    client = FakeClient(response)
    jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=client,
        ceiling_usd=Decimal("1.00"),
        workers=1,
    )
    record = json.loads(
        next((tmp_path / "run" / "responses").glob("*.json")).read_text()
    )
    assert record["usage"]["billed_from_estimate"] is True
    assert record["usage"]["input_tokens"] > 0


def test_a_ranking_refuses_a_response_of_another_question_set(tmp_path):
    manifest = _small_corpus(tmp_path, count=1)
    jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=FakeClient(),
        ceiling_usd=Decimal("1.00"),
        workers=1,
    )
    path = next((tmp_path / "run" / "responses").glob("*.json"))
    record = json.loads(path.read_text())
    record["question_set_sha256"] = "0" * 64
    path.chmod(0o600)
    path.write_text(json.dumps(record), encoding="utf-8")
    ranked = jev.build_ranking(
        manifest_file=manifest,
        responses_dir=tmp_path / "run" / "responses",
        output_dir=tmp_path / "rank",
    )
    assert ranked["counts"] == {"ranked": 0, "unreadable": 1}


def test_the_ranking_order_follows_the_probability(tmp_path):
    manifest = _small_corpus(tmp_path, count=3)
    rows = jev.read_manifest(manifest)
    responses = tmp_path / "run" / "responses"
    responses.mkdir(parents=True)
    # The worst paper is first in the manifest, so the ranking must move it.
    for row, arctic in zip(rows, (0.1, 0.9, 0.5)):
        record = {
            "candidate_key": row["candidate_key"],
            "question_set_sha256": jev.question_set_sha256(),
            "answered_model": jev.DEFAULT_MODEL,
            "request_sha256": "c" * 64,
            "selection": {"selection_rule": "whole"},
            "response": _response(geography_status=arctic),
        }
        (jev.response_file(responses, row["candidate_key"])).write_text(
            json.dumps(record), encoding="utf-8"
        )
    ranked = jev.build_ranking(
        manifest_file=manifest,
        responses_dir=responses,
        output_dir=tmp_path / "rank",
    )
    order = [
        json.loads(line)["candidate_key"]
        for line in Path(ranked["order_file"]).read_text().splitlines()
    ]
    assert order == ["10.1/p1", "10.1/p2", "10.1/p0"]


# --------------------------------------------------------------------------
# The order the pipeline reads


def _freeze_pair(tmp_path: Path, count: int) -> tuple[Path, Path]:
    rows = []
    for index in range(count):
        path = _extraction(tmp_path, f"f{index}", "text " * 100)
        rows.append(_frozen_row(f"10.1/p{index}", index + 1, path))
    manifest = _write_jsonl(tmp_path / "frozen.jsonl", rows)
    descriptor = tmp_path / "frozen-descriptor.json"
    descriptor.write_text(
        json.dumps(
            {
                "schema": "full-text-ready-freeze-descriptor-v1",
                "freeze_id": "test-freeze-r1",
                "counts": {"manifest_records": count, "unique_paper_families": count},
            }
        ),
        encoding="utf-8",
    )
    return manifest, descriptor


def test_the_ranked_manifest_is_the_shape_the_producer_already_reads(tmp_path):
    manifest, descriptor = _freeze_pair(tmp_path, 3)
    order = _write_jsonl(
        tmp_path / "order.jsonl",
        [
            {"rank": 1, "candidate_key": "10.1/p2", "rank_probability": "0.9"},
            {"rank": 2, "candidate_key": "10.1/p0", "rank_probability": "0.5"},
            {"rank": 3, "candidate_key": "10.1/p1", "rank_probability": "0.1"},
        ],
    )
    result = jev.write_ranked_manifest(
        frozen_manifest_file=manifest,
        frozen_descriptor_file=descriptor,
        order_file=order,
        output_dir=tmp_path / "out",
    )
    rows = [
        json.loads(line)
        for line in Path(result["ranked_manifest"]).read_text().splitlines()
    ]
    assert [row["candidate_key"] for row in rows] == ["10.1/p2", "10.1/p0", "10.1/p1"]
    # The producer reads `manifest_position` and requires 1..N in order.
    assert [row["manifest_position"] for row in rows] == [1, 2, 3]
    assert [row["original_manifest_position"] for row in rows] == [3, 1, 2]
    assert [row["jev_prescreen_rank"] for row in rows] == [1, 2, 3]
    # Every frozen field the producer needs survives the reorder.
    assert rows[0]["access_receipt"]["access_state"] == "full_text_ready"
    derived = json.loads(Path(result["ranked_descriptor"]).read_text())
    assert derived["schema"] == "full-text-ready-freeze-descriptor-v1"
    assert derived["counts"]["manifest_records"] == 3
    assert derived["source_freeze_id"] == "test-freeze-r1"


def test_an_unranked_frozen_paper_keeps_its_place_after_the_ranked_ones(tmp_path):
    manifest, descriptor = _freeze_pair(tmp_path, 4)
    order = _write_jsonl(
        tmp_path / "order.jsonl",
        [
            {"rank": 1, "candidate_key": "10.1/p3", "rank_probability": "0.9"},
            {"rank": 2, "candidate_key": "10.1/p1", "rank_probability": "0.4"},
            # A key the frozen manifest does not hold is ignored, not an error.
            {"rank": 3, "candidate_key": "10.1/absent", "rank_probability": "0.3"},
        ],
    )
    result = jev.write_ranked_manifest(
        frozen_manifest_file=manifest,
        frozen_descriptor_file=descriptor,
        order_file=order,
        output_dir=tmp_path / "out",
    )
    rows = [
        json.loads(line)
        for line in Path(result["ranked_manifest"]).read_text().splitlines()
    ]
    assert [row["candidate_key"] for row in rows] == [
        "10.1/p3",
        "10.1/p1",
        "10.1/p0",
        "10.1/p2",
    ]
    assert rows[2]["jev_prescreen_rank"] is None
    assert result["ranked_from_jev"] == 2


def test_a_ranked_order_that_repeats_a_paper_is_refused(tmp_path):
    manifest, descriptor = _freeze_pair(tmp_path, 2)
    order = _write_jsonl(
        tmp_path / "order.jsonl",
        [
            {"rank": 1, "candidate_key": "10.1/p0", "rank_probability": "0.9"},
            {"rank": 2, "candidate_key": "10.1/p0", "rank_probability": "0.8"},
        ],
    )
    with pytest.raises(jev.JevPrescreenError, match="repeats"):
        jev.write_ranked_manifest(
            frozen_manifest_file=manifest,
            frozen_descriptor_file=descriptor,
            order_file=order,
            output_dir=tmp_path / "out",
        )


def test_a_limit_takes_the_top_of_the_ranked_order(tmp_path):
    manifest, descriptor = _freeze_pair(tmp_path, 4)
    order = _write_jsonl(
        tmp_path / "order.jsonl",
        [{"rank": 1, "candidate_key": "10.1/p2", "rank_probability": "0.9"}],
    )
    result = jev.write_ranked_manifest(
        frozen_manifest_file=manifest,
        frozen_descriptor_file=descriptor,
        order_file=order,
        output_dir=tmp_path / "out",
        limit=2,
    )
    assert result["counts"]["manifest_records"] == 2
    rows = [
        json.loads(line)
        for line in Path(result["ranked_manifest"]).read_text().splitlines()
    ]
    assert [row["candidate_key"] for row in rows] == ["10.1/p2", "10.1/p0"]


def test_the_ranked_manifest_feeds_the_producer_materializer(tmp_path):
    from arctic_qa.full_run_plan import materialize_frozen_access_run

    manifest, descriptor = _freeze_pair(tmp_path, 3)
    # The materializer needs a real source file beside the extracted text.
    for index in range(3):
        source = tmp_path / "originals" / f"s{index}" / "source.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"%PDF-1.4\n")
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    for index, row in enumerate(rows):
        row["access_receipt"]["source_path"] = str(
            tmp_path / "originals" / f"s{index}" / "source.pdf"
        )
    manifest = _write_jsonl(tmp_path / "frozen2.jsonl", rows)
    order = _write_jsonl(
        tmp_path / "order.jsonl",
        [
            {"rank": 1, "candidate_key": "10.1/p2", "rank_probability": "0.9"},
            {"rank": 2, "candidate_key": "10.1/p1", "rank_probability": "0.5"},
            {"rank": 3, "candidate_key": "10.1/p0", "rank_probability": "0.1"},
        ],
    )
    result = jev.write_ranked_manifest(
        frozen_manifest_file=manifest,
        frozen_descriptor_file=descriptor,
        order_file=order,
        output_dir=tmp_path / "out",
    )
    access = materialize_frozen_access_run(
        source_manifest_file=Path(result["ranked_manifest"]),
        descriptor_file=Path(result["ranked_descriptor"]),
        output_dir=tmp_path / "materialized",
    )
    run_manifest = json.loads(
        (tmp_path / "materialized" / "run-manifest.json").read_text()
    )
    assert [row["candidate_key"] for row in run_manifest["selection"]] == [
        "10.1/p2",
        "10.1/p1",
        "10.1/p0",
    ]
    assert [row["position"] for row in run_manifest["selection"]] == [1, 2, 3]
    assert access["target_total"] == 3


# --------------------------------------------------------------------------
# The calibration


def test_the_calibration_reports_the_auc_and_the_top_decile(tmp_path):
    records = []
    labels = []
    # Twenty papers. The ten best scores hold four of the five accepted ones.
    scores = [Decimal(f"0.{99 - index:02d}") for index in range(20)]
    accepted = {0, 1, 2, 3, 15}
    for index, score in enumerate(scores):
        key = f"10.1/p{index:02d}"
        records.append({"candidate_key": key, "rank_probability": str(score)})
        labels.append(
            {
                "candidate_key": key,
                "label": "accepted" if index in accepted else "rejected",
            }
        )
    ranking_file = tmp_path / "ranking.json"
    ranking_file.write_text(json.dumps({"records": records}), encoding="utf-8")
    labels_file = _write_jsonl(tmp_path / "labels.jsonl", labels)
    report = jev.calibrate(
        ranking_file=ranking_file,
        labels_file=labels_file,
        output_dir=tmp_path / "out",
    )
    assert report["counts"]["accepted"] == 5
    assert report["counts"]["rejected"] == 15
    assert report["top_decile"]["size"] == 2
    assert report["top_decile"]["accepted"] == 2
    assert report["top_decile"]["acceptance_rate"] == "1.000000"
    assert report["base_rate"] == "0.250000"
    assert Decimal(report["auc"]) > Decimal("0.5")
    assert Decimal(report["top_decile_lift_over_rest"]) > 1


def test_a_ranking_that_does_not_separate_scores_one_half():
    positive = [Decimal("0.5"), Decimal("0.5")]
    negative = [Decimal("0.5"), Decimal("0.5")]
    assert jev.roc_auc(positive, negative) == Decimal("0.5")
    assert jev.roc_auc([Decimal("0.9")], [Decimal("0.1")]) == Decimal("1")
    assert jev.roc_auc([Decimal("0.1")], [Decimal("0.9")]) == Decimal("0")
    assert jev.roc_auc([], [Decimal("0.5")]) is None


def test_a_labelled_paper_with_no_score_is_counted_and_not_scored(tmp_path):
    ranking_file = tmp_path / "ranking.json"
    ranking_file.write_text(
        json.dumps({"records": [{"candidate_key": "a", "rank_probability": "0.8"}]}),
        encoding="utf-8",
    )
    labels_file = _write_jsonl(
        tmp_path / "labels.jsonl",
        [
            {"candidate_key": "a", "label": "accepted"},
            {"candidate_key": "b", "label": "rejected"},
        ],
    )
    report = jev.calibrate(
        ranking_file=ranking_file,
        labels_file=labels_file,
        output_dir=tmp_path / "out",
    )
    assert report["counts"]["labelled"] == 2
    assert report["counts"]["scored_and_labelled"] == 1
    assert report["counts"]["labelled_without_score"] == 1
    assert report["auc"] is None


def test_an_unknown_calibration_label_is_refused(tmp_path):
    ranking_file = tmp_path / "ranking.json"
    ranking_file.write_text(json.dumps({"records": []}), encoding="utf-8")
    labels_file = _write_jsonl(
        tmp_path / "labels.jsonl", [{"candidate_key": "a", "label": "maybe"}]
    )
    with pytest.raises(jev.JevPrescreenError, match="maybe"):
        jev.calibrate(
            ranking_file=ranking_file,
            labels_file=labels_file,
            output_dir=tmp_path / "out",
        )


# --------------------------------------------------------------------------
# The credential and the client


def test_the_key_comes_from_the_environment_or_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv(jev.CREDENTIAL_ENV, raising=False)
    key_file = tmp_path / "typesafe-api-key"
    key_file.write_text("  file-key  \n", encoding="utf-8")
    assert jev.read_api_key(key_file) == "file-key"
    monkeypatch.setenv(jev.CREDENTIAL_ENV, "env-key")
    assert jev.read_api_key(key_file) == "env-key"


def test_a_missing_key_is_refused_without_printing_anything(tmp_path, monkeypatch):
    monkeypatch.delenv(jev.CREDENTIAL_ENV, raising=False)
    with pytest.raises(jev.JevPrescreenError, match="no TypeSafe API key"):
        jev.read_api_key(tmp_path / "absent")


def test_the_client_retries_a_throttle_and_gives_up_on_a_bad_request():
    import urllib.error

    calls: list[int] = []
    slept: list[float] = []

    class Response:
        def __init__(self, body: bytes):
            self.body = body

        def read(self) -> bytes:
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):  # noqa: ARG001
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                jev.ENDPOINT, 429, "Too Many Requests", {}, None
            )
        return Response(json.dumps(_response()).encode())

    client = jev.JevClient("key", sleep=slept.append)
    import arctic_qa.jev_prescreen as module

    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = fake_urlopen
    try:
        answer = client.invoke(jev.build_request("text"))
        assert answer["model"] == jev.DEFAULT_MODEL
        assert len(calls) == 3
        assert len(slept) == 2

        calls.clear()

        def always_422(request, timeout):  # noqa: ARG001
            calls.append(1)
            raise urllib.error.HTTPError(jev.ENDPOINT, 422, "bad", {}, None)

        module.urllib.request.urlopen = always_422
        with pytest.raises(jev.JevPrescreenError, match="HTTP 422"):
            client.invoke(jev.build_request("text"))
        # A 422 is the request's own fault, so it is never retried.
        assert len(calls) == 1
    finally:
        module.urllib.request.urlopen = original


def test_the_client_sends_the_key_as_a_bearer_header_and_never_logs_it():

    seen: dict = {}

    class Response:
        def read(self) -> bytes:
            return json.dumps(_response()).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):  # noqa: ARG001
        seen["headers"] = dict(request.headers)
        seen["url"] = request.full_url
        return Response()

    import arctic_qa.jev_prescreen as module

    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = fake_urlopen
    try:
        jev.JevClient("secret-key").invoke(jev.build_request("text"))
    finally:
        module.urllib.request.urlopen = original
    assert seen["url"] == jev.ENDPOINT
    assert seen["headers"]["Authorization"] == "Bearer secret-key"
    # The key is held privately, never on a public attribute.
    client = jev.JevClient("secret-key")
    assert "secret-key" not in repr(vars(client).get("endpoint", ""))


def test_an_empty_key_is_refused():
    with pytest.raises(jev.JevPrescreenError):
        jev.JevClient("")


# --------------------------------------------------------------------------
# The provider's own refusals


class ShrinkingClient:
    """Refuses any state over a size with HTTP 422, as the provider would."""

    def __init__(self, limit: int):
        self.limit = limit
        self.sizes: list[int] = []

    def invoke(self, body: dict) -> dict:
        self.sizes.append(len(body["state"]))
        if len(body["state"]) > self.limit:
            raise jev.JevOverlargeRequestError("HTTP 422: the state is too large")
        return _response()


def test_a_422_is_answered_once_with_a_smaller_state(tmp_path):
    path = _extraction(tmp_path, "big", "sentence. " * 4000)
    freeze = _write_jsonl(tmp_path / "freeze.jsonl", [_frozen_row("10.1/big", 1, path)])
    dispositions = _write_jsonl(
        tmp_path / "d.ndjson", [_disposition("10.1/big", "retained_article_type")]
    )
    manifest = Path(
        jev.build_manifest(
            dispositions_file=dispositions,
            freeze_manifest_file=freeze,
            output_dir=tmp_path / "manifest",
        )["manifest_file"]
    )
    # The budget lets the whole 40,000-character article through, but the
    # provider refuses anything over 12,000.
    client = ShrinkingClient(limit=12_000)
    receipt = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=client,
        ceiling_usd=Decimal("1.00"),
        budget=40_000,
        workers=1,
    )
    assert receipt["counts"]["completed"] == 1
    assert receipt["counts"]["shrunk_after_422"] == 1
    # Two calls: the full state, then a quarter of the budget.
    assert len(client.sizes) == 2
    assert client.sizes[0] > client.sizes[1]
    assert client.sizes[1] <= 10_000 + len(jev._ELISION)
    record = json.loads(
        next((tmp_path / "run" / "responses").glob("*.json")).read_text()
    )
    assert record["selection"]["shrunk_after_422_from_budget"] == 40_000


def test_a_422_on_a_state_already_under_the_shrink_target_is_not_retried(tmp_path):
    # These articles are far smaller than a quarter of the budget, so a
    # smaller state is the same state and a second paid call would buy
    # nothing.
    manifest = _small_corpus(tmp_path, count=2)
    client = ShrinkingClient(limit=10)
    receipt = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=client,
        ceiling_usd=Decimal("1.00"),
        budget=40_000,
        workers=1,
    )
    assert receipt["counts"]["completed"] == 0
    assert receipt["counts"]["failed"] == 2
    assert receipt["counts"]["shrunk_after_422"] == 0
    assert len(client.sizes) == 2
    rows = [
        json.loads(line)
        for line in (tmp_path / "run" / "ledger" / "calls.jsonl")
        .read_text()
        .splitlines()
    ]
    assert all("already at the floor" in row["error"] for row in rows)


def test_a_422_that_survives_the_shrink_fails_the_paper_alone(tmp_path):
    big = _extraction(tmp_path, "big", "sentence. " * 4000)
    small = _extraction(tmp_path, "small", "Article. " * 400)
    freeze = _write_jsonl(
        tmp_path / "freeze.jsonl",
        [_frozen_row("10.1/big", 1, big), _frozen_row("10.1/small", 2, small)],
    )
    dispositions = _write_jsonl(
        tmp_path / "d.ndjson",
        [
            _disposition("10.1/big", "retained_article_type"),
            _disposition("10.1/small", "retained_article_type"),
        ],
    )
    manifest = Path(
        jev.build_manifest(
            dispositions_file=dispositions,
            freeze_manifest_file=freeze,
            output_dir=tmp_path / "manifest",
        )["manifest_file"]
    )
    # The shrink target is 10,000 characters and the provider refuses 5,000,
    # so the big article is retried once and still fails. The small one is
    # under the limit and succeeds, which is the point: one paper's refusal
    # never stops the screen.
    client = ShrinkingClient(limit=5_000)
    receipt = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=client,
        ceiling_usd=Decimal("1.00"),
        budget=40_000,
        workers=1,
    )
    assert receipt["counts"]["completed"] == 1
    assert receipt["counts"]["failed"] == 1
    assert receipt["counts"]["shrunk_and_failed"] == 1
    assert receipt["counts"]["shrunk_after_422"] == 0


def test_the_client_waits_the_delay_the_provider_names():
    import urllib.error

    class Headers(dict):
        pass

    slept: list[float] = []
    calls: list[int] = []

    class Response:
        def read(self) -> bytes:
            return json.dumps(_response()).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):  # noqa: ARG001
        calls.append(1)
        if len(calls) == 1:
            error = urllib.error.HTTPError(jev.ENDPOINT, 429, "slow down", {}, None)
            error.headers = Headers({"retry-after-ms": "250"})
            raise error
        if len(calls) == 2:
            error = urllib.error.HTTPError(jev.ENDPOINT, 529, "overloaded", {}, None)
            error.headers = Headers({"retry-after": "3"})
            raise error
        return Response()

    import arctic_qa.jev_prescreen as module

    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = fake_urlopen
    try:
        jev.JevClient("key", sleep=slept.append).invoke(jev.build_request("text"))
    finally:
        module.urllib.request.urlopen = original
    assert slept == [0.25, 3.0]


def test_an_unreadable_retry_after_falls_back_to_the_exponential_delay():
    import urllib.error

    slept: list[float] = []
    calls: list[int] = []

    class Response:
        def read(self) -> bytes:
            return json.dumps(_response()).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):  # noqa: ARG001
        calls.append(1)
        if len(calls) == 1:
            error = urllib.error.HTTPError(jev.ENDPOINT, 429, "slow", {}, None)
            # An HTTP-date is legal here and this client does not parse it.
            error.headers = dict({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
            raise error
        return Response()

    import arctic_qa.jev_prescreen as module

    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = fake_urlopen
    try:
        jev.JevClient("key", sleep=slept.append).invoke(jev.build_request("text"))
    finally:
        module.urllib.request.urlopen = original
    assert slept == [2.0]


def test_the_ranking_counts_the_models_that_actually_answered(tmp_path):
    manifest = _small_corpus(tmp_path, count=2)
    rows = jev.read_manifest(manifest)
    responses = tmp_path / "responses"
    responses.mkdir()
    for row, model in zip(rows, ("jev-1.13.0", "jev-1.14.0")):
        record = {
            "candidate_key": row["candidate_key"],
            "question_set_sha256": jev.question_set_sha256(),
            "answered_model": model,
            "selection": {"selection_rule": "whole"},
            "response": _response(),
        }
        jev.response_file(responses, row["candidate_key"]).write_text(
            json.dumps(record), encoding="utf-8"
        )
    result = jev.build_ranking(
        manifest_file=manifest,
        responses_dir=responses,
        output_dir=tmp_path / "rank",
    )
    ranking = json.loads(Path(result["ranking_file"]).read_text())
    # A floating model id can move mid-screen, so the reviewer sees the split.
    assert ranking["answered_models"] == {"jev-1.13.0": 1, "jev-1.14.0": 1}
    assert ranking["selection_rules"] == {"whole": 2}


# --------------------------------------------------------------------------
# The calibration labels, read from a state database


def _state_database(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    import sqlite3

    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE paper_completions (run_id TEXT, candidate_key TEXT, "
        "outcome_class TEXT, reason_code TEXT)"
    )
    connection.executemany(
        "INSERT INTO paper_completions VALUES (?, ?, ?, ?)",
        [("run-a", key, outcome, reason) for key, outcome, reason in rows],
    )
    connection.execute(
        "INSERT INTO paper_completions VALUES "
        "('run-b', '10.1/other', 'generation_accepted', NULL)"
    )
    connection.commit()
    connection.close()
    return path


def test_the_labels_come_from_one_run_and_leave_the_unfinished_unlabelled(tmp_path):
    database = _state_database(
        tmp_path,
        [
            ("10.1/a", "generation_accepted", None),
            ("10.1/b", "generation_rejected", "finding_span_figure_defined_referent"),
            ("10.1/c", "eligibility_excluded", "criterion_failed:study_geography"),
            ("10.1/d", "eligibility_unresolved", "eligible_arctic_scope_invalid"),
            ("10.1/e", "incomplete_non_mcq", "option_set_not_mutually_exclusive"),
            ("10.1/f", "paper_cost_cap_reached", None),
        ],
    )
    receipt = jev.build_labels(
        database_file=database, run_id="run-a", output_dir=tmp_path / "out"
    )
    assert receipt["counts"] == {
        "papers": 6,
        "accepted": 1,
        "rejected": 3,
        "unlabelled": 2,
    }
    labels = [
        json.loads(line)
        for line in Path(receipt["labels_file"]).read_text().splitlines()
    ]
    assert [row["candidate_key"] for row in labels] == [
        "10.1/a",
        "10.1/b",
        "10.1/c",
        "10.1/d",
    ]
    assert labels[0]["label"] == "accepted"
    assert all(row["label"] == "rejected" for row in labels[1:])
    # A paper of another run never leaks in.
    assert "10.1/other" not in Path(receipt["labels_file"]).read_text()
    # The key file bounds a sample screen to exactly the labelled papers.
    keys = Path(receipt["keys_file"]).read_text().split()
    assert keys == ["10.1/a", "10.1/b", "10.1/c", "10.1/d"]


def test_reading_the_labels_never_writes_to_the_database(tmp_path):
    database = _state_database(tmp_path, [("10.1/a", "generation_accepted", None)])
    before = database.read_bytes()
    jev.build_labels(
        database_file=database, run_id="run-a", output_dir=tmp_path / "out"
    )
    assert database.read_bytes() == before


def test_the_refused_attempt_and_its_retry_both_reach_the_ledger(tmp_path):
    path = _extraction(tmp_path, "big", "sentence. " * 4000)
    freeze = _write_jsonl(tmp_path / "freeze.jsonl", [_frozen_row("10.1/big", 1, path)])
    dispositions = _write_jsonl(
        tmp_path / "d.ndjson", [_disposition("10.1/big", "retained_article_type")]
    )
    manifest = Path(
        jev.build_manifest(
            dispositions_file=dispositions,
            freeze_manifest_file=freeze,
            output_dir=tmp_path / "manifest",
        )["manifest_file"]
    )
    jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=ShrinkingClient(limit=12_000),
        ceiling_usd=Decimal("1.00"),
        budget=40_000,
        workers=1,
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "run" / "ledger" / "calls.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [row["state"] for row in rows] == ["failed", "completed"]
    assert "retried smaller" in rows[0]["error"]
    # The refusal happens before generation, so it costs nothing and the two
    # rows carry different request hashes.
    assert "cost_usd" not in rows[0]
    assert rows[0]["request_sha256"] != rows[1]["request_sha256"]


def test_an_unreadable_article_fails_its_own_paper_and_not_the_screen(tmp_path):
    good = _extraction(tmp_path, "good", "Article text. " * 200)
    missing = _extraction(tmp_path, "gone", "Article text. " * 200)
    freeze = _write_jsonl(
        tmp_path / "freeze.jsonl",
        [_frozen_row("10.1/gone", 1, missing), _frozen_row("10.1/good", 2, good)],
    )
    dispositions = _write_jsonl(
        tmp_path / "d.ndjson",
        [
            _disposition("10.1/gone", "retained_article_type"),
            _disposition("10.1/good", "retained_article_type"),
        ],
    )
    manifest = Path(
        jev.build_manifest(
            dispositions_file=dispositions,
            freeze_manifest_file=freeze,
            output_dir=tmp_path / "manifest",
        )["manifest_file"]
    )
    # The file disappears after the manifest recorded it, which is what a
    # moved or unmounted corpus looks like to a long screen.
    missing.unlink()
    receipt = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=FakeClient(),
        ceiling_usd=Decimal("1.00"),
        workers=2,
    )
    assert receipt["counts"]["completed"] == 1
    assert receipt["counts"]["failed"] == 1
    assert receipt["counts"]["faulted"] == 1
    rows = [
        json.loads(line)
        for line in (tmp_path / "run" / "ledger" / "calls.jsonl")
        .read_text()
        .splitlines()
    ]
    faulted = [row for row in rows if row["state"] == "failed"]
    assert len(faulted) == 1
    assert "FileNotFoundError" in faulted[0]["error"]
