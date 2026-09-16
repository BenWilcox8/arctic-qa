"""Command-line surface of the abstention evaluation harness.

``python -m arctic_qa abstention-eval --action <action> ...``

Actions: ``build-set``, ``render``, ``dry-run``, ``run``, ``canary``, ``score``,
``list-models``, ``gate-template``. See docs/ABSTENTION_EVALUATION.md.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .abstention_providers import (
    annotate_models,
    decoding_record,
    fetch_model_list,
)
from .abstention_render import CONDITIONS
from .abstention_run import (
    RESPONSES_FILENAME,
    RUN_MANIFEST_FILENAME,
    build_broker_provider,
    dry_run,
    git_head,
    plan_trials,
    run_evaluation,
    write_evaluation_gate,
)
from .abstention_score import score_run
from .abstention_set import build_eval_set, load_eval_set
from .model_broker import SharedGeminiBroker
from .util import atomic_json, canonical_json


ACTIONS = (
    "build-set",
    "render",
    "dry-run",
    "run",
    "canary",
    "score",
    "list-models",
    "gate-template",
)
CANARY_CEILING_USD = Decimal("5.00")
CANARY_ARM = "medium"
CANARY_REPEATS = 1
DEFAULT_CREDENTIAL_FILE = Path("/home/ben/.config/arctic-qa/gemini-api-key")


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser(
        "abstention-eval",
        help="Build, dry-run, run and score the abstention evaluation.",
    )
    parser.add_argument("--action", choices=ACTIONS, required=True)
    # Set building.
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--population", choices=("current", "production", "list"), default="current"
    )
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument(
        "--contract-file",
        type=Path,
        default=Path("config/live-dataset-current-contract-v1.json"),
    )
    parser.add_argument(
        "--production-run-prefix", default="arctic-qa-production-campaign-"
    )
    parser.add_argument("--item-ids-file", type=Path)
    # Run identity and plan.
    parser.add_argument("--eval-set-dir", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--models", help="Comma-separated evaluated model ids.")
    parser.add_argument("--model", help="The one model of a canary.")
    parser.add_argument("--arms", default="medium", help="Comma-separated presets.")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--item-limit", type=int)
    parser.add_argument("--output-file", type=Path)
    # Dry run.
    parser.add_argument(
        "--scripted-policy",
        choices=("gold", "abstain", "random", "always_a", "invalid"),
        default="random",
    )
    parser.add_argument("--seed", default="dry-run")
    parser.add_argument("--scripted-overrides-file", type=Path)
    # Scoring.
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=7)
    parser.add_argument("--no-score", action="store_true")
    # Evaluation files.
    parser.add_argument(
        "--evaluation-policy-file",
        type=Path,
        default=Path("config/benchmark-evaluation-policy-v1.json"),
    )
    parser.add_argument(
        "--evaluation-price-config-file",
        type=Path,
        default=Path("config/benchmark-evaluation-prices-v1.json"),
    )
    parser.add_argument("--evaluation-gate-file", type=Path)
    parser.add_argument("--review-record", type=Path)
    parser.add_argument("--code-commit")
    # Shared broker files (same names as `stream`).
    parser.add_argument(
        "--streaming-budget-policy-file",
        type=Path,
        default=Path("config/streaming-dataset-budget-policy-v1.json"),
    )
    parser.add_argument(
        "--price-config-file",
        type=Path,
        default=Path("config/gemini-eligibility-v1.json"),
    )
    parser.add_argument(
        "--execution-gate-file",
        type=Path,
        default=Path("config/streaming-live-execution-gate-v1.json"),
    )
    parser.add_argument("--shared-ledger-file", type=Path)
    parser.add_argument("--model-receipts-dir", type=Path)
    parser.add_argument("--ledger-config-transition-file", type=Path)
    parser.add_argument("--credential-file", type=Path, default=DEFAULT_CREDENTIAL_FILE)
    parser.add_argument(
        "--prior-construction-spend-usd", type=Decimal, default=Decimal("0")
    )
    # Model enumeration.
    parser.add_argument("--models-file", type=Path, help="Saved models.list JSON.")


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _require(args: argparse.Namespace, *names: str) -> None:
    for name in names:
        if getattr(args, name) is None:
            raise ValueError(f"--{name.replace('_', '-')} is required for this action")


def handle(args: argparse.Namespace) -> Any:
    action = args.action
    if action == "build-set":
        _require(args, "state_db", "output_dir")
        item_ids = None
        if args.item_ids_file is not None:
            item_ids = [
                line.strip()
                for line in args.item_ids_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return build_eval_set(
            state_db=args.state_db,
            output_dir=args.output_dir,
            population=args.population,
            k=args.k,
            contract_file=args.contract_file,
            production_run_prefix=args.production_run_prefix,
            item_ids=item_ids,
        )
    if action == "render":
        _require(args, "eval_set_dir", "models")
        manifest, items = load_eval_set(args.eval_set_dir)
        trials = plan_trials(
            manifest,
            items,
            models=_split(args.models),
            arms=_split(args.arms),
            repeats=args.repeats,
        )
        if args.output_file is not None:
            args.output_file.parent.mkdir(parents=True, exist_ok=True)
            args.output_file.write_text(
                "".join(canonical_json(row) + "\n" for row in trials), encoding="utf-8"
            )
        return {
            "eval_set_id": manifest["eval_set_id"],
            "items": len(items),
            "conditions": list(CONDITIONS),
            "trials": len(trials),
            "output_file": str(args.output_file) if args.output_file else None,
            "sample": {
                key: trials[0][key]
                for key in (
                    "trial_id",
                    "condition",
                    "letters",
                    "abstain_letter",
                    "system_text",
                    "user_text",
                )
            }
            if trials
            else None,
        }
    if action == "dry-run":
        _require(args, "eval_set_dir", "run_dir", "run_id", "models")
        overrides = None
        if args.scripted_overrides_file is not None:
            overrides = json.loads(
                args.scripted_overrides_file.read_text(encoding="utf-8")
            )
        summary = dry_run(
            set_dir=args.eval_set_dir,
            output_dir=args.run_dir,
            run_id=args.run_id,
            models=_split(args.models),
            arms=_split(args.arms),
            repeats=args.repeats,
            construction_policy_file=args.streaming_budget_policy_file,
            construction_price_config_file=args.price_config_file,
            construction_gate_file=args.execution_gate_file,
            evaluation_policy_file=args.evaluation_policy_file,
            evaluation_price_config_file=args.evaluation_price_config_file,
            policy=args.scripted_policy,
            seed=args.seed,
            overrides=overrides,
            max_calls=args.max_calls,
            item_limit=args.item_limit,
            code_commit=args.code_commit or git_head(),
        )
        if not args.no_score:
            summary["scores"] = _score(args, args.run_dir)["summary"]
        return summary
    if action in {"run", "canary"}:
        return _run_paid(args, canary=action == "canary")
    if action == "score":
        _require(args, "run_dir")
        return _score(args, args.run_dir)
    if action == "list-models":
        return _list_models(args)
    if action == "gate-template":
        _require(
            args, "eval_set_dir", "run_id", "models", "output_file", "review_record"
        )
        price_config = json.loads(
            args.evaluation_price_config_file.read_text(encoding="utf-8")
        )
        models = _split(args.models)
        arms = _split(args.arms)
        decoding = decoding_record(price_config, models, arms)
        gate = write_evaluation_gate(
            args.output_file,
            set_dir=args.eval_set_dir,
            run_id=args.run_id,
            models=models,
            arms=arms,
            repeats_maximum=args.repeats,
            decoding=decoding,
            evaluation_policy_file=args.evaluation_policy_file,
            evaluation_price_config_file=args.evaluation_price_config_file,
            integrated_code_commit=args.code_commit or git_head() or "unknown",
            review_record=args.review_record,
            review_verdict="pending",
        )
        return {"gate_file": str(args.output_file), "gate": gate}
    raise ValueError(f"unsupported action: {action}")


def _score(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    manifest = json.loads((run_dir / RUN_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    _, items = load_eval_set(Path(manifest["eval_set_dir"]))
    output_dir = args.output_dir or (run_dir / "scores")
    scores = score_run(
        responses_file=run_dir / RESPONSES_FILENAME,
        items=items,
        k=int(manifest["k"]),
        output_dir=output_dir,
        resamples=args.bootstrap,
        seed=args.bootstrap_seed,
        run_manifest=manifest,
    )
    summary = {
        key: {
            "counts": group["counts"],
            "invalid_rate": group["invalid_rate"],
            "metrics": group["metrics"],
        }
        for key, group in scores["groups"].items()
    }
    return {
        "scores_dir": str(output_dir),
        "files": sorted(path.name for path in output_dir.iterdir()),
        "random_baseline": scores["random_baseline"]["metrics"],
        "summary": summary,
    }


def _broker(
    args: argparse.Namespace, *, evaluation_gate_file: Path
) -> SharedGeminiBroker:
    _require(args, "shared_ledger_file", "model_receipts_dir", "credential_file")
    return SharedGeminiBroker(
        policy_file=args.streaming_budget_policy_file.resolve(),
        price_config_file=args.price_config_file.resolve(),
        execution_gate_file=args.execution_gate_file.resolve(),
        ledger_file=args.shared_ledger_file.resolve(),
        receipts_dir=args.model_receipts_dir.resolve(),
        credential_file=args.credential_file.resolve(),
        prior_construction_spend_usd=args.prior_construction_spend_usd,
        config_transition_file=(
            args.ledger_config_transition_file.resolve()
            if args.ledger_config_transition_file
            else None
        ),
        evaluation_policy_file=args.evaluation_policy_file.resolve(),
        evaluation_price_config_file=args.evaluation_price_config_file.resolve(),
        evaluation_gate_file=evaluation_gate_file.resolve(),
    )


def _run_paid(args: argparse.Namespace, *, canary: bool) -> dict[str, Any]:
    _require(args, "eval_set_dir", "run_dir", "run_id", "evaluation_gate_file")
    if canary:
        _require(args, "model")
        models = [args.model]
        arms = [CANARY_ARM]
        repeats = CANARY_REPEATS
        policy = json.loads(args.evaluation_policy_file.read_text(encoding="utf-8"))
        if Decimal(str(policy["evaluation_ceiling_usd"])) > CANARY_CEILING_USD:
            raise ValueError(
                "the canary needs an evaluation policy ceiling of USD 5.00 or less"
            )
        prices = json.loads(
            args.evaluation_price_config_file.read_text(encoding="utf-8")
        )
        entry = prices["models"].get(args.model) or {}
        if entry.get("is_pro") is not True:
            raise ValueError("the canary runs on one Pro variant only")
    else:
        _require(args, "models")
        models = _split(args.models)
        arms = _split(args.arms)
        repeats = args.repeats
    code_commit = args.code_commit or git_head()
    gate = json.loads(args.evaluation_gate_file.read_text(encoding="utf-8"))
    if (
        gate.get("integrated_code_commit")
        and code_commit
        and (gate["integrated_code_commit"] != code_commit)
    ):
        raise ValueError(
            "the evaluation gate binds another code commit than the running code"
        )
    broker = _broker(args, evaluation_gate_file=args.evaluation_gate_file)
    provider, decoding = build_broker_provider(
        broker=broker,
        set_dir=args.eval_set_dir,
        run_id=args.run_id,
        models=models,
        arms=arms,
        repeats=repeats,
    )

    def progress(row: dict[str, Any]) -> None:
        response = row["response"]
        print(
            canonical_json(
                {
                    "trial_id": row["trial_id"],
                    "item_id": row["item_id"],
                    "condition": row["condition"],
                    "model": row["model"],
                    "arm": row["arm"],
                    "state": response["state"],
                    "letter": row["parsed_letter"],
                    "outcome": row["outcome"],
                    "cost_usd": response["cost_usd"],
                    "latency_seconds": response["latency_seconds"],
                    "usage": response["usage"],
                }
            ),
            flush=True,
        )

    summary = run_evaluation(
        set_dir=args.eval_set_dir,
        output_dir=args.run_dir,
        run_id=args.run_id,
        models=models,
        arms=arms,
        repeats=repeats,
        provider=provider,
        decoding=decoding,
        max_calls=args.max_calls,
        code_commit=code_commit,
        item_limit=args.item_limit,
        progress=progress,
    )
    status = broker.status()
    summary["ledger"] = {
        "evaluation": status["evaluation"],
        "usage": status["usage"],
        "remaining": status["remaining"],
    }
    atomic_json(args.run_dir / "run-summary.json", summary)
    if not args.no_score and summary["recorded_trials"]:
        summary["scores"] = _score(args, args.run_dir)["summary"]
    return summary


def _list_models(args: argparse.Namespace) -> dict[str, Any]:
    price_config = json.loads(
        args.evaluation_price_config_file.read_text(encoding="utf-8")
    )
    if args.models_file is not None:
        saved = json.loads(args.models_file.read_text(encoding="utf-8"))
        models = saved.get("models") if isinstance(saved, dict) else saved
        source = str(args.models_file)
    else:
        models = fetch_model_list(price_config["api_base"], args.credential_file)
        source = f"{price_config['api_base']}/models"
    rows = annotate_models(models, price_config)
    result = {
        "source": source,
        "model_count": len(rows),
        "pro_variants": [row["model"] for row in rows if row["is_pro"]],
        "priced_models": [row["model"] for row in rows if row["priced"]],
        "models": rows,
    }
    if args.output_file is not None:
        atomic_json(args.output_file, {"models": models, "annotated": rows})
        result["output_file"] = str(args.output_file)
    return result
