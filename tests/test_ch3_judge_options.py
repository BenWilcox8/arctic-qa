"""Chapter 3 judge and option slice: ch2 yield audit sections 4.2 and 4.8.

Every rule and every re-ask bound of the slice has a test here. The scripted
end-to-end paths live in ``test_cli_integration.py``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from arctic_qa import generation, validation
from arctic_qa import streaming as streaming_module
from arctic_qa.broker_provider import ROLE_STAGES
from arctic_qa.db import Database
from arctic_qa.errors import CandidateRejectedError
from arctic_qa.model_roles import JUDGE_ROLES, STRONGEST_JUDGE_ROLES
from arctic_qa.providers import _validate_schema
from arctic_qa.util import canonical_json, stable_id


REPO = Path(__file__).resolve().parents[1]

# The one clause that 17 of the 18 confirmed false fails quoted.
DELETED_CLAUSE = "arbitrary study-specific quantity"
FAIL_LIST = (
    "- An acronym, run label, station code, or expedition code that the task never "
    "expands, such as 'ITP', 'refRun', 'CTL', 'GHSZ', 'PS80', or 'DBO4'.",
    "- A definite description with no antecedent in the task, such as 'the southern "
    "station' with no other property, 'the identified OTUs', 'the combined "
    "expeditions', or 'this experiment'.",
    "- A period fixed only by the publication date, such as 'the past 20 years', "
    "'recent years', or 'at this time'.",
    "- A pointer to source material, such as 'Table 2', 'the fourth column', "
    "'Figure 6', or 'according to the study'.",
    "- A measured variable whose unit the displayed text neither names nor implies "
    "through a named metric, such as 'what concentration value' with no unit, when "
    "the answer is a value of that variable. A named metric with a unit "
    "convention, such as 'RMS error' or 'net N2O flux', implies its unit and "
    "passes. Code undefined_measured_variable.",
    "- A quantity reported for samples whose sample type is absent from the "
    "displayed text, such as 'in samples from Resolute Bay' with no word for what "
    "was sampled. A sample set that the task only counts, such as 'how many groups "
    "were the 12 samples clustered into', does not need its sample type. Code "
    "undefined_population_or_sample.",
    "- A relative comparison such as 'higher', 'lower', 'more', or 'percentage "
    "higher' with no stated baseline of the comparison. Code "
    "undefined_comparison_basis.",
    "- A word such as 'listed', 'reported', 'identified', or 'associated' that "
    "points at a table, list, or record the reader cannot see, such as 'which "
    "ports are listed in association with'. An empty question_context does not "
    "excuse it. Code source_dependent_locator.",
    "- Text that is broken, garbled, or cut in the middle of a word.",
    "- A task that states its own answer.",
)


# ---------------------------------------------------------------------------
# 4.2 The standalone prompt
# ---------------------------------------------------------------------------


def test_standalone_prompt_deletes_the_guessability_step() -> None:
    system = generation.STANDALONE_SYSTEM
    assert DELETED_CLAUSE not in system
    assert "choose the correct option from the displayed text alone" not in system
    assert "whenever the value can differ between sites or periods" not in system
    assert (
        "The reader will later choose between four mutually exclusive options of "
        "one type. You do not see them, so never judge whether the reader could "
        "pick the right one." in system
    )
    assert (
        "3. List every referent the task uses. A referent fails when the displayed "
        "text never says what it is. If any referent fails, the task fails." in system
    )
    assert (
        "4. For each detail you believe is still missing, apply the necessity test. "
        "The task passes when no missing detail is necessary." in system
    )
    assert (
        "NEVER fail a task because the value is specific to one study, because the "
        'value is "arbitrary", or because the reader could not derive or guess the '
        "value without the paper." in system
    )
    assert "Judge only whether the reader knows WHAT is asked." in system


def test_standalone_prompt_restates_the_necessity_test() -> None:
    system = generation.STANDALONE_SYSTEM
    assert (
        "(a) The displayed text names or implies more than one candidate referent, "
        "so two readers who both understand the task can defend answers about "
        "different things. Write both readings out." in system
    )
    assert "(b) The reader cannot tell what kind of fact the task asks for." in system
    assert (
        "A location or a period is necessary whenever the displayed text does not "
        "identify which single result is meant, including when it names no site and "
        "no time and the quantity is site-specific or time-specific." in system
    )
    assert (
        "It is NOT necessary merely because the value would differ at another site "
        "or in another year, when the displayed text already identifies one result."
        in system
    )


def test_standalone_prompt_keeps_the_fail_list_word_for_word() -> None:
    system = generation.STANDALONE_SYSTEM
    for line in FAIL_LIST:
        assert line in system
    assert (
        "A study, publication, author, journal, dataset, or campaign identity is "
        "never a necessary detail." in system
    )
    assert (
        "A named campaign, cruise, core, or project code does not resolve a referent."
        in system
    )
    assert "You receive only the question and question_context." in system


def test_standalone_prompt_binds_every_code_to_evidence() -> None:
    system = generation.STANDALONE_SYSTEM
    rule = generation.STANDALONE_EVIDENCE_RULE
    assert rule in system
    for clause in (
        "copy into unresolved_phrases the exact words of the DISPLAYED TEXT",
        "a question never states its own answer",
        "Put both readings in competing_readings",
        "Never use a reason code to record that a value is study-specific",
    ):
        assert clause in rule


def test_standalone_schema_requires_competing_readings() -> None:
    schema = generation.ROLE_SCHEMAS["standalone_verifier"]
    assert "competing_readings" in schema["required"]
    verdict = {
        "pass": False,
        "answer_leakage_absent": True,
        "unresolved_phrases": ["the southern station"],
        "competing_readings": [],
        "missing_detail_types": ["location"],
        "reasons": ["undefined_location"],
        "review_rationale": "The station has no antecedent.",
    }
    _validate_schema(verdict, schema)
    incomplete = dict(verdict)
    incomplete.pop("competing_readings")
    with pytest.raises(ValueError, match="competing_readings"):
        _validate_schema(incomplete, schema)


@pytest.mark.parametrize(
    ("verdict", "unevidenced"),
    [
        (
            {
                "pass": False,
                "reasons": ["undefined_location"],
                "unresolved_phrases": [],
            },
            True,
        ),
        (
            {
                "pass": False,
                "reasons": ["undefined_location"],
                "unresolved_phrases": ["the southern station"],
            },
            False,
        ),
        (
            {
                "pass": False,
                "reasons": ["multiple_interpretations"],
                "competing_readings": ["the July value"],
            },
            True,
        ),
        (
            {
                "pass": False,
                "reasons": ["multiple_interpretations"],
                "competing_readings": ["the July value", "the August value"],
            },
            False,
        ),
        # Codes outside undefined_* and multiple_interpretations are not scoped.
        (
            {
                "pass": False,
                "reasons": ["source_dependent_locator"],
                "unresolved_phrases": [],
            },
            False,
        ),
        ({"pass": True, "reasons": [], "unresolved_phrases": []}, False),
    ],
)
def test_unevidenced_verdict_detection(verdict: dict, unevidenced: bool) -> None:
    assert validation.standalone_verdict_is_unevidenced(verdict) is unevidenced


def test_unevidenced_verdict_collapses_to_one_operational_code() -> None:
    verdict = {
        "pass": False,
        "answer_leakage_absent": True,
        "reasons": ["undefined_location", "undefined_period_or_event"],
        "unresolved_phrases": [],
        "competing_readings": [],
        "missing_detail_types": ["location"],
    }
    assert generation._standalone_gate_reasons(verdict) == [
        "standalone_verdict_unevidenced"
    ]
    assert validation._standalone_reason_codes(verdict) == [
        "standalone_verdict_unevidenced"
    ]
    evidenced = {**verdict, "unresolved_phrases": ["the northern site"]}
    assert generation._standalone_gate_reasons(evidenced) == [
        "standalone_undefined_location",
        "standalone_undefined_period_or_event",
    ]


def test_verdict_fingerprint_reads_codes_and_detail_types_only() -> None:
    first = {
        "reasons": ["undefined_location", "multiple_interpretations"],
        "missing_detail_types": ["location"],
        "unresolved_phrases": ["Procrustes correlation value"],
    }
    second = {
        "reasons": ["multiple_interpretations", "undefined_location"],
        "missing_detail_types": ["location"],
        "unresolved_phrases": ["average TGM value"],
    }
    third = {**first, "missing_detail_types": ["period_or_event"]}
    assert validation.standalone_verdict_fingerprint(first) == (
        validation.standalone_verdict_fingerprint(second)
    )
    assert validation.standalone_verdict_fingerprint(first) != (
        validation.standalone_verdict_fingerprint(third)
    )
    assert validation.standalone_verdict_fingerprint("not a verdict") == ""


def _bound_verdict(**overrides: object) -> dict:
    verdict = {
        "pass": True,
        "answer_leakage_absent": True,
        "unresolved_phrases": [],
        "competing_readings": [],
        "missing_detail_types": [],
        "reasons": [],
        "review_rationale": "The displayed task is self-contained.",
    }
    verdict.update(overrides)
    return generation._bind_standalone_contract_version(verdict)


def test_bound_verdict_carries_version_and_fingerprint() -> None:
    bound = _bound_verdict()
    assert bound["contract_version"] == "source-blind-scientific-referent-v6"
    assert bound["verdict_fingerprint"] == validation.standalone_verdict_fingerprint(
        bound
    )


def test_v4_verdict_resolves_only_with_its_fingerprint_and_readings() -> None:
    candidate = {"schema_version": "2.8.0"}
    bound = _bound_verdict()
    assert validation.standalone_verification_resolves(candidate, bound)
    tampered = {**bound, "verdict_fingerprint": "0" * 64}
    assert not validation.standalone_verification_resolves(candidate, tampered)
    without_readings = dict(bound)
    without_readings.pop("competing_readings")
    assert not validation.standalone_verification_resolves(candidate, without_readings)
    # A stored chapter 2 verdict keeps its v3 key set under schema 2.7.0.
    chapter2 = {
        "contract_version": "source-blind-scientific-referent-v3",
        "pass": True,
        "answer_leakage_absent": True,
        "unresolved_phrases": [],
        "missing_detail_types": [],
        "reasons": [],
        "review_rationale": "The displayed task is self-contained.",
    }
    assert validation.standalone_verification_resolves(
        {"schema_version": "2.7.0"}, chapter2
    )
    assert not validation.standalone_verification_resolves(candidate, chapter2)


def test_contract_table_pins_chapter2_to_v3_and_chapter3_to_v4() -> None:
    contracts = validation.CANDIDATE_CONTRACTS
    assert contracts["2.7.0"]["standalone_verification_contract_version"] == (
        "source-blind-scientific-referent-v3"
    )
    assert contracts["2.8.0"]["standalone_verification_contract_version"] == (
        "source-blind-scientific-referent-v6"
    )
    assert "option_verification_contract_version" not in contracts["2.7.0"]
    assert contracts["2.8.0"]["option_verification_contract_version"] == (
        "option-admitting-interpretation-v1"
    )
    assert validation.OPTION_SET_VERDICT_SCHEMA_VERSIONS == frozenset({"2.8.0"})
    assert generation.CANDIDATE_SCHEMA_VERSION == "2.8.0"


def test_deterministic_screen_speaks_in_its_own_namespace() -> None:
    question = "What was measured at the southern station (74.5 °N)?"
    assert validation.standalone_deterministic_reason(question, "") == (
        "standalone_det_question_context_missing"
    )
    assert (
        validation.standalone_deterministic_reason(
            question, "The southern station was at 74.5 °N."
        )
        == "standalone_det_question_context_referent_unresolved"
    )
    assert (
        validation.standalone_deterministic_reason(
            "What reported water depth was documented?", ""
        )
        is None
    )
    assert validation.standalone_gate_decision(question, "") == [
        "standalone_det_question_context_missing"
    ]


def test_namespaced_deterministic_codes_keep_their_repair_rungs() -> None:
    for code in (
        "standalone_det_question_context_missing",
        "standalone_det_question_context_referent_unresolved",
    ):
        assert code in streaming_module.REPAIRABLE_QUESTION_REASONS
        assert code in streaming_module.SURGICAL_CORRECTION_REASONS
        assert code in streaming_module._DEPENDENT_ROUTING_REASONS
    path = _primary_path()
    repair = _next({(1, 0): path}, path, ["standalone_det_question_context_missing"])
    assert repair is not None
    assert repair["attempt_kind"] == "surgical_correction"


def test_unevidenced_verdict_routes_to_another_finding() -> None:
    assert (
        "standalone_verdict_unevidenced" in streaming_module.ALTERNATIVE_FINDING_REASONS
    )
    assert (
        "standalone_verdict_unevidenced"
        in streaming_module.IMMEDIATE_ALTERNATIVE_FINDING_REASONS
    )
    path = _primary_path()
    attempt = _next({(1, 0): path}, path, ["standalone_verdict_unevidenced"])
    assert attempt is not None
    assert attempt["attempt_kind"] == "alternative_finding"
    assert attempt["trigger_reason_code"] == "standalone_verdict_unevidenced"


# ---------------------------------------------------------------------------
# 4.2 The satisfiability guard
# ---------------------------------------------------------------------------


def _primary_path(candidate: dict | None = None) -> dict:
    primary = streaming_module._generation_attempt(
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
    payload = candidate or {"answer": {"source_span_id": "span-primary"}}
    return {
        "attempt": primary,
        "candidate": {
            "item_id": "item-primary",
            "candidate_json": canonical_json(payload),
        },
    }


def _next(paths: dict, failed: dict, reasons: list[str], **kwargs) -> dict | None:
    return streaming_module._next_generation_attempt(
        campaign_id="campaign",
        family_id="family",
        paths=paths,
        failed_path=failed,
        reason_codes=reasons,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("reasons", "scope", "supplied", "expected"),
    [
        (
            ["standalone_undefined_period_or_event"],
            {"period": None},
            frozenset(),
            {"standalone_undefined_period_or_event"},
        ),
        (
            ["standalone_undefined_period_or_event"],
            {"period": "August 2015"},
            frozenset(),
            set(),
        ),
        (
            ["standalone_undefined_period_or_event"],
            {"period": None},
            frozenset({"period"}),
            set(),
        ),
        (
            ["standalone_undefined_location"],
            {"geography": None},
            frozenset({"period"}),
            {"standalone_undefined_location"},
        ),
        (
            ["standalone_undefined_location"],
            {"geography": "Disko Island"},
            frozenset(),
            set(),
        ),
        (["standalone_undefined_location"], None, frozenset({"place"}), set()),
        # Unknown forwarded text disables the guard rather than guessing.
        (["standalone_undefined_location"], {"geography": None}, None, set()),
        # Only the two dimension codes are guarded.
        (
            ["standalone_undefined_acronym", "standalone_multiple_interpretations"],
            {},
            frozenset(),
            set(),
        ),
        (
            ["standalone_undefined_location", "standalone_undefined_period_or_event"],
            {},
            frozenset(),
            {"standalone_undefined_location", "standalone_undefined_period_or_event"},
        ),
    ],
)
def test_unsatisfiable_standalone_demands(
    reasons: list[str], scope: dict | None, supplied: frozenset | None, expected: set
) -> None:
    assert generation.unsatisfiable_standalone_demands(
        reasons, answer_scope=scope, supplied_slots=supplied
    ) == frozenset(expected)


def test_forwarded_slot_evidence_reads_the_text_the_writer_saw() -> None:
    path = _primary_path(
        {
            "answer": {
                "source_span_id": "span-primary",
                "evidence_quote": "Net CO2 exchange was estimated on a cloudy day in August.",
                "scope": {"period": None, "geography": None},
            },
            "provenance": {
                "context_only_source": {
                    "spans": [
                        {"text": "Permafrost cores were collected near Disko Island."}
                    ]
                }
            },
        }
    )
    supplied = streaming_module._forwarded_slot_evidence(path)
    assert supplied is not None
    assert {"period", "place"} <= supplied
    assert streaming_module._failed_answer_scope(path) == {
        "period": None,
        "geography": None,
    }
    assert streaming_module._forwarded_slot_evidence(_primary_path()) is None
    assert streaming_module._failed_answer_scope({"candidate": None}) is None


def test_routing_never_spends_a_revision_on_an_unsatisfiable_dimension() -> None:
    path = _primary_path(
        {"answer": {"source_span_id": "span-primary", "scope": {"period": None}}}
    )
    paths = {(1, 0): path}
    unmet = _next(
        paths,
        path,
        ["standalone_undefined_period_or_event"],
        forwarded_slot_evidence=frozenset({"place"}),
    )
    assert unmet is not None
    assert unmet["attempt_kind"] == "alternative_finding"
    assert unmet["trigger_reason_code"] == "slot_evidence_unavailable"
    supplied = _next(
        paths,
        path,
        ["standalone_undefined_period_or_event"],
        forwarded_slot_evidence=frozenset({"period"}),
    )
    assert supplied is not None
    assert supplied["attempt_kind"] == "surgical_correction"
    bound = _primary_path(
        {"answer": {"source_span_id": "span-primary", "scope": {"period": "August"}}}
    )
    scoped = _next(
        {(1, 0): bound},
        bound,
        ["standalone_undefined_period_or_event"],
        forwarded_slot_evidence=frozenset(),
    )
    assert scoped is not None
    assert scoped["attempt_kind"] == "surgical_correction"
    # No forwarded text: the guard stays out of the way.
    unknown = _next(paths, path, ["standalone_undefined_period_or_event"])
    assert unknown is not None
    assert unknown["attempt_kind"] == "surgical_correction"


def test_there_is_no_freeze_time_dimension_rejection() -> None:
    source = inspect.getsource(generation)
    assert "finding_scope_dimension_unavailable" not in source


# ---------------------------------------------------------------------------
# 4.8 The option stage
# ---------------------------------------------------------------------------


def test_distractor_kind_is_a_closed_described_enum_in_prompt_and_schema() -> None:
    kind = generation.DISTRACTOR_SCHEMA["properties"]["deterministic"]["properties"][
        "kind"
    ]
    expected = {
        "numeric_outside_tolerance",
        "unique_categorical",
        "directional_contradiction",
        "scope_excluded",
        "unique_entity",
        "closed_set",
    }
    assert set(kind["enum"]) == expected
    assert set(generation.OPTION_DETERMINISTIC_KINDS) == expected
    for name in expected:
        assert f"{name}:" in kind["description"]
        assert name in generation.DISTRACTOR_WRITER_INSTRUCTIONS
    assert "Do not invent a kind name." in generation.DISTRACTOR_WRITER_INSTRUCTIONS
    with pytest.raises(ValueError):
        _validate_schema(
            {
                "text": "1 ppt",
                "type": "numeric",
                "generation_rationale": "x",
                "source_span_id": "span",
                "deterministic": {"kind": "plausible_value"},
            },
            generation.SPAN_DISTRACTOR_SCHEMA,
        )


def test_writer_proposes_six_typed_distractors_without_construction_bans() -> None:
    array = generation.ROLE_SCHEMAS["distractor_writer"]["properties"]["distractors"]
    assert array["minItems"] == 6
    assert array["maxItems"] == 8
    text = generation.DISTRACTOR_WRITER_INSTRUCTIONS
    assert "Propose exactly six typed distractors." in text
    assert "Build the set from typed contrasts." in text
    # D5 bans were refuted: they removed a distractor that passed twice.
    assert "Do not build a numeric option" not in text
    assert "Do not write a null-change option" not in text
    assert "Do not reuse the wording of a different phenomenon" not in text


def test_option_verifier_schema_states_the_admitting_reading_first() -> None:
    schema = generation.ROLE_SCHEMAS["option_verifier"]
    assert schema["required"][:5] == [
        "rationale",
        "admitting_interpretation",
        "contradiction_established",
        "option_standalone_interpretable",
        "question_admits_option_as_correct",
    ]
    assert "true_in_different_context" not in schema["properties"]
    assert "alternative_answer_search_passed" not in schema["properties"]
    flag = schema["properties"]["question_admits_option_as_correct"]["description"]
    assert "True only when admitting_interpretation is non-empty." in flag
    assert "Do not set this field true to report that the option is false." in flag
    assert (
        "bare latitude"
        in (schema["properties"]["option_standalone_interpretable"]["description"])
    )
    assert "Decide two separate things." in generation.OPTION_ADMISSION_RULE
    assert (
        "A contradiction is not an alternate reading."
        in generation.OPTION_ADMISSION_RULE
    )


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (
            {
                "contradiction_established": False,
                "option_standalone_interpretable": True,
                "question_admits_option_as_correct": False,
                "admitting_interpretation": "",
            },
            "option_contradiction_unresolved",
        ),
        (
            {
                "contradiction_established": True,
                "option_standalone_interpretable": False,
                "question_admits_option_as_correct": False,
                "admitting_interpretation": "",
            },
            "option_standalone_uninterpretable",
        ),
        (
            {
                "contradiction_established": True,
                "option_standalone_interpretable": True,
                "question_admits_option_as_correct": True,
                "admitting_interpretation": "",
            },
            "option_admission_unexplained",
        ),
        (
            {
                "contradiction_established": True,
                "option_standalone_interpretable": True,
                "question_admits_option_as_correct": True,
                "admitting_interpretation": "Read the depth as the second site.",
            },
            "option_correct_under_question_interpretation",
        ),
        (
            {
                "contradiction_established": True,
                "option_standalone_interpretable": True,
                "question_admits_option_as_correct": False,
                "admitting_interpretation": "",
            },
            None,
        ),
        # Legacy chapter 2 verdicts keep their legacy codes.
        (
            {
                "contradiction_established": True,
                "alternative_answer_search_passed": False,
                "true_in_different_context": False,
                "question_admits_option_as_correct": False,
            },
            "distractor_alternative_answer_possible",
        ),
        (
            {
                "contradiction_established": True,
                "alternative_answer_search_passed": True,
                "true_in_different_context": True,
                "question_admits_option_as_correct": True,
            },
            "option_correct_under_question_interpretation",
        ),
    ],
)
def test_option_verdict_rejection_reason(verdict: dict, reason: str | None) -> None:
    assert validation.option_verdict_rejection_reason(verdict) == reason


def test_malformed_admission_is_a_flag_without_a_reading() -> None:
    assert validation.option_verdict_is_malformed(
        {"question_admits_option_as_correct": True, "admitting_interpretation": "  "}
    )
    assert not validation.option_verdict_is_malformed(
        {
            "question_admits_option_as_correct": True,
            "admitting_interpretation": "a reading",
        }
    )
    assert not validation.option_verdict_is_malformed(
        {"question_admits_option_as_correct": False, "admitting_interpretation": ""}
    )
    # A legacy verdict has no reading field and is never malformed.
    assert not validation.option_verdict_is_malformed(
        {"question_admits_option_as_correct": True}
    )


def test_option_response_schema_accepts_every_contract_key_set() -> None:
    admitting = {
        "rationale": "x",
        "admitting_interpretation": "",
        "contradiction_established": True,
        "option_standalone_interpretable": True,
        "question_admits_option_as_correct": False,
        "source_span_id": "span",
    }
    assert validation._option_response_schema_valid(admitting)
    assert not validation._option_response_schema_valid(
        {**admitting, "admitting_interpretation": None}
    )
    assert not validation._option_response_schema_valid({**admitting, "extra": True})
    legacy = {
        "contradiction_established": True,
        "alternative_answer_search_passed": True,
        "true_in_different_context": False,
        "question_admits_option_as_correct": False,
        "source_span_id": "span",
        "rationale": "x",
    }
    assert validation._option_response_schema_valid(legacy)


def _numeric_answer() -> dict:
    return {
        "text": "2.0 m",
        "variants": ["200 cm"],
        "numeric_rule": {
            "canonical_value": "2.0",
            "unit": "m",
            "tolerance": "0.1",
            "tolerance_basis": "tolerance of 0.1 m",
        },
    }


@pytest.mark.parametrize(
    ("distractor", "duplicate", "reason"),
    [
        ({"text": "2.5 m"}, True, "duplicate_or_equivalent_distractor"),
        ({"text": "200 cm"}, False, "distractor_matches_answer"),
        ({"text": "None of the above"}, False, "forbidden_meta_option"),
        ({"text": "not 2.5 m"}, False, "displayed_assertion_negated"),
        (
            {"text": "2 m", "numeric": {"canonical_value": "200", "unit": "cm"}},
            False,
            "numeric_display_ambiguous",
        ),
        (
            {"text": "0.002 km", "numeric": {"canonical_value": "0.002", "unit": "km"}},
            False,
            "distractor_is_equivalent_numeric_answer",
        ),
        (
            {"text": "2.5 m", "numeric": {"canonical_value": "2.5", "unit": "m"}},
            False,
            None,
        ),
    ],
)
def test_free_option_prefilter_mirrors_the_gate(
    distractor: dict, duplicate: bool, reason: str | None
) -> None:
    assert (
        validation.option_free_rejection_reason(
            _numeric_answer(), distractor, "", duplicate_text=duplicate
        )
        == reason
    )


def _closed_set_answer(
    evidence: str, members: tuple[str, ...] = ("ethanol", "methanol")
) -> dict:
    return {
        "text": " and ".join(members),
        "evidence_quote": evidence,
        "deterministic_rule": {
            "kind": "closed_set",
            "source_values": list(members),
            "member_type": "categorical_entity",
            "ordering": "unordered",
        },
    }


def test_closed_set_without_source_closure_is_rejected_before_any_paid_call() -> None:
    garbled = (
        "release a compounds positively correlated with the dissolved organic "
        "variety of BVOCs with ethanol and methanol as the dominant compounds."
    )
    assert validation.closed_set_closure_reason(_closed_set_answer(garbled)) == (
        "closed_set_closure_not_source_established"
    )
    closed = "The emitted compounds consist of ethanol and methanol."
    assert validation.closed_set_closure_reason(_closed_set_answer(closed)) is None
    single = _closed_set_answer(garbled, ("ethanol",))
    assert validation.closed_set_closure_reason(single) is None
    assert validation.closed_set_closure_reason(_numeric_answer()) is None
    quantity = _closed_set_answer(garbled)
    quantity["deterministic_rule"]["member_type"] = "quantity"
    assert validation.closed_set_closure_reason(quantity) is None
    with pytest.raises(CandidateRejectedError) as raised:
        generation._generate_distractors(
            db=None,
            source={},
            context="",
            context_spans={},
            question="Which two compounds dominated?",
            question_context="",
            answer=_closed_set_answer(garbled),
            qa_hash="qa",
            entity_id="entity",
            author=None,
            verifier=None,
            run_id="run",
            parameters={},
            reservation=0,
            timeout=1,
            retries=0,
            rate_limit_seconds=0,
        )
    assert raised.value.reason_code == "closed_set_closure_not_source_established"


def test_superlative_closure_is_narrowed_and_shadow_only() -> None:
    values = ["ethanol", "methanol"]
    assert validation.definite_superlative_closure(
        "Ethanol and methanol were the two dominant compounds released on thaw.",
        values,
    )
    assert validation.definite_superlative_closure(
        "The two most abundant compounds were ethanol and methanol.", values
    )
    # No count: the bare superlative does not close the set.
    assert not validation.definite_superlative_closure(
        "The dominant compounds were ethanol and methanol.", values
    )
    # An enumeration lead never closes the set.
    assert not validation.definite_superlative_closure(
        "Compounds including ethanol and methanol were the two dominant ones.", values
    )
    # The count must equal the number of members.
    assert not validation.definite_superlative_closure(
        "Ethanol and methanol were the three dominant compounds.", values
    )
    # Members split across sentences do not close the set.
    assert not validation.definite_superlative_closure(
        "Ethanol was the two dominant compounds. Methanol was also found.", values
    )
    shadowed = _closed_set_answer(
        "Ethanol and methanol were the two dominant compounds released on thaw."
    )
    assert validation._closed_set_shadow_labels(shadowed) == [
        "superlative_closure_would_establish_set"
    ]
    # The gate itself is unchanged: the shadowed set still has no closure.
    assert validation.closed_set_closure_reason(shadowed) == (
        "closed_set_closure_not_source_established"
    )
    assert (
        validation._closed_set_shadow_labels(
            _closed_set_answer("The compounds consist of ethanol and methanol.")
        )
        == []
    )


def test_option_set_hash_binds_the_question_and_the_ordered_options() -> None:
    assert validation.option_set_hash("qa", ["a", "b"]) != validation.option_set_hash(
        "qa", ["b", "a"]
    )
    assert validation.option_set_hash("qa", ["a", "b"]) == stable_id(
        "option-set", "qa", "a", "b"
    )


def _set_candidate(db_path: Path) -> tuple[Database, dict, str, list[str]]:
    db = Database(db_path)
    db.migrate(db_path.parent / "backups")
    distractors = [
        {"text": "2.5 m", "type": "numeric"},
        {"text": "3.0 m", "type": "numeric"},
        {"text": "4.0 m", "type": "numeric"},
        {"text": "5.0 m", "type": "numeric"},
    ]
    candidate = {
        "source": {"content_hash": "sha256:source"},
        "finding_id": "finding",
        "provenance": {"run_id": "run", "generation_arm": "answer_first"},
        "distractors": distractors,
    }
    qa_hash = "qa-hash"
    hashes = [
        stable_id("option", qa_hash, row["text"], row["type"]) for row in distractors
    ]
    return db, candidate, qa_hash, hashes


def _set_verdict(
    candidate: dict, qa_hash: str, hashes: list[str], **flags: object
) -> dict:
    payload = {
        "rationale": "Every depth is distinct.",
        "overlapping_option_pairs": [],
        "options_mutually_exclusive": True,
        "answer_choosable_from_displayed_text": True,
    }
    payload.update(flags)
    set_hash = validation.option_set_hash(qa_hash, hashes)
    # One prompt hash per distinct payload, as one live call would have.
    prompt_hash = stable_id("prompt-set", canonical_json(payload))
    return {
        "source_hash": candidate["source"]["content_hash"],
        "qa_hash": qa_hash,
        "option_hashes": hashes,
        "set_hash": set_hash,
        **payload,
        "provenance": {
            "role": "option_set_verifier",
            "provider": "fake",
            "requested_model": "fake-verifier",
            "returned_model": "fake-verifier",
            "request_id": stable_id("request-set", prompt_hash),
            "prompt_version": "test-only",
            "prompt_hash": prompt_hash,
        },
    }


def _insert_set_receipt(db: Database, candidate: dict, verdict: dict) -> None:
    entity_id = stable_id(
        "option-set-verdict",
        stable_id("unit", candidate["finding_id"], "answer_first"),
        verdict["set_hash"],
    )
    payload = {
        key: verdict[key]
        for key in (
            "rationale",
            "overlapping_option_pairs",
            "options_mutually_exclusive",
            "answer_choosable_from_displayed_text",
        )
    }
    provenance = verdict["provenance"]
    with db.transaction():
        db.connection.execute(
            """INSERT INTO calls
            (call_id,run_id,entity_id,role,provider,requested_model,
             returned_model,prompt_version,prompt_hash,parameters_json,
             request_id,attempt,status,response_json,started_at,completed_at)
            VALUES (?,?,?,'option_set_verifier','fake','fake-verifier',
                    'fake-verifier','test-only',?,'{}',?,1,
                    'completed',?,'test-only','test-only')""",
            (
                stable_id("call", entity_id, provenance["prompt_hash"]),
                "run",
                entity_id,
                provenance["prompt_hash"],
                provenance["request_id"],
                canonical_json(payload),
            ),
        )


def test_option_set_verdict_reasons(tmp_path: Path) -> None:
    db, candidate, qa_hash, hashes = _set_candidate(tmp_path / "state.sqlite3")
    accepted = hashes[:3]
    assert validation.option_set_verdict_reasons(
        db, candidate, None, qa_hash, accepted, hashes
    ) == ["option_set_verdict_missing"]
    verdict = _set_verdict(candidate, qa_hash, hashes)
    assert validation.option_set_verdict_reasons(
        db, candidate, verdict, qa_hash, accepted, hashes
    ) == ["option_set_verdict_call_receipt_missing"]
    _insert_set_receipt(db, candidate, verdict)
    assert (
        validation.option_set_verdict_reasons(
            db, candidate, verdict, qa_hash, accepted, hashes
        )
        == []
    )
    # Every accepted option must sit inside the judged set.
    outside = stable_id("option", qa_hash, "9.0 m", "numeric")
    assert validation.option_set_verdict_reasons(
        db, candidate, verdict, qa_hash, [*accepted, outside], [*hashes, outside]
    ) == ["option_set_verdict_stale"]
    # The judged set may hold nothing outside this candidate.
    foreign = _set_verdict(candidate, qa_hash, [*hashes, outside])
    assert validation.option_set_verdict_reasons(
        db, candidate, foreign, qa_hash, accepted, hashes
    ) == ["option_set_verdict_stale"]
    stale = {**verdict, "qa_hash": "other"}
    assert validation.option_set_verdict_reasons(
        db, candidate, stale, qa_hash, accepted, hashes
    ) == ["option_set_verdict_stale"]
    unbound = {**verdict, "provenance": {}}
    assert validation.option_set_verdict_reasons(
        db, candidate, unbound, qa_hash, accepted, hashes
    ) == ["option_set_verdict_provenance_missing"]
    failing = _set_verdict(
        candidate,
        qa_hash,
        hashes,
        overlapping_option_pairs=["2.5 m | 3.0 m"],
        options_mutually_exclusive=False,
        answer_choosable_from_displayed_text=False,
    )
    _insert_set_receipt(db, candidate, failing)
    assert validation.option_set_verdict_reasons(
        db, candidate, failing, qa_hash, accepted, hashes
    ) == [
        "option_set_not_mutually_exclusive",
        "option_set_answer_not_choosable",
    ]
    # The receipt must say what the record says.
    tampered = {**failing, "options_mutually_exclusive": True}
    assert validation.option_set_verdict_reasons(
        db, candidate, tampered, qa_hash, accepted, hashes
    ) == ["option_set_verdict_call_receipt_missing"]
    db.close()


def test_option_set_schema_and_prompt_are_source_blind() -> None:
    schema = generation.ROLE_SCHEMAS["option_set_verifier"]
    assert set(schema["required"]) == validation.OPTION_SET_VERDICT_RESPONSE_KEYS
    assert "SOURCE_DATA" not in generation.OPTION_SET_SYSTEM
    assert "cannot see the paper" in generation.OPTION_SET_SYSTEM
    assert "The reader is not expected to know the measured value" in (
        generation.OPTION_SET_INSTRUCTIONS
    )
    assert "option_set_verifier" in JUDGE_ROLES
    assert "option_set_verifier" in STRONGEST_JUDGE_ROLES
    assert ROLE_STAGES["option_set_verifier"] == "option_verification"
    roles = json.loads((REPO / "config" / "roles.v1.json").read_text(encoding="utf-8"))
    for profile in roles["profiles"].values():
        assert profile["option_set_verifier"] == profile["option_verifier"]


def test_option_stage_codes_are_registered_in_routing() -> None:
    for code in (
        "option_set_not_mutually_exclusive",
        "option_set_answer_not_choosable",
    ):
        assert code in streaming_module.OPTION_REPAIR_REASONS
        assert code in streaming_module.REPAIRABLE_QUESTION_REASONS
        assert streaming_module._failure_layer(code) == "options"
    for code in (
        "option_pool_empty_after_prefilter",
        "closed_set_closure_not_source_established",
    ):
        assert code in streaming_module.ALTERNATIVE_FINDING_REASONS
        assert code in streaming_module.IMMEDIATE_ALTERNATIVE_FINDING_REASONS
    path = _primary_path()
    repair = _next({(1, 0): path}, path, ["option_set_not_mutually_exclusive"])
    assert repair is not None
    assert repair["attempt_kind"] == "option_repair"
    alternative = _next({(1, 0): path}, path, ["option_pool_empty_after_prefilter"])
    assert alternative is not None
    assert alternative["attempt_kind"] == "alternative_finding"


def test_repair_guidance_is_per_code_and_the_targets_are_bounded() -> None:
    guidance = generation.OPTION_REPAIR_GUIDANCE
    for code in (
        "option_correct_under_question_interpretation",
        "option_contradiction_unresolved",
        "option_standalone_uninterpretable",
        "option_set_not_mutually_exclusive",
        "option_set_answer_not_choosable",
    ):
        assert code in guidance
    assert "deterministic rule that rejected it" not in guidance
    assert generation.OPTION_PROPOSAL_COUNT == 6
    # Floor of three plus one for the answer-absent MCQ export.
    assert generation.OPTION_VERIFIED_TARGET == 4
