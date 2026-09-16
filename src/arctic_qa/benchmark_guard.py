"""Cost and quota guard of the live benchmark evaluation.

The streaming evaluator (`src/arctic_qa/abstention_watch.py`) scores every newly
accepted chapter 3 question on eight models. Three vendors pay for that work in
three different currencies:

- Google Gemini bills the captain's API credits through the shared paid-call
  ledger, under the phase `benchmark_evaluation`. The captain allocated
  USD 200 for it.
- Claude Code bills the claude.ai subscription. The bound is the 5-hour session
  window of the account, and one extra weekly window for Fable.
- Codex bills the ChatGPT subscription. The bound is the weekly window.

This module reads those three meters plus the evaluator's own records every few
minutes, writes one guard-state JSON and one append-only guard log, and pauses
one model at a time when a bound is urgent. It never kills a process. It pauses
through the evaluator's documented switch: the file
`benchmark-evaluation-model-pause-v1`, which the evaluator reads before it
dispatches a trial. A paused model's trials stay pending and run later.

Nothing here makes a paid model call. Every input is a file on disk or the
read-only `quota-axi` report.

Captain's order of 2026-09-16: "make sure that the benchmark does not get too
expensive for my budget (I will allocate $200 for benchmarking the gemini
models) vis a vis gemini budget, claude session/weekly usage (dont worry about
the weekly usage except for fable), codex weekly usage. If one is rising too
fast, then pause the benchmarking for only that model. This should be rare and
only happen if there are very urget issues with the benchmarking cost."
"""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .abstention_render import TAXONOMY
from .abstention_score import metrics_from_counts
from .model_broker import EVALUATION_PHASE, EVALUATION_STAGE_PREFIX
from .util import atomic_json


GUARD_STATE_SCHEMA = "benchmark-guard-state-v1"
GUARD_LOG_SCHEMA = "benchmark-guard-log-v1"
PAUSE_SCHEMA = "benchmark-evaluation-model-pause-v1"
GUARD_OWNER = "benchmark-cost-guard"

GUARD_STATE_FILENAME = "guard-state.json"
GUARD_LOG_FILENAME = "guard-log.jsonl"
JOURNAL_FILENAME = "cost-journal.jsonl"
WATCH_STATE_FILENAME = "watch-state.json"

VENDOR_GOOGLE_GEMINI = "google_gemini"
VENDOR_ANTHROPIC_CLAUDE_CODE = "anthropic_claude_code"
VENDOR_OPENAI_CODEX = "openai_codex"
VENDOR_ORDER = (
    VENDOR_GOOGLE_GEMINI,
    VENDOR_ANTHROPIC_CLAUDE_CODE,
    VENDOR_OPENAI_CODEX,
)
VENDOR_LABELS = {
    VENDOR_GOOGLE_GEMINI: "Google Gemini (API credits)",
    VENDOR_ANTHROPIC_CLAUDE_CODE: "Claude Code (subscription)",
    VENDOR_OPENAI_CODEX: "Codex (subscription)",
}
MODEL_VENDOR_PREFIXES = (
    ("gemini-", VENDOR_GOOGLE_GEMINI),
    ("claude-", VENDOR_ANTHROPIC_CLAUDE_CODE),
    ("gpt-", VENDOR_OPENAI_CODEX),
)

FABLE_MODEL = "claude-fable-5-1"

# The eight models of the captain's evaluation plan of 2026-09-16. A rule may
# pause only a model the evaluator actually runs, so this list, not the ledger,
# names the candidates. `--plan-file` overrides it from
# `config/benchmark-evaluation-plan-high-v1.json` when that file is available.
PLAN_MODELS_BY_VENDOR = {
    VENDOR_GOOGLE_GEMINI: ("gemini-3.8-flash", "gemini-3.7-flash"),
    VENDOR_ANTHROPIC_CLAUDE_CODE: (
        FABLE_MODEL,
        "claude-opus-5",
        "claude-sonnet-5",
    ),
    VENDOR_OPENAI_CODEX: ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"),
}

# The captain's thresholds. A rule fires only on an urgent condition, because
# the captain asked for a pause to be rare.
GEMINI_BUDGET_USD = Decimal("200.00")
GEMINI_CEILING_MARGIN_USD = Decimal("10.00")
CLAUDE_SESSION_FLOOR_PERCENT = Decimal("15")
FABLE_WEEKLY_FLOOR_PERCENT = Decimal("10")
CODEX_WEEKLY_FLOOR_PERCENT = Decimal("10")

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_EVALUATOR_STALE_SECONDS = 900
QUOTA_COMMAND = ("quota-axi", "--json", "--full")
QUOTA_TIMEOUT_SECONDS = 60
CENT = Decimal("0.000001")


# --- Small helpers --------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    """Read one ISO timestamp; return None when the value is absent or broken."""
    if value in (None, "", "none", "unknown"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except (ArithmeticError, ValueError):
        return Decimal("0")


def _quantize(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(CENT))


def vendor_of_model(model: str) -> str | None:
    """Return the vendor that bills one evaluated model."""
    for prefix, vendor in MODEL_VENDOR_PREFIXES:
        if model.startswith(prefix):
            return vendor
    return None


def plan_models(plan: dict[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """Return the models of each vendor: the plan file, or the captain's plan."""
    vendors = (plan or {}).get("vendors")
    if not isinstance(vendors, dict) or not vendors:
        return dict(PLAN_MODELS_BY_VENDOR)
    result = {}
    for vendor in VENDOR_ORDER:
        models = (vendors.get(vendor) or {}).get("models")
        result[vendor] = (
            tuple(str(model) for model in models)
            if isinstance(models, list) and models
            else PLAN_MODELS_BY_VENDOR[vendor]
        )
    return result


# --- Reading the evaluator's records --------------------------------------------


def read_journal_rows(journal_dir: Path) -> list[dict[str, Any]]:
    """Read the streaming cost journal: one row per evaluated question."""
    path = Path(journal_dir) / JOURNAL_FILENAME
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def item_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the question rows; drop the vendor-pause rows of the evaluator."""
    return [row for row in rows if row.get("kind", "item") == "item"]


def pause_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the vendor-pause rows that the evaluator itself journalled."""
    return [row for row in rows if row.get("kind") == "vendor_pause"]


def read_watch_state(journal_dir: Path) -> dict[str, Any] | None:
    """Read the evaluator's watch state, or None when it never ran."""
    path = Path(journal_dir) / WATCH_STATE_FILENAME
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def read_ledger(path: Path) -> dict[str, Any]:
    """Read the shared paid-call ledger read-only, under its own shared lock."""
    path = Path(path)
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        handle = lock_path.open("a+")
    except OSError:
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        fcntl.flock(handle, fcntl.LOCK_SH)
        return json.loads(path.read_text(encoding="utf-8"))
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _empty_tokens() -> dict[str, int]:
    return {"input": 0, "output": 0, "thinking": 0}


def evaluation_phase_totals(ledger: dict[str, Any]) -> dict[str, Any]:
    """Sum the `benchmark_evaluation` liabilities of the shared ledger.

    The broker counts a reservation and an ambiguous charge against the
    evaluation ceiling, so the guard counts them too: `used_usd` is what the
    ceiling sees.
    """
    spent = Decimal("0")
    reserved = Decimal("0")
    ambiguous = Decimal("0")
    calls = 0
    tokens = _empty_tokens()
    by_model: dict[str, dict[str, Any]] = {}
    items: set[str] = set()
    items_by_model: dict[str, set[str]] = {}
    for request in (ledger.get("requests") or {}).values():
        if request.get("phase") != EVALUATION_PHASE:
            continue
        state = request.get("state")
        stage = str(request.get("stage") or "")
        model = (
            stage[len(EVALUATION_STAGE_PREFIX) :]
            if stage.startswith(EVALUATION_STAGE_PREFIX)
            else str(request.get("model") or "unknown")
        )
        entry = by_model.setdefault(
            model,
            {
                "model": model,
                "charged_calls": 0,
                "spent_usd": Decimal("0"),
                "reserved_usd": Decimal("0"),
                "ambiguous_usd": Decimal("0"),
                "tokens": _empty_tokens(),
            },
        )
        family = str(request.get("family_id") or "")
        if family.startswith("evaluation-item:"):
            item = family.removeprefix("evaluation-item:")
            items.add(item)
            items_by_model.setdefault(model, set()).add(item)
        if state == "completed":
            cost = _money(request.get("actual_cost_usd"))
            spent += cost
            entry["spent_usd"] += cost
            calls += 1
            entry["charged_calls"] += 1
            usage = request.get("usage") or {}
            for name, key in (
                ("input", "promptTokenCount"),
                ("output", "candidatesTokenCount"),
                ("thinking", "thoughtsTokenCount"),
            ):
                value = int(usage.get(key, 0) or 0)
                tokens[name] += value
                entry["tokens"][name] += value
        elif state in {"submitted", "orphaned_no_replay"}:
            value = _money(request.get("reserved_usd"))
            reserved += value
            entry["reserved_usd"] += value
        elif state == "ambiguous_charge":
            value = _money(request.get("reserved_usd"))
            ambiguous += value
            entry["ambiguous_usd"] += value
    models = {}
    for model, entry in sorted(by_model.items()):
        used = entry["spent_usd"] + entry["reserved_usd"] + entry["ambiguous_usd"]
        models[model] = {
            "model": model,
            "charged_calls": entry["charged_calls"],
            "spent_usd": _quantize(entry["spent_usd"]),
            "reserved_usd": _quantize(entry["reserved_usd"]),
            "ambiguous_usd": _quantize(entry["ambiguous_usd"]),
            "used_usd": _quantize(used),
            "tokens": entry["tokens"],
            "questions": len(items_by_model.get(model, ())),
        }
    return {
        "charged_calls": calls,
        "spent_usd": _quantize(spent),
        "reserved_usd": _quantize(reserved),
        "ambiguous_usd": _quantize(ambiguous),
        "used_usd": _quantize(spent + reserved + ambiguous),
        "tokens": tokens,
        "questions": len(items),
        "by_model": models,
    }


def construction_totals(ledger: dict[str, Any]) -> dict[str, Any]:
    """Return the construction spend: the ledger total minus the evaluation phase.

    `AGENTS.md` states this baseline: "The construction baseline is the ledger's
    `spent_usd` minus the evaluation-stage spend."
    """
    evaluation = evaluation_phase_totals(ledger)
    total = _money(ledger.get("spent_usd"))
    spent = total - _money(evaluation["spent_usd"])
    return {
        "ledger_spent_usd": _quantize(total),
        "evaluation_spent_usd": evaluation["spent_usd"],
        "construction_spent_usd": _quantize(spent),
        "accepted_question_count": int(ledger.get("accepted_question_count") or 0),
    }


# --- Reading the quota report ----------------------------------------------------


def read_quota_report(
    *, command: tuple[str, ...] = QUOTA_COMMAND, recorded_file: Path | None = None
) -> dict[str, Any]:
    """Return the `quota-axi --json --full` report, live or from a recording."""
    if recorded_file is not None:
        return json.loads(Path(recorded_file).read_text(encoding="utf-8"))
    finished = subprocess.run(  # noqa: S603 - a fixed read-only local command
        list(command),
        capture_output=True,
        text=True,
        timeout=QUOTA_TIMEOUT_SECONDS,
        check=True,
    )
    return json.loads(finished.stdout)


def quota_windows(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten the quota report to the four windows this guard bounds.

    The keys are `claude_session` (the 5-hour window that bounds every Claude
    model), `claude_week`, `claude_fable` (the extra Fable weekly window) and
    `codex_weekly`. A window the report does not carry is absent.
    """
    wanted = {
        ("claude", "five_hour"): "claude_session",
        ("claude", "seven_day"): "claude_week",
        ("claude", "model:fable"): "claude_fable",
        ("codex", "weekly"): "codex_weekly",
    }
    result: dict[str, dict[str, Any]] = {}
    for provider in report.get("providers") or []:
        name = provider.get("provider")
        for window in provider.get("windows") or []:
            key = wanted.get((name, window.get("id")))
            if key is None:
                continue
            pace = window.get("pace") or {}
            result[key] = {
                "provider": name,
                "window_id": window.get("id"),
                "label": window.get("label"),
                "percent_remaining": window.get("percentRemaining"),
                "resets_at_utc": window.get("resetsAt"),
                "projected_exhausted_at_utc": pace.get("projectedExhaustedAt"),
                "pace": pace.get("status"),
                "burn_multiple": pace.get("burnMultiple"),
                "projection_confidence": pace.get("projectionConfidence"),
            }
    result["generated_at_utc"] = report.get("generatedAt")
    return result


def _percent(windows: dict[str, Any], key: str) -> Decimal | None:
    window = windows.get(key)
    if not isinstance(window, dict):
        return None
    value = window.get("percent_remaining")
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


# --- Extrapolation arithmetic ----------------------------------------------------


def expected_questions(
    *,
    accepted_now: int,
    construction_allocation_usd: Decimal,
    construction_spent_usd: Decimal,
    questions_evaluated: int,
    accepted_question_target: int | None = None,
) -> dict[str, Any]:
    """Project how many questions the chapter 3 run will still produce.

    The captain's rule: the questions already accepted in the production ledger,
    plus the chapter 3 allocation's remaining USD divided by the run's cost per
    accepted item. The construction policy also stops the run at its accepted
    question target, so the smaller of the two bounds wins.
    `questions_still_expected` is what the evaluator has not evaluated yet, so it
    never counts a finished question twice.
    """
    accepted = max(int(accepted_now), 0)
    remaining_usd = construction_allocation_usd - construction_spent_usd
    if remaining_usd < 0:
        remaining_usd = Decimal("0")
    per_item = construction_spent_usd / Decimal(accepted) if accepted else None
    further = int(remaining_usd / per_item) if per_item and per_item > 0 else 0
    money_total = accepted + further
    total = money_total
    bound = "allocation"
    if accepted_question_target and accepted_question_target < money_total:
        total = max(int(accepted_question_target), accepted)
        bound = "accepted_question_target"
    still = max(total - max(int(questions_evaluated), 0), 0)
    return {
        "accepted_now": accepted,
        "construction_allocation_usd": _quantize(construction_allocation_usd),
        "construction_spent_usd": _quantize(construction_spent_usd),
        "construction_remaining_usd": _quantize(remaining_usd),
        "usd_per_accepted_item": _quantize(per_item),
        "further_accepted_items": further,
        "allocation_total_questions": money_total,
        "accepted_question_target": accepted_question_target,
        "binding_bound": bound,
        "expected_total_questions": total,
        "questions_evaluated": max(int(questions_evaluated), 0),
        "questions_still_expected": still,
    }


def extrapolate(
    *,
    spend_so_far: Decimal,
    questions_evaluated: int,
    questions_still_expected: int,
) -> dict[str, Any]:
    """Project one meter forward at its own observed cost per question."""
    evaluated = max(int(questions_evaluated), 0)
    still = max(int(questions_still_expected), 0)
    per_question = spend_so_far / Decimal(evaluated) if evaluated else None
    total = (
        None if per_question is None else spend_so_far + per_question * Decimal(still)
    )
    return {
        "spend_so_far_usd": _quantize(spend_so_far),
        "questions_evaluated": evaluated,
        "questions_still_expected": still,
        "usd_per_question": _quantize(per_question),
        "extrapolated_total_usd": _quantize(total),
    }


# --- Per-model readings ----------------------------------------------------------


def _counts_template() -> dict[str, int]:
    return {name: 0 for name in TAXONOMY}


def model_readings(
    rows: list[dict[str, Any]],
    evaluation: dict[str, Any],
    models_by_vendor: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build one reading per evaluated model from the journal and the ledger.

    Gemini money comes from the shared ledger, which is the authority that the
    broker bounds. The subscription vendors bill USD 0, so their meter is the
    token count and its informational list-price equivalent, both of which the
    journal records per model.

    Every model of the plan gets a row, even before its first trial. A model
    that only the ledger knows - an earlier canary, for example - also gets a
    row, marked `in_plan: false`, because its spend still counts against the
    evaluation budget.
    """
    readings: dict[str, dict[str, Any]] = {}
    plan = models_by_vendor or dict(PLAN_MODELS_BY_VENDOR)
    planned = {model for models in plan.values() for model in models}

    def reading(model: str) -> dict[str, Any]:
        return readings.setdefault(
            model,
            {
                "model": model,
                "vendor": vendor_of_model(model),
                "in_plan": model in planned,
                "questions_evaluated": 0,
                "trials": 0,
                "counts": _counts_template(),
                "tokens": _empty_tokens(),
                "calls": 0,
                "list_price_equivalent_usd": Decimal("0"),
                "gemini_used_usd": None,
            },
        )

    for vendor in VENDOR_ORDER:
        for model in plan.get(vendor) or ():
            reading(model)

    for row in rows:
        for model, counts in (row.get("outcomes_by_model") or {}).items():
            entry = reading(model)
            entry["questions_evaluated"] += 1
            for name, value in counts.items():
                entry["counts"][name] = entry["counts"].get(name, 0) + int(value)
                entry["trials"] += int(value)
        subscription = ((row.get("evaluation") or {}).get("subscription")) or {}
        for vendor_record in subscription.values():
            for model, usage in (vendor_record.get("by_model") or {}).items():
                entry = reading(model)
                entry["calls"] += int(usage.get("calls") or 0)
                entry["list_price_equivalent_usd"] += _money(
                    usage.get("list_price_equivalent_usd")
                )
                for name in ("input", "output", "thinking"):
                    entry["tokens"][name] += int(
                        (usage.get("tokens") or {}).get(name, 0) or 0
                    )

    for model, record in (evaluation.get("by_model") or {}).items():
        entry = reading(model)
        entry["gemini_used_usd"] = _money(record.get("used_usd"))
        entry["calls"] = max(entry["calls"], int(record.get("charged_calls") or 0))
        if not entry["questions_evaluated"]:
            entry["questions_evaluated"] = int(record.get("questions") or 0)
        for name in ("input", "output", "thinking"):
            entry["tokens"][name] = max(
                entry["tokens"][name],
                int((record.get("tokens") or {}).get(name, 0) or 0),
            )
    return readings


def _cost_per_question(entry: dict[str, Any]) -> Decimal | None:
    """The per-question meter that ranks a model inside its vendor.

    Gemini ranks by real USD. A subscription model has no USD, so it ranks by
    the list-price equivalent of its tokens, which is the only per-model
    quantity that tracks how hard it leans on the shared quota.
    """
    evaluated = entry.get("questions_evaluated") or 0
    if not evaluated:
        return None
    if entry.get("gemini_used_usd") is not None:
        return entry["gemini_used_usd"] / Decimal(evaluated)
    equivalent = entry.get("list_price_equivalent_usd")
    if equivalent is None:
        return None
    return Decimal(equivalent) / Decimal(evaluated)


def render_model_reading(
    entry: dict[str, Any], *, questions_still_expected: int
) -> dict[str, Any]:
    """Return the public, JSON-safe reading of one model."""
    counts = entry["counts"]
    scored = sum(counts[name] for name in TAXONOMY if name != "N0")
    metrics = metrics_from_counts(counts) if scored else None
    per_question = _cost_per_question(entry)
    gemini = entry.get("gemini_used_usd")
    projection = (
        extrapolate(
            spend_so_far=gemini,
            questions_evaluated=entry["questions_evaluated"],
            questions_still_expected=questions_still_expected,
        )
        if gemini is not None
        else None
    )
    trials = entry["trials"]
    return {
        "model": entry["model"],
        "vendor": entry["vendor"],
        "vendor_label": VENDOR_LABELS.get(entry["vendor"] or "", "unknown vendor"),
        "in_plan": bool(entry.get("in_plan", True)),
        "questions_evaluated": entry["questions_evaluated"],
        "trials": trials,
        "counts": dict(counts),
        "metrics": metrics,
        "invalid_count": counts.get("N0", 0),
        "invalid_rate": round(counts.get("N0", 0) / trials, 4) if trials else None,
        "calls": entry["calls"],
        "tokens": dict(entry["tokens"]),
        "list_price_equivalent_usd": _quantize(
            Decimal(entry["list_price_equivalent_usd"])
        ),
        "gemini_used_usd": _quantize(gemini) if gemini is not None else None,
        "cost_per_question_usd": _quantize(per_question),
        "cost_per_question_basis": (
            "gemini_api_credits" if gemini is not None else "subscription_list_price"
        ),
        "extrapolation": projection,
    }


# --- The pause switch ------------------------------------------------------------


def read_pause_file(path: Path) -> dict[str, Any]:
    """Read the evaluator's paused-model file; an absent file pauses nothing."""
    path = Path(path)
    if not path.is_file():
        return {"schema": PAUSE_SCHEMA, "paused_models": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != PAUSE_SCHEMA:
        raise ValueError("unsupported benchmark evaluation model-pause schema")
    if not isinstance(value.get("paused_models"), dict):
        raise ValueError("the model-pause file has no paused_models block")
    return value


def active_pauses(
    pause: dict[str, Any], *, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Return the entries that pause a model at `now`.

    An entry with no resume time holds until an operator or this guard removes
    it. An entry whose resume time has passed no longer pauses, which is how the
    captain's Fable pause lets Fable restart by itself at 23:00 UTC.
    """
    moment = now or _utc_now()
    result = {}
    for model, entry in (pause.get("paused_models") or {}).items():
        resume = parse_utc((entry or {}).get("resume_at_utc"))
        if resume is None or resume > moment:
            result[model] = dict(entry or {})
    return result


def write_pause_file(path: Path, pause: dict[str, Any]) -> None:
    """Write the paused-model file atomically, keeping its documented shape."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_json(
        Path(path),
        {
            "schema": PAUSE_SCHEMA,
            "config_id": pause.get("config_id") or "arctic-abstention-model-pause-v1",
            "purpose": pause.get("purpose")
            or (
                "Models the evaluator must not call for now. A paused model's "
                "trials stay pending: they are not recorded, not counted as "
                "invalid, and they run after the pause is lifted."
            ),
            "paused_models": dict(sorted((pause.get("paused_models") or {}).items())),
        },
    )


# --- The rules -------------------------------------------------------------------


def _finding(
    rule: str,
    *,
    fired: bool,
    vendor: str,
    detail: str,
    numbers: dict[str, Any],
    candidates: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "rule": rule,
        "vendor": vendor,
        "fired": bool(fired),
        "detail": detail,
        "numbers": numbers,
        "candidate_models": list(candidates),
    }


def evaluate_rules(
    *,
    gemini: dict[str, Any],
    evaluation_ceiling_usd: Decimal | None,
    evaluation_used_usd: Decimal,
    windows: dict[str, Any],
    gemini_budget_usd: Decimal = GEMINI_BUDGET_USD,
    fable_rule_active_after: datetime | None = None,
    benchmark_drives_codex: bool = False,
    models_by_vendor: dict[str, tuple[str, ...]] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Apply the captain's six urgent conditions and return one finding each.

    A finding names the models the rule may pause. Nothing else in this module
    decides when to pause: the caller pauses the costliest candidate of a fired
    rule, one model at a time.
    """
    moment = now or _utc_now()
    models_of = models_by_vendor or dict(PLAN_MODELS_BY_VENDOR)
    findings: list[dict[str, Any]] = []

    extrapolated = gemini.get("extrapolated_total_usd")
    total = None if extrapolated is None else _money(extrapolated)
    findings.append(
        _finding(
            "gemini_extrapolated_over_budget",
            fired=total is not None and total > gemini_budget_usd,
            vendor=VENDOR_GOOGLE_GEMINI,
            detail=(
                "the extrapolated Gemini total of the whole chapter 3 run "
                f"exceeds the allocated USD {gemini_budget_usd}"
            ),
            numbers={
                "extrapolated_total_usd": extrapolated,
                "budget_usd": str(gemini_budget_usd),
                "spend_so_far_usd": gemini.get("spend_so_far_usd"),
                "usd_per_question": gemini.get("usd_per_question"),
                "questions_still_expected": gemini.get("questions_still_expected"),
            },
            candidates=models_of[VENDOR_GOOGLE_GEMINI],
        )
    )

    margin = (
        None
        if evaluation_ceiling_usd is None
        else evaluation_ceiling_usd - evaluation_used_usd
    )
    # A warning band of USD 10 needs a ceiling that is large against it. Under a
    # small ceiling the band covers the whole budget and the rule would fire on
    # the first call, which is not urgent. The evaluator's own ceiling precheck
    # already pauses the Gemini vendor before it passes a small ceiling.
    margin_applies = (
        evaluation_ceiling_usd is not None
        and evaluation_ceiling_usd >= 2 * GEMINI_CEILING_MARGIN_USD
    )
    findings.append(
        _finding(
            "gemini_evaluation_ceiling_margin",
            fired=margin_applies
            and margin is not None
            and margin < GEMINI_CEILING_MARGIN_USD,
            vendor=VENDOR_GOOGLE_GEMINI,
            detail=(
                "the benchmark_evaluation phase is within USD "
                f"{GEMINI_CEILING_MARGIN_USD} of its ledger ceiling"
            ),
            numbers={
                "ceiling_usd": _quantize(evaluation_ceiling_usd)
                if evaluation_ceiling_usd is not None
                else None,
                "used_usd": _quantize(evaluation_used_usd),
                "remaining_usd": _quantize(margin) if margin is not None else None,
                "margin_usd": str(GEMINI_CEILING_MARGIN_USD),
                "rule_applies": margin_applies,
                "rule_applies_note": (
                    None
                    if margin_applies
                    else (
                        "the ceiling is smaller than twice the warning band; the "
                        "evaluator's own ceiling precheck bounds this run"
                    )
                ),
            },
            candidates=models_of[VENDOR_GOOGLE_GEMINI],
        )
    )

    session = _percent(windows, "claude_session")
    findings.append(
        _finding(
            "claude_session_window_floor",
            fired=session is not None and session < CLAUDE_SESSION_FLOOR_PERCENT,
            vendor=VENDOR_ANTHROPIC_CLAUDE_CODE,
            detail=(
                "the Claude 5-hour session window that bounds every Claude "
                f"model is below {CLAUDE_SESSION_FLOOR_PERCENT} percent remaining"
            ),
            numbers={
                "percent_remaining": None if session is None else str(session),
                "floor_percent": str(CLAUDE_SESSION_FLOOR_PERCENT),
                "resets_at_utc": (windows.get("claude_session") or {}).get(
                    "resets_at_utc"
                ),
            },
            candidates=models_of[VENDOR_ANTHROPIC_CLAUDE_CODE],
        )
    )

    fable = _percent(windows, "claude_fable")
    after = fable_rule_active_after
    active = after is None or moment >= after
    findings.append(
        _finding(
            "fable_weekly_window_floor",
            fired=active
            and fable is not None
            and fable < FABLE_WEEKLY_FLOOR_PERCENT
            and FABLE_MODEL in models_of[VENDOR_ANTHROPIC_CLAUDE_CODE],
            vendor=VENDOR_ANTHROPIC_CLAUDE_CODE,
            detail=(
                "the Fable weekly window is below "
                f"{FABLE_WEEKLY_FLOOR_PERCENT} percent remaining after the "
                "captain's own Fable pause has expired"
            ),
            numbers={
                "percent_remaining": None if fable is None else str(fable),
                "floor_percent": str(FABLE_WEEKLY_FLOOR_PERCENT),
                "rule_active_after_utc": _stamp(after) if after else None,
                "rule_active": active,
            },
            candidates=(
                (FABLE_MODEL,)
                if FABLE_MODEL in models_of[VENDOR_ANTHROPIC_CLAUDE_CODE]
                else ()
            ),
        )
    )

    codex = _percent(windows, "codex_weekly")
    findings.append(
        _finding(
            "codex_weekly_window_floor",
            fired=codex is not None and codex < CODEX_WEEKLY_FLOOR_PERCENT,
            vendor=VENDOR_OPENAI_CODEX,
            detail=(
                "the Codex weekly window is below "
                f"{CODEX_WEEKLY_FLOOR_PERCENT} percent remaining"
            ),
            numbers={
                "percent_remaining": None if codex is None else str(codex),
                "floor_percent": str(CODEX_WEEKLY_FLOOR_PERCENT),
                "resets_at_utc": (windows.get("codex_weekly") or {}).get(
                    "resets_at_utc"
                ),
            },
            candidates=models_of[VENDOR_OPENAI_CODEX],
        )
    )

    window = windows.get("codex_weekly") or {}
    exhausted = parse_utc(window.get("projected_exhausted_at_utc"))
    resets = parse_utc(window.get("resets_at_utc"))
    early = exhausted is not None and resets is not None and exhausted < resets
    findings.append(
        _finding(
            "codex_projected_exhaustion",
            fired=early and benchmark_drives_codex,
            vendor=VENDOR_OPENAI_CODEX,
            detail=(
                "quota-axi projects the Codex weekly window exhausted before "
                "its reset while the benchmark is the main consumer"
            ),
            numbers={
                "projected_exhausted_at_utc": window.get("projected_exhausted_at_utc"),
                "resets_at_utc": window.get("resets_at_utc"),
                "projected_before_reset": early,
                "benchmark_is_main_consumer": benchmark_drives_codex,
                "projection_confidence": window.get("projection_confidence"),
            },
            candidates=models_of[VENDOR_OPENAI_CODEX],
        )
    )
    return findings


def select_pause_model(
    candidates: tuple[str, ...] | list[str],
    readings: dict[str, dict[str, Any]],
    already_paused: set[str],
) -> str | None:
    """Pick the costliest candidate that nothing pauses yet.

    The captain's tie-break for Gemini - "pause the model with the higher cost
    per question first" - applies to every vendor. A model with no measured cost
    yet sorts last, because pausing it would save nothing that is proven.
    """
    open_models = [model for model in candidates if model not in already_paused]
    if not open_models:
        return None

    def key(model: str) -> tuple[int, Decimal, str]:
        cost = _cost_per_question(readings.get(model) or {})
        return (0 if cost is not None else 1, -(cost or Decimal("0")), model)

    return sorted(open_models, key=key)[0]


# --- One guard cycle --------------------------------------------------------------


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).is_file():
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def evaluator_activity(
    watch_state: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_EVALUATOR_STALE_SECONDS,
) -> dict[str, Any]:
    """Say whether the streaming evaluator is still polling."""
    moment = now or _utc_now()
    updated = parse_utc((watch_state or {}).get("updated_at_utc"))
    age = None if updated is None else int((moment - updated).total_seconds())
    running = age is not None and age <= stale_after_seconds
    return {
        "present": watch_state is not None,
        "running": running,
        "updated_at_utc": (watch_state or {}).get("updated_at_utc"),
        "age_seconds": age,
        "stale_after_seconds": stale_after_seconds,
        "polls": (watch_state or {}).get("polls"),
        "active_vendors": (watch_state or {}).get("active_vendors") or [],
        "paused_vendors": (watch_state or {}).get("paused_vendors") or {},
        "evaluated_items": len((watch_state or {}).get("evaluated_items") or []),
        "skipped_items": (watch_state or {}).get("skipped_items") or {},
    }


class BenchmarkGuard:
    """Read the meters, decide, and pause or resume one model at a time."""

    def __init__(
        self,
        *,
        journal_dir: Path,
        guard_dir: Path,
        pause_file: Path,
        shared_ledger_file: Path | None = None,
        construction_policy_file: Path | None = None,
        evaluation_policy_file: Path | None = None,
        plan_file: Path | None = None,
        status_file: Path | None = None,
        gemini_budget_usd: Decimal = GEMINI_BUDGET_USD,
        quota_command: tuple[str, ...] = QUOTA_COMMAND,
        recorded_quota_file: Path | None = None,
        evaluator_stale_seconds: int = DEFAULT_EVALUATOR_STALE_SECONDS,
    ) -> None:
        self.journal_dir = Path(journal_dir)
        self.guard_dir = Path(guard_dir)
        self.pause_file = Path(pause_file)
        self.shared_ledger_file = (
            Path(shared_ledger_file) if shared_ledger_file else None
        )
        self.construction_policy_file = (
            Path(construction_policy_file) if construction_policy_file else None
        )
        self.evaluation_policy_file = (
            Path(evaluation_policy_file) if evaluation_policy_file else None
        )
        self.plan_file = Path(plan_file) if plan_file else None
        self.status_file = Path(status_file) if status_file else None
        self.gemini_budget_usd = gemini_budget_usd
        self.quota_command = quota_command
        self.recorded_quota_file = (
            Path(recorded_quota_file) if recorded_quota_file else None
        )
        self.evaluator_stale_seconds = evaluator_stale_seconds
        self.state_file = self.guard_dir / GUARD_STATE_FILENAME
        self.log_file = self.guard_dir / GUARD_LOG_FILENAME

    # -- inputs

    def _quota(self, errors: list[str]) -> dict[str, Any]:
        try:
            report = read_quota_report(
                command=self.quota_command, recorded_file=self.recorded_quota_file
            )
        except (
            OSError,
            ValueError,
            subprocess.SubprocessError,
        ) as error:  # pragma: no cover - environment
            errors.append(f"quota report unavailable: {error}")
            return {}
        return quota_windows(report)

    def _ledger(self, errors: list[str]) -> dict[str, Any]:
        if self.shared_ledger_file is None or not self.shared_ledger_file.is_file():
            errors.append("no shared paid-call ledger selected")
            return {}
        try:
            return read_ledger(self.shared_ledger_file)
        except (OSError, ValueError) as error:  # pragma: no cover - environment
            errors.append(f"shared ledger unreadable: {error}")
            return {}

    def _fable_rule_active_after(self, pause: dict[str, Any]) -> datetime | None:
        """The captain's own Fable pause defines when the Fable rule starts."""
        entry = (pause.get("paused_models") or {}).get(FABLE_MODEL) or {}
        if entry.get("owner") == GUARD_OWNER:
            return None
        return parse_utc(entry.get("resume_at_utc"))

    # -- one cycle

    def cycle(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Read every meter once, decide, act, and return the guard state."""
        moment = now or _utc_now()
        errors: list[str] = []
        rows = read_journal_rows(self.journal_dir)
        items = item_rows(rows)
        watch = read_watch_state(self.journal_dir)
        activity = evaluator_activity(
            watch, now=moment, stale_after_seconds=self.evaluator_stale_seconds
        )
        ledger = self._ledger(errors)
        evaluation = (
            evaluation_phase_totals(ledger)
            if ledger
            else {
                "used_usd": "0.000000",
                "spent_usd": "0.000000",
                "questions": 0,
                "by_model": {},
                "tokens": _empty_tokens(),
                "charged_calls": 0,
            }
        )
        construction = (
            construction_totals(ledger)
            if ledger
            else {
                "construction_spent_usd": "0.000000",
                "accepted_question_count": 0,
                "ledger_spent_usd": "0.000000",
                "evaluation_spent_usd": "0.000000",
            }
        )
        construction_policy = _read_json(self.construction_policy_file) or {}
        evaluation_policy = _read_json(self.evaluation_policy_file) or {}
        allocation = _money(
            construction_policy.get("dataset_construction_allocation_usd") or "0"
        )
        ceiling = (
            _money(evaluation_policy["evaluation_ceiling_usd"])
            if evaluation_policy.get("evaluation_ceiling_usd") is not None
            else None
        )

        models_by_vendor = plan_models(_read_json(self.plan_file))
        readings = model_readings(items, evaluation, models_by_vendor)
        target = construction_policy.get("accepted_question_target")
        expectation = expected_questions(
            accepted_now=construction["accepted_question_count"],
            construction_allocation_usd=allocation,
            construction_spent_usd=_money(construction["construction_spent_usd"]),
            questions_evaluated=len(items),
            accepted_question_target=int(target) if target else None,
        )
        still = expectation["questions_still_expected"]
        gemini = extrapolate(
            spend_so_far=_money(evaluation["used_usd"]),
            questions_evaluated=evaluation.get("questions") or len(items),
            questions_still_expected=still,
        )
        gemini["budget_usd"] = str(self.gemini_budget_usd)
        gemini["budget_remaining_usd"] = _quantize(
            self.gemini_budget_usd - _money(evaluation["used_usd"])
        )
        windows = self._quota(errors)

        previous = _read_json(self.state_file) or {}
        drives_codex = self._benchmark_drives_codex(readings, previous, activity)

        try:
            pause = read_pause_file(self.pause_file)
        except (OSError, ValueError) as error:
            errors.append(f"model-pause file unreadable: {error}")
            pause = {"schema": PAUSE_SCHEMA, "paused_models": {}}

        findings = evaluate_rules(
            gemini=gemini,
            evaluation_ceiling_usd=ceiling,
            evaluation_used_usd=_money(evaluation["used_usd"]),
            windows=windows,
            gemini_budget_usd=self.gemini_budget_usd,
            fable_rule_active_after=self._fable_rule_active_after(pause),
            benchmark_drives_codex=drives_codex,
            models_by_vendor=models_by_vendor,
            now=moment,
        )
        actions = self._act(
            pause=pause, findings=findings, readings=readings, now=moment
        )

        state = {
            "schema": GUARD_STATE_SCHEMA,
            "generated_at_utc": _stamp(moment),
            "journal_dir": str(self.journal_dir),
            "plan_models": {
                vendor: list(models) for vendor, models in models_by_vendor.items()
            },
            "pause_file": str(self.pause_file),
            "gemini_budget_usd": str(self.gemini_budget_usd),
            "evaluator": activity,
            "questions_evaluated": len(items),
            "questions_complete": sum(1 for row in items if row.get("complete")),
            "evaluator_pause_rows": [
                {
                    "vendor": row.get("vendor"),
                    "reason": row.get("reason"),
                    "recorded_at_utc": row.get("recorded_at_utc"),
                }
                for row in pause_rows(rows)
            ],
            "expectation": expectation,
            "construction": construction,
            "evaluation_phase": {
                "used_usd": evaluation["used_usd"],
                "spent_usd": evaluation["spent_usd"],
                "reserved_usd": evaluation.get("reserved_usd"),
                "ambiguous_usd": evaluation.get("ambiguous_usd"),
                "charged_calls": evaluation.get("charged_calls"),
                "questions": evaluation.get("questions"),
                "ceiling_usd": _quantize(ceiling) if ceiling is not None else None,
                "remaining_usd": (
                    _quantize(ceiling - _money(evaluation["used_usd"]))
                    if ceiling is not None
                    else None
                ),
                "policy_id": evaluation_policy.get("policy_id"),
            },
            "gemini_budget": gemini,
            "vendors": self._vendor_rollup(readings, gemini, still),
            "models": [
                render_model_reading(readings[model], questions_still_expected=still)
                for model in sorted(readings)
            ],
            "quota": windows,
            "benchmark_is_main_codex_consumer": drives_codex,
            "rules": findings,
            "paused_models": active_pauses(pause, now=moment),
            "actions": actions,
            "errors": errors,
        }
        self.guard_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(self.state_file, state)
        self._log({"event": "poll", **self._headline(state)}, moment)
        for action in actions:
            self._log({"event": action["action"], **action}, moment)
            self._report_status(action)
        return state

    # -- helpers of one cycle

    def _benchmark_drives_codex(
        self,
        readings: dict[str, dict[str, Any]],
        previous: dict[str, Any],
        activity: dict[str, Any],
    ) -> bool:
        """Is this benchmark the main consumer of the Codex weekly window?

        quota-axi reports no per-caller attribution, so the guard proves the
        only thing it can prove: the evaluator is still polling and it booked
        more Codex calls since the last cycle. When the evaluator is idle, the
        Codex burn belongs to the other agent sessions on this machine and
        pausing a benchmark model would save nothing.
        """
        if not activity.get("running"):
            return False
        current = sum(
            entry["calls"]
            for entry in readings.values()
            if entry.get("vendor") == VENDOR_OPENAI_CODEX
        )
        earlier = sum(
            int(model.get("calls") or 0)
            for model in previous.get("models") or []
            if model.get("vendor") == VENDOR_OPENAI_CODEX
        )
        return current > earlier

    def _vendor_rollup(
        self,
        readings: dict[str, dict[str, Any]],
        gemini: dict[str, Any],
        still: int,
    ) -> dict[str, Any]:
        rollup: dict[str, Any] = {}
        for vendor in VENDOR_ORDER:
            members = [
                entry for entry in readings.values() if entry.get("vendor") == vendor
            ]
            tokens = _empty_tokens()
            equivalent = Decimal("0")
            questions = 0
            calls = 0
            for entry in members:
                for name in ("input", "output", "thinking"):
                    tokens[name] += entry["tokens"][name]
                equivalent += Decimal(entry["list_price_equivalent_usd"])
                questions = max(questions, entry["questions_evaluated"])
                calls += entry["calls"]
            record: dict[str, Any] = {
                "vendor": vendor,
                "label": VENDOR_LABELS[vendor],
                "models": sorted(entry["model"] for entry in members),
                "questions_evaluated": questions,
                "calls": calls,
                "tokens": tokens,
                "list_price_equivalent_usd": _quantize(equivalent),
                "billing": (
                    "api_credits" if vendor == VENDOR_GOOGLE_GEMINI else "subscription"
                ),
            }
            if vendor == VENDOR_GOOGLE_GEMINI:
                record["budget"] = gemini
            else:
                record["extrapolated_list_price_equivalent_usd"] = _quantize(
                    equivalent / Decimal(questions) * Decimal(questions + still)
                    if questions
                    else None
                )
                record["charged_usd"] = "0.000000"
            rollup[vendor] = record
        return rollup

    def _act(
        self,
        *,
        pause: dict[str, Any],
        findings: list[dict[str, Any]],
        readings: dict[str, dict[str, Any]],
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Pause one model per fired rule and resume what no rule holds."""
        models = dict(pause.get("paused_models") or {})
        actions: list[dict[str, Any]] = []
        fired = [finding for finding in findings if finding["fired"]]
        held_by_rule: dict[str, set[str]] = {}
        for finding in fired:
            held_by_rule[finding["rule"]] = set(finding["candidate_models"])

        for finding in fired:
            already = {
                model
                for model, entry in active_pauses(
                    {"paused_models": models}, now=now
                ).items()
            }
            chosen = select_pause_model(finding["candidate_models"], readings, already)
            if chosen is None:
                continue
            reading = readings.get(chosen) or {}
            models[chosen] = {
                "reason": finding["detail"],
                "paused_at_utc": _stamp(now),
                "owner": GUARD_OWNER,
                "rule": finding["rule"],
                "vendor": finding["vendor"],
                "numbers": finding["numbers"],
                "cost_per_question_usd": _quantize(_cost_per_question(reading)),
                "resume_note": (
                    "the guard removes this entry by itself when the condition clears"
                ),
            }
            actions.append(
                {
                    "action": "pause",
                    "model": chosen,
                    "vendor": finding["vendor"],
                    "rule": finding["rule"],
                    "reason": finding["detail"],
                    "numbers": finding["numbers"],
                    "cost_per_question_usd": _quantize(_cost_per_question(reading)),
                }
            )

        for model, entry in list(models.items()):
            if (entry or {}).get("owner") != GUARD_OWNER:
                continue
            rule = (entry or {}).get("rule")
            if model in held_by_rule.get(str(rule), set()):
                continue
            del models[model]
            actions.append(
                {
                    "action": "resume",
                    "model": model,
                    "vendor": (entry or {}).get("vendor"),
                    "rule": rule,
                    "reason": f"the condition of {rule} no longer holds",
                    "numbers": next(
                        (
                            finding["numbers"]
                            for finding in findings
                            if finding["rule"] == rule
                        ),
                        {},
                    ),
                }
            )

        if actions:
            pause["paused_models"] = models
            write_pause_file(self.pause_file, pause)
        return actions

    @staticmethod
    def _headline(state: dict[str, Any]) -> dict[str, Any]:
        quota = state.get("quota") or {}
        return {
            "questions_evaluated": state["questions_evaluated"],
            "gemini_used_usd": state["evaluation_phase"]["used_usd"],
            "gemini_extrapolated_total_usd": state["gemini_budget"][
                "extrapolated_total_usd"
            ],
            "gemini_budget_usd": state["gemini_budget"]["budget_usd"],
            "questions_still_expected": state["expectation"][
                "questions_still_expected"
            ],
            "claude_session_percent_remaining": (quota.get("claude_session") or {}).get(
                "percent_remaining"
            ),
            "claude_fable_percent_remaining": (quota.get("claude_fable") or {}).get(
                "percent_remaining"
            ),
            "codex_weekly_percent_remaining": (quota.get("codex_weekly") or {}).get(
                "percent_remaining"
            ),
            "paused_models": sorted(state.get("paused_models") or {}),
            "evaluator_running": (state.get("evaluator") or {}).get("running"),
            "errors": state.get("errors") or [],
        }

    def _log(self, record: dict[str, Any], moment: datetime) -> None:
        self.guard_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"schema": GUARD_LOG_SCHEMA, "at_utc": _stamp(moment), **record},
            sort_keys=True,
        )
        with self.log_file.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.write(f"{line}\n")
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _report_status(self, action: dict[str, Any]) -> None:
        """Append one `working:` line so the supervisor sees the pause."""
        if self.status_file is None:
            return
        numbers = " ".join(
            f"{name}={value}"
            for name, value in sorted((action.get("numbers") or {}).items())
            if value is not None
        )
        line = (
            f"working: {action['action']}d {action['model']} on "
            f"{action['rule']} ({numbers})"
        )
        try:
            with self.status_file.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        except OSError:  # pragma: no cover - environment
            return

    def run(
        self, *, interval_seconds: int = DEFAULT_INTERVAL_SECONDS, once: bool = False
    ) -> int:
        """Poll every `interval_seconds` until the process is stopped."""
        while True:
            state = self.cycle()
            print(
                json.dumps({"guard": self._headline(state)}, sort_keys=True), flush=True
            )
            if once:
                return 0
            time.sleep(max(int(interval_seconds), 1))


# --- The read-only view for the corpus viewer -------------------------------------


def _question_cost_row(row: dict[str, Any]) -> dict[str, Any]:
    evaluation = row.get("evaluation") or {}
    generation = row.get("generation") or {}
    family = generation.get("family") or {}
    return {
        "item_id": row.get("item_id"),
        "recorded_at_utc": row.get("recorded_at_utc"),
        "complete": bool(row.get("complete")),
        "family_id": row.get("family_id"),
        "generation_usd": family.get("usd"),
        "generation_paid_calls": family.get("paid_calls"),
        "campaign_usd_per_accepted_item": generation.get(
            "campaign_usd_per_accepted_item"
        ),
        "evaluation_gemini_usd": (evaluation.get("google_gemini") or {}).get("usd"),
        "subscription_tokens": evaluation.get("subscription_tokens") or _empty_tokens(),
        "subscription_list_price_equivalent_usd": evaluation.get(
            "subscription_list_price_equivalent_usd"
        ),
        "recorded_trials": evaluation.get("recorded_trials"),
        "planned_trials": evaluation.get("planned_trials"),
        "invalid_count": row.get("invalid_count"),
        "invalid_rate": row.get("invalid_rate"),
        "wall_seconds": evaluation.get("wall_seconds"),
        "vendors_paused": evaluation.get("vendors_paused") or [],
    }


def benchmark_report(
    *,
    journal_dir: Path | None,
    guard_state_file: Path | None,
    maximum_rows: int = 50,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the live-benchmarking view, rebuilt from the files on each request.

    This function never calls a model, never runs a subprocess and never writes.
    The per-model table and the per-question cost rows come from the evaluator's
    cost journal; the budget, the extrapolation and the quota readings come from
    the guard state that `BenchmarkGuard` writes.
    """
    moment = now or _utc_now()
    if journal_dir is None:
        return {
            "availability": "not_selected",
            "message": "No streaming evaluation journal is selected.",
            "generated_at_utc": _stamp(moment),
            "models": [],
            "questions": [],
        }
    rows = read_journal_rows(journal_dir)
    items = item_rows(rows)
    watch = read_watch_state(journal_dir)
    guard = _read_json(guard_state_file)
    guard_fresh = None
    if guard is not None:
        generated = parse_utc(guard.get("generated_at_utc"))
        guard_fresh = (
            None if generated is None else int((moment - generated).total_seconds())
        )
    evaluation = {"by_model": {}}
    if guard is not None:
        evaluation = {
            "by_model": {
                model["model"]: {
                    "used_usd": model.get("gemini_used_usd"),
                    "charged_calls": model.get("calls"),
                    "tokens": model.get("tokens") or _empty_tokens(),
                    "questions": model.get("questions_evaluated"),
                }
                for model in guard.get("models") or []
                if model.get("gemini_used_usd") is not None
            }
        }
    readings = model_readings(items, evaluation)
    still = int(
        ((guard or {}).get("expectation") or {}).get("questions_still_expected") or 0
    )
    # A pause with a resume time can expire between two guard polls, so the
    # page applies the same expiry rule the evaluator applies.
    paused = active_pauses(
        {"paused_models": (guard or {}).get("paused_models") or {}}, now=moment
    )
    models = []
    for model in sorted(readings):
        record = render_model_reading(readings[model], questions_still_expected=still)
        entry = paused.get(model)
        record["paused"] = entry is not None
        record["pause_reason"] = (entry or {}).get("reason")
        record["pause_owner"] = (entry or {}).get("owner")
        record["pause_resume_at_utc"] = (entry or {}).get("resume_at_utc")
        record["state"] = "paused" if entry is not None else "active"
        models.append(record)
    activity = evaluator_activity(watch, now=moment)
    availability = "available" if items or watch is not None else "not_started"
    message = {
        "available": (
            f"{len(items)} evaluated questions on {len(models)} models."
            if items
            else "The streaming evaluator is selected but evaluated no question yet."
        ),
        "not_started": "The streaming evaluator has not run yet.",
    }[availability]
    return {
        "availability": availability,
        "message": message,
        "generated_at_utc": _stamp(moment),
        "journal_dir": str(journal_dir),
        "questions_evaluated": len(items),
        "questions_complete": sum(1 for row in items if row.get("complete")),
        "trials_recorded": sum(
            int(((row.get("evaluation") or {}).get("recorded_trials")) or 0)
            for row in items
        ),
        "models": models,
        "questions": [_question_cost_row(row) for row in items[-maximum_rows:]][::-1],
        "questions_truncated": max(len(items) - maximum_rows, 0),
        "evaluator": activity,
        "evaluator_pause_rows": [
            {
                "vendor": row.get("vendor"),
                "reason": row.get("reason"),
                "recorded_at_utc": row.get("recorded_at_utc"),
            }
            for row in pause_rows(rows)
        ],
        "cumulative": (items[-1].get("cumulative") if items else None),
        "guard": {
            "present": guard is not None,
            "state_file": str(guard_state_file) if guard_state_file else None,
            "generated_at_utc": (guard or {}).get("generated_at_utc"),
            "age_seconds": guard_fresh,
            "gemini_budget": (guard or {}).get("gemini_budget"),
            "evaluation_phase": (guard or {}).get("evaluation_phase"),
            "expectation": (guard or {}).get("expectation"),
            "vendors": (guard or {}).get("vendors"),
            "quota": (guard or {}).get("quota"),
            "rules": (guard or {}).get("rules"),
            "paused_models": paused,
            "actions": (guard or {}).get("actions") or [],
            "errors": (guard or {}).get("errors") or [],
        },
    }


# --- Command line -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Guard the live benchmark evaluation against the Gemini budget and "
            "the Claude and Codex subscription quotas."
        )
    )
    parser.add_argument("--journal-dir", type=Path, required=True)
    parser.add_argument("--guard-dir", type=Path, required=True)
    parser.add_argument("--pause-file", type=Path, required=True)
    parser.add_argument("--shared-ledger-file", type=Path)
    parser.add_argument("--construction-policy-file", type=Path)
    parser.add_argument("--evaluation-policy-file", type=Path)
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--gemini-budget-usd", default=str(GEMINI_BUDGET_USD))
    parser.add_argument("--recorded-quota-file", type=Path)
    parser.add_argument(
        "--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS
    )
    parser.add_argument(
        "--evaluator-stale-seconds", type=int, default=DEFAULT_EVALUATOR_STALE_SECONDS
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    guard = BenchmarkGuard(
        journal_dir=args.journal_dir,
        guard_dir=args.guard_dir,
        pause_file=args.pause_file,
        shared_ledger_file=args.shared_ledger_file,
        construction_policy_file=args.construction_policy_file,
        evaluation_policy_file=args.evaluation_policy_file,
        plan_file=args.plan_file,
        status_file=args.status_file,
        gemini_budget_usd=Decimal(str(args.gemini_budget_usd)),
        recorded_quota_file=args.recorded_quota_file,
        evaluator_stale_seconds=args.evaluator_stale_seconds,
    )
    return guard.run(interval_seconds=args.interval_seconds, once=args.once)


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
