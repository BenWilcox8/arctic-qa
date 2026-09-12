from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import __version__
from .db import Database
from .discovery import (
    crossref_exact_doi,
    crossref_reference_expansion,
    crossref_query,
    catalog_records,
    ingest_pages,
    manual_record,
    openalex_exact_doi,
    replay_pages,
)
from .errors import ArcticQAError
from .exporting import export_run
from .extraction import extract_source, load_chunks
from .generation import generate_candidate
from .manifests import write_source_manifest
from .paths import DEFAULT_DATA_ROOT, DataPaths
from .providers import make_provider
from .screening import screen_source
from .storage import fetch_source, store_original
from .util import atomic_json, canonical_json
from .validation import validate_candidate


SEED_DOIS = ("10.1111/j.1365-2419.2005.00365.x", "10.1016/j.rsase.2025.101797")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="arctic-qa",
        description="Build source-supported scientific QA records locally.",
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT))
    root.add_argument(
        "--test-mode",
        action="store_true",
        help="Permit an explicit temporary data root for tests.",
    )
    root.add_argument(
        "--json", action="store_true", help="Write one JSON result to standard output."
    )
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "doctor", help="Check the mounted data root and local dependencies."
    )

    discover = commands.add_parser(
        "discover", help="Discover and deduplicate source metadata."
    )
    discover.add_argument(
        "--adapter",
        choices=("crossref", "openalex", "replay", "manual", "catalog"),
        default="crossref",
    )
    discover.add_argument("--doi", action="append", default=[])
    discover.add_argument("--query")
    discover.add_argument("--pages", type=int, default=1)
    discover.add_argument("--per-page", type=int, default=5)
    discover.add_argument("--input", type=Path)
    discover.add_argument("--timeout", type=float, default=20)
    discover.add_argument("--references-of")
    discover.add_argument("--max-references", type=int, default=5)

    screen = commands.add_parser(
        "screen", help="Apply the versioned Arctic geography rule."
    )
    screen.add_argument("--source-id", required=True)
    screen.add_argument("--evidence", type=Path, required=True)

    fetch = commands.add_parser(
        "fetch", help="Fetch and store one immutable source object."
    )
    fetch.add_argument("--source-id", required=True)
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--media-type")
    fetch.add_argument("--max-bytes", type=int, default=50 * 1024 * 1024)
    fetch.add_argument("--timeout", type=float, default=30)
    fetch.add_argument(
        "--allow-test-file",
        action="store_true",
        help="Permit an explicit file URL only together with global --test-mode.",
    )

    extract = commands.add_parser(
        "extract", help="Extract sections and chunks from one stored source."
    )
    extract.add_argument("--source-id", required=True)
    extract.add_argument("--char-cap", type=int, default=6000)
    extract.add_argument("--overlap-chars", type=int, default=500)

    generate = commands.add_parser(
        "generate", help="Generate one candidate with separate provider roles."
    )
    generate.add_argument("--source-id", required=True)
    generate.add_argument("--run-id", required=True)
    generate.add_argument(
        "--arm", choices=("answer_first", "direct_joint"), default="answer_first"
    )
    generate.add_argument(
        "--author-provider", choices=("fake", "replay", "claude"), required=True
    )
    generate.add_argument("--author-model", required=True)
    generate.add_argument("--author-script", type=Path)
    generate.add_argument(
        "--verifier-provider", choices=("fake", "replay", "gemini"), required=True
    )
    generate.add_argument("--verifier-model", required=True)
    generate.add_argument("--verifier-script", type=Path)
    generate.add_argument("--budget-mode", choices=("usd", "tokens"), default="tokens")
    generate.add_argument("--budget-limit", type=Decimal)
    generate.add_argument("--reservation", type=Decimal, default=Decimal("100"))
    generate.add_argument("--max-output-tokens", type=int, default=2048)
    generate.add_argument("--reasoning-token-cap", type=int, default=2048)
    generate.add_argument("--billable-token-overhead", type=int, default=1024)
    generate.add_argument("--input-price-per-million", type=Decimal)
    generate.add_argument("--output-price-per-million", type=Decimal)
    generate.add_argument("--reasoning-price-per-million", type=Decimal)
    generate.add_argument("--timeout", type=float, default=60)
    generate.add_argument("--retries", type=int, default=1)
    generate.add_argument("--rate-limit-seconds", type=float, default=0)
    generate.add_argument(
        "--allow-ineligible",
        action="store_true",
        help="Test-only override for fake or replay providers.",
    )

    validate = commands.add_parser(
        "validate", help="Apply deterministic and recorded model-verification gates."
    )
    choice = validate.add_mutually_exclusive_group(required=True)
    choice.add_argument("--item-id")
    choice.add_argument("--candidate", type=Path)
    validate.add_argument("--allow-model-only-distractors", action="store_true")

    export = commands.add_parser(
        "export", help="Write stable machine-labeled JSONL exports."
    )
    export.add_argument("--run-id", required=True)
    export.add_argument("--seed", default="arctic-qa-v1")

    resume = commands.add_parser(
        "resume", help="Show resumable and ambiguous stage receipts."
    )
    resume.add_argument("--run-id", required=True)

    status = commands.add_parser(
        "status", help="Show source, item, call, and rejection counts."
    )
    status.add_argument("--run-id")

    smoke = commands.add_parser("smoke", help="Run the bounded fake-provider workflow.")
    smoke.add_argument(
        "--public",
        action="store_true",
        help="Also look up the two collaborator DOI seeds through Crossref.",
    )
    smoke.add_argument("--fixture-dir", type=Path, default=Path("fixtures"))
    smoke.add_argument("--run-id", default="smoke-r1")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return _emit(args, _doctor(args))
        paths, db = _open(args)
        try:
            handler = globals()[f"_{args.command}"]
            return _emit(args, handler(args, paths, db))
        finally:
            db.close()
    except (
        ArcticQAError,
        ValueError,
        FileNotFoundError,
        json.JSONDecodeError,
    ) as error:
        payload = {
            "status": "error",
            "code": getattr(error, "code", type(error).__name__.upper()),
            "message": str(error),
        }
        print(canonical_json(payload), file=sys.stderr)
        return 2


def _open(args) -> tuple[DataPaths, Database]:
    paths = DataPaths.open(args.data_root, test_mode=args.test_mode)
    db = Database(paths.database)
    db.migrate(paths.namespace / "backups")
    return paths, db


def _emit(args, value: Any) -> int:
    print(
        canonical_json(value) if args.json or isinstance(value, (dict, list)) else value
    )
    return 0


def _doctor(args) -> dict[str, Any]:
    result: dict[str, Any] = {
        "data_root": str(Path(args.data_root)),
        "expected_data_root": str(DEFAULT_DATA_ROOT),
        "python": sys.version.split()[0],
        "pdftotext": shutil.which("pdftotext"),
        "credentials": {
            "ANTHROPIC_API_KEY": "set"
            if os.environ.get("ANTHROPIC_API_KEY")
            else "not_set",
            "GEMINI_API_KEY": "set" if os.environ.get("GEMINI_API_KEY") else "not_set",
        },
    }
    paths = DataPaths.open(args.data_root, test_mode=args.test_mode, create=False)
    result.update(
        {"status": "ok", "mounted_writable": True, "namespace": str(paths.namespace)}
    )
    return result


def _discover(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    if args.adapter == "replay":
        if not args.input:
            raise ValueError("replay discovery requires --input")
        pages = replay_pages(args.input)
    elif args.adapter in {"manual", "catalog"}:
        if not args.input:
            raise ValueError(f"{args.adapter} discovery requires --input")
        if args.adapter == "catalog":
            records = catalog_records(args.input)
        else:
            payload = json.loads(args.input.read_text(encoding="utf-8"))
            records = payload if isinstance(payload, list) else [payload]
        result = {"added": 0, "duplicates": 0, "pages": 0}
        for record in records:
            normalized = record if record.get("source_id") else manual_record(record)
            if db.upsert_source(normalized):
                result["added"] += 1
            else:
                result["duplicates"] += 1
        result["manifest"] = write_source_manifest(db, paths.namespace)
        return result
    elif args.references_of:
        if args.adapter != "crossref":
            raise ValueError("reference expansion is available only through Crossref")
        if not 1 <= args.max_references <= 25:
            raise ValueError("--max-references must be between 1 and 25")
        pages = crossref_reference_expansion(
            args.references_of, limit=args.max_references, timeout=args.timeout
        )
    elif args.doi:
        lookup = (
            crossref_exact_doi if args.adapter == "crossref" else openalex_exact_doi
        )
        pages = (lookup(doi, timeout=args.timeout) for doi in args.doi[:5])
    elif args.query and args.adapter == "crossref":
        if not 1 <= args.pages <= 5 or not 1 <= args.per_page <= 25:
            raise ValueError(
                "discovery limits are 1 to 5 pages and 1 to 25 records per page"
            )
        pages = crossref_query(
            args.query, pages=args.pages, per_page=args.per_page, timeout=args.timeout
        )
    else:
        raise ValueError("discovery requires --doi, --query, or --input")
    result = ingest_pages(db, pages, paths.namespace / "replay")
    result["manifest"] = write_source_manifest(db, paths.namespace)
    return result


def _screen(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    result = screen_source(
        db,
        args.source_id,
        json.loads(args.evidence.read_text(encoding="utf-8")),
        paths.namespace,
    )
    result["manifest"] = write_source_manifest(db, paths.namespace)
    return result


def _fetch(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    result = fetch_source(
        db,
        paths.namespace,
        args.source_id,
        args.url,
        max_bytes=args.max_bytes,
        timeout=args.timeout,
        media_type=args.media_type,
        allow_file=bool(args.test_mode and args.allow_test_file),
    )
    result["manifest"] = write_source_manifest(db, paths.namespace)
    return result


def _extract(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    return extract_source(
        db,
        paths.namespace,
        args.source_id,
        char_cap=args.char_cap,
        overlap_chars=args.overlap_chars,
    )


def _generate(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    author = make_provider(args.author_provider, args.author_model, args.author_script)
    verifier = make_provider(
        args.verifier_provider, args.verifier_model, args.verifier_script
    )
    live = args.author_provider == "claude" or args.verifier_provider == "gemini"
    if live and (args.budget_limit is None or args.budget_limit <= 0):
        raise ValueError(
            "live mode requires --budget-limit with a value greater than zero"
        )
    if args.allow_ineligible and (
        args.author_provider not in {"fake", "replay"}
        or args.verifier_provider not in {"fake", "replay"}
    ):
        raise ValueError(
            "--allow-ineligible is available only with fake or replay providers"
        )
    if not 0 <= args.retries <= 5:
        raise ValueError("--retries must be between 0 and 5")
    if args.timeout <= 0 or args.rate_limit_seconds < 0:
        raise ValueError("timeout must be positive and rate limit must be nonnegative")
    limit = args.budget_limit if args.budget_limit is not None else Decimal("100000")
    prices = (
        args.input_price_per_million,
        args.output_price_per_million,
        args.reasoning_price_per_million,
    )
    if any(value is not None for value in prices) and not all(
        value is not None for value in prices
    ):
        raise ValueError("all three provider price fields must be supplied together")
    if args.budget_mode == "usd" and any(
        value is not None and value <= 0 for value in prices
    ):
        raise ValueError("USD provider price fields must be positive")
    pricing = (
        {"input": prices[0], "output": prices[1], "reasoning": prices[2]}
        if all(value is not None for value in prices)
        else None
    )
    return generate_candidate(
        db,
        paths.namespace,
        source_id=args.source_id,
        run_id=args.run_id,
        arm=args.arm,
        author=author,
        verifier=verifier,
        budget_mode=args.budget_mode,
        budget_limit=limit,
        reservation=args.reservation,
        timeout=args.timeout,
        retries=args.retries,
        rate_limit_seconds=args.rate_limit_seconds,
        allow_ineligible=args.allow_ineligible,
        max_output_tokens=args.max_output_tokens,
        reasoning_token_cap=args.reasoning_token_cap,
        billable_token_overhead=args.billable_token_overhead,
        pricing_usd_per_million_tokens=pricing,
    )


def _validate(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    if args.item_id:
        row = db.one(
            "SELECT candidate_json FROM candidates WHERE item_id=?", (args.item_id,)
        )
        if not row:
            raise ValueError(f"unknown candidate: {args.item_id}")
        candidate = json.loads(row["candidate_json"])
    else:
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    return validate_candidate(
        db,
        paths.namespace,
        candidate,
        strict_release=not args.allow_model_only_distractors,
        persist=bool(args.item_id),
    ).as_dict()


def _export(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    return export_run(db, paths.namespace, args.run_id, seed=args.seed)


def _resume(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    receipts = db.rows(
        "SELECT receipt_id,entity_id,stage,status,error_code,started_at FROM stage_receipts WHERE run_id=? AND status!='completed' ORDER BY started_at",
        (args.run_id,),
    )
    calls = db.rows(
        "SELECT call_id,entity_id,role,status,error_code,started_at FROM calls WHERE run_id=? AND status!='completed' ORDER BY started_at",
        (args.run_id,),
    )
    return {
        "run_id": args.run_id,
        "incomplete_stage_receipts": receipts,
        "incomplete_calls": calls,
        "manual_reconciliation_required": any(
            row["status"] == "ambiguous_charge" for row in calls
        ),
    }


def _status(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    result = {
        "sources": db.one("SELECT COUNT(*) AS count FROM sources")["count"],
        "source_states": db.rows(
            "SELECT eligibility_state,COUNT(*) AS count FROM sources GROUP BY eligibility_state ORDER BY eligibility_state"
        ),
        "candidates": db.rows(
            "SELECT status,COUNT(*) AS count FROM candidates GROUP BY status ORDER BY status"
        ),
        "rejections": db.one("SELECT COUNT(*) AS count FROM rejection_ledger")["count"],
    }
    if args.run_id:
        result["run_id"] = args.run_id
        result["calls"] = db.rows(
            "SELECT status,COUNT(*) AS count FROM calls WHERE run_id=? GROUP BY status ORDER BY status",
            (args.run_id,),
        )
        result["budget"] = db.one(
            "SELECT * FROM budgets WHERE run_id=?", (args.run_id,)
        )
    return result


def _smoke(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    fixture_dir = args.fixture_dir.resolve()
    for name in ("public-source.html", "fake-author.jsonl", "fake-verifier.jsonl"):
        if not (fixture_dir / name).is_file():
            raise FileNotFoundError(f"smoke fixture is missing: {fixture_dir / name}")
    target = paths.namespace / "runs" / args.run_id / "smoke-receipt.json"
    if target.exists():
        receipt = json.loads(target.read_text(encoding="utf-8"))
        receipt["resumed"] = True
        receipt["receipt_path"] = str(target.relative_to(paths.namespace))
        return receipt
    discovery = {"added": 0, "duplicates": 0, "pages": 0}
    if args.public:
        discovery = ingest_pages(
            db,
            (crossref_exact_doi(doi, timeout=20) for doi in SEED_DOIS),
            paths.namespace / "replay",
        )
    fixture = manual_record(
        json.loads(
            (fixture_dir / "public-source-metadata.json").read_text(encoding="utf-8")
        ),
        "public_fixture",
    )
    db.upsert_source(fixture)
    body = (fixture_dir / "public-source.html").read_bytes()
    stored = store_original(
        db,
        paths.namespace,
        fixture["source_id"],
        body,
        "text/html",
        fixture["retrieval_url"],
    )
    extracted = extract_source(
        db, paths.namespace, fixture["source_id"], char_cap=2000, overlap_chars=100
    )
    evidence_quote = "The complete study site was at 71.3 N."
    evidence_chunk = next(
        row
        for row in load_chunks(db, paths.namespace, fixture["source_id"])
        if evidence_quote in row["text"]
    )
    evidence_start = evidence_chunk["text"].index(evidence_quote)
    screen = screen_source(
        db,
        fixture["source_id"],
        {
            "evidence_kind": "site_coordinates",
            "latitudes": [71.3],
            "named_regions": [],
            "source_content_hash": stored["sha256"],
            "evidence_quote": evidence_quote,
            "locator": {
                "chunk_id": evidence_chunk["chunk_id"],
                "start_offset": evidence_start,
                "end_offset": evidence_start + len(evidence_quote),
            },
            "site_coverage": "complete",
            "test_only": True,
        },
        paths.namespace,
    )
    author = make_provider("fake", "claude-opus-5", fixture_dir / "fake-author.jsonl")
    verifier = make_provider(
        "fake", "gemini-3.1-pro-preview", fixture_dir / "fake-verifier.jsonl"
    )
    candidate = generate_candidate(
        db,
        paths.namespace,
        source_id=fixture["source_id"],
        run_id=args.run_id,
        arm="answer_first",
        author=author,
        verifier=verifier,
        budget_mode="tokens",
        budget_limit=Decimal("100000"),
        reservation=Decimal("100"),
        timeout=1,
        retries=1,
        rate_limit_seconds=0,
    )
    validated = validate_candidate(db, paths.namespace, candidate).as_dict()
    exported = export_run(db, paths.namespace, args.run_id, seed="smoke-seed")
    manifest = write_source_manifest(db, paths.namespace)
    receipt = {
        "status": "passed"
        if validated["final_label"] == "machine_accepted_unverified"
        else "failed",
        "test_only": True,
        "interpretation": "This smoke tests infrastructure. It is not a generated research result.",
        "public_discovery": discovery,
        "screen": screen,
        "stored": stored,
        "extracted": extracted,
        "item_id": candidate["item_id"],
        "validation": validated,
        "export": exported,
        "source_manifest": manifest,
    }
    atomic_json(target, receipt, immutable=True)
    receipt["receipt_path"] = str(target.relative_to(paths.namespace))
    return receipt
