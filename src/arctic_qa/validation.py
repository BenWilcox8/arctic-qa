from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .db import Database, now
from .extraction import load_chunks
from .util import canonical_json, normalize_text, stable_id


UNIT_FACTORS: dict[tuple[str, str], Decimal] = {
    ("m", "cm"): Decimal("100"),
    ("cm", "m"): Decimal("0.01"),
    ("km", "m"): Decimal("1000"),
    ("m", "km"): Decimal("0.001"),
    ("kg", "g"): Decimal("1000"),
    ("g", "kg"): Decimal("0.001"),
}

DIRECTION_PAIRS = {
    ("increased", "decreased"),
    ("higher", "lower"),
    ("positive", "negative"),
    ("earlier", "later"),
    ("north", "south"),
    ("greater", "less"),
}

REQUIRED_ITEM_KEYS = {
    "schema_version",
    "item_id",
    "source",
    "question",
    "answer",
    "provenance",
}
REQUIRED_ANSWER_KEYS = {
    "text",
    "evidence_quote",
    "locator",
    "scope",
    "required_question_phrases",
}


@dataclass
class ValidationResult:
    item_id: str
    final_label: str
    labels: dict[str, bool]
    reasons: list[str]
    distractors: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "final_label": self.final_label,
            "labels": self.labels,
            "reasons": self.reasons,
            "distractors": self.distractors,
        }


def validate_candidate(
    db: Database, namespace, candidate: dict[str, Any], *, strict_release: bool = True
) -> ValidationResult:
    reasons: list[str] = []
    labels = {
        "schema_valid": False,
        "evidence_located": False,
        "scope_complete": False,
        "source_entailment_model_verified": False,
        "reconstruction_agreement": False,
        "deterministic_contradiction": False,
        "alternative_answer_search_passed": False,
        "machine_accepted_unverified": False,
        "rejected": False,
        "unresolved": False,
    }
    if REQUIRED_ITEM_KEYS - candidate.keys() or not isinstance(
        candidate.get("answer"), dict
    ):
        reasons.append("schema_invalid")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    if REQUIRED_ANSWER_KEYS - candidate["answer"].keys():
        reasons.append("answer_schema_invalid")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    labels["schema_valid"] = True
    try:
        chunks = {
            row["chunk_id"]: row
            for row in load_chunks(db, namespace, candidate["source"]["source_id"])
        }
    except Exception:
        reasons.append("source_chunks_missing")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    if not evidence_resolves(candidate["answer"], chunks):
        reasons.append("answer_evidence_not_located")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    labels["evidence_located"] = True
    missing_scope = [
        phrase
        for phrase in candidate["answer"].get("required_question_phrases", [])
        if normalize_text(str(phrase)) not in normalize_text(candidate["question"])
    ]
    if missing_scope:
        reasons.append("scope_qualifier_missing")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    labels["scope_complete"] = True
    if (
        candidate["answer"].get("claim_type")
        in {
            "observation",
            "association",
        }
        and candidate.get("question_claim_type") == "causal"
    ):
        reasons.append("causal_overclaim")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    verification = candidate.get("verification") or {}
    labels["source_entailment_model_verified"] = bool(
        verification.get("source_entailment_model_verified")
    )
    if not labels["source_entailment_model_verified"]:
        reasons.append("source_entailment_not_verified")
        labels["unresolved"] = True
        return _finish(db, candidate, labels, reasons, [], "unresolved")
    reconstruction = candidate.get("reconstruction") or {}
    if reconstruction.get("ambiguity_label") != "one_answer":
        reasons.append("answer_ambiguous")
        labels["unresolved"] = True
        return _finish(db, candidate, labels, reasons, [], "unresolved")
    if reconstruction_matches(candidate["answer"], reconstruction):
        labels["reconstruction_agreement"] = True
    else:
        reasons.append("reconstruction_disagreement")
        labels["unresolved"] = True
        return _finish(db, candidate, labels, reasons, [], "unresolved")
    if not verification.get("alternative_answer_search_passed"):
        reasons.append("alternative_answer_unresolved")
        labels["unresolved"] = True
        return _finish(db, candidate, labels, reasons, [], "unresolved")
    labels["alternative_answer_search_passed"] = True
    distractor_results = [
        validate_distractor(candidate, distractor, chunks)
        for distractor in candidate.get("distractors", [])
    ]
    accepted = [result for result in distractor_results if result["accepted"]]
    labels["deterministic_contradiction"] = bool(accepted) and all(
        result["deterministic"] for result in accepted
    )
    task_type = candidate.get("task_type", "short_answer")
    required = (
        0
        if task_type == "short_answer"
        else 4
        if task_type == "answer_absent_mcq"
        else 3
    )
    if required and len(accepted) < required:
        reasons.append("insufficient_verified_distractors")
        return _finish(db, candidate, labels, reasons, distractor_results, "rejected")
    if (
        strict_release
        and required
        and not all(result["deterministic"] for result in accepted[:required])
    ):
        reasons.append("model_only_distractor_verification")
        return _finish(db, candidate, labels, reasons, distractor_results, "rejected")
    labels["machine_accepted_unverified"] = True
    return _finish(
        db,
        candidate,
        labels,
        reasons,
        distractor_results,
        "machine_accepted_unverified",
    )


def evidence_resolves(
    record: dict[str, Any], chunks: dict[str, dict[str, Any]]
) -> bool:
    locator = record.get("locator") or {}
    chunk = chunks.get(locator.get("chunk_id"))
    quote = record.get("evidence_quote")
    if not chunk or not isinstance(quote, str) or not quote:
        return False
    try:
        start = int(locator["start_offset"])
        end = int(locator["end_offset"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= start < end <= len(chunk["text"]) and chunk["text"][start:end] == quote


def reconstruction_matches(
    answer: dict[str, Any], reconstruction: dict[str, Any]
) -> bool:
    proposed = [answer.get("text", ""), *answer.get("variants", [])]
    rebuilt = reconstruction.get("answer", "")
    if any(
        normalize_text(str(value)) == normalize_text(str(rebuilt)) for value in proposed
    ):
        return True
    numeric = answer.get("numeric_rule")
    rebuilt_numeric = reconstruction.get("numeric")
    return bool(numeric and rebuilt_numeric and numeric_equal(numeric, rebuilt_numeric))


def numeric_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        left_value = Decimal(str(left["canonical_value"]))
        right_value = convert(
            Decimal(str(right["canonical_value"])),
            str(right["unit"]),
            str(left["unit"]),
        )
        tolerance = Decimal(str(left["tolerance"]))
    except (KeyError, InvalidOperation, ValueError):
        return False
    return abs(left_value - right_value) <= tolerance


def convert(value: Decimal, source_unit: str, target_unit: str) -> Decimal:
    source = source_unit.casefold()
    target = target_unit.casefold()
    if source == target:
        return value
    factor = UNIT_FACTORS.get((source, target))
    if factor is not None:
        return value * factor
    if source in {"c", "°c"} and target == "k":
        return value + Decimal("273.15")
    if source == "k" and target in {"c", "°c"}:
        return value - Decimal("273.15")
    raise ValueError(f"unknown unit conversion: {source_unit} to {target_unit}")


def validate_distractor(
    candidate: dict[str, Any],
    distractor: dict[str, Any],
    chunks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result = {
        "text": distractor.get("text"),
        "type": distractor.get("type"),
        "accepted": False,
        "deterministic": False,
        "label": "rejected",
        "reasons": [],
        "evidence_quote": distractor.get("evidence_quote"),
        "locator": distractor.get("locator"),
    }
    answer = candidate["answer"]
    answers = [answer.get("text", ""), *answer.get("variants", [])]
    if any(
        normalize_text(str(distractor.get("text", ""))) == normalize_text(str(value))
        for value in answers
    ):
        result["reasons"].append("distractor_matches_answer")
        return result
    normalized_option = normalize_text(str(distractor.get("text", "")))
    if normalized_option in {"all of the above", "none of the above"}:
        result["reasons"].append("forbidden_meta_option")
        return result
    numeric = distractor.get("numeric")
    if (
        numeric
        and answer.get("numeric_rule")
        and numeric_equal(answer["numeric_rule"], numeric)
    ):
        result["reasons"].append("distractor_is_equivalent_numeric_answer")
        return result
    verification = distractor.get("verification") or {}
    if not verification.get("model_verified"):
        result["reasons"].append("distractor_not_model_verified")
        return result
    if not verification.get("alternative_answer_search_passed"):
        result["reasons"].append("distractor_alternative_answer_possible")
        return result
    if distractor.get("true_under_other_scope"):
        result["reasons"].append("distractor_true_under_other_scope")
        return result
    if not evidence_resolves(distractor, chunks):
        result["reasons"].append("distractor_evidence_not_located")
        return result
    deterministic = distractor.get("deterministic") or {}
    kind = deterministic.get("kind")
    passed = False
    if kind == "numeric_outside_tolerance":
        if _numeric_rule_is_source_bound(answer):
            passed = _numeric_incompatible(answer.get("numeric_rule"), numeric)
        else:
            result["reasons"].append("source_bound_numeric_rule_missing")
    elif kind in {
        "unique_categorical",
        "directional_contradiction",
        "scope_excluded",
        "unique_entity",
    }:
        passed = _source_bound_typed_incompatibility(answer, distractor)
        if not passed:
            result["reasons"].append("source_bound_predicate_missing")
    if passed:
        result.update(
            {
                "accepted": True,
                "deterministic": True,
                "label": "deterministic-contradiction",
            }
        )
    else:
        result.update(
            {"accepted": True, "deterministic": False, "label": "model-verified"}
        )
        result["reasons"].append("residual_model_error_possible")
    return result


def _source_bound_typed_incompatibility(
    answer: dict[str, Any], distractor: dict[str, Any]
) -> bool:
    rule = answer.get("deterministic_rule")
    proposed_rule = distractor.get("deterministic") or {}
    if not isinstance(rule, dict) or not isinstance(proposed_rule, dict):
        return False
    source_text = normalize_text(str(answer.get("evidence_quote", "")))
    answer_text = normalize_text(str(answer.get("text", "")))
    option_text = normalize_text(str(distractor.get("text", "")))
    kind = proposed_rule.get("kind")
    proposed = normalize_text(str(proposed_rule.get("candidate_value", "")))
    if kind == "directional_contradiction":
        if rule.get("kind") != "directional_relation":
            return False
        correct = normalize_text(str(rule.get("source_value", "")))
        proposed = normalize_text(str(proposed_rule.get("candidate_relation", "")))
        if not correct or not proposed:
            return False
        if correct not in answer_text or correct not in source_text:
            return False
        option_relations = {
            value
            for pair in DIRECTION_PAIRS
            for value in pair
            if value in option_text.split()
        }
        return (
            len(option_relations) == 1
            and proposed in option_relations
            and ((correct, proposed) in DIRECTION_PAIRS)
        )
    if kind not in {"unique_categorical", "scope_excluded", "unique_entity"}:
        return False
    required_rule_kind = "closed_scope" if kind == "scope_excluded" else "closed_set"
    if rule.get("kind") != required_rule_kind:
        return False
    allowed = {
        normalize_text(str(value))
        for value in rule.get("source_values", [])
        if normalize_text(str(value))
    }
    closure_terms = {"only", "sole", "solely", "exclusively"}
    return bool(
        allowed
        and answer_text in allowed
        and all(value in source_text for value in allowed)
        and closure_terms.intersection(source_text.split())
        and proposed
        and proposed == option_text
        and proposed not in allowed
    )


def _numeric_incompatible(
    answer_rule: dict[str, Any] | None, numeric: dict[str, Any] | None
) -> bool:
    if not answer_rule or not numeric:
        return False
    try:
        answer = Decimal(str(answer_rule["canonical_value"]))
        candidate = convert(
            Decimal(str(numeric["canonical_value"])),
            str(numeric["unit"]),
            str(answer_rule["unit"]),
        )
        tolerance = Decimal(str(answer_rule["tolerance"]))
        if tolerance < 0 or not answer_rule.get("tolerance_basis"):
            return False
    except (KeyError, InvalidOperation, ValueError):
        return False
    return abs(answer - candidate) > tolerance


def _numeric_rule_is_source_bound(answer: dict[str, Any]) -> bool:
    rule = answer.get("numeric_rule")
    if not isinstance(rule, dict):
        return False
    try:
        answer_value = Decimal(str(rule["canonical_value"]))
        tolerance = Decimal(str(rule["tolerance"]))
        unit = str(rule["unit"])
        tolerance_basis = normalize_text(str(rule["tolerance_basis"]))
    except (KeyError, InvalidOperation, ValueError):
        return False
    evidence = str(answer.get("evidence_quote", ""))
    displayed = str(answer.get("text", ""))
    return bool(
        tolerance >= 0
        and tolerance_basis
        and tolerance_basis in normalize_text(evidence)
        and _contains_quantity(displayed, answer_value, unit)
        and _contains_quantity(evidence, answer_value, unit)
        and _contains_quantity(evidence, tolerance, unit)
    )


def _contains_quantity(text: str, expected: Decimal, expected_unit: str) -> bool:
    for value, unit in re.findall(
        r"(?<![\w.])([-+]?\d+(?:\.\d+)?)\s*(°?[A-Za-z]+|%)(?!\w)", text
    ):
        try:
            if convert(Decimal(value), unit, expected_unit) == expected:
                return True
        except (InvalidOperation, ValueError):
            continue
    return False


def _finish(
    db: Database,
    candidate: dict[str, Any],
    labels: dict[str, bool],
    reasons: list[str],
    distractors: list[dict[str, Any]],
    final_label: str,
) -> ValidationResult:
    item_id = candidate.get("item_id", stable_id("invalid", canonical_json(candidate)))
    labels["rejected"] = final_label == "rejected"
    labels["unresolved"] = final_label == "unresolved"
    event_id = stable_id(
        "validation", item_id, final_label, labels, reasons, distractors
    )
    with db.transaction():
        db.connection.execute(
            """INSERT OR IGNORE INTO validation_events
            (event_id,item_id,stage,label,reason_codes_json,details_json,created_at)
            VALUES (?,?,'automated_acceptance',?,?,?,?)""",
            (
                event_id,
                item_id,
                final_label,
                canonical_json(reasons),
                canonical_json({"labels": labels, "distractors": distractors}),
                now(),
            ),
        )
        if db.one("SELECT item_id FROM candidates WHERE item_id=?", (item_id,)):
            db.connection.execute(
                "UPDATE candidates SET status=?,updated_at=? WHERE item_id=?",
                (final_label, now(), item_id),
            )
        for reason in reasons:
            rejection_id = stable_id("rejection", item_id, final_label, reason)
            db.connection.execute(
                """INSERT OR IGNORE INTO rejection_ledger
                (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
                VALUES (?,?,?,'automated_acceptance',?,?,?)""",
                (
                    rejection_id,
                    item_id,
                    candidate.get("source", {}).get("source_id"),
                    reason,
                    canonical_json({}),
                    now(),
                ),
            )
    return ValidationResult(item_id, final_label, labels, reasons, distractors)
