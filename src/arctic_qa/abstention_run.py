"""Drive one abstention evaluation run: one isolated provider call per trial.

A trial is one (item, condition, model, thinking arm, repeat). The runner
renders it, sends it through the provider, parses the single letter, maps it
onto N0 to N5, and appends one row to ``responses.jsonl``. It resumes from
that file and from the broker receipts, never replays a completed call, and
stops on the first budget stop or ambiguous charge.

The dry run builds a private ledger in its output directory and answers every
call from a scripted transport, so the whole path, gate, binding, ledger
phase, receipts and scoring, runs with no paid call.
"""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .abstention_providers import (
    COMPLETED,
    EvaluationProvider,
    EvaluationRequest,
    GeminiBrokerEvaluationProvider,
    ScriptedTransport,
    decoding_record,
    evaluation_payload,
    run_binding_record,
    scripted_key,
    scripted_letter,
)
from .abstention_render import (
    ABSTENTION_OPTION_TEXT,
    CONDITIONS,
    N0,
    PROMPT_VERSION,
    classify,
    parse_letter,
    prompt_sha256,
    render_trial,
)
from .abstention_set import (
    evaluation_identity,
    load_eval_set,
    manifest_sha256,
    require_current_prompt_contract,
)
from .model_broker import EVALUATION_PHASE, SharedGeminiBroker
from .util import atomic_json, canonical_json, sha256_bytes, sha256_file


RUN_MANIFEST_SCHEMA = "abstention-eval-run-v1"
RESPONSES_FILENAME = "responses.jsonl"
RUN_MANIFEST_FILENAME = "run-manifest.json"
RUN_SUMMARY_FILENAME = "run-summary.json"
TRIALS_FILENAME = "trials.jsonl"
INVALID_FINISH_REASON = "finish_reason_not_stop"
DRY_RUN_REQUESTS_PER_MINUTE = 100_000


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def git_head(repo: Path | None = None) -> str | None:
    """Return the short commit of the running code, when it is resolvable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo or Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def plan_trials(
    manifest: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    models: list[str],
    arms: list[str],
    repeats: int,
    conditions: tuple[str, ...] = CONDITIONS,
) -> list[dict[str, Any]]:
    """Render every trial of one run in a fixed order."""
    if not models or not arms:
        raise ValueError("a run needs at least one model and one thinking arm")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    trials = []
    for item in items:
        for condition in conditions:
            for repeat in range(1, repeats + 1):
                for model in models:
                    for arm in arms:
                        trials.append(
                            render_trial(
                                item,
                                eval_set_id=manifest["eval_set_id"],
                                condition=condition,
                                repeat=repeat,
                                model=model,
                                arm=arm,
                                k=int(manifest["k"]),
                            )
                        )
    return trials


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def response_row(
    trial: dict[str, Any],
    response: Any,
    *,
    enum_output: bool,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """Build one responses.jsonl row: the trial, the raw response, the outcome.

    ``code_commit`` is the commit that ran this one call. An item can be
    finished by a later pass on a later snapshot, so the commit belongs to the
    trial and not to the run directory alone.
    """
    record = response.as_dict()
    if record["state"] == COMPLETED:
        parsed = parse_letter(record["raw_text"], trial["letters"])
        if parsed["valid"] and record["finish_reason"] not in (None, "STOP"):
            parsed = {
                "letter": None,
                "valid": False,
                "reason": INVALID_FINISH_REASON,
            }
        outcome = classify(trial, parsed["letter"])
        invalid_reason = parsed["reason"]
    else:
        parsed = {"letter": None, "valid": False, "reason": record["state"]}
        outcome = N0
        invalid_reason = record["state"]
    return {
        "trial_id": trial["trial_id"],
        "eval_set_id": trial["eval_set_id"],
        "item_id": trial["item_id"],
        "condition": trial["condition"],
        "repeat": trial["repeat"],
        "model": trial["model"],
        "arm": trial["arm"],
        "k": trial["k"],
        "letters": trial["letters"],
        "options": [
            {"letter": row["letter"], "kind": row["kind"], "text": row["text"]}
            for row in trial["options"]
        ],
        "gold_letter": trial["gold_letter"],
        "abstain_letter": trial["abstain_letter"],
        "correct_letter": trial["correct_letter"],
        "dropped_distractor_text": trial["dropped_distractor_text"],
        "shuffle_seed": trial["shuffle_seed"],
        "stimulus_sha256": trial["stimulus_sha256"],
        "prompt_version": trial["prompt_version"],
        "code_commit": code_commit,
        "enum_output": enum_output,
        "response": record,
        "parsed_letter": parsed["letter"],
        "valid": parsed["valid"],
        "invalid_reason": invalid_reason,
        "outcome": outcome,
        "recorded_at_utc": _utc_now(),
    }


def summarize_run(
    rows: list[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Aggregate cost, tokens, latency and the invalid rate per model and arm."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["model"], row["arm"]), []).append(row)
    per_model = []
    total_cost = Decimal("0")
    for (model, arm), group in sorted(groups.items()):
        completed = [row for row in group if row["response"]["state"] == COMPLETED]
        costs = [
            Decimal(row["response"]["cost_usd"])
            for row in completed
            if row["response"]["cost_usd"] is not None
        ]
        latencies = [
            row["response"]["latency_seconds"]
            for row in completed
            if row["response"]["latency_seconds"] is not None
            and not row["response"]["resumed"]
        ]
        usage_keys = ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount")
        tokens = {
            key: sum(
                int((row["response"]["usage"] or {}).get(key, 0)) for row in completed
            )
            for key in usage_keys
        }
        invalid = [row for row in group if not row["valid"]]
        reasons: dict[str, int] = {}
        for row in invalid:
            reasons[str(row["invalid_reason"])] = (
                reasons.get(str(row["invalid_reason"]), 0) + 1
            )
        cost_sum = sum(costs, Decimal("0"))
        total_cost += cost_sum
        per_model.append(
            {
                "model": model,
                "arm": arm,
                "trials": len(group),
                "completed_calls": len(completed),
                "cost_usd": str(cost_sum),
                "cost_per_call_usd": (
                    str((cost_sum / len(costs)).quantize(Decimal("0.000001")))
                    if costs
                    else None
                ),
                "tokens": tokens,
                "tokens_per_call": {
                    key: (round(value / len(completed), 1) if completed else None)
                    for key, value in tokens.items()
                },
                "latency_seconds": {
                    "count": len(latencies),
                    "mean": round(statistics.fmean(latencies), 3)
                    if latencies
                    else None,
                    "median": round(statistics.median(latencies), 3)
                    if latencies
                    else None,
                    "max": round(max(latencies), 3) if latencies else None,
                },
                "invalid_count": len(invalid),
                "invalid_rate": (
                    round(len(invalid) / len(group), 4) if group else None
                ),
                "invalid_reasons": reasons,
                "outcomes": {
                    name: sum(1 for row in group if row["outcome"] == name)
                    for name in ("N0", "N1", "N2", "N3", "N4", "N5")
                },
            }
        )
    stopped = [row for row in rows if row["response"]["state"] != COMPLETED]
    return {
        "schema": "abstention-eval-run-summary-v1",
        "run_id": manifest["run_id"],
        "eval_set_id": manifest["eval_set_id"],
        "planned_trials": manifest["planned_trials"],
        "recorded_trials": len(rows),
        "complete": len(rows) == manifest["planned_trials"] and not stopped,
        "stopped_on": (
            {
                "trial_id": stopped[0]["trial_id"],
                "state": stopped[0]["response"]["state"],
                "error": stopped[0]["response"]["error"],
            }
            if stopped
            else None
        ),
        "total_cost_usd": str(total_cost),
        "per_model_arm": per_model,
        "updated_at_utc": _utc_now(),
    }


def without_harness_version(manifest: dict[str, Any]) -> dict[str, Any]:
    """The run manifest with the harness version of the pass taken out.

    The harness version is a record of the pass that ran, exactly as
    ``code_commit`` is. The `claude` and `codex` binaries are upgraded on this
    machine while a run is open, and every response row records the version
    that answered it, so the evidence of each call is complete without the
    manifest binding it.

    Binding it into the identity of the run directory made an upgrade final.
    A question with trials left could never be finished afterwards, because
    :func:`prepare_run` refused its own directory with "the run directory
    holds a different run manifest". Eight questions of the `streaming-r11`
    work directory met that on 2026-09-17, when the Claude Code binary went
    from 2.1.273 to 2.1.274, and the refusal ended the unit at 16:04:18 UTC.
    """
    decoding = dict(manifest.get("decoding") or {})
    decoding.pop("harness_version", None)
    return {**manifest, "decoding": decoding}


def pass_manifest_name(output_dir: Path, run_manifest: dict[str, Any]) -> str:
    """The file name of this pass's own manifest record.

    A pass is named by its code commit. Two passes of one commit can still
    differ, because the harness binary is upgraded between them, so a name
    that another record already holds takes a short digest as well.
    """
    commit = str(run_manifest.get("code_commit") or "unknown")
    name = f"run-manifest-{commit}.json"
    path = output_dir / name
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if (existing.get("decoding") or {}).get("harness_version") != (
            run_manifest.get("decoding") or {}
        ).get("harness_version"):
            digest = sha256_bytes(canonical_json(run_manifest).encode())[:12]
            return f"run-manifest-{commit}-{digest}.json"
    return name


def prepare_run(
    *,
    set_dir: Path,
    output_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats: int,
    provider: EvaluationProvider,
    decoding: dict[str, Any],
    code_commit: str | None = None,
    item_limit: int | None = None,
) -> dict[str, Any]:
    """Write or verify the run manifest and trials; load the recorded rows.

    Returns the run manifest, the items, the planned trials and the rows of
    ``responses.jsonl`` so far. A run directory that holds a different
    manifest is refused: one directory is one plan.
    """
    manifest, items = load_eval_set(set_dir)
    require_current_prompt_contract(manifest)
    if item_limit is not None:
        items = items[: int(item_limit)]
    trials = plan_trials(manifest, items, models=models, arms=arms, repeats=repeats)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_manifest = {
        "schema": RUN_MANIFEST_SCHEMA,
        "run_id": run_id,
        "provider": provider.name,
        "phase": EVALUATION_PHASE,
        "eval_set_id": manifest["eval_set_id"],
        "eval_set_dir": str(set_dir.resolve()),
        "eval_set_manifest_sha256": manifest_sha256(set_dir),
        "items_sha256": manifest["items_sha256"],
        "item_count": len(items),
        "k": manifest["k"],
        "conditions": list(CONDITIONS),
        "models": sorted(models),
        "arms": sorted(arms),
        "repeats": repeats,
        "planned_trials": len(trials),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "abstention_option_text": ABSTENTION_OPTION_TEXT,
        "decoding": decoding,
        "decoding_by_model_arm": {
            f"{model}/{arm}": provider.decoding(model, arm)
            for model in sorted(models)
            for arm in sorted(arms)
        },
        "isolation": "one provider call per trial, its own session, no history",
        "re_ask": False,
        "code_commit": code_commit,
        "trials_sha256": sha256_bytes(
            "".join(canonical_json(row) + "\n" for row in trials).encode()
        ),
    }
    manifest_path = output_dir / RUN_MANIFEST_FILENAME
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        stable = {
            key: value
            for key, value in existing.items()
            if key not in ("created_at_utc", "code_commit")
        }
        current = {
            key: value for key, value in run_manifest.items() if key != "code_commit"
        }
        if without_harness_version(stable) != without_harness_version(current):
            raise ValueError("the run directory holds a different run manifest")
        # The first manifest stays exactly as it was written. A later pass on
        # another snapshot, or on an upgraded harness binary, records its own
        # beside it, so an item can be finished after a cutover and the record
        # of each pass is its own.
        if existing.get("code_commit") != run_manifest["code_commit"] or (
            existing.get("decoding") or {}
        ).get("harness_version") != (run_manifest.get("decoding") or {}).get(
            "harness_version"
        ):
            later = output_dir / pass_manifest_name(output_dir, run_manifest)
            if not later.is_file():
                atomic_json(
                    later,
                    {
                        **run_manifest,
                        "created_at_utc": _utc_now(),
                        "continues_run_manifest_sha256": sha256_file(manifest_path),
                    },
                    immutable=True,
                )
    else:
        atomic_json(
            manifest_path,
            {**run_manifest, "created_at_utc": _utc_now()},
            immutable=True,
        )
    trials_path = output_dir / TRIALS_FILENAME
    if not trials_path.is_file():
        trials_path.write_text(
            "".join(canonical_json(row) + "\n" for row in trials), encoding="utf-8"
        )
    responses_path = output_dir / RESPONSES_FILENAME
    return {
        "run_manifest": run_manifest,
        "items": items,
        "trials": trials,
        "rows": _load_rows(responses_path),
        "responses_path": responses_path,
    }


def run_evaluation(
    *,
    set_dir: Path,
    output_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats: int,
    provider: EvaluationProvider,
    decoding: dict[str, Any],
    max_calls: int | None = None,
    code_commit: str | None = None,
    item_limit: int | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run or resume one evaluation, one call at a time, and return its summary."""
    prepared = prepare_run(
        set_dir=set_dir,
        output_dir=output_dir,
        run_id=run_id,
        models=models,
        arms=arms,
        repeats=repeats,
        provider=provider,
        decoding=decoding,
        code_commit=code_commit,
        item_limit=item_limit,
    )
    run_manifest = prepared["run_manifest"]
    items = prepared["items"]
    trials = prepared["trials"]
    rows = prepared["rows"]
    responses_path = prepared["responses_path"]
    done = {row["trial_id"] for row in rows}
    calls = 0
    with responses_path.open("a", encoding="utf-8") as handle:
        for trial in trials:
            if trial["trial_id"] in done:
                continue
            if max_calls is not None and calls >= max_calls:
                break
            item = next(row for row in items if row["item_id"] == trial["item_id"])
            request = EvaluationRequest(
                trial=trial, identity=evaluation_identity(item), run_id=run_id
            )
            response = provider.answer(request)
            row = response_row(
                trial,
                response,
                enum_output=provider.supports_enum_output(trial["model"]),
                code_commit=code_commit,
            )
            handle.write(canonical_json(row) + "\n")
            handle.flush()
            rows.append(row)
            done.add(trial["trial_id"])
            if not response.resumed:
                calls += 1
            if progress is not None:
                progress(row)
            if response.state != COMPLETED:
                # Fail closed: a budget stop, count error or ambiguous charge
                # ends the run. Nothing is retried and nothing is re-asked.
                break
    summary = summarize_run(rows, {**run_manifest, "planned_trials": len(trials)})
    summary["calls_this_invocation"] = calls
    atomic_json(output_dir / RUN_SUMMARY_FILENAME, summary)
    return summary


# --- Dry run through the broker with a scripted transport --------------------


def _private_credential(directory: Path) -> Path:
    private = directory / "private"
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    private.chmod(0o700)
    credential = private / "unused.key"
    if not credential.is_file():
        credential.write_text("dry-run-no-credential\n", encoding="utf-8")
    credential.chmod(0o600)
    return credential


def write_evaluation_gate(
    path: Path,
    *,
    set_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats_maximum: int,
    decoding: dict[str, Any],
    evaluation_policy_file: Path,
    evaluation_price_config_file: Path,
    integrated_code_commit: str,
    review_record: Path,
    review_verdict: str = "pass",
) -> dict[str, Any]:
    """Write one complete evaluation gate that binds a set, prompt and run."""
    manifest, _ = load_eval_set(set_dir)
    gate = {
        "schema": "benchmark-evaluation-execution-gate-v1",
        "evaluation_enabled": review_verdict == "pass",
        "allowed_phase": EVALUATION_PHASE,
        "eval_set_id": manifest["eval_set_id"],
        "eval_set_manifest_sha256": manifest_sha256(set_dir),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "abstention_option_text": ABSTENTION_OPTION_TEXT,
        "models": sorted(models),
        "arms": sorted(arms),
        "decoding": decoding,
        "repeats_maximum": repeats_maximum,
        "authorized_run_id": run_id,
        "evaluation_policy_sha256": sha256_file(evaluation_policy_file),
        "evaluation_price_config_sha256": sha256_file(evaluation_price_config_file),
        "integrated_code_commit": integrated_code_commit,
        "independent_review_verdict": review_verdict,
        "review_record": str(review_record.resolve()),
        "review_record_sha256": sha256_file(review_record),
        "written_at_utc": _utc_now(),
    }
    atomic_json(path, gate)
    return gate


def build_broker_provider(
    *,
    broker: SharedGeminiBroker,
    set_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats: int,
    output_tokens_by_arm: dict[str, int] | None = None,
    admission: AbstractContextManager[Any] | None = None,
) -> tuple[GeminiBrokerEvaluationProvider, dict[str, Any]]:
    """Bind the run to the broker gate and return the provider and decoding.

    ``admission`` paces the paid calls of every question this process runs at
    once under the one concurrency limit of the evaluation phase.
    """
    manifest, _ = load_eval_set(set_dir)
    decoding = decoding_record(
        broker.evaluation_config or {}, models, arms, output_tokens_by_arm
    )
    binding = run_binding_record(
        eval_set_id=manifest["eval_set_id"],
        run_id=run_id,
        models=models,
        arms=arms,
        repeats=repeats,
        decoding=decoding,
    )
    broker.bind_evaluation(
        eval_set_manifest_file=set_dir / "manifest.json",
        eval_set_id=binding["eval_set_id"],
        run_id=run_id,
        models=binding["models"],
        arms=binding["arms"],
        repeats=repeats,
        prompt_version=binding["prompt_version"],
        prompt_sha256=binding["prompt_sha256"],
        abstention_option_text=binding["abstention_option_text"],
        decoding=decoding,
    )
    provider = GeminiBrokerEvaluationProvider(
        broker, run_id=run_id, decoding=decoding, admission=admission
    )
    return provider, decoding


def scripted_answers(
    trials: list[dict[str, Any]],
    decoding: dict[str, Any],
    *,
    policy: str,
    seed: str,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Map every planned payload hash to its scripted event."""
    answers: dict[str, dict[str, Any]] = {}
    for trial in trials:
        payload = evaluation_payload(
            system_text=trial["system_text"],
            user_text=trial["user_text"],
            letters=trial["letters"],
            temperature=decoding["temperature_by_model"][trial["model"]],
            max_output_tokens=decoding["max_output_tokens_by_arm"][trial["arm"]],
            arm=trial["arm"],
        )
        event = (overrides or {}).get(trial["trial_id"]) or scripted_letter(
            trial, policy, seed=seed
        )
        answers[scripted_key(trial["model"], payload)] = event
    return answers


def dry_run_policy(evaluation_policy_file: Path, output_path: Path) -> Path:
    """Write the private dry-run copy of an evaluation policy.

    The dry run keeps every control of the given evaluation policy except
    the per-minute pace, which exists to protect a paid provider quota. The
    scripted transport has no quota, so the private copy raises that one
    limit for every vendor; the ceiling, request cap, concurrency limit,
    repeat limit and retry ban stay exact.
    """
    budget_policy = json.loads(evaluation_policy_file.read_text(encoding="utf-8"))
    budget_policy["policy_id"] = f"{budget_policy['policy_id']}-dry-run"
    budget_policy["maximum_requests_per_minute"] = DRY_RUN_REQUESTS_PER_MINUTE
    for overrides in (budget_policy.get("vendors") or {}).values():
        if "maximum_requests_per_minute" in overrides:
            overrides["maximum_requests_per_minute"] = DRY_RUN_REQUESTS_PER_MINUTE
    atomic_json(output_path, budget_policy)
    return output_path


def dry_run_broker(
    *,
    ledger_dir: Path,
    set_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats: int,
    trials: list[dict[str, Any]],
    construction_policy_file: Path,
    construction_price_config_file: Path,
    construction_gate_file: Path,
    evaluation_policy_file: Path,
    evaluation_price_config_file: Path,
    policy: str = "random",
    seed: str = "dry-run",
    overrides: dict[str, dict[str, Any]] | None = None,
    latency_seconds: float = 0.0,
    code_commit: str | None = None,
) -> tuple[SharedGeminiBroker, ScriptedTransport, Path]:
    """Build the private dry-run ledger, gate and scripted broker for a plan."""
    ledger_dir.mkdir(parents=True, exist_ok=True)
    review = ledger_dir / "dry-run-review.md"
    if not review.is_file():
        review.write_text(
            "Dry run: scripted transport, no paid call, no credential read.\n",
            encoding="utf-8",
        )
    price_config = json.loads(evaluation_price_config_file.read_text(encoding="utf-8"))
    decoding = decoding_record(price_config, models, arms)
    evaluation_policy_file = dry_run_policy(
        evaluation_policy_file, ledger_dir / "evaluation-policy-dry-run.json"
    )
    gate_path = ledger_dir / "evaluation-gate.json"
    write_evaluation_gate(
        gate_path,
        set_dir=set_dir,
        run_id=run_id,
        models=models,
        arms=arms,
        repeats_maximum=max(repeats, 1),
        decoding=decoding,
        evaluation_policy_file=evaluation_policy_file,
        evaluation_price_config_file=evaluation_price_config_file,
        integrated_code_commit=code_commit or "dry-run",
        review_record=review,
    )
    transport = ScriptedTransport(
        scripted_answers(
            trials, decoding, policy=policy, seed=seed, overrides=overrides
        ),
        latency_seconds=latency_seconds,
    )
    # A private copy of the construction files keeps the dry-run ledger
    # self-contained and reproducible.
    for source in (
        construction_policy_file,
        construction_price_config_file,
        construction_gate_file,
    ):
        target = ledger_dir / source.name
        if not target.is_file():
            shutil.copy2(source, target)
    broker = SharedGeminiBroker(
        policy_file=ledger_dir / construction_policy_file.name,
        price_config_file=ledger_dir / construction_price_config_file.name,
        execution_gate_file=ledger_dir / construction_gate_file.name,
        ledger_file=ledger_dir / "shared-paid-call-ledger.json",
        receipts_dir=ledger_dir / "model-receipts",
        credential_file=_private_credential(ledger_dir),
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        evaluation_policy_file=evaluation_policy_file,
        evaluation_price_config_file=evaluation_price_config_file,
        evaluation_gate_file=gate_path,
    )
    return broker, transport, gate_path


def dry_run(
    *,
    set_dir: Path,
    output_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats: int,
    construction_policy_file: Path,
    construction_price_config_file: Path,
    construction_gate_file: Path,
    evaluation_policy_file: Path,
    evaluation_price_config_file: Path,
    policy: str = "random",
    seed: str = "dry-run",
    overrides: dict[str, dict[str, Any]] | None = None,
    max_calls: int | None = None,
    item_limit: int | None = None,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """Run the whole harness on a private ledger with a scripted transport."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_dir = output_dir / "ledger"
    manifest, items_all = load_eval_set(set_dir)
    items = items_all[: int(item_limit)] if item_limit is not None else items_all
    trials = plan_trials(manifest, items, models=models, arms=arms, repeats=repeats)
    broker, transport, _ = dry_run_broker(
        ledger_dir=ledger_dir,
        set_dir=set_dir,
        run_id=run_id,
        models=models,
        arms=arms,
        repeats=repeats,
        trials=trials,
        construction_policy_file=construction_policy_file,
        construction_price_config_file=construction_price_config_file,
        construction_gate_file=construction_gate_file,
        evaluation_policy_file=evaluation_policy_file,
        evaluation_price_config_file=evaluation_price_config_file,
        policy=policy,
        seed=seed,
        overrides=overrides,
        code_commit=code_commit,
    )
    provider, bound_decoding = build_broker_provider(
        broker=broker,
        set_dir=set_dir,
        run_id=run_id,
        models=models,
        arms=arms,
        repeats=repeats,
    )
    summary = run_evaluation(
        set_dir=set_dir,
        output_dir=output_dir,
        run_id=run_id,
        models=models,
        arms=arms,
        repeats=repeats,
        provider=provider,
        decoding=bound_decoding,
        max_calls=max_calls,
        code_commit=code_commit,
        item_limit=item_limit,
    )
    status = broker.status()
    summary["dry_run"] = {
        "ledger_file": str(broker.ledger_file),
        "scripted_policy": policy,
        "transport_calls": len(transport.calls),
        "ledger_evaluation": status["evaluation"],
        "ledger_usage": status["usage"],
    }
    atomic_json(output_dir / RUN_SUMMARY_FILENAME, summary)
    return summary
