"""Cost journal of the streaming evaluation: one row per evaluated question.

Each row answers the captain's question of 2026-09-16: what did this question
cost to build, what did it cost to evaluate, and what do the numbers project
to for the whole dataset. A row holds:

- the question id, its paper family and the campaign;
- the generation cost: the paid calls that the construction ledger booked to
  that item's paper family, and the campaign's running spend divided by the
  accepted items so far;
- the evaluation cost: the Gemini USD of that item in the evaluation phase of
  the shared ledger, plus the token counts and the list-price equivalent of
  every Claude Code and Codex call, marked as subscription and not charged;
- the per-model N1 to N5 outcomes and the invalid (N0) count;
- the wall time of the 48-trial plan and the cumulative totals.

Nothing here charges anything. The list-price equivalent is informational: it
multiplies the recorded token counts by the vendor's published API price, so
the captain can see what the subscription calls would have cost. The file
``config/benchmark-evaluation-list-prices-v1.json`` holds those rates and is
never bound into a gate.
"""

from __future__ import annotations

import fcntl
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import db

from . import ledger_store

from .abstention_providers import COMPLETED
from .abstention_subscription import (
    PROVIDER_ANTHROPIC_CLAUDE_CODE,
    PROVIDER_OPENAI_CODEX,
)
from .abstention_render import TAXONOMY
from .abstention_score import metrics_from_counts
from .abstention_set import ACCEPTED_STATUS
from .model_broker import EVALUATION_PHASE
from .util import atomic_json, canonical_json


JOURNAL_SCHEMA = "abstention-eval-cost-journal-v1"
JOURNAL_ROW_SCHEMA = "abstention-eval-cost-row-v1"
SUMMARY_SCHEMA = "abstention-eval-cost-summary-v1"
JOURNAL_FILENAME = "cost-journal.jsonl"
JOURNAL_HEADER_FILENAME = "cost-journal-manifest.json"
DEFAULT_LIST_PRICE_FILE = Path("config/benchmark-evaluation-list-prices-v1.json")
LIST_PRICE_SCHEMA = "benchmark-evaluation-list-prices-v1"
MILLION = Decimal("1000000")
CENT = Decimal("0.000001")
PAUSE_KIND = "vendor_pause"
RESUME_KIND = "vendor_resume"
ITEM_KIND = "item"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _money(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _quantize(value: Decimal) -> str:
    return str(value.quantize(CENT))


# --- List prices ---------------------------------------------------------------


def load_list_prices(path: Path) -> dict[str, Any]:
    """Read the informational list-price file of the subscription vendors."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != LIST_PRICE_SCHEMA:
        raise ValueError("unsupported benchmark evaluation list-price schema")
    for vendor, entry in (value.get("vendors") or {}).items():
        if vendor not in (PROVIDER_ANTHROPIC_CLAUDE_CODE, PROVIDER_OPENAI_CODEX):
            raise ValueError(f"the list-price file names an unknown vendor: {vendor}")
        if not isinstance(entry.get("models"), dict) or not entry["models"]:
            raise ValueError(f"the list-price vendor {vendor} lists no model")
    return value


def list_price_equivalent_usd(
    prices: dict[str, Any], vendor: str, model: str, usage: dict[str, Any] | None
) -> Decimal | None:
    """Return what one subscription call would cost at the vendor's API price.

    The native token fields of each harness decide the rate: Claude splits the
    input into fresh input, cache writes and cache reads; Codex splits it into
    input and cached input. The output price covers the thinking tokens, which
    both harnesses already include in their output count.
    """
    entry = (
        ((prices.get("vendors") or {}).get(vendor) or {}).get("models", {}).get(model)
    )
    if entry is None or not usage:
        return None
    native = usage.get("native") or {}
    if vendor == PROVIDER_ANTHROPIC_CLAUDE_CODE:
        fresh = int(native.get("input_tokens", 0) or 0)
        writes = int(native.get("cache_creation_input_tokens", 0) or 0)
        reads = int(native.get("cache_read_input_tokens", 0) or 0)
        output = int(native.get("output_tokens", 0) or 0)
        if not any((fresh, writes, reads, output)):
            fresh = int(usage.get("promptTokenCount", 0) or 0)
            output = int(usage.get("candidatesTokenCount", 0) or 0) + int(
                usage.get("thoughtsTokenCount", 0) or 0
            )
        total = (
            Decimal(fresh) * _money(entry["input"])
            + Decimal(writes) * _money(entry["cache_write_5m"])
            + Decimal(reads) * _money(entry["cache_read"])
            + Decimal(output) * _money(entry["output"])
        )
        return total / MILLION
    inputs = int(native.get("input_tokens", 0) or 0)
    cached = int(native.get("cached_input_tokens", 0) or 0)
    output = int(native.get("output_tokens", 0) or 0)
    if not any((inputs, cached, output)):
        inputs = int(usage.get("promptTokenCount", 0) or 0)
        output = int(usage.get("candidatesTokenCount", 0) or 0) + int(
            usage.get("thoughtsTokenCount", 0) or 0
        )
    fresh = max(inputs - cached, 0)
    total = (
        Decimal(fresh) * _money(entry["input"])
        + Decimal(cached) * _money(entry["cached_input"])
        + Decimal(output) * _money(entry["output"])
    )
    return total / MILLION


# --- Generation cost from the construction ledger --------------------------------


def read_ledger(path: Path) -> dict[str, Any]:
    """Read the shared paid-call ledger read-only, under its own lock."""
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


def construction_totals(
    ledger: dict[str, Any], *, run_prefixes: tuple[str, ...]
) -> dict[str, Any]:
    """Sum the construction spend of one campaign from its ledger run ids.

    The construction ledger records the stream invocation as ``run_id``; a
    campaign runs under one or more of those ids. ``run_prefixes`` selects
    them. Evaluation requests are excluded: they are a different phase.
    """
    spend = Decimal("0")
    calls = 0
    tokens = {"input": 0, "output": 0, "thinking": 0}
    for request in ledger["requests"].values():
        if request.get("phase") == EVALUATION_PHASE:
            continue
        run_id = str(request.get("run_id") or "")
        if run_prefixes and not run_id.startswith(run_prefixes):
            continue
        if request.get("state") != "completed":
            continue
        spend += _money(request.get("actual_cost_usd"))
        calls += 1
        usage = request.get("usage") or {}
        tokens["input"] += int(usage.get("promptTokenCount", 0) or 0)
        tokens["output"] += int(usage.get("candidatesTokenCount", 0) or 0)
        tokens["thinking"] += int(usage.get("thoughtsTokenCount", 0) or 0)
    return {"spent_usd": spend, "calls": calls, "tokens": tokens}


def family_generation_cost(
    ledger: dict[str, Any], family_id: str, *, run_prefixes: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Return the paid calls that the construction ledger booked to one family."""
    spend = Decimal("0")
    calls = 0
    stages: dict[str, int] = {}
    tokens = {"input": 0, "output": 0, "thinking": 0}
    for request in ledger["requests"].values():
        if request.get("family_id") != family_id:
            continue
        if request.get("phase") == EVALUATION_PHASE:
            continue
        run_id = str(request.get("run_id") or "")
        if run_prefixes and not run_id.startswith(run_prefixes):
            continue
        if request.get("state") != "completed":
            continue
        spend += _money(request.get("actual_cost_usd"))
        calls += 1
        stage = str(request.get("stage") or "unknown")
        stages[stage] = stages.get(stage, 0) + 1
        usage = request.get("usage") or {}
        tokens["input"] += int(usage.get("promptTokenCount", 0) or 0)
        tokens["output"] += int(usage.get("candidatesTokenCount", 0) or 0)
        tokens["thinking"] += int(usage.get("thoughtsTokenCount", 0) or 0)
    paper = (ledger.get("papers") or {}).get(family_id) or {}
    return {
        "family_id": family_id,
        "paid_calls": calls,
        "calls_by_stage": dict(sorted(stages.items())),
        "usd": _quantize(spend),
        "tokens": tokens,
        "ledger_paper_spent_usd": str(paper.get("spent_usd") or "0"),
    }


def accepted_item_count(state_db: Path, campaign_id: str) -> int:
    """Count the accepted candidates of one campaign in the state database."""
    def query() -> Any:
        connection = db.connect_read_only(state_db)
        try:
            return connection.execute(
                "SELECT COUNT(*) FROM candidates WHERE run_id=? AND status=?",
                (campaign_id, ACCEPTED_STATUS),
            ).fetchone()
        finally:
            connection.close()

    row = db.retry_locked_read(query)
    return int(row[0]) if row else 0


def generation_cost_record(
    *,
    ledger: dict[str, Any],
    state_db: Path,
    campaign_id: str,
    family_id: str,
    run_prefixes: tuple[str, ...],
) -> dict[str, Any]:
    """Return the generation cost of one item: its family and its campaign share."""
    family = family_generation_cost(ledger, family_id, run_prefixes=run_prefixes)
    campaign = construction_totals(ledger, run_prefixes=run_prefixes)
    accepted = accepted_item_count(state_db, campaign_id)
    per_item = campaign["spent_usd"] / Decimal(accepted) if accepted else None
    return {
        "campaign_id": campaign_id,
        "ledger_run_prefixes": list(run_prefixes),
        "family": family,
        "campaign_spent_usd": _quantize(campaign["spent_usd"]),
        "campaign_paid_calls": campaign["calls"],
        "campaign_accepted_items": accepted,
        "campaign_usd_per_accepted_item": (
            _quantize(per_item) if per_item is not None else None
        ),
    }


# --- Evaluation cost of one item -------------------------------------------------


def gemini_evaluation_cost(
    ledger: dict[str, Any], *, run_id: str, item_id: str
) -> dict[str, Any]:
    """Sum the evaluation-phase Gemini spend of one item in one run."""
    family_id = f"evaluation-item:{item_id}"
    spend = Decimal("0")
    reserved = Decimal("0")
    ambiguous = Decimal("0")
    calls = 0
    tokens = {"input": 0, "output": 0, "thinking": 0}
    for request in ledger["requests"].values():
        if request.get("phase") != EVALUATION_PHASE:
            continue
        if request.get("family_id") != family_id or request.get("run_id") != run_id:
            continue
        state = request.get("state")
        if state == "completed":
            spend += _money(request.get("actual_cost_usd"))
            calls += 1
            usage = request.get("usage") or {}
            tokens["input"] += int(usage.get("promptTokenCount", 0) or 0)
            tokens["output"] += int(usage.get("candidatesTokenCount", 0) or 0)
            tokens["thinking"] += int(usage.get("thoughtsTokenCount", 0) or 0)
        elif state == "submitted":
            reserved += _money(request.get("reserved_usd"))
        elif state == "ambiguous_charge":
            ambiguous += _money(request.get("reserved_usd"))
    return {
        "charged_calls": calls,
        "usd": _quantize(spend),
        "reserved_usd": _quantize(reserved),
        "ambiguous_usd": _quantize(ambiguous),
        "tokens": tokens,
    }


def subscription_evaluation_cost(
    rows: list[dict[str, Any]], prices: dict[str, Any], vendor: str
) -> dict[str, Any]:
    """Token counts and the list-price equivalent of one vendor's calls."""
    tokens = {"input": 0, "output": 0, "thinking": 0}
    equivalent = Decimal("0")
    by_model: dict[str, dict[str, Any]] = {}
    calls = 0
    for row in rows:
        response = row["response"]
        if response["state"] != COMPLETED:
            continue
        usage = response.get("usage") or {}
        calls += 1
        model = row["model"]
        entry = by_model.setdefault(
            model,
            {
                "calls": 0,
                "tokens": {"input": 0, "output": 0, "thinking": 0},
                "list_price_equivalent_usd": Decimal("0"),
            },
        )
        entry["calls"] += 1
        pairs = (
            ("input", "promptTokenCount"),
            ("output", "candidatesTokenCount"),
            ("thinking", "thoughtsTokenCount"),
        )
        for name, key in pairs:
            value = int(usage.get(key, 0) or 0)
            tokens[name] += value
            entry["tokens"][name] += value
        amount = list_price_equivalent_usd(prices, vendor, model, usage)
        if amount is not None:
            equivalent += amount
            entry["list_price_equivalent_usd"] += amount
    return {
        "vendor": vendor,
        "billing": "subscription",
        "charged_usd": "0",
        "calls": calls,
        "tokens": tokens,
        "list_price_equivalent_usd": _quantize(equivalent),
        "by_model": {
            model: {
                **entry,
                "list_price_equivalent_usd": _quantize(
                    entry["list_price_equivalent_usd"]
                ),
            }
            for model, entry in sorted(by_model.items())
        },
    }


def outcome_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per-model N0 to N5 counts of one item's rows."""
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        counts = result.setdefault(row["model"], {name: 0 for name in TAXONOMY})
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return dict(sorted(result.items()))


def cost_row(
    *,
    item: dict[str, Any],
    run_id: str,
    plan_id: str,
    rows_by_vendor: dict[str, list[dict[str, Any]]],
    ledger: dict[str, Any],
    prices: dict[str, Any],
    generation: dict[str, Any],
    wall_seconds: float,
    planned_trials: int,
    vendors_paused: list[str] | None = None,
    vendors_excluded: list[str] | None = None,
    models_paused: list[str] | None = None,
    pending_paused_trials: int = 0,
    cumulative: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one cost-journal row for one evaluated question.

    ``models_paused`` names the models the evaluator did not call for this
    item, and ``pending_paused_trials`` counts the trials they hold. A row
    with a held trial is not a complete item: the evaluator returns to that
    item after the resume time and appends a later row that supersedes this
    one. Read a run's totals through :meth:`CostJournal.latest_item_rows`.

    ``vendors_paused`` names the vendors that stopped on an earlier item of
    this invocation, and they owe this item their trials, so a row with one is
    not complete either. ``vendors_excluded`` names the vendors ``--vendors``
    left out, which owe nothing, because the operator chose the scope of the
    run. The two were one field until 2026-09-17, and an item evaluated while
    the Claude Code arm was paused was journalled complete with 30 of its 48
    trials.
    """
    all_rows = [row for rows in rows_by_vendor.values() for row in rows]
    gemini = gemini_evaluation_cost(ledger, run_id=run_id, item_id=item["item_id"])
    subscription = {
        vendor: subscription_evaluation_cost(
            rows_by_vendor.get(vendor, []), prices, vendor
        )
        for vendor in (PROVIDER_ANTHROPIC_CLAUDE_CODE, PROVIDER_OPENAI_CODEX)
        if vendor in rows_by_vendor
    }
    subscription_equivalent = sum(
        (_money(entry["list_price_equivalent_usd"]) for entry in subscription.values()),
        Decimal("0"),
    )
    subscription_tokens = {
        name: sum(entry["tokens"][name] for entry in subscription.values())
        for name in ("input", "output", "thinking")
    }
    invalid = [row for row in all_rows if not row["valid"]]
    return {
        "schema": JOURNAL_ROW_SCHEMA,
        "kind": ITEM_KIND,
        "item_id": item["item_id"],
        "family_id": item["family_id"],
        "source": item.get("source"),
        "eval_set_id": item.get("eval_set_id"),
        "run_id": run_id,
        "plan_id": plan_id,
        "generation": generation,
        "evaluation": {
            "planned_trials": planned_trials,
            "recorded_trials": len(all_rows),
            "google_gemini": {**gemini, "billing": "api_credits"},
            "subscription": subscription,
            "subscription_tokens": subscription_tokens,
            "subscription_list_price_equivalent_usd": _quantize(
                subscription_equivalent
            ),
            "charged_usd": gemini["usd"],
            "vendors_paused": sorted(vendors_paused or []),
            "vendors_excluded": sorted(vendors_excluded or []),
            "models_paused": sorted(models_paused or []),
            "pending_paused_trials": int(pending_paused_trials),
            "complete": int(pending_paused_trials) == 0 and not (vendors_paused or []),
            "wall_seconds": round(float(wall_seconds), 3),
        },
        "outcomes_by_model": outcome_counts(all_rows),
        "invalid_count": len(invalid),
        "invalid_rate": round(len(invalid) / len(all_rows), 4) if all_rows else None,
        "cumulative": cumulative or {},
        "recorded_at_utc": _utc_now(),
    }


def pause_row(
    *, run_id: str, vendor: str, reason: str, remaining_usd: str | None = None
) -> dict[str, Any]:
    """Build one journal row that records a paused vendor."""
    return {
        "schema": JOURNAL_ROW_SCHEMA,
        "kind": PAUSE_KIND,
        "run_id": run_id,
        "vendor": vendor,
        "reason": reason,
        "evaluation_remaining_usd": remaining_usd,
        "recorded_at_utc": _utc_now(),
    }


def resume_row(*, run_id: str, vendor: str, paused_reason: str) -> dict[str, Any]:
    """Build one journal row that records a resumed vendor.

    A vendor paused for an ambiguous charge waits on a supervisor release, not
    on a restart. This row is the record of the resume the evaluator made on
    its own, and it names the pause it lifted.
    """
    return {
        "schema": JOURNAL_ROW_SCHEMA,
        "kind": RESUME_KIND,
        "run_id": run_id,
        "vendor": vendor,
        "paused_reason": paused_reason,
        "recorded_at_utc": _utc_now(),
    }


# --- Journal file ----------------------------------------------------------------


class CostJournal:
    """Append-only journal: one row per evaluated question, plus pause rows."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / JOURNAL_FILENAME
        self.manifest_path = directory / JOURNAL_HEADER_FILENAME
        if not self.manifest_path.is_file():
            atomic_json(
                self.manifest_path,
                {
                    "schema": JOURNAL_SCHEMA,
                    "journal_file": JOURNAL_FILENAME,
                    "created_at_utc": _utc_now(),
                    "note": (
                        "One row per evaluated question. Gemini USD is charged to the "
                        "Google API credits through the shared ledger. Claude Code and "
                        "Codex rows are subscription calls at USD 0; their "
                        "list_price_equivalent_usd is informational only."
                    ),
                },
            )

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def item_rows(self) -> list[dict[str, Any]]:
        """Every item row in file order, including a superseded one."""
        return [row for row in self.rows() if row.get("kind", ITEM_KIND) == ITEM_KIND]

    def latest_item_rows(self) -> list[dict[str, Any]]:
        """The last row of each item, in the order the items first appeared.

        An item that a paused model held is evaluated again after the resume
        time. Its later row holds the item's whole cost, because the Gemini
        cost comes from the ledger and the outcomes come from the run
        directory, and both are cumulative per item. So the totals read the
        last row of each item and never add a superseded one.
        """
        latest: dict[str, dict[str, Any]] = {}
        for row in self.item_rows():
            latest[str(row["item_id"])] = row
        return list(latest.values())

    @staticmethod
    def row_is_complete(row: dict[str, Any]) -> bool:
        """True when no paused model and no paused vendor owes this item.

        The vendor half is read here and not only from the row's own
        ``complete`` flag, because the rows a run wrote before 2026-09-17 set
        that flag from the paused models alone. One of them, written while the
        Claude Code arm was paused, called an item complete with 30 of its 48
        trials.
        """
        evaluation = row.get("evaluation") or {}
        if evaluation.get("vendors_paused"):
            return False
        if "complete" in evaluation:
            return bool(evaluation["complete"])
        return int(evaluation.get("pending_paused_trials", 0) or 0) == 0

    def completed_item_ids(self) -> set[str]:
        """The items the evaluator must not evaluate again.

        An item whose latest row still holds paused trials is not complete, so
        a later pass takes it up again and runs only the trials that are
        missing. The run directory keeps every recorded trial, so the second
        pass calls no model twice.
        """
        return {
            str(row["item_id"])
            for row in self.latest_item_rows()
            if self.row_is_complete(row)
        }

    def held_item_ids(self) -> set[str]:
        """The items a paused model holds, in journal order."""
        return {
            str(row["item_id"])
            for row in self.latest_item_rows()
            if not self.row_is_complete(row)
        }

    def items_held_by(self, paused: frozenset[str] | set[str]) -> set[str]:
        """The held items whose every missing trial belongs to a paused model.

        Such an item cannot advance while those models stay paused: a revisit
        records nothing and calls nothing. So the evaluator leaves it alone
        and takes it up when the pause lifts. An item held for any other
        reason is revisited as usual, because a later pass can finish it.
        """
        result = set()
        for row in self.latest_item_rows():
            if self.row_is_complete(row):
                continue
            models = (row.get("evaluation") or {}).get("models_paused") or []
            if models and all(model in paused for model in models):
                result.add(str(row["item_id"]))
        return result

    def items_awaiting_vendors(self, paused: frozenset[str] | set[str]) -> set[str]:
        """The items whose every missing trial belongs to a still-paused vendor.

        This is the vendor half of :meth:`items_held_by`. Such an item cannot
        advance while those vendors stay paused: a revisit records nothing and
        calls nothing. A start clears a vendor pause, so the next invocation
        takes the item up and runs only the trials that are missing.
        """
        result = set()
        for row in self.latest_item_rows():
            if self.row_is_complete(row):
                continue
            evaluation = row.get("evaluation") or {}
            if evaluation.get("models_paused"):
                continue
            vendors = evaluation.get("vendors_paused") or []
            if vendors and all(vendor in paused for vendor in vendors):
                result.add(str(row["item_id"]))
        return result

    def append(self, row: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(row) + "\n")
            handle.flush()

    def cumulative(self) -> dict[str, Any]:
        """Totals over every item so far, for the next row's cumulative block."""
        rows = self.latest_item_rows()
        gemini = sum(
            (_money(row["evaluation"]["google_gemini"]["usd"]) for row in rows),
            Decimal("0"),
        )
        generation = sum(
            (_money(row["generation"]["family"]["usd"]) for row in rows), Decimal("0")
        )
        equivalent = sum(
            (
                _money(row["evaluation"]["subscription_list_price_equivalent_usd"])
                for row in rows
            ),
            Decimal("0"),
        )
        tokens = {
            name: sum(
                int(row["evaluation"]["subscription_tokens"][name]) for row in rows
            )
            for name in ("input", "output", "thinking")
        }
        trials = sum(int(row["evaluation"]["recorded_trials"]) for row in rows)
        wall = sum(float(row["evaluation"]["wall_seconds"]) for row in rows)
        return {
            "items": len(rows),
            "trials": trials,
            "generation_family_usd": _quantize(generation),
            "evaluation_gemini_usd": _quantize(gemini),
            "subscription_tokens": tokens,
            "subscription_list_price_equivalent_usd": _quantize(equivalent),
            "wall_seconds": round(wall, 3),
        }


# --- Summary ---------------------------------------------------------------------


def summarize_journal(
    directory: Path, *, project_items: int | None = None
) -> dict[str, Any]:
    """Return the run-so-far summary the captain reads to judge rigor and budget."""
    journal = CostJournal(directory)
    rows = journal.latest_item_rows()
    pauses = [row for row in journal.rows() if row.get("kind") == PAUSE_KIND]
    items = len(rows)
    held = [row for row in rows if not journal.row_is_complete(row)]
    totals = journal.cumulative()
    gemini = _money(totals["evaluation_gemini_usd"])
    generation = _money(totals["generation_family_usd"])
    equivalent = _money(totals["subscription_list_price_equivalent_usd"])
    per_item = {
        "generation_family_usd": _quantize(generation / items) if items else None,
        "evaluation_gemini_usd": _quantize(gemini / items) if items else None,
        "subscription_list_price_equivalent_usd": (
            _quantize(equivalent / items) if items else None
        ),
        "subscription_tokens": {
            name: round(totals["subscription_tokens"][name] / items, 1)
            if items
            else None
            for name in ("input", "output", "thinking")
        },
        "trials": round(totals["trials"] / items, 1) if items else None,
        "wall_seconds": round(totals["wall_seconds"] / items, 1) if items else None,
    }
    campaign = rows[-1]["generation"] if rows else {}
    outcomes: dict[str, dict[str, int]] = {}
    for row in rows:
        for model, counts in row["outcomes_by_model"].items():
            target = outcomes.setdefault(model, {name: 0 for name in TAXONOMY})
            for name, value in counts.items():
                target[name] = target.get(name, 0) + int(value)
    metrics = {
        model: model_metrics(counts) for model, counts in sorted(outcomes.items())
    }
    projection = None
    if project_items and items:
        projection = {
            "items": int(project_items),
            "evaluation_gemini_usd": _quantize(gemini / items * Decimal(project_items)),
            "generation_family_usd": _quantize(
                generation / items * Decimal(project_items)
            ),
            "subscription_list_price_equivalent_usd": _quantize(
                equivalent / items * Decimal(project_items)
            ),
            "subscription_tokens": {
                name: int(totals["subscription_tokens"][name] / items * project_items)
                for name in ("input", "output", "thinking")
            },
            "wall_hours": round(
                totals["wall_seconds"] / items * project_items / 3600, 2
            ),
        }
    return {
        "schema": SUMMARY_SCHEMA,
        "journal_dir": str(directory),
        "accepted_items_evaluated": items,
        "complete_items": items - len(held),
        "items_held_by_a_paused_model": len(held),
        "paused_models": sorted(
            {
                model
                for row in held
                for model in (row["evaluation"].get("models_paused") or [])
            }
        ),
        "pending_paused_trials": sum(
            int(row["evaluation"].get("pending_paused_trials", 0) or 0) for row in held
        ),
        "totals": totals,
        "per_item": per_item,
        "campaign": {
            "campaign_id": campaign.get("campaign_id"),
            "campaign_spent_usd": campaign.get("campaign_spent_usd"),
            "campaign_accepted_items": campaign.get("campaign_accepted_items"),
            "campaign_usd_per_accepted_item": campaign.get(
                "campaign_usd_per_accepted_item"
            ),
        },
        "projection": projection,
        "per_model": metrics,
        "paused_vendors": [
            {
                "vendor": row["vendor"],
                "reason": row["reason"],
                "at": row["recorded_at_utc"],
            }
            for row in pauses
        ],
        "updated_at_utc": _utc_now(),
    }


def model_metrics(counts: dict[str, int]) -> dict[str, Any]:
    """The abstention metrics of one model from its N0 to N5 counts.

    The metrics come from :func:`arctic_qa.abstention_score.metrics_from_counts`,
    so the journal summary and the scorer can never drift apart. N0 trials are
    excluded from the metrics and reported as the invalid rate.
    """
    tally = {name: int(counts.get(name, 0)) for name in TAXONOMY}
    valid = sum(value for name, value in tally.items() if name != "N0")
    metrics = metrics_from_counts(tally)
    return {
        "counts": tally,
        "trials": valid + tally["N0"],
        "valid_trials": valid,
        "invalid_rate": (
            round(tally["N0"] / (valid + tally["N0"]), 4)
            if valid + tally["N0"]
            else None
        ),
        **{
            name: (round(value, 4) if isinstance(value, float) else value)
            for name, value in metrics.items()
        },
    }


__all__ = [
    "CostJournal",
    "DEFAULT_LIST_PRICE_FILE",
    "ITEM_KIND",
    "JOURNAL_FILENAME",
    "PAUSE_KIND",
    "RESUME_KIND",
    "accepted_item_count",
    "construction_totals",
    "cost_row",
    "family_generation_cost",
    "gemini_evaluation_cost",
    "generation_cost_record",
    "list_price_equivalent_usd",
    "load_list_prices",
    "model_metrics",
    "outcome_counts",
    "pause_row",
    "resume_row",
    "read_ledger",
    "subscription_evaluation_cost",
    "summarize_journal",
]
