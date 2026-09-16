"""Regressions for the chapter 2 yield audit routing and persistence defects.

Each test names the audit finding it closes. Routing decides which repair runs
and never whether an item is accepted, so none of these tests can admit an
item: every repaired candidate still re-runs the whole gate sequence and
consumes a path.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa import generation
from arctic_qa import streaming as routing
from arctic_qa.db import Database, now
from arctic_qa.errors import CandidateRejectedError
from arctic_qa.providers import ProviderResult
from arctic_qa.util import canonical_json, stable_id
from arctic_qa import validation
from arctic_qa.validation import scope_defect_records


REPO = Path(__file__).resolve().parents[1]
EVIDENCE_QUOTE = (
    "Fluxes of 12 mg were measured in humus soils along an elevational gradient "
    "in Abisko, Sweden during the 2016 growing season."
)


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate(tmp_path / "backups")
    return database


def _budget(database: Database) -> None:
    with database.transaction():
        database.connection.execute(
            """INSERT INTO budgets (run_id,mode,limit_value,reserved_value,
            spent_value,updated_at) VALUES (?,?,?,?,?,?)""",
            ("run", "tokens", "1000000", "0", "0", now()),
        )


def _candidate(**overrides: object) -> dict[str, object]:
    candidate = {
        "question": "What was the mean flux?",
        "question_context": "Fluxes were measured in humus soils in Abisko, Sweden.",
        "answer": {
            "text": "12 mg",
            "evidence_quote": EVIDENCE_QUOTE,
            "scope": {"geography": "Abisko, Sweden"},
        },
        "answer_verification": {},
        "standalone_verification": {},
        "provenance": {},
    }
    candidate.update(overrides)
    return candidate


def _path(candidate: dict[str, object], **attempt_overrides: object) -> dict:
    attempt = routing._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )
    attempt.update(attempt_overrides)
    return {
        "attempt": attempt,
        "candidate": {
            "item_id": "item-primary",
            "candidate_json": canonical_json(candidate),
        },
    }


def _next(paths: dict, failed: dict, reasons: list[str], **kwargs):
    return routing._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths=paths,
        failed_path=failed,
        reason_codes=reasons,
        **kwargs,
    )


# Audit 4.6 a / finding R2: the rejection now carries the scope field it
# objects to, the frozen string and the hash-bound span it was checked against.


def test_a_frozen_scope_value_the_evidence_omits_reports_not_in_evidence() -> None:
    """family-a2bb181f: six attempts never dropped "northern Sweden"."""
    candidate = _candidate()
    candidate["answer"]["scope"] = {"geography": "northern Sweden"}

    defects = scope_defect_records(candidate)

    assert [defect["field"] for defect in defects] == ["geography"]
    assert defects[0]["demand"] == "not_in_evidence"
    assert defects[0]["frozen_value"] == "northern Sweden"
    assert defects[0]["evidence_quote_span"] == EVIDENCE_QUOTE


def test_a_frozen_scope_value_no_reader_sees_reports_display_verbatim() -> None:
    candidate = _candidate()
    candidate["answer"]["scope"] = {"period": "2016 growing season"}

    defects = scope_defect_records(candidate)

    assert defects[0]["demand"] == "display_verbatim"
    assert defects[0]["frozen_value"] == "2016 growing season"


def test_a_bound_and_displayed_scope_value_reports_no_defect() -> None:
    assert scope_defect_records(_candidate()) == []


def test_the_verifier_contradiction_names_its_own_field() -> None:
    candidate = _candidate()
    candidate["answer_verification"] = {
        "scope_value_contradicted_by_source": True,
        "contradicted_scope_field": "geography",
        "scope_representation_note": "the span mentions 'Abisko, Sweden'",
    }

    defects = scope_defect_records(candidate)

    assert defects[0]["demand"] == "not_in_evidence"
    assert defects[0]["verifier_note"] == "the span mentions 'Abisko, Sweden'"


def test_the_scope_defect_block_reaches_the_writer_only_for_display_verbatim(
    tmp_path: Path,
) -> None:
    """Audit 4.10: the not_in_evidence writer instruction was refuted."""
    assert "display_verbatim" in generation.SCOPE_DEFECT_INSTRUCTIONS
    assert "not_in_evidence" not in generation.SCOPE_DEFECT_INSTRUCTIONS
    assert "Never invent a scope value" in generation.SCOPE_DEFECT_INSTRUCTIONS


# Audit 4.6 d / finding R3: one scope family, one layer, one pair of rungs.


@pytest.mark.parametrize("reason", sorted(routing.SCOPE_FAMILY_REASONS))
def test_every_scope_family_code_sits_in_the_context_layer(reason: str) -> None:
    assert routing._failure_layer(reason) == "context"
    assert routing._reason_family(reason) == "scope"
    assert reason in routing.REPAIRABLE_QUESTION_REASONS


def test_a_display_demand_routes_to_the_scope_display_repair() -> None:
    candidate = _candidate()
    candidate["answer"]["scope"] = {"period": "2016 growing season"}
    path = _path(candidate)

    repair = _next(
        {(1, 0): path},
        path,
        ["scope_qualifier_not_displayed"],
        evidence=routing._routing_evidence(path),
    )

    assert repair is not None
    assert repair["attempt_kind"] == "scope_display_repair"
    assert repair["question_revision_index"] == 1


def test_an_unsupported_frozen_value_routes_to_the_rebind_rung() -> None:
    """family-3480407b ended after one attempt with a mechanical trim unspent."""
    candidate = _candidate()
    candidate["answer"]["scope"] = {"geography": "northern Sweden"}
    path = _path(candidate)

    repair = _next(
        {(1, 0): path},
        path,
        ["answer_scope_not_source_bound"],
        evidence=routing._routing_evidence(path),
    )

    assert repair is not None
    assert repair["attempt_kind"] == "frozen_scope_rebind"


def test_the_rebind_rung_runs_once_and_never_becomes_a_question_rewrite() -> None:
    candidate = _candidate()
    candidate["answer"]["scope"] = {"geography": "northern Sweden"}
    path = _path(candidate)
    paths = {(1, 0): path}
    first = _next(
        paths,
        path,
        ["answer_scope_not_source_bound"],
        evidence=routing._routing_evidence(path),
    )
    assert first is not None
    paths[(1, 1)] = {"attempt": first, "candidate": path["candidate"]}

    second = _next(
        paths,
        paths[(1, 1)],
        ["answer_scope_not_source_bound"],
        evidence=routing._routing_evidence(path),
    )

    assert second is None or second["attempt_kind"] != "question_revision"


# Audit 4.6 b / finding R2: no retry on a rejection a rewrite cannot act on.


def _blind_candidate(**verification: object) -> dict[str, object]:
    candidate = _candidate()
    candidate["answer"]["scope"] = {}
    candidate["answer_verification"] = dict(verification)
    return candidate


def test_clean_judges_and_no_diagnostic_stop_the_family_for_gate_review() -> None:
    """families 3185c3a7 and b0a9366f: three Pro passes, empty diagnostics."""
    candidate = _blind_candidate(
        question_context_referent_resolved=True,
        relation_scope_match=True,
        scope_value_contradicted_by_source=False,
    )
    candidate["standalone_verification"] = {"pass": True}
    path = _path(candidate)
    unroutable: list[str] = []

    repair = _next(
        {(1, 0): path},
        path,
        ["scope_qualifier_not_displayed"],
        evidence=routing._routing_evidence(path),
        unroutable=unroutable,
    )

    assert repair is None
    assert unroutable == ["gate_contradiction_unroutable"]


def test_a_named_defect_with_no_displayed_words_stops_as_an_empty_diagnostic() -> None:
    candidate = _blind_candidate(question_context_referent_resolved=False)
    candidate["standalone_verification"] = {"pass": False}
    path = _path(candidate)
    unroutable: list[str] = []

    repair = _next(
        {(1, 0): path},
        path,
        ["standalone_undefined_location"],
        evidence=routing._routing_evidence(path),
        unroutable=unroutable,
    )

    assert repair is None
    assert unroutable == ["empty_diagnostic_unroutable"]


def test_a_rejection_that_names_a_phrase_still_earns_its_repair() -> None:
    candidate = _blind_candidate()
    candidate["standalone_verification"] = {
        "pass": False,
        "unresolved_phrases": ["the southern station"],
    }
    path = _path(candidate)
    unroutable: list[str] = []

    repair = _next(
        {(1, 0): path},
        path,
        ["standalone_undefined_location"],
        evidence=routing._routing_evidence(path),
        unroutable=unroutable,
    )

    assert repair is not None
    assert unroutable == []


def test_a_self_describing_code_is_never_stopped_by_the_guard() -> None:
    candidate = _blind_candidate()
    path = _path(candidate)
    unroutable: list[str] = []

    repair = _next(
        {(1, 0): path},
        path,
        ["question_context_missing"],
        evidence=routing._routing_evidence(path),
        unroutable=unroutable,
    )

    assert repair is not None
    assert unroutable == []


def test_the_guard_never_stops_an_option_repair() -> None:
    """option_repair reuses the verified question and went 5 for 6."""
    candidate = _blind_candidate()
    path = _path(candidate)
    unroutable: list[str] = []

    repair = _next(
        {(1, 0): path},
        path,
        ["insufficient_verified_distractors"],
        evidence=routing._routing_evidence(path),
        unroutable=unroutable,
    )

    assert repair is not None
    assert repair["attempt_kind"] == "option_repair"
    assert unroutable == []


def test_an_unroutable_outcome_can_never_earn_a_repair() -> None:
    for reason in routing.UNROUTABLE_OUTCOME_REASONS:
        assert reason not in routing.REPAIRABLE_QUESTION_REASONS
        assert reason not in routing.ALTERNATIVE_FINDING_REASONS
        assert reason not in routing.OPTION_REPAIR_REASONS
        assert reason not in routing.ANSWER_RULE_REPAIR_REASONS
        assert routing._failure_layer(reason) == "contract"


# Audit 4.6 d / finding R3: the numeric repair is a cost swap, not a competitor.


def test_the_numeric_repair_rides_along_with_the_question_repair() -> None:
    """family-7edb49fb hit the code twice and got a question rewrite both times."""
    path = _path(_candidate())

    repair = _next(
        {(1, 0): path},
        path,
        ["question_context_missing", "source_bound_numeric_rule_missing"],
    )

    assert repair is not None
    assert repair["trigger_reason_code"] == "question_context_missing"
    assert repair["repair_numeric_rule"] is True


def test_the_numeric_code_alone_still_earns_the_answer_rule_repair() -> None:
    path = _path(_candidate())

    repair = _next({(1, 0): path}, path, ["source_bound_numeric_rule_missing"])

    assert repair is not None
    assert repair["attempt_kind"] == "answer_rule_repair"
    assert repair["repair_numeric_rule"] is True


# Audit 4.6 e: the repeat detector spans the whole family lineage.


def test_the_repeat_counter_spans_a_finding_switch() -> None:
    """72 of 139 candidates re-failed on a defect their family had seen."""
    paths = {
        (1, 0): _path(_candidate()),
        (1, 1): _path(
            _candidate(), trigger_reason_code="standalone_undefined_location"
        ),
        (1, 2): _path(
            _candidate(),
            attempt_kind="context_widened_revision",
            trigger_reason_code="standalone_undefined_location",
        ),
    }

    assert routing._repeat_depth(paths, "referent_slot") == 2


def test_the_option_rung_is_exempt_from_the_lineage_counter() -> None:
    paths = {
        (1, 1): _path(
            _candidate(),
            attempt_kind="option_repair",
            trigger_reason_code="insufficient_verified_distractors",
        ),
    }

    assert routing._repeat_depth(paths, "insufficient_verified_distractors") == 0


def test_a_finding_that_has_not_widened_still_gets_its_one_widening() -> None:
    """Audit 4.6 e: the replay showed the counter would suppress this rung."""
    assert (
        routing._repair_kind(
            ["standalone_undefined_location"], 3, finding_has_widened=False
        )
        == "context_widened_revision"
    )
    assert (
        routing._repair_kind(
            ["standalone_undefined_location"], 3, finding_has_widened=True
        )
        is None
    )


# Audit 4.6 f / finding R6: a weak judge may not buy a repair cycle.


def test_a_flash_lite_disagreement_is_not_a_trigger() -> None:
    """family-2fa3406e: "24 species" against "24" bought a whole cycle."""
    candidate = _candidate(
        answer_agreement={
            "method": "llm_judge",
            "judge": {"requested_model": "gemini-3.1-flash-lite", "verdict": "no"},
        }
    )
    path = _path(candidate)

    assert (
        routing._authoritative_reason_codes(
            ["reconstruction_disagreement"], routing._routing_evidence(path)
        )
        == []
    )


def test_a_deterministic_disagreement_still_triggers_a_repair() -> None:
    candidate = _candidate(answer_agreement={"method": "deterministic", "judge": None})
    path = _path(candidate)

    assert routing._authoritative_reason_codes(
        ["reconstruction_disagreement"], routing._routing_evidence(path)
    ) == ["reconstruction_disagreement"]


def test_a_pro_disagreement_still_triggers_a_repair() -> None:
    candidate = _candidate(
        answer_agreement={
            "method": "llm_judge",
            "judge": {"requested_model": "gemini-3.1-pro-preview", "verdict": "no"},
        }
    )
    path = _path(candidate)

    assert routing._authoritative_reason_codes(
        ["reconstruction_disagreement"], routing._routing_evidence(path)
    ) == ["reconstruction_disagreement"]


# Audit 4.6 c / finding R1: the slot pool is the text the writer will see.


def test_the_slot_pool_reads_the_evidence_the_writer_received(
    tmp_path: Path,
) -> None:
    """family-e3d2c9790e: the expansion sat in the finding's own quote."""
    database = _database(tmp_path)
    candidate = _candidate()
    candidate["answer"]["evidence_quote"] = (
        "Hydroperoxymethyl thioformate (HPMTF) was measured at the station."
    )
    evidence = routing._routing_evidence(_path(candidate))

    slots = routing._slot_evidence_pool(database, "source-absent", evidence)

    assert slots is not None
    assert "acronym" in slots


def test_the_widened_patterns_read_a_season_and_a_counted_noun() -> None:
    """The closed noun list scored false on all 7 sample families."""
    slots = routing._slot_evidence_types(
        ["Counts of 24 species were made through the melt season."]
    )

    assert slots == frozenset({"period", "sample"})
    assert routing._slot_evidence_types(["this was less than before"]) == frozenset()


def test_an_unmet_demand_names_the_slot_the_lookup_must_ask_for() -> None:
    assert routing._unmet_slot_demands(
        ["standalone_undefined_period_or_event"], frozenset({"place"})
    ) == frozenset({"period"})
    assert (
        routing._unmet_slot_demands(
            ["standalone_undefined_period_or_event"], frozenset({"period"})
        )
        == frozenset()
    )


class _LookupProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def invoke(self, role, system, prompt, parameters, timeout):
        self.prompts.append(prompt)
        return ProviderResult(self.payload, self.model, "request-1", 1, 1, None)


def test_the_slot_lookup_returns_a_verbatim_sentence(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _budget(database)
    setting = "Sampling ran in Abisko, Sweden during the 2016 growing season."
    provider = _LookupProvider({"found": True, "quote": setting})

    record = generation.slot_lookup_quote(
        database,
        provider,
        run_id="run",
        entity_id="entity",
        slot="period",
        texts=[setting],
        answer={"text": "12 mg"},
        parameters={"temperature": 0, "max_tokens": 512},
        reservation=Decimal("1000"),
        timeout=5,
        retries=0,
        rate_limit_seconds=0,
    )

    assert record is not None
    assert record["slot"] == "period"
    assert record["quote"] == setting


def test_the_slot_lookup_refuses_a_sentence_that_is_not_in_the_pool(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _budget(database)
    provider = _LookupProvider({"found": True, "quote": "Invented in 1999."})

    record = generation.slot_lookup_quote(
        database,
        provider,
        run_id="run",
        entity_id="entity",
        slot="period",
        texts=[EVIDENCE_QUOTE],
        answer=None,
        parameters={"temperature": 0, "max_tokens": 512},
        reservation=Decimal("1000"),
        timeout=5,
        retries=0,
        rate_limit_seconds=0,
    )

    assert record is None


def test_the_slot_lookup_runs_behind_the_answer_leak_filter(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _budget(database)
    leaking = "The mean flux was 12 mg during the 2016 growing season."
    provider = _LookupProvider({"found": True, "quote": leaking})

    record = generation.slot_lookup_quote(
        database,
        provider,
        run_id="run",
        entity_id="entity",
        slot="period",
        texts=[leaking],
        answer={"text": "12 mg", "evidence_quote": leaking},
        parameters={"temperature": 0, "max_tokens": 512},
        reservation=Decimal("1000"),
        timeout=5,
        retries=0,
        rate_limit_seconds=0,
    )

    assert record is None


def test_a_found_slot_quote_travels_on_the_question_repair() -> None:
    candidate = _candidate()
    candidate["standalone_verification"] = {
        "pass": False,
        "unresolved_phrases": ["the growing season"],
    }
    path = _path(candidate)
    evidence = routing._routing_evidence(path)
    evidence["slot_lookup"] = {
        "slot": "period",
        "quote": EVIDENCE_QUOTE,
        "span_id": "span-1",
    }

    repair = _next(
        {(1, 0): path},
        path,
        ["standalone_undefined_period_or_event"],
        slot_evidence=frozenset({"period"}),
        evidence=evidence,
    )

    assert repair is not None
    assert repair["slot_lookup"]["quote"] == EVIDENCE_QUOTE


# Audit 4.9 C8 and 4.6 e: a candidate row for every generation call.


def _attempt() -> dict:
    return routing._generation_attempt(
        campaign_id="campaign",
        family_id="family",
        finding_attempt_index=1,
        question_revision_index=0,
        attempt_kind="primary",
        parent_attempt_id=None,
        parent_item_id=None,
        trigger_reason_code=None,
        excluded_finding_span_ids=[],
    )


def test_the_call_record_opens_before_the_call_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """family-c4aa016e's attempt vanished with no terminal record."""
    database = _database(tmp_path)
    attempt = _attempt()

    first = routing._open_generation_call_record(
        database,
        run_id="run",
        source_id="source",
        family_id="family",
        source_version_id="a" * 64,
        attempt=attempt,
    )
    second = routing._open_generation_call_record(
        database,
        run_id="run",
        source_id="source",
        family_id="family",
        source_version_id="a" * 64,
        attempt=attempt,
    )

    assert first == second
    rows = database.rows("SELECT item_id,status FROM candidates")
    assert len(rows) == 1
    assert rows[0]["status"] == "incomplete_infra"


def test_a_changed_source_version_opens_its_own_call_record() -> None:
    attempt = _attempt()

    first = routing._generation_call_record_id(
        "run", "family", "a" * 64, attempt["attempt_id"]
    )
    second = routing._generation_call_record_id(
        "run", "family", "b" * 64, attempt["attempt_id"]
    )

    assert first != second


def test_a_dead_call_settles_as_generation_incomplete(tmp_path: Path) -> None:
    """26 chapter 2 generation calls produced no row of any kind."""
    database = _database(tmp_path)
    item_id = routing._open_generation_call_record(
        database,
        run_id="run",
        source_id="source",
        family_id="family",
        source_version_id="a" * 64,
        attempt=_attempt(),
    )

    routing._settle_generation_call_record(
        database, item_id, state="generation_incomplete", reason_code="writer_failed"
    )

    row = database.one("SELECT status,candidate_json FROM candidates")
    assert row["status"] == "generation_incomplete"
    assert json.loads(row["candidate_json"])["reason_code"] == "writer_failed"


def test_an_open_call_record_stays_visible_for_infrastructure_diagnosis(
    tmp_path: Path,
) -> None:
    """families aeed4bfe and 446bb3c2 lost every completed call."""
    database = _database(tmp_path)
    routing._open_generation_call_record(
        database,
        run_id="run",
        source_id="source",
        family_id="family",
        source_version_id="a" * 64,
        attempt=_attempt(),
    )

    records = routing._incomplete_infra_records(database, "run", "family")

    assert len(records) == 1
    assert records[0]["status"] == "incomplete_infra"


def test_a_call_record_is_never_a_benchmark_item(tmp_path: Path) -> None:
    database = _database(tmp_path)
    routing._open_generation_call_record(
        database,
        run_id="run",
        source_id="source",
        family_id="family",
        source_version_id="a" * 64,
        attempt=_attempt(),
    )

    paths = routing._generation_paths(
        database, campaign_id="run", source_id="source", family_id="family"
    )

    assert paths == {}
    assert routing._generation_counts(database, "run")["qa_candidate_count"] == 0
    for status in routing.INCOMPLETE_CANDIDATE_STATUSES:
        assert status in routing.BENCHMARK_CANDIDATE_PREDICATE


def test_the_gate_review_flag_records_why_routing_stopped(tmp_path: Path) -> None:
    database = _database(tmp_path)
    attempt = _attempt()

    routing._record_gate_review_flag(
        database,
        campaign_id="run",
        candidate_key="paper",
        source_id="source",
        selected={},
        attempt=attempt,
        reason_code="gate_contradiction_unroutable",
        rejection_reason_codes=["scope_qualifier_not_displayed"],
    )

    row = database.one("SELECT reason_code,stage,detail_json FROM rejection_ledger")
    assert row["reason_code"] == "gate_contradiction_unroutable"
    assert row["stage"] == "generation_routing"
    assert json.loads(row["detail_json"])["gate_review_required"] is True


# Audit 4.6 a: the fourth correction component re-grounds the frozen scope.


class _ScopeCorrector:
    name = "fake"
    model = "fake-model"

    def __init__(self, replacement: object) -> None:
        self.replacement = replacement

    def invoke(self, role, system, prompt, parameters, timeout):
        return ProviderResult(
            {"component": "scope", "replacement": self.replacement},
            self.model,
            "request-1",
            1,
            1,
            None,
        )


def _rebind(database: Database, provider: object, scope: dict) -> dict:
    answer = {
        "text": "12 mg",
        "evidence_quote": EVIDENCE_QUOTE,
        "locator": {
            "chunk_id": "chunk-1",
            "start_offset": 0,
            "end_offset": len(EVIDENCE_QUOTE),
        },
        "required_question_phrases": ["mean flux"],
        "scope": scope,
        "claim_type": "observation",
        "selection_rationale": "one bounded observed flux",
    }
    parent = _candidate()
    parent["answer"] = answer
    return generation._rebound_scope_answer(
        database,
        provider,
        run_id="run",
        entity_id="entity",
        answer=answer,
        parent=parent,
        chunk={"chunk_id": "chunk-1", "text": EVIDENCE_QUOTE},
        interpretation_texts=[],
        arctic_scope=None,
        context="SOURCE_DATA\n" + EVIDENCE_QUOTE,
        parameters={"temperature": 0, "max_tokens": 512},
        reservation=Decimal("1000"),
        timeout=5,
        retries=0,
        rate_limit_seconds=0,
    )


def test_the_rebind_removes_an_unsupported_qualifier(tmp_path: Path) -> None:
    """family-a5bcbcf9's hallucinated period "February 2009"."""
    database = _database(tmp_path)
    _budget(database)

    repaired = _rebind(database, _ScopeCorrector({}), {"geography": "northern Sweden"})

    assert repaired["scope"] == {}
    assert repaired["evidence_quote"] == EVIDENCE_QUOTE


def test_the_rebind_accepts_a_value_the_frozen_span_states(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _budget(database)

    repaired = _rebind(
        database,
        _ScopeCorrector({"geography": "Abisko, Sweden"}),
        {"geography": "northern Sweden"},
    )

    assert repaired["scope"] == {"geography": "Abisko, Sweden"}


def test_the_rebind_refuses_a_value_the_evidence_does_not_state(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _budget(database)

    with pytest.raises(CandidateRejectedError) as error:
        _rebind(
            database,
            _ScopeCorrector({"geography": "northern Finland"}),
            {"geography": "northern Sweden"},
        )

    assert error.value.reason_code == "frozen_scope_rebind_invalid"


def test_the_rebind_refuses_a_dimension_the_frozen_record_never_carried(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _budget(database)

    with pytest.raises(CandidateRejectedError) as error:
        _rebind(
            database,
            _ScopeCorrector({"geography": "Abisko, Sweden", "period": "2016"}),
            {"geography": "northern Sweden"},
        )

    assert error.value.reason_code == "frozen_scope_rebind_invalid"


def test_the_rebind_refuses_to_return_the_same_unsupported_value(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _budget(database)

    with pytest.raises(CandidateRejectedError) as error:
        _rebind(
            database,
            _ScopeCorrector({"geography": "northern Sweden"}),
            {"geography": "northern Sweden"},
        )

    assert error.value.reason_code in {
        "frozen_scope_rebind_invalid",
        "frozen_scope_rebind_unchanged",
    }


# Audit 4.6 e: the replay that had to run before the detector shipped.


def test_the_chapter2_replay_suppresses_no_accepted_item() -> None:
    report = json.loads(
        (
            REPO / "research" / "arctic-ch3-routing-r1" / "replay-chapter2-routing.json"
        ).read_text(encoding="utf-8")
    )

    assert report["recorded_candidates"] == 139
    assert report["families"] == 56
    assert report["suppressed_accepted_items"] == []
    assert report["detector_suppressed_accepted_items"] == []


# Contract versions this slice owns.


def test_the_routing_contract_version_is_current() -> None:
    assert routing.GENERATION_ATTEMPT_CONTRACT_VERSION == ("bounded-failure-routing-v5")
    assert generation.ROUTING_CONTRACT_VERSION == "bounded-failure-routing-v5"
    assert "frozen_scope_rebind" in generation.ATTEMPT_KINDS
    assert "scope_display_repair" in generation.QUESTION_REPAIR_KINDS
    assert validation.SCOPE_DEFECT_CONTRACT_VERSION == "scope-defect-v1"
    assert validation.SCOPE_DEFECT_DEMANDS == ("display_verbatim", "not_in_evidence")


def test_the_broker_request_key_is_idempotent_for_one_exact_request() -> None:
    """Audit 4.9 C8: a retry must resolve to the request already reserved."""
    from arctic_qa.model_broker import broker_request_key

    identity = {
        "model": "gemini-3.8-flash",
        "run_id": "run-current",
        "phase": "away_production",
        "stage": "question_generation",
        "paper_id": "paper-one",
        "family_id": "family-one",
        "source_version_id": "a" * 64,
    }
    payload = {"contents": [{"role": "user", "parts": [{"text": "Paper text."}]}]}

    first = broker_request_key(**identity, payload=payload)
    second = broker_request_key(**identity, payload=dict(payload))
    changed = broker_request_key(
        **identity,
        payload={"contents": [{"role": "user", "parts": [{"text": "Other text."}]}]},
    )

    assert first == second
    assert first != changed


@pytest.mark.parametrize(
    "reason",
    [
        "eligible_arctic_scope_dimension_unsupported",
        "eligible_arctic_scope_phrase_not_specific",
    ],
)
def test_a_registered_eligibility_code_claims_no_candidate_rung(reason: str) -> None:
    """The eligibility slice announced these; the routing slice owns the logic."""
    assert routing._failure_layer(reason) == "finding"
    assert reason not in routing.REPAIRABLE_QUESTION_REASONS
    assert reason not in routing.ALTERNATIVE_FINDING_REASONS
    assert reason not in routing.IMMEDIATE_ALTERNATIVE_FINDING_REASONS
    assert reason not in routing.OPTION_REPAIR_REASONS
    assert reason not in routing.ANSWER_RULE_REPAIR_REASONS
    assert reason not in routing.SURGICAL_CORRECTION_REASONS


def test_a_stopped_family_keeps_an_accepted_question_with_failed_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accepted question whose distractors failed is a product output."""
    from arctic_qa.streaming import _Progress, _progress_generation

    database = _database(tmp_path)
    namespace = tmp_path / "namespace"
    namespace.mkdir()
    manifest = namespace / "run-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    candidate = _candidate()
    candidate["answer"]["scope"] = {}
    candidate["answer_verification"] = {
        "question_context_referent_resolved": True,
        "relation_scope_match": True,
        "scope_value_contradicted_by_source": False,
    }
    candidate["standalone_verification"] = {"pass": True}
    candidate["schema_version"] = generation.CANDIDATE_SCHEMA_VERSION
    candidate["item_id"] = "item-incomplete"
    candidate["finding_id"] = "finding-1"
    candidate["source"] = {"source_id": "source", "paper_family_id": "family"}
    attempt = _attempt()
    candidate["finding_policy_version"] = attempt["finding_policy_version"]
    candidate["provenance"] = {
        "prompt_version": generation.PROMPT_VERSION,
        "generation_attempt_contract_version": (
            generation.GENERATION_ATTEMPT_CONTRACT_VERSION
        ),
        "answer_agreement_contract_version": (
            generation.ANSWER_AGREEMENT_CONTRACT_VERSION
        ),
        "question_verification_contract_version": (
            generation.QUESTION_VERIFICATION_CONTRACT_VERSION
        ),
        "standalone_verification_contract_version": (
            generation.STANDALONE_VERIFICATION_CONTRACT_VERSION
        ),
        "numeric_rule_contract_version": generation.NUMERIC_RULE_CONTRACT_VERSION,
        "direct_value_contract_version": (
            generation.DIRECT_SOURCE_VALUE_CONTRACT_VERSION
        ),
        "scope_contract_version": generation.SCOPE_CONTRACT_VERSION,
        "scope_role_semantics_version": generation.SCOPE_ROLE_SEMANTICS_VERSION,
        "scope_role_binding_contract_version": (
            generation.SCOPE_ROLE_BINDING_CONTRACT_VERSION
        ),
        "evidence_combination_contract_version": (
            generation.EVIDENCE_COMBINATION_CONTRACT_VERSION
        ),
        "generation_attempt": attempt,
    }
    with database.transaction():
        database.connection.execute(
            """INSERT INTO candidates
            (item_id,run_id,source_id,paper_family_id,generation_arm,candidate_json,
             status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,'incomplete_non_mcq',?,?)""",
            (
                "item-incomplete",
                "campaign",
                "source",
                "family",
                "answer_first",
                canonical_json(candidate),
                now(),
                now(),
            ),
        )
        database.connection.execute(
            """INSERT INTO validation_events
            (event_id,item_id,stage,label,reason_codes_json,details_json,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (
                "event-1",
                "item-incomplete",
                "automated_acceptance",
                "machine_accepted_unverified",
                canonical_json(["scope_qualifier_not_displayed"]),
                canonical_json(
                    {
                        "candidate_hash": stable_id(
                            "candidate-payload", canonical_json(candidate)
                        ),
                        "labels": {"mcq_eligible": False},
                    }
                ),
                now(),
            ),
        )

    monkeypatch.setattr(
        routing,
        "validate_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("no revalidation is expected")
        ),
    )

    result = _progress_generation(
        database,
        namespace,
        _Progress(
            namespace / "progress.json",
            run_id="campaign",
            invocation_run_id="invocation",
            run_manifest_file=manifest,
            counts={},
        ),
        campaign_id="campaign",
        candidate_key="paper",
        source_id="source",
        family_id="family",
        selected={},
        source_version_id="a" * 64,
        title="Fixture",
        author=object(),
        verifier=object(),
    )

    assert result["disposition"] == "incomplete_non_mcq"
    assert (
        database.one(
            "SELECT reason_code FROM rejection_ledger WHERE stage='generation_routing'"
        )["reason_code"]
        == "gate_contradiction_unroutable"
    )
