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
    unrelated = {
        "evidence_kind": "site_coordinates",
        "latitudes": [71.3],
        "named_regions": [],
        "source_content_hash": item["source"]["content_hash"],
        "evidence_quote": item["answer"]["evidence_quote"],
        "locator": item["answer"]["locator"],
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
        ("wrong_quote", "answer_evidence_not_located", "rejected"),
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
    path = write_candidate(tmp_path, item, f"{mutation}.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == label
    assert reason in result["reasons"]


def test_validation_requires_positive_source_entailment(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["verification"]["source_entailment_model_verified"] = False
    path = write_candidate(tmp_path, item, "failed-entailment.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "unresolved"
    assert "source_entailment_not_verified" in result["reasons"]

    item["verification"]["source_entailment_model_verified"] = True
    path = write_candidate(tmp_path, item, "passed-entailment.json")
    control = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert control["final_label"] == "machine_accepted_unverified"


def test_true_distractors_and_equivalent_units_are_not_false(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["distractors"][0]["text"] = "2 m"
    item["distractors"][0]["numeric"] = {"canonical_value": "200", "unit": "cm"}
    item["distractors"][1]["true_under_other_scope"] = True
    item["distractors"][2]["text"] = "2.0 m"
    item["distractors"][3]["text"] = "None of the above"
    path = write_candidate(tmp_path, item, "true-distractors.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "rejected"
    reasons = [reason for row in result["distractors"] for reason in row["reasons"]]
    assert "distractor_is_equivalent_numeric_answer" in reasons
    assert "distractor_true_under_other_scope" in reasons
    assert "distractor_matches_answer" in reasons
    assert "forbidden_meta_option" in reasons


def test_numeric_rule_must_match_source_and_displayed_answer(tmp_path: Path) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    item["answer"]["numeric_rule"]["canonical_value"] = "999"
    path = write_candidate(tmp_path, item, "unbound-numeric-rule.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "rejected"
    assert all(
        "source_bound_numeric_rule_missing" in row["reasons"]
        for row in result["distractors"]
    )

    control = candidate(tmp_path)
    control_path = write_candidate(tmp_path, control, "bound-numeric-rule.json")
    accepted = json.loads(
        cli(tmp_path, "validate", "--candidate", str(control_path)).stdout
    )
    assert accepted["final_label"] == "machine_accepted_unverified"


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
    assert result["final_label"] == "rejected"
    for row in result["distractors"][:3]:
        assert row["deterministic"] is False
        assert "source_bound_predicate_missing" in row["reasons"]

    control = candidate(tmp_path)
    control_path = write_candidate(tmp_path, control, "numeric-control.json")
    accepted = json.loads(
        cli(tmp_path, "validate", "--candidate", str(control_path)).stdout
    )
    assert accepted["final_label"] == "machine_accepted_unverified"


@pytest.mark.parametrize(
    ("kind", "quote", "answer_text", "answer_rule", "options"),
    [
        (
            "unique_categorical",
            "The only reported substrate category was gravel.",
            "gravel",
            {"kind": "closed_set", "source_values": ["gravel"]},
            ["sand", "silt", "clay"],
        ),
        (
            "directional_contradiction",
            "The reported trend increased.",
            "increased",
            {"kind": "directional_relation", "source_value": "increased"},
            ["decreased", "the response decreased", "a decreased response"],
        ),
        (
            "scope_excluded",
            "The only included region was the central basin.",
            "central basin",
            {"kind": "closed_scope", "source_values": ["central basin"]},
            ["outer basin", "southern shelf", "river delta"],
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
) -> None:
    smoke(tmp_path)
    item = candidate(tmp_path)
    locator = source_locator_for_quote(tmp_path, item["source"]["source_id"], quote)
    item["answer"].update(
        {
            "text": answer_text,
            "variants": [],
            "evidence_quote": quote,
            "locator": locator,
            "required_question_phrases": [],
            "deterministic_rule": answer_rule,
        }
    )
    item["answer"].pop("numeric_rule", None)
    item["question"] = "What source-bounded value was reported?"
    item["reconstruction"] = {
        "answer": answer_text,
        "evidence_quote": quote,
        "ambiguity_label": "one_answer",
        "alternatives": [],
    }
    item["distractors"] = []
    for option in options:
        predicate = {"kind": kind}
        if kind == "directional_contradiction":
            predicate["candidate_relation"] = "decreased"
        else:
            predicate["candidate_value"] = option
        item["distractors"].append(
            {
                "text": option,
                "type": kind,
                "evidence_quote": quote,
                "locator": locator,
                "verification": {
                    "model_verified": True,
                    "alternative_answer_search_passed": True,
                },
                "deterministic": predicate,
            }
        )
    path = write_candidate(tmp_path, item, f"valid-{kind}.json")
    result = json.loads(cli(tmp_path, "validate", "--candidate", str(path)).stdout)
    assert result["final_label"] == "machine_accepted_unverified"
    assert all(row["deterministic"] for row in result["distractors"])


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
    assert count == 5


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
