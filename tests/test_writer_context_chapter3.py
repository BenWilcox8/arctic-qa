"""Chapter 3 writer-context slice: the chapter 2 yield audit, section 4.1.

Every rule this slice adds has a test here, and every test shows both halves:
the writer now receives the study setting, and the rigor safeguard still
rejects an item that rests on it wrongly. The refuted proposals of audit
section 4.10 are tested as absent.
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from arctic_qa import context_projection, generation, streaming, validation
from arctic_qa.errors import CandidateRejectedError
from arctic_qa.util import sha256_bytes

SCOPE_KEYS = (
    "geography",
    "population",
    "period",
    "method",
    "comparison",
    "uncertainty",
)
FINDING_SENTENCE = (
    "The lipid content of the strains declined by 15 percent after cold storage."
)
STRAIN_SENTENCE = (
    "Strains KMM 9713 and KMM 9724 T were isolated from a deep bottom sediment "
    "sampled at a depth of 29 m from the Chukchi Sea in September 2016, as "
    "described previously [8]."
)
STRAIN_SENTENCE_DISPLAYED = (
    "Strains KMM 9713 and KMM 9724 T were isolated from a deep bottom sediment "
    "sampled at a depth of 29 m from the Chukchi Sea in September 2016, as "
    "described previously."
)


def _scope(**values: str) -> dict[str, str | None]:
    return {key: values.get(key) for key in SCOPE_KEYS}


def _span_record(
    quote: str, span_id: str, dimension: str | None = None
) -> dict[str, object]:
    record: dict[str, object] = {
        "span_id": span_id,
        "quote": quote,
        "source_bytes_sha256": sha256_bytes(quote.encode("utf-8")),
    }
    if dimension is not None:
        record["dimension"] = dimension
    return record


def _source(
    component: str,
    finding_quotes: list[str],
    activity: list[str | tuple[str, str]],
    phrases: list[str] | None = None,
) -> dict[str, object]:
    activity_records = []
    for index, item in enumerate(activity):
        if isinstance(item, tuple):
            activity_records.append(_span_record(item[0], f"a{index}", item[1]))
        else:
            activity_records.append(_span_record(item, f"a{index}"))
    return {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "eligibility_job_key": "job-chapter-3",
                "resolved_eligible_arctic_scope": {
                    "component": component,
                    "finding_spans": [
                        _span_record(quote, f"f{index}")
                        for index, quote in enumerate(finding_quotes)
                    ],
                    "activity_spans": activity_records,
                    "question_scope_phrases": list(phrases or []),
                },
            }
        ),
    }


def _chunks(*texts: str) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": f"chunk-{index + 1}",
            "section_id": "results",
            "heading": "Results",
            "chunk_index": index,
            "text": text,
        }
        for index, text in enumerate(texts)
    ]


def _candidate(
    span_id: str,
    scope: dict[str, str | None],
    scope_evidence: list[dict[str, str]] | None,
    **extra: object,
) -> dict[str, object]:
    answer: dict[str, object] = {
        "text": "15 percent",
        "source_span_id": span_id,
        "scope": scope,
        "required_question_phrases": ["lipid content"],
        "claim_type": "observation",
        "selection_rationale": "prose sentence",
        **extra,
    }
    if scope_evidence is not None:
        answer["scope_evidence"] = scope_evidence
    return {"rank": 1, "answer": answer, "ranking_rationale": "first"}


# 4.1 (a): redact the pointer and keep the sentence.


def test_a_citation_marker_no_longer_deletes_the_study_setting_sentence() -> None:
    text = f"{STRAIN_SENTENCE}\n\n{FINDING_SENTENCE}"
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [STRAIN_SENTENCE]), _chunks(text)
    )

    assert len(interpretation) == 1
    span = interpretation[0]
    # Custody stays on the raw bytes.
    assert span["text"] == STRAIN_SENTENCE
    assert span["text_sha256"] == sha256_bytes(STRAIN_SENTENCE.encode("utf-8"))
    # The reader sees the redacted projection.
    assert span["display_text"] == STRAIN_SENTENCE_DISPLAYED
    assert "[8]" not in span["display_text"]


def test_the_model_sees_the_projection_and_the_validator_sees_the_bytes() -> None:
    text = f"{STRAIN_SENTENCE}\n\n{FINDING_SENTENCE}"
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [STRAIN_SENTENCE]), _chunks(text)
    )
    span = interpretation[0]

    model_span = generation._context_only_model_span(span)
    assert model_span["text"] == STRAIN_SENTENCE_DISPLAYED
    assert model_span["source_text_sha256"] == span["text_sha256"]
    assert "[8]" not in generation._context_only_source(interpretation)

    record = generation._context_only_span(span)
    assert record["text"] == STRAIN_SENTENCE
    assert record["display_text"] == STRAIN_SENTENCE_DISPLAYED
    assert record["span_role"] == "interpretation"
    provenance = {
        "context_only_source": {
            "contract_version": validation.CONTEXT_ONLY_EVIDENCE_CONTRACT_VERSION,
            "selectable_for_answer_evidence": False,
            "spans": [record],
        }
    }
    chunks = {"chunk-1": {"text": text}}
    assert validation.context_only_spans_resolve(provenance, chunks)


def test_a_tampered_display_projection_fails_custody() -> None:
    text = f"{STRAIN_SENTENCE}\n\n{FINDING_SENTENCE}"
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [STRAIN_SENTENCE]), _chunks(text)
    )
    record = generation._context_only_span(interpretation[0])
    record["display_text"] = record["display_text"].replace("Chukchi", "Beaufort")
    provenance = {
        "context_only_source": {
            "contract_version": validation.CONTEXT_ONLY_EVIDENCE_CONTRACT_VERSION,
            "selectable_for_answer_evidence": False,
            "spans": [record],
        }
    }

    assert not validation.context_only_spans_resolve(
        provenance, {"chunk-1": {"text": text}}
    )


def test_a_chapter_two_record_still_resolves_under_its_own_contract() -> None:
    text = f"{STRAIN_SENTENCE}\n\n{FINDING_SENTENCE}"
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [STRAIN_SENTENCE]), _chunks(text)
    )
    record = generation._context_only_span(interpretation[0])
    record.pop("display_text")
    provenance = {
        "context_only_source": {
            "contract_version": (
                validation.PREDECESSOR_CONTEXT_ONLY_EVIDENCE_CONTRACT_VERSION
            ),
            "selectable_for_answer_evidence": False,
            "spans": [record],
        }
    }

    assert validation.context_only_spans_resolve(
        provenance, {"chunk-1": {"text": text}}
    )


@pytest.mark.parametrize(
    ("raw", "displayed"),
    [
        (
            "An Arctic Ocean research cruise (number: MR18-05C) was conducted aboard "
            "the R/V Mirai from October 24 to December 3, 2018 (Figs. 1, 2, Table S1).",
            "An Arctic Ocean research cruise (number: MR18-05C) was conducted aboard "
            "the R/V Mirai from October 24 to December 3, 2018.",
        ),
        (
            "Samples were taken at Utqiagvik, Alaska (71.323 N, 156.615 W) "
            "(Verlinde et al., 2016).",
            "Samples were taken at Utqiagvik, Alaska (71.323 N, 156.615 W).",
        ),
        (
            "This cross-sectional study was carried out at 4 open-pit mines over the "
            "period from November 2012 to November 2013 [19].",
            "This cross-sectional study was carried out at 4 open-pit mines over the "
            "period from November 2012 to November 2013.",
        ),
        (
            "Observations were made between 2007 and 2016 using the DARDAR-MASK "
            "v2.23 product (Delanoe and Hogan, 2008, 2010; Ceccaldi et al., 2013).",
            "Observations were made between 2007 and 2016 using the DARDAR-MASK "
            "v2.23 product.",
        ),
    ],
)
def test_redaction_removes_only_the_pointer(raw: str, displayed: str) -> None:
    assert context_projection.context_only_display_text(raw) == displayed


@pytest.mark.parametrize(
    "raw",
    [
        # RESIDUAL_LOCATOR_PATTERN is kept: the sentence points at an unseen table.
        "Table 2 lists the twelve stations that were sampled in 2016.",
        "Sample locations are shown in Figure 3 of the same study.",
        # A fragment: no finite verb.
        "Villum Research Station at Station Nord in northeastern Greenland.",
        # No terminal punctuation.
        "Samples were collected at Station Nord in 2016",
        # A two-column join in the raw bytes.
        "Samples were collected at Station Nord          in the 2016 season.",
        "Short one.",
    ],
)
def test_an_unusable_span_has_no_display_projection(raw: str) -> None:
    assert context_projection.context_only_display_text(raw) is None
    assert not generation._context_only_span_is_usable(raw)


def test_the_residual_locator_pattern_is_the_locator_half_only() -> None:
    assert generation._RESIDUAL_LOCATOR_PATTERN.search("see Table 2 for details")
    assert generation._RESIDUAL_LOCATOR_PATTERN.search("Fig. S3 shows the site")
    assert not generation._RESIDUAL_LOCATOR_PATTERN.search("as described [8]")
    assert not generation._RESIDUAL_LOCATOR_PATTERN.search("(Verlinde et al., 2016)")


def test_the_answer_leak_test_runs_on_the_displayed_text() -> None:
    # The raw bytes split the answer around a citation; the reader would see it.
    raw = "The lipid content declined by 15 [3] percent at the northern site."
    displayed = context_projection.context_only_display_text(raw)
    assert displayed == "The lipid content declined by 15 percent at the northern site."
    span = {
        "span_id": "s1",
        "chunk_id": "chunk-1",
        "start_offset": 0,
        "end_offset": len(raw),
        "text_sha256": sha256_bytes(raw.encode("utf-8")),
        "text": raw,
        "display_text": displayed,
    }
    answer = {"text": "15 percent", "variants": []}

    assert not validation.interpretation_spans_contain_answer([{"text": raw}], answer)
    assert generation._forwarded_context_only_spans([span], answer) == []
    assert validation.interpretation_spans_contain_answer(
        generation._leak_test_records([span]), answer
    )


def test_the_context_bundle_keeps_its_cap() -> None:
    # Audit 4.10: the MAX_CONTEXT_ONLY_SPANS raise was dropped by amendment.
    assert generation.MAX_CONTEXT_ONLY_SPANS == 12


# 4.1 (d) second half: a context qualifier binds to the forwarded text.


def test_a_context_qualifier_binds_to_a_forwarded_context_span() -> None:
    answer = {
        "evidence_quote": FINDING_SENTENCE,
        "scope": {"period": "September 2016"},
    }
    context_texts = [STRAIN_SENTENCE]

    # In question_context, resting on the forwarded span: supported.
    assert (
        validation.question_qualifier_binding_reason(
            "By how much did the lipid content of the strains decline?",
            answer,
            None,
            None,
            question_context="The strains were isolated in September 2016.",
            context_only_texts=context_texts,
        )
        is None
    )
    # In question_context with no forwarded span: rejected.
    assert (
        validation.question_qualifier_binding_reason(
            "By how much did the lipid content of the strains decline?",
            answer,
            None,
            None,
            question_context="The strains were isolated in September 2016.",
        )
        == "question_qualifier_not_evidence_bound"
    )
    # In the stem: the stem stays bound to the role evidence alone.
    assert (
        validation.question_qualifier_binding_reason(
            "By how much did the lipid content decline in September 2016?",
            answer,
            None,
            None,
            question_context="",
            context_only_texts=context_texts,
        )
        == "question_qualifier_not_evidence_bound"
    )


# 4.1 (c): the separable filter keeps the phrase test where it matters.


def test_period_method_and_definition_spans_are_exempt_from_the_phrase_test() -> None:
    arctic = "At the Svalbard station nitrate declined by 15 percent."
    period = ("Sampling ran from June to September 2016.", "period")
    method = ("Nitrate was measured with a Seal AA3 autoanalyser.", "method")
    definition = (
        "Dissolved inorganic nitrogen (DIN) is the sum of nitrate and ammonium.",
        "definition",
    )
    geography = ("Samples were also taken at the Bothnian Bay station.", "geography")
    sample = ("Puffins were sampled in Norway, Iceland and Scotland.", "sample")
    activity = [period, method, definition, geography, sample]
    text = "\n\n".join([arctic, *(row[0] for row in activity)])
    _, _, interpretation = generation._eligible_generation_scope(
        _source("separable_arctic_component", [arctic], activity, ["Svalbard station"]),
        _chunks(text),
    )

    assert [span["text"] for span in interpretation] == [
        period[0],
        method[0],
        definition[0],
    ]
    assert [span["dimension"] for span in interpretation] == [
        "period",
        "method",
        "definition",
    ]


def test_an_unlabelled_span_keeps_the_phrase_test_only_when_it_names_a_place() -> None:
    arctic = "At the Svalbard station nitrate declined by 15 percent."
    period_only = "Sampling ran from June to September 2016 during the campaign."
    other_place = "Samples were also taken at the Bothnian Bay station."
    text = "\n\n".join([arctic, period_only, other_place])
    _, _, interpretation = generation._eligible_generation_scope(
        _source(
            "separable_arctic_component",
            [arctic],
            [period_only, other_place],
            ["Svalbard station"],
        ),
        _chunks(text),
    )

    assert [span["text"] for span in interpretation] == [period_only]
    assert interpretation[0]["dimension"] is None


def test_the_verifier_applicability_test_names_population_sample_and_method() -> None:
    schema = generation.ROLE_SCHEMAS["answer_verifier"]
    description = schema["properties"]["interpretation_scope_applies_to_finding"][
        "description"
    ]

    assert "a population, a sample or a method" in description
    # Kept as the display duty.
    assert validation.DISPLAYED_SCOPE_DIMENSIONS == (
        "geography",
        "period",
        "population",
    )


# 4.1 (d): every scope value cites the span it came from.


def _admission_fixture() -> tuple[list[dict], dict[str, dict], list[dict]]:
    text = f"{STRAIN_SENTENCE}\n\n{FINDING_SENTENCE}"
    chunks = _chunks(text)
    _, _, interpretation = generation._eligible_generation_scope(
        _source("whole_study", [FINDING_SENTENCE], [STRAIN_SENTENCE]), chunks
    )
    spans = {span["span_id"]: span for span in generation._finding_spans(chunks[0])}
    return chunks, spans, interpretation


def _finding_span_id(spans: dict[str, dict]) -> str:
    return next(
        span_id for span_id, span in spans.items() if FINDING_SENTENCE in span["text"]
    )


def test_a_scope_value_without_scope_evidence_is_rejected_before_freeze() -> None:
    chunks, spans, interpretation = _admission_fixture()
    candidates = [
        _candidate(_finding_span_id(spans), _scope(method="lipid content"), None)
    ]

    with pytest.raises(CandidateRejectedError) as error:
        generation._admit_ranked_finding(
            candidates, spans, chunks, None, interpretation, excluded_span_ids=[]
        )

    assert error.value.reason_code == "finding_scope_value_unsourced"
    assert "finding_scope_value_unsourced" in generation.FINDING_ADMISSION_REASK_REASONS


@pytest.mark.parametrize(
    "entry",
    [
        # The span was never supplied.
        {
            "dimension": "method",
            "span_id": "span-from-memory",
            "quote": "lipid content",
        },
        # The quote is not inside the cited span.
        {"dimension": "method", "span_id": "finding", "quote": "Eurasian Basin"},
        # The value is not inside the quote.
        {"dimension": "method", "span_id": "finding", "quote": "cold storage"},
        # The entry names another dimension.
        {"dimension": "period", "span_id": "finding", "quote": "lipid content"},
    ],
)
def test_scope_evidence_must_resolve_to_a_supplied_span_and_quote(entry: dict) -> None:
    chunks, spans, interpretation = _admission_fixture()
    span_id = _finding_span_id(spans)
    entry = {
        **entry,
        "span_id": span_id if entry["span_id"] == "finding" else entry["span_id"],
    }
    candidates = [_candidate(span_id, _scope(method="lipid content"), [entry])]

    with pytest.raises(CandidateRejectedError) as error:
        generation._admit_ranked_finding(
            candidates, spans, chunks, None, interpretation, excluded_span_ids=[]
        )

    assert error.value.reason_code == "finding_scope_value_unsourced"


def test_a_scope_value_cited_from_the_finding_span_is_admitted() -> None:
    chunks, spans, interpretation = _admission_fixture()
    span_id = _finding_span_id(spans)
    candidates = [
        _candidate(
            span_id,
            _scope(method="lipid content", comparison=None),
            [
                {"dimension": "method", "span_id": span_id, "quote": "lipid content"},
                # An entry for a null dimension is dropped, not rejected.
                {"dimension": "period", "span_id": span_id, "quote": "cold storage"},
            ],
        )
    ]

    answer, _, admission = generation._admit_ranked_finding(
        candidates, spans, chunks, None, interpretation, excluded_span_ids=[]
    )

    assert answer["scope_evidence"] == [
        {"dimension": "method", "span_id": span_id, "quote": "lipid content"}
    ]
    assert "scope_context_span_ids" not in answer
    assert admission["contract_version"] == "freeze-time-finding-admission-v2"


def test_a_scope_value_cited_from_an_interpretation_span_becomes_required_context() -> (
    None
):
    chunks, spans, interpretation = _admission_fixture()
    span_id = _finding_span_id(spans)
    context_id = interpretation[0]["span_id"]
    candidates = [
        _candidate(
            span_id,
            _scope(method="lipid content", period="September 2016"),
            [
                {"dimension": "method", "span_id": span_id, "quote": "lipid content"},
                {
                    "dimension": "period",
                    "span_id": context_id,
                    "quote": "Chukchi Sea in September 2016",
                },
            ],
        )
    ]

    answer, _, _ = generation._admit_ranked_finding(
        candidates, spans, chunks, None, interpretation, excluded_span_ids=[]
    )

    assert answer["scope_context_span_ids"] == [context_id]
    assert answer["interpretation_span_ids"] == [context_id]
    # The cited span leads the bundle on every attempt on this finding.
    forwarded = generation._finding_interpretation_spans(answer, interpretation, chunks)
    assert forwarded[0]["span_id"] == context_id
    # The scope value binds through the raw span text at the gate.
    assert validation.scope_is_evidence_bound(
        answer["scope"],
        {"evidence_quote": FINDING_SENTENCE},
        [interpretation[0]["text"]],
    )


def test_a_cited_span_that_carries_the_answer_is_still_dropped() -> None:
    chunks, spans, interpretation = _admission_fixture()
    span_id = _finding_span_id(spans)
    context_id = interpretation[0]["span_id"]
    answer = {
        "text": "Chukchi Sea",
        "variants": [],
        "scope": _scope(period="September 2016"),
        "scope_context_span_ids": [context_id],
        "locator": {"chunk_id": "chunk-1", "start_offset": 0, "end_offset": 1},
    }
    assert span_id

    ordered = generation._finding_interpretation_spans(answer, interpretation, chunks)
    assert [span["span_id"] for span in ordered] == [context_id]
    assert generation._forwarded_context_only_spans(ordered, answer) == []


def test_extractor_schema_requires_scope_evidence_and_the_frozen_schema_keeps_it() -> (
    None
):
    extractor = generation.ROLE_SCHEMAS["extractor"]
    answer = extractor["properties"]["candidate_findings"]["items"]["properties"][
        "answer"
    ]

    assert "scope_evidence" in answer["required"]
    entry = answer["properties"]["scope_evidence"]["items"]
    assert entry["required"] == ["dimension", "span_id", "quote"]
    assert entry["properties"]["dimension"]["enum"] == list(SCOPE_KEYS)
    frozen = generation.FROZEN_ANSWER_SCHEMA["properties"]
    assert "scope_evidence" in frozen
    assert "scope_context_span_ids" in frozen
    joint = generation.ROLE_SCHEMAS["direct_joint"]["properties"]["answer"]
    assert "scope_evidence" in joint["properties"]


def test_the_freeze_time_code_routes_to_the_next_finding() -> None:
    assert "finding_scope_value_unsourced" in streaming.ALTERNATIVE_FINDING_REASONS
    assert (
        "finding_scope_value_unsourced"
        in streaming.IMMEDIATE_ALTERNATIVE_FINDING_REASONS
    )
    assert "finding_scope_value_unsourced" not in streaming.REPAIRABLE_QUESTION_REASONS


# 4.1 (f): definition retrieval.


def _definition_answer(evidence: str, phrases: list[str]) -> dict[str, object]:
    return {
        "text": "12 W m-2",
        "variants": [],
        "evidence_quote": evidence,
        "locator": {
            "chunk_id": "chunk-2",
            "start_offset": 0,
            "end_offset": len(evidence),
        },
        "scope": _scope(method="NET"),
        "required_question_phrases": phrases,
    }


def test_a_gloss_sentence_is_forwarded_as_a_definition_span() -> None:
    methods = (
        "We measured shortwave radiation (SW), longwave radiation (LW), and net "
        "radiation (NET) at the tower (Fig. 2). The tower stood on the tundra."
    )
    results = "NET declined by 12 W m-2 during the melt season at the tower."
    chunks = _chunks(methods, results)
    answer = _definition_answer(results, ["NET"])

    spans = generation._definition_context_spans(answer, chunks, set())

    assert len(spans) == 1
    span = spans[0]
    assert span["span_role"] == "definition"
    assert span["dimension"] == "definition"
    assert span["chunk_id"] == "chunk-1"
    assert methods[span["start_offset"] : span["end_offset"]] == span["text"]
    assert span["text"].endswith("(NET) at the tower (Fig. 2).")
    assert span["display_text"].endswith("(NET) at the tower.")
    assert span["text_sha256"] == sha256_bytes(span["text"].encode("utf-8"))


def test_the_reverse_gloss_form_and_an_abbreviated_binomial_are_retrieved() -> None:
    methods = (
        "The instrument HELIPOD (Helicopter-borne Probe) flew above the sea ice. "
        "Saxifraga polaris was the dominant plant at the site."
    )
    results = "HELIPOD recorded 12 W m-2 over S. polaris stands."
    chunks = _chunks(methods, results)
    answer = _definition_answer(results, ["HELIPOD", "S. polaris"])

    spans = generation._definition_context_spans(answer, chunks, set())

    assert [span["text"] for span in spans] == [
        "The instrument HELIPOD (Helicopter-borne Probe) flew above the sea ice.",
        "Saxifraga polaris was the dominant plant at the site.",
    ]


def test_a_definition_sentence_that_states_the_answer_is_dropped() -> None:
    methods = "Net radiation (NET) reached 12 W m-2 at the tower in 2016."
    results = "NET declined by 12 W m-2 during the melt season at the tower."
    chunks = _chunks(methods, results)
    answer = _definition_answer(results, ["NET"])

    spans = generation._definition_context_spans(answer, chunks, set())
    assert len(spans) == 1
    assert generation._forwarded_context_only_spans(spans, answer) == []


def test_a_gloss_inside_the_finding_span_is_not_forwarded_twice() -> None:
    results = "Net radiation (NET) declined by 12 W m-2 during the melt season."
    chunks = _chunks("The tower stood on the tundra.", results)
    answer = _definition_answer(results, ["net radiation"])
    answer["locator"]["chunk_id"] = "chunk-2"

    assert generation._definition_context_spans(answer, chunks, set()) == []


def test_an_allowlisted_or_glossed_token_triggers_no_scan() -> None:
    methods = "Carbon dioxide (CO2) was measured hourly at the tower."
    results = "CO2 flux declined by 12 W m-2 during the melt season."
    chunks = _chunks(methods, results)
    answer = _definition_answer(results, ["CO2 flux"])

    assert generation._definition_context_spans(answer, chunks, set()) == []


def test_the_gloss_matcher_requires_a_plausible_expansion() -> None:
    assert generation._expansion_matches_token("net radiation", "NET")
    assert generation._expansion_matches_token(
        "Coupled Model Intercomparison Project", "CMIP6"
    )
    assert generation._expansion_matches_token("dissolved organic carbon", "DOC")
    assert not generation._expansion_matches_token("the tower", "NET")
    assert not generation._expansion_matches_token("was measured hourly", "DOC")


# 4.1 (e) and (g): the writer prompt.


def test_the_referent_slot_definition_is_a_resolvability_test() -> None:
    definition = generation.REFERENT_SLOT_DEFINITION

    assert "A displayed task is self-contained when every referent slot is fixed." in (
        definition
    )
    assert (
        "A slot is fixed when a reader who cannot see the paper can name the exact "
        "thing the slot refers to, using only the question and question_context."
    ) in definition
    assert "'The ten selected models' is not fixed." in definition
    assert "'An ensemble of ten CMIP6 models' is fixed." in definition
    assert "'The southern station' is not fixed." in definition
    assert (
        "A slot is not_applicable only when the claim is true whatever the value "
        "of that slot." in definition
    )
    # The presence test is gone.
    assert "A slot is fixed when the question or question_context states it" not in (
        definition
    )


def test_the_writer_schema_requires_resolver_text() -> None:
    items = generation.REFERENT_SLOTS_SCHEMA["items"]

    assert items["required"] == ["slot", "state", "displayed_text", "resolver_text"]
    assert "resolver_text" in items["properties"]
    assert "resolver_text" in generation.REFERENT_SLOT_RECORD_INSTRUCTIONS
    assert (
        "referent_slots is a diagnostic record. It does not replace any "
        "question_context rule and it never supplies a missing slot."
    ) in generation.REFERENT_SLOT_RECORD_INSTRUCTIONS


def test_the_resolvability_record_is_logged_and_never_gates() -> None:
    question = (
        "What was the mean bias of the ten CMIP6 climate models selected for analysis?"
    )
    context = "An ensemble of ten CMIP6 models was evaluated for the Barents Sea."
    slots = [
        {
            "slot": "subject_or_system",
            "state": "stated_in_question",
            "displayed_text": "ten CMIP6 climate models selected for analysis",
            "resolver_text": "ten CMIP6 climate models selected for analysis",
        },
        {
            "slot": "location",
            "state": "stated_in_context",
            "displayed_text": "the Barents Sea",
            "resolver_text": "",
        },
        {
            "slot": "period_or_event",
            "state": "stated_in_context",
            "displayed_text": "the Barents Sea",
            "resolver_text": "during the 1990s",
        },
        {
            "slot": "measured_variable",
            "state": "stated_in_question",
            "displayed_text": "mean bias",
            "resolver_text": "mean bias of the ten CMIP6 climate models",
        },
        {
            "slot": "acronym",
            "state": "not_applicable",
            "displayed_text": "",
            "resolver_text": "",
        },
    ]

    record = generation._referent_slot_resolvability_record(slots, question, context)

    assert record["contract_version"] == "referent-slot-resolvability-v2"
    assert record["mode"] == "shadow"
    assert record["unresolved_slots"] == [
        {
            "slot": "subject_or_system",
            "state": "stated_in_question",
            "reason": "resolver_text_equals_displayed_text",
        },
        {
            "slot": "location",
            "state": "stated_in_context",
            "reason": "resolver_text_empty",
        },
        {
            "slot": "period_or_event",
            "state": "stated_in_context",
            "reason": "resolver_text_not_displayed",
        },
    ]


def test_context_only_qualifiers_are_placed_in_question_context() -> None:
    placement = (
        "A qualifier that you take from CONTEXT_ONLY_SOURCE must go in "
        "question_context. Never put it in the question stem. A qualifier that the "
        "SOURCE_DATA evidence span itself states can go in either field."
    )

    assert placement in generation.QUESTION_CONTEXT_INSTRUCTIONS
    assert placement in generation.REQUIRED_PHRASE_COVERAGE_INSTRUCTIONS
    assert "when the question reads better without it" not in (
        generation.REQUIRED_PHRASE_COVERAGE_INSTRUCTIONS
    )


def test_the_verbatim_scope_rule_is_stated_to_the_writer() -> None:
    rule = generation.VERBATIM_SCOPE_DISPLAY_INSTRUCTIONS

    assert "word for word" in rule
    assert "Add words around that phrase, never inside it." in rule
    assert "geography, period and population strings of ANSWER_RECORD.scope" in rule


def test_one_supported_setting_sentence_is_encouraged() -> None:
    instructions = generation.QUESTION_CONTEXT_INSTRUCTIONS

    assert instructions.startswith("Write question_context for every question.")
    assert "Adding one supported setting sentence is correct." in instructions
    assert (
        "When SOURCE_DATA or CONTEXT_ONLY_SOURCE states the study place, the study "
        "period or the sample set, state it in question_context in the source's own "
        "words" in instructions
    )
    # An empty context stays legal (audit 4.10 refuted the deterministic reject).
    assert "Leave it empty only when the question alone names" in instructions


def test_every_anti_leakage_rule_and_ban_survives() -> None:
    context = generation.QUESTION_CONTEXT_INSTRUCTIONS
    standalone = generation.BENCHMARK_STANDALONE_INSTRUCTIONS

    assert "Do not invent a slot value that neither source states." in context
    assert "Do not include answer-bearing numbers" in context
    assert "If an acronym expansion answers the question, do not supply that" in context
    assert "Do not infer or invent a definition." in context
    assert "Expand an unfamiliar acronym only when its expansion occurs in" in context
    assert "Do not add answer-bearing information to resolve a referent." in standalone
    assert "Never state the proposed answer in the question or question_context" in (
        standalone
    )
    assert (
        "A scientific referent does not require a paper title, DOI, author, journal, "
        "dataset, or campaign identity. Never add one as a context shortcut."
    ) in standalone
    for example in (
        "'the southern station'",
        "'the identified OTUs'",
        "'the sampled group'",
        "'this experiment'",
    ):
        assert example in standalone
    assert generation.CONTEXT_ONLY_SOURCE_INSTRUCTIONS == (
        "CONTEXT_ONLY_SOURCE supports question_context statements only. "
        "Never select a CONTEXT_ONLY_SOURCE span as answer evidence, as a scope value, "
        "or as a required question phrase."
    )
    assert "Do not restate the question" in generation.ANSWER_FORMAT_INSTRUCTIONS
    assert "complete typed set" in generation.CLOSED_SET_INSTRUCTIONS


def test_refuted_period_rule_is_absent() -> None:
    # Audit 4.10: W6, a bare season fails the period slot, was refuted.
    period = generation.DISPLAYED_PERIOD_INSTRUCTIONS
    assert "A season name" not in period
    assert "the past N years" in period


# Contract versions this slice owns.


def test_chapter_three_contract_versions_are_recorded() -> None:
    assert generation.CANDIDATE_SCHEMA_VERSION == "2.8.0"
    assert generation.PROMPT_VERSION == "arctic-qa-generation-v23"
    assert validation.CONTEXT_ONLY_EVIDENCE_CONTRACT_VERSION == (
        "question-context-redacted-evidence-v2"
    )
    assert validation.REFERENT_SLOT_CONTRACT_VERSION == "referent-slot-resolvability-v2"
    assert validation.FINDING_ADMISSION_CONTRACT_VERSION == (
        "freeze-time-finding-admission-v2"
    )
    assert "2.8.0" in validation.CONTEXT_ONLY_EVIDENCE_SCHEMA_VERSIONS
    row = validation.CANDIDATE_CONTRACTS["2.8.0"]
    assert row["prompt_version"] == "arctic-qa-generation-v23"
    assert row["context_only_evidence_contract_version"] == (
        "question-context-redacted-evidence-v2"
    )
    assert row["referent_slot_contract_version"] == "referent-slot-resolvability-v2"
    assert row["finding_admission_contract_version"] == (
        "freeze-time-finding-admission-v2"
    )
    # Sibling-owned versions, re-pinned at integration to what each slice landed.
    assert row["generation_attempt_contract_version"] == "bounded-failure-routing-v5"
    assert row["standalone_verification_contract_version"] == (
        "source-blind-scientific-referent-v5"
    )
    assert row["question_verification_contract_version"] == "question-verification-v2"
    assert row["numeric_rule_contract_version"] == "numeric-rule-source-support-v4"
    assert row["option_display_contract_version"] == "displayed-option-structure-v1"
    # The option verification contract exists only from 2.8.0 (judge-options).
    assert set(row) == set(validation.CANDIDATE_CONTRACTS["2.7.0"]) | {
        "option_verification_contract_version"
    }


def test_the_chapter_two_row_and_new_row_disagree_only_on_owned_versions() -> None:
    old = validation.CANDIDATE_CONTRACTS["2.7.0"]
    new = validation.CANDIDATE_CONTRACTS["2.8.0"]

    # The four writer-context versions, plus the three a sibling slice moved
    # (standalone v4, numeric-rule v4, routing v5), reconciled at integration.
    assert {key for key in old if old[key] != new[key]} == {
        "prompt_version",
        "context_only_evidence_contract_version",
        "referent_slot_contract_version",
        "finding_admission_contract_version",
        "standalone_verification_contract_version",
        "numeric_rule_contract_version",
        "generation_attempt_contract_version",
    }


def test_the_gate_reasons_read_the_displayed_projection_for_leaks() -> None:
    raw = "The lipid content declined by 15 [3] percent at the northern site."
    displayed = context_projection.context_only_display_text(raw)
    evidence = FINDING_SENTENCE
    chunk = {"chunk_id": "chunk-1", "text": evidence}
    locator = {"chunk_id": "chunk-1", "start_offset": 0, "end_offset": len(evidence)}
    scope = _scope(method="lipid content")
    answer = {
        "text": "15 percent",
        "variants": [],
        "evidence_quote": evidence,
        "locator": locator,
        "scope": scope,
        "required_question_phrases": ["lipid content"],
        "claim_type": "observation",
    }
    reconstruction = {
        "answer": "15 percent",
        "evidence_quote": evidence,
        "locator": locator,
        "scope": deepcopy(scope),
        "ambiguity_label": "one_answer",
        "question_claim_type": "observation",
    }
    verification = {
        **reconstruction,
        "scope": deepcopy(scope),
        "source_entailment_model_verified": True,
        "relation_scope_match": True,
        "ambiguity_resolved": True,
        "alternative_answer_search_passed": True,
        "question_context_required": False,
        "question_context_source_supported": True,
        "question_context_answer_leakage_absent": True,
        "interpretation_scope_applies_to_finding": True,
        "question_verification_contract_version": (
            generation.QUESTION_VERIFICATION_CONTRACT_VERSION
        ),
        "question_context_referent_resolved": True,
        "question_context_missing_detail": "",
        "question_answer_leakage_absent": True,
        "scope_value_contradicted_by_source": False,
        "contradicted_scope_field": "",
        "scope_representation_note": "",
    }
    span = {
        "span_id": "s1",
        "chunk_id": "chunk-1",
        "start_offset": 0,
        "end_offset": len(raw),
        "text_sha256": sha256_bytes(raw.encode("utf-8")),
        "text": raw,
        "display_text": displayed,
    }

    reasons = generation._qa_gate_reasons(
        chunk,
        "By how much did the lipid content decline after cold storage?",
        answer,
        reconstruction,
        verification,
        "",
        None,
        answer_agreement=None,
        standalone_verification={
            "pass": True,
            "answer_leakage_absent": True,
            "unresolved_phrases": [],
            "missing_detail_types": [],
            "reasons": [],
        },
        interpretation_spans=[span],
    )

    assert "interpretation_span_contains_answer" in reasons
