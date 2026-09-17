"""The per-paper completion label of one streaming run.

The chapter 3 producer keeps no bookmark. At every start it walks the frozen
paper order from the first paper and, for every call it would make, looks for a
completed receipt in the shared ledger, re-validating each receipt and the run
authorization before it reuses that receipt. At about 5,000 receipts that walk
took about 22 minutes before the first paid call.

This module gives the run a durable per-paper label instead. A labelled paper is
skipped at the next start before any of its receipts is read. Nothing in the
ledger is altered or deleted: the label is a note about work already finished,
never a substitute for a receipt.

The label rule is in ``docs/STREAMING_DATASET.md``, section "Paper completion
labels". One function owns it, ``classify_paper``, and the producer's own
generation ladder, ``streaming._stored_generation_outcome``, answers the
generation half of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database, now
from .util import canonical_json, stable_id


COMPLETION_SCHEMA = "streaming-paper-completion-v1"

# A paper the run finished. Every class is one terminal outcome of the producer.
OUTCOME_CLASSES = (
    "eligibility_excluded",
    "eligibility_unresolved",
    "generation_accepted",
    "generation_rejected",
    "incomplete_non_mcq",
    "paper_cost_cap_reached",
)

# The producer's own disposition for one paper, mapped to the label's outcome
# class. A disposition that is not here is not a terminal outcome and takes no
# label: ``operational_unresolved`` and ``candidate_processing_fault`` leave the
# paper mid-family, and ``count_tokens_unavailable`` submitted nothing at all.
DISPOSITION_OUTCOME_CLASSES = {
    "eligibility_rejected": "eligibility_excluded",
    "eligibility_unresolved": "eligibility_unresolved",
    "accepted": "generation_accepted",
    "generation_rejected": "generation_rejected",
    "incomplete_non_mcq": "incomplete_non_mcq",
    "paper_cost_cap_reached": "paper_cost_cap_reached",
}

# Why a paper is not labelled. These are reported by the batch command and are
# never written to the state database.
INCOMPLETE_REASONS = (
    "not_full_text_ready",
    "not_screened",
    "eligibility_incomplete",
    "operational_unresolved",
    "incomplete_infra_call",
    "source_not_imported",
    "candidate_unvalidated",
    "generation_pending",
    "slot_lookup_pending",
)


def completion_id(run_id: str, candidate_key: str) -> str:
    return stable_id("paper-completion", run_id, candidate_key)


def load_completions(db: Database, *, run_id: str) -> dict[str, dict[str, Any]]:
    """Return every completion label of one run, by candidate key."""
    return {
        str(row["candidate_key"]): row
        for row in db.rows(
            """SELECT * FROM paper_completions WHERE run_id=?
            ORDER BY candidate_key""",
            (run_id,),
        )
    }


def completion_row(
    *,
    run_id: str,
    campaign_id: str,
    candidate_key: str,
    family_id: str,
    source_id: str | None,
    outcome_class: str,
    eligibility_decision: str | None,
    reason_code: str | None,
    code_commit: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render one completion label. The row is the record, not a decision."""
    if outcome_class not in OUTCOME_CLASSES:
        raise ValueError(f"unknown paper completion outcome class: {outcome_class}")
    return {
        "completion_id": completion_id(run_id, candidate_key),
        "schema": COMPLETION_SCHEMA,
        "run_id": run_id,
        "campaign_id": campaign_id,
        "candidate_key": str(candidate_key),
        "paper_family_id": str(family_id),
        "source_id": source_id,
        "outcome_class": outcome_class,
        "eligibility_decision": eligibility_decision,
        "reason_code": reason_code,
        "detail_json": canonical_json(detail or {}),
        "labelled_at_utc": now(),
        "labelled_by_commit": code_commit,
    }


_INSERT = """INSERT OR IGNORE INTO paper_completions
    (completion_id,schema,run_id,campaign_id,candidate_key,paper_family_id,
     source_id,outcome_class,eligibility_decision,reason_code,detail_json,
     labelled_at_utc,labelled_by_commit)
    VALUES (:completion_id,:schema,:run_id,:campaign_id,:candidate_key,
            :paper_family_id,:source_id,:outcome_class,:eligibility_decision,
            :reason_code,:detail_json,:labelled_at_utc,:labelled_by_commit)"""


def record_completion(db: Database, row: dict[str, Any]) -> bool:
    """Write one label. A paper already labelled keeps its first label."""
    with db.transaction():
        cursor = db.connection.execute(_INSERT, row)
    return bool(cursor.rowcount)


def record_completions(db: Database, rows: list[dict[str, Any]]) -> int:
    """Write every label of one batch in one transaction."""
    written = 0
    with db.transaction():
        for row in rows:
            written += bool(db.connection.execute(_INSERT, row).rowcount)
    return written


def label_from_disposition(
    *,
    run_id: str,
    campaign_id: str,
    candidate_key: str,
    family_id: str,
    source_id: str | None,
    disposition: str,
    reason_codes: list[str] | None,
    eligibility_decision: str | None,
    code_commit: str,
) -> dict[str, Any] | None:
    """Render the label of a paper the producer has just finished, or none.

    A disposition outside ``DISPOSITION_OUTCOME_CLASSES`` left the paper
    mid-family, so the paper keeps today's behaviour and is walked again.
    """
    outcome_class = DISPOSITION_OUTCOME_CLASSES.get(disposition)
    if outcome_class is None:
        return None
    codes = list(reason_codes or [])
    return completion_row(
        run_id=run_id,
        campaign_id=campaign_id,
        candidate_key=candidate_key,
        family_id=family_id,
        source_id=source_id,
        outcome_class=outcome_class,
        eligibility_decision=eligibility_decision,
        reason_code=codes[0] if codes else None,
        code_commit=code_commit,
        detail={
            "disposition": disposition,
            "reason_codes": codes,
            "labelled_by": "producer",
        },
    )


# ---------------------------------------------------------------------------
# The batch classification: what the run already finished, read from its state.


def _source_row(
    db: Database, *, candidate_key: str, family_id: str
) -> dict[str, Any] | None:
    rows = db.rows(
        "SELECT source_id,paper_family_id FROM sources WHERE stable_id=?",
        (candidate_key,),
    )
    for row in rows:
        if row["paper_family_id"] == family_id:
            return row
    return rows[0] if rows else None


def _operational_unresolved(
    db: Database, *, campaign_id: str, candidate_key: str
) -> str | None:
    """Return the reason of a reviewed no-replay row for this paper, or none."""
    for row in db.rows(
        """SELECT reason_code,detail_json FROM rejection_ledger
        WHERE stage='generation' ORDER BY rejection_id"""
    ):
        detail = json.loads(row["detail_json"])
        if (
            detail.get("operational_unresolved") is True
            and detail.get("campaign_id") == campaign_id
            and str(detail.get("candidate_key")) == candidate_key
        ):
            return str(row["reason_code"])
    return None


def _paper_cost_cap(
    db: Database, *, campaign_id: str, candidate_key: str
) -> dict[str, Any] | None:
    for row in db.rows(
        """SELECT reason_code,detail_json FROM rejection_ledger
        WHERE stage='paper_cost_cap' ORDER BY rejection_id"""
    ):
        detail = json.loads(row["detail_json"])
        if (
            detail.get("campaign_id") == campaign_id
            and str(detail.get("candidate_key")) == candidate_key
        ):
            return {"reason_code": str(row["reason_code"]), "detail": detail}
    return None


def classify_paper(
    db: Database,
    *,
    campaign_id: str,
    candidate_key: str,
    family_id: str,
    access: dict[str, Any] | None,
    eligibility: dict[str, Any] | None,
) -> dict[str, Any]:
    """Say whether the run reached a terminal outcome for one paper.

    The answer reads stored state only. It makes no provider call, validates no
    receipt and writes nothing. ``complete`` true carries the outcome class;
    ``complete`` false carries the reason in ``incomplete_reason``.
    """
    from . import streaming

    def incomplete(reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "candidate_key": candidate_key,
            "paper_family_id": family_id,
            "complete": False,
            "incomplete_reason": reason,
            **extra,
        }

    if access is None or access.get("access_state") != "full_text_ready":
        return incomplete("not_full_text_ready")

    unresolved = _operational_unresolved(
        db, campaign_id=campaign_id, candidate_key=candidate_key
    )
    if unresolved is not None:
        return incomplete("operational_unresolved", reason_code=unresolved)
    # A call record whose outcome is unknown leaves the family mid-flight, and
    # an ambiguous charge is exactly such a record.
    if streaming._incomplete_infra_records(db, campaign_id, family_id):
        return incomplete("incomplete_infra_call")

    capped = _paper_cost_cap(db, campaign_id=campaign_id, candidate_key=candidate_key)

    def cost_cap_reached(decision: str | None, source_id: str | None) -> dict[str, Any]:
        return {
            "candidate_key": candidate_key,
            "paper_family_id": family_id,
            "complete": True,
            "outcome_class": "paper_cost_cap_reached",
            "reason_code": capped["reason_code"],
            "eligibility_decision": decision,
            "source_id": source_id or capped["detail"].get("source_id"),
        }

    if eligibility is None or eligibility.get("execution_authority") != (
        "shared_gemini_broker"
    ):
        # The cap stopped the family before it was screened: the producer
        # records the cap and never asks for that screening again.
        if capped is not None:
            return cost_cap_reached(None, None)
        return incomplete("not_screened")
    # The stored job's own validation is the decision the producer records:
    # an invalid answer stays ``uncertain`` and is final for this prompt
    # version, whatever the job's ``state`` says about a later re-screen.
    validation = eligibility.get("validation") or {}
    decision = validation.get("decision")
    if decision not in {"eligible", "excluded", "uncertain"}:
        return incomplete("eligibility_incomplete")
    if decision != "eligible":
        reason_codes = (
            validation.get("errors") or ["eligibility_validation_unresolved"]
            if validation.get("valid") is not True
            else validation.get(
                "overall_reason_codes",
                (eligibility.get("parsed_response") or {}).get(
                    "overall_reason_codes", ["eligibility_unresolved"]
                ),
            )
        )
        unresolved_decision = validation.get("valid") is not True or (
            decision == "uncertain"
        )
        return {
            "candidate_key": candidate_key,
            "paper_family_id": family_id,
            "complete": True,
            "outcome_class": (
                "eligibility_unresolved"
                if unresolved_decision
                else "eligibility_excluded"
            ),
            "reason_code": (list(reason_codes) or [None])[0],
            "eligibility_decision": decision,
            "source_id": None,
        }

    source = _source_row(db, candidate_key=candidate_key, family_id=family_id)
    if source is None:
        return incomplete("source_not_imported", eligibility_decision=decision)
    source_id = str(source["source_id"])
    paths = streaming._generation_paths(
        db,
        campaign_id=campaign_id,
        source_id=source_id,
        family_id=family_id,
    )
    outcome = streaming._stored_generation_outcome(
        db,
        paths,
        campaign_id=campaign_id,
        family_id=family_id,
        source_id=source_id,
        slot_lookup=None,
        require_validation_events=False,
    )
    kind = outcome["kind"]
    if kind == streaming.STORED_OUTCOME_UNVALIDATED:
        return incomplete(
            "candidate_unvalidated",
            eligibility_decision=decision,
            source_id=source_id,
        )
    if kind == streaming.STORED_OUTCOME_SLOT_LOOKUP_REQUIRED:
        return incomplete(
            "slot_lookup_pending",
            eligibility_decision=decision,
            source_id=source_id,
        )
    if kind == streaming.STORED_OUTCOME_ATTEMPT:
        # The ladder wants another paid call. A family that also holds a cost
        # cap row is the family that cap stopped, and the producer never pays
        # for it again. The order matters: the producer reads the accepted
        # path before it meets the cap, so an accepted family above keeps its
        # own class even when a cap row exists.
        if capped is not None:
            return cost_cap_reached(decision, source_id)
        return incomplete(
            "generation_pending",
            eligibility_decision=decision,
            source_id=source_id,
        )
    outcome_class = DISPOSITION_OUTCOME_CLASSES[outcome["disposition"]]
    reason_codes = list(outcome.get("reason_codes") or [])
    return {
        "candidate_key": candidate_key,
        "paper_family_id": family_id,
        "complete": True,
        "outcome_class": outcome_class,
        "reason_code": reason_codes[0] if reason_codes else None,
        "eligibility_decision": decision,
        "source_id": source_id,
    }


def classify_run(
    db: Database,
    *,
    run_id: str,
    campaign_id: str,
    selection: list[dict[str, Any]],
    access_items: dict[str, dict[str, Any]],
    eligibility_jobs: dict[str, dict[str, Any]],
    max_papers: int | None = None,
) -> list[dict[str, Any]]:
    """Classify every paper of the frozen selection, in that order."""
    from .util import stable_id as _stable_id

    already = load_completions(db, run_id=run_id)
    results: list[dict[str, Any]] = []
    walked = 0
    for selected in selection:
        if max_papers is not None and walked >= max_papers:
            break
        candidate_key = str(selected.get("candidate_key"))
        access = access_items.get(candidate_key)
        if access is None or access.get("access_state") != "full_text_ready":
            continue
        walked += 1
        family_id = str(
            access.get("paper_family_id")
            or _stable_id("family", access.get("doi") or candidate_key)
        )
        if candidate_key in already:
            row = already[candidate_key]
            results.append(
                {
                    "candidate_key": candidate_key,
                    "paper_family_id": family_id,
                    "complete": True,
                    "already_labelled": True,
                    "outcome_class": str(row["outcome_class"]),
                    "reason_code": row["reason_code"],
                    "eligibility_decision": row["eligibility_decision"],
                    "source_id": row["source_id"],
                }
            )
            continue
        results.append(
            classify_paper(
                db,
                campaign_id=campaign_id,
                candidate_key=candidate_key,
                family_id=family_id,
                access=access,
                eligibility=eligibility_jobs.get(candidate_key),
            )
        )
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Count one classification by outcome class and by incomplete reason."""
    per_class: dict[str, int] = {name: 0 for name in OUTCOME_CLASSES}
    per_reason: dict[str, int] = {name: 0 for name in INCOMPLETE_REASONS}
    already = 0
    for row in results:
        if row["complete"]:
            per_class[row["outcome_class"]] += 1
            already += bool(row.get("already_labelled"))
        else:
            per_reason[row["incomplete_reason"]] += 1
    return {
        "walked": len(results),
        "complete": sum(row["complete"] for row in results),
        "already_labelled": already,
        "to_label": sum(
            row["complete"] and not row.get("already_labelled") for row in results
        ),
        "incomplete": sum(not row["complete"] for row in results),
        "per_outcome_class": per_class,
        "per_incomplete_reason": per_reason,
    }


def label_run(
    db: Database,
    *,
    run_id: str,
    campaign_id: str,
    selection: list[dict[str, Any]],
    access_items: dict[str, dict[str, Any]],
    eligibility_jobs: dict[str, dict[str, Any]],
    code_commit: str,
    max_papers: int | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Label every analyzed paper of one run, in one transaction.

    ``dry_run`` classifies and counts and writes nothing.
    """
    results = classify_run(
        db,
        run_id=run_id,
        campaign_id=campaign_id,
        selection=selection,
        access_items=access_items,
        eligibility_jobs=eligibility_jobs,
        max_papers=max_papers,
    )
    rows = [
        completion_row(
            run_id=run_id,
            campaign_id=campaign_id,
            candidate_key=row["candidate_key"],
            family_id=row["paper_family_id"],
            source_id=row.get("source_id"),
            outcome_class=row["outcome_class"],
            eligibility_decision=row.get("eligibility_decision"),
            reason_code=row.get("reason_code"),
            code_commit=code_commit,
            detail={"labelled_by": "batch"},
        )
        for row in results
        if row["complete"] and not row.get("already_labelled")
    ]
    written = 0 if dry_run else record_completions(db, rows)
    return {
        "schema": COMPLETION_SCHEMA,
        "run_id": run_id,
        "campaign_id": campaign_id,
        "code_commit": code_commit,
        "dry_run": dry_run,
        "written": written,
        "counts": summarize(results),
        "papers": results,
    }


def load_stream_inputs(
    *,
    access_run_dir: Path,
    eligibility_run_dir: Path,
    eligibility_prompt_file: Path,
    eligibility_schema_file: Path,
    eligibility_policy_file: Path,
    eligibility_rescreen_prompt_file: Path | None,
) -> dict[str, Any]:
    """Read the frozen selection, the access items and the eligibility jobs.

    These are the producer's own readers, so the batch walks exactly the papers
    and the eligibility attempts the producer walks.
    """
    from . import streaming

    access_manifest = streaming._read(access_run_dir / "run-manifest.json")
    selection = access_manifest.get("selection")
    if not isinstance(selection, list):
        raise ValueError("the article-access selection is missing")
    access_items = {
        item["candidate_key"]: item
        for item in (
            streaming._read(path)
            for path in sorted((access_run_dir / "items").glob("*.json"))
        )
    }
    eligibility_jobs = streaming._load_eligibility_jobs(
        eligibility_run_dir,
        prompt_file=eligibility_prompt_file,
        schema_file=eligibility_schema_file,
        policy_file=eligibility_policy_file,
        rescreen_prompt_file=eligibility_rescreen_prompt_file,
    )
    return {
        "selection": selection,
        "access_items": access_items,
        "eligibility_jobs": eligibility_jobs,
    }
