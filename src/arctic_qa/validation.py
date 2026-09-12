from __future__ import annotations

import json
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
    "finding_id",
    "source",
    "question",
    "answer",
    "reconstruction",
    "answer_verification",
    "option_verdicts",
    "provenance",
}
REQUIRED_ANSWER_KEYS = {
    "text",
    "evidence_quote",
    "locator",
    "scope",
    "required_question_phrases",
}
OPTION_VERDICT_RESPONSE_KEYS = frozenset(
    {
        "contradiction_established",
        "alternative_answer_search_passed",
        "true_in_different_context",
        "question_admits_option_as_correct",
        "evidence_quote",
        "locator",
        "rationale",
    }
)


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
    db: Database,
    namespace,
    candidate: dict[str, Any],
    *,
    strict_release: bool = True,
    persist: bool = True,
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
        "model_verified": False,
        "mcq_eligible": False,
        "machine_accepted_unverified": False,
        "rejected": False,
        "unresolved": False,
        "_persist_validation": persist,
    }
    if candidate.get("schema_version") != "2.0.0":
        reasons.append("unsafe_legacy_candidate_schema")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    if REQUIRED_ITEM_KEYS - candidate.keys() or not isinstance(
        candidate.get("answer"), dict
    ):
        reasons.append("schema_invalid")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    if REQUIRED_ANSWER_KEYS - candidate["answer"].keys():
        reasons.append("answer_schema_invalid")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    labels["schema_valid"] = True
    source_record = db.one(
        "SELECT source_id,paper_family_id,content_hash FROM sources WHERE source_id=?",
        (candidate.get("source", {}).get("source_id"),),
    )
    if not source_record or any(
        candidate["source"].get(key) != source_record[key]
        for key in ("source_id", "paper_family_id", "content_hash")
    ):
        reasons.append("source_manifest_mismatch")
        return _finish(db, candidate, labels, reasons, [], "rejected")
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
    if candidate["answer"].get("numeric_rule") and not numeric_rule_is_source_bound(
        candidate["answer"]
    ):
        reasons.append("source_bound_numeric_rule_missing")
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
    reconstruction = candidate.get("reconstruction") or {}
    if not evidence_resolves(reconstruction, chunks):
        reasons.append("reconstruction_evidence_not_located")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    verification = candidate.get("answer_verification") or {}
    if not evidence_resolves(verification, chunks):
        reasons.append("answer_verifier_evidence_not_located")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    if reconstruction.get("scope") != candidate["answer"].get("scope"):
        reasons.append("reconstruction_scope_mismatch")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    if verification.get("scope") != candidate["answer"].get("scope"):
        reasons.append("answer_verifier_scope_mismatch")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    if reconstruction.get("question_claim_type") != verification.get(
        "question_claim_type"
    ):
        reasons.append("question_claim_type_disagreement")
        return _finish(db, candidate, labels, reasons, [], "unresolved")
    if (
        candidate["answer"].get("claim_type")
        in {
            "observation",
            "association",
        }
        and verification.get("question_claim_type") == "causal"
    ):
        reasons.append("causal_overclaim")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    labels["source_entailment_model_verified"] = bool(
        verification.get("source_entailment_model_verified")
    )
    if not labels["source_entailment_model_verified"]:
        reasons.append("source_entailment_not_verified")
        labels["unresolved"] = True
        return _finish(db, candidate, labels, reasons, [], "unresolved")
    if not verification.get("relation_scope_match"):
        reasons.append("relation_scope_mismatch")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    if not verification.get("ambiguity_resolved"):
        reasons.append("answer_ambiguous")
        labels["unresolved"] = True
        return _finish(db, candidate, labels, reasons, [], "unresolved")
    if reconstruction.get("ambiguity_label") != "one_answer":
        reasons.append("answer_ambiguous")
        labels["unresolved"] = True
        return _finish(db, candidate, labels, reasons, [], "unresolved")
    if reconstruction.get("alternatives"):
        reasons.append("reconstruction_alternative_answer_present")
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
    if not _qa_verification_receipts_match(db, candidate):
        reasons.append("qa_verification_call_receipt_missing")
        return _finish(db, candidate, labels, reasons, [], "rejected")
    qa_hash = stable_id(
        "qa", candidate["question"], canonical_json(candidate["answer"])
    )
    verdicts = candidate.get("option_verdicts") or []
    distractor_results = []
    normalized_options = [
        normalize_text(str(row.get("text", "")))
        for row in candidate.get("distractors", [])
    ]
    for distractor in candidate.get("distractors", []):
        option_hash = stable_id(
            "option", qa_hash, distractor.get("text"), distractor.get("type")
        )
        verdict = next(
            (row for row in verdicts if row.get("option_hash") == option_hash), None
        )
        distractor_results.append(
            validate_distractor(
                db,
                candidate,
                distractor,
                chunks,
                verdict,
                qa_hash,
                option_hash,
                normalized_options.count(
                    normalize_text(str(distractor.get("text", "")))
                )
                > 1,
            )
        )
    accepted = [result for result in distractor_results if result["accepted"]]
    labels["deterministic_contradiction"] = bool(accepted) and all(
        result["deterministic"] for result in accepted
    )
    labels["model_verified"] = bool(accepted) and all(
        result["model_verified"] for result in accepted
    )
    if len(accepted) < 3:
        reasons.append("insufficient_verified_distractors")
    elif strict_release and not all(result["deterministic"] for result in accepted[:3]):
        reasons.append("model_only_distractor_verification")
    else:
        labels["mcq_eligible"] = True
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
    db: Database,
    candidate: dict[str, Any],
    distractor: dict[str, Any],
    chunks: dict[str, dict[str, Any]],
    verdict: dict[str, Any] | None,
    qa_hash: str,
    option_hash: str,
    duplicate_text: bool,
) -> dict[str, Any]:
    result = {
        "text": distractor.get("text"),
        "type": distractor.get("type"),
        "accepted": False,
        "deterministic": False,
        "model_verified": False,
        "label": "rejected",
        "reasons": [],
        "evidence_quote": verdict.get("evidence_quote") if verdict else None,
        "locator": verdict.get("locator") if verdict else None,
    }
    answer = candidate["answer"]
    if duplicate_text:
        result["reasons"].append("duplicate_distractor_text")
        return result
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
    if not numeric:
        display_issue = _text_display_issue(str(distractor.get("text", "")))
        if display_issue:
            result["reasons"].append(display_issue)
            return result
    if numeric:
        numeric_display_issue = _numeric_display_issue(
            str(distractor.get("text", "")), numeric
        )
        if numeric_display_issue:
            result["reasons"].append(numeric_display_issue)
            return result
    if (
        numeric
        and answer.get("numeric_rule")
        and numeric_equal(answer["numeric_rule"], numeric)
    ):
        result["reasons"].append("distractor_is_equivalent_numeric_answer")
        return result
    if not verdict:
        result["reasons"].append("option_verdict_missing_or_stale")
        return result
    if verdict.get("source_hash") != candidate["source"].get("content_hash"):
        result["reasons"].append("option_verdict_source_hash_mismatch")
        return result
    if verdict.get("qa_hash") != qa_hash:
        result["reasons"].append("option_verdict_qa_hash_mismatch")
        return result
    if verdict.get("option_hash") != option_hash:
        result["reasons"].append("option_verdict_hash_mismatch")
        return result
    if verdict.get("option_text") != distractor.get("text"):
        result["reasons"].append("option_verdict_text_mismatch")
        return result
    provenance = verdict.get("provenance") or {}
    if (
        provenance.get("role") != "option_verifier"
        or not provenance.get("provider")
        or not provenance.get("requested_model")
        or not provenance.get("prompt_version")
        or not provenance.get("prompt_hash")
    ):
        result["reasons"].append("option_verdict_provenance_missing")
        return result
    if not _option_verdict_receipt_matches(
        db, candidate, verdict, option_hash, provenance
    ):
        result["reasons"].append("option_verdict_call_receipt_missing")
        return result
    if not verdict.get("contradiction_established"):
        result["reasons"].append("option_contradiction_unresolved")
        return result
    if not verdict.get("alternative_answer_search_passed"):
        result["reasons"].append("distractor_alternative_answer_possible")
        return result
    if verdict.get("question_admits_option_as_correct"):
        result["reasons"].append("option_correct_under_question_interpretation")
        return result
    if not evidence_resolves(verdict, chunks):
        result["reasons"].append("distractor_evidence_not_located")
        return result
    result["model_verified"] = True
    deterministic = distractor.get("deterministic") or {}
    kind = deterministic.get("kind")
    passed = False
    if kind == "numeric_outside_tolerance":
        if numeric_rule_is_source_bound(answer):
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


def _option_verdict_receipt_matches(
    db: Database,
    candidate: dict[str, Any],
    verdict: dict[str, Any],
    option_hash: str,
    provenance: dict[str, Any],
) -> bool:
    candidate_provenance = candidate.get("provenance") or {}
    run_id = candidate_provenance.get("run_id")
    arm = candidate_provenance.get("generation_arm")
    finding_id = candidate.get("finding_id")
    if not all(isinstance(value, str) and value for value in (run_id, arm, finding_id)):
        return False
    entity_id = stable_id(
        "option-verdict", stable_id("unit", finding_id, arm), option_hash
    )
    receipt = db.one(
        """SELECT * FROM calls
        WHERE run_id=? AND entity_id=? AND role='option_verifier'
          AND provider=? AND requested_model=? AND prompt_version=?
          AND prompt_hash=? AND status='completed'
        ORDER BY attempt DESC LIMIT 1""",
        (
            run_id,
            entity_id,
            provenance.get("provider"),
            provenance.get("requested_model"),
            provenance.get("prompt_version"),
            provenance.get("prompt_hash"),
        ),
    )
    if not receipt or not receipt.get("response_json"):
        return False
    if provenance.get("returned_model") != receipt.get("returned_model"):
        return False
    if provenance.get("request_id") != receipt.get("request_id"):
        return False
    try:
        response = json.loads(receipt["response_json"])
    except (TypeError, json.JSONDecodeError):
        return False
    if not _option_response_schema_valid(response):
        return False
    recorded_response = {key: verdict.get(key) for key in OPTION_VERDICT_RESPONSE_KEYS}
    return canonical_json(response) == canonical_json(recorded_response)


def _option_response_schema_valid(response: Any) -> bool:
    if not isinstance(response, dict) or set(response) != OPTION_VERDICT_RESPONSE_KEYS:
        return False
    boolean_fields = (
        "contradiction_established",
        "alternative_answer_search_passed",
        "true_in_different_context",
        "question_admits_option_as_correct",
    )
    if not all(type(response[field]) is bool for field in boolean_fields):
        return False
    if not all(
        isinstance(response[field], str) and response[field]
        for field in ("evidence_quote", "rationale")
    ):
        return False
    locator = response["locator"]
    return bool(
        isinstance(locator, dict)
        and set(locator) == {"chunk_id", "start_offset", "end_offset"}
        and isinstance(locator["chunk_id"], str)
        and locator["chunk_id"]
        and type(locator["start_offset"]) is int
        and type(locator["end_offset"]) is int
    )


def _qa_verification_receipts_match(db: Database, candidate: dict[str, Any]) -> bool:
    provenance = candidate.get("provenance") or {}
    run_id = provenance.get("run_id")
    arm = provenance.get("generation_arm")
    finding_id = candidate.get("finding_id")
    calls = provenance.get("verification_calls") or {}
    if not all(isinstance(value, str) and value for value in (run_id, arm, finding_id)):
        return False
    entity_id = stable_id("unit", finding_id, arm)
    records = {
        "reconstructor": candidate.get("reconstruction"),
        "answer_verifier": candidate.get("answer_verification"),
    }
    for role, record in records.items():
        call = calls.get(role) or {}
        if call.get("role") != role:
            return False
        receipt = db.one(
            """SELECT * FROM calls
            WHERE run_id=? AND entity_id=? AND role=? AND provider=?
              AND requested_model=? AND prompt_version=? AND prompt_hash=?
              AND status='completed'
            ORDER BY attempt DESC LIMIT 1""",
            (
                run_id,
                entity_id,
                role,
                call.get("provider"),
                call.get("requested_model"),
                call.get("prompt_version"),
                call.get("prompt_hash"),
            ),
        )
        if not receipt or not receipt.get("response_json"):
            return False
        if call.get("returned_model") != receipt.get("returned_model"):
            return False
        if call.get("request_id") != receipt.get("request_id"):
            return False
        try:
            response = json.loads(receipt["response_json"])
        except (TypeError, json.JSONDecodeError):
            return False
        if canonical_json(response) != canonical_json(record):
            return False
    return True


def _text_display_issue(text: str) -> str | None:
    normalized = normalize_text(text)
    if re.search(r"\b(?:not|no|never|without|except|unless|neither|nor)\b", normalized):
        return "displayed_assertion_negated"
    if ";" in text or re.search(
        r"\b(?:or|either|and|but|although|though|while|whereas|if)\b", normalized
    ):
        return "displayed_assertion_compound"
    return None


def _numeric_display_issue(text: str, numeric: dict[str, Any]) -> str | None:
    try:
        value = Decimal(str(numeric["canonical_value"]))
        unit = str(numeric["unit"])
    except (KeyError, InvalidOperation, ValueError):
        return "numeric_metadata_display_mismatch"
    quantities = re.findall(
        r"(?<![\w.])([-+]?\d+(?:\.\d+)?)\s*(°?[A-Za-z]+|%)(?!\w)", text
    )
    if (
        len(quantities) != 1
        or ";" in text
        or re.search(
            r"\b(?:not|no|never|without|except|unless|neither|nor|or|either|and|but|although|though|while|whereas|if)\b",
            normalize_text(text),
        )
    ):
        return "numeric_display_ambiguous"
    displayed_value, displayed_unit = quantities[0]
    try:
        if convert(Decimal(displayed_value), displayed_unit, unit) != value:
            return "numeric_metadata_display_mismatch"
    except (InvalidOperation, ValueError):
        return "numeric_metadata_display_mismatch"
    return None


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


def numeric_rule_is_source_bound(answer: dict[str, Any]) -> bool:
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
    persist = labels.pop("_persist_validation", True)
    labels["rejected"] = final_label == "rejected"
    labels["unresolved"] = final_label == "unresolved"
    if not persist:
        return ValidationResult(item_id, final_label, labels, reasons, distractors)
    candidate_json = canonical_json(candidate)
    candidate_hash = stable_id("candidate-payload", candidate_json)
    stored = db.one("SELECT candidate_json FROM candidates WHERE item_id=?", (item_id,))
    if not stored or stored["candidate_json"] != candidate_json:
        labels["rejected"] = True
        labels["machine_accepted_unverified"] = False
        labels["mcq_eligible"] = False
        final_label = "rejected"
        if "stored_candidate_payload_mismatch" not in reasons:
            reasons.append("stored_candidate_payload_mismatch")
    event_id = stable_id(
        "validation", item_id, candidate_hash, final_label, labels, reasons, distractors
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
                canonical_json(
                    {
                        "candidate_hash": candidate_hash,
                        "labels": labels,
                        "distractors": distractors,
                    }
                ),
                now(),
            ),
        )
        if stored and stored["candidate_json"] == candidate_json:
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
        for distractor in distractors:
            if distractor.get("accepted"):
                continue
            for reason in distractor.get("reasons", []):
                rejection_id = stable_id(
                    "rejection",
                    item_id,
                    "option_validation",
                    distractor.get("text"),
                    reason,
                )
                db.connection.execute(
                    """INSERT OR IGNORE INTO rejection_ledger
                    (rejection_id,item_id,source_id,stage,reason_code,detail_json,created_at)
                    VALUES (?,?,?,'option_validation',?,?,?)""",
                    (
                        rejection_id,
                        item_id,
                        candidate.get("source", {}).get("source_id"),
                        reason,
                        canonical_json(
                            {
                                "option_text": distractor.get("text"),
                                "option_type": distractor.get("type"),
                            }
                        ),
                        now(),
                    ),
                )
    return ValidationResult(item_id, final_label, labels, reasons, distractors)
