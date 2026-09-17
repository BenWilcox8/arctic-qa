"""Command-line surface of the abstention evaluation harness.

``python -m arctic_qa abstention-eval --action <action> ...``

Actions: ``build-set``, ``render``, ``dry-run``, ``run``, ``canary``, ``score``,
``list-models``, ``gate-template``. ``--provider`` selects Google Gemini (the
default, through the shared paid-call broker) or one of the subscription
providers, Claude Code and Codex. See docs/ABSTENTION_EVALUATION.md.

The plan actions run every vendor of one evaluation plan file together, the
vendors in parallel and N calls in flight per vendor: ``plan-gates`` writes
one gate per vendor, ``dry-run-plan`` runs the whole plan offline,
``run-plan`` runs it live, and ``score-plan`` scores every vendor of one plan
run together.

The streaming actions evaluate every accepted question as it lands:
``watch-authorization`` writes the streaming authorization for a reviewer,
``watch`` runs the evaluator, ``pause-status`` shows which models are held,
and ``cost-summary`` prints the run so far:
the accepted items, the USD per item on generation and on Gemini evaluation,
the subscription tokens per item, the projected cost of N items, and the
per-model abstention metrics.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .abstention_providers import (
    PROVIDER_GOOGLE_GEMINI,
    annotate_models,
    decoding_record,
    fetch_model_list,
)
from .abstention_render import CONDITIONS
from .abstention_cost import DEFAULT_LIST_PRICE_FILE, summarize_journal
from .abstention_plan import (
    DEFAULT_PAUSE_FILE,
    DEFAULT_PLAN_FILE,
    GATE_FILENAME_BY_VENDOR,
    build_vendor_runs,
    dry_run_plan,
    load_pause,
    load_plan,
    merge_pause,
    parse_concurrency,
    parse_pause_models,
    paused_models,
    plan_vendors,
    run_plan,
    score_plan,
    write_plan_gates,
)
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
from .abstention_subscription import (
    DEFAULT_SUBSCRIPTION_MODELS_FILE,
    PROVIDER_OPENAI_CODEX,
    SUBSCRIPTION_PROVIDER_NAMES,
    SubprocessTransport,
    annotate_subscription_models,
    binary_version,
    build_subscription_provider,
    codex_model_catalog,
    load_subscription_models,
    subscription_decoding_record,
    subscription_dry_run,
    subscription_gate_record,
    vendor_entry,
)
from .abstention_watch import (
    DEFAULT_ITEM_WORKERS,
    DEFAULT_POLL_SECONDS,
    authorization_record,
    watch,
)
from .model_broker import SharedGeminiBroker
from .paths import default_credential_file
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
    "plan-gates",
    "dry-run-plan",
    "run-plan",
    "score-plan",
    "watch-authorization",
    "watch",
    "cost-summary",
    "pause-status",
    "apply-evaluation-ceiling",
)
CANARY_CEILING_USD = Decimal("5.00")
CANARY_ARM = "medium"
CANARY_REPEATS = 1
PROVIDER_CHOICES = (PROVIDER_GOOGLE_GEMINI, *SUBSCRIPTION_PROVIDER_NAMES)
DEFAULT_CREDENTIAL_FILE = default_credential_file()


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser(
        "abstention-eval",
        help="Build, dry-run, run and score the abstention evaluation.",
    )
    parser.add_argument("--action", choices=ACTIONS, required=True)
    parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=PROVIDER_GOOGLE_GEMINI,
        help="The evaluation provider of run, dry-run, gate-template and list-models.",
    )
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
    # Subscription providers (Claude Code, Codex).
    parser.add_argument(
        "--subscription-models-file",
        type=Path,
        default=DEFAULT_SUBSCRIPTION_MODELS_FILE,
        help="The registry of subscription models, binaries and presets.",
    )
    parser.add_argument(
        "--subscription-ledger-dir",
        type=Path,
        help="The subscription ledger of the vendor (receipts, codex home, scratch).",
    )
    parser.add_argument(
        "--binary-path", help="Override the harness binary of the vendor."
    )
    parser.add_argument(
        "--scratch-dir",
        type=Path,
        help="The empty directory every harness call runs from (default: <ledger>/scratch).",
    )
    parser.add_argument(
        "--catalog-bundled",
        action="store_true",
        help="list-models: read the model catalog bundled in the codex binary, no refresh.",
    )
    # Concurrent plan actions (every vendor of one plan on each item).
    parser.add_argument(
        "--plan-file",
        type=Path,
        default=DEFAULT_PLAN_FILE,
        help="The evaluation plan: the models of each vendor, the arms, the repeats.",
    )
    parser.add_argument(
        "--plan-gate-dir",
        type=Path,
        help="The directory that holds one reviewed evaluation gate per vendor.",
    )
    parser.add_argument(
        "--vendors",
        help="Comma-separated subset of the plan's vendors (default: every vendor).",
    )
    parser.add_argument(
        "--concurrency",
        help="Override the calls in flight per vendor, as vendor=N,vendor=N.",
    )
    parser.add_argument(
        "--item-workers",
        type=int,
        default=DEFAULT_ITEM_WORKERS,
        help=(
            "watch: the accepted questions scored at once (default "
            f"{DEFAULT_ITEM_WORKERS}). Each question keeps its own evaluation "
            "set, gates, run directory and journal row; the calls in flight "
            "per vendor stay the plan's and the policy's."
        ),
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Run the vendors one after another with one call in flight (the baseline).",
    )
    parser.add_argument(
        "--latency-seconds",
        type=float,
        default=0.0,
        help="dry-run-plan: make every scripted call sleep, to measure the schedule.",
    )
    parser.add_argument(
        "--binary-version",
        action="append",
        default=[],
        metavar="VENDOR=VERSION",
        help="plan-gates: pin the harness version of a subscription vendor.",
    )
    parser.add_argument(
        "--subscription-ledger-root",
        type=Path,
        help="run-plan: the parent of the per-vendor subscription ledger directories.",
    )
    parser.add_argument(
        "--list-price-file",
        type=Path,
        default=DEFAULT_LIST_PRICE_FILE,
        help="Informational list prices of the subscription vendors for the cost journal.",
    )
    # Streaming evaluator and cost journal.
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="The streaming evaluator's directory: sets, runs, gates, cost journal.",
    )
    parser.add_argument(
        "--authorization-file",
        type=Path,
        help="The reviewed streaming authorization that bounds the derived gates.",
    )
    parser.add_argument(
        "--campaign-id",
        help="The production campaign whose accepted items the evaluator watches.",
    )
    parser.add_argument(
        "--ledger-run-prefix",
        action="append",
        default=[],
        help="A construction ledger run id prefix of the campaign (repeatable).",
    )
    parser.add_argument(
        "--run-id-prefix",
        help="The evaluation run id prefix; each item runs as <prefix>-<item id>.",
    )
    parser.add_argument(
        "--maximum-items",
        type=int,
        help="Stop taking new items after this many (the authorization also bounds it).",
    )
    parser.add_argument(
        "--maximum-gemini-usd",
        default="1.00",
        help="watch-authorization: the Gemini USD bound of the streaming run.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_POLL_SECONDS,
        help="How long the evaluator waits when no new item exists (5 to 600).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="watch: run one pass over the pending items and return.",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        help="watch: return after this many seconds of waiting (for a bounded test).",
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        help=(
            "watch: the supervisor status file. The watcher appends one "
            "'blocked:' line to it when a bound ends the run, because a bound "
            "is not an error and the exit code alone says nothing."
        ),
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="watch: also evaluate the items of --backfill-contract-file (off by default).",
    )
    parser.add_argument(
        "--backfill-contract-file",
        type=Path,
        default=Path("config/abstention-eval-chapter2-contract-v1.json"),
        help="The population contract of the backfill items (chapter 2).",
    )
    parser.add_argument(
        "--project-items",
        type=int,
        help="cost-summary: project the run's cost onto this many accepted items.",
    )
    parser.add_argument(
        "--evaluation-policy-transition-file",
        type=Path,
        help=(
            "The reviewed transition that raises the evaluation ceiling. It is "
            "needed only for the first start under the larger ceiling; later "
            "starts read the immutable event."
        ),
    )
    parser.add_argument(
        "--pause-file",
        type=Path,
        action="append",
        default=[],
        help=(
            "A paused-model file (repeatable). The evaluator re-reads every one "
            "before every item, so a cost guard can pause or resume a model "
            "while it runs. A later file wins for the same model. Without this "
            f"option the evaluator reads {DEFAULT_PAUSE_FILE}."
        ),
    )
    parser.add_argument(
        "--no-pause-file",
        action="store_true",
        help="Ignore the paused-model file (the command line still pauses).",
    )
    parser.add_argument(
        "--pause-model",
        action="append",
        default=[],
        metavar="MODEL[=RESUME_UTC]",
        help=(
            "Pause one model for this invocation, with an optional resume time, "
            "for example claude-fable-5-1=2026-09-16T23:00:00Z (repeatable)."
        ),
    )


def _pairs(values: list[str]) -> dict[str, str]:
    """Parse repeated ``name=value`` options."""
    result: dict[str, str] = {}
    for item in values or []:
        name, _, value = str(item).partition("=")
        if not name or not value:
            raise ValueError(f"expected name=value, received: {item}")
        result[name] = value
    return result


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
        if args.provider != PROVIDER_GOOGLE_GEMINI:
            summary = subscription_dry_run(
                vendor=args.provider,
                set_dir=args.eval_set_dir,
                output_dir=args.run_dir,
                run_id=args.run_id,
                models=_split(args.models),
                arms=_split(args.arms),
                repeats=args.repeats,
                evaluation_policy_file=args.evaluation_policy_file,
                subscription_models_file=args.subscription_models_file,
                policy=args.scripted_policy,
                seed=args.seed,
                overrides=overrides,
                max_calls=args.max_calls,
                item_limit=args.item_limit,
                code_commit=args.code_commit or git_head(),
                binary=args.binary_path,
            )
            if not args.no_score:
                summary["scores"] = _score(args, args.run_dir)["summary"]
            return summary
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
    if action == "run" and args.provider != PROVIDER_GOOGLE_GEMINI:
        return _run_subscription(args)
    if action in {"run", "canary"}:
        if args.provider != PROVIDER_GOOGLE_GEMINI:
            raise ValueError("the canary runs on Google Gemini only")
        return _run_paid(args, canary=action == "canary")
    if action == "score":
        _require(args, "run_dir")
        return _score(args, args.run_dir)
    if action == "list-models":
        return _list_models(args)
    if action == "watch-authorization":
        return _watch_authorization(args)
    if action == "watch":
        return _watch(args)
    if action == "cost-summary":
        _require(args, "work_dir")
        return summarize_journal(args.work_dir, project_items=args.project_items)
    if action == "pause-status":
        return _pause_status(args)
    if action == "apply-evaluation-ceiling":
        return _apply_evaluation_ceiling(args)
    if action == "plan-gates":
        return _plan_gates(args)
    if action == "dry-run-plan":
        return _dry_run_plan(args)
    if action == "run-plan":
        return _run_plan(args)
    if action == "score-plan":
        _require(args, "run_dir")
        scores = score_plan(
            args.run_dir,
            output_dir=args.output_dir,
            resamples=args.bootstrap,
            seed=args.bootstrap_seed,
        )
        return {
            "scores_dir": str(args.output_dir or (args.run_dir / "scores")),
            "random_baseline": scores["random_baseline"]["metrics"],
            "summary": {
                key: {
                    "counts": group["counts"],
                    "invalid_rate": group["invalid_rate"],
                    "metrics": group["metrics"],
                }
                for key, group in scores["groups"].items()
            },
        }
    if action == "gate-template" and args.provider != PROVIDER_GOOGLE_GEMINI:
        return _subscription_gate_template(args)
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


def _watch_authorization(args: argparse.Namespace) -> dict[str, Any]:
    _require(
        args, "state_db", "campaign_id", "run_id_prefix", "review_record", "output_file"
    )
    record = authorization_record(
        contract_file=args.contract_file,
        plan_file=args.plan_file,
        evaluation_policy_file=args.evaluation_policy_file,
        evaluation_price_config_file=args.evaluation_price_config_file,
        subscription_models_file=args.subscription_models_file,
        state_db=args.state_db,
        campaign_id=args.campaign_id,
        run_id_prefix=args.run_id_prefix,
        maximum_items=args.maximum_items or 1,
        maximum_gemini_usd=args.maximum_gemini_usd,
        integrated_code_commit=args.code_commit or git_head() or "unknown",
        review_record=args.review_record,
    )
    atomic_json(args.output_file, record)
    return {
        "authorization_file": str(args.output_file),
        "authorization": record,
        "next_step": (
            "An independent reviewer sets independent_review_verdict to pass and "
            "authorization_enabled to true."
        ),
    }


def _watch(args: argparse.Namespace) -> dict[str, Any]:
    _require(args, "authorization_file", "state_db", "work_dir", "shared_ledger_file")
    plan = load_plan(args.plan_file)
    chosen = _split(args.vendors) or plan_vendors(plan)
    needs_broker = PROVIDER_GOOGLE_GEMINI in chosen
    return watch(
        authorization_file=args.authorization_file,
        plan_file=args.plan_file,
        contract_file=args.contract_file,
        evaluation_policy_file=args.evaluation_policy_file,
        evaluation_price_config_file=args.evaluation_price_config_file,
        subscription_models_file=args.subscription_models_file,
        state_db=args.state_db,
        work_dir=args.work_dir,
        shared_ledger_file=args.shared_ledger_file,
        broker_factory=(
            (
                lambda gate: _broker(
                    args, evaluation_gate_file=gate, deferred_snapshot=True
                )
            )
            if needs_broker
            else None
        ),
        subscription_ledger_root=(
            args.subscription_ledger_root.resolve()
            if args.subscription_ledger_root
            else None
        ),
        list_price_file=args.list_price_file,
        poll_seconds=args.poll_seconds,
        maximum_items=args.maximum_items,
        once=args.once,
        backfill=args.backfill,
        backfill_contract_file=args.backfill_contract_file,
        item_workers=args.item_workers,
        concurrency=parse_concurrency(args.concurrency),
        vendors=_split(args.vendors) or None,
        scratch_root=args.scratch_dir.resolve() if args.scratch_dir else None,
        code_commit=args.code_commit or git_head(),
        ledger_run_prefixes=tuple(args.ledger_run_prefix),
        pause_files=_pause_files(args),
        pause_models=parse_pause_models(args.pause_model),
        progress=_progress,
        log=_watch_log,
        deadline_seconds=args.deadline_seconds,
        status_file=args.status_file,
    )


def _apply_evaluation_ceiling(args: argparse.Namespace) -> dict[str, Any]:
    """Apply one reviewed evaluation-ceiling transition, and call nothing.

    The streaming evaluator derives one gate per item, so it cannot be the
    first start under a larger ceiling: the transition binds one gate hash.
    This action is that first start. It constructs the broker with the
    transition file and a gate of its own, which applies the transition and
    writes its immutable event. Every later start, the evaluator included,
    reads that event. The action makes no paid call.
    """
    # The transition file is needed only for the first start under a larger
    # ceiling. Once the event exists, this action reads the authorized ceiling
    # back, which is how an operator proves the transition is in force.
    _require(args, "evaluation_gate_file")
    broker = _broker(args, evaluation_gate_file=args.evaluation_gate_file)
    status = broker.status()
    return {
        "schema": "abstention-eval-ceiling-transition-result-v1",
        "authorized_ceiling_usd": str(broker.authorized_evaluation_ceiling_usd()),
        "active_policy_file": str(args.evaluation_policy_file),
        "transition_event_sha256": broker.evaluation_transition_sha256,
        "evaluation": status["evaluation"],
        "ledger": {
            "integrity_valid": status["integrity_valid"],
            "halted": status["halted"],
            "inflight": status["usage"]["concurrent_generation_requests"],
        },
    }


def _pause_status(args: argparse.Namespace) -> dict[str, Any]:
    """Show which models the evaluator holds now, and which the file names.

    This is the read side of the pause interface. A cost guard writes the
    ``benchmark-evaluation-model-pause-v1`` file and reads this action back to
    prove the pause is in force. The evaluator re-reads the same file before
    every item, so it needs no restart.
    """
    paths = _pause_files(args)
    record = _pause_record(args)
    return {
        "schema": "abstention-eval-pause-status-v1",
        "pause_file": str(paths[0]) if paths else None,
        "pause_files": [str(path) for path in paths],
        "paused_now": sorted(paused_models(record)),
        "entries": record["paused_models"],
    }


def _pause_files(args: argparse.Namespace) -> list[Path]:
    """The paused-model files of this invocation, in the order they are read.

    A cost guard owns its own file, and the committed file holds the standing
    orders of the captain. So the evaluator reads a list, and a later file
    wins for the same model. Without the option it reads the committed file.
    """
    if args.no_pause_file:
        return []
    return list(args.pause_file) or [DEFAULT_PAUSE_FILE]


def _pause_record(args: argparse.Namespace) -> dict[str, Any]:
    """Merge the paused-model file with the repeated ``--pause-model`` options."""
    return merge_pause(
        *(load_pause(path) for path in _pause_files(args)),
        parse_pause_models(args.pause_model),
    )


def _watch_log(event: dict[str, Any]) -> None:
    print(canonical_json(event), flush=True)


def _plan_gates(args: argparse.Namespace) -> dict[str, Any]:
    _require(args, "eval_set_dir", "run_id", "review_record", "output_dir")
    plan = load_plan(args.plan_file)
    paths = write_plan_gates(
        plan=plan,
        set_dir=args.eval_set_dir,
        run_id=args.run_id,
        output_dir=args.output_dir,
        review_record=args.review_record,
        review_verdict="pending",
        evaluation_policy_file=args.evaluation_policy_file,
        evaluation_price_config_file=args.evaluation_price_config_file,
        subscription_models_file=args.subscription_models_file,
        integrated_code_commit=args.code_commit or git_head() or "unknown",
        binary_versions=_pairs(args.binary_version),
        vendors=_split(args.vendors) or None,
    )
    return {
        "plan_id": plan["plan_id"],
        "gate_dir": str(args.output_dir),
        "trials_per_item": plan["trials_per_item"],
        "gates": {
            vendor: {
                "file": str(path),
                "gate": json.loads(path.read_text(encoding="utf-8")),
            }
            for vendor, path in paths.items()
        },
        "next_step": (
            "An independent reviewer sets independent_review_verdict to pass and "
            "evaluation_enabled to true in every gate file."
        ),
    }


def _dry_run_plan(args: argparse.Namespace) -> dict[str, Any]:
    _require(args, "eval_set_dir", "run_dir", "run_id")
    overrides = None
    if args.scripted_overrides_file is not None:
        overrides = json.loads(args.scripted_overrides_file.read_text(encoding="utf-8"))
    summary = dry_run_plan(
        plan=load_plan(args.plan_file),
        set_dir=args.eval_set_dir,
        output_dir=args.run_dir,
        run_id=args.run_id,
        construction_policy_file=args.streaming_budget_policy_file,
        construction_price_config_file=args.price_config_file,
        construction_gate_file=args.execution_gate_file,
        evaluation_policy_file=args.evaluation_policy_file,
        evaluation_price_config_file=args.evaluation_price_config_file,
        subscription_models_file=args.subscription_models_file,
        policy=args.scripted_policy,
        seed=args.seed,
        overrides=overrides,
        latency_seconds=args.latency_seconds,
        serial=args.serial,
        item_limit=args.item_limit,
        max_calls=args.max_calls,
        code_commit=args.code_commit or git_head(),
        concurrency=parse_concurrency(args.concurrency),
        vendors=_split(args.vendors) or None,
        # A dry run makes no call and uses no quota, so the standing pause
        # file does not apply to it. Only an explicit --pause-model does,
        # which is how the held-trial accounting is exercised offline.
        pause=parse_pause_models(args.pause_model),
    )
    if not args.no_score and summary["recorded_trials"]:
        summary["scores"] = _score_plan_summary(args)
    return summary


def _score_plan_summary(args: argparse.Namespace) -> dict[str, Any]:
    scores = score_plan(
        args.run_dir,
        output_dir=args.output_dir,
        resamples=args.bootstrap,
        seed=args.bootstrap_seed,
    )
    return {
        key: {
            "counts": group["counts"],
            "invalid_rate": group["invalid_rate"],
            "metrics": group["metrics"],
        }
        for key, group in scores["groups"].items()
    }


def _run_plan(args: argparse.Namespace) -> dict[str, Any]:
    _require(args, "eval_set_dir", "run_dir", "run_id", "plan_gate_dir")
    plan = load_plan(args.plan_file)
    vendors = _split(args.vendors) or plan_vendors(plan)
    code_commit = args.code_commit or git_head()
    for vendor in vendors:
        gate_file = args.plan_gate_dir / GATE_FILENAME_BY_VENDOR[vendor]
        gate = json.loads(gate_file.read_text(encoding="utf-8"))
        if (
            gate.get("integrated_code_commit")
            and code_commit
            and gate["integrated_code_commit"] != code_commit
        ):
            raise ValueError(
                "an evaluation gate binds another code commit than the running code"
            )
    needs_broker = PROVIDER_GOOGLE_GEMINI in vendors
    vendor_runs = build_vendor_runs(
        plan=plan,
        set_dir=args.eval_set_dir,
        run_id=args.run_id,
        gate_dir=args.plan_gate_dir,
        evaluation_policy_file=args.evaluation_policy_file,
        subscription_models_file=args.subscription_models_file.resolve(),
        broker_factory=(
            (
                lambda gate: _broker(
                    args, evaluation_gate_file=gate, deferred_snapshot=True
                )
            )
            if needs_broker
            else None
        ),
        subscription_ledger_root=(
            args.subscription_ledger_root.resolve()
            if args.subscription_ledger_root
            else None
        ),
        concurrency=parse_concurrency(args.concurrency),
        vendors=vendors,
        scratch_root=args.scratch_dir.resolve() if args.scratch_dir else None,
    )
    summary = run_plan(
        set_dir=args.eval_set_dir,
        output_dir=args.run_dir,
        run_id=args.run_id,
        plan=plan,
        vendor_runs=vendor_runs,
        item_limit=args.item_limit,
        max_calls=args.max_calls,
        code_commit=code_commit,
        progress=_progress,
        serial=args.serial,
        gate_dir=args.plan_gate_dir,
        pause=_pause_record(args),
    )
    gemini = vendor_runs.get(PROVIDER_GOOGLE_GEMINI)
    if gemini is not None:
        status = gemini.provider.broker.status()
        summary["ledger"] = {
            "evaluation": status["evaluation"],
            "usage": status["usage"],
            "remaining": status["remaining"],
        }
    atomic_json(args.run_dir / "plan-summary.json", summary)
    if not args.no_score and summary["recorded_trials"]:
        summary["scores"] = _score_plan_summary(args)
    return summary


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
    args: argparse.Namespace,
    *,
    evaluation_gate_file: Path,
    deferred_snapshot: bool = False,
) -> SharedGeminiBroker:
    """Build the evaluation broker.

    ``deferred_snapshot`` is for the concurrent evaluator. The compacted
    snapshot and the status file are then written by the compactor thread
    instead of inside every commit, which would hold the shared ledger lock
    for the length of a durable 8 MB write and starve the producer that waits
    for it. Read "Parallel bookkeeping" in ``docs/SHARED_MODEL_BROKER.md``.
    """
    _require(args, "shared_ledger_file", "model_receipts_dir", "credential_file")
    broker = SharedGeminiBroker(
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
        evaluation_policy_transition_file=(
            args.evaluation_policy_transition_file.resolve()
            if args.evaluation_policy_transition_file
            else None
        ),
    )
    broker.deferred_snapshot = bool(deferred_snapshot)
    return broker


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
        progress=_progress,
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


def _subscription_gate_template(args: argparse.Namespace) -> dict[str, Any]:
    _require(args, "eval_set_dir", "run_id", "models", "output_file", "review_record")
    config = load_subscription_models(args.subscription_models_file)
    models = _split(args.models)
    arms = _split(args.arms)
    binary = args.binary_path or str(vendor_entry(config, args.provider)["binary"])
    version = binary_version(
        args.provider, binary, SubprocessTransport(), cwd=Path.cwd()
    )
    decoding = subscription_decoding_record(
        config, args.provider, models, arms, binary=binary, binary_version=version
    )
    gate = subscription_gate_record(
        vendor=args.provider,
        set_dir=args.eval_set_dir,
        run_id=args.run_id,
        models=models,
        arms=arms,
        repeats_maximum=args.repeats,
        decoding=decoding,
        evaluation_policy_file=args.evaluation_policy_file,
        subscription_models_file=args.subscription_models_file,
        integrated_code_commit=args.code_commit or git_head() or "unknown",
        review_record=args.review_record,
        review_verdict="pending",
    )
    atomic_json(args.output_file, gate)
    return {"gate_file": str(args.output_file), "gate": gate}


def _progress(row: dict[str, Any]) -> None:
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


def _run_subscription(args: argparse.Namespace) -> dict[str, Any]:
    _require(
        args,
        "eval_set_dir",
        "run_dir",
        "run_id",
        "models",
        "evaluation_gate_file",
        "subscription_ledger_dir",
    )
    models = _split(args.models)
    arms = _split(args.arms)
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
    provider, decoding = build_subscription_provider(
        vendor=args.provider,
        set_dir=args.eval_set_dir,
        run_id=args.run_id,
        models=models,
        arms=arms,
        repeats=args.repeats,
        ledger_dir=args.subscription_ledger_dir.resolve(),
        evaluation_policy_file=args.evaluation_policy_file.resolve(),
        evaluation_gate_file=args.evaluation_gate_file.resolve(),
        subscription_models_file=args.subscription_models_file.resolve(),
        scratch_root=args.scratch_dir.resolve() if args.scratch_dir else None,
        binary=args.binary_path,
    )
    summary = run_evaluation(
        set_dir=args.eval_set_dir,
        output_dir=args.run_dir,
        run_id=args.run_id,
        models=models,
        arms=arms,
        repeats=args.repeats,
        provider=provider,
        decoding=decoding,
        max_calls=args.max_calls,
        code_commit=code_commit,
        item_limit=args.item_limit,
        progress=_progress,
    )
    summary["ledger"] = provider.ledger.status()
    atomic_json(args.run_dir / "run-summary.json", summary)
    if not args.no_score and summary["recorded_trials"]:
        summary["scores"] = _score(args, args.run_dir)["summary"]
    return summary


def _list_subscription_models(args: argparse.Namespace) -> dict[str, Any]:
    config = load_subscription_models(args.subscription_models_file)
    entry = vendor_entry(config, args.provider)
    binary = args.binary_path or str(entry["binary"])
    version = binary_version(
        args.provider, binary, SubprocessTransport(), cwd=Path.cwd()
    )
    catalog = None
    source = f"registry {args.subscription_models_file}"
    if args.models_file is not None:
        saved = json.loads(args.models_file.read_text(encoding="utf-8"))
        catalog = saved.get("models") if isinstance(saved, dict) else saved
        source = str(args.models_file)
    elif args.provider == PROVIDER_OPENAI_CODEX:
        catalog = codex_model_catalog(binary, bundled=args.catalog_bundled)
        source = f"{binary} debug models"
    rows = annotate_subscription_models(config, args.provider, catalog)
    result = {
        "provider": args.provider,
        "billing": "subscription",
        "binary": binary,
        "binary_version": version,
        "source": source,
        "model_count": len(rows),
        "registered_models": [row["model"] for row in rows if row["registered"]],
        "models": rows,
    }
    if args.output_file is not None:
        atomic_json(args.output_file, {"models": catalog, "annotated": rows})
        result["output_file"] = str(args.output_file)
    return result


def _list_models(args: argparse.Namespace) -> dict[str, Any]:
    if args.provider != PROVIDER_GOOGLE_GEMINI:
        return _list_subscription_models(args)
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
