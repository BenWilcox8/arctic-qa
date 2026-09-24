"""Rank retained papers by the chance the pipeline can build a question.

The pipeline pays for a full-text eligibility call on every paper it opens, and
most papers end there. This module asks a cheap typed-judgement model, TypeSafe
Jev, a small batched question set over the article's own full text, and turns
the answers into one ranking probability. The ranking only reorders the papers
the pipeline opens; it decides nothing and removes no paper.

The module never touches the shared paid-call ledger, the producer or the
chapter 3 state. It keeps its own small append-only call ledger with its own
USD ceiling. ``docs/JEV_PRESCREEN.md`` holds the contract and the cost.

Actions, through ``python -m arctic_qa jev-prescreen --action <action>``:

``build-manifest``  the input manifest over the retained set, free
``screen``          the paid Jev calls, one per paper, recorded
``rank``            the ranking, free, from the recorded responses alone
``write-order``     a ranked frozen manifest a future launch can consume
``calibrate``       how well the ranking separates this run's own outcomes
``estimate``        the projected cost of a screen, free
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .db import now
from .paths import configured_config_dir
from .util import atomic_json, atomic_write, canonical_json, jsonl_bytes, sha256_bytes


CONTRACT_ID = "arctic-qa-jev-prescreen-v1"
MANIFEST_SCHEMA = "jev-prescreen-manifest-item-v1"
RESULT_SCHEMA = "jev-prescreen-result-v1"
LEDGER_SCHEMA = "jev-prescreen-ledger-v1"
CALL_SCHEMA = "jev-prescreen-call-v1"
RANKING_SCHEMA = "jev-prescreen-ranking-v1"
CALIBRATION_SCHEMA = "jev-prescreen-calibration-v1"

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# `jev-latest` is the one model id the API reference documents. It floats: a
# request for it is served by a concrete version, and the response names that
# version. So every receipt records the answered model, and the ranking counts
# the distinct answered models of a screen, which is how a version change in
# the middle of a screen becomes visible. Pin a concrete version with --model
# once `models.list()` on a live key has named one.
DEFAULT_MODEL = "jev-latest"

# The credential is configuration, never a constant of the code, and is never
# printed. The environment variable wins; the file is the local default.
CREDENTIAL_ENV = "TYPESAFE_API_KEY"
CREDENTIAL_FILENAME = "typesafe-api-key"

# TypeSafe publishes no price page. This is the constant its own cookbooks
# carry, and the documentation disclaims it as an assumption rather than a
# billed rate, so every receipt records it and names it as unverified.
PRICE_USD_PER_MILLION_INPUT_TOKENS = Decimal("0.042")
PRICE_USD_PER_MILLION_OUTPUT_TOKENS = Decimal("0.00")
PRICE_SOURCE = "https://docs.typesafe.ai/cookbooks/parallel_questions.md"
PRICE_STATUS = "unverified_cookbook_constant"

# The provider publishes no state size limit, and the calibration screen of
# 2026-09-17 measured one: it refuses a request with HTTP 400 and the body
# `max_tokens_exceeded`. The largest accepted request of that screen carried
# 32,350 input tokens and the refusals lay above it, so the limit is taken as
# 32,768 and the default budget keeps headroom under it.
PROVIDER_TOKEN_LIMIT = 32_768
DEFAULT_STATE_TOKEN_BUDGET = 30_000

# Characters per token, measured over the 116 recorded calls of that screen
# that carry a real usage record: 5.38 at best, 3.88 median, 1.51 at worst.
# Every worst-ratio paper is a Cyrillic journal. A flat ratio of 4 was safe for
# Latin text and wrong by 2.6 times for Cyrillic, which both under-projected
# the cost and pushed a state over the provider limit.
ASCII_CHARACTERS_PER_TOKEN = 4.0
WIDE_CHARACTERS_PER_TOKEN = 1.5
QUESTION_OVERHEAD_TOKENS = 700

# How many times a refused request is retried at half the budget.
OVERLARGE_RETRIES = 3

# The provider names an oversized request in its error body with this marker.
OVERLARGE_MARKER = "max_tokens_exceeded"

DEFAULT_WORKERS = 4
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_ATTEMPTS = 4
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})

# How much of the ranking the gates carry. Run chapter3-7dc6485-r3 lost 84
# papers to causes the gate questions name and 87 to causes the quality
# questions name, so the two sides weigh almost the same. This is a measured
# value, not a preference, and a later run re-measures it.
GATE_EXPONENT = Decimal("0.5")


def _noul(
    key: str,
    weight: str,
    role: str,
    instructions: str,
    yes: str,
    no: str,
    targets: Sequence[str],
) -> dict[str, Any]:
    return {
        "key": key,
        "type": "noul",
        "role": role,
        "weight": weight,
        "instructions": instructions,
        "criteria": {"true": yes, "false": no},
        "targets": list(targets),
    }


def _score(
    key: str,
    weight: str,
    role: str,
    instructions: str,
    levels: Sequence[str],
    targets: Sequence[str],
) -> dict[str, Any]:
    return {
        "key": key,
        "type": "score",
        "role": role,
        "weight": weight,
        "instructions": instructions,
        "criteria": list(levels),
        "targets": list(targets),
    }


def _choice(
    key: str,
    weight: str,
    role: str,
    instructions: str,
    options: dict[str, str],
    good_options: Sequence[str],
    targets: Sequence[str],
) -> dict[str, Any]:
    """A choice question, read as the probability mass of its good options."""
    if not set(good_options) <= set(options):
        raise ValueError(f"the good options of {key} are not among its options")
    return {
        "key": key,
        "type": "choice",
        "role": role,
        "weight": weight,
        "instructions": instructions,
        "criteria": dict(options),
        "good_options": list(good_options),
        "targets": list(targets),
    }


# One narrow judgement per question, every question answerable as a probability,
# and every question tied to a rejection reason this run actually recorded.
# `targets` names those codes and `docs/JEV_PRESCREEN.md` holds the counts,
# measured over the 276 papers run chapter3-7dc6485-r3 had completed.
#
# A gate is a near-necessary condition: a paper that fails it cannot yield a
# question at all, so the gates multiply. A quality term separates the papers
# that pass the gates, so the quality terms average. The two sides carry almost
# almost the same measured loss, 84 papers against 87, which is why they weigh
# the same in the end (GATE_EXPONENT).
QUESTIONS: tuple[dict[str, Any], ...] = (
    # ---------------- gates: what ends a paper at eligibility ----------------
    _choice(
        "geography_status",
        "2.0",
        "gate",
        "Read the article's own methods and results, not its title, its "
        "background or its citations. Where were this study's own "
        "observations, samples, field sites, stations, cruises, or modelled "
        "or remote-sensing domain located?",
        {
            "arctic_activity_stated": "The text states that an observation, a "
            "sample, a field site, a station, a cruise or a modelled domain of "
            "this study's own activity is at or north of 66.56 degrees north, "
            "or inside a named region wholly north of that line.",
            "outside_or_incidental": "The text states that all of this study's "
            "own activity and results are south of 66.56 degrees north, or the "
            "only Arctic mention is in the title, an affiliation, a background "
            "statement or a citation to other work.",
            "not_stated": "The text does not say where this study's own "
            "activity took place.",
        },
        ["arctic_activity_stated"],
        # 29 criterion_failed:study_geography, 28 criterion_unresolved, 3
        # criterion_evidence_missing. The largest predictable loss of the run.
        [
            "criterion_failed:study_geography",
            "criterion_unresolved:study_geography",
            "criterion_evidence_missing:study_geography",
        ],
    ),
    _choice(
        "article_type",
        "1.5",
        "gate",
        "What does this article report?",
        {
            "primary_research": "The article reports the authors' own original "
            "observations, samples, experiment or model run, with its own "
            "methods and its own results.",
            "review_or_synthesis": "The article reviews, synthesises or "
            "comments on other people's findings, and reports no new primary "
            "result of its own.",
            "other_non_primary": "The article is a dataset description, a "
            "protocol, an editorial, a correction, a conference abstract or "
            "another format that is not primary research.",
        },
        ["primary_research"],
        # 21 criterion_failed:published_primary_findings.
        ["criterion_failed:published_primary_findings"],
    ),
    _noul(
        "reports_own_finding",
        "1.5",
        "gate",
        "Does the article state, in its own prose, at least one specific "
        "numeric or categorical result of this study: a named measured "
        "quantity, a value, and its unit or category?",
        "A results sentence names a measured quantity and gives this study's "
        "own value for it.",
        "The results are only qualitative, or every number belongs to another "
        "author's work, to the method setup, or to a sample identifier, a "
        "catalogue number or a station code rather than a measurement.",
        # 3 no_admissible_finding, and the floor every other question assumes.
        ["no_admissible_finding"],
    ),
    _noul(
        "extraction_complete",
        "1.0",
        "gate",
        "Is this text a complete, readable article body?",
        "The text holds the article's own prose from its start to its "
        "discussion or its conclusions, and is readable.",
        "The text is a title page only, a reference list only, an abstract "
        "only, unreadable character noise, or is cut off before the results.",
        # The extraction floor. extractor_response_invalid is 23 papers, and
        # most of those are provider faults rather than bad text, so this
        # question claims only the part a reader of the text can see.
        ["extractor_response_invalid"],
    ),
    # ------ quality: what ends a paper after eligibility has passed it ------
    _noul(
        "finding_not_figure_dependent",
        "2.0",
        "quality",
        "Is this study's main finding, with its value, stated directly in a "
        "sentence of prose, so that a reader who cannot see any figure or "
        "table still knows exactly what was measured and what the value "
        "means?",
        "The finding sentence stands on its own, and no figure or table "
        "caption is needed to know what its referent is.",
        "The value's meaning depends on seeing a figure or a table, or the "
        "article never names the referent outside a caption.",
        # 36 finding_span_figure_defined_referent, the largest generation code.
        ["finding_span_figure_defined_referent"],
    ),
    _noul(
        "scope_bound_with_finding",
        "2.0",
        "quality",
        "Does the same sentence, or the same short paragraph, that states this "
        "study's main value also state the place, the time period and the "
        "sample or population it belongs to?",
        "The place, the period and the sample sit together with the value in "
        "one span of text.",
        "At least one of the place, the period or the sample is missing near "
        "the value, or is only findable in a different, distant part of the "
        "article.",
        # 24 in the scope-unsourced family plus 9 slot_evidence_unavailable.
        [
            "finding_scope_value_unsourced",
            "eligible_arctic_scope_finding_unbound",
            "scope_qualifier_missing",
            "scope_qualifier_not_source_bound",
            "answer_scope_not_source_bound",
            "reconstruction_scope_not_source_bound",
            "slot_evidence_unavailable",
        ],
    ),
    _score(
        "self_contained_claim",
        "1.0",
        "quality",
        "How well could this article's main finding be written as one sentence "
        "that a reader who has never seen the article fully understands?",
        [
            "The main finding cannot be stated without a referent that only "
            "the article defines, such as 'Site A', 'the treatment group' or "
            "'the study period'.",
            "The main finding needs several of those referents replaced before "
            "it stands alone.",
            "The main finding needs one such referent replaced.",
            "The main finding already names its own place, period and measured "
            "quantity in full, with no article-only referent left.",
        ],
        # The captain's own phrase, and the axis behind
        # standalone_det_question_context_referent_unresolved (5).
        ["standalone_det_question_context_referent_unresolved"],
    ),
    _noul(
        "arctic_attributed_finding",
        "0.5",
        "quality",
        "If this study reports results from more than one region or component, "
        "is the clearest quantitative finding inside its Arctic part rather "
        "than in a non-Arctic comparison?",
        "The clearest value sits inside the Arctic component, or the whole "
        "study is Arctic and the question does not divide it.",
        "The clearest value belongs to a non-Arctic comparison or to another "
        "regional component of the study.",
        # 13 eligible_arctic_scope_missing_from_finding. It only divides the
        # papers that report a separable Arctic component, so it weighs least.
        ["eligible_arctic_scope_missing_from_finding"],
    ),
)

QUESTION_KEYS: tuple[str, ...] = tuple(question["key"] for question in QUESTIONS)
GATE_KEYS: tuple[str, ...] = tuple(
    question["key"] for question in QUESTIONS if question["role"] == "gate"
)
QUALITY_KEYS: tuple[str, ...] = tuple(
    question["key"] for question in QUESTIONS if question["role"] == "quality"
)


def question_payload() -> dict[str, dict[str, Any]]:
    """Return the `questions` map exactly as it is sent to the provider."""
    payload: dict[str, dict[str, Any]] = {}
    for question in QUESTIONS:
        entry: dict[str, Any] = {
            "type": question["type"],
            "instructions": question["instructions"],
        }
        if question["criteria"]:
            entry["criteria"] = question["criteria"]
        # `good_options`, `role`, `weight` and `targets` are ours. The provider
        # takes only the three fields its own request schema defines.
        payload[question["key"]] = entry
    return payload


def question_set_sha256() -> str:
    """Return the hash that binds a recorded answer to this question set."""
    return sha256_bytes(canonical_json(question_payload()).encode())


class JevPrescreenError(Exception):
    """A prescreen refusal that the operator has to read."""


class JevCeilingReached(JevPrescreenError):
    """The prescreen ledger's own USD ceiling stops further calls."""


class JevOverlargeRequestError(JevPrescreenError):
    """The provider refused the request body itself, with HTTP 422.

    The provider publishes no state size limit, so a refusal of an article this
    module considered small enough is the only signal that one exists. The
    screen answers it once, with a smaller state.
    """


# --------------------------------------------------------------------------
# The credential


def credential_file() -> Path:
    """Return the default TypeSafe key file inside the credential directory."""
    return configured_config_dir() / CREDENTIAL_FILENAME


def read_api_key(key_file: Path | None = None) -> str:
    """Return the API key from the environment or the credential file.

    The key is never printed, never written to a receipt and never put in an
    error message.
    """
    value = os.environ.get(CREDENTIAL_ENV, "").strip()
    if value:
        return value
    path = key_file or credential_file()
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise JevPrescreenError(
        f"no TypeSafe API key: set {CREDENTIAL_ENV} or write {path}"
    )


# --------------------------------------------------------------------------
# The input manifest


def wide_characters(text: str) -> int:
    """Return how many characters of the text are outside plain ASCII."""
    return sum(1 for character in text if ord(character) > 127)


def estimated_tokens(characters: int, wide: int = 0) -> int:
    """Return the token estimate this module bills and bounds against.

    A Cyrillic or other non-Latin character costs far more tokens than a Latin
    one, so the two are counted apart. `wide` is how many of `characters` are
    outside ASCII.
    """
    if wide < 0 or wide > characters:
        raise JevPrescreenError("the wide character count is outside the text")
    plain = characters - wide
    return int(plain / ASCII_CHARACTERS_PER_TOKEN + wide / WIDE_CHARACTERS_PER_TOKEN)


def estimate_text_tokens(text: str) -> int:
    """Return the token estimate of one piece of text."""
    return estimated_tokens(len(text), wide_characters(text))


def characters_per_token(text: str) -> float:
    """Return how many characters of this text fit in one token.

    A budget in tokens becomes a budget in characters through this ratio, which
    is measured from the text's own script mix rather than assumed.
    """
    if not text:
        return ASCII_CHARACTERS_PER_TOKEN
    tokens = estimate_text_tokens(text)
    return len(text) / tokens if tokens else ASCII_CHARACTERS_PER_TOKEN


def build_manifest(
    *,
    dispositions_file: Path,
    freeze_manifest_file: Path,
    output_dir: Path,
    retained_disposition: str = "retained_article_type",
) -> dict[str, Any]:
    """Write the prescreen input manifest over the whole retained set.

    Every retained paper gets a row. A row carries the full-text path and the
    token estimate when the text is on disk, and ``has_full_text`` false when
    it is not, so the papers that still need retrieval stay visible and
    counted.
    """
    frozen: dict[str, dict[str, Any]] = {}
    with freeze_manifest_file.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            frozen[str(row["candidate_key"])] = row

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with dispositions_file.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("metadata_disposition") != retained_disposition:
                continue
            candidate_key = str(record["candidate_key"])
            if candidate_key in seen:
                continue
            seen.add(candidate_key)
            rows.append(_manifest_row(candidate_key, frozen.get(candidate_key)))

    # The frozen manifest position is the pipeline's own order, so it is the
    # stable tie-break. Papers with no full text sort after it, by key.
    rows.sort(
        key=lambda row: (
            row["frozen_manifest_position"] is None,
            row["frozen_manifest_position"] or 0,
            row["candidate_key"],
        )
    )
    for position, row in enumerate(rows, start=1):
        row["manifest_position"] = position

    with_text = [row for row in rows if row["has_full_text"]]
    total_tokens = sum(row["estimated_tokens"] or 0 for row in with_text)
    counts = {
        "retained": len(rows),
        "with_full_text": len(with_text),
        "without_full_text": len(rows) - len(with_text),
        "estimated_state_tokens": total_tokens,
    }
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = output_dir / "prescreen-manifest.jsonl"
    atomic_write(manifest_file, jsonl_bytes(rows), immutable=True)
    descriptor = {
        "schema": "jev-prescreen-manifest-descriptor-v1",
        "contract_id": CONTRACT_ID,
        "created_at_utc": now(),
        "retained_disposition": retained_disposition,
        "dispositions_file": str(dispositions_file.resolve()),
        "freeze_manifest_file": str(freeze_manifest_file.resolve()),
        "manifest_file": str(manifest_file),
        "manifest_sha256": sha256_bytes(manifest_file.read_bytes()),
        "counts": counts,
        "projected_cost_usd": str(projected_cost_usd(total_tokens, len(with_text))),
    }
    atomic_json(
        output_dir / "prescreen-manifest-descriptor.json", descriptor, immutable=True
    )
    return descriptor


def _manifest_row(candidate_key: str, frozen: dict[str, Any] | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "candidate_key": candidate_key,
        "paper_id": candidate_key,
        "doi": None,
        "family_key": candidate_key,
        "title": None,
        "year": None,
        "has_full_text": False,
        "extraction_path": None,
        "extraction_sha256": None,
        "source_characters": None,
        "non_ascii_characters": None,
        "estimated_tokens": None,
        "frozen_manifest_position": None,
        "manifest_position": None,
    }
    if frozen is None:
        return row
    receipt = frozen.get("access_receipt") or {}
    path = Path(str(receipt.get("extraction_path") or ""))
    row.update(
        {
            "doi": frozen.get("doi"),
            "family_key": str(frozen.get("family_key") or candidate_key),
            "title": frozen.get("title"),
            "year": frozen.get("year"),
            "extraction_sha256": receipt.get("extraction_sha256"),
            "frozen_manifest_position": frozen.get("manifest_position"),
        }
    )
    if receipt.get("access_state") == "full_text_ready" and path.is_file():
        characters, wide = _character_count(path)
        row.update(
            {
                "has_full_text": True,
                "extraction_path": str(path),
                "source_characters": characters,
                "non_ascii_characters": wide,
                "estimated_tokens": estimated_tokens(characters, wide),
            }
        )
    return row


def _character_count(path: Path) -> tuple[int, int]:
    """Return the character count of an extraction, and its wide share."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return len(text), wide_characters(text)


def read_manifest(manifest_file: Path) -> list[dict[str, Any]]:
    """Return the manifest rows in their recorded order."""
    rows = [
        json.loads(line)
        for line in manifest_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise JevPrescreenError(f"the prescreen manifest is empty: {manifest_file}")
    # `build_manifest` cannot produce a duplicate, but a hand-edited manifest
    # can, and two workers would then pay for the same paper twice.
    keys = [str(row["candidate_key"]) for row in rows]
    if len(set(keys)) != len(keys):
        raise JevPrescreenError(
            f"the prescreen manifest repeats a candidate: {manifest_file}"
        )
    return rows


# --------------------------------------------------------------------------
# The state: what text of the article is sent


# The extracted text is a flat PDF dump whose headings are rarely on their own
# line, so a section split cannot be trusted. Only the reference list is found
# reliably and late, so that is the one section this rule cuts.
_REFERENCES = re.compile(
    r"(?mi)^[^\S\n]{0,60}(?:\d+[.)]?[^\S\n]{1,6})?"
    r"(references|bibliography|literature cited|works cited)[^\S\n]*:?[^\S\n]*$"
)
_ELISION = "\n\n[... middle of the article omitted by the prescreen budget ...]\n\n"


def select_state_text(
    text: str, *, token_budget: int = DEFAULT_STATE_TOKEN_BUDGET
) -> dict[str, Any]:
    """Return the state text for one paper, and how it was chosen.

    The budget is in tokens, because the provider's limit is in tokens. It
    becomes a character budget through this text's own script mix, so a
    Cyrillic article is cut shorter than a Latin one of the same length.

    Whole text when it fits. Otherwise the reference list is cut first, because
    it carries no finding of this study. If the body alone is still over the
    budget, the head and the tail are kept and the middle is elided: the head
    carries the title, the abstract and the introduction, and the tail carries
    the discussion and the conclusions, which is where a stated main finding
    lives.
    """
    if token_budget < 250:
        raise JevPrescreenError("the state token budget must be at least 250")
    budget = max(1000, int(token_budget * characters_per_token(text)))
    source_characters = len(text)
    if source_characters <= budget:
        return _selection(text, "whole", [(0, source_characters)], source_characters)

    body_end = _references_start(text)
    body = text[:body_end]
    if len(body) <= budget:
        return _selection(
            body, "references_trimmed", [(0, body_end)], source_characters
        )

    head = (budget * 6) // 10
    tail = budget - head
    state = body[:head] + _ELISION + body[body_end - tail : body_end]
    rule = (
        "head_tail" if body_end == source_characters else "references_trimmed_head_tail"
    )
    spans = [(0, head), (body_end - tail, body_end)]
    return _selection(state, rule, spans, source_characters)


def _selection(
    state: str, rule: str, spans: Sequence[tuple[int, int]], source_characters: int
) -> dict[str, Any]:
    return {
        "state_text": state,
        "selection_rule": rule,
        "character_budget": len(state),
        "source_characters": source_characters,
        "state_characters": len(state),
        "state_sha256": sha256_bytes(state.encode()),
        "spans": [[int(start), int(stop)] for start, stop in spans],
        "estimated_tokens": estimate_text_tokens(state),
    }


def _references_start(text: str) -> int:
    """Return where the trailing reference list starts, or the text length.

    Only a marker in the last half of the article is taken, because the word
    appears in the body of many articles, and the last such marker wins. Over
    the 4,420 frozen papers 1,710 carry a heading this pattern finds, and the
    last one sits at a median 0.777 of the text and a fifth percentile of
    0.604, so the half-way floor keeps 1,678 of those 1,710 and refuses the
    rest as body prose.
    """
    floor = len(text) // 2
    start = len(text)
    for match in _REFERENCES.finditer(text):
        if match.start() >= floor:
            start = match.start()
    return start


# --------------------------------------------------------------------------
# The request and the answers


def build_request(state_text: str, *, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Return the POST body for one paper: one state, every question batched."""
    return {"state": state_text, "model": model, "questions": question_payload()}


def request_sha256(body: dict[str, Any]) -> str:
    """Return the hash that identifies one request exactly."""
    return sha256_bytes(canonical_json(body).encode())


def read_probabilities(answers: dict[str, Any]) -> dict[str, Decimal]:
    """Return one probability in [0,1] per question, from a Jev answer map.

    A ``noul`` answer is already a probability. A ``choice`` answer gives a
    probability per option, so the value is the mass of the question's good
    options. A ``score`` answer is the probability-weighted level, so it is
    divided by the top level index.
    """
    values: dict[str, Decimal] = {}
    for question in QUESTIONS:
        key = question["key"]
        answer = answers.get(key)
        if not isinstance(answer, dict):
            raise JevPrescreenError(f"the answer for {key} is missing")
        if answer.get("type") != question["type"]:
            raise JevPrescreenError(f"the answer for {key} is not a {question['type']}")
        if question["type"] == "noul":
            value = _as_number(
                answer.get("noul"), f"the answer for {key} is not a noul"
            )
        elif question["type"] == "choice":
            value = _choice_mass(question, answer)
        else:
            score = _as_number(
                answer.get("score"), f"the answer for {key} is not a score"
            )
            top = len(question["criteria"]) - 1
            if top < 1:
                raise JevPrescreenError(f"the question {key} has fewer than two levels")
            value = score / Decimal(top)
        if value < 0 or value > 1:
            raise JevPrescreenError(f"the probability for {key} is outside [0,1]")
        values[key] = value
    return values


def _as_number(value: Any, message: str) -> Decimal:
    """Return a JSON number as a Decimal, and refuse anything else.

    `bool` is a subclass of `int` in Python, so a JSON `true` would pass a
    plain `isinstance` check and then fail inside `Decimal`, which raises an
    error this module does not catch. It is rejected here instead.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevPrescreenError(message)
    return Decimal(str(value))


def _choice_mass(question: dict[str, Any], answer: dict[str, Any]) -> Decimal:
    """Return the probability mass the answer puts on the good options."""
    key = question["key"]
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict):
        raise JevPrescreenError(f"the answer for {key} has no probabilities")
    missing = set(question["criteria"]) - set(probabilities)
    if missing:
        raise JevPrescreenError(
            f"the answer for {key} omits an option: {sorted(missing)[0]}"
        )
    mass = Decimal("0")
    for option in question["good_options"]:
        mass += _as_number(
            probabilities[option], f"the answer for {key} has a bad probability"
        )
    return mass


# --------------------------------------------------------------------------
# The ranking


def _weighted_geometric_mean(
    values: dict[str, Decimal], weights: dict[str, Decimal]
) -> Decimal:
    total = sum(weights.values(), Decimal("0"))
    if total <= 0:
        raise JevPrescreenError("the gate weights must be positive")
    product = 1.0
    for key, weight in weights.items():
        product *= float(values[key]) ** (float(weight) / float(total))
    return _quantize(Decimal(str(product)))


def _weighted_arithmetic_mean(
    values: dict[str, Decimal], weights: dict[str, Decimal]
) -> Decimal:
    total = sum(weights.values(), Decimal("0"))
    if total <= 0:
        raise JevPrescreenError("the quality weights must be positive")
    combined = sum(
        (values[key] * weight for key, weight in weights.items()), Decimal("0")
    )
    return _quantize(combined / total)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def rank_probability(probabilities: dict[str, Decimal]) -> dict[str, Any]:
    """Return the ranking probability and the two factors that produced it.

    The gates are near-necessary conditions, so they combine as a weighted
    geometric mean and a near-zero gate sinks the paper. The quality terms only
    separate the papers that pass the gates, so they combine as a weighted
    arithmetic mean. The two factors combine as a weighted geometric mean, so
    the result stays in [0,1] and rises with every input.
    """
    weights = {q["key"]: Decimal(q["weight"]) for q in QUESTIONS}
    gate = _weighted_geometric_mean(
        probabilities, {key: weights[key] for key in GATE_KEYS}
    )
    quality = _weighted_arithmetic_mean(
        probabilities, {key: weights[key] for key in QUALITY_KEYS}
    )
    rank = Decimal(
        str(
            float(gate) ** float(GATE_EXPONENT)
            * float(quality) ** float(1 - GATE_EXPONENT)
        )
    )
    return {
        "gate_probability": str(gate),
        "quality_probability": str(quality),
        "gate_exponent": str(GATE_EXPONENT),
        "rank_probability": str(_quantize(rank)),
        "probabilities": {key: str(value) for key, value in probabilities.items()},
    }


# --------------------------------------------------------------------------
# The cost and the ledger


def projected_cost_usd(input_tokens: int, calls: int) -> Decimal:
    """Return the projected USD of a screen, questions included."""
    tokens = Decimal(input_tokens + calls * QUESTION_OVERHEAD_TOKENS)
    return _usd(tokens * PRICE_USD_PER_MILLION_INPUT_TOKENS / Decimal(1_000_000))


def call_cost_usd(input_tokens: int, output_tokens: int) -> Decimal:
    """Return the USD of one recorded call at the documented price."""
    value = (
        Decimal(input_tokens) * PRICE_USD_PER_MILLION_INPUT_TOKENS
        + Decimal(output_tokens) * PRICE_USD_PER_MILLION_OUTPUT_TOKENS
    ) / Decimal(1_000_000)
    return _usd(value)


def _usd(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


class CallLedger:
    """A small append-only ledger of the prescreen's own calls.

    It is not the shared paid-call broker and never talks to it. It holds one
    JSONL row per attempt, a summary object, and one USD ceiling that stops the
    screen before a call rather than after it.
    """

    def __init__(self, ledger_dir: Path, *, ceiling_usd: Decimal):
        if ceiling_usd <= 0:
            raise JevPrescreenError("the prescreen ceiling must be positive")
        self.dir = ledger_dir.resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.calls_file = self.dir / "calls.jsonl"
        self.summary_file = self.dir / "ledger.json"
        self.ceiling_usd = _usd(ceiling_usd)
        self._lock = threading.Lock()
        self._spent = Decimal("0")
        # What the calls now in flight are expected to cost. Without it, every
        # worker would measure the ceiling against a spend that none of them
        # has recorded yet, and four workers would pass a ceiling one call
        # should have stopped.
        self._reserved = Decimal("0")
        self._calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._ceiling_reached = False
        self._replay()

    def _replay(self) -> None:
        if not self.calls_file.is_file():
            self._write_summary()
            return
        lines = [
            line
            for line in self.calls_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for position, line in enumerate(lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                # A kill in the middle of an append can leave a partial last
                # line. That one is an incomplete write and is dropped, so the
                # ledger stays usable. Any earlier bad line is real corruption.
                if position == len(lines) - 1:
                    self._truncate_partial_line()
                    break
                raise JevPrescreenError(
                    f"the prescreen ledger is corrupt at line {position + 1}: {error}"
                ) from None
            if row.get("state") != "completed":
                continue
            self._calls += 1
            self._spent += Decimal(str(row.get("cost_usd", "0")))
            self._input_tokens += int(row.get("input_tokens") or 0)
            self._output_tokens += int(row.get("output_tokens") or 0)
        self._write_summary()

    def _truncate_partial_line(self) -> None:
        """Drop an incomplete trailing line so the ledger can be appended to.

        The dropped text is kept beside the ledger, because a partial row is
        still evidence that a call may have been made.
        """
        text = self.calls_file.read_text(encoding="utf-8")
        whole: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                break
            whole.append(line)
        dropped = text[len("".join(line + "\n" for line in whole)) :]
        if dropped.strip():
            atomic_write(self.dir / "calls.partial-line", dropped.encode())
        atomic_write(self.calls_file, "".join(line + "\n" for line in whole).encode())

    @property
    def spent_usd(self) -> Decimal:
        with self._lock:
            return _usd(self._spent)

    @property
    def ceiling_reached(self) -> bool:
        with self._lock:
            return self._ceiling_reached

    def reserve(self, projected_usd: Decimal) -> None:
        """Refuse before a call whose projected cost passes the ceiling.

        The check counts the calls already in flight, so concurrent workers
        cannot each pass a ceiling that only one of them fits under.
        """
        with self._lock:
            if (
                self._ceiling_reached
                or self._spent + self._reserved + projected_usd > self.ceiling_usd
            ):
                self._ceiling_reached = True
                raise JevCeilingReached(
                    f"the prescreen ceiling {self.ceiling_usd} USD stops this call"
                )
            self._reserved += projected_usd

    def release(self, projected_usd: Decimal) -> None:
        """Give back a reservation once its call is settled or abandoned."""
        with self._lock:
            self._reserved = max(Decimal("0"), self._reserved - projected_usd)

    @contextlib.contextmanager
    def reservation_held(self, projected_usd: Decimal) -> Iterator[None]:
        """Give a reservation back on every path out of the call it covers.

        `reserve` takes it, because a refusal there has to be handled by the
        caller. This gives it back whether the call succeeded, failed or
        raised, so an abandoned call never holds room against the ceiling.
        """
        try:
            yield
        finally:
            self.release(projected_usd)

    def record(self, row: dict[str, Any]) -> None:
        """Append one attempt and update the summary."""
        entry = {"schema": CALL_SCHEMA, **row}
        line = (canonical_json(entry) + "\n").encode()
        with self._lock:
            with self.calls_file.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            if entry.get("state") == "completed":
                self._calls += 1
                self._spent += Decimal(str(entry.get("cost_usd", "0")))
                self._input_tokens += int(entry.get("input_tokens") or 0)
                self._output_tokens += int(entry.get("output_tokens") or 0)
                if self._spent >= self.ceiling_usd:
                    self._ceiling_reached = True
            self._write_summary()

    def _write_summary(self) -> None:
        atomic_json(
            self.summary_file,
            {
                "schema": LEDGER_SCHEMA,
                "contract_id": CONTRACT_ID,
                "updated_at_utc": now(),
                "ceiling_usd": str(self.ceiling_usd),
                "spent_usd": str(_usd(self._spent)),
                "reserved_usd": str(_usd(self._reserved)),
                "remaining_usd": str(
                    _usd(max(Decimal("0"), self.ceiling_usd - self._spent))
                ),
                "completed_calls": self._calls,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "ceiling_reached": self._ceiling_reached,
                "price_usd_per_million_input_tokens": str(
                    PRICE_USD_PER_MILLION_INPUT_TOKENS
                ),
                "price_source": PRICE_SOURCE,
                "price_status": PRICE_STATUS,
            },
        )


# --------------------------------------------------------------------------
# The provider


class JevClient:
    """One HTTP client for `POST /v1/systemone`, with a bounded retry.

    The project carries no third-party dependency, so this is the standard
    library exactly as `providers.py` calls the other vendors. The TypeSafe SDK
    is read as documentation and never imported.
    """

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        attempts: int = DEFAULT_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise JevPrescreenError("the TypeSafe API key is empty")
        self._api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self._sleep = sleep

    def invoke(self, body: dict[str, Any]) -> dict[str, Any]:
        """Return the decoded response, after at most `attempts` tries."""
        payload = canonical_json(body).encode()
        last: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(
                self.endpoint,
                data=payload,
                method="POST",
                headers={
                    "authorization": f"Bearer {self._api_key}",
                    "content-type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as error:
                status = int(error.code)
                after = _retry_after_seconds(error)
                detail = _short_error_body(error)
                last = JevPrescreenError(f"HTTP {status} from the provider: {detail}")
                if OVERLARGE_MARKER in detail or status == 422:
                    # The provider refuses an oversized state with HTTP 400 and
                    # this marker, not with 422, which the calibration screen of
                    # 2026-09-17 measured on 27 papers. A repeat of the same
                    # request would be refused again, so the caller decides
                    # whether a smaller state helps.
                    raise JevOverlargeRequestError(str(last)) from None
                if status not in RETRY_STATUS or attempt == self.attempts:
                    raise last from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                after = None
                last = JevPrescreenError(f"the provider is unreachable: {error}")
                if attempt == self.attempts:
                    raise last from None
            self._sleep(after if after is not None else min(30.0, 2.0**attempt))
        raise last or JevPrescreenError("the provider call failed")


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    """Return the provider's own back-off, in seconds, when it names one.

    The API reference tells a caller to back off on 429 and 529 but does not
    say which header carries the delay, so both documented spellings are read
    and anything unreadable falls back to the exponential delay.
    """
    headers = getattr(error, "headers", None)
    if headers is None:
        return None
    milliseconds = headers.get("retry-after-ms")
    if milliseconds:
        try:
            return max(0.0, float(milliseconds) / 1000.0)
        except ValueError:
            return None
    seconds = headers.get("retry-after")
    if seconds:
        try:
            return max(0.0, float(seconds))
        except ValueError:
            # An HTTP-date is also legal there, and is not worth parsing.
            return None
    return None


def _short_error_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace")[:500]
    except Exception:  # pragma: no cover - the body is optional
        return error.reason if isinstance(error.reason, str) else "no body"


# --------------------------------------------------------------------------
# The screen


def _write_response(
    responses_dir: Path, candidate_key: str, record: dict[str, Any]
) -> Path:
    """Write one response, moving an older question set's answer aside first.

    A recorded response is immutable, so a paper re-screened under a new
    question set cannot overwrite its old answer. The old one is kept beside
    it, named by the question set it answered, because it is the receipt for a
    call that was really paid for.
    """
    path = response_file(responses_dir, candidate_key)
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8"))
        stale = str(previous.get("question_set_sha256") or "unknown")[:16]
        # A subdirectory, never a sibling: `build_ranking` globs `*.json` here
        # and must not read a superseded answer as a second row of the paper.
        atomic_write(
            responses_dir / "superseded" / f"{path.stem}.{stale}.json",
            path.read_bytes(),
        )
        path.unlink()
    atomic_json(path, record, immutable=True)
    return path


def _recorded_scores(
    rows: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    responses_dir: Path,
    scan_position: "itertools.count[int]",
) -> list[tuple[str, str, int]]:
    """Return the score of every paper whose response is already recorded."""
    outstanding = {row["candidate_key"] for row in pending}
    scores: list[tuple[str, str, int]] = []
    for row in rows:
        candidate_key = row["candidate_key"]
        if candidate_key in outstanding:
            continue
        path = response_file(responses_dir, candidate_key)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            probabilities = read_probabilities(
                (record.get("response") or {}).get("answers") or {}
            )
        except (OSError, ValueError, JevPrescreenError):
            continue
        scores.append(
            (
                candidate_key,
                rank_probability(probabilities)["rank_probability"],
                next(scan_position),
            )
        )
    return scores


def _answers_this_question_set(path: Path, question_hash: str) -> bool:
    """Say whether a recorded response answers the current question set."""
    if not path.is_file():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return record.get("question_set_sha256") == question_hash


LIVE_RANKING_SCHEMA = "jev-live-ranking-v1"
LIVE_RANKING_FILENAME = "live-ranking.json"
DEFAULT_LIVE_RANKING_EVERY = 25


class LiveRanking:
    """A ranking the producer can read while this scan is still running.

    The captain's rule is that a paper the scan has scored reaches the
    pipeline at once, rather than waiting for the whole corpus. So the scan
    keeps every score it has so far in memory and rewrites one small file,
    atomically, every `every` papers and at the end. The producer re-reads it
    when a worker frees up.

    The file is replaced, never appended, so a reader either sees the previous
    whole file or the next whole file.
    """

    def __init__(self, path: Path, *, every: int = DEFAULT_LIVE_RANKING_EVERY):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.every = max(1, every)
        self._rows: dict[str, dict[str, Any]] = {}
        self._since_write = 0
        self._writes = 0
        self._lock = threading.Lock()

    def add(
        self, candidate_key: str, rank_probability: str, scan_position: int
    ) -> None:
        """Record one scored paper and write when enough have arrived."""
        with self._lock:
            self._rows[candidate_key] = {
                "candidate_key": candidate_key,
                "rank_probability": rank_probability,
                "scan_position": scan_position,
            }
            self._since_write += 1
            if self._since_write >= self.every:
                self._write()

    def seed(self, rows: Iterable[tuple[str, str, int]]) -> None:
        """Take scores already on disk without writing once per row.

        A resumed scan replays its recorded responses free, so those papers
        never reach `add`. Without this they would be missing from the live
        ranking, and the producer would treat papers this scan already scored
        as unscored.
        """
        with self._lock:
            for candidate_key, probability, position in rows:
                self._rows[candidate_key] = {
                    "candidate_key": candidate_key,
                    "rank_probability": probability,
                    "scan_position": position,
                }
            self._write()

    def flush(self) -> None:
        """Write whatever has been scored, whether or not the batch is full."""
        with self._lock:
            self._write()

    @property
    def writes(self) -> int:
        with self._lock:
            return self._writes

    def _write(self) -> None:
        records = sorted(
            self._rows.values(),
            key=lambda row: (-Decimal(row["rank_probability"]), row["candidate_key"]),
        )
        atomic_json(
            self.path,
            {
                "schema": LIVE_RANKING_SCHEMA,
                "contract_id": CONTRACT_ID,
                "updated_at_utc": now(),
                "question_set_sha256": question_set_sha256(),
                "count": len(records),
                "records": records,
            },
        )
        self._since_write = 0
        self._writes += 1


def _failed_row(
    candidate_key: str, digest: str, model: str, started: str, error: str
) -> dict[str, Any]:
    return {
        "state": "failed",
        "candidate_key": candidate_key,
        "request_sha256": digest,
        "model": model,
        "started_at_utc": started,
        "finished_at_utc": now(),
        "error": error,
    }


def response_file(responses_dir: Path, candidate_key: str) -> Path:
    """Return the recorded-response path of one paper.

    A candidate key can be a DOI, so the file is named by its hash and the key
    itself is inside the record.
    """
    return responses_dir / f"{sha256_bytes(candidate_key.encode())[:32]}.json"


def run_screen(
    *,
    manifest_file: Path,
    output_dir: Path,
    client: JevClient,
    ceiling_usd: Decimal,
    model: str = DEFAULT_MODEL,
    token_budget: int = DEFAULT_STATE_TOKEN_BUDGET,
    workers: int = DEFAULT_WORKERS,
    limit: int | None = None,
    only_keys: Iterable[str] | None = None,
    live_ranking_every: int = DEFAULT_LIVE_RANKING_EVERY,
) -> dict[str, Any]:
    """Call Jev once per paper and record every response.

    Papers with no full text are skipped and counted. A paper whose response is
    already recorded is skipped free, so a stopped screen resumes. The ceiling
    stops the screen before a call, never in the middle of one.
    """
    rows = [row for row in read_manifest(manifest_file) if row["has_full_text"]]
    if only_keys is not None:
        wanted = set(only_keys)
        rows = [row for row in rows if row["candidate_key"] in wanted]
    if limit is not None:
        rows = rows[:limit]

    output_dir = output_dir.resolve()
    responses_dir = output_dir / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    ledger = CallLedger(output_dir / "ledger", ceiling_usd=ceiling_usd)
    question_hash = question_set_sha256()
    # The producer reads this while the scan runs, so a scored paper reaches
    # the pipeline without waiting for the rest of the corpus.
    live = LiveRanking(output_dir / LIVE_RANKING_FILENAME, every=live_ranking_every)
    scan_position = itertools.count(1)

    # A recorded response counts as done only when it answers the question set
    # this screen sends. A response of an older set is re-queued, because the
    # ranking would otherwise drop that paper as unreadable for ever.
    pending = [
        row
        for row in rows
        if not _answers_this_question_set(
            response_file(responses_dir, row["candidate_key"]), question_hash
        )
    ]
    # A resumed scan seeds the live ranking from what it already bought, so a
    # paper scored by an earlier pass is offered to the producer at once.
    live.seed(_recorded_scores(rows, pending, responses_dir, scan_position))
    counts = {
        "selected": len(rows),
        "already_recorded": len(rows) - len(pending),
        "completed": 0,
        "failed": 0,
        "stopped_on_ceiling": 0,
        "shrunk_after_422": 0,
        "shrunk_and_failed": 0,
        "faulted": 0,
    }
    lock = threading.Lock()

    def guarded(row: dict[str, Any]) -> None:
        """Contain every fault of one paper, as the producer does.

        `ThreadPoolExecutor.map` re-raises the first exception when its result
        is read, so an unreadable file or any other unexpected fault would end
        the whole screen. One paper must never do that: the fault is recorded
        against that paper and the screen continues.
        """
        try:
            one(row)
        except Exception as error:  # noqa: BLE001 - the containment boundary
            ledger.record(
                _failed_row(
                    str(row.get("candidate_key")),
                    "",
                    model,
                    now(),
                    f"{type(error).__name__}: {error}",
                )
            )
            with lock:
                counts["failed"] += 1
                counts["faulted"] += 1

    def one(row: dict[str, Any]) -> None:
        candidate_key = row["candidate_key"]
        if ledger.ceiling_reached:
            with lock:
                counts["stopped_on_ceiling"] += 1
            return
        text = Path(row["extraction_path"]).read_text(
            encoding="utf-8", errors="replace"
        )
        selection = select_state_text(text, token_budget=token_budget)
        body = build_request(selection["state_text"], model=model)
        digest = request_sha256(body)
        projected = projected_cost_usd(selection["estimated_tokens"], 1)
        started = now()
        try:
            ledger.reserve(projected)
        except JevCeilingReached:
            with lock:
                counts["stopped_on_ceiling"] += 1
            return
        with ledger.reservation_held(projected):
            try:
                raw = client.invoke(body)
            except JevOverlargeRequestError as refusal:
                # The provider refuses an oversized state with HTTP 400 and
                # `max_tokens_exceeded`. Its limit is not published, so the
                # only way through is to halve the budget and try again. Every
                # refused attempt leaves its own ledger row: the provider
                # refuses before generation, so the row costs nothing.
                attempt_budget = token_budget
                shrunk = False
                for _ in range(OVERLARGE_RETRIES):
                    ledger.record(
                        _failed_row(
                            candidate_key,
                            digest,
                            model,
                            started,
                            f"{refusal} (retried at a smaller budget)",
                        )
                    )
                    attempt_budget = max(250, attempt_budget // 2)
                    smaller = select_state_text(text, token_budget=attempt_budget)
                    if smaller["state_characters"] >= selection["state_characters"]:
                        break
                    selection = smaller
                    selection["shrunk_from_token_budget"] = token_budget
                    selection["shrunk_to_token_budget"] = attempt_budget
                    body = build_request(selection["state_text"], model=model)
                    digest = request_sha256(body)
                    try:
                        raw = client.invoke(body)
                    except JevOverlargeRequestError as again:
                        refusal = again
                        continue
                    except JevPrescreenError as error:
                        ledger.record(
                            _failed_row(
                                candidate_key, digest, model, started, str(error)
                            )
                        )
                        with lock:
                            counts["failed"] += 1
                            counts["shrunk_and_failed"] += 1
                        return
                    shrunk = True
                    break
                if not shrunk:
                    ledger.record(
                        _failed_row(
                            candidate_key,
                            digest,
                            model,
                            started,
                            f"{refusal} (still refused after shrinking)",
                        )
                    )
                    with lock:
                        counts["failed"] += 1
                        counts["shrunk_and_failed"] += 1
                    return
                with lock:
                    counts["shrunk_after_422"] += 1
            except JevPrescreenError as error:
                ledger.record(
                    _failed_row(candidate_key, digest, model, started, str(error))
                )
                with lock:
                    counts["failed"] += 1
                return
            usage = raw.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            # A provider that reports no input count cannot prove the charge, so
            # the projection is billed instead and the row says so.
            billed_estimate = input_tokens == 0
            if billed_estimate:
                input_tokens = selection["estimated_tokens"] + QUESTION_OVERHEAD_TOKENS
            cost = call_cost_usd(input_tokens, output_tokens)
            finished = now()
            record = {
                "schema": RESULT_SCHEMA,
                "contract_id": CONTRACT_ID,
                "candidate_key": candidate_key,
                "paper_id": row["paper_id"],
                "family_key": row["family_key"],
                "manifest_position": row["manifest_position"],
                "extraction_sha256": row["extraction_sha256"],
                "question_set_sha256": question_hash,
                "request_sha256": digest,
                "requested_model": model,
                "answered_model": raw.get("model"),
                "selection": {
                    key: value
                    for key, value in selection.items()
                    if key != "state_text"
                },
                "started_at_utc": started,
                "finished_at_utc": finished,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "billed_from_estimate": billed_estimate,
                },
                "cost_usd": str(cost),
                "response": raw,
            }
            # The money lands first. The call is already made and already
            # billed, so if the response file cannot be written the spend must
            # still be on the ledger. The other order would report a paid call
            # as a free failure and understate the spend against the ceiling.
            ledger.record(
                {
                    "state": "completed",
                    "candidate_key": candidate_key,
                    "request_sha256": digest,
                    "model": model,
                    "answered_model": raw.get("model"),
                    "started_at_utc": started,
                    "finished_at_utc": finished,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "billed_from_estimate": billed_estimate,
                    "cost_usd": str(cost),
                }
            )
            _write_response(responses_dir, candidate_key, record)
            # The score joins the live ranking the moment it exists, so the
            # producer can pick this paper up before the scan is finished.
            try:
                probabilities = read_probabilities(raw.get("answers") or {})
                live.add(
                    candidate_key,
                    rank_probability(probabilities)["rank_probability"],
                    next(scan_position),
                )
            except JevPrescreenError:
                # An answer the ranking cannot read is recorded all the same
                # and reported by `--action rank`. It simply carries no score.
                pass
            with lock:
                counts["completed"] += 1

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(guarded, pending))

    live.flush()
    receipt = {
        "schema": "jev-prescreen-screen-receipt-v1",
        "contract_id": CONTRACT_ID,
        "created_at_utc": now(),
        "manifest_file": str(manifest_file.resolve()),
        "responses_dir": str(responses_dir),
        "ledger_dir": str(ledger.dir),
        "question_set_sha256": question_hash,
        "requested_model": model,
        "state_token_budget": token_budget,
        "counts": counts,
        "spent_usd": str(ledger.spent_usd),
        "ceiling_usd": str(ledger.ceiling_usd),
        "ceiling_reached": ledger.ceiling_reached,
        "live_ranking_file": str(live.path),
        "live_ranking_writes": live.writes,
    }
    atomic_json(output_dir / "screen-receipt.json", receipt)
    return receipt


# --------------------------------------------------------------------------
# The ranking, free, from the recorded responses alone


def build_ranking(
    *, manifest_file: Path, responses_dir: Path, output_dir: Path
) -> dict[str, Any]:
    """Rank every paper that has a recorded response. No provider call."""
    rows = read_manifest(manifest_file)
    by_key = {row["candidate_key"]: row for row in rows}
    ranked: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    for path in sorted(responses_dir.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        candidate_key = str(record["candidate_key"])
        manifest_row = by_key.get(candidate_key)
        if manifest_row is None:
            unreadable.append(
                {"candidate_key": candidate_key, "reason": "not_in_manifest"}
            )
            continue
        if record.get("question_set_sha256") != question_set_sha256():
            unreadable.append(
                {"candidate_key": candidate_key, "reason": "question_set_changed"}
            )
            continue
        try:
            probabilities = read_probabilities(
                (record.get("response") or {}).get("answers") or {}
            )
        except JevPrescreenError as error:
            unreadable.append({"candidate_key": candidate_key, "reason": str(error)})
            continue
        ranked.append(
            {
                "candidate_key": candidate_key,
                "paper_id": manifest_row["paper_id"],
                "family_key": manifest_row["family_key"],
                "doi": manifest_row["doi"],
                "title": manifest_row["title"],
                "frozen_manifest_position": manifest_row["frozen_manifest_position"],
                "answered_model": record.get("answered_model"),
                "request_sha256": record.get("request_sha256"),
                "question_set_sha256": record.get("question_set_sha256"),
                "selection_rule": (record.get("selection") or {}).get("selection_rule"),
                **rank_probability(probabilities),
            }
        )

    # A stable total order: the probability first, then the pipeline's own
    # frozen position, then the key. No random seed, so the order replays.
    ranked.sort(
        key=lambda row: (
            -Decimal(row["rank_probability"]),
            row["frozen_manifest_position"]
            if row["frozen_manifest_position"]
            else 10**9,
            row["candidate_key"],
        )
    )
    for position, row in enumerate(ranked, start=1):
        row["rank"] = position

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ranking = {
        "schema": RANKING_SCHEMA,
        "contract_id": CONTRACT_ID,
        "created_at_utc": now(),
        "manifest_file": str(manifest_file.resolve()),
        "responses_dir": str(responses_dir.resolve()),
        "question_set_sha256": question_set_sha256(),
        "gate_keys": list(GATE_KEYS),
        "quality_keys": list(QUALITY_KEYS),
        "gate_exponent": str(GATE_EXPONENT),
        "weights": {q["key"]: q["weight"] for q in QUESTIONS},
        "ranking_inputs": "article_full_text_only",
        "excluded_inputs": [
            "pipeline_acceptance_labels",
            "pipeline_rejection_reason_codes",
            "trial_qa_outcomes",
            "citation_count",
            "author_prestige",
        ],
        "counts": {"ranked": len(ranked), "unreadable": len(unreadable)},
        # `jev-latest` floats, so a screen can span two concrete versions. More
        # than one entry here means the ranking mixes them, and the reviewer
        # has to decide whether that matters before the order is used.
        "answered_models": _answered_models(ranked),
        "selection_rules": _selection_rules(ranked),
        "unreadable": unreadable,
        "records": ranked,
    }
    atomic_json(output_dir / "jev-ranking.json", ranking, immutable=True)
    order_file = output_dir / "ranked-order.jsonl"
    atomic_write(
        order_file,
        jsonl_bytes(
            [
                {
                    "rank": row["rank"],
                    "candidate_key": row["candidate_key"],
                    "rank_probability": row["rank_probability"],
                }
                for row in ranked
            ]
        ),
        immutable=True,
    )
    return {
        "ranking_file": str(output_dir / "jev-ranking.json"),
        "order_file": str(order_file),
        "counts": ranking["counts"],
    }


# --------------------------------------------------------------------------
# The order the pipeline can consume


def _answered_models(ranked: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in ranked:
        name = str(row.get("answered_model") or "unreported")
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def _selection_rules(ranked: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in ranked:
        name = str(row.get("selection_rule") or "unreported")
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def write_ranked_manifest(
    *,
    frozen_manifest_file: Path,
    frozen_descriptor_file: Path,
    order_file: Path,
    output_dir: Path,
    limit: int | None = None,
) -> dict[str, Any]:
    """Write a frozen manifest in the ranked order, plus its descriptor.

    The output is exactly the shape ``full_run_plan.materialize_frozen_access_run``
    already reads, so a future launch consumes it with no change to the
    producer and the live run is untouched. Papers of the frozen manifest that
    the ranking never saw keep their own relative order after the ranked ones,
    so no paper is lost.
    """
    frozen = [
        json.loads(line)
        for line in frozen_manifest_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_key = {str(row["candidate_key"]): row for row in frozen}
    descriptor = json.loads(frozen_descriptor_file.read_text(encoding="utf-8"))
    if descriptor.get("schema") != "full-text-ready-freeze-descriptor-v1":
        raise JevPrescreenError("the frozen descriptor schema is not the expected one")
    if (descriptor.get("counts") or {}).get("manifest_records") != len(frozen):
        raise JevPrescreenError("the frozen manifest and descriptor do not match")

    order: list[str] = []
    seen: set[str] = set()
    for line in order_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        candidate_key = str(json.loads(line)["candidate_key"])
        if candidate_key in seen:
            raise JevPrescreenError(
                f"the ranked order repeats a candidate: {candidate_key}"
            )
        seen.add(candidate_key)
        if candidate_key in by_key:
            order.append(candidate_key)
    remainder = [
        str(row["candidate_key"])
        for row in frozen
        if str(row["candidate_key"]) not in seen
    ]
    ordered_keys = order + remainder
    if limit is not None:
        if limit < 1 or limit > len(ordered_keys):
            raise JevPrescreenError("the selected count is outside the frozen manifest")
        ordered_keys = ordered_keys[:limit]

    rank_of = {candidate_key: index for index, candidate_key in enumerate(order, 1)}
    rows: list[dict[str, Any]] = []
    for position, candidate_key in enumerate(ordered_keys, start=1):
        row = dict(by_key[candidate_key])
        row["original_manifest_position"] = row["manifest_position"]
        row["manifest_position"] = position
        row["jev_prescreen_rank"] = rank_of.get(candidate_key)
        rows.append(row)

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = output_dir / f"jev-ranked-manifest-{len(rows)}.jsonl"
    atomic_write(manifest_file, jsonl_bytes(rows), immutable=True)
    derived = {
        "schema": "full-text-ready-freeze-descriptor-v1",
        "state": "frozen_offline",
        "freeze_id": f"{descriptor['freeze_id']}-jev-prescreen-{len(rows)}-r1",
        "counts": {
            "manifest_records": len(rows),
            # Counted exactly as `materialize_frozen_access_run` counts it, so
            # the descriptor and its only consumer can never disagree.
            "unique_paper_families": len(
                {
                    str(
                        row.get("paper_family_id")
                        or row.get("family_key")
                        or row["candidate_key"]
                    )
                    for row in rows
                }
            ),
        },
        "source_freeze_id": descriptor["freeze_id"],
        "order_file": str(order_file.resolve()),
        "order_file_sha256": sha256_bytes(order_file.read_bytes()),
        "ranked_from_jev": len(order),
        "carried_unranked": len(rows) - min(len(order), len(rows)),
        "contract_id": CONTRACT_ID,
    }
    descriptor_file = output_dir / f"jev-ranked-manifest-{len(rows)}-descriptor.json"
    atomic_json(descriptor_file, derived, immutable=True)
    return {
        "ranked_manifest": str(manifest_file),
        "ranked_descriptor": str(descriptor_file),
        "counts": derived["counts"],
        "ranked_from_jev": len(order),
    }


# --------------------------------------------------------------------------
# The calibration


ACCEPTED_LABEL = "accepted"
REJECTED_LABEL = "rejected"

# `paper_completions.outcome_class` as the chapter 3 producer writes it. A
# label says only what this one run did with the paper. It measures the
# ranking and never builds it.
ACCEPTED_OUTCOMES = ("generation_accepted",)
REJECTED_OUTCOMES = (
    "generation_rejected",
    "eligibility_excluded",
    "eligibility_unresolved",
)
# A paper the run never finished judging carries no label at all.
UNLABELLED_OUTCOMES = ("incomplete_non_mcq", "paper_cost_cap_reached")


def build_labels(
    *, database_file: Path, run_id: str, output_dir: Path
) -> dict[str, Any]:
    """Write the calibration labels of one finished run, read-only.

    The database is opened through a read-only URI and no statement writes, so
    this runs beside a live producer. A paper the run did not finish judging is
    counted and left unlabelled rather than called a rejection.
    """

    from . import db as _db

    connection = _db.connect_read_only(database_file)
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT candidate_key, outcome_class, reason_code "
            "FROM paper_completions WHERE run_id = ? ORDER BY candidate_key",
            (run_id,),
        ).fetchall()
    finally:
        connection.close()

    labels: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate_key, outcome_class, reason_code in rows:
        if outcome_class in ACCEPTED_OUTCOMES:
            label = ACCEPTED_LABEL
        elif outcome_class in REJECTED_OUTCOMES:
            label = REJECTED_LABEL
        else:
            skipped.append(
                {"candidate_key": candidate_key, "outcome_class": outcome_class}
            )
            continue
        labels.append(
            {
                "candidate_key": str(candidate_key),
                "label": label,
                "outcome_class": outcome_class,
                "reason_code": reason_code,
            }
        )

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    labels_file = output_dir / f"labels-{run_id}.jsonl"
    atomic_write(labels_file, jsonl_bytes(labels))
    keys_file = output_dir / f"labelled-keys-{run_id}.txt"
    atomic_write(
        keys_file,
        "".join(f"{row['candidate_key']}\n" for row in labels).encode(),
    )
    receipt = {
        "schema": "jev-prescreen-labels-v1",
        "contract_id": CONTRACT_ID,
        "created_at_utc": now(),
        "database_file": str(database_file.resolve()),
        "run_id": run_id,
        "labels_file": str(labels_file),
        "keys_file": str(keys_file),
        "counts": {
            "papers": len(rows),
            "accepted": sum(1 for row in labels if row["label"] == ACCEPTED_LABEL),
            "rejected": sum(1 for row in labels if row["label"] == REJECTED_LABEL),
            "unlabelled": len(skipped),
        },
        "unlabelled": skipped,
    }
    atomic_json(output_dir / f"labels-{run_id}-receipt.json", receipt)
    return receipt


def roc_auc(positive: Sequence[Decimal], negative: Sequence[Decimal]) -> Decimal | None:
    """Return the rank AUC of the scores, with ties counted as half."""
    if not positive or not negative:
        return None
    wins = Decimal("0")
    for value in positive:
        for other in negative:
            if value > other:
                wins += 1
            elif value == other:
                wins += Decimal("0.5")
    return _quantize(wins / Decimal(len(positive) * len(negative)))


def calibrate(
    *, ranking_file: Path, labels_file: Path, output_dir: Path
) -> dict[str, Any]:
    """Measure how well the ranking separates this run's own outcomes.

    ``labels_file`` is a JSONL of ``{"candidate_key": ..., "label": ...}`` with
    the label ``accepted`` for a paper whose family the pipeline accepted, and
    ``rejected`` for a paper it opened and did not accept. The labels measure
    the ranking and never build it.
    """
    ranking = json.loads(ranking_file.read_text(encoding="utf-8"))
    scores = {
        str(row["candidate_key"]): Decimal(row["rank_probability"])
        for row in ranking["records"]
    }
    labels: dict[str, str] = {}
    for line in labels_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        label = str(row["label"])
        if label not in (ACCEPTED_LABEL, REJECTED_LABEL):
            raise JevPrescreenError(f"unknown calibration label: {label}")
        labels[str(row["candidate_key"])] = label

    paired = [
        (key, scores[key], label)
        for key, label in sorted(labels.items())
        if key in scores
    ]
    positive = [score for _, score, label in paired if label == ACCEPTED_LABEL]
    negative = [score for _, score, label in paired if label == REJECTED_LABEL]
    ordered = sorted(paired, key=lambda item: (-item[1], item[0]))
    decile = max(1, len(ordered) // 10) if ordered else 0
    top = ordered[:decile]
    rest = ordered[decile:]

    report = {
        "schema": CALIBRATION_SCHEMA,
        "contract_id": CONTRACT_ID,
        "created_at_utc": now(),
        "ranking_file": str(ranking_file.resolve()),
        "labels_file": str(labels_file.resolve()),
        "counts": {
            "labelled": len(labels),
            "scored_and_labelled": len(paired),
            "labelled_without_score": len(labels) - len(paired),
            "accepted": len(positive),
            "rejected": len(negative),
        },
        "auc": str(roc_auc(positive, negative)) if positive and negative else None,
        "top_decile": {
            "size": len(top),
            "accepted": sum(1 for _, _, label in top if label == ACCEPTED_LABEL),
            "acceptance_rate": _rate(top),
        },
        "rest": {
            "size": len(rest),
            "accepted": sum(1 for _, _, label in rest if label == ACCEPTED_LABEL),
            "acceptance_rate": _rate(rest),
        },
        "base_rate": _rate(ordered),
    }
    lift = None
    rest_rate = report["rest"]["acceptance_rate"]
    if rest_rate is not None and Decimal(rest_rate) > 0:
        lift = str(
            _quantize(
                Decimal(report["top_decile"]["acceptance_rate"]) / Decimal(rest_rate)
            )
        )
    report["top_decile_lift_over_rest"] = lift
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(output_dir / "jev-calibration.json", report)
    return report


def _rate(rows: Sequence[tuple[str, Decimal, str]]) -> str | None:
    if not rows:
        return None
    accepted = sum(1 for _, _, label in rows if label == ACCEPTED_LABEL)
    return str(_quantize(Decimal(accepted) / Decimal(len(rows))))


# --------------------------------------------------------------------------
# The command line


ACTIONS = (
    "build-manifest",
    "labels",
    "estimate",
    "screen",
    "rank",
    "write-order",
    "calibrate",
    "questions",
)


def add_parser(commands: argparse._SubParsersAction) -> None:
    """Register ``jev-prescreen`` on the project command line."""
    parser = commands.add_parser(
        "jev-prescreen",
        help="Rank retained papers by the chance the pipeline can build a question.",
    )
    parser.add_argument("--action", choices=ACTIONS, required=True)
    parser.add_argument("--dispositions-file", type=Path)
    parser.add_argument("--freeze-manifest", type=Path)
    parser.add_argument("--freeze-descriptor", type=Path)
    parser.add_argument("--manifest-file", type=Path)
    parser.add_argument("--responses-dir", type=Path)
    parser.add_argument("--ranking-file", type=Path)
    parser.add_argument("--order-file", type=Path)
    parser.add_argument("--labels-file", type=Path)
    parser.add_argument("--database-file", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ceiling-usd", default="5.00")
    parser.add_argument(
        "--state-token-budget", type=int, default=DEFAULT_STATE_TOKEN_BUDGET
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--live-ranking-every", type=int, default=DEFAULT_LIVE_RANKING_EVERY
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--only-keys-file",
        type=Path,
        help="A file of candidate keys, one per line, to bound a sample screen.",
    )


def _read_keys(path: Path) -> set[str]:
    """Return the candidate keys of a bounding key file, one per line."""
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _require(value: Any, name: str) -> Any:
    if value is None:
        raise JevPrescreenError(f"{name} is required for this action")
    return value


def handle(args: argparse.Namespace) -> Any:
    """Run one ``jev-prescreen`` action and return its receipt."""
    if args.action == "questions":
        return {
            "contract_id": CONTRACT_ID,
            "question_set_sha256": question_set_sha256(),
            "questions": QUESTIONS,
            "payload": question_payload(),
        }
    if args.action == "build-manifest":
        return build_manifest(
            dispositions_file=_require(args.dispositions_file, "--dispositions-file"),
            freeze_manifest_file=_require(args.freeze_manifest, "--freeze-manifest"),
            output_dir=_require(args.output_dir, "--output-dir"),
        )
    if args.action == "labels":
        return build_labels(
            database_file=_require(args.database_file, "--database-file"),
            run_id=_require(args.run_id, "--run-id"),
            output_dir=_require(args.output_dir, "--output-dir"),
        )
    if args.action == "estimate":
        rows = [
            row
            for row in read_manifest(_require(args.manifest_file, "--manifest-file"))
            if row["has_full_text"]
        ]
        if args.only_keys_file is not None:
            wanted = _read_keys(args.only_keys_file)
            rows = [row for row in rows if row["candidate_key"] in wanted]
        if args.limit is not None:
            rows = rows[: args.limit]
        tokens = sum(
            min(row["estimated_tokens"], args.state_token_budget) for row in rows
        )
        return {
            "papers": len(rows),
            "state_token_budget": args.state_token_budget,
            "estimated_input_tokens": tokens,
            "question_overhead_tokens": QUESTION_OVERHEAD_TOKENS * len(rows),
            "projected_cost_usd": str(projected_cost_usd(tokens, len(rows))),
            "price_usd_per_million_input_tokens": str(
                PRICE_USD_PER_MILLION_INPUT_TOKENS
            ),
            "price_source": PRICE_SOURCE,
            "price_status": PRICE_STATUS,
        }
    if args.action == "screen":
        only_keys = (
            _read_keys(args.only_keys_file) if args.only_keys_file is not None else None
        )
        client = JevClient(
            read_api_key(args.key_file),
            timeout=args.timeout,
            attempts=args.attempts,
        )
        return run_screen(
            manifest_file=_require(args.manifest_file, "--manifest-file"),
            output_dir=_require(args.output_dir, "--output-dir"),
            client=client,
            ceiling_usd=Decimal(str(args.ceiling_usd)),
            model=args.model,
            token_budget=args.state_token_budget,
            workers=args.workers,
            limit=args.limit,
            only_keys=only_keys,
            live_ranking_every=args.live_ranking_every,
        )
    if args.action == "rank":
        return build_ranking(
            manifest_file=_require(args.manifest_file, "--manifest-file"),
            responses_dir=_require(args.responses_dir, "--responses-dir"),
            output_dir=_require(args.output_dir, "--output-dir"),
        )
    if args.action == "write-order":
        return write_ranked_manifest(
            frozen_manifest_file=_require(args.freeze_manifest, "--freeze-manifest"),
            frozen_descriptor_file=_require(
                args.freeze_descriptor, "--freeze-descriptor"
            ),
            order_file=_require(args.order_file, "--order-file"),
            output_dir=_require(args.output_dir, "--output-dir"),
            limit=args.limit,
        )
    if args.action == "calibrate":
        return calibrate(
            ranking_file=_require(args.ranking_file, "--ranking-file"),
            labels_file=_require(args.labels_file, "--labels-file"),
            output_dir=_require(args.output_dir, "--output-dir"),
        )
    raise JevPrescreenError(f"unknown action: {args.action}")
