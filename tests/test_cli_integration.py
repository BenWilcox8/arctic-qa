from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"
sys.path.insert(0, str(REPO / "src"))

from arctic_qa.util import canonical_json, sha256_bytes, stable_id  # noqa: E402


def cli(
    root: Path, *arguments: str, expected: int = 0
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "arctic_qa",
            "--data-root",
            str(root),
            "--test-mode",
            "--json",
            *arguments,
        ],
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected, result.stderr or result.stdout
    return result


def smoke(root: Path, run_id: str = "test-smoke") -> dict:
    result = cli(root, "smoke", "--fixture-dir", str(FIXTURES), "--run-id", run_id)
    return json.loads(result.stdout)


def database(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / "arctic-qa" / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    return connection


def candidate(root: Path) -> dict:
    with database(root) as connection:
        row = connection.execute(
            "SELECT candidate_json FROM candidates LIMIT 1"
        ).fetchone()
    return json.loads(row[0])


def write_candidate(tmp_path: Path, payload: dict, name: str) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def bind_option_verdicts(
    item: dict, quote: str, locator: dict, *, receipt_root: Path | None = None
) -> None:
    qa_hash = stable_id("qa", item["question"], canonical_json(item["answer"]))
    item["option_verdicts"] = []
    for option in item["distractors"]:
        option_hash = stable_id("option", qa_hash, option["text"], option["type"])
        payload = {
            "contradiction_established": True,
            "alternative_answer_search_passed": True,
            "true_in_different_context": False,
            "question_admits_option_as_correct": False,
            "evidence_quote": quote,
            "locator": locator,
            "rationale": "Test-only source-bound contradiction.",
        }
        prompt_hash = stable_id("test-prompt", option_hash)
        request_id = stable_id("test-request", option_hash)
        item["option_verdicts"].append(
            {
                "source_hash": item["source"]["content_hash"],
                "qa_hash": qa_hash,
                "option_hash": option_hash,
                "option_text": option["text"],
                **payload,
                "provenance": {
                    "role": "option_verifier",
                    "provider": "fake",
                    "requested_model": "fake-verifier",
                    "returned_model": "fake-verifier",
                    "request_id": request_id,
                    "prompt_version": "test-only",
                    "prompt_hash": prompt_hash,
                },
            }
        )
        if receipt_root is not None:
            run_id = item["provenance"]["run_id"]
            entity_id = stable_id(
                "option-verdict",
                stable_id(
                    "unit", item["finding_id"], item["provenance"]["generation_arm"]
                ),
                option_hash,
            )
            with database(receipt_root) as connection:
                connection.execute(
                    """INSERT INTO calls
                    (call_id,run_id,entity_id,role,provider,requested_model,
                     returned_model,prompt_version,prompt_hash,parameters_json,
                     request_id,attempt,status,response_json,started_at,completed_at)
                    VALUES (?,?,?,'option_verifier','fake','fake-verifier',
                            'fake-verifier','test-only',?, '{}',?,1,'completed',?,
                            'test-only','test-only')""",
                    (
                        stable_id("test-call", run_id, entity_id),
                        run_id,
                        entity_id,
                        prompt_hash,
                        request_id,
                        canonical_json(payload),
                    ),
                )


def source_locator_for_quote(root: Path, source_id: str, quote: str) -> dict:
    with database(root) as connection:
        relative = connection.execute(
            "SELECT relative_path FROM artifacts WHERE source_id=? AND kind='chunks'",
            (source_id,),
        ).fetchone()[0]
    for line in (
        (root / "arctic-qa" / relative).read_text(encoding="utf-8").splitlines()
    ):
        chunk = json.loads(line)
        start = chunk["text"].find(quote)
        if start >= 0:
            return {
                "chunk_id": chunk["chunk_id"],
                "start_offset": start,
                "end_offset": start + len(quote),
            }
    raise AssertionError(f"quote not found in extracted chunks: {quote}")


def bind_source_span(record: dict, quote: str, locator: dict) -> None:
    contract = "finding-evidence-span-v2"
    text_sha256 = sha256_bytes(quote.encode("utf-8"))
    record.update(
        {
            "evidence_quote": quote,
            "locator": locator,
            "evidence_text_sha256": text_sha256,
            "span_contract_version": contract,
            "source_span_id": stable_id(
                contract,
                locator["chunk_id"],
                locator["start_offset"],
                locator["end_offset"],
                text_sha256,
            ),
        }
    )


def sync_option_receipt(root: Path, item: dict, index: int) -> None:
    verdict = item["option_verdicts"][index]
    payload = {
        key: verdict[key]
        for key in (
            "contradiction_established",
            "alternative_answer_search_passed",
            "true_in_different_context",
            "question_admits_option_as_correct",
            "evidence_quote",
            "locator",
            "rationale",
        )
    }
    provenance = verdict["provenance"]
    with database(root) as connection:
        cursor = connection.execute(
            """UPDATE calls SET response_json=?
            WHERE run_id=? AND role='option_verifier' AND provider=?
              AND requested_model=? AND prompt_hash=? AND status='completed'""",
            (
                canonical_json(payload),
                item["provenance"]["run_id"],
                provenance["provider"],
                provenance["requested_model"],
                provenance["prompt_hash"],
            ),
        )
        assert cursor.rowcount == 1


def bind_qa_verification_receipts(root: Path, item: dict) -> None:
    run_id = item["provenance"]["run_id"]
    entity_id = stable_id(
        "unit", item["finding_id"], item["provenance"]["generation_arm"]
    )
    calls = {}
    for role, record in (
        ("reconstructor", item["reconstruction"]),
        ("answer_verifier", item["answer_verification"]),
    ):
        prompt_hash = stable_id("test-qa-prompt", entity_id, role, record)
        request_id = stable_id("test-qa-request", entity_id, role)
        calls[role] = {
            "role": role,
            "provider": "fake",
            "requested_model": "fake-verifier",
            "returned_model": "fake-verifier",
            "request_id": request_id,
            "prompt_version": "test-only",
            "prompt_hash": prompt_hash,
        }
        with database(root) as connection:
            connection.execute(
                """INSERT INTO calls
                (call_id,run_id,entity_id,role,provider,requested_model,
                 returned_model,prompt_version,prompt_hash,parameters_json,
                 request_id,attempt,status,response_json,started_at,completed_at)
                VALUES (?,?,?,?,'fake','fake-verifier','fake-verifier',
                        'test-only',?,'{}',?,1,'completed',?,'test-only','test-only')""",
                (
                    stable_id("test-qa-call", run_id, entity_id, role),
                    run_id,
                    entity_id,
                    role,
                    prompt_hash,
                    request_id,
                    canonical_json(record),
                ),
            )
    item["provenance"]["verification_calls"] = calls


def author_script(tmp_path: Path, prefix: dict) -> Path:
    lines = [
        json.dumps(prefix),
        *FIXTURES.joinpath("fake-author.jsonl")
        .read_text(encoding="utf-8")
        .splitlines(),
    ]
    path = tmp_path / f"author-{prefix['kind']}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def generate_command(
    source_id: str,
    run_id: str,
    author: Path,
    *,
    retries: int = 1,
    budget: str = "100000",
    reservation: str = "100",
) -> tuple[str, ...]:
    return (
        "generate",
        "--source-id",
        source_id,
        "--run-id",
        run_id,
        "--author-provider",
        "fake",
        "--author-model",
        "claude-opus-5",
        "--author-script",
        str(author),
        "--verifier-provider",
        "fake",
        "--verifier-model",
        "gemini-3.1-pro-preview",
        "--verifier-script",
        str(FIXTURES / "fake-verifier.jsonl"),
        "--budget-limit",
        budget,
        "--reservation",
        reservation,
        "--retries",
        str(retries),
    )


def test_absent_mount_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    result = cli(missing, "doctor", expected=2)
    assert "DATA_ROOT_UNAVAILABLE" in result.stderr
    assert not missing.exists()


def test_discovery_replay_pages_and_deduplicates(tmp_path: Path) -> None:
    replay = tmp_path / "discovery.json"
    record = {
        "doi": "10.1234/example",
        "title": "One result",
        "authors": [{"name": "A Researcher"}],
        "published_date": "2024-01-01",
    }
    replay.write_text(
        json.dumps(
            [
                {
                    "adapter": "replay",
                    "query": "q",
                    "page_number": 1,
                    "records": [record],
                },
                {
                    "adapter": "replay",
                    "query": "q",
                    "page_number": 2,
                    "records": [record],
                },
            ]
        ),
        encoding="utf-8",
    )
    result = json.loads(
        cli(tmp_path, "discover", "--adapter", "replay", "--input", str(replay)).stdout
    )
    assert result["added"] == 1
    assert result["duplicates"] == 1
    assert result["pages"] == 2
    assert result["manifest"]["record_count"] == 1


def test_geography_requires_valid_source_bound_complete_site_evidence(
    tmp_path: Path,
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    source_id = item["source"]["source_id"]
    unrelated_quote = (
        "The reported water depth was 2.0 m with a source-grounded tolerance of 0.1 m."
    )
    unrelated = {
        "evidence_kind": "site_coordinates",
        "latitudes": [71.3],
        "named_regions": [],
        "source_content_hash": item["source"]["content_hash"],
        "evidence_quote": unrelated_quote,
        "locator": source_locator_for_quote(tmp_path, source_id, unrelated_quote),
        "site_coverage": "complete",
    }
    geography_quote = "The complete study site was at 71.3 N."
    bound = {
        **unrelated,
        "evidence_quote": geography_quote,
        "locator": source_locator_for_quote(tmp_path, source_id, geography_quote),
    }

    naked_path = write_candidate(
        tmp_path,
        {"evidence_kind": "site_coordinates", "latitudes": [71.3]},
        "naked-geography.json",
    )
    naked = json.loads(
        cli(
            tmp_path, "screen", "--source-id", source_id, "--evidence", str(naked_path)
        ).stdout
    )
    assert naked["eligibility_state"] == "pending"
    assert naked["reason"] == "source_bound_geography_evidence_required"

    unrelated_path = write_candidate(tmp_path, unrelated, "unrelated-geography.json")
    unrelated_result = json.loads(
        cli(
            tmp_path,
            "screen",
            "--source-id",
            source_id,
            "--evidence",
            str(unrelated_path),
        ).stdout
    )
    assert unrelated_result["eligibility_state"] == "pending"
    assert unrelated_result["reason"] == "geographic_values_not_in_evidence"

    invalid_path = write_candidate(
        tmp_path, {**bound, "latitudes": [91.0]}, "invalid-geography.json"
    )
    invalid = json.loads(
        cli(
            tmp_path,
            "screen",
            "--source-id",
            source_id,
            "--evidence",
            str(invalid_path),
        ).stdout
    )
    assert invalid["eligibility_state"] == "pending"
    assert invalid["reason"] == "invalid_coordinates"

    mixed_path = write_candidate(
        tmp_path, {**bound, "latitudes": [71.0, -10.0]}, "mixed-geography.json"
    )
    mixed = json.loads(
        cli(
            tmp_path, "screen", "--source-id", source_id, "--evidence", str(mixed_path)
        ).stdout
    )
    assert mixed["eligibility_state"] == "excluded"
    assert mixed["geography_state"] == "mixed"

    valid_path = write_candidate(tmp_path, bound, "valid-geography.json")
    valid = json.loads(
        cli(
            tmp_path, "screen", "--source-id", source_id, "--evidence", str(valid_path)
        ).stdout
    )
    assert valid["eligibility_state"] == "eligible"
    assert valid["geography_state"] == "core_arctic"


@pytest.mark.parametrize(
    ("mutation", "reason", "label"),
    [
        ("wrong_quote", "answer_evidence_span_invalid", "rejected"),
        ("qualifier_loss", "scope_qualifier_missing", "rejected"),
        ("ambiguous", "answer_ambiguous", "unresolved"),
        ("causal_overclaim", "causal_overclaim", "rejected"),
    ],
)
def test_validation_rejects_answer_failures(
    tmp_path: Path, mutation: str, reason: str, label: str
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    if mutation == "wrong_quote":
        item["answer"]["evidence_quote"] = (
            "A fabricated quote that is not in the source."
        )
    elif mutation == "qualifier_loss":
        item["question"] = "What water depth was reported?"
    elif mutation == "ambiguous":
        item["reconstruction"]["ambiguity_label"] = "multiple_answers"
    else:
        item["question_claim_type"] = "causal"
        item["reconstruction"]["question_claim_type"] = "causal"
        item["answer_verification"]["question_claim_type"] = "causal"
    path = write_candidate(tmp_path, item, f"{mutation}.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == label
    assert reason in result["reasons"]


def test_validation_requires_positive_source_entailment(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["answer_verification"]["source_entailment_model_verified"] = False
    path = write_candidate(tmp_path, item, "failed-entailment.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "unresolved"
    assert "source_entailment_not_verified" in result["reasons"]

    item["answer_verification"]["source_entailment_model_verified"] = True
    path = write_candidate(tmp_path, item, "passed-entailment.json")
    control = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert control["final_label"] == "machine_accepted_unverified"


def test_true_distractors_and_equivalent_units_are_not_false(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["distractors"][0]["text"] = "2 m"
    item["distractors"][0]["numeric"] = {"canonical_value": "200", "unit": "cm"}
    item["option_verdicts"][1]["question_admits_option_as_correct"] = True
    sync_option_receipt(tmp_path, item, 1)
    item["distractors"][2]["text"] = "2.0 m"
    item["distractors"][3]["text"] = "None of the above"
    path = write_candidate(tmp_path, item, "true-distractors.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "machine_accepted_unverified"
    assert result["labels"]["mcq_eligible"] is False
    reasons = [reason for row in result["distractors"] for reason in row["reasons"]]
    assert "distractor_is_equivalent_numeric_answer" in reasons
    assert "option_correct_under_question_interpretation" in reasons
    assert "distractor_matches_answer" in reasons
    assert "forbidden_meta_option" in reasons


def test_numeric_rule_must_match_source_and_displayed_answer(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["answer"]["numeric_rule"]["canonical_value"] = "999"
    bind_option_verdicts(
        item,
        item["answer"]["evidence_quote"],
        item["answer"]["locator"],
    )
    path = write_candidate(tmp_path, item, "unbound-numeric-rule.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "rejected"
    assert result["labels"]["mcq_eligible"] is False
    assert "source_bound_numeric_rule_missing" in result["reasons"]

    control = candidate(tmp_path)
    control_path = write_candidate(tmp_path, control, "bound-numeric-rule.json")
    accepted = json.loads(
        cli(tmp_path, "validate", "--candidate", str(control_path)).stdout
    )
    assert accepted["final_label"] == "machine_accepted_unverified"


@pytest.mark.parametrize(
    ("field", "invented_value"),
    [
        ("reported_precision", "invented precision"),
        ("rounding_rule", "invented rounding rule"),
        ("conversion_rule", "invented conversion rule"),
    ],
)
def test_numeric_metadata_must_be_source_bound_before_export(
    tmp_path: Path, field: str, invented_value: str
) -> None:
    run_id = f"numeric-metadata-{field}"
    receipt = smoke(tmp_path, run_id)
    item = candidate(tmp_path)
    item["answer"]["numeric_rule"][field] = invented_value
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE candidates SET candidate_json=? WHERE item_id=?",
            (canonical_json(item), receipt["item_id"]),
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    exported = json.loads(cli(tmp_path, "export", "--run-id", run_id).stdout)

    assert result["final_label"] == "rejected"
    assert result["reasons"] == ["source_bound_numeric_rule_missing"]
    assert exported["short_answer_count"] == 0
    assert exported["mcq_count"] == 0


def test_all_null_scope_is_rejected(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    empty_scope = {
        "geography": None,
        "population": None,
        "period": None,
        "method": None,
        "comparison": None,
        "uncertainty": None,
    }
    item["answer"]["scope"] = empty_scope
    item["answer"]["required_question_phrases"] = []
    item["reconstruction"]["scope"] = empty_scope
    item["answer_verification"]["scope"] = empty_scope
    path = write_candidate(tmp_path, item, "all-null-scope.json")

    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)

    assert result["final_label"] == "rejected"
    assert result["reasons"] == ["answer_scope_not_source_bound"]


def test_scope_alias_not_present_in_source_is_rejected(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    unsupported_scope = {
        **item["answer"]["scope"],
        "method": "laboratory experiment",
    }
    item["answer"]["scope"] = unsupported_scope
    item["reconstruction"]["scope"] = unsupported_scope
    item["answer_verification"]["scope"] = unsupported_scope
    path = write_candidate(tmp_path, item, "unsupported-scope-alias.json")

    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)

    assert result["final_label"] == "rejected"
    assert result["reasons"] == ["answer_scope_not_source_bound"]


@pytest.mark.parametrize(
    ("role", "expected_reason"),
    [
        ("answer", "answer_scope_not_source_bound"),
        ("reconstruction", "reconstruction_scope_not_source_bound"),
        ("answer_verification", "answer_verifier_scope_not_source_bound"),
    ],
)
def test_scope_must_be_bound_to_each_role_selected_evidence_before_export(
    tmp_path: Path,
    role: str,
    expected_reason: str,
) -> None:
    run_id = "selected-evidence-scope"
    receipt = smoke(tmp_path, run_id)
    item = candidate(tmp_path)
    item[role]["scope"]["geography"] = "71.4 N"
    bind_qa_verification_receipts(tmp_path, item)
    bind_option_verdicts(
        item,
        item["answer"]["evidence_quote"],
        item["answer"]["locator"],
        receipt_root=tmp_path,
    )
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE candidates SET candidate_json=? WHERE item_id=?",
            (canonical_json(item), receipt["item_id"]),
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    exported = json.loads(cli(tmp_path, "export", "--run-id", run_id).stdout)

    assert result["final_label"] == "rejected"
    assert result["reasons"] == [expected_reason]
    assert exported["short_answer_count"] == 0
    assert exported["mcq_count"] == 0


def test_stored_qa_gate_failure_preserves_all_generation_reasons(
    tmp_path: Path,
) -> None:
    receipt = smoke(tmp_path)
    item = candidate(tmp_path)
    item["answer"]["scope"]["method"] = "laboratory experiment"
    item["status"] = "qa_gate_failed"
    item["qa_gate_reasons"] = [
        "answer_scope_not_source_bound",
        "reconstruction_scope_mismatch",
    ]
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE candidates SET candidate_json=?,status=? WHERE item_id=?",
            (canonical_json(item), "qa_gate_failed", receipt["item_id"]),
        )
    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )

    assert result["final_label"] == "rejected"
    assert result["reasons"] == item["qa_gate_reasons"]
    exported = json.loads(cli(tmp_path, "export", "--run-id", "test-smoke").stdout)
    rejection_path = tmp_path / "arctic-qa" / exported["files"]["rejections"]
    rejection_rows = [
        json.loads(line) for line in rejection_path.read_text().splitlines()
    ]
    assert exported["rejection_count"] == 2
    assert {row["reason_code"] for row in rejection_rows} == set(
        item["qa_gate_reasons"]
    )


def test_empty_required_question_phrases_are_rejected(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["answer"]["required_question_phrases"] = []
    path = write_candidate(tmp_path, item, "empty-question-phrases.json")

    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)

    assert result["final_label"] == "rejected"
    assert result["reasons"] == ["scope_qualifier_missing"]


def test_question_qualifier_must_be_bound_to_selected_evidence_before_export(
    tmp_path: Path,
) -> None:
    run_id = "source-bound-question-qualifier"
    receipt = smoke(tmp_path, run_id)
    item = candidate(tmp_path)
    item["answer"]["required_question_phrases"] = ["invented qualifier"]
    item["question"] = f"{item['question']} Invented qualifier."
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE candidates SET candidate_json=? WHERE item_id=?",
            (canonical_json(item), receipt["item_id"]),
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    exported = json.loads(cli(tmp_path, "export", "--run-id", run_id).stdout)

    assert result["final_label"] == "rejected"
    assert result["reasons"] == ["scope_qualifier_not_source_bound"]
    assert exported["short_answer_count"] == 0
    assert exported["mcq_count"] == 0


@pytest.mark.parametrize(
    "field",
    [
        "prompt_version",
        "numeric_rule_contract_version",
        "scope_contract_version",
    ],
)
def test_generation_contract_versions_must_match_before_export(
    tmp_path: Path, field: str
) -> None:
    run_id = f"generation-contract-{field}"
    receipt = smoke(tmp_path, run_id)
    item = candidate(tmp_path)
    item["provenance"][field] = "tampered-contract-version"
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE candidates SET candidate_json=? WHERE item_id=?",
            (canonical_json(item), receipt["item_id"]),
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    exported = json.loads(cli(tmp_path, "export", "--run-id", run_id).stdout)

    assert result["final_label"] == "rejected"
    assert result["reasons"] == ["generation_contract_version_mismatch"]
    assert exported["short_answer_count"] == 0
    assert exported["mcq_count"] == 0


def test_self_asserted_typed_distractor_rules_are_not_deterministic(
    tmp_path: Path,
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    counterexamples = [
        {
            "text": "invented category",
            "type": "categorical",
            "deterministic": {
                "kind": "unique_categorical",
                "allowed_values": ["2.0 m"],
                "correct_value": "2.0 m",
                "candidate_value": "invented category",
            },
        },
        {
            "text": "decreased",
            "type": "directional",
            "deterministic": {
                "kind": "directional_contradiction",
                "correct_relation": "increased",
                "candidate_relation": "decreased",
            },
        },
        {
            "text": "invented excluded scope",
            "type": "scope",
            "deterministic": {
                "kind": "scope_excluded",
                "candidate_value": "invented excluded scope",
                "excluded_values": ["invented excluded scope"],
            },
        },
    ]
    for index, values in enumerate(counterexamples):
        distractor = item["distractors"][index]
        distractor.pop("numeric", None)
        distractor.update(values)
    path = write_candidate(tmp_path, item, "self-asserted-rules.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "machine_accepted_unverified"
    assert result["labels"]["mcq_eligible"] is False
    for row in result["distractors"][:3]:
        assert row["deterministic"] is False
        assert "option_verdict_missing_or_stale" in row["reasons"]

    control = candidate(tmp_path)
    control_path = write_candidate(tmp_path, control, "numeric-control.json")
    accepted = json.loads(
        cli(tmp_path, "validate", "--candidate", str(control_path)).stdout
    )
    assert accepted["final_label"] == "machine_accepted_unverified"


@pytest.mark.parametrize(
    (
        "kind",
        "quote",
        "answer_text",
        "answer_rule",
        "options",
        "expected_deterministic",
    ),
    [
        (
            "unique_categorical",
            "The only reported substrate category was gravel.",
            "gravel",
            {"kind": "closed_set", "source_values": ["gravel"]},
            ["sand", "silt", "clay"],
            True,
        ),
        (
            "unique_categorical",
            "The only reported substrate category was gravel.",
            "gravel",
            {"kind": "closed_set", "source_values": ["gravel"]},
            [
                "The substrate was not sand.",
                "The substrate was not silt.",
                "The substrate was not clay.",
            ],
            False,
        ),
        (
            "directional_contradiction",
            "The reported trend increased.",
            "increased",
            {"kind": "directional_relation", "source_value": "increased"},
            ["decreased", "the response decreased", "a decreased response"],
            True,
        ),
        (
            "scope_excluded",
            "The only included region was the central basin.",
            "central basin",
            {"kind": "closed_scope", "source_values": ["central basin"]},
            ["outer basin", "southern shelf", "river delta"],
            True,
        ),
    ],
)
def test_source_bound_typed_distractor_controls(
    tmp_path: Path,
    kind: str,
    quote: str,
    answer_text: str,
    answer_rule: dict,
    options: list[str],
    expected_deterministic: bool,
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    locator = source_locator_for_quote(tmp_path, item["source"]["source_id"], quote)
    scope_phrase = {
        "unique_categorical": "substrate category",
        "directional_contradiction": "reported trend",
        "scope_excluded": "included region",
    }[kind]
    source_bound_scope = {
        "geography": None,
        "population": None,
        "period": None,
        "method": scope_phrase,
        "comparison": None,
        "uncertainty": None,
    }
    item["answer"].update(
        {
            "text": answer_text,
            "variants": [],
            "scope": source_bound_scope,
            "required_question_phrases": [scope_phrase],
            "deterministic_rule": answer_rule,
        }
    )
    bind_source_span(item["answer"], quote, locator)
    item["answer"].pop("numeric_rule", None)
    item["question"] = f"What {scope_phrase} was documented?"
    item["reconstruction"] = {
        "answer": answer_text,
        "evidence_quote": quote,
        "locator": locator,
        "scope": item["answer"]["scope"],
        "question_claim_type": item["answer"]["claim_type"],
        "ambiguity_label": "one_answer",
        "alternatives": [],
    }
    item["answer_verification"].update(
        {
            "evidence_quote": quote,
            "locator": locator,
            "scope": item["answer"]["scope"],
            "question_claim_type": item["answer"]["claim_type"],
        }
    )
    bind_qa_verification_receipts(tmp_path, item)
    item["distractors"] = []
    for option in options:
        predicate = {"kind": kind}
        if kind == "directional_contradiction":
            predicate["candidate_relation"] = "decreased"
        else:
            predicate["candidate_value"] = option
        distractor = {
            "text": option,
            "type": kind,
            "deterministic": predicate,
        }
        bind_source_span(distractor, quote, locator)
        item["distractors"].append(distractor)
    bind_option_verdicts(item, quote, locator, receipt_root=tmp_path)
    path = write_candidate(tmp_path, item, f"valid-{kind}.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "machine_accepted_unverified"
    assert all(row["deterministic"] for row in result["distractors"]) is (
        expected_deterministic
    )
    if not expected_deterministic:
        assert result["labels"]["mcq_eligible"] is False
        assert all(
            "displayed_assertion_negated" in row["reasons"]
            for row in result["distractors"]
        )


def test_malformed_json_and_429_retry_are_recorded(tmp_path: Path) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    malformed = author_script(tmp_path, {"role": "extractor", "kind": "malformed"})
    result = cli(tmp_path, *generate_command(source_id, "malformed-run", malformed))
    assert json.loads(result.stdout)["item_id"]
    rate_limited = author_script(tmp_path, {"role": "extractor", "kind": "429"})
    result = cli(tmp_path, *generate_command(source_id, "rate-run", rate_limited))
    assert json.loads(result.stdout)["item_id"]
    with database(tmp_path) as connection:
        codes = {
            row[0]
            for row in connection.execute(
                "SELECT error_code FROM calls WHERE error_code IS NOT NULL"
            )
        }
    assert {"MALFORMED_RESPONSE", "HTTP_429"} <= codes


def test_nested_provider_contract_is_checked_before_completion(
    tmp_path: Path,
) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    script = tmp_path / "invalid-nested.jsonl"
    valid_tail = (
        (FIXTURES / "fake-author.jsonl").read_text(encoding="utf-8").splitlines()[1:]
    )
    script.write_text(
        "\n".join(
            [
                json.dumps(
                    {"role": "extractor", "response": {"answer": "not-an-object"}}
                ),
                *valid_tail,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = cli(
        tmp_path,
        *generate_command(source_id, "nested-schema-run", script, retries=0),
        expected=2,
    )
    error = json.loads(result.stderr)
    assert error["code"] == "PROVIDER_ERROR"
    with database(tmp_path) as connection:
        row = connection.execute(
            "SELECT status,error_code,response_json FROM calls WHERE run_id='nested-schema-run'"
        ).fetchone()
    assert row["status"] == "failed"
    assert row["error_code"] == "MALFORMED_RESPONSE"
    assert row["response_json"] is None

    control = cli(
        tmp_path,
        *generate_command(
            source_id, "nested-schema-control", FIXTURES / "fake-author.jsonl"
        ),
    )
    assert json.loads(control.stdout)["item_id"]


def test_timeout_creates_ambiguous_receipt_and_resume_gate(tmp_path: Path) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    timeout = author_script(tmp_path, {"role": "extractor", "kind": "timeout"})
    result = cli(
        tmp_path, *generate_command(source_id, "timeout-run", timeout), expected=2
    )
    assert "AMBIGUOUS_CHARGE" in result.stderr
    resumed = json.loads(cli(tmp_path, "resume", "--run-id", "timeout-run").stdout)
    assert resumed["manual_reconciliation_required"] is True
    assert resumed["incomplete_calls"][0]["status"] == "ambiguous_charge"


def test_budget_exhaustion_stops_before_provider_call(tmp_path: Path) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    result = cli(
        tmp_path,
        *generate_command(
            source_id,
            "budget-run",
            FIXTURES / "fake-author.jsonl",
            budget="50",
            reservation="100",
        ),
        expected=2,
    )
    assert "BUDGET_EXHAUSTED" in result.stderr
    with database(tmp_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM calls WHERE run_id='budget-run'"
        ).fetchone()[0]
    assert count == 0


def test_tiny_user_reservation_cannot_bypass_full_request_bound(
    tmp_path: Path,
) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    rejected = cli(
        tmp_path,
        *generate_command(
            source_id,
            "tiny-reservation-run",
            FIXTURES / "fake-author.jsonl",
            budget="1",
            reservation="1",
        ),
        expected=2,
    )
    assert json.loads(rejected.stderr)["code"] == "BUDGET_EXHAUSTED"
    with database(tmp_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM calls WHERE run_id='tiny-reservation-run'"
            ).fetchone()[0]
            == 0
        )

    control = json.loads(
        cli(
            tmp_path,
            *generate_command(
                source_id,
                "bounded-control-run",
                FIXTURES / "fake-author.jsonl",
                budget="100000",
                reservation="1",
            ),
        ).stdout
    )
    assert control["item_id"]
    with database(tmp_path) as connection:
        budget = connection.execute(
            "SELECT * FROM budgets WHERE run_id='bounded-control-run'"
        ).fetchone()
    assert int(budget["spent_value"]) <= int(budget["limit_value"])
    assert int(budget["reserved_value"]) == 0


def test_unexpected_provider_overage_is_recorded_and_stops_run(
    tmp_path: Path,
) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    first = json.loads(
        (FIXTURES / "fake-author.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    first["input_tokens"] = 1_000_000
    first["output_tokens"] = 1
    script = tmp_path / "overage.jsonl"
    script.write_text(json.dumps(first) + "\n", encoding="utf-8")
    rejected = cli(
        tmp_path,
        *generate_command(
            source_id,
            "overage-run",
            script,
            budget="2000000",
            reservation="1",
        ),
        expected=2,
    )
    assert json.loads(rejected.stderr)["code"] == "BUDGET_OVERAGE"
    with database(tmp_path) as connection:
        calls = connection.execute(
            "SELECT status,input_tokens,output_tokens,actual_cost_usd FROM calls WHERE run_id='overage-run'"
        ).fetchall()
        budget = connection.execute(
            "SELECT spent_value,reserved_value FROM budgets WHERE run_id='overage-run'"
        ).fetchone()
    assert len(calls) == 1
    assert calls[0]["status"] == "budget_overage"
    assert calls[0]["input_tokens"] == 1_000_000
    assert int(calls[0]["actual_cost_usd"]) == 1_000_001
    assert int(budget["spent_value"]) == 1_000_001
    assert int(budget["reserved_value"]) == 0


def test_usd_budget_requires_pricing_before_dispatch(tmp_path: Path) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    base = generate_command(
        source_id,
        "usd-without-pricing",
        FIXTURES / "fake-author.jsonl",
        budget="1",
        reservation="0",
    )
    rejected = cli(tmp_path, *base, "--budget-mode", "usd", expected=2)
    assert json.loads(rejected.stderr)["code"] == "BUDGET_EXHAUSTED"
    with database(tmp_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM calls WHERE run_id='usd-without-pricing'"
            ).fetchone()[0]
            == 0
        )

    control = cli(
        tmp_path,
        *generate_command(
            source_id,
            "usd-with-pricing",
            FIXTURES / "fake-author.jsonl",
            budget="1",
            reservation="0",
        ),
        "--budget-mode",
        "usd",
        "--input-price-per-million",
        "1",
        "--output-price-per-million",
        "1",
        "--reasoning-price-per-million",
        "1",
    )
    assert json.loads(control.stdout)["item_id"]


def test_provider_errors_redact_secrets(tmp_path: Path) -> None:
    receipt = smoke(tmp_path)
    source_id = receipt["screen"]["source_id"]
    script = author_script(
        tmp_path,
        {
            "role": "extractor",
            "kind": "error",
            "message": "Authorization: sk-private-secret-123456789",
        },
    )
    cli(
        tmp_path,
        *generate_command(source_id, "secret-run", script, retries=0),
        expected=2,
    )
    with database(tmp_path) as connection:
        error_text = connection.execute(
            "SELECT error_text FROM calls WHERE run_id='secret-run'"
        ).fetchone()[0]
    assert "sk-private-secret" not in error_text
    assert "[REDACTED]" in error_text


def test_stable_exports_and_absent_answer_label(tmp_path: Path) -> None:
    receipt = smoke(tmp_path, "stable-run")
    first = cli(tmp_path, "export", "--run-id", "stable-run", "--seed", "fixed")
    second = cli(tmp_path, "export", "--run-id", "stable-run", "--seed", "fixed")
    assert first.stdout == second.stdout
    manifest = json.loads(first.stdout)
    mcq_path = tmp_path / "arctic-qa" / manifest["files"]["mcq"]
    rows = [
        json.loads(line) for line in mcq_path.read_text(encoding="utf-8").splitlines()
    ]
    absent = next(row for row in rows if row["task_type"] == "answer_absent_mcq")
    assert absent["evidence_state"] == "invalid_option_set"
    assert "does not establish model ignorance" in absent["interpretation_limit"]
    assert receipt["test_only"] is True


def test_smoke_resume_does_not_duplicate_calls(tmp_path: Path) -> None:
    first = smoke(tmp_path, "resume-run")
    second = smoke(tmp_path, "resume-run")
    assert second["resumed"] is True
    assert first["item_id"] == second["item_id"]
    with database(tmp_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM calls WHERE run_id='resume-run'"
        ).fetchone()[0]
    assert count == 9


def test_shared_content_keeps_per_source_provenance(tmp_path: Path) -> None:
    metadata = tmp_path / "two-sources.json"
    metadata.write_text(
        json.dumps(
            [
                {
                    "stable_id": "test:source-a",
                    "title": "Source A",
                    "year": 2026,
                    "discipline": "test",
                },
                {
                    "stable_id": "test:source-b",
                    "title": "Source B",
                    "year": 2026,
                    "discipline": "test",
                },
            ]
        ),
        encoding="utf-8",
    )
    cli(tmp_path, "discover", "--adapter", "manual", "--input", str(metadata))
    with database(tmp_path) as connection:
        source_ids = [
            row[0]
            for row in connection.execute(
                "SELECT source_id FROM sources ORDER BY source_id"
            )
        ]
    for source_id in source_ids:
        cli(
            tmp_path,
            "fetch",
            "--source-id",
            source_id,
            "--url",
            (FIXTURES / "public-source.html").as_uri(),
            "--media-type",
            "text/html",
            "--allow-test-file",
        )
        cli(tmp_path, "extract", "--source-id", source_id)
    with database(tmp_path) as connection:
        original_rows = connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='original'"
        ).fetchone()[0]
    original_files = list(
        (tmp_path / "arctic-qa" / "originals").glob("*/*/source.html")
    )
    assert original_rows == 2
    assert len(original_files) == 1


def test_fetch_rejects_local_urls_without_explicit_test_fixture_mode(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "source.json"
    metadata.write_text(
        json.dumps(
            {
                "stable_id": "test:local-fetch",
                "title": "Local fetch boundary",
                "year": 2026,
                "discipline": "test",
            }
        ),
        encoding="utf-8",
    )
    cli(tmp_path, "discover", "--adapter", "manual", "--input", str(metadata))
    with database(tmp_path) as connection:
        source_id = connection.execute("SELECT source_id FROM sources").fetchone()[0]
    arguments = (
        "fetch",
        "--source-id",
        source_id,
        "--url",
        (FIXTURES / "public-source.html").as_uri(),
        "--media-type",
        "text/html",
    )
    rejected = cli(tmp_path, *arguments, expected=2)
    assert json.loads(rejected.stderr)["code"] == "SOURCE_URL_NOT_ALLOWED"

    allowed = json.loads(cli(tmp_path, *arguments, "--allow-test-file").stdout)
    assert allowed["media_type"] == "text/html"
    assert allowed["size_bytes"] > 0


def test_generation_runs_qa_gates_before_exact_option_verification(
    tmp_path: Path,
) -> None:
    receipt = smoke(tmp_path, "ordered-verification-run")
    item = candidate(tmp_path)
    with database(tmp_path) as connection:
        calls = connection.execute(
            "SELECT role,prompt_hash,response_json FROM calls WHERE run_id=? ORDER BY rowid",
            ("ordered-verification-run",),
        ).fetchall()
    roles = [row["role"] for row in calls]
    assert roles[:5] == [
        "extractor",
        "question_writer",
        "reconstructor",
        "answer_verifier",
        "distractor_writer",
    ]
    assert roles[5:] == ["option_verifier"] * 4
    assert item["schema_version"] == "2.0.0"
    assert item["finding_id"]
    assert len(item["option_verdicts"]) == 4
    assert all(verdict["option_text"] for verdict in item["option_verdicts"])
    assert receipt["validation"]["labels"]["mcq_eligible"] is True


def test_failed_qa_gate_stops_before_distractor_generation(tmp_path: Path) -> None:
    receipt = smoke(tmp_path, "setup-run")
    source_id = receipt["screen"]["source_id"]
    events = [
        json.loads(line)
        for line in (FIXTURES / "fake-verifier.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    events[0]["response"]["alternatives"] = ["another source-supported answer"]
    events[0]["response"]["scope"]["method"] = "a conflicting method"
    verifier = tmp_path / "ambiguous-verifier.jsonl"
    verifier.write_text(
        "\n".join(json.dumps(event) for event in events[:2]) + "\n",
        encoding="utf-8",
    )
    command = list(
        generate_command(
            source_id,
            "qa-gate-stop-run",
            FIXTURES / "fake-author.jsonl",
        )
    )
    command[command.index(str(FIXTURES / "fake-verifier.jsonl"))] = str(verifier)
    generated = json.loads(cli(tmp_path, *command).stdout)
    assert generated["status"] == "qa_gate_failed"
    assert generated["provenance"]["prompt_version"] == "arctic-qa-generation-v10"
    assert (
        generated["provenance"]["numeric_rule_contract_version"]
        == "numeric-rule-source-support-v2"
    )
    assert (
        generated["provenance"]["scope_contract_version"]
        == "selected-evidence-literal-scope-v2"
    )
    assert generated["distractors"] == []
    assert generated["qa_gate_reasons"] == [
        "reconstruction_scope_not_source_bound",
    ]
    candidate_path = tmp_path / "qa-gate-failed-candidate.json"
    candidate_path.write_text(json.dumps(generated), encoding="utf-8")
    validation = json.loads(
        cli(tmp_path, "validate", "--candidate", str(candidate_path)).stdout
    )
    assert validation["reasons"] == generated["qa_gate_reasons"]
    with database(tmp_path) as connection:
        roles = [
            row[0]
            for row in connection.execute(
                "SELECT role FROM calls WHERE run_id=? ORDER BY rowid",
                ("qa-gate-stop-run",),
            )
        ]
    assert roles == [
        "extractor",
        "question_writer",
        "reconstructor",
        "answer_verifier",
    ]


def test_generation_arms_share_one_frozen_finding(tmp_path: Path) -> None:
    receipt = smoke(tmp_path, "matched-arm-run")
    first = candidate(tmp_path)
    direct_author = tmp_path / "direct-author.jsonl"
    distractor_event = json.loads(
        (FIXTURES / "fake-author.jsonl").read_text(encoding="utf-8").splitlines()[2]
    )
    direct_author.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "direct_joint",
                        "response": {
                            "question": "What reported water depth was documented?",
                            "answer": first["answer"],
                        },
                    }
                ),
                json.dumps(distractor_event),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cli(
        tmp_path,
        *generate_command(
            receipt["screen"]["source_id"],
            "matched-arm-run",
            direct_author,
        ),
        "--arm",
        "direct_joint",
    )
    with database(tmp_path) as connection:
        finding_ids = {
            json.loads(row[0])["finding_id"]
            for row in connection.execute(
                "SELECT candidate_json FROM candidates WHERE run_id=?",
                ("matched-arm-run",),
            )
        }
        finding_count = connection.execute(
            "SELECT COUNT(*) FROM findings WHERE run_id=?",
            ("matched-arm-run",),
        ).fetchone()[0]
    assert finding_ids == {first["finding_id"]}
    assert finding_count == 1


def test_second_source_version_cannot_select_another_family_finding(
    tmp_path: Path,
) -> None:
    first = smoke(tmp_path, "family-finding-run")
    family_id = candidate(tmp_path)["source"]["paper_family_id"]
    stable_source_id = "test-only:public-arctic-source-version-b"
    source_id = stable_id("src", stable_source_id)
    metadata = tmp_path / "source-version-b.json"
    metadata.write_text(
        json.dumps(
            [
                {
                    "stable_id": stable_source_id,
                    "title": "Synthetic public Arctic extraction fixture, version B",
                    "published_date": "2026-09-11",
                    "year": 2026,
                    "discipline": "synthetic calibration",
                    "paper_family_id": family_id,
                }
            ]
        ),
        encoding="utf-8",
    )
    cli(tmp_path, "discover", "--adapter", "manual", "--input", str(metadata))
    cli(
        tmp_path,
        "fetch",
        "--source-id",
        source_id,
        "--url",
        (FIXTURES / "public-source.html").as_uri(),
        "--media-type",
        "text/html",
        "--allow-test-file",
    )
    cli(tmp_path, "extract", "--source-id", source_id)
    quote = "The complete study site was at 71.3 N."
    locator = source_locator_for_quote(tmp_path, source_id, quote)
    with database(tmp_path) as connection:
        content_hash = connection.execute(
            "SELECT content_hash FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()[0]
    evidence = write_candidate(
        tmp_path,
        {
            "evidence_kind": "site_coordinates",
            "latitudes": [71.3],
            "named_regions": [],
            "source_content_hash": content_hash,
            "evidence_quote": quote,
            "locator": locator,
            "site_coverage": "complete",
            "test_only": True,
        },
        "source-version-b-screen.json",
    )
    cli(tmp_path, "screen", "--source-id", source_id, "--evidence", str(evidence))

    rejected = cli(
        tmp_path,
        *generate_command(
            source_id, "family-finding-run", FIXTURES / "fake-author.jsonl"
        ),
        expected=2,
    )
    error = json.loads(rejected.stderr)
    assert "already has a frozen finding" in error["message"]
    assert first["item_id"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("fabricated_reconstruction_quote", "reconstruction_evidence_not_located"),
        ("stated_alternative", "alternative_answer_unresolved"),
        ("causal_question", "causal_overclaim"),
    ],
)
def test_reconstruction_and_independent_claim_type_are_enforced(
    tmp_path: Path, mutation: str, reason: str
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    if mutation == "fabricated_reconstruction_quote":
        item["reconstruction"]["evidence_quote"] = "A fabricated quotation."
    elif mutation == "stated_alternative":
        item["reconstruction"]["alternatives"] = ["another supported answer"]
        item["answer_verification"]["alternative_answer_search_passed"] = False
    else:
        item["answer_verification"]["question_claim_type"] = "causal"
        item["reconstruction"]["question_claim_type"] = "causal"
    path = write_candidate(tmp_path, item, f"{mutation}.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert reason in result["reasons"]
    assert result["final_label"] in {"rejected", "unresolved"}


def test_author_flags_and_hidden_numeric_metadata_cannot_certify_true_options(
    tmp_path: Path,
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    true_paraphrases = [
        ("The reported depth was 2.0 m.", "5"),
        ("A depth of 200 cm was reported.", "6"),
        ("Two metres was reported.", "7"),
    ]
    for distractor, (text, hidden_value) in zip(
        item["distractors"][:3], true_paraphrases, strict=True
    ):
        distractor["text"] = text
        distractor["numeric"] = {"canonical_value": hidden_value, "unit": "m"}
        distractor["verification"] = {
            "model_verified": True,
            "alternative_answer_search_passed": True,
        }
    item["distractors"] = item["distractors"][:3]
    path = write_candidate(tmp_path, item, "true-paraphrase-options.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    reasons = {reason for row in result["distractors"] for reason in row["reasons"]}
    assert "numeric_metadata_display_mismatch" in reasons
    assert result["labels"]["mcq_eligible"] is False


def test_author_verification_flags_have_no_acceptance_authority(
    tmp_path: Path,
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["option_verdicts"] = []
    for distractor in item["distractors"]:
        distractor["verification"] = {
            "model_verified": True,
            "alternative_answer_search_passed": True,
        }
    path = write_candidate(tmp_path, item, "forged-author-flags.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["labels"]["mcq_eligible"] is False
    assert all(
        "option_verdict_missing_or_stale" in row["reasons"]
        for row in result["distractors"]
    )


@pytest.mark.parametrize(
    "displayed_text",
    [
        "The reported depth was not 5.0 m.",
        "The source measured 2.0 m, not the proposed 5.0 m.",
        "The depth was 5.0 m, or the reported substrate was gravel.",
    ],
)
def test_negated_or_compound_numeric_options_fail_closed(
    tmp_path: Path, displayed_text: str
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["distractors"][0]["text"] = displayed_text
    item["distractors"][0]["numeric"] = {
        "canonical_value": "5.0",
        "unit": "m",
    }
    item["distractors"] = item["distractors"][:3]
    item["option_verdicts"] = item["option_verdicts"][:3]
    path = write_candidate(tmp_path, item, "compound-numeric-option.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    row = next(
        value for value in result["distractors"] if value["text"] == displayed_text
    )
    assert row["accepted"] is False
    assert "numeric_display_ambiguous" in row["reasons"]
    assert result["labels"]["mcq_eligible"] is False


def test_truth_in_another_scope_does_not_invalidate_a_scoped_distractor(
    tmp_path: Path,
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["option_verdicts"][0]["true_in_different_context"] = True
    item["option_verdicts"][0]["question_admits_option_as_correct"] = False
    sync_option_receipt(tmp_path, item, 0)
    path = write_candidate(tmp_path, item, "different-scope-truth.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    row = next(value for value in result["distractors"] if value["text"] == "2.5 m")
    assert row["accepted"] is True
    assert row["label"] == "deterministic-contradiction"


def test_option_verdict_hashes_are_source_and_display_bound(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["option_verdicts"][0]["source_hash"] = "sha256:wrong-source"
    item["option_verdicts"][1]["option_text"] = "A stale option text."
    path = write_candidate(tmp_path, item, "stale-option-verdicts.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    reasons = {reason for row in result["distractors"] for reason in row["reasons"]}
    assert "option_verdict_source_hash_mismatch" in reasons
    assert "option_verdict_text_mismatch" in reasons
    assert result["labels"]["mcq_eligible"] is False


@pytest.mark.parametrize("forgery", ["source", "receipt", "qa_receipt"])
def test_validation_rejects_unstored_source_or_verifier_provenance(
    tmp_path: Path, forgery: str
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    if forgery == "source":
        item["source"]["content_hash"] = "forged-source-hash"
        for verdict in item["option_verdicts"]:
            verdict["source_hash"] = "forged-source-hash"
        expected_reason = "source_manifest_mismatch"
    elif forgery == "receipt":
        for verdict in item["option_verdicts"]:
            verdict["provenance"].update(
                {
                    "provider": "invented-provider",
                    "requested_model": "invented-model",
                    "prompt_hash": "invented-prompt-hash",
                }
            )
        expected_reason = "option_verdict_call_receipt_missing"
    else:
        item["provenance"].pop("verification_calls", None)
        expected_reason = "qa_verification_call_receipt_missing"
    path = write_candidate(tmp_path, item, f"forged-{forgery}.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["labels"]["mcq_eligible"] is False
    if forgery == "source":
        assert result["final_label"] == "rejected"
        assert expected_reason in result["reasons"]
    elif forgery == "receipt":
        assert all(expected_reason in row["reasons"] for row in result["distractors"])
    else:
        assert result["final_label"] == "rejected"
        assert expected_reason in result["reasons"]


def test_validation_rejects_empty_option_receipt_response(tmp_path: Path) -> None:
    receipt = smoke(tmp_path, "empty-option-response-run")
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE calls SET response_json='{}' WHERE role='option_verifier'"
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    assert result["labels"]["mcq_eligible"] is False
    assert all(row["accepted"] is False for row in result["distractors"])
    assert all(
        "option_verdict_call_receipt_missing" in row["reasons"]
        for row in result["distractors"]
    )


def test_validation_rejects_empty_array_option_receipt_response(
    tmp_path: Path,
) -> None:
    receipt = smoke(tmp_path, "empty-array-option-response-run")
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE calls SET response_json='[]' WHERE role='option_verifier'"
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    assert result["labels"]["mcq_eligible"] is False
    assert all(row["accepted"] is False for row in result["distractors"])
    assert all(
        "option_verdict_call_receipt_missing" in row["reasons"]
        for row in result["distractors"]
    )


def test_validation_rejects_nonempty_array_option_receipt_response(
    tmp_path: Path,
) -> None:
    receipt = smoke(tmp_path, "nonempty-array-option-response-run")
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE calls SET response_json='[true]' WHERE role='option_verifier'"
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    assert result["labels"]["mcq_eligible"] is False
    assert all(row["accepted"] is False for row in result["distractors"])
    assert all(
        "option_verdict_call_receipt_missing" in row["reasons"]
        for row in result["distractors"]
    )


def test_validation_rejects_incomplete_option_receipt_response(
    tmp_path: Path,
) -> None:
    receipt = smoke(tmp_path, "incomplete-option-response-run")
    with database(tmp_path) as connection:
        connection.execute(
            """UPDATE calls SET response_json='{"contradiction_established":true}'
            WHERE role='option_verifier'"""
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    assert result["labels"]["mcq_eligible"] is False
    assert all(row["accepted"] is False for row in result["distractors"])
    assert all(
        "option_verdict_call_receipt_missing" in row["reasons"]
        for row in result["distractors"]
    )


def test_validation_accepts_complete_option_receipt_response(tmp_path: Path) -> None:
    receipt = smoke(tmp_path, "complete-option-response-run")

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    assert result["final_label"] == "machine_accepted_unverified"
    assert result["labels"]["mcq_eligible"] is True
    assert all(row["accepted"] is True for row in result["distractors"])


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("source_span_id", "finding-evidence-span-v2-forged"),
        ("evidence_text_sha256", "0" * 64),
        ("span_contract_version", "finding-evidence-span-forged"),
        ("evidence_quote", "A changed quote."),
        ("locator", {"chunk_id": "forged", "start_offset": 0, "end_offset": 1}),
    ],
)
def test_stored_answer_span_tampering_blocks_validation_and_export(
    tmp_path: Path, field: str, tampered_value: object
) -> None:
    receipt = smoke(tmp_path, f"answer-span-{field}")
    item = candidate(tmp_path)
    control_path = write_candidate(tmp_path, item, f"answer-control-{field}.json")
    control = json.loads(
        cli(tmp_path, "validate", "--candidate", str(control_path)).stdout
    )
    assert control["final_label"] == "machine_accepted_unverified"

    item["answer"][field] = tampered_value
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE candidates SET candidate_json=? WHERE item_id=?",
            (canonical_json(item), receipt["item_id"]),
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    assert result["final_label"] == "rejected"
    assert "answer_evidence_span_invalid" in result["reasons"]
    exported = json.loads(
        cli(tmp_path, "export", "--run-id", f"answer-span-{field}").stdout
    )
    assert exported["short_answer_count"] == 0
    assert exported["mcq_count"] == 0


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("source_span_id", "finding-evidence-span-v2-forged"),
        ("evidence_text_sha256", "0" * 64),
        ("span_contract_version", "finding-evidence-span-forged"),
        ("evidence_quote", "A changed quote."),
        ("locator", {"chunk_id": "forged", "start_offset": 0, "end_offset": 1}),
    ],
)
def test_stored_distractor_span_tampering_excludes_that_option_from_export(
    tmp_path: Path, field: str, tampered_value: object
) -> None:
    run_id = f"distractor-span-{field}"
    receipt = smoke(tmp_path, run_id)
    item = candidate(tmp_path)
    control_path = write_candidate(tmp_path, item, f"distractor-control-{field}.json")
    control = json.loads(
        cli(tmp_path, "validate", "--candidate", str(control_path)).stdout
    )
    assert control["labels"]["mcq_eligible"] is True

    tampered_text = item["distractors"][0]["text"]
    item["distractors"][0][field] = tampered_value
    with database(tmp_path) as connection:
        connection.execute(
            "UPDATE candidates SET candidate_json=? WHERE item_id=?",
            (canonical_json(item), receipt["item_id"]),
        )

    result = json.loads(
        cli(tmp_path, "validate", "--item-id", receipt["item_id"]).stdout
    )
    tampered = next(
        row for row in result["distractors"] if row["text"] == tampered_text
    )
    assert tampered["accepted"] is False
    assert "distractor_proposal_evidence_span_invalid" in tampered["reasons"]
    exported = json.loads(cli(tmp_path, "export", "--run-id", run_id).stdout)
    assert exported["short_answer_count"] == 1
    assert exported["mcq_count"] == 1


def test_external_candidate_validation_cannot_change_stored_candidate_status(
    tmp_path: Path,
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["source"]["content_hash"] = "forged-source-hash"
    path = write_candidate(tmp_path, item, "candidate-id-alias.json")
    validation = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert validation["final_label"] == "rejected"

    status = json.loads(cli(tmp_path, "status").stdout)
    assert status["candidates"] == [
        {"count": 1, "status": "machine_accepted_unverified"}
    ]


def test_identical_content_in_separate_runs_has_separate_candidate_identity(
    tmp_path: Path,
) -> None:
    first = smoke(tmp_path, "identity-run-a")
    generated = json.loads(
        cli(
            tmp_path,
            *generate_command(
                first["screen"]["source_id"],
                "identity-run-b",
                FIXTURES / "fake-author.jsonl",
            ),
        ).stdout
    )
    assert generated["item_id"] != first["item_id"]

    validation = json.loads(
        cli(tmp_path, "validate", "--item-id", generated["item_id"]).stdout
    )
    assert validation["final_label"] == "machine_accepted_unverified"
    exported = json.loads(cli(tmp_path, "export", "--run-id", "identity-run-b").stdout)
    assert exported["short_answer_count"] == 1
    assert exported["mcq_count"] == 2


def test_unestablished_option_contradiction_is_not_export_eligible(
    tmp_path: Path,
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["option_verdicts"][0]["contradiction_established"] = False
    sync_option_receipt(tmp_path, item, 0)
    path = write_candidate(tmp_path, item, "unresolved-option.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["labels"]["mcq_eligible"] is True
    rejected = next(row for row in result["distractors"] if row["text"] == "2.5 m")
    assert rejected["accepted"] is False
    assert "option_contradiction_unresolved" in rejected["reasons"]


def test_insufficient_options_preserve_accepted_short_answer(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["distractors"] = item["distractors"][:2]
    item["option_verdicts"] = item["option_verdicts"][:2]
    path = write_candidate(tmp_path, item, "qa-with-two-options.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "machine_accepted_unverified"
    assert result["labels"]["mcq_eligible"] is False
    assert "insufficient_verified_distractors" in result["reasons"]


def test_unsafe_legacy_candidate_is_rejected(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["schema_version"] = "1.0.0"
    item.pop("option_verdicts", None)
    path = write_candidate(tmp_path, item, "legacy-candidate.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "rejected"
    assert "unsafe_legacy_candidate_schema" in result["reasons"]


def test_v2_database_migrates_with_recoverable_backup(tmp_path: Path) -> None:
    namespace = tmp_path / "arctic-qa"
    namespace.mkdir()
    with sqlite3.connect(namespace / "state.sqlite3") as connection:
        connection.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_info(version) VALUES (2)")
    status = json.loads(cli(tmp_path, "status").stdout)
    assert status["sources"] == 0
    backups = list((namespace / "backups").glob("state-before-v5-*.sqlite3"))
    assert len(backups) == 1
    with database(tmp_path) as connection:
        assert connection.execute("SELECT version FROM schema_info").fetchone()[0] == 5
        finding_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(findings)")
        }
        finding_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(findings)")
        }
    assert "run_id" in finding_columns
    assert "sqlite_autoindex_findings_2" in finding_indexes
