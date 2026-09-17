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

The guard also watches the evaluator itself. An evaluator that stopped scores
nothing, and a bound that ends it is not an error, so the exit code says
nothing: the unit met its item bound at 2026-09-16T19:31:44Z and no operator
saw it until the next morning. So a watch state that is absent or stale is an
error of `guard-state.json`, and the guard appends one `blocked:` line to its
status file when the evaluator stops and one `working:` line when it returns.

Nothing here makes a paid model call. Every input is a file on disk or the
read-only `quota-axi` report.

Captain's order of 2026-09-16: "make sure that the benchmark does not get too
expensive for my budget (I will allocate $200 for benchmarking the gemini
models) vis a vis gemini budget, claude session/weekly usage (dont worry about
the weekly usage except for fable), codex weekly usage. If one is rising too
fast, then pause the benchmarking for only that model. This should be rare and
only happen if there are very urget issues with the benchmarking cost."

Captain's sleep order of 2026-09-17, 08:35 UTC: "I need to have all results
FINISHED by around 8:00am this morning [13:00 UTC]. Extrapolate current rates
to determine if this is feasible", and, earlier that night, "pause the fable
testing". The deadline is the point, so the subscription arms now run to a
floor of their own quota window instead of to a projection:

- the ChatGPT arms run until the Codex weekly window has 10 percent left,
  which is the reserve of the paper worker, and then pause;
- the Claude arms run until the 5-hour session window or the 7-day window has
  5 percent left, and then pause with that window's own reset as their resume
  time, so they come back by themselves;
- Fable, and Fable alone, stops on a usage bound instead: the captain's order
  of 08:55 UTC stops it when its weekly window is 80 percent used, which is
  20 percent or less remaining, and it does not resume by itself. Until then
  it is held by the captain's own entry, which this guard never touches.

The projection rule that paused a ChatGPT model hours before that floor
(`codex_projected_exhaustion`) is retired, and the guard removes the pause
entries it wrote. The Codex attribution measurement stays in the guard state
as a measurement; no rule pauses on it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import ledger_store
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
GUARD_MEMORY_FILENAME = "guard-memory.json"
GUARD_MEMORY_SCHEMA = "benchmark-guard-memory-v1"
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
# The captain's sleep order of 2026-09-17, 08:35 UTC: "I need to have all
# results FINISHED by around 8:00am this morning". The subscription arms
# therefore run to a floor and not to a projection: a reserve is kept, and
# everything above it is spent on the deadline. The Claude floor is 5 percent
# of the 5-hour session window, which resets about every five hours, so a
# paused Claude arm comes back on its own. The Codex floor is 10 percent of
# the weekly window, which is the reserve the paper worker keeps.
CLAUDE_SESSION_FLOOR_PERCENT = Decimal("5")
# The same floor bounds the Claude 7-day window: both windows bound every
# Claude model, and either one at the floor stops the arm until that window
# resets (captain order 2026-09-17, 08:55 UTC).
CLAUDE_WEEKLY_FLOOR_PERCENT = Decimal("5")
# Fable is the one arm with a usage bound instead of a floor: the captain
# stops it when its weekly window is 80 percent used, which is 20 percent or
# less remaining, and it does not come back by itself.
FABLE_WEEKLY_FLOOR_PERCENT = Decimal("20")
CODEX_WEEKLY_FLOOR_PERCENT = Decimal("10")

# Rules the captain retired. A guard-owned pause entry of a retired rule
# pauses a model that the captain wants running, so the guard removes such an
# entry at once and never applies the hysteresis of a rule that no longer
# exists. `codex_projected_exhaustion` paused gpt-6-astra at 08:29 UTC and
# gpt-5.6-sol at 08:34 UTC on 2026-09-17 on a projection, hours before the
# floor of the window was anywhere near; the captain replaced it with the
# floor above.
RETIRED_RULES = ("codex_projected_exhaustion",)
RETIRED_RULE_NOTE = (
    "the captain retired this rule on 2026-09-17 and replaced it with the "
    "floor of the vendor's own quota window"
)

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_EVALUATOR_STALE_SECONDS = 900

# The Codex attribution measurement. `quota-axi` gives no per-caller
# attribution, so the guard measures the benchmark's own Codex burn against the
# whole account's burn over a trailing window. The window is at least 30
# minutes, which is six cycles at the default interval.
CODEX_ATTRIBUTION_WINDOW_SECONDS = 1800
# The benchmark drives the Codex window when it causes at least this share of
# the window's burn over that period.
CODEX_DRIVES_SHARE = Decimal("0.5")
# `quota-axi` reports whole percent points, so a measured burn of `d` points
# means a true burn below `d + 1` points.
QUOTA_PERCENT_RESOLUTION = Decimal("1")
# 48 hours of five-minute cycles.
CODEX_SAMPLE_LIMIT = 576
CODEX_SAMPLE_MAXIMUM_AGE_SECONDS = 172800

# Hysteresis. A guard-owned pause is removed only after its rule has been clear
# for this many consecutive cycles, and a model the guard resumed is not paused
# again by the same rule for this many seconds.
RESUME_CLEAR_CYCLES = 3
REPAUSE_HOLD_SECONDS = 1800

QUOTA_COMMAND = ("quota-axi", "--json", "--full")
# The nix devshell has no npm global bin on PATH, so a service passes the
# absolute path of the binary with `--quota-binary`.
QUOTA_ARGUMENTS = ("--json", "--full")
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


def latest_item_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last row of each question, in the order the questions appeared.

    The evaluator appends one row per pass, and it passes over a question
    again whenever a paused arm owes it trials. Every number of a later row
    is the question's whole record: its Gemini USD comes from the ledger and
    its outcomes come from the run directory, and both are cumulative. So a
    reader that counts every row counts a revisited question twice, and both
    the cost per question and the Codex list-price equivalent drift. The
    evaluator's own reader is `CostJournal.latest_item_rows`, and this is the
    guard's half of that rule.
    """
    latest: dict[str, dict[str, Any]] = {}
    for row in item_rows(rows):
        latest[str(row["item_id"])] = row
    return list(latest.values())


def pause_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the vendor-pause rows that the evaluator itself journalled."""
    return [row for row in rows if row.get("kind") == "vendor_pause"]


def plan_model_count(
    rows: list[dict[str, Any]],
    *,
    models_by_vendor: dict[str, tuple[str, ...]] | None = None,
) -> int:
    """The number of models one question's plan runs.

    ``outcomes_by_model`` names only the models that recorded something, so a
    question whose Gemini arm was paused throughout names six models and not
    eight. The count is therefore the plan's own model count, raised to the
    widest row of the journal when a run outgrew the plan, and it is never one
    row's own model count: dividing a narrow row's planned trials by that put
    the per-arm share at 24 instead of 6 and undercounted every arm.

    Every reader of a share takes it from here, so the per-question counts and
    the per-arm counts can never disagree about the same journal.
    """
    plan = models_by_vendor or dict(PLAN_MODELS_BY_VENDOR)
    planned = len({model for models in plan.values() for model in models})
    widest = max(
        (len(row.get("outcomes_by_model") or {}) for row in item_rows(rows)),
        default=0,
    )
    return max(planned, widest)


def trials_per_model(row: dict[str, Any], *, plan_models: int) -> int:
    """The share of one question's trials that belongs to one model."""
    planned = int((row.get("evaluation") or {}).get("planned_trials") or 0)
    return planned // plan_models if plan_models and planned else 0


def row_recorded_trials(row: dict[str, Any]) -> int:
    """The trials recorded for one question.

    The evaluator's own field is the authority: a question's row records how
    many of its plan it holds as ``evaluation.recorded_trials`` against
    ``evaluation.planned_trials``. The outcomes are the fallback for a row
    written before that field, and the two agreed on every row of
    `streaming-r11` on 2026-09-17.
    """
    evaluation = row.get("evaluation") or {}
    if "recorded_trials" in evaluation:
        return int(evaluation.get("recorded_trials") or 0)
    return sum(
        sum(counts.values())
        for counts in (row.get("outcomes_by_model") or {}).values()
    )


def row_is_pause_held(row: dict[str, Any]) -> bool:
    """True when a pause names this question, so an arm still owes it trials."""
    evaluation = row.get("evaluation") or {}
    return bool(evaluation.get("models_paused")) or bool(
        evaluation.get("vendors_paused")
    )


def question_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count questions the way the evaluator counts them, not journal rows.

    The evaluator appends one row per pass over a question and passes again
    whenever a paused arm owes it trials, so the journal holds far more rows
    than questions. Every count here reads the last row of each question,
    which carries that question's whole record. A reader that counted rows
    told the live page 286 questions on 2026-09-17 while the journal held 160,
    so ``journal_rows`` and ``question_rows`` are reported beside the question
    counts and the difference can never hide again.

    ``complete`` in a row is not the same thing as "every response arrived".
    A row the evaluator wrote while an arm was paused sets it, and the row
    still holds part of its plan. So a question counts as complete here only
    when its recorded trials reach its planned trials.
    """
    questions = latest_item_rows(rows)
    seen: dict[str, int] = {}
    best: dict[str, int] = {}
    reopened: set[str] = set()
    for row in item_rows(rows):
        item_id = str(row["item_id"])
        seen[item_id] = seen.get(item_id, 0) + 1
        recorded = row_recorded_trials(row)
        if item_id in best and recorded > best[item_id]:
            # A later pass added trials to a question an earlier pass left
            # short. That is the measurable evidence of a reopen.
            reopened.add(item_id)
        best[item_id] = max(best.get(item_id, 0), recorded)

    plan_models = plan_model_count(rows)
    with_response = 0
    complete = 0
    partial = 0
    stranded = 0
    pause_held = 0
    recorded_total = 0
    planned_total = 0
    share = 0
    for row in questions:
        recorded = row_recorded_trials(row)
        planned = int((row.get("evaluation") or {}).get("planned_trials") or 0)
        share = share or trials_per_model(row, plan_models=plan_models)
        recorded_total += recorded
        planned_total += planned
        if recorded:
            with_response += 1
        if planned and recorded >= planned:
            complete += 1
            continue
        partial += 1
        if row_is_pause_held(row):
            pause_held += 1
        else:
            stranded += 1
    return {
        "questions_in_journal": len(questions),
        "questions_with_a_response": with_response,
        "questions_with_every_response": complete,
        "questions_partial": partial,
        "questions_stranded": stranded,
        "questions_held_by_a_pause": pause_held,
        "questions_revisited": sum(1 for count in seen.values() if count > 1),
        "questions_reopened": len(reopened),
        "trials_recorded": recorded_total,
        "trials_planned": planned_total,
        "planned_trials_per_question": (
            planned_total // len(questions) if questions else 0
        ),
        "trials_per_model": share,
        "plan_models": plan_models,
        "journal_rows": len(rows),
        "question_rows": len(item_rows(rows)),
    }


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
        return ledger_store.read_ledger(path)
    try:
        fcntl.flock(handle, fcntl.LOCK_SH)
        # The file is the compacted snapshot of the parallel bookkeeping
        # store; the journal beside it holds everything committed since.
        return ledger_store.read_ledger(path)
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
                "window_seconds": pace.get("cycleSeconds")
                or window.get("windowSeconds"),
                "projected_exhausted_at_utc": pace.get("projectedExhaustedAt"),
                "pace": pace.get("status"),
                "burn_multiple": pace.get("burnMultiple"),
                "projection_confidence": pace.get("projectionConfidence"),
            }
    result["generated_at_utc"] = report.get("generatedAt")
    return result


def codex_list_price_equivalent_usd(rows: list[dict[str, Any]]) -> Decimal:
    """Sum the Codex list-price equivalent of every evaluated question."""
    total = Decimal("0")
    for row in rows:
        vendor = ((row.get("evaluation") or {}).get("subscription") or {}).get(
            VENDOR_OPENAI_CODEX
        ) or {}
        total += _money(vendor.get("list_price_equivalent_usd"))
    return total


def codex_calls(rows: list[dict[str, Any]]) -> int:
    """Count the Codex calls of every evaluated question."""
    total = 0
    for row in rows:
        vendor = ((row.get("evaluation") or {}).get("subscription") or {}).get(
            VENDOR_OPENAI_CODEX
        ) or {}
        total += int(vendor.get("calls") or 0)
    return total


def _reported_burn_percent_per_second(window: dict[str, Any]) -> Decimal | None:
    """The average burn `quota-axi` reports for one window, in percent a second.

    `pace.burnMultiple` is the used percent divided by the elapsed percent of
    the cycle, so `burnMultiple * 100 / cycleSeconds` is that average rate.
    """
    multiple = window.get("burn_multiple")
    seconds = window.get("window_seconds")
    if multiple is None or not seconds:
        return None
    try:
        return Decimal(str(multiple)) * Decimal("100") / Decimal(str(seconds))
    except (ArithmeticError, ValueError):
        return None


def codex_idle_baseline(
    samples: list[dict[str, Any]],
) -> tuple[Decimal | None, Decimal, Decimal]:
    """Return the burn rate of the other Codex sessions on this machine.

    `quota-axi` reports no per-caller attribution, so the guard separates the
    two regimes it can see. In an interval between two samples the benchmark
    either spent Codex list-price-equivalent USD or it spent none. The
    intervals in which it spent none carry only the burn of the other sessions,
    so their percent points a second is the baseline rate of everything that is
    not this benchmark.

    Returns the rate, the seconds it was measured over and the percent points
    those seconds burned. The rate is None until an idle interval exists.
    """
    seconds = Decimal("0")
    percent = Decimal("0")
    for earlier, later in zip(samples, samples[1:], strict=False):
        first = parse_utc(earlier.get("at_utc"))
        second = parse_utc(later.get("at_utc"))
        if first is None or second is None or second <= first:
            continue
        if (
            earlier.get("codex_percent_remaining") is None
            or later.get("codex_percent_remaining") is None
        ):
            continue
        spent = _money(later.get("benchmark_codex_usd")) - _money(
            earlier.get("benchmark_codex_usd")
        )
        if spent > 0:
            continue
        burn = _money(earlier["codex_percent_remaining"]) - _money(
            later["codex_percent_remaining"]
        )
        if burn < 0:
            # The weekly window reset inside this interval.
            continue
        seconds += Decimal(str((second - first).total_seconds()))
        percent += burn
    rate = percent / seconds if seconds > 0 else None
    return rate, seconds, percent


def codex_attribution(
    *,
    samples: list[dict[str, Any]],
    now: datetime,
    window: dict[str, Any],
    percent_remaining: Decimal | None,
    benchmark_codex_usd: Decimal,
    questions_evaluated: int,
    questions_still_expected: int,
    window_seconds: int = CODEX_ATTRIBUTION_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Estimate how much of the Codex weekly burn this benchmark causes.

    The guard measures three things over one trailing window of at least 30
    minutes, all from samples it takes itself every cycle:

    - `window_percent_burn`, what the whole account burned, from the
      `percent_remaining` delta of the Codex weekly window. The delta measures
      the window itself. `quota-axi`'s own burn rate is an average over the
      whole elapsed weekly cycle and still carries the burn of earlier
      sessions, so the guard takes it only when the delta is unusable, that is
      after a reset inside the trailing window.
    - `benchmark_usd_burn`, what this benchmark spent in Codex
      list-price-equivalent USD over the same period, from the cost journal.
    - `other_percent_burn`, what the other Codex sessions on this machine burned
      over the same period, at the baseline rate of `codex_idle_baseline`.

    The rest of the burn belongs to the benchmark, which gives both the share
    and the window's percent-per-USD. The benchmark drives the window when its
    share reaches `CODEX_DRIVES_SHARE`, or when its own extrapolated burn to the
    end of the run would exhaust the window before the reset by itself.

    Two measurements must be complete before the rule that reads this can fire:
    the trailing window, and the idle baseline. `quota-axi` reports whole
    percent points, so a window burn at or below that resolution is no measured
    burn at all and no share is read from it.
    """
    attribution: dict[str, Any] = {
        "measured": False,
        "reason": None,
        "drives": False,
        "share_floor": str(CODEX_DRIVES_SHARE),
        "window_seconds": window_seconds,
        "measured_from_utc": None,
        "measured_seconds": None,
        "window_percent_burn": None,
        "window_percent_burn_source": None,
        "other_percent_burn": None,
        "other_percent_per_second": None,
        "idle_baseline_seconds": None,
        "benchmark_percent_burn": None,
        "benchmark_usd_burn": None,
        "benchmark_share_of_window": None,
        "percent_per_usd": None,
        "share_drives": False,
        "codex_usd_per_question": None,
        "extrapolated_benchmark_usd": None,
        "extrapolated_benchmark_percent": None,
        "percent_remaining": (
            None if percent_remaining is None else str(percent_remaining)
        ),
        "resets_at_utc": window.get("resets_at_utc"),
        "projected_benchmark_exhaustion_at_utc": None,
        "benchmark_alone_exhausts_before_reset": False,
    }
    if percent_remaining is None:
        attribution["reason"] = "the Codex weekly window is not in the quota report"
        return attribution

    base = None
    for sample in reversed(samples):
        moment = parse_utc(sample.get("at_utc"))
        if moment is None or sample.get("codex_percent_remaining") is None:
            continue
        if (now - moment).total_seconds() >= window_seconds:
            base = sample
            break
    if base is None:
        attribution["reason"] = (
            f"the trailing measurement window of {window_seconds} seconds is "
            "not full yet"
        )
        return attribution

    other_rate, idle_seconds, _idle_percent = codex_idle_baseline(samples)
    attribution["idle_baseline_seconds"] = int(idle_seconds)
    if other_rate is None or idle_seconds < window_seconds:
        attribution["reason"] = (
            "the guard has not yet watched the Codex window for "
            f"{window_seconds} seconds with this benchmark idle, so it cannot "
            "tell the other sessions' burn from its own"
        )
        return attribution

    started = parse_utc(base["at_utc"])
    duration = Decimal(str((now - started).total_seconds()))
    if duration <= 0:
        attribution["reason"] = "the trailing measurement window has no duration"
        return attribution

    burn = _money(base.get("codex_percent_remaining")) - percent_remaining
    source = "percent_remaining_delta"
    if burn < 0:
        rate = _reported_burn_percent_per_second(window)
        if rate is None:
            attribution["reason"] = (
                "the Codex weekly window reset inside the trailing window and "
                "the report carries no burn rate"
            )
            return attribution
        burn = rate * duration
        source = "reported_burn_rate"

    spent = benchmark_codex_usd - _money(base.get("benchmark_codex_usd"))
    if spent < 0:
        spent = Decimal("0")
    others = other_rate * duration
    mine = burn - others
    if mine < 0:
        mine = Decimal("0")
    # `quota-axi` reports whole percent points, so a burn that small is not a
    # measured burn and nothing urgent can be read from its share.
    readable = burn > QUOTA_PERCENT_RESOLUTION
    share = (mine / burn) if readable else Decimal("0")
    percent_per_usd = (mine / spent) if spent > 0 else None

    per_question = (
        benchmark_codex_usd / Decimal(questions_evaluated)
        if questions_evaluated > 0
        else None
    )
    extrapolated_usd = (
        None
        if per_question is None
        else per_question * Decimal(max(int(questions_still_expected), 0))
    )
    extrapolated_percent = (
        None
        if extrapolated_usd is None or percent_per_usd is None
        else extrapolated_usd * percent_per_usd
    )

    alone = False
    projected_at = None
    resets = parse_utc(window.get("resets_at_utc"))
    rate = mine / duration if mine > 0 else None
    if (
        extrapolated_percent is not None
        and extrapolated_percent >= percent_remaining
        and rate is not None
        and resets is not None
    ):
        projected = (now + timedelta(seconds=float(percent_remaining / rate))).replace(
            microsecond=0
        )
        projected_at = _stamp(projected)
        alone = projected < resets

    share_drives = readable and share >= CODEX_DRIVES_SHARE
    attribution.update(
        {
            "measured": True,
            "drives": bool(share_drives or alone),
            "measured_from_utc": base["at_utc"],
            "measured_seconds": int(duration),
            "window_percent_burn": _quantize(burn),
            "window_percent_burn_source": source,
            "other_percent_burn": _quantize(others),
            "other_percent_per_second": _quantize(other_rate),
            "benchmark_percent_burn": _quantize(mine),
            "benchmark_usd_burn": _quantize(spent),
            "benchmark_share_of_window": _quantize(share),
            "percent_per_usd": _quantize(percent_per_usd),
            "share_drives": bool(share_drives),
            "codex_usd_per_question": _quantize(per_question),
            "extrapolated_benchmark_usd": _quantize(extrapolated_usd),
            "extrapolated_benchmark_percent": _quantize(extrapolated_percent),
            "projected_benchmark_exhaustion_at_utc": projected_at,
            "benchmark_alone_exhausts_before_reset": bool(alone),
        }
    )
    return attribution


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
                "questions_with_a_response": 0,
                "questions_with_every_response": 0,
                "trials_per_question": 0,
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

    plan_models = plan_model_count(rows, models_by_vendor=plan)
    for row in rows:
        share = trials_per_model(row, plan_models=plan_models)
        for model, counts in (row.get("outcomes_by_model") or {}).items():
            entry = reading(model)
            entry["questions_evaluated"] += 1
            recorded = sum(int(value) for value in counts.values())
            # An arm covers a question only when it recorded its whole share
            # of that question's plan. A question the arm answered once and
            # then lost to a pause or a stop is not covered, and the page must
            # not read it as one.
            if recorded:
                entry["questions_with_a_response"] += 1
            if share and recorded >= share:
                entry["questions_with_every_response"] += 1
            entry["trials_per_question"] = entry["trials_per_question"] or share
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
        "questions_with_a_response": entry["questions_with_a_response"],
        "questions_with_every_response": entry["questions_with_every_response"],
        "trials_per_question": entry["trials_per_question"],
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
    pause_all: bool = False,
    resume_at_utc: str | None = None,
) -> dict[str, Any]:
    """Return one rule's reading of the meters.

    ``pause_all`` says that the rule pauses every candidate model of its
    vendor in the cycle it fires, not the costliest one. A floor of a shared
    quota window is such a rule: the window bounds every model of the vendor
    at once, so pausing one model of three leaves the floor being crossed by
    the other two.

    ``resume_at_utc`` is the moment the pause lifts by itself, which is the
    reset of the window the rule reads. A pause with a resume time needs no
    cycle of this guard to come back, so the arm returns even if the guard is
    not running then.
    """
    return {
        "rule": rule,
        "vendor": vendor,
        "fired": bool(fired),
        "detail": detail,
        "numbers": numbers,
        "candidate_models": list(candidates),
        "pause_all": bool(pause_all),
        "resume_at_utc": resume_at_utc,
    }


def evaluate_rules(
    *,
    gemini: dict[str, Any],
    evaluation_ceiling_usd: Decimal | None,
    evaluation_used_usd: Decimal,
    windows: dict[str, Any],
    gemini_budget_usd: Decimal = GEMINI_BUDGET_USD,
    fable_rule_active_after: datetime | None = None,
    codex_attribution_record: dict[str, Any] | None = None,
    models_by_vendor: dict[str, tuple[str, ...]] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Apply the captain's five urgent conditions and return one finding each.

    A finding names the models the rule may pause. Nothing else in this module
    decides when to pause: the caller pauses the costliest candidate of a fired
    rule, one model at a time, or every candidate of a rule whose window
    bounds the whole vendor (``pause_all``).

    The sixth rule, `codex_projected_exhaustion`, is retired: see
    :data:`RETIRED_RULES` and the captain's order in the module docstring.
    """
    moment = now or _utc_now()
    models_of = models_by_vendor or dict(PLAN_MODELS_BY_VENDOR)
    attribution = codex_attribution_record or {}
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

    # The Fable rule comes first of the Claude rules on purpose. It holds one
    # model and it gives no automatic resume, while the two window rules below
    # hold the whole arm and do give one. So Fable takes its own entry first,
    # and the arm-wide rule then finds it paused already and leaves it alone;
    # the other order would give Fable a resume time the captain did not
    # authorize.
    fable = _percent(windows, "claude_fable")
    after = fable_rule_active_after
    active = after is None or moment >= after
    findings.append(
        _finding(
            "fable_weekly_window_floor",
            # "at or below", because the captain set the bound on the usage:
            # 20 percent remaining is 80 percent used and stops the arm.
            fired=active
            and fable is not None
            and fable <= FABLE_WEEKLY_FLOOR_PERCENT
            and FABLE_MODEL in models_of[VENDOR_ANTHROPIC_CLAUDE_CODE],
            vendor=VENDOR_ANTHROPIC_CLAUDE_CODE,
            detail=(
                "the Fable weekly window is "
                f"{100 - int(FABLE_WEEKLY_FLOOR_PERCENT)} percent used or more "
                "after the captain's own Fable pause has expired"
            ),
            numbers={
                "percent_remaining": None if fable is None else str(fable),
                "floor_percent": str(FABLE_WEEKLY_FLOOR_PERCENT),
                "rule_active_after_utc": _stamp(after) if after else None,
                "rule_active": active,
                "captain_order": (
                    "2026-09-17 08:55 UTC: stop claude-fable-5-1, with no "
                    "automatic resume, when the Fable weekly usage reaches 80 "
                    "percent"
                ),
            },
            candidates=(
                (FABLE_MODEL,)
                if FABLE_MODEL in models_of[VENDOR_ANTHROPIC_CLAUDE_CODE]
                else ()
            ),
        )
    )

    session = _percent(windows, "claude_session")
    session_resets = (windows.get("claude_session") or {}).get("resets_at_utc")
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
                "resets_at_utc": session_resets,
                "captain_order": (
                    "2026-09-17 08:35 UTC: the Claude arms run until the 5-hour "
                    "window reaches 5 percent, then pause and resume on the "
                    "reset automatically"
                ),
            },
            candidates=models_of[VENDOR_ANTHROPIC_CLAUDE_CODE],
            # One window bounds every Claude model, and it resets by itself.
            pause_all=True,
            resume_at_utc=session_resets,
        )
    )

    weekly = _percent(windows, "claude_week")
    weekly_resets = (windows.get("claude_week") or {}).get("resets_at_utc")
    findings.append(
        _finding(
            "claude_weekly_window_floor",
            fired=weekly is not None and weekly < CLAUDE_WEEKLY_FLOOR_PERCENT,
            vendor=VENDOR_ANTHROPIC_CLAUDE_CODE,
            detail=(
                "the Claude 7-day window that bounds every Claude model is "
                f"below {CLAUDE_WEEKLY_FLOOR_PERCENT} percent remaining"
            ),
            numbers={
                "percent_remaining": None if weekly is None else str(weekly),
                "floor_percent": str(CLAUDE_WEEKLY_FLOOR_PERCENT),
                "resets_at_utc": weekly_resets,
                "captain_order": (
                    "2026-09-17 08:55 UTC: the Claude arms stop when the 5-hour "
                    "window or the seven_day window hits the floor and resume "
                    "at that window's reset"
                ),
            },
            candidates=models_of[VENDOR_ANTHROPIC_CLAUDE_CODE],
            pause_all=True,
            resume_at_utc=weekly_resets,
        )
    )

    codex = _percent(windows, "codex_weekly")
    codex_window = windows.get("codex_weekly") or {}
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
                "resets_at_utc": codex_window.get("resets_at_utc"),
                "captain_order": (
                    "2026-09-17 08:35 UTC: the ChatGPT arms run until the Codex "
                    "weekly window reaches 10 percent remaining, then pause; "
                    "that reserve belongs to the paper worker"
                ),
                # The measurement the retired projection rule read. It is kept
                # here as a number an operator can see, and it fires nothing.
                "attribution_measured": bool(attribution.get("measured")),
                "benchmark_share_of_window": attribution.get(
                    "benchmark_share_of_window"
                ),
                "projected_exhausted_at_utc": codex_window.get(
                    "projected_exhausted_at_utc"
                ),
                "retired_projection_rule": "codex_projected_exhaustion",
                "retired_projection_note": RETIRED_RULE_NOTE,
            },
            candidates=models_of[VENDOR_OPENAI_CODEX],
            # One weekly window bounds every ChatGPT model, and the reserve is
            # held until that window resets.
            pause_all=True,
            resume_at_utc=codex_window.get("resets_at_utc"),
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


# --- The guard's own memory ------------------------------------------------------


def empty_memory() -> dict[str, Any]:
    """The memory of a guard that never ran."""
    return {
        "schema": GUARD_MEMORY_SCHEMA,
        "updated_at_utc": None,
        "codex_samples": [],
        "rule_clear_cycles": {},
        "guard_resumes": {},
        "evaluator_reported": None,
    }


def read_memory(path: Path) -> dict[str, Any]:
    """Read the guard's memory sidecar; a missing or broken file starts empty.

    The memory holds what the guard must remember between cycles and must not
    write into the pause file: the Codex attribution samples, how many
    consecutive cycles each guard-owned pause has been clear, and when the guard
    last resumed a model. The pause file keeps the captain's own entries exactly
    as written, so nothing of this belongs there.
    """
    memory = empty_memory()
    value = _read_json(path)
    if not isinstance(value, dict) or value.get("schema") != GUARD_MEMORY_SCHEMA:
        return memory
    samples = value.get("codex_samples")
    if isinstance(samples, list):
        memory["codex_samples"] = [
            sample for sample in samples if isinstance(sample, dict)
        ]
    for name in ("rule_clear_cycles", "guard_resumes"):
        block = value.get(name)
        if isinstance(block, dict):
            memory[name] = dict(block)
    reported = value.get("evaluator_reported")
    if isinstance(reported, bool):
        memory["evaluator_reported"] = reported
    return memory


def memory_key(model: str, rule: str) -> str:
    """One hysteresis key: a pause belongs to one model of one rule."""
    return f"{model}::{rule}"


def trim_codex_samples(
    samples: list[dict[str, Any]], *, now: datetime
) -> list[dict[str, Any]]:
    """Keep the recent samples: 48 hours, and at most `CODEX_SAMPLE_LIMIT`."""
    kept = []
    for sample in samples:
        moment = parse_utc(sample.get("at_utc"))
        if moment is None:
            continue
        if (now - moment).total_seconds() > CODEX_SAMPLE_MAXIMUM_AGE_SECONDS:
            continue
        kept.append(sample)
    return kept[-CODEX_SAMPLE_LIMIT:]


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
        "items_in_flight": list((watch_state or {}).get("items_in_flight") or []),
        "item_workers": (watch_state or {}).get("item_workers"),
        "started_at_utc": (watch_state or {}).get("started_at_utc"),
        "skipped_items": (watch_state or {}).get("skipped_items") or {},
    }


def _evaluator_error(activity: dict[str, Any]) -> str:
    """One line that says the evaluator is not polling, and for how long."""
    if not activity["present"]:
        return (
            "the streaming evaluator has no watch state in this journal "
            "directory: it never started, or it runs elsewhere"
        )
    age = activity["age_seconds"]
    return (
        "the streaming evaluator is not running: its watch state has not "
        f"moved for {age} seconds (the bound is "
        f"{activity['stale_after_seconds']}), last at "
        f"{activity['updated_at_utc']}"
    )


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
        codex_attribution_window_seconds: int = CODEX_ATTRIBUTION_WINDOW_SECONDS,
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
        self.codex_attribution_window_seconds = codex_attribution_window_seconds
        self.state_file = self.guard_dir / GUARD_STATE_FILENAME
        self.log_file = self.guard_dir / GUARD_LOG_FILENAME
        self.memory_file = self.guard_dir / GUARD_MEMORY_FILENAME

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
        items = latest_item_rows(rows)
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

        memory = read_memory(self.memory_file)
        codex_usd = codex_list_price_equivalent_usd(items)
        attribution = codex_attribution(
            samples=memory["codex_samples"],
            now=moment,
            window=windows.get("codex_weekly") or {},
            percent_remaining=_percent(windows, "codex_weekly"),
            benchmark_codex_usd=codex_usd,
            questions_evaluated=len(items),
            questions_still_expected=still,
            window_seconds=self.codex_attribution_window_seconds,
        )
        drives_codex = bool(attribution["drives"])
        memory["codex_samples"] = trim_codex_samples(
            [
                *memory["codex_samples"],
                {
                    "at_utc": _stamp(moment),
                    "codex_percent_remaining": (windows.get("codex_weekly") or {}).get(
                        "percent_remaining"
                    ),
                    "benchmark_codex_usd": _quantize(codex_usd),
                    "benchmark_codex_calls": codex_calls(items),
                    "questions_evaluated": len(items),
                },
            ],
            now=moment,
        )

        try:
            pause = read_pause_file(self.pause_file)
        except (OSError, ValueError) as error:
            errors.append(f"model-pause file unreadable: {error}")
            pause = {"schema": PAUSE_SCHEMA, "paused_models": {}}

        # An evaluator that stopped scores nothing, and a bound that ends it is
        # not an error, so nothing else says the benchmark stopped.
        if not activity["running"]:
            errors.append(_evaluator_error(activity))

        findings = evaluate_rules(
            gemini=gemini,
            evaluation_ceiling_usd=ceiling,
            evaluation_used_usd=_money(evaluation["used_usd"]),
            windows=windows,
            gemini_budget_usd=self.gemini_budget_usd,
            fable_rule_active_after=self._fable_rule_active_after(pause),
            codex_attribution_record=attribution,
            models_by_vendor=models_by_vendor,
            now=moment,
        )
        actions, holds = self._act(
            pause=pause,
            findings=findings,
            readings=readings,
            memory=memory,
            now=moment,
        )
        memory["updated_at_utc"] = _stamp(moment)

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
            "codex_attribution": attribution,
            "rules": findings,
            "retired_rules": [
                {"rule": rule, "note": RETIRED_RULE_NOTE} for rule in RETIRED_RULES
            ],
            "paused_models": active_pauses(pause, now=moment),
            "actions": actions,
            "hysteresis": holds,
            "errors": errors,
        }
        self._report_evaluator(activity, memory)
        self.guard_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(self.state_file, state)
        atomic_json(self.memory_file, memory)
        self._log({"event": "poll", **self._headline(state)}, moment)
        for action in actions:
            self._log({"event": action["action"], **action}, moment)
            self._report_status(action)
        return state

    # -- helpers of one cycle

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

    def _retire(self, models: dict[str, Any], *, now: datetime) -> list[dict[str, Any]]:
        """Drop the guard's own entries that no live rule holds any more.

        Two kinds go. An entry of a retired rule pauses a model the captain
        wants running and no rule can ever clear it, so the hysteresis of
        :meth:`_act` would hold it for ever. An entry whose resume time has
        passed pauses nothing already, so leaving it in the file only tells an
        operator a model is paused when it is not.

        A captain-owned entry is never touched, whatever its state.
        """
        actions: list[dict[str, Any]] = []
        for model, entry in sorted(models.items()):
            if (entry or {}).get("owner") != GUARD_OWNER:
                continue
            rule = str((entry or {}).get("rule"))
            resume = parse_utc((entry or {}).get("resume_at_utc"))
            if rule in RETIRED_RULES:
                note = RETIRED_RULE_NOTE
            elif resume is not None and resume <= now:
                note = "the resume time of this pause has passed"
            else:
                continue
            del models[model]
            actions.append(
                {
                    "action": "resume",
                    "model": model,
                    "vendor": (entry or {}).get("vendor"),
                    "rule": rule,
                    "reason": note,
                    "numbers": {"paused_at_utc": (entry or {}).get("paused_at_utc")},
                }
            )
        return actions

    def _act(
        self,
        *,
        pause: dict[str, Any],
        findings: list[dict[str, Any]],
        readings: dict[str, dict[str, Any]],
        memory: dict[str, Any],
        now: datetime,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Pause one model of each affected vendor and resume what no rule holds.

        Two rules of one vendor say the same thing: that vendor is running out.
        The guard therefore pauses at most one model per vendor per cycle, and
        the first fired rule of that vendor owns the pause. The captain asked
        for a pause to be rare.

        A rule whose window bounds the whole vendor pauses every candidate of
        that vendor in the cycle it fires (`pause_all`), because pausing one
        model of three leaves the other two crossing the same floor. Such a
        pause carries the window's own reset as its resume time, so the arm
        comes back without a cycle of this guard.

        Two hysteresis bounds keep a pause from flapping. A guard-owned pause is
        removed only after its rule has been clear for `RESUME_CLEAR_CYCLES`
        consecutive cycles, and a model the guard resumed is not paused again by
        the same rule for `REPAUSE_HOLD_SECONDS`. Both live in the guard's own
        memory sidecar, never in the pause file, whose captain-owned entries
        stay exactly as written.

        Returns the actions of this cycle and the holds that hysteresis applied.
        """
        models = dict(pause.get("paused_models") or {})
        actions: list[dict[str, Any]] = []
        holds: list[dict[str, Any]] = []
        actions.extend(self._retire(models, now=now))
        fired = [finding for finding in findings if finding["fired"]]
        held_by_rule: dict[str, set[str]] = {}
        for finding in fired:
            held_by_rule[finding["rule"]] = set(finding["candidate_models"])
        clear_cycles = dict(memory.get("rule_clear_cycles") or {})
        resumes = dict(memory.get("guard_resumes") or {})
        acted_vendors: set[str] = set()

        for finding in fired:
            # One pause per vendor per cycle, because two rules of one vendor
            # say the same thing: that vendor is running out. An arm-wide rule
            # is the exception: "stop this one model" and "stop the whole arm"
            # are different statements, and the arm-wide rule must still reach
            # the models the first rule did not name.
            if finding["vendor"] in acted_vendors and not finding["pause_all"]:
                continue
            already = {
                model
                for model, entry in active_pauses(
                    {"paused_models": models}, now=now
                ).items()
            }
            blocked = {}
            for model in finding["candidate_models"]:
                resumed = parse_utc(resumes.get(memory_key(model, finding["rule"])))
                if resumed is None:
                    continue
                waited = (now - resumed).total_seconds()
                if waited < REPAUSE_HOLD_SECONDS:
                    blocked[model] = int(REPAUSE_HOLD_SECONDS - waited)
            if finding["pause_all"]:
                chosen_models = [
                    model
                    for model in finding["candidate_models"]
                    if model not in already and model not in blocked
                ]
            else:
                one = select_pause_model(
                    finding["candidate_models"], readings, already | set(blocked)
                )
                chosen_models = [one] if one is not None else []
            if not chosen_models:
                if blocked:
                    holds.append(
                        {
                            "hold": "repause_hold",
                            "rule": finding["rule"],
                            "vendor": finding["vendor"],
                            "models": sorted(blocked),
                            "seconds_remaining": max(blocked.values()),
                            "hold_seconds": REPAUSE_HOLD_SECONDS,
                            "note": (
                                "the guard resumed these models on this rule "
                                "less than the hold ago"
                            ),
                        }
                    )
                continue
            if not finding["pause_all"]:
                acted_vendors.add(finding["vendor"])
            resume = finding.get("resume_at_utc")
            for chosen in chosen_models:
                reading = readings.get(chosen) or {}
                clear_cycles.pop(memory_key(chosen, finding["rule"]), None)
                models[chosen] = {
                    "reason": finding["detail"],
                    "paused_at_utc": _stamp(now),
                    "owner": GUARD_OWNER,
                    "rule": finding["rule"],
                    "vendor": finding["vendor"],
                    "numbers": finding["numbers"],
                    "cost_per_question_usd": _quantize(_cost_per_question(reading)),
                    **({"resume_at_utc": resume} if resume else {}),
                    "resume_note": (
                        f"the pause lifts by itself at {resume}, the reset of "
                        "the window this rule reads"
                        if resume
                        else "the guard removes this entry by itself after the "
                        f"condition has been clear for {RESUME_CLEAR_CYCLES} cycles"
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
                        "resume_at_utc": resume,
                        "cost_per_question_usd": _quantize(
                            _cost_per_question(reading)
                        ),
                    }
                )

        for model, entry in list(models.items()):
            if (entry or {}).get("owner") != GUARD_OWNER:
                continue
            rule = str((entry or {}).get("rule"))
            key = memory_key(model, rule)
            if model in held_by_rule.get(rule, set()):
                clear_cycles[key] = 0
                continue
            clear = int(clear_cycles.get(key) or 0) + 1
            clear_cycles[key] = clear
            if clear < RESUME_CLEAR_CYCLES:
                holds.append(
                    {
                        "hold": "clear_cycles",
                        "model": model,
                        "rule": rule,
                        "vendor": (entry or {}).get("vendor"),
                        "clear_cycles": clear,
                        "clear_cycles_required": RESUME_CLEAR_CYCLES,
                        "note": (
                            "the rule is clear, but the pause holds until it "
                            f"has been clear for {RESUME_CLEAR_CYCLES} cycles"
                        ),
                    }
                )
                continue
            del models[model]
            clear_cycles.pop(key, None)
            resumes[key] = _stamp(now)
            actions.append(
                {
                    "action": "resume",
                    "model": model,
                    "vendor": (entry or {}).get("vendor"),
                    "rule": rule,
                    "reason": (
                        f"the condition of {rule} has been clear for "
                        f"{RESUME_CLEAR_CYCLES} cycles"
                    ),
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

        memory["rule_clear_cycles"] = clear_cycles
        memory["guard_resumes"] = {
            key: stamp
            for key, stamp in resumes.items()
            if (moment := parse_utc(stamp)) is not None
            and (now - moment).total_seconds() <= CODEX_SAMPLE_MAXIMUM_AGE_SECONDS
        }
        if actions:
            pause["paused_models"] = models
            write_pause_file(self.pause_file, pause)
        return actions, holds

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
            "codex_benchmark_share_of_window": (
                state.get("codex_attribution") or {}
            ).get("benchmark_share_of_window"),
            "codex_attribution_measured": (state.get("codex_attribution") or {}).get(
                "measured"
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

    def _report_evaluator(
        self, activity: dict[str, Any], memory: dict[str, Any]
    ) -> None:
        """Tell the supervisor when the evaluator stops, and when it returns.

        The guard polls every few minutes, so it reports the change of state
        and not the state: one `blocked:` line when the evaluator stops and
        one `working:` line when it polls again. `evaluator_reported` in the
        guard memory holds what was reported last. A guard that has reported
        nothing yet reports nothing about an evaluator that runs, because a
        running evaluator is the ordinary case and there is no stop to clear.
        """
        running = bool(activity["running"])
        reported = memory.get("evaluator_reported")
        if reported == running:
            return
        memory["evaluator_reported"] = running
        if not running:
            self._append_status(f"blocked: {_evaluator_error(activity)}")
        elif reported is not None:
            self._append_status(
                f"working: the streaming evaluator is polling again "
                f"({activity['polls']} polls, {activity['evaluated_items']} items)"
            )

    def _append_status(self, line: str) -> None:
        if self.status_file is None:
            return
        try:
            with self.status_file.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        except OSError:  # pragma: no cover - environment
            return

    def _report_status(self, action: dict[str, Any]) -> None:
        """Append one `working:` line so the supervisor sees the pause."""
        if self.status_file is None:
            return
        numbers = " ".join(
            f"{name}={value}"
            for name, value in sorted((action.get("numbers") or {}).items())
            if value is not None
        )
        self._append_status(
            f"working: {action['action']}d {action['model']} on "
            f"{action['rule']} ({numbers})"
        )

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
    planned = int(evaluation.get("planned_trials") or 0)
    return {
        "item_id": row.get("item_id"),
        "recorded_at_utc": row.get("recorded_at_utc"),
        # The same rule the counts above the table use: a question is complete
        # when its responses arrived, not when its row carries the flag. The
        # flag said yes beside "36 of 48" in the trials column of the same row.
        "complete": bool(
            planned and row_recorded_trials(row) >= planned
        ),
        "row_complete_flag": bool(row.get("complete")),
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
            "coverage": question_coverage([]),
            "counts_source": "No streaming evaluation journal is selected.",
        }
    rows = read_journal_rows(journal_dir)
    items = latest_item_rows(rows)
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
    coverage = question_coverage(rows)
    message = {
        "available": (
            f"{coverage['questions_with_a_response']} questions answered on "
            f"{len(models)} models, "
            f"{coverage['questions_with_every_response']} of them with all "
            f"{coverage['planned_trials_per_question']} responses."
            if items
            else "The streaming evaluator is selected but evaluated no question yet."
        ),
        "not_started": "The streaming evaluator has not run yet.",
    }[availability]
    counts_source = (
        f"The last row of each question in {journal_dir}/{JOURNAL_FILENAME}: "
        f"{coverage['question_rows']} rows over "
        f"{coverage['questions_in_journal']} questions. A question is complete "
        "when its recorded trials reach its planned trials, not when its row "
        "carries the complete flag."
    )
    return {
        "availability": availability,
        "message": message,
        "generated_at_utc": _stamp(moment),
        "journal_dir": str(journal_dir),
        "questions_evaluated": coverage["questions_with_a_response"],
        "questions_complete": coverage["questions_with_every_response"],
        "trials_recorded": coverage["trials_recorded"],
        "coverage": coverage,
        "counts_source": counts_source,
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
    parser.add_argument("--quota-binary", default=QUOTA_COMMAND[0])
    parser.add_argument(
        "--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS
    )
    parser.add_argument(
        "--evaluator-stale-seconds", type=int, default=DEFAULT_EVALUATOR_STALE_SECONDS
    )
    parser.add_argument(
        "--codex-attribution-window-seconds",
        type=int,
        default=CODEX_ATTRIBUTION_WINDOW_SECONDS,
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
        quota_command=(str(args.quota_binary), *QUOTA_ARGUMENTS),
        recorded_quota_file=args.recorded_quota_file,
        evaluator_stale_seconds=args.evaluator_stale_seconds,
        codex_attribution_window_seconds=max(
            int(args.codex_attribution_window_seconds),
            CODEX_ATTRIBUTION_WINDOW_SECONDS,
        ),
    )
    return guard.run(interval_seconds=args.interval_seconds, once=args.once)


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
