"""Concurrent evaluation plan: every model of every vendor on one item at once.

A plan lists the models of each vendor, the thinking arms and the repeat
count. One item under the captain's plan of 2026-09-16 is 8 models x 2
conditions x 3 repeats = 48 trials at the ``high`` preset. The runner starts
the three vendors in parallel and, inside a vendor, keeps up to N calls in
flight (the smaller of the plan value, the policy limit and the command-line
override). Every existing invariant of the serial runner stays: one immutable
receipt per request key, the evaluation policy caps, resume from
``responses.jsonl`` and the receipts, stop on the first non-completed
response of a vendor, and the gate binding of the whole plan per vendor.

Layout of one plan run directory::

    <run-dir>/plan-manifest.json
    <run-dir>/plan-summary.json
    <run-dir>/<vendor>/run-manifest.json, trials.jsonl, responses.jsonl,
                       run-summary.json

Each vendor directory is a complete serial-runner directory, so the ``score``
action and every existing reader work on it unchanged.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .abstention_providers import (
    COMPLETED,
    PROVIDER_GOOGLE_GEMINI,
    EvaluationProvider,
    EvaluationRequest,
    decoding_record,
)
from .abstention_render import TAXONOMY
from .abstention_run import (
    RESPONSES_FILENAME,
    RUN_SUMMARY_FILENAME,
    dry_run_broker,
    build_broker_provider,
    plan_trials,
    prepare_run,
    response_row,
    summarize_run,
    write_evaluation_gate,
)
from .abstention_score import score_run
from .abstention_set import evaluation_identity, load_eval_set
from .abstention_subscription import (
    PROVIDER_ANTHROPIC_CLAUDE_CODE,
    PROVIDER_OPENAI_CODEX,
    SUBSCRIPTION_PROVIDER_NAMES,
    SubprocessTransport,
    binary_version,
    build_subscription_provider,
    load_subscription_models,
    subscription_decoding_record,
    subscription_dry_run_provider,
    subscription_gate_record,
    vendor_entry,
    vendor_policy_limit,
)
from .model_broker import SharedGeminiBroker
from .util import atomic_json, canonical_json, sha256_file


PLAN_SCHEMA = "benchmark-evaluation-plan-v1"
PAUSE_SCHEMA = "benchmark-evaluation-model-pause-v1"
DEFAULT_PAUSE_FILE = Path("config/benchmark-evaluation-model-pause-v1.json")
PLAN_MANIFEST_SCHEMA = "abstention-eval-plan-run-v1"
PLAN_SUMMARY_SCHEMA = "abstention-eval-plan-summary-v1"
PLAN_MANIFEST_FILENAME = "plan-manifest.json"
PLAN_SUMMARY_FILENAME = "plan-summary.json"
VENDOR_NAMES = (PROVIDER_GOOGLE_GEMINI, *SUBSCRIPTION_PROVIDER_NAMES)
DEFAULT_PLAN_FILE = Path("config/benchmark-evaluation-plan-high-v1.json")
GATE_FILENAME_BY_VENDOR = {vendor: f"{vendor}.json" for vendor in VENDOR_NAMES}


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- Plan file ---------------------------------------------------------------


def load_plan(path: Path) -> dict[str, Any]:
    """Read and validate one evaluation plan file."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported benchmark evaluation plan schema")
    if not str(value.get("plan_id") or "").strip():
        raise ValueError("the benchmark evaluation plan lacks a plan id")
    arms = value.get("arms")
    if (
        not isinstance(arms, list)
        or not arms
        or not all(isinstance(a, str) for a in arms)
    ):
        raise ValueError("the benchmark evaluation plan lists no thinking arm")
    repeats = value.get("repeats")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("the benchmark evaluation plan repeats value is invalid")
    k = value.get("k")
    if isinstance(k, bool) or not isinstance(k, int) or k < 2:
        raise ValueError("the benchmark evaluation plan k value is invalid")
    vendors = value.get("vendors")
    if not isinstance(vendors, dict) or not vendors:
        raise ValueError("the benchmark evaluation plan lists no vendor")
    seen: set[str] = set()
    for vendor, entry in vendors.items():
        if vendor not in VENDOR_NAMES:
            raise ValueError(
                f"the benchmark evaluation plan names an unknown vendor: {vendor}"
            )
        models = (entry or {}).get("models")
        if not isinstance(models, list) or not models:
            raise ValueError(f"the plan vendor {vendor} lists no model")
        for model in models:
            if not isinstance(model, str) or not model or model in seen:
                raise ValueError(
                    f"the plan vendor {vendor} repeats or misnames a model"
                )
            seen.add(model)
        concurrency = entry.get("concurrency", 1)
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or concurrency < 1
        ):
            raise ValueError(f"the plan vendor {vendor} concurrency is invalid")
    expected = len(seen) * 2 * len(arms) * repeats
    declared = value.get("trials_per_item")
    if declared is not None and int(declared) != expected:
        raise ValueError("the benchmark evaluation plan trials_per_item does not match")
    value["trials_per_item"] = expected
    return value


# --- Paused models -----------------------------------------------------------


def load_pause(path: Path) -> dict[str, Any]:
    """Read the paused-model file: which models the evaluator must not call.

    Captain order 2026-09-16: "pause the fable evaluation because I only have
    ~80% fable usage left today; I will run the fable benchmarking after the
    reset at 6:00pm today". A paused model keeps its trials pending: they are
    not recorded, not counted as invalid, and they run after the resume time
    without a re-run of anything else.
    """
    if not path.is_file():
        return {"schema": PAUSE_SCHEMA, "paused_models": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != PAUSE_SCHEMA:
        raise ValueError("unsupported benchmark evaluation model-pause schema")
    models = value.get("paused_models")
    if not isinstance(models, dict):
        raise ValueError("the model-pause file has no paused_models block")
    for model, entry in models.items():
        if not isinstance(model, str) or not model or not isinstance(entry, dict):
            raise ValueError("a model-pause entry is invalid")
        resume = entry.get("resume_at_utc")
        if resume is not None:
            _parse_utc(resume)
    return value


def _parse_utc(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid UTC timestamp: {value}") from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_pause_models(values: list[str] | None) -> dict[str, Any]:
    """Parse repeated ``model`` or ``model=<resume UTC>`` options."""
    models: dict[str, Any] = {}
    for item in values or []:
        model, _, resume = str(item).partition("=")
        model = model.strip()
        if not model:
            raise ValueError(f"invalid paused model: {item}")
        entry: dict[str, Any] = {"reason": "paused on the command line"}
        if resume.strip():
            _parse_utc(resume.strip())
            entry["resume_at_utc"] = resume.strip()
        models[model] = entry
    return {"schema": PAUSE_SCHEMA, "paused_models": models}


def merge_pause(*records: dict[str, Any] | None) -> dict[str, Any]:
    """Merge pause records; a later record wins for the same model."""
    models: dict[str, Any] = {}
    for record in records:
        models.update((record or {}).get("paused_models") or {})
    return {"schema": PAUSE_SCHEMA, "paused_models": models}


def paused_models(
    pause: dict[str, Any] | None, *, now: datetime | None = None
) -> frozenset[str]:
    """Return the models that are paused at ``now``.

    A model with no resume time stays paused until an operator removes it. A
    model whose resume time has passed is not paused any more, so the
    evaluator picks its pending trials up by itself.
    """
    current = now or datetime.now(UTC)
    result = set()
    for model, entry in ((pause or {}).get("paused_models") or {}).items():
        resume = (entry or {}).get("resume_at_utc")
        if resume is None or _parse_utc(resume) > current:
            result.add(model)
    return frozenset(result)


def plan_vendors(plan: dict[str, Any]) -> list[str]:
    return [vendor for vendor in VENDOR_NAMES if vendor in plan["vendors"]]


def plan_models(plan: dict[str, Any], vendor: str) -> list[str]:
    return list(plan["vendors"][vendor]["models"])


def plan_trials_per_model(plan: dict[str, Any]) -> int:
    """The trials one model of the plan owes one question.

    Two conditions, every arm, every repeat. A plan manifest carries the same
    two fields and answers the same way.
    """
    return 2 * len(plan["arms"]) * int(plan["repeats"])


def effective_concurrency(
    plan: dict[str, Any],
    policy: dict[str, Any],
    vendor: str,
    requested: int | None = None,
) -> int:
    """Return the calls in flight for one vendor: plan, policy and override."""
    value = int(plan["vendors"][vendor].get("concurrency", 1))
    value = min(
        value, vendor_policy_limit(policy, vendor, "maximum_concurrent_requests")
    )
    if requested is not None:
        if isinstance(requested, bool) or int(requested) < 1:
            raise ValueError("the concurrency override must be a positive integer")
        value = min(value, int(requested))
    return max(value, 1)


def parse_concurrency(value: str | None) -> dict[str, int]:
    """Parse ``vendor=N,vendor=N`` from the command line."""
    result: dict[str, int] = {}
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        vendor, _, number = part.partition("=")
        if vendor not in VENDOR_NAMES or not number.strip().isdigit():
            raise ValueError(f"invalid concurrency override: {part}")
        result[vendor] = int(number)
    return result


# --- Gates -------------------------------------------------------------------


def write_plan_gates(
    *,
    plan: dict[str, Any],
    set_dir: Path,
    run_id: str,
    output_dir: Path,
    review_record: Path,
    review_verdict: str,
    evaluation_policy_file: Path,
    evaluation_price_config_file: Path,
    subscription_models_file: Path,
    integrated_code_commit: str,
    binary_versions: dict[str, str] | None = None,
    binaries: dict[str, str] | None = None,
    vendors: list[str] | None = None,
) -> dict[str, Path]:
    """Write one evaluation gate per vendor of the plan into ``output_dir``.

    The Gemini gate follows :func:`write_evaluation_gate`; the subscription
    gates follow :func:`subscription_gate_record`. ``binary_versions`` pins
    the harness version of each subscription vendor; when absent the version
    is read from the binary.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    price_config = json.loads(evaluation_price_config_file.read_text(encoding="utf-8"))
    config = load_subscription_models(subscription_models_file)
    paths: dict[str, Path] = {}
    for vendor in vendors or plan_vendors(plan):
        models = plan_models(plan, vendor)
        path = output_dir / GATE_FILENAME_BY_VENDOR[vendor]
        if vendor == PROVIDER_GOOGLE_GEMINI:
            write_evaluation_gate(
                path,
                set_dir=set_dir,
                run_id=run_id,
                models=models,
                arms=list(plan["arms"]),
                repeats_maximum=int(plan["repeats"]),
                decoding=decoding_record(price_config, models, list(plan["arms"])),
                evaluation_policy_file=evaluation_policy_file,
                evaluation_price_config_file=evaluation_price_config_file,
                integrated_code_commit=integrated_code_commit,
                review_record=review_record,
                review_verdict=review_verdict,
            )
        else:
            binary = (binaries or {}).get(vendor) or str(
                vendor_entry(config, vendor)["binary"]
            )
            version = (binary_versions or {}).get(vendor)
            if version is None:
                version = binary_version(
                    vendor, binary, SubprocessTransport(), cwd=output_dir
                )
            decoding = subscription_decoding_record(
                config,
                vendor,
                models,
                list(plan["arms"]),
                binary=binary,
                binary_version=version,
            )
            atomic_json(
                path,
                subscription_gate_record(
                    vendor=vendor,
                    set_dir=set_dir,
                    run_id=run_id,
                    models=models,
                    arms=list(plan["arms"]),
                    repeats_maximum=int(plan["repeats"]),
                    decoding=decoding,
                    evaluation_policy_file=evaluation_policy_file,
                    subscription_models_file=subscription_models_file,
                    integrated_code_commit=integrated_code_commit,
                    review_record=review_record,
                    review_verdict=review_verdict,
                ),
            )
        paths[vendor] = path
    return paths


# --- Vendor runs -------------------------------------------------------------


@dataclass
class VendorRun:
    """One vendor of a plan: its provider, decoding, models and concurrency."""

    vendor: str
    provider: EvaluationProvider
    decoding: dict[str, Any]
    models: list[str]
    concurrency: int


def build_vendor_runs(
    *,
    plan: dict[str, Any],
    set_dir: Path,
    run_id: str,
    gate_dir: Path,
    evaluation_policy_file: Path,
    subscription_models_file: Path,
    broker_factory: Callable[[Path], SharedGeminiBroker] | None,
    subscription_ledger_root: Path | None,
    concurrency: dict[str, int] | None = None,
    vendors: list[str] | None = None,
    scratch_root: Path | None = None,
    binaries: dict[str, str] | None = None,
) -> dict[str, VendorRun]:
    """Bind every vendor of the plan to its gate and return the vendor runs.

    ``broker_factory`` receives the Gemini gate file and returns the broker
    that already binds the shared ledger. ``subscription_ledger_root`` holds
    one ledger directory per subscription vendor.
    """
    policy = json.loads(evaluation_policy_file.read_text(encoding="utf-8"))
    runs: dict[str, VendorRun] = {}
    for vendor in vendors or plan_vendors(plan):
        models = plan_models(plan, vendor)
        gate = gate_dir / GATE_FILENAME_BY_VENDOR[vendor]
        if vendor == PROVIDER_GOOGLE_GEMINI:
            if broker_factory is None:
                raise ValueError("the Gemini vendor needs a broker factory")
            provider, decoding = build_broker_provider(
                broker=broker_factory(gate),
                set_dir=set_dir,
                run_id=run_id,
                models=models,
                arms=list(plan["arms"]),
                repeats=int(plan["repeats"]),
            )
        else:
            if subscription_ledger_root is None:
                raise ValueError("a subscription vendor needs a ledger root")
            provider, decoding = build_subscription_provider(
                vendor=vendor,
                set_dir=set_dir,
                run_id=run_id,
                models=models,
                arms=list(plan["arms"]),
                repeats=int(plan["repeats"]),
                ledger_dir=subscription_ledger_root / vendor,
                evaluation_policy_file=evaluation_policy_file,
                evaluation_gate_file=gate,
                subscription_models_file=subscription_models_file,
                scratch_root=(scratch_root / vendor) if scratch_root else None,
                binary=(binaries or {}).get(vendor),
            )
        runs[vendor] = VendorRun(
            vendor=vendor,
            provider=provider,
            decoding=decoding,
            models=models,
            concurrency=effective_concurrency(
                plan, policy, vendor, (concurrency or {}).get(vendor)
            ),
        )
    return runs


def run_vendor(
    *,
    set_dir: Path,
    output_dir: Path,
    run_id: str,
    vendor_run: VendorRun,
    arms: list[str],
    repeats: int,
    item_limit: int | None = None,
    max_calls: int | None = None,
    code_commit: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    serial: bool = False,
    paused: frozenset[str] = frozenset(),
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run or resume one vendor of a plan with N calls in flight.

    Trials are dispatched in plan order to a pool of ``concurrency`` workers.
    Every row is appended under one lock. After the first non-completed
    response no further trial is dispatched; the calls already in flight
    finish and are recorded. Nothing is retried and nothing is re-asked.

    A trial of a paused model is not dispatched and not recorded, so it stays
    pending for a later invocation.

    ``should_stop`` is the same door from outside: an operator stop of the
    unit. Its trials are held exactly as a paused model's are, so the item is
    not complete and a later invocation runs what is missing. Without it a
    stop could only land between two whole items, which is minutes of trials,
    and systemd killed the evaluator at its 90-second stop bound on
    2026-09-17 at 07:25:55 UTC.
    """
    provider = vendor_run.provider
    prepared = prepare_run(
        set_dir=set_dir,
        output_dir=output_dir,
        run_id=run_id,
        models=vendor_run.models,
        arms=arms,
        repeats=repeats,
        provider=provider,
        decoding=vendor_run.decoding,
        code_commit=code_commit,
        item_limit=item_limit,
    )
    run_manifest = prepared["run_manifest"]
    items = prepared["items"]
    trials = prepared["trials"]
    rows = prepared["rows"]
    responses_path = prepared["responses_path"]
    done = {row["trial_id"] for row in rows}
    items_by_id = {row["item_id"]: row for row in items}
    pending = [trial for trial in trials if trial["trial_id"] not in done]
    held = [trial for trial in pending if trial["model"] in paused]
    pending = [trial for trial in pending if trial["model"] not in paused]
    if max_calls is not None:
        pending = pending[: int(max_calls)]
    workers = 1 if serial else max(int(vendor_run.concurrency), 1)
    lock = threading.Lock()
    stop = threading.Event()
    counters = {"calls": 0}
    errors: list[BaseException] = []
    # The rows this pass recorded, which is not every row of the run: a
    # revisit of an item resumes what an earlier pass wrote. A stop belongs to
    # the pass that met it, because the caller pauses the vendor on it.
    fresh: list[dict[str, Any]] = []
    started = time.monotonic()

    def work(trial: dict[str, Any]) -> None:
        if stop.is_set() or (should_stop is not None and should_stop()):
            return
        request = EvaluationRequest(
            trial=trial,
            identity=evaluation_identity(items_by_id[trial["item_id"]]),
            run_id=run_id,
        )
        try:
            response = provider.answer(request)
        except BaseException as error:  # noqa: BLE001 - recorded, then re-raised
            stop.set()
            with lock:
                errors.append(error)
            return
        row = response_row(
            trial,
            response,
            enum_output=provider.supports_enum_output(trial["model"]),
            code_commit=code_commit,
        )
        with lock:
            with responses_path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(row) + "\n")
                handle.flush()
            rows.append(row)
            fresh.append(row)
            if not response.resumed:
                counters["calls"] += 1
        if response.state != COMPLETED:
            # Fail closed for this vendor: a budget stop, a harness error or an
            # ambiguous charge stops new dispatch. In-flight calls finish.
            stop.set()
        if progress is not None:
            progress(row)

    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=vendor_run.vendor
    ) as pool:
        futures = [pool.submit(work, trial) for trial in pending]
        for future in futures:
            future.result()
    wall = time.monotonic() - started
    if errors:
        raise errors[0]
    summary = summarize_run(rows, {**run_manifest, "planned_trials": len(trials)})
    # The stop of THIS pass, which is what pauses a vendor. `stopped_on` holds
    # the first stop of the whole run directory, and a stop there is permanent:
    # the policy forbids a retry, so the recorded row stays for ever. Reading
    # it would pause the vendor again on every later pass over that item, and
    # the arm would be off again the moment a revisit touched an old stop.
    stopped_now = [row for row in fresh if row["response"]["state"] != COMPLETED]
    summary["stopped_this_pass"] = (
        {
            "trial_id": stopped_now[0]["trial_id"],
            "state": stopped_now[0]["response"]["state"],
            "error": stopped_now[0]["response"]["error"],
        }
        if stopped_now
        else None
    )
    summary["calls_this_invocation"] = counters["calls"]
    summary["vendor"] = vendor_run.vendor
    summary["concurrency"] = workers
    summary["wall_seconds"] = round(wall, 3)
    summary["paused_models"] = sorted({trial["model"] for trial in held})
    summary["pending_trials"] = len(trials) - len(rows)
    summary["pending_paused_trials"] = len(held)
    atomic_json(output_dir / RUN_SUMMARY_FILENAME, summary)
    return summary


def outcomes_by_model(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Tally N0 to N5 per model over the given response rows."""
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        counts = result.setdefault(row["model"], {name: 0 for name in TAXONOMY})
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return dict(sorted(result.items()))


def load_vendor_rows(run_dir: Path, vendor: str) -> list[dict[str, Any]]:
    path = run_dir / vendor / RESPONSES_FILENAME
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


PLAN_MANIFEST_IMMUTABLE_FIELDS = (
    "schema",
    "plan_id",
    "run_id",
    "eval_set_id",
    "eval_set_dir",
    "item_count",
    "k",
    "arms",
    "repeats",
    "gate_dir",
)


def extend_plan_manifest(
    existing: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Merge one pass into the item's plan manifest, and never alter a record.

    The manifest was immutable field by field, which made an item impossible
    to finish on any later pass: a pass on another code commit, or a pass that
    runs a vendor the first pass could not reach, writes a different manifest
    and the run stopped with "the run directory holds a different plan
    manifest". The Claude Code arm was paused for two items on 2026-09-17, and
    their manifests bound two vendors, so the arm could never come back to
    them.

    The identity of the item still cannot move: the plan, the run id, the
    evaluation set, the arms, the repeats and the gate directory are compared
    field by field. Everything else only grows.

    - ``vendors`` takes the union. A vendor in both passes must name the same
      models, because a changed model list is a changed plan.
    - ``trials_per_item`` is recomputed from that union.
    - ``code_commits`` appends the commit of this pass, in order, and
      ``code_commit`` keeps the commit that opened the item.

    Nothing recorded is altered. Every trial keeps the receipt, the gate and
    the commit of the pass that ran it, so the money evidence of each call
    stays exactly as it was written.
    """
    for field in PLAN_MANIFEST_IMMUTABLE_FIELDS:
        if existing.get(field) != current.get(field):
            raise ValueError("the run directory holds a different plan manifest")
    vendors = dict(existing.get("vendors") or {})
    for vendor, entry in (current.get("vendors") or {}).items():
        if vendor in vendors and vendors[vendor] != entry:
            raise ValueError("the run directory holds another model list")
        vendors[vendor] = entry
    trials_per_model = plan_trials_per_model(current)
    commits = [
        commit
        for commit in (
            list(existing.get("code_commits") or [])
            or ([existing["code_commit"]] if existing.get("code_commit") else [])
        )
    ]
    for commit in current.get("code_commits") or []:
        if commit not in commits:
            commits.append(commit)
    return {
        **current,
        "vendors": {vendor: vendors[vendor] for vendor in sorted(vendors)},
        "trials_per_item": sum(
            len(entry["models"]) * trials_per_model for entry in vendors.values()
        ),
        "code_commit": existing.get("code_commit") or current.get("code_commit"),
        "code_commits": commits,
    }


def run_plan(
    *,
    set_dir: Path,
    output_dir: Path,
    run_id: str,
    plan: dict[str, Any],
    vendor_runs: dict[str, VendorRun],
    item_limit: int | None = None,
    max_calls: int | None = None,
    code_commit: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    serial: bool = False,
    gate_dir: Path | None = None,
    pause: dict[str, Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run or resume every vendor of a plan on one set, vendors in parallel.

    With ``serial`` the vendors run one after another with one call in
    flight each: the baseline of the wall-time comparison. ``pause`` holds the
    models the evaluator must not call now; their trials stay pending, and
    ``should_stop`` holds the rest the same way when an operator stops the
    unit.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest, items = load_eval_set(set_dir)
    if item_limit is not None:
        items = items[: int(item_limit)]
    plan_manifest = {
        "schema": PLAN_MANIFEST_SCHEMA,
        "plan_id": plan["plan_id"],
        "run_id": run_id,
        "eval_set_id": manifest["eval_set_id"],
        "eval_set_dir": str(set_dir.resolve()),
        "item_count": len(items),
        "k": manifest["k"],
        "arms": sorted(plan["arms"]),
        "repeats": int(plan["repeats"]),
        "vendors": {
            vendor: {"models": sorted(run.models)}
            for vendor, run in vendor_runs.items()
        },
        "trials_per_item": sum(
            len(run.models) * 2 * len(plan["arms"]) * int(plan["repeats"])
            for run in vendor_runs.values()
        ),
        "gate_dir": str(gate_dir.resolve()) if gate_dir else None,
        "code_commit": code_commit,
        "code_commits": [code_commit] if code_commit else [],
    }
    manifest_path = output_dir / PLAN_MANIFEST_FILENAME
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        plan_manifest = extend_plan_manifest(existing, plan_manifest)
        atomic_json(
            manifest_path,
            {
                **plan_manifest,
                "created_at_utc": existing.get("created_at_utc") or _utc_now(),
            },
        )
    else:
        atomic_json(manifest_path, {**plan_manifest, "created_at_utc": _utc_now()})
    held = paused_models(pause)
    results: dict[str, dict[str, Any]] = {}
    failures: dict[str, BaseException] = {}
    started = time.monotonic()

    def run_one(vendor: str, vendor_run: VendorRun) -> None:
        try:
            results[vendor] = run_vendor(
                set_dir=set_dir,
                output_dir=output_dir / vendor,
                run_id=run_id,
                vendor_run=vendor_run,
                arms=list(plan["arms"]),
                repeats=int(plan["repeats"]),
                item_limit=item_limit,
                max_calls=max_calls,
                code_commit=code_commit,
                progress=progress,
                serial=serial,
                paused=held,
                should_stop=should_stop,
            )
        except BaseException as error:  # noqa: BLE001 - reported per vendor
            failures[vendor] = error

    if serial:
        for vendor, vendor_run in vendor_runs.items():
            run_one(vendor, vendor_run)
    else:
        threads = [
            threading.Thread(target=run_one, args=(vendor, run), name=f"plan-{vendor}")
            for vendor, run in vendor_runs.items()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    wall = time.monotonic() - started
    summary = summarize_plan(
        output_dir,
        plan_manifest=plan_manifest,
        results=results,
        failures={vendor: f"{type(e).__name__}: {e}" for vendor, e in failures.items()},
        wall_seconds=wall,
        serial=serial,
        paused=held,
    )
    atomic_json(output_dir / PLAN_SUMMARY_FILENAME, summary)
    if failures:
        raise next(iter(failures.values()))
    return summary


def summarize_plan(
    output_dir: Path,
    *,
    plan_manifest: dict[str, Any],
    results: dict[str, dict[str, Any]],
    failures: dict[str, str],
    wall_seconds: float,
    serial: bool,
    paused: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Aggregate the vendor summaries of one plan run into one record."""
    rows_by_vendor = {
        vendor: load_vendor_rows(output_dir, vendor)
        for vendor in plan_manifest["vendors"]
    }
    all_rows = [row for rows in rows_by_vendor.values() for row in rows]
    gemini_rows = rows_by_vendor.get(PROVIDER_GOOGLE_GEMINI, [])
    gemini_usd = sum(
        (
            Decimal(row["response"]["cost_usd"])
            for row in gemini_rows
            if row["response"]["state"] == COMPLETED
            and row["response"]["cost_usd"] is not None
        ),
        Decimal("0"),
    )
    tokens_by_vendor: dict[str, dict[str, int]] = {}
    for vendor, rows in rows_by_vendor.items():
        tokens = {
            "promptTokenCount": 0,
            "candidatesTokenCount": 0,
            "thoughtsTokenCount": 0,
        }
        for row in rows:
            usage = row["response"].get("usage") or {}
            for key in tokens:
                tokens[key] += int(usage.get(key, 0) or 0)
        tokens_by_vendor[vendor] = tokens
    planned = plan_manifest["trials_per_item"] * plan_manifest["item_count"]
    vendor_complete = {
        vendor: bool(results.get(vendor, {}).get("complete"))
        for vendor in plan_manifest["vendors"]
    }
    invalid = [row for row in all_rows if not row["valid"]]
    return {
        "schema": PLAN_SUMMARY_SCHEMA,
        "plan_id": plan_manifest["plan_id"],
        "run_id": plan_manifest["run_id"],
        "eval_set_id": plan_manifest["eval_set_id"],
        "item_count": plan_manifest["item_count"],
        "planned_trials": planned,
        "recorded_trials": len(all_rows),
        "complete": all(vendor_complete.values()) and not failures,
        "serial": serial,
        "paused_models": sorted(paused),
        "pending_trials": planned - len(all_rows),
        "wall_seconds": round(wall_seconds, 3),
        "vendors": {
            vendor: {
                "models": plan_manifest["vendors"][vendor]["models"],
                "complete": vendor_complete[vendor],
                "recorded_trials": len(rows_by_vendor[vendor]),
                "calls_this_invocation": results.get(vendor, {}).get(
                    "calls_this_invocation"
                ),
                "concurrency": results.get(vendor, {}).get("concurrency"),
                "wall_seconds": results.get(vendor, {}).get("wall_seconds"),
                "stopped_on": results.get(vendor, {}).get("stopped_on"),
                "stopped_this_pass": results.get(vendor, {}).get("stopped_this_pass"),
                "paused_models": results.get(vendor, {}).get("paused_models"),
                "pending_trials": results.get(vendor, {}).get("pending_trials"),
                "error": failures.get(vendor),
                "tokens": tokens_by_vendor[vendor],
                "per_model_arm": results.get(vendor, {}).get("per_model_arm"),
            }
            for vendor in plan_manifest["vendors"]
        },
        "gemini_usd": str(gemini_usd),
        "invalid_count": len(invalid),
        "invalid_rate": round(len(invalid) / len(all_rows), 4) if all_rows else None,
        "outcomes_by_model": outcomes_by_model(all_rows),
        "updated_at_utc": _utc_now(),
    }


def score_plan(
    run_dir: Path,
    *,
    output_dir: Path | None = None,
    resamples: int = 2000,
    seed: int = 7,
) -> dict[str, Any]:
    """Score every vendor of one plan run together (one responses file)."""
    manifest = json.loads(
        (run_dir / PLAN_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    _, items = load_eval_set(Path(manifest["eval_set_dir"]))
    output_dir = output_dir or (run_dir / "scores")
    output_dir.mkdir(parents=True, exist_ok=True)
    combined = output_dir / "responses-all.jsonl"
    rows = [
        row
        for vendor in manifest["vendors"]
        for row in load_vendor_rows(run_dir, vendor)
    ]
    combined.write_text(
        "".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8"
    )
    return score_run(
        responses_file=combined,
        items=items,
        k=int(manifest["k"]),
        output_dir=output_dir,
        resamples=resamples,
        seed=seed,
        run_manifest={
            **manifest,
            "models": sorted(
                m for v in manifest["vendors"].values() for m in v["models"]
            ),
            "eval_set_manifest_sha256": sha256_file(
                Path(manifest["eval_set_dir"]) / "manifest.json"
            ),
        },
    )


# --- Dry run -----------------------------------------------------------------


def dry_run_plan(
    *,
    plan: dict[str, Any],
    set_dir: Path,
    output_dir: Path,
    run_id: str,
    construction_policy_file: Path,
    construction_price_config_file: Path,
    construction_gate_file: Path,
    evaluation_policy_file: Path,
    evaluation_price_config_file: Path,
    subscription_models_file: Path,
    policy: str = "random",
    seed: str = "dry-run",
    overrides: dict[str, dict[str, Any]] | None = None,
    latency_seconds: float | dict[str, float] = 0.0,
    serial: bool = False,
    item_limit: int | None = None,
    max_calls: int | None = None,
    code_commit: str | None = None,
    concurrency: dict[str, int] | None = None,
    vendors: list[str] | None = None,
    pause: dict[str, Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the whole plan offline: private ledgers, scripted transports.

    ``latency_seconds`` makes every scripted call sleep, so the wall time of
    the serial and the concurrent schedule can be compared. It is one value
    for every vendor, or a mapping of vendor to seconds when the measured
    latencies differ by vendor.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest, items_all = load_eval_set(set_dir)
    items = items_all[: int(item_limit)] if item_limit is not None else items_all
    policy_record = json.loads(evaluation_policy_file.read_text(encoding="utf-8"))
    vendor_runs: dict[str, VendorRun] = {}
    for vendor in vendors or plan_vendors(plan):
        models = plan_models(plan, vendor)
        latency = (
            float(latency_seconds.get(vendor, 0.0))
            if isinstance(latency_seconds, dict)
            else float(latency_seconds)
        )
        trials = plan_trials(
            manifest,
            items,
            models=models,
            arms=list(plan["arms"]),
            repeats=int(plan["repeats"]),
        )
        ledger_dir = output_dir / vendor / "ledger"
        if vendor == PROVIDER_GOOGLE_GEMINI:
            broker, _, _ = dry_run_broker(
                ledger_dir=ledger_dir,
                set_dir=set_dir,
                run_id=run_id,
                models=models,
                arms=list(plan["arms"]),
                repeats=int(plan["repeats"]),
                trials=trials,
                construction_policy_file=construction_policy_file,
                construction_price_config_file=construction_price_config_file,
                construction_gate_file=construction_gate_file,
                evaluation_policy_file=evaluation_policy_file,
                evaluation_price_config_file=evaluation_price_config_file,
                policy=policy,
                seed=seed,
                overrides=overrides,
                latency_seconds=latency,
                code_commit=code_commit,
            )
            provider, decoding = build_broker_provider(
                broker=broker,
                set_dir=set_dir,
                run_id=run_id,
                models=models,
                arms=list(plan["arms"]),
                repeats=int(plan["repeats"]),
            )
        else:
            provider, decoding, _, _ = subscription_dry_run_provider(
                vendor=vendor,
                set_dir=set_dir,
                ledger_dir=ledger_dir,
                run_id=run_id,
                models=models,
                arms=list(plan["arms"]),
                repeats=int(plan["repeats"]),
                trials=trials,
                evaluation_policy_file=evaluation_policy_file,
                subscription_models_file=subscription_models_file,
                policy=policy,
                seed=seed,
                overrides=overrides,
                latency_seconds=latency,
                code_commit=code_commit,
            )
        vendor_runs[vendor] = VendorRun(
            vendor=vendor,
            provider=provider,
            decoding=decoding,
            models=models,
            concurrency=effective_concurrency(
                plan, policy_record, vendor, (concurrency or {}).get(vendor)
            ),
        )
    summary = run_plan(
        set_dir=set_dir,
        output_dir=output_dir,
        run_id=run_id,
        plan=plan,
        vendor_runs=vendor_runs,
        item_limit=item_limit,
        max_calls=max_calls,
        code_commit=code_commit,
        serial=serial,
        pause=pause,
        should_stop=should_stop,
    )
    summary["dry_run"] = {
        "scripted_policy": policy,
        "latency_seconds": latency_seconds,
        "concurrency": {vendor: run.concurrency for vendor, run in vendor_runs.items()},
        "ledgers": {
            vendor: (
                run.provider.broker.status()["evaluation"]
                if vendor == PROVIDER_GOOGLE_GEMINI
                else run.provider.ledger.status()
            )
            for vendor, run in vendor_runs.items()
        },
    }
    atomic_json(output_dir / PLAN_SUMMARY_FILENAME, summary)
    return summary


__all__ = [
    "DEFAULT_PAUSE_FILE",
    "DEFAULT_PLAN_FILE",
    "GATE_FILENAME_BY_VENDOR",
    "PLAN_MANIFEST_FILENAME",
    "PLAN_SUMMARY_FILENAME",
    "PROVIDER_ANTHROPIC_CLAUDE_CODE",
    "PROVIDER_OPENAI_CODEX",
    "VENDOR_NAMES",
    "VendorRun",
    "build_vendor_runs",
    "dry_run_plan",
    "effective_concurrency",
    "load_pause",
    "load_plan",
    "merge_pause",
    "parse_pause_models",
    "paused_models",
    "load_vendor_rows",
    "outcomes_by_model",
    "parse_concurrency",
    "plan_models",
    "plan_vendors",
    "run_plan",
    "run_vendor",
    "score_plan",
    "summarize_plan",
    "write_plan_gates",
]
