from __future__ import annotations

import json

import pytest

from arctic_qa import validation
from arctic_qa.util import canonical_json, sha256_bytes


def test_scope_phrase_projection_matches_alpha_line_break_hyphenation() -> None:
    assert validation._scope_phrase_in_text(
        "photosynthetic biomass", "photosyn-\nthetic biomass"
    )
    assert validation._scope_phrase_in_text("winter", "win-\nter")
    assert validation.scope_is_evidence_bound(
        {
            "population": "standing stock of photosynthetic biomass",
            "comparison": "between summer and winter",
        },
        {
            "evidence_quote": (
                "The standing stock of photosyn-\nthetic biomass decreased "
                "between summer and win-\nter."
            )
        },
    )


@pytest.mark.parametrize(
    ("phrase", "text"),
    [
        ("15–20°N", "15-20°N"),
        ("−80°C", "-80°C"),
        ("mg L−1", "mg L-1"),
        ("not retained", "retained"),
        ("non-Arctic", "non-\nArctic"),
        ("Mys Vankarem", "Mys Vanka-"),
        ("winter", "win- ter"),
        ("Arctic station", "Arctic"),
    ],
)
def test_scope_phrase_projection_rejects_unsafe_differences(
    phrase: str, text: str
) -> None:
    assert not validation._scope_phrase_in_text(phrase, text)


def test_scope_phrase_projection_does_not_aggregate_across_evidence() -> None:
    assert not validation.scope_is_evidence_bound(
        {"geography": "Arctic station"},
        {"evidence_quote": "The Arctic result was reported."},
    )


def test_eligible_scope_phrase_binding_accepts_a_split_question_phrase() -> None:
    finding_quote = "The result occurred in win-\nter."
    scope = {
        "component": "separable_arctic_component",
        "question_scope_phrases": ["winter"],
        "eligibility_job_key": "job-1",
        "finding_spans": [
            {
                "span_id": "s1",
                "quote": finding_quote,
                "source_bytes_sha256": sha256_bytes(finding_quote.encode()),
                "locator": {"source_block_id": "block-1"},
            }
        ],
    }
    candidate = {
        "schema_version": "2.3.0",
        "question": "Which result occurred in win-\nter?",
        "answer": {"evidence_quote": finding_quote},
        "provenance": {
            "eligible_arctic_scope": scope,
            "eligible_arctic_scope_sha256": sha256_bytes(
                canonical_json(scope).encode()
            ),
        },
    }
    source = {
        "scope_rule_version": "gemini-fulltext-arctic-eligibility-v2",
        "scope_evidence_json": json.dumps(
            {
                "eligibility_job_key": "job-1",
                "resolved_eligible_arctic_scope": scope,
            }
        ),
    }

    assert validation._eligible_arctic_scope_error(candidate, source) is None
