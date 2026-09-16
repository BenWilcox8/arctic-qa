from __future__ import annotations

import json
from pathlib import Path

import pytest

from arctic_qa.abstention_providers import (
    ScriptedEvaluationProvider,
    annotate_models,
    decoding_record,
)
from arctic_qa.abstention_render import GOLD_ABSENT, GOLD_PRESENT, N0, N1, N3, N5
from arctic_qa.abstention_run import (
    RESPONSES_FILENAME,
    RUN_MANIFEST_FILENAME,
    dry_run,
    plan_trials,
    run_evaluation,
)
from arctic_qa.abstention_score import (
    metrics_from_counts,
    random_baseline,
    score_run,
    uniform_baseline,
)
from arctic_qa.abstention_set import load_eval_set, write_eval_set
from arctic_qa.cli import main as cli_main
from test_abstention_render import item


ROOT = Path(__file__).parents[1]
PRO = "gemini-3.1-pro-preview"
FLASH = "gemini-3.8-flash"


def frozen_set(tmp_path: Path, count: int = 3) -> Path:
    manifest = write_eval_set(
        [item(item_id=f"aqa-{index}") for index in range(count)],
        excluded=[],
        output_dir=tmp_path / "sets",
        population_record={"population": "list"},
        k=4,
        state_db=None,
    )
    return tmp_path / "sets" / manifest["eval_set_id"]


def test_plan_covers_both_conditions_models_arms_and_repeats(tmp_path: Path) -> None:
    set_dir = frozen_set(tmp_path)
    manifest, items = load_eval_set(set_dir)
    trials = plan_trials(
        manifest, items, models=[PRO, FLASH], arms=["low", "high"], repeats=2
    )
    assert len(trials) == 3 * 2 * 2 * 2 * 2
    assert len({row["trial_id"] for row in trials}) == len(trials)
    present = [row for row in trials if row["condition"] == GOLD_PRESENT]
    absent = [row for row in trials if row["condition"] == GOLD_ABSENT]
    assert {len(row["options"]) for row in trials} == {5}
    assert all(row["dropped_distractor_text"] for row in present)
    assert all(row["dropped_distractor_text"] is None for row in absent)


def test_scripted_provider_run_resumes_scores_and_files_invalid_responses(
    tmp_path: Path,
) -> None:
    set_dir = frozen_set(tmp_path)
    manifest, items = load_eval_set(set_dir)
    trials = plan_trials(manifest, items, models=[PRO], arms=["medium"], repeats=2)
    invalid_trial = trials[0]["trial_id"]
    max_tokens_trial = trials[1]["trial_id"]
    provider = ScriptedEvaluationProvider(
        policy="gold",
        overrides={
            invalid_trial: {"text": "I choose B."},
            max_tokens_trial: {
                "text": trials[1]["correct_letter"],
                "finish_reason": "MAX_TOKENS",
            },
        },
    )
    price_config = json.loads(
        (ROOT / "config" / "benchmark-evaluation-prices-v1.json").read_text()
    )
    decoding = decoding_record(price_config, [PRO], ["medium"])
    run_dir = tmp_path / "run"
    first = run_evaluation(
        set_dir=set_dir,
        output_dir=run_dir,
        run_id="r1",
        models=[PRO],
        arms=["medium"],
        repeats=2,
        provider=provider,
        decoding=decoding,
        max_calls=5,
    )
    assert first["recorded_trials"] == 5 and first["complete"] is False
    second = run_evaluation(
        set_dir=set_dir,
        output_dir=run_dir,
        run_id="r1",
        models=[PRO],
        arms=["medium"],
        repeats=2,
        provider=provider,
        decoding=decoding,
    )
    assert second["recorded_trials"] == 12 and second["complete"] is True
    assert second["calls_this_invocation"] == 7
    rows = [
        json.loads(line)
        for line in (run_dir / RESPONSES_FILENAME).read_text().splitlines()
    ]
    assert len(rows) == 12 and len({row["trial_id"] for row in rows}) == 12
    by_id = {row["trial_id"]: row for row in rows}
    assert by_id[invalid_trial]["outcome"] == N0
    assert by_id[invalid_trial]["invalid_reason"] == "not_a_single_letter"
    assert by_id[invalid_trial]["response"]["raw_text"] == "I choose B."
    assert by_id[max_tokens_trial]["outcome"] == N0
    assert by_id[max_tokens_trial]["invalid_reason"] == "finish_reason_not_stop"
    valid = [row for row in rows if row["valid"]]
    assert {row["outcome"] for row in valid if row["condition"] == GOLD_PRESENT} == {N1}
    assert {row["outcome"] for row in valid if row["condition"] == GOLD_ABSENT} == {N5}
    per_model = second["per_model_arm"][0]
    assert per_model["invalid_count"] == 2 and per_model["invalid_reasons"] == {
        "not_a_single_letter": 1,
        "finish_reason_not_stop": 1,
    }
    manifest_record = json.loads((run_dir / RUN_MANIFEST_FILENAME).read_text())
    assert manifest_record["re_ask"] is False
    assert manifest_record["decoding"]["temperature_by_model"] == {PRO: "2.0"}
    with pytest.raises(ValueError, match="different run manifest"):
        run_evaluation(
            set_dir=set_dir,
            output_dir=run_dir,
            run_id="r1",
            models=[PRO],
            arms=["low"],
            repeats=2,
            provider=provider,
            decoding=decoding,
        )
    scores = score_run(
        responses_file=run_dir / RESPONSES_FILENAME,
        items=items,
        k=4,
        output_dir=run_dir / "scores",
        resamples=100,
        seed=1,
        run_manifest=manifest_record,
    )
    group = scores["groups"][f"{PRO}/medium"]
    # Both invalid trials are gold-present repeats of the first item.
    assert group["counts"] == {"N0": 2, "N1": 4, "N2": 0, "N3": 0, "N4": 0, "N5": 6}
    assert group["invalid_rate"] == pytest.approx(2 / 12)
    assert group["metrics"]["acc"] == 1.0 and group["metrics"]["ssr"] == 1.0
    assert group["metrics"]["ideal_pair_rate"] == 1.0
    assert group["sensitivity_invalid_as_abstention"]["counts"]["N0"] == 0
    assert group["intervals"]["acc"]["low"] == 1.0
    assert scores["random_baseline"]["metrics"]["acc"] == pytest.approx(0.2)
    assert scores["contamination"][0]["items_by_role"] == {"judge": 3}
    assert scores["contamination"][0]["metrics_without_role"] is None
    for name in (
        "scores.json",
        "main-table.csv",
        "main-table.tex",
        "condition-table.csv",
        "letters.csv",
        "contamination.csv",
        "contamination.tex",
        "strata.csv",
    ):
        assert (run_dir / "scores" / name).is_file()
    latex = (run_dir / "scores" / "main-table.tex").read_text()
    assert r"\textbf{gemini-3.1-pro-preview}" in latex and "Random Baseline" in latex
    csv_text = (run_dir / "scores" / "main-table.csv").read_text()
    assert csv_text.splitlines()[0].startswith(
        "model,arm,items,trials,N0,N1,N2,N3,N4,N5"
    )


def test_metric_formulas_reproduce_both_random_baselines() -> None:
    previous = uniform_baseline(5, 4)["metrics"]
    expected = {
        "acc": 0.225,
        "precision_abs": 0.556,
        "recall_abs": 0.156,
        "f1_abs": 0.244,
        "abstention_rate": 0.225,
        "r_acc": 0.129,
        "ssr": 0.325,
    }
    for name, value in expected.items():
        assert round(previous[name], 3) == value, name
    current = random_baseline(4)["metrics"]
    for name, value in {
        "acc": 0.2,
        "precision_abs": 0.5,
        "recall_abs": 0.125,
        "f1_abs": 0.2,
        "abstention_rate": 0.2,
        "r_acc": 0.125,
        "ssr": 0.3,
    }.items():
        assert current[name] == pytest.approx(value), name
    three = random_baseline(3)["metrics"]
    assert three["acc"] == pytest.approx(0.25) and three["ssr"] == pytest.approx(0.375)
    undefined = metrics_from_counts(
        {"N0": 3, "N1": 0, "N2": 0, "N3": 0, "N4": 0, "N5": 0}
    )
    assert all(value is None for value in undefined.values())
    always_abstain = metrics_from_counts(
        {"N0": 0, "N1": 0, "N2": 0, "N3": 1, "N4": 0, "N5": 1}
    )
    assert always_abstain["acc"] == 0.5 and always_abstain["r_acc"] is None
    assert always_abstain["recall_abs"] == 1.0 and always_abstain["ssr"] == 1.0


def test_dry_run_exercises_the_broker_path_and_separates_the_phases(
    tmp_path: Path,
) -> None:
    set_dir = frozen_set(tmp_path, count=2)
    manifest, items = load_eval_set(set_dir)
    trials = plan_trials(
        manifest, items, models=[PRO, FLASH], arms=["medium"], repeats=1
    )
    invalid = trials[0]["trial_id"]
    summary = dry_run(
        set_dir=set_dir,
        output_dir=tmp_path / "dry",
        run_id="dry-1",
        models=[PRO, FLASH],
        arms=["medium"],
        repeats=1,
        construction_policy_file=ROOT
        / "config"
        / "streaming-dataset-budget-policy-v1.json",
        construction_price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        construction_gate_file=ROOT
        / "config"
        / "streaming-live-execution-gate-v1.json",
        evaluation_policy_file=ROOT / "config" / "benchmark-evaluation-policy-v1.json",
        evaluation_price_config_file=ROOT
        / "config"
        / "benchmark-evaluation-prices-v1.json",
        policy="abstain",
        overrides={invalid: {"text": "B?"}},
        code_commit="test-commit",
    )
    assert summary["planned_trials"] == 8 and summary["complete"] is True
    assert summary["dry_run"]["transport_calls"] == 16
    ledger = summary["dry_run"]["ledger_evaluation"]
    assert ledger["submissions"] == 8 and ledger["phase"] == "benchmark_evaluation"
    assert float(ledger["spent_usd"]) > 0 and ledger["ambiguous_usd"] == "0"
    usage = summary["dry_run"]["ledger_usage"]
    assert usage["benchmark_evaluation_usd"] == ledger["spent_usd"]
    assert float(usage["dataset_construction_usd"]) == 0
    assert usage["project_lifetime_usd"] == ledger["spent_usd"]
    rows = [
        json.loads(line)
        for line in (tmp_path / "dry" / RESPONSES_FILENAME).read_text().splitlines()
    ]
    by_id = {row["trial_id"]: row for row in rows}
    assert (
        by_id[invalid]["outcome"] == N0
        and by_id[invalid]["response"]["raw_text"] == "B?"
    )
    valid = [row for row in rows if row["valid"]]
    assert {row["outcome"] for row in valid if row["condition"] == GOLD_PRESENT} == {N3}
    assert {row["outcome"] for row in valid if row["condition"] == GOLD_ABSENT} == {N5}
    assert all(row["response"]["receipt_sha256"] for row in rows)
    assert all(row["response"]["request_key"] for row in rows)
    receipts = tmp_path / "dry" / "ledger" / "model-receipts"
    assert len(list(receipts.glob("*.json"))) >= 8 * 3
    # A second dry run resumes from the same receipts and makes no new call.
    again = dry_run(
        set_dir=set_dir,
        output_dir=tmp_path / "dry",
        run_id="dry-1",
        models=[PRO, FLASH],
        arms=["medium"],
        repeats=1,
        construction_policy_file=ROOT
        / "config"
        / "streaming-dataset-budget-policy-v1.json",
        construction_price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        construction_gate_file=ROOT
        / "config"
        / "streaming-live-execution-gate-v1.json",
        evaluation_policy_file=ROOT / "config" / "benchmark-evaluation-policy-v1.json",
        evaluation_price_config_file=ROOT
        / "config"
        / "benchmark-evaluation-prices-v1.json",
        policy="abstain",
        overrides={invalid: {"text": "B?"}},
        code_commit="test-commit",
    )
    assert again["calls_this_invocation"] == 0
    assert again["dry_run"]["ledger_evaluation"]["submissions"] == 8


def test_cli_dry_run_score_render_and_list_models(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_dir = frozen_set(tmp_path, count=2)
    common = [
        "--json",
        "--test-mode",
        "--data-root",
        str(tmp_path / "data"),
        "abstention-eval",
        "--streaming-budget-policy-file",
        str(ROOT / "config" / "streaming-dataset-budget-policy-v1.json"),
        "--price-config-file",
        str(ROOT / "config" / "gemini-eligibility-v1.json"),
        "--execution-gate-file",
        str(ROOT / "config" / "streaming-live-execution-gate-v1.json"),
        "--evaluation-policy-file",
        str(ROOT / "config" / "benchmark-evaluation-policy-v1.json"),
        "--evaluation-price-config-file",
        str(ROOT / "config" / "benchmark-evaluation-prices-v1.json"),
    ]
    assert (
        cli_main(
            [
                *common,
                "--action",
                "render",
                "--eval-set-dir",
                str(set_dir),
                "--models",
                PRO,
                "--arms",
                "low,high",
                "--repeats",
                "3",
                "--output-file",
                str(tmp_path / "trials.jsonl"),
            ]
        )
        == 0
    )
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["trials"] == 2 * 2 * 3 * 2
    assert "I abstain from answering" in rendered["sample"]["system_text"]
    assert (
        cli_main(
            [
                *common,
                "--action",
                "dry-run",
                "--eval-set-dir",
                str(set_dir),
                "--run-dir",
                str(tmp_path / "run"),
                "--run-id",
                "cli-dry",
                "--models",
                PRO,
                "--arms",
                "medium",
                "--repeats",
                "1",
                "--scripted-policy",
                "gold",
                "--bootstrap",
                "50",
                "--code-commit",
                "cli-commit",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["complete"] is True and summary["recorded_trials"] == 4
    assert summary["scores"][f"{PRO}/medium"]["metrics"]["acc"] == 1.0
    assert (
        cli_main(
            [
                *common,
                "--action",
                "score",
                "--run-dir",
                str(tmp_path / "run"),
                "--bootstrap",
                "20",
            ]
        )
        == 0
    )
    scored = json.loads(capsys.readouterr().out)
    assert "main-table.tex" in scored["files"]
    saved = tmp_path / "models.json"
    saved.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "name": "models/gemini-3.1-pro-preview",
                        "displayName": "Gemini 3.1 Pro Preview",
                        "supportedGenerationMethods": [
                            "generateContent",
                            "countTokens",
                        ],
                        "inputTokenLimit": 1048576,
                        "outputTokenLimit": 65536,
                        "thinking": True,
                        "version": "3.1",
                    },
                    {
                        "name": "models/gemini-2.5-pro",
                        "displayName": "Gemini 2.5 Pro",
                        "supportedGenerationMethods": ["generateContent"],
                        "thinking": True,
                    },
                    {
                        "name": "models/gemini-3.8-flash",
                        "displayName": "Gemini 3.8 Flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/imagen-4",
                        "displayName": "Imagen",
                        "supportedGenerationMethods": ["predict"],
                    },
                ]
            }
        )
    )
    assert (
        cli_main([*common, "--action", "list-models", "--models-file", str(saved)]) == 0
    )
    listed = json.loads(capsys.readouterr().out)
    assert listed["pro_variants"] == ["gemini-2.5-pro", "gemini-3.1-pro-preview"]
    assert listed["priced_models"] == ["gemini-3.1-pro-preview", "gemini-3.8-flash"]
    rows = {row["model"]: row for row in listed["models"]}
    assert rows["gemini-3.1-pro-preview"]["thinking_levels"] == [
        "low",
        "medium",
        "high",
    ]
    assert rows["gemini-3.1-pro-preview"]["temperature"] == "2.0"
    assert rows["gemini-2.5-pro"]["priced"] is False
    assert rows["imagen-4"]["supports_generate_content"] is False
    assert annotate_models([], {"models": {}}) == []
    # The canary refuses a non-Pro model and a ceiling above USD 5.00 before any broker work.
    assert (
        cli_main(
            [
                *common,
                "--action",
                "canary",
                "--eval-set-dir",
                str(set_dir),
                "--run-dir",
                str(tmp_path / "canary"),
                "--run-id",
                "c",
                "--model",
                FLASH,
                "--evaluation-gate-file",
                str(tmp_path / "missing-gate.json"),
            ]
        )
        == 2
    )
    assert "Pro variant" in capsys.readouterr().err
