"""Streaming evaluator: evaluate every accepted question as soon as it lands.

The watcher reads the production state database read-only, finds every
accepted candidate that matches the current contract, freezes it as a
one-item evaluation set with its fixed distractor order and k = 4, runs the
whole plan of :mod:`arctic_qa.abstention_plan` on it (8 models x 2 conditions
x 3 repeats at the high preset), and appends one row to the cost journal of
:mod:`arctic_qa.abstention_cost`. Then it waits for the next item.

Authorization. A paid Gemini call needs a gate that binds one evaluation set,
and a streaming run meets a new set at every item, so a human cannot review
each one in time. The watcher therefore needs one reviewed *streaming
authorization* file that binds the population contract, the plan file, the
evaluation policy, the price config, the subscription registry, the code
commit, and two hard bounds: the item count and the Gemini USD. From that
authorization the watcher derives one gate per item and per vendor, with
every field taken from the authorization and only the set identity filled in
per item. The derivation is deterministic, each derived gate is written to
disk beside its item, and the authorization is the ``review_record`` of every
derived gate. A changed contract, plan, policy, price config, registry or
commit stops the watcher before the first call.

Paused models. A model can be paused without a stop of the run. Captain
order 2026-09-16: "pause the fable evaluation because I only have ~80% fable
usage left today; I will run the fable benchmarking after the reset at 6:00pm
today". The watcher reads
every paused-model file it is given (the committed
``config/benchmark-evaluation-model-pause-v1.json`` by default, a cost guard's
own file through a second ``--pause-file``) and the repeatable
``--pause-model`` option, and never dispatches a trial of a paused model. The
held trials stay pending: they are not recorded and not counted as invalid.
The item's journal row names the paused models and counts the held trials, so
the item is not complete, and a later pass, after the resume time, runs only
the missing trials. The other models of the plan run as usual. A cost guard
can add a model to that file at any time; the next item honours it.

Bounds. The watcher stops taking new items at the authorized item count. It
never lets one Gemini call pass the evaluation ceiling: the broker refuses
the call, and the watcher then pauses the Gemini vendor, journals the pause,
and keeps the subscription vendors running. It exits non-zero only on a real
error.

A bound is not an error, so the exit code alone never says that the benchmark
stopped. That silence cost a day: the unit met its item bound at
2026-09-16T19:31:44Z, exited 0, and no operator saw it until the next
morning. So ``status_file`` takes the supervisor's status file, and the
watcher appends one ``blocked:`` line to it when a bound ends the run.

Paused vendors. A vendor that stops on an item is paused for the rest of the
invocation, because the policy forbids a retry, and a start clears that pause.
One pause lifts on its own: an ambiguous charge. The broker keeps the
reservation of a paid call whose charge it cannot prove and halts the
evaluation phase until a supervisor releases it with a reviewed continuation.
That release is a ledger fact, so the watcher reads the shared ledger on every
poll, and resumes the Gemini vendor on the first poll after the release with a
``vendor_resumed`` event and a journal row. No restart is needed.
"""

from __future__ import annotations

import json
import signal
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .abstention_cost import (
    CostJournal,
    DEFAULT_LIST_PRICE_FILE,
    cost_row,
    generation_cost_record,
    load_list_prices,
    pause_row,
    read_ledger,
    resume_row,
    summarize_journal,
)
from .abstention_plan import (
    GATE_FILENAME_BY_VENDOR,
    PLAN_SUMMARY_FILENAME,
    build_vendor_runs,
    load_pause,
    load_plan,
    load_vendor_rows,
    merge_pause,
    paused_models,
    plan_models,
    plan_vendors,
    run_plan,
    write_plan_gates,
)
from .abstention_providers import PROVIDER_GOOGLE_GEMINI
from .abstention_set import (
    ACCEPTED_STATUS,
    build_eval_set,
    load_contract,
    load_eval_set,
)
from .model_broker import (
    EVALUATION_CEILING_REASON,
    EVALUATION_ITEM_REPEAT_REASON,
    EVALUATION_PHASE,
    SharedGeminiBroker,
    phase_halt_reason,
)
from .util import atomic_json, sha256_file


AUTHORIZATION_SCHEMA = "abstention-streaming-authorization-v1"
WATCH_STATE_SCHEMA = "abstention-streaming-watch-state-v1"
WATCH_STATE_FILENAME = "watch-state.json"
SKIP_KIND = "skipped_item"
# The request state the broker records for a paid call whose charge it
# cannot prove. Such a pause waits on a supervisor release, not a restart.
AMBIGUOUS_CHARGE_STATE = "ambiguous_charge"
DEFAULT_POLL_SECONDS = 30
MINIMUM_POLL_SECONDS = 5
MAXIMUM_POLL_SECONDS = 600
ASSUMED_INPUT_TOKENS = 1200
CEILING_SAFETY = Decimal("1.0")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- Authorization ---------------------------------------------------------------


def authorization_record(
    *,
    contract_file: Path,
    plan_file: Path,
    evaluation_policy_file: Path,
    evaluation_price_config_file: Path,
    subscription_models_file: Path,
    state_db: Path,
    campaign_id: str,
    run_id_prefix: str,
    maximum_items: int,
    maximum_gemini_usd: str,
    integrated_code_commit: str,
    review_record: Path,
    review_verdict: str = "pending",
) -> dict[str, Any]:
    """Return one streaming authorization template for a reviewer to sign."""
    plan = load_plan(plan_file)
    return {
        "schema": AUTHORIZATION_SCHEMA,
        "authorization_enabled": review_verdict == "pass",
        "independent_review_verdict": review_verdict,
        "purpose": (
            "Authorize the streaming evaluator to derive one evaluation gate per "
            "accepted item of this campaign and contract, inside the item and USD "
            "bounds below. See docs/ABSTENTION_EVALUATION.md, 'Streaming evaluator'."
        ),
        "state_db": str(state_db),
        "campaign_id": campaign_id,
        "run_id_prefix": run_id_prefix,
        "contract": load_contract(contract_file),
        "contract_file": str(contract_file),
        "contract_file_sha256": sha256_file(contract_file),
        "plan_id": plan["plan_id"],
        "plan_file": str(plan_file),
        "plan_file_sha256": sha256_file(plan_file),
        "trials_per_item": plan["trials_per_item"],
        "k": int(plan["k"]),
        "maximum_items": int(maximum_items),
        "maximum_gemini_usd": str(maximum_gemini_usd),
        "evaluation_policy_file": str(evaluation_policy_file),
        "evaluation_policy_sha256": sha256_file(evaluation_policy_file),
        "evaluation_price_config_file": str(evaluation_price_config_file),
        "evaluation_price_config_sha256": sha256_file(evaluation_price_config_file),
        "subscription_models_file": str(subscription_models_file),
        "subscription_models_sha256": sha256_file(subscription_models_file),
        "integrated_code_commit": integrated_code_commit,
        "review_record": str(review_record.resolve()),
        "review_record_sha256": sha256_file(review_record),
        "written_at_utc": _utc_now(),
    }


def validate_authorization(
    path: Path,
    *,
    plan_file: Path,
    contract_file: Path,
    evaluation_policy_file: Path,
    evaluation_price_config_file: Path,
    subscription_models_file: Path,
    code_commit: str | None,
) -> dict[str, Any]:
    """Read the authorization and check every binding before the first call."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != AUTHORIZATION_SCHEMA:
        raise ValueError("unsupported streaming authorization schema")
    if value.get("authorization_enabled") is not True:
        raise ValueError("the streaming authorization is disabled")
    if value.get("independent_review_verdict") != "pass":
        raise ValueError("the streaming authorization review did not pass")
    for field in (
        "campaign_id",
        "run_id_prefix",
        "review_record",
        "integrated_code_commit",
    ):
        if not str(value.get(field) or "").strip():
            raise ValueError(f"the streaming authorization lacks {field}")
    items = value.get("maximum_items")
    if isinstance(items, bool) or not isinstance(items, int) or items < 1:
        raise ValueError("the streaming authorization item bound is invalid")
    if Decimal(str(value.get("maximum_gemini_usd") or "0")) <= 0:
        raise ValueError("the streaming authorization USD bound is invalid")
    hashes = {
        "plan_file_sha256": plan_file,
        "contract_file_sha256": contract_file,
        "evaluation_policy_sha256": evaluation_policy_file,
        "evaluation_price_config_sha256": evaluation_price_config_file,
        "subscription_models_sha256": subscription_models_file,
    }
    for field, file in hashes.items():
        if value.get(field) != sha256_file(file):
            raise ValueError(f"the streaming authorization binds another {field}")
    if value["contract"] != load_contract(contract_file):
        raise ValueError(
            "the streaming authorization binds another population contract"
        )
    if value["plan_id"] != load_plan(plan_file)["plan_id"]:
        raise ValueError("the streaming authorization binds another plan id")
    review = Path(str(value["review_record"]))
    if not review.is_file() or value.get("review_record_sha256") != sha256_file(review):
        raise ValueError("the streaming authorization review record changed")
    if code_commit and value["integrated_code_commit"] != code_commit:
        raise ValueError(
            "the streaming authorization binds another code commit than the running code"
        )
    return value


# --- Pending items ---------------------------------------------------------------


def pending_item_ids(
    *,
    state_db: Path,
    contract: dict[str, str],
    exclude: set[str],
    campaign_id: str | None = None,
) -> list[str]:
    """Return the accepted item ids of one contract, oldest first.

    ``campaign_id`` restricts the query to one campaign. The backfill
    contract belongs to an earlier campaign, so it passes None and every
    campaign is searched.
    """
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if campaign_id:
            rows = connection.execute(
                "SELECT item_id,candidate_json FROM candidates "
                "WHERE run_id=? AND status=? ORDER BY updated_at,item_id",
                (campaign_id, ACCEPTED_STATUS),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT item_id,candidate_json FROM candidates "
                "WHERE status=? ORDER BY updated_at,item_id",
                (ACCEPTED_STATUS,),
            ).fetchall()
    finally:
        connection.close()
    pending: list[str] = []
    for row in rows:
        item_id = str(row["item_id"])
        if item_id in exclude:
            continue
        try:
            candidate = json.loads(row["candidate_json"])
        except json.JSONDecodeError:
            continue
        provenance = candidate.get("provenance") or {}
        if (
            candidate.get("schema_version") != contract["candidate_schema_version"]
            or provenance.get("prompt_version") != contract["generation_prompt_version"]
            or provenance.get("scope_contract_version")
            != contract["scope_contract_version"]
        ):
            continue
        pending.append(item_id)
    return pending


# --- Ceiling estimate ------------------------------------------------------------


def estimated_gemini_item_usd(
    price_config: dict[str, Any],
    models: list[str],
    arms: list[str],
    repeats: int,
    *,
    output_cap: int,
    assumed_input_tokens: int = ASSUMED_INPUT_TOKENS,
) -> Decimal:
    """Estimate what the broker will reserve for one item's Gemini trials.

    The broker reserves the full output cap of the arm, so this is the exact
    reservation for an assumed input size. The watcher compares it with the
    remaining ceiling before it starts an item.
    """
    total = Decimal("0")
    trials_per_model = 2 * len(arms) * int(repeats)
    for model in models:
        entry = price_config["models"][model]
        per_call = (
            Decimal(assumed_input_tokens)
            * Decimal(entry["input_usd_per_million_tokens"])
            + Decimal(output_cap)
            * Decimal(entry["output_usd_per_million_tokens_including_thinking"])
        ) / Decimal("1000000")
        total += per_call * Decimal(trials_per_model)
    return total


# --- Watch state -----------------------------------------------------------------


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema": WATCH_STATE_SCHEMA,
            "paused_vendors": {},
            "skipped_items": {},
            "started_at_utc": _utc_now(),
        }
    return json.loads(path.read_text(encoding="utf-8"))


# --- One item --------------------------------------------------------------------


def evaluate_item(
    *,
    item_id: str,
    authorization: dict[str, Any],
    plan: dict[str, Any],
    state_db: Path,
    sets_dir: Path,
    runs_dir: Path,
    gates_dir: Path,
    journal: CostJournal,
    prices: dict[str, Any],
    shared_ledger_file: Path,
    broker_factory: Callable[[Path], SharedGeminiBroker] | None,
    subscription_ledger_root: Path | None,
    authorization_file: Path,
    ledger_run_prefixes: tuple[str, ...],
    vendors: list[str],
    concurrency: dict[str, int] | None = None,
    scratch_root: Path | None = None,
    code_commit: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    pause: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze one item, run the whole plan on it, and journal its cost row.

    ``pause`` names the models the evaluator must not call now. Their trials
    are held, so the row records the item as not complete and a later pass
    runs the trials that are missing.
    """
    manifest = build_eval_set(
        state_db=state_db,
        output_dir=sets_dir,
        population="list",
        k=int(authorization["k"]),
        item_ids=[item_id],
    )
    set_dir = sets_dir / manifest["eval_set_id"]
    if manifest["item_count"] != 1:
        reason = next(
            (
                row["reason"]
                for row in manifest["excluded"]
                if row.get("item_id") == item_id
            ),
            "not_in_the_frozen_set",
        )
        row = {
            "schema": "abstention-eval-cost-row-v1",
            "kind": SKIP_KIND,
            "item_id": item_id,
            "reason": reason,
            "eval_set_id": manifest["eval_set_id"],
            "recorded_at_utc": _utc_now(),
        }
        journal.append(row)
        return {"item_id": item_id, "skipped": True, "reason": reason, "row": row}
    _, items = load_eval_set(set_dir)
    item = {**items[0], "eval_set_id": manifest["eval_set_id"]}
    run_id = f"{authorization['run_id_prefix']}-{item_id}"
    gate_dir = gates_dir / item_id
    write_plan_gates(
        plan=plan,
        set_dir=set_dir,
        run_id=run_id,
        output_dir=gate_dir,
        review_record=authorization_file,
        review_verdict="pass",
        evaluation_policy_file=Path(authorization["evaluation_policy_file"]),
        evaluation_price_config_file=Path(
            authorization["evaluation_price_config_file"]
        ),
        subscription_models_file=Path(authorization["subscription_models_file"]),
        integrated_code_commit=authorization["integrated_code_commit"],
        vendors=vendors,
    )
    vendor_runs = build_vendor_runs(
        plan=plan,
        set_dir=set_dir,
        run_id=run_id,
        gate_dir=gate_dir,
        evaluation_policy_file=Path(authorization["evaluation_policy_file"]),
        subscription_models_file=Path(authorization["subscription_models_file"]),
        broker_factory=broker_factory,
        subscription_ledger_root=subscription_ledger_root,
        concurrency=concurrency,
        vendors=vendors,
        scratch_root=scratch_root,
    )
    run_dir = runs_dir / item_id
    started = time.monotonic()
    error: str | None = None
    try:
        summary = run_plan(
            set_dir=set_dir,
            output_dir=run_dir,
            run_id=run_id,
            plan=plan,
            vendor_runs=vendor_runs,
            code_commit=code_commit or authorization["integrated_code_commit"],
            progress=progress,
            gate_dir=gate_dir,
            pause=pause,
        )
    except Exception as failure:  # noqa: BLE001 - journalled, then reported
        error = f"{type(failure).__name__}: {failure}"
        summary_path = run_dir / PLAN_SUMMARY_FILENAME
        summary = (
            json.loads(summary_path.read_text(encoding="utf-8"))
            if summary_path.is_file()
            else {"vendors": {}, "recorded_trials": 0, "complete": False}
        )
    wall = time.monotonic() - started
    rows_by_vendor = {vendor: load_vendor_rows(run_dir, vendor) for vendor in vendors}
    ledger = read_ledger(shared_ledger_file)
    generation = generation_cost_record(
        ledger=ledger,
        state_db=state_db,
        campaign_id=str(authorization["campaign_id"]),
        family_id=str(item["family_id"]),
        run_prefixes=ledger_run_prefixes,
    )
    held = paused_models(pause)
    trials_per_model = 2 * len(plan["arms"]) * int(plan["repeats"])
    recorded_by_model: dict[str, int] = {}
    for rows in rows_by_vendor.values():
        for entry in rows:
            recorded_by_model[entry["model"]] = (
                recorded_by_model.get(entry["model"], 0) + 1
            )
    held_models = sorted(
        model
        for vendor in vendors
        for model in plan_models(plan, vendor)
        if model in held
    )
    pending_paused = sum(
        max(trials_per_model - recorded_by_model.get(model, 0), 0)
        for model in held_models
    )
    row = cost_row(
        item=item,
        run_id=run_id,
        plan_id=str(plan["plan_id"]),
        rows_by_vendor=rows_by_vendor,
        ledger=ledger,
        prices=prices,
        generation=generation,
        wall_seconds=wall,
        planned_trials=int(plan["trials_per_item"]),
        vendors_paused=[
            vendor for vendor in plan_vendors(plan) if vendor not in vendors
        ],
        models_paused=held_models,
        pending_paused_trials=pending_paused,
        cumulative=journal.cumulative(),
    )
    row["run_dir"] = str(run_dir)
    row["gate_dir"] = str(gate_dir)
    row["complete"] = (
        bool(summary.get("complete")) and error is None and pending_paused == 0
    )
    if error is not None:
        row["error"] = error
    journal.append(row)
    return {
        "item_id": item_id,
        "skipped": False,
        "row": row,
        "summary": summary,
        "error": error,
        "vendor_runs": vendor_runs,
        "run_dir": run_dir,
    }


def vendor_stop_reason(summary: dict[str, Any], vendor: str) -> str | None:
    """Return why one vendor stopped on this item, or None when it finished.

    A vendor stops on the first response that is not complete: a budget stop,
    a harness error, a timeout or an ambiguous charge. The policy forbids a
    retry, so the evaluator pauses that vendor for the rest of the watch and
    journals the reason. The other vendors keep running.
    """
    record = (summary.get("vendors") or {}).get(vendor) or {}
    if record.get("error"):
        return str(record["error"])
    stopped = record.get("stopped_on")
    if not stopped:
        return None
    state = str(stopped.get("state") or "stopped")
    detail = str(stopped.get("error") or "").strip()
    return f"{state}: {detail}" if detail else state


def is_item_scoped_reason(reason: str | None) -> bool:
    """Say whether one stop reason belongs to this item alone.

    The per-item repeat limit of the evaluation policy counts the calls of one
    item, condition, model and arm. It says nothing about the next item, so a
    vendor that meets it must keep running: the evaluator records the stop on
    this item and takes the next one. Every other stop pauses the vendor,
    because the policy forbids a retry and the next item would repeat it.

    The live service met this on 2026-09-16: a re-evaluated item exhausted its
    Gemini repeat budget, and the Gemini vendor was then paused for every
    later question, which is the arm the captain most wants measured.
    """
    return bool(reason) and EVALUATION_ITEM_REPEAT_REASON in str(reason)


def is_ambiguous_charge_reason(reason: str | None) -> bool:
    """Say whether one stop reason is an ambiguous shared-ledger charge.

    The broker books a paid call whose charge it cannot prove as an ambiguous
    charge and halts the evaluation phase. The reason the evaluator journals is
    the request state, with the provider detail after a colon when there is
    one, so the state is the first field.
    """
    if not reason:
        return False
    return str(reason).split(":", 1)[0].strip() == AMBIGUOUS_CHARGE_STATE


def is_ceiling_reason(reason: str | None) -> bool:
    """Return whether one stop reason is the evaluation budget wall."""
    if not reason:
        return False
    return (
        EVALUATION_CEILING_REASON in reason
        or "ceiling" in reason
        or "reserve" in reason
    )


# --- The loop --------------------------------------------------------------------


def watch(
    *,
    authorization_file: Path,
    plan_file: Path,
    contract_file: Path,
    evaluation_policy_file: Path,
    evaluation_price_config_file: Path,
    subscription_models_file: Path,
    state_db: Path,
    work_dir: Path,
    shared_ledger_file: Path,
    broker_factory: Callable[[Path], SharedGeminiBroker] | None,
    subscription_ledger_root: Path | None,
    list_price_file: Path = DEFAULT_LIST_PRICE_FILE,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    maximum_items: int | None = None,
    once: bool = False,
    backfill: bool = False,
    backfill_contract_file: Path | None = None,
    concurrency: dict[str, int] | None = None,
    vendors: list[str] | None = None,
    scratch_root: Path | None = None,
    code_commit: str | None = None,
    ledger_run_prefixes: tuple[str, ...] = (),
    pause_files: tuple[Path, ...] | list[Path] = (),
    pause_models: dict[str, Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    log: Callable[[dict[str, Any]], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    deadline_seconds: float | None = None,
    status_file: Path | None = None,
) -> dict[str, Any]:
    """Evaluate every accepted item as it appears, then wait for the next.

    ``once`` runs one pass and returns; without it the loop runs until the
    item bound, the deadline, or a stop signal. ``backfill`` also evaluates
    the items of ``backfill_contract_file`` (the chapter 2 contract), which
    is off by default. ``vendors`` restricts the plan to a subset, for
    example the subscription vendors while the Gemini arm waits for its own
    reviewed gate; the journal then lists the rest as paused.

    ``status_file`` is the supervisor's status file. A bound that ends the run
    is not an error, so it appends one ``blocked:`` line there. Without that
    line a bound is a silent stop: the exit code is 0 and the journal simply
    stops growing.

    ``pause_files`` are the paused-model files, re-read before every item, so
    an operator or a cost guard can pause or resume a model while the watcher
    runs. A later file wins for the same model, and ``pause_models`` holds the
    models the command line paused, which wins over every file. A paused
    model's trials are held, not recorded; the item is revisited after the
    resume time.
    """
    if not MINIMUM_POLL_SECONDS <= int(poll_seconds) <= MAXIMUM_POLL_SECONDS:
        raise ValueError(
            f"the poll interval must be from {MINIMUM_POLL_SECONDS} through "
            f"{MAXIMUM_POLL_SECONDS} seconds"
        )
    authorization = validate_authorization(
        authorization_file,
        plan_file=plan_file,
        contract_file=contract_file,
        evaluation_policy_file=evaluation_policy_file,
        evaluation_price_config_file=evaluation_price_config_file,
        subscription_models_file=subscription_models_file,
        code_commit=code_commit,
    )
    plan = load_plan(plan_file)
    prices = load_list_prices(list_price_file)
    price_config = json.loads(evaluation_price_config_file.read_text(encoding="utf-8"))
    work_dir.mkdir(parents=True, exist_ok=True)
    journal = CostJournal(work_dir)
    state_path = work_dir / WATCH_STATE_FILENAME
    state = _read_state(state_path)
    bound = min(int(authorization["maximum_items"]), int(maximum_items or 10**9))
    contracts = [load_contract(contract_file)]
    if backfill and backfill_contract_file is not None:
        contracts.append(load_contract(backfill_contract_file))
    wanted = set(vendors) - set(plan_vendors(plan)) if vendors else set()
    if wanted:
        raise ValueError(f"the plan has no vendor {sorted(wanted)[0]}")
    # A vendor pause is a circuit breaker for one invocation, and a start is an
    # operator action that says to try again. So a start clears the pauses the
    # last invocation left, and records which ones it cleared. A model pause is
    # not cleared: it lives in the pause files, which an operator or the cost
    # guard owns.
    cleared_vendor_pauses = dict(state["paused_vendors"])
    state["paused_vendors"] = {}
    active = [
        vendor for vendor in plan_vendors(plan) if not vendors or vendor in vendors
    ]
    if not active:
        raise ValueError("every vendor of the plan is excluded")
    # The vendors this invocation may run. A pause narrows `vendors`; a resume
    # restores one of them, and never a vendor `--vendors` excluded.
    authorized_vendors = list(active)
    vendors = active
    stop = {"now": False}

    def handle_signal(*_: Any) -> None:
        stop["now"] = True

    for name in ("SIGTERM", "SIGINT"):
        try:
            signal.signal(getattr(signal, name), handle_signal)
        except (ValueError, OSError, AttributeError):
            # Not the main thread, or no such signal. The loop still honours
            # `once`, the item bound and the deadline.
            pass

    evaluated: list[dict[str, Any]] = []
    errors: list[str] = []
    polls = 0
    started = clock()
    state["started_at_utc"] = _utc_now()

    def emit(event: dict[str, Any]) -> None:
        if log is not None:
            log({**event, "at": _utc_now()})

    def report_blocked(line: str) -> None:
        """Append one ``blocked:`` line for the supervisor of this unit.

        A bound ends the run with exit code 0, which no supervisor reads as a
        stop. The line is the one signal that says the benchmark needs an
        operator. A status file that cannot be written must not end the run,
        because the run is over already.
        """
        if status_file is None:
            return
        try:
            with Path(status_file).open("a", encoding="utf-8") as handle:
                handle.write(f"blocked: {line}\n")
        except OSError as error:  # pragma: no cover - environment
            emit({"event": "status_file_unwritable", "error": str(error)})

    def write_state() -> None:
        """Publish the watcher state: the poll count and the items so far."""
        atomic_json(
            state_path,
            {
                **state,
                "polls": polls,
                "evaluated_items": [row["item_id"] for row in evaluated],
                "active_vendors": vendors,
                "started_at_utc": state.get("started_at_utc") or _utc_now(),
                "updated_at_utc": _utc_now(),
            },
        )

    for vendor, record in sorted(cleared_vendor_pauses.items()):
        emit(
            {
                "event": "vendor_pause_cleared",
                "vendor": vendor,
                "reason": (record or {}).get("reason"),
            }
        )

    while True:
        polls += 1
        # A vendor paused for an ambiguous charge waits on a supervisor
        # release, not on a restart: the broker keeps the reservation and
        # halts the evaluation phase until a reviewed continuation releases
        # it. So the evaluator asks the shared ledger on every poll whether
        # the evaluation phase can call again, and resumes the vendor itself.
        # Only the shared-ledger vendor is covered, because that ledger is the
        # record that proves the release.
        if (
            PROVIDER_GOOGLE_GEMINI in authorized_vendors
            and PROVIDER_GOOGLE_GEMINI not in vendors
            and is_ambiguous_charge_reason(
                (state["paused_vendors"].get(PROVIDER_GOOGLE_GEMINI) or {}).get(
                    "reason"
                )
            )
            and phase_halt_reason(read_ledger(shared_ledger_file), EVALUATION_PHASE)
            is None
        ):
            paused_reason = str(
                state["paused_vendors"][PROVIDER_GOOGLE_GEMINI]["reason"]
            )
            del state["paused_vendors"][PROVIDER_GOOGLE_GEMINI]
            running = set(vendors) | {PROVIDER_GOOGLE_GEMINI}
            vendors = [name for name in authorized_vendors if name in running]
            atomic_json(state_path, {**state, "updated_at_utc": _utc_now()})
            journal.append(
                resume_row(
                    run_id=str(authorization["run_id_prefix"]),
                    vendor=PROVIDER_GOOGLE_GEMINI,
                    paused_reason=paused_reason,
                )
            )
            emit(
                {
                    "event": "vendor_resumed",
                    "vendor": PROVIDER_GOOGLE_GEMINI,
                    "paused_reason": paused_reason,
                    "reason": "the released ambiguous charge no longer halts "
                    "the evaluation phase",
                }
            )
        # An item whose only missing trials belong to a model that is still
        # paused cannot advance: a revisit records nothing, calls nothing and
        # appends one more journal row. So it waits here until the pause
        # lifts, and the evaluator spends its poll on the items that can move.
        held_now = paused_models(
            merge_pause(*(load_pause(path) for path in pause_files), pause_models)
        )
        done = (
            journal.completed_item_ids()
            | journal.items_held_by(held_now)
            | set(
                row["item_id"] for row in journal.rows() if row.get("kind") == SKIP_KIND
            )
        )
        pending: list[str] = []
        for index, contract in enumerate(contracts):
            pending.extend(
                pending_item_ids(
                    state_db=state_db,
                    campaign_id=str(authorization["campaign_id"])
                    if index == 0
                    else None,
                    contract=contract,
                    exclude=done | set(pending),
                )
            )
        remaining_bound = bound - len(journal.latest_item_rows())
        if remaining_bound <= 0:
            emit({"event": "item_bound_reached", "bound": bound})
            report_blocked(
                f"the streaming evaluator met its item bound of {bound} items "
                "and stopped; a larger run needs a new reviewed authorization"
            )
            break
        pending = pending[:remaining_bound]
        if not pending:
            emit({"event": "idle", "poll": polls, "evaluated": len(evaluated)})
        for item_id in pending:
            if stop["now"]:
                break
            # Re-read the pause file before every item: a cost guard can pause
            # a model at any moment, and a resume time can pass mid-run.
            pause = merge_pause(
                *(load_pause(path) for path in pause_files),
                pause_models,
            )
            held_now = paused_models(pause)
            if held_now:
                emit(
                    {
                        "event": "models_paused",
                        "item_id": item_id,
                        "models": sorted(held_now),
                    }
                )
            if PROVIDER_GOOGLE_GEMINI in vendors and broker_factory is not None:
                # `ceiling_pause` is the vendor pause of a budget bound. It is
                # not the model pause above, and it must never overwrite it.
                ceiling_pause = _ceiling_precheck(
                    authorization=authorization,
                    plan=plan,
                    price_config=price_config,
                    journal=journal,
                    shared_ledger_file=shared_ledger_file,
                )
                if ceiling_pause is not None:
                    vendors = [v for v in vendors if v != PROVIDER_GOOGLE_GEMINI]
                    state["paused_vendors"][PROVIDER_GOOGLE_GEMINI] = ceiling_pause
                    atomic_json(state_path, {**state, "updated_at_utc": _utc_now()})
                    journal.append(
                        pause_row(
                            run_id=str(authorization["run_id_prefix"]),
                            vendor=PROVIDER_GOOGLE_GEMINI,
                            reason=ceiling_pause["reason"],
                            remaining_usd=ceiling_pause.get("remaining_usd"),
                        )
                    )
                    emit(
                        {
                            "event": "vendor_paused",
                            "vendor": PROVIDER_GOOGLE_GEMINI,
                            **ceiling_pause,
                        }
                    )
                    # The Gemini arm is the arm the USD allocation pays for.
                    # A budget bound turns it off while the subscription
                    # vendors keep the run looking healthy, so the supervisor
                    # must hear about it here and not at the exit.
                    report_blocked(
                        "the streaming evaluator paused the Gemini vendor on a "
                        f"budget bound: {ceiling_pause['reason']}"
                    )
            emit(
                {"event": "item_started", "item_id": item_id, "vendors": list(vendors)}
            )
            result = evaluate_item(
                item_id=item_id,
                authorization=authorization,
                plan=plan,
                state_db=state_db,
                sets_dir=work_dir / "sets",
                runs_dir=work_dir / "runs",
                gates_dir=work_dir / "gates",
                journal=journal,
                prices=prices,
                shared_ledger_file=shared_ledger_file,
                broker_factory=broker_factory,
                subscription_ledger_root=subscription_ledger_root,
                authorization_file=authorization_file,
                ledger_run_prefixes=ledger_run_prefixes,
                vendors=list(vendors),
                concurrency=concurrency,
                scratch_root=scratch_root,
                code_commit=code_commit,
                progress=progress,
                pause=pause,
            )
            evaluated.append(result)
            if result["skipped"]:
                emit(
                    {
                        "event": "item_skipped",
                        "item_id": item_id,
                        "reason": result["reason"],
                    }
                )
                continue
            emit(
                {
                    "event": "item_done",
                    "item_id": item_id,
                    "complete": result["row"]["complete"],
                    "models_paused": result["row"]["evaluation"]["models_paused"],
                    "pending_paused_trials": result["row"]["evaluation"][
                        "pending_paused_trials"
                    ],
                    "gemini_usd": result["row"]["evaluation"]["google_gemini"]["usd"],
                    "wall_seconds": result["row"]["evaluation"]["wall_seconds"],
                }
            )
            # A vendor that stopped on this item is paused for the rest of the
            # watch, whatever the reason: the policy forbids a retry, so the
            # next item repeats the same stop. The other vendors run on. The
            # one exception is an item-scoped stop, which says nothing about
            # the next item.
            for vendor in list(vendors):
                reason = vendor_stop_reason(result["summary"], vendor)
                if reason is None:
                    continue
                if is_item_scoped_reason(reason):
                    emit(
                        {
                            "event": "vendor_stopped_on_this_item",
                            "vendor": vendor,
                            "item_id": item_id,
                            "reason": reason,
                        }
                    )
                    continue
                vendors = [name for name in vendors if name != vendor]
                state["paused_vendors"][vendor] = {"reason": reason}
                atomic_json(state_path, {**state, "updated_at_utc": _utc_now()})
                journal.append(
                    pause_row(
                        run_id=result["row"]["run_id"], vendor=vendor, reason=reason
                    )
                )
                emit(
                    {
                        "event": "vendor_paused",
                        "vendor": vendor,
                        "reason": reason,
                        "budget": is_ceiling_reason(reason),
                    }
                )
            if not vendors:
                emit({"event": "every_vendor_paused"})
                report_blocked(
                    "the streaming evaluator stopped because every vendor is "
                    "paused: "
                    + ", ".join(
                        f"{name} ({(record or {}).get('reason')})"
                        for name, record in sorted(state["paused_vendors"].items())
                    )
                )
                break
            if result["error"]:
                # run_plan raised: an error outside the recorded responses.
                errors.append(f"{item_id}: {result['error']}")
                break
        # Publish the state after every poll cycle, not only at the end. A
        # long-running unit never reaches the end, so an operator reading
        # `watch-state.json` must see the poll count rise and the items grow
        # while it runs.
        write_state()
        if errors or stop["now"] or once:
            break
        if deadline_seconds is not None and clock() - started >= deadline_seconds:
            emit({"event": "deadline_reached", "seconds": round(clock() - started, 1)})
            break
        sleep(float(poll_seconds))

    write_state()
    summary = summarize_journal(work_dir)
    result = {
        "schema": "abstention-streaming-watch-result-v1",
        "authorization_file": str(authorization_file),
        "plan_id": plan["plan_id"],
        "work_dir": str(work_dir),
        "journal_file": str(journal.path),
        "polls": polls,
        "items_this_invocation": [
            {
                "item_id": row["item_id"],
                "skipped": row["skipped"],
                "complete": row["row"].get("complete") if not row["skipped"] else None,
            }
            for row in evaluated
        ],
        "active_vendors": vendors,
        "paused_vendors": state["paused_vendors"],
        "paused_models": sorted(
            paused_models(
                merge_pause(
                    *(load_pause(path) for path in pause_files),
                    pause_models,
                )
            )
        ),
        "stopped_by_signal": stop["now"],
        "errors": errors,
        "summary": summary,
    }
    if errors:
        raise RuntimeError("; ".join(errors))
    return result


def _ceiling_precheck(
    *,
    authorization: dict[str, Any],
    plan: dict[str, Any],
    price_config: dict[str, Any],
    journal: CostJournal,
    shared_ledger_file: Path,
) -> dict[str, Any] | None:
    """Return a pause record when one more item would pass a budget bound."""
    ledger = read_ledger(shared_ledger_file)
    policy = json.loads(
        Path(authorization["evaluation_policy_file"]).read_text(encoding="utf-8")
    )
    ceiling = Decimal(str(policy["evaluation_ceiling_usd"]))
    used = Decimal("0")
    for request in ledger["requests"].values():
        if request.get("phase") != "benchmark_evaluation":
            continue
        state = request.get("state")
        if state == "completed":
            used += Decimal(str(request.get("actual_cost_usd") or "0"))
        elif state in {"submitted", "ambiguous_charge", "orphaned_no_replay"}:
            used += Decimal(str(request.get("reserved_usd") or "0"))
    models = plan_models(plan, PROVIDER_GOOGLE_GEMINI)
    estimate = estimated_gemini_item_usd(
        price_config,
        models,
        list(plan["arms"]),
        int(plan["repeats"]),
        output_cap=int(policy["maximum_output_tokens_including_thinking"]),
    )
    authorized = Decimal(str(authorization["maximum_gemini_usd"]))
    journal_used = Decimal(str(journal.cumulative()["evaluation_gemini_usd"]))
    if journal_used + estimate * CEILING_SAFETY > authorized:
        return {
            "reason": "one more item would pass the authorized Gemini USD bound",
            "authorized_usd": str(authorized),
            "journal_usd": str(journal_used),
            "estimate_usd": str(estimate),
        }
    if used + estimate * CEILING_SAFETY > ceiling:
        return {
            "reason": EVALUATION_CEILING_REASON,
            "ceiling_usd": str(ceiling),
            "remaining_usd": str(ceiling - used),
            "estimate_usd": str(estimate),
        }
    return None


__all__ = [
    "AUTHORIZATION_SCHEMA",
    "DEFAULT_POLL_SECONDS",
    "GATE_FILENAME_BY_VENDOR",
    "SKIP_KIND",
    "WATCH_STATE_FILENAME",
    "authorization_record",
    "is_ambiguous_charge_reason",
    "is_ceiling_reason",
    "estimated_gemini_item_usd",
    "evaluate_item",
    "pending_item_ids",
    "validate_authorization",
    "vendor_stop_reason",
    "watch",
]
