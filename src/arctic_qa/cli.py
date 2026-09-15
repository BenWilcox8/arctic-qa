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
from .access_readiness import run_access_readiness, supervise_access_readiness
from .broker_provider import BrokerProvider
from .corpus_viewer import serve_corpus_viewer
from .db import Database, now
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
from .chapter2_corpus import CHAPTER2_DIRECTORY
from .chapter2_corpus import DEFAULT_JOBS as CHAPTER2_DEFAULT_JOBS
from .chapter2_corpus import freeze as chapter2_freeze
from .chapter2_corpus import prepare_root as chapter2_prepare_root
from .chapter2_corpus import reextract as chapter2_reextract
from .extraction import extract_source, load_chunks
from .extraction_quality import quality_report as chapter2_quality_report
from .generation import generate_candidate, resume_candidate_distractors
from .geography_correction import write_geography_correction_overlay
from .gemini_eligibility import run_gemini_eligibility
from .manifests import write_source_manifest
from .metadata_prefilter import run_metadata_prefilter
from .model_broker import SharedGeminiBroker
from .paths import DEFAULT_DATA_ROOT, DataPaths
from .providers import make_provider
from .screening import screen_source
from .source_pass import run_source_pass
from .storage import fetch_source, store_original
from .streaming import run_stream
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

    viewer = commands.add_parser(
        "corpus-view", help="Serve the read-only corpus and pipeline monitor."
    )
    viewer.add_argument("--corpus-root", type=Path, required=True)
    viewer.add_argument("--run-id", required=True)
    viewer.add_argument("--runtime-dir", type=Path, required=True)
    viewer.add_argument("--progress-file", type=Path)
    viewer.add_argument("--zotero-receipts-dir", type=Path)
    viewer.add_argument("--metadata-run-dir", type=Path)
    viewer.add_argument("--source-run-dir", type=Path)
    viewer.add_argument("--access-run-dir", type=Path)
    viewer.add_argument("--gemini-run-dir", type=Path)
    viewer.add_argument("--gemini-connection-file", type=Path)
    viewer.add_argument("--shared-ledger-file", type=Path)
    viewer.add_argument("--streaming-budget-policy-file", type=Path)
    viewer.add_argument("--streaming-progress-file", type=Path)
    viewer.add_argument("--dataset-metadata-file", type=Path)
    viewer.add_argument("--production-plan-file", type=Path)
    viewer.add_argument("--publication-package-dir", type=Path)
    viewer.add_argument("--live-dataset-dir", type=Path)
    viewer.add_argument("--project-overview-file", type=Path)
    viewer.add_argument("--research-timeline-file", type=Path)
    viewer.add_argument("--pipeline-namespace", type=Path)
    viewer.add_argument("--pipeline-db-file", type=Path)
    viewer.add_argument("--pipeline-receipts-dir", type=Path)
    viewer.add_argument(
        "--pipeline-eligibility-root", type=Path, action="append", default=[]
    )
    viewer.add_argument("--host", default="127.0.0.1")
    viewer.add_argument("--port", type=int, default=8787)
    viewer.add_argument("--stale-after-seconds", type=int, default=86400)
    viewer.add_argument("--process-stale-after-seconds", type=int, default=300)

    metadata = commands.add_parser(
        "metadata-prefilter",
        help="Build a deterministic review queue from an existing metadata ledger.",
    )
    metadata.add_argument("--candidates-file", type=Path, required=True)
    metadata.add_argument("--screening-file", type=Path, required=True)
    metadata.add_argument("--protocol-file", type=Path, required=True)
    metadata.add_argument("--policy-file", type=Path, required=True)
    metadata.add_argument("--output-dir", type=Path, required=True)
    metadata.add_argument("--run-id", required=True)
    metadata.add_argument("--code-commit", required=True)
    metadata.add_argument("--viewer-progress-file", type=Path)

    source_pass = commands.add_parser(
        "source-pass",
        help="Run one bounded, source-bound eligibility pass.",
    )
    source_pass.add_argument(
        "--action", choices=("prepare", "smoke", "continue", "decide"), required=True
    )
    source_pass.add_argument("--queue-file", type=Path, required=True)
    source_pass.add_argument("--candidates-file", type=Path, required=True)
    source_pass.add_argument("--protocol-file", type=Path, required=True)
    source_pass.add_argument("--prior-screening-file", type=Path, required=True)
    source_pass.add_argument("--policy-file", type=Path, required=True)
    source_pass.add_argument("--output-dir", type=Path, required=True)
    source_pass.add_argument("--run-id", required=True)
    source_pass.add_argument("--code-commit", required=True)
    source_pass.add_argument("--viewer-progress-file", type=Path)
    source_pass.add_argument("--decisions-file", type=Path)

    access = commands.add_parser(
        "article-access", help="Prepare or continue the frozen article access queue."
    )
    access.add_argument(
        "--action",
        choices=("prepare", "smoke10", "smoke100", "continue", "status"),
        required=True,
    )
    access.add_argument("--queue-file", type=Path, required=True)
    access.add_argument("--candidates-file", type=Path, required=True)
    access.add_argument("--protocol-file", type=Path, required=True)
    access.add_argument("--policy-file", type=Path, required=True)
    access.add_argument("--output-dir", type=Path, required=True)
    access.add_argument("--run-id", required=True)
    access.add_argument("--code-commit", required=True)
    access.add_argument("--reuse-source-run-dir", type=Path)
    access.add_argument("--reuse-access-run-dir", type=Path)
    access.add_argument("--max-items", type=int)
    access.add_argument("--max-network-seconds", type=int)
    access.add_argument("--max-new-bytes", type=int)

    access_supervisor = commands.add_parser(
        "article-access-supervise",
        help="Continue one frozen article-access run through bounded invocations.",
    )
    access_supervisor.add_argument("--queue-file", type=Path, required=True)
    access_supervisor.add_argument("--candidates-file", type=Path, required=True)
    access_supervisor.add_argument("--protocol-file", type=Path, required=True)
    access_supervisor.add_argument("--policy-file", type=Path, required=True)
    access_supervisor.add_argument("--output-dir", type=Path, required=True)
    access_supervisor.add_argument("--run-id", required=True)
    access_supervisor.add_argument("--manifest-code-commit", required=True)
    access_supervisor.add_argument("--runner-code-commit", required=True)
    access_supervisor.add_argument("--status-file", type=Path, required=True)
    access_supervisor.add_argument("--service-unit", required=True)
    access_supervisor.add_argument("--reuse-source-run-dir", type=Path)
    access_supervisor.add_argument("--reuse-access-run-dir", type=Path)
    access_supervisor.add_argument("--max-network-seconds", type=int, default=3600)

    gemini = commands.add_parser(
        "gemini-eligibility", help="Prepare or operate bounded Gemini eligibility jobs."
    )
    gemini.add_argument(
        "--action",
        choices=(
            "doctor",
            "dry-run",
            "run",
            "resume",
            "pause",
            "status",
            "geography-rescreen",
            "geography-rescreen-dry-run",
        ),
        required=True,
    )
    gemini.add_argument("--access-run-dir", type=Path, required=True)
    gemini.add_argument("--run-dir", type=Path, required=True)
    gemini.add_argument(
        "--prior-run-dir",
        type=Path,
        help="The completed run whose unresolved geography papers are re-screened.",
    )
    gemini.add_argument(
        "--config-file", type=Path, default=Path("config/gemini-eligibility-v1.json")
    )
    gemini.add_argument(
        "--prompt-file",
        type=Path,
        default=Path("config/gemini-eligibility-prompt-v3.txt"),
    )
    gemini.add_argument(
        "--schema-file",
        type=Path,
        default=Path("schemas/gemini-eligibility.v1.schema.json"),
    )
    gemini.add_argument("--policy-file", type=Path, required=True)
    gemini.add_argument("--safety-policy-file", type=Path, required=True)
    gemini.add_argument("--project-ledger-file", type=Path, required=True)
    gemini.add_argument(
        "--credential-file",
        type=Path,
        default=Path("/home/ben/.config/arctic-qa/gemini-api-key"),
    )
    gemini.add_argument("--max-cost-usd", type=Decimal, required=True)

    correction_overlay = commands.add_parser(
        "geography-correction-overlay",
        help="Write a reviewed geography-correction overlay without changing model jobs.",
    )
    correction_overlay.add_argument("--affected-papers-file", type=Path, required=True)
    correction_overlay.add_argument("--historical-jobs-dir", type=Path, required=True)
    correction_overlay.add_argument("--old-policy-file", type=Path, required=True)
    correction_overlay.add_argument("--new-policy-file", type=Path, required=True)
    correction_overlay.add_argument("--output-dir", type=Path, required=True)
    correction_overlay.add_argument("--decision-source", required=True)
    correction_overlay.add_argument("--decision-at-utc", required=True)

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

    chapter2 = commands.add_parser(
        "chapter2-corpus",
        help="Build, freeze and measure the chapter 2 column-aware corpus.",
    )
    chapter2.add_argument(
        "--action",
        choices=("prepare", "extract", "freeze", "quality"),
        required=True,
    )
    chapter2.add_argument("--access-run-dir", type=Path, required=True)
    chapter2.add_argument("--legacy-freeze-dir", type=Path, required=True)
    chapter2.add_argument("--code-commit", default="unknown")
    chapter2.add_argument("--freeze-id")
    chapter2.add_argument("--run-id")
    chapter2.add_argument("--jobs", type=int, default=CHAPTER2_DEFAULT_JOBS)
    chapter2.add_argument("--limit", type=int)
    chapter2.add_argument("--char-cap", type=int, default=6000)
    chapter2.add_argument("--overlap-chars", type=int, default=500)
    chapter2.add_argument("--sample-size", type=int, default=50)
    chapter2.add_argument("--report-file", type=Path)

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

    stream = commands.add_parser(
        "stream", help="Move eligible full-text papers through validated export."
    )
    stream.add_argument(
        "--phase",
        choices=("offline", "live_test", "away_production"),
        default="offline",
    )
    stream.add_argument("--run-id", required=True)
    stream.add_argument("--campaign-id")
    stream.add_argument("--access-run-dir", type=Path, required=True)
    stream.add_argument("--eligibility-run-dir", type=Path, required=True)
    stream.add_argument(
        "--eligibility-prompt-file",
        type=Path,
        default=Path("config/gemini-eligibility-prompt-v3.txt"),
    )
    stream.add_argument(
        "--eligibility-schema-file",
        type=Path,
        default=Path("schemas/gemini-eligibility.v1.schema.json"),
    )
    stream.add_argument("--eligibility-policy-file", type=Path)
    stream.add_argument("--author-script", type=Path)
    stream.add_argument("--verifier-script", type=Path)
    stream.add_argument("--max-papers", type=int, default=1)
    stream.add_argument("--resume-distractors-item-id")
    stream.add_argument("--resume-distractors-paper-id")
    stream.add_argument("--progress-file", type=Path)
    stream.add_argument(
        "--streaming-budget-policy-file",
        type=Path,
        default=Path("config/streaming-dataset-budget-policy-v1.json"),
    )
    stream.add_argument(
        "--price-config-file",
        type=Path,
        default=Path("config/gemini-eligibility-v1.json"),
    )
    stream.add_argument(
        "--execution-gate-file",
        type=Path,
        default=Path("config/streaming-live-execution-gate-v1.json"),
    )
    stream.add_argument("--shared-ledger-file", type=Path)
    stream.add_argument("--model-receipts-dir", type=Path)
    stream.add_argument("--ledger-config-transition-file", type=Path)
    stream.add_argument("--credential-file", type=Path)
    stream.add_argument("--prior-construction-spend-usd", type=Decimal)

    reconcile = commands.add_parser(
        "reconcile-usage",
        help="Settle one saved response that proves an omitted thought count is zero.",
    )
    reconcile.add_argument("--request-key", required=True)
    reconcile.add_argument(
        "--streaming-budget-policy-file",
        type=Path,
        default=Path("config/streaming-dataset-budget-policy-v1.json"),
    )
    reconcile.add_argument(
        "--price-config-file",
        type=Path,
        default=Path("config/gemini-eligibility-v1.json"),
    )
    reconcile.add_argument("--execution-gate-file", type=Path, required=True)
    reconcile.add_argument("--shared-ledger-file", type=Path, required=True)
    reconcile.add_argument("--model-receipts-dir", type=Path, required=True)
    reconcile.add_argument("--ledger-config-transition-file", type=Path)
    reconcile.add_argument("--credential-file", type=Path, required=True)
    reconcile.add_argument(
        "--prior-construction-spend-usd", type=Decimal, required=True
    )

    continuation = commands.add_parser(
        "authorize-ambiguous-continuation",
        help="Authorize unrelated papers after a reviewed ambiguous charge.",
    )
    continuation.add_argument("--request-key", required=True)
    continuation.add_argument("--expected-ledger-sha256", required=True)
    continuation.add_argument("--review-file", type=Path, required=True)
    continuation.add_argument("--evidence-file", type=Path, required=True)
    continuation.add_argument("--authorized-run-id", required=True)
    continuation.add_argument("--operator-id", required=True)
    continuation.add_argument(
        "--streaming-budget-policy-file", type=Path, required=True
    )
    continuation.add_argument("--price-config-file", type=Path, required=True)
    continuation.add_argument("--execution-gate-file", type=Path, required=True)
    continuation.add_argument("--shared-ledger-file", type=Path, required=True)
    continuation.add_argument("--model-receipts-dir", type=Path, required=True)
    continuation.add_argument("--ledger-config-transition-file", type=Path)
    continuation.add_argument("--credential-file", type=Path, required=True)
    continuation.add_argument(
        "--prior-construction-spend-usd", type=Decimal, required=True
    )

    orphaned = commands.add_parser(
        "authorize-orphaned-continuation",
        help="Release execution occupancy for a reviewed dead-owner request.",
    )
    orphaned.add_argument("--request-key", required=True)
    orphaned.add_argument("--expected-ledger-sha256", required=True)
    orphaned.add_argument("--review-file", type=Path, required=True)
    orphaned.add_argument("--evidence-file", type=Path, required=True)
    orphaned.add_argument("--authorized-run-id", required=True)
    orphaned.add_argument("--operator-id", required=True)
    orphaned.add_argument("--streaming-budget-policy-file", type=Path, required=True)
    orphaned.add_argument("--price-config-file", type=Path, required=True)
    orphaned.add_argument("--execution-gate-file", type=Path, required=True)
    orphaned.add_argument("--shared-ledger-file", type=Path, required=True)
    orphaned.add_argument("--model-receipts-dir", type=Path, required=True)
    orphaned.add_argument("--ledger-config-transition-file", type=Path)
    orphaned.add_argument("--credential-file", type=Path, required=True)
    orphaned.add_argument("--prior-construction-spend-usd", type=Decimal, required=True)

    settle = commands.add_parser(
        "settle-pretransport-reservation",
        help="Settle the reviewed reservation that stopped before generation transport.",
    )
    settle.add_argument("--request-key", required=True)
    settle.add_argument("--expected-ledger-sha256", required=True)
    settle.add_argument("--review-file", type=Path, required=True)
    settle.add_argument("--traceback-evidence-file", type=Path, required=True)
    settle.add_argument("--streaming-budget-policy-file", type=Path, required=True)
    settle.add_argument("--price-config-file", type=Path, required=True)
    settle.add_argument("--execution-gate-file", type=Path, required=True)
    settle.add_argument("--shared-ledger-file", type=Path, required=True)
    settle.add_argument("--model-receipts-dir", type=Path, required=True)
    settle.add_argument("--ledger-config-transition-file", type=Path)
    settle.add_argument("--credential-file", type=Path, required=True)
    settle.add_argument("--prior-construction-spend-usd", type=Decimal, required=True)
    count_error = commands.add_parser(
        "authorize-count-error-continuation",
        help="Authorize continuation after a reviewed countTokens error without replay.",
    )
    count_error.add_argument("--request-key", required=True)
    count_error.add_argument("--expected-ledger-sha256", required=True)
    count_error.add_argument("--review-file", type=Path, required=True)
    count_error.add_argument("--evidence-file", type=Path, required=True)
    count_error.add_argument("--streaming-budget-policy-file", type=Path, required=True)
    count_error.add_argument("--price-config-file", type=Path, required=True)
    count_error.add_argument("--execution-gate-file", type=Path, required=True)
    count_error.add_argument("--shared-ledger-file", type=Path, required=True)
    count_error.add_argument("--model-receipts-dir", type=Path, required=True)
    count_error.add_argument("--ledger-config-transition-file", type=Path)
    count_error.add_argument("--credential-file", type=Path, required=True)
    count_error.add_argument(
        "--prior-construction-spend-usd", type=Decimal, required=True
    )
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return _emit(args, _doctor(args))
        if args.command == "corpus-view":
            serve_corpus_viewer(
                corpus_root=args.corpus_root,
                run_id=args.run_id,
                runtime_dir=args.runtime_dir,
                progress_file=args.progress_file,
                zotero_receipts_dir=args.zotero_receipts_dir,
                metadata_run_dir=args.metadata_run_dir,
                source_run_dir=args.source_run_dir,
                access_run_dir=args.access_run_dir,
                gemini_run_dir=args.gemini_run_dir,
                gemini_connection_file=args.gemini_connection_file,
                shared_ledger_file=args.shared_ledger_file,
                streaming_budget_policy_file=args.streaming_budget_policy_file,
                streaming_progress_file=args.streaming_progress_file,
                dataset_metadata_file=args.dataset_metadata_file,
                production_plan_file=args.production_plan_file,
                publication_package_dir=args.publication_package_dir,
                live_dataset_dir=args.live_dataset_dir,
                project_overview_file=args.project_overview_file,
                research_timeline_file=args.research_timeline_file,
                pipeline_namespace=args.pipeline_namespace,
                pipeline_db_file=args.pipeline_db_file,
                pipeline_receipts_dir=args.pipeline_receipts_dir,
                pipeline_eligibility_roots=tuple(args.pipeline_eligibility_root),
                host=args.host,
                port=args.port,
                stale_after_seconds=args.stale_after_seconds,
                process_stale_after_seconds=args.process_stale_after_seconds,
            )
            return 0
        if args.command == "chapter2-corpus":
            paths = DataPaths.open(args.data_root, test_mode=args.test_mode)
            return _emit(args, _chapter2_corpus(args, paths))
        if args.command == "metadata-prefilter":
            return _emit(
                args,
                run_metadata_prefilter(
                    candidates_file=args.candidates_file,
                    screening_file=args.screening_file,
                    protocol_file=args.protocol_file,
                    policy_file=args.policy_file,
                    output_dir=args.output_dir,
                    run_id=args.run_id,
                    code_commit=args.code_commit,
                    viewer_progress_file=args.viewer_progress_file,
                ),
            )
        if args.command == "source-pass":
            return _emit(
                args,
                run_source_pass(
                    action=args.action,
                    queue_file=args.queue_file,
                    candidates_file=args.candidates_file,
                    protocol_file=args.protocol_file,
                    prior_screening_file=args.prior_screening_file,
                    policy_file=args.policy_file,
                    output_dir=args.output_dir,
                    run_id=args.run_id,
                    code_commit=args.code_commit,
                    viewer_progress_file=args.viewer_progress_file,
                    decisions_file=args.decisions_file,
                ),
            )
        if args.command == "article-access":
            return _emit(
                args,
                run_access_readiness(
                    action=args.action,
                    queue_file=args.queue_file,
                    candidates_file=args.candidates_file,
                    protocol_file=args.protocol_file,
                    policy_file=args.policy_file,
                    output_dir=args.output_dir,
                    run_id=args.run_id,
                    code_commit=args.code_commit,
                    reuse_source_run_dir=args.reuse_source_run_dir,
                    reuse_access_run_dir=args.reuse_access_run_dir,
                    max_items=args.max_items,
                    max_network_seconds=args.max_network_seconds,
                    max_new_bytes=args.max_new_bytes,
                ),
            )
        if args.command == "article-access-supervise":
            return _emit(
                args,
                supervise_access_readiness(
                    queue_file=args.queue_file,
                    candidates_file=args.candidates_file,
                    protocol_file=args.protocol_file,
                    policy_file=args.policy_file,
                    output_dir=args.output_dir,
                    run_id=args.run_id,
                    manifest_code_commit=args.manifest_code_commit,
                    runner_code_commit=args.runner_code_commit,
                    status_file=args.status_file,
                    service_unit=args.service_unit,
                    reuse_source_run_dir=args.reuse_source_run_dir,
                    reuse_access_run_dir=args.reuse_access_run_dir,
                    max_network_seconds=args.max_network_seconds,
                ),
            )
        if args.command == "gemini-eligibility":
            return _emit(
                args,
                run_gemini_eligibility(
                    action=args.action,
                    access_run_dir=args.access_run_dir,
                    run_dir=args.run_dir,
                    config_file=args.config_file,
                    prompt_file=args.prompt_file,
                    schema_file=args.schema_file,
                    policy_file=args.policy_file,
                    safety_policy_file=args.safety_policy_file,
                    project_ledger_file=args.project_ledger_file,
                    max_cost_usd=args.max_cost_usd,
                    credential_file=args.credential_file,
                    prior_run_dir=args.prior_run_dir,
                ),
            )
        if args.command == "geography-correction-overlay":
            return _emit(
                args,
                write_geography_correction_overlay(
                    affected_papers_file=args.affected_papers_file,
                    historical_jobs_dir=args.historical_jobs_dir,
                    old_policy_file=args.old_policy_file,
                    new_policy_file=args.new_policy_file,
                    output_dir=args.output_dir,
                    decision_source=args.decision_source,
                    decision_at_utc=args.decision_at_utc,
                ),
            )
        if args.command == "reconcile-usage":
            return _emit(args, _reconcile_usage(args))
        if args.command == "authorize-ambiguous-continuation":
            return _emit(args, _authorize_ambiguous_continuation(args))
        if args.command == "authorize-orphaned-continuation":
            return _emit(args, _authorize_orphaned_continuation(args))
        if args.command == "settle-pretransport-reservation":
            return _emit(args, _settle_pretransport_reservation(args))
        if args.command == "authorize-count-error-continuation":
            return _emit(args, _authorize_count_error_continuation(args))
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


def _reconcile_usage(args) -> dict[str, Any]:
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
    )
    return broker.reconcile_omitted_thought_usage(args.request_key)


def _authorize_ambiguous_continuation(args) -> dict[str, Any]:
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
    )
    return broker.authorize_ambiguous_continuation(
        request_key=args.request_key,
        expected_ledger_sha256=args.expected_ledger_sha256,
        review_file=args.review_file.resolve(),
        evidence_file=args.evidence_file.resolve(),
        authorized_run_id=args.authorized_run_id,
        operator_id=args.operator_id,
    )


def _authorize_orphaned_continuation(args) -> dict[str, Any]:
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
    )
    return broker.authorize_orphaned_request_continuation(
        request_key=args.request_key,
        expected_ledger_sha256=args.expected_ledger_sha256,
        review_file=args.review_file.resolve(),
        evidence_file=args.evidence_file.resolve(),
        authorized_run_id=args.authorized_run_id,
        operator_id=args.operator_id,
    )


def _settle_pretransport_reservation(args) -> dict[str, Any]:
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
    )
    return broker.settle_pretransport_reservation(
        request_key=args.request_key,
        expected_ledger_sha256=args.expected_ledger_sha256,
        review_file=args.review_file.resolve(),
        traceback_evidence_file=args.traceback_evidence_file.resolve(),
    )


def _authorize_count_error_continuation(args) -> dict[str, Any]:
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
    )
    return broker.authorize_count_error_continuation(
        request_key=args.request_key,
        expected_ledger_sha256=args.expected_ledger_sha256,
        review_file=args.review_file.resolve(),
        evidence_file=args.evidence_file.resolve(),
    )


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


def _chapter2_corpus(args, paths: DataPaths) -> dict[str, Any]:
    root = paths.namespace / CHAPTER2_DIRECTORY
    if args.action == "prepare":
        return chapter2_prepare_root(
            root,
            access_run_dir=args.access_run_dir,
            legacy_freeze_dir=args.legacy_freeze_dir,
            code_commit=args.code_commit,
        )
    if args.action == "extract":
        chapter2_prepare_root(
            root,
            access_run_dir=args.access_run_dir,
            legacy_freeze_dir=args.legacy_freeze_dir,
            code_commit=args.code_commit,
        )
        return chapter2_reextract(
            root,
            access_run_dir=args.access_run_dir,
            jobs=args.jobs,
            limit=args.limit,
            char_cap=args.char_cap,
            overlap_chars=args.overlap_chars,
            on_result=_chapter2_progress(root),
        )
    if args.action == "quality":
        return chapter2_quality_report(
            root,
            access_run_dir=args.access_run_dir,
            sample_size=args.sample_size,
            report_file=args.report_file,
        )
    if not args.freeze_id or not args.run_id:
        raise ValueError("the chapter 2 freeze needs a freeze id and a run id")
    return chapter2_freeze(
        root,
        access_run_dir=args.access_run_dir,
        legacy_freeze_dir=args.legacy_freeze_dir,
        freeze_id=args.freeze_id,
        run_id=args.run_id,
        code_commit=args.code_commit,
    )


def _chapter2_progress(root: Path):
    path = root / "progress" / "reextraction-progress.json"

    def report(result: dict[str, Any], counts: dict[str, int]) -> None:
        atomic_json(
            path,
            {
                "schema": "arctic-qa-chapter2-reextraction-status-v1",
                "counts": dict(counts),
                "latest": result,
                "updated_at_utc": result.get("at_utc"),
            },
        )

    return report


def _extract(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    return extract_source(
        db,
        paths.namespace,
        args.source_id,
        char_cap=args.char_cap,
        overlap_chars=args.overlap_chars,
    )


def _generate(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    if args.author_provider not in {"fake", "replay"} or args.verifier_provider not in {
        "fake",
        "replay",
    }:
        raise ValueError(
            "legacy live generation is disabled; use stream with the shared broker"
        )
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


def _stream(args, paths: DataPaths, db: Database) -> dict[str, Any]:
    if args.phase == "offline":
        if not args.author_script or not args.verifier_script:
            raise ValueError("offline streaming requires author and verifier scripts")
        author = make_provider(
            "fake", "fake-gemini-3.8-flash", args.author_script.resolve()
        )
        verifier = make_provider(
            "fake", "fake-gemini-3.8-flash", args.verifier_script.resolve()
        )
    else:
        if args.credential_file is None:
            raise ValueError("live streaming requires a private credential file")
        if args.prior_construction_spend_usd is None:
            raise ValueError("live streaming requires known prior construction spend")
        shared_dir = paths.namespace / "streaming-dataset-r1"
        broker = SharedGeminiBroker(
            policy_file=args.streaming_budget_policy_file.resolve(),
            price_config_file=args.price_config_file.resolve(),
            execution_gate_file=args.execution_gate_file.resolve(),
            ledger_file=(
                args.shared_ledger_file.resolve()
                if args.shared_ledger_file
                else shared_dir / "shared-paid-call-ledger.json"
            ),
            receipts_dir=(
                args.model_receipts_dir.resolve()
                if args.model_receipts_dir
                else shared_dir / "model-receipts"
            ),
            credential_file=args.credential_file.resolve(),
            prior_construction_spend_usd=args.prior_construction_spend_usd,
            config_transition_file=(
                args.ledger_config_transition_file.resolve()
                if args.ledger_config_transition_file
                else None
            ),
        )
        author = verifier = BrokerProvider(
            broker=broker,
            phase=args.phase,
            invocation_run_id=args.run_id,
        )
    if args.resume_distractors_item_id:
        if args.phase != "offline":
            if not args.resume_distractors_paper_id:
                raise ValueError(
                    "live targeted distractor resume requires --resume-distractors-paper-id"
                )
            broker.bind_stream_input(
                args.access_run_dir.resolve(),
                phase=args.phase,
                run_id=args.run_id,
                campaign_id=args.campaign_id or args.run_id,
                eligibility_prompt_file=args.eligibility_prompt_file.resolve(),
                eligibility_schema_file=args.eligibility_schema_file.resolve(),
                eligibility_policy_file=args.eligibility_policy_file.resolve(),
            )
            row = db.one(
                "SELECT candidate_json,paper_family_id FROM candidates WHERE item_id=?",
                (args.resume_distractors_item_id,),
            )
            if not row:
                raise ValueError(
                    f"unknown candidate: {args.resume_distractors_item_id}"
                )
            source_version_id = json.loads(row["candidate_json"])["source"][
                "content_hash"
            ]
            author = verifier = author.bind(
                paper_id=args.resume_distractors_paper_id,
                family_id=row["paper_family_id"],
                source_version_id=source_version_id,
            )
        candidate = resume_candidate_distractors(
            db,
            paths.namespace,
            item_id=args.resume_distractors_item_id,
            author=author,
            verifier=verifier,
        )
        validation = validate_candidate(db, paths.namespace, candidate).as_dict()
        accepted = (
            validation["final_label"] == "machine_accepted_unverified"
            and validation["labels"]["mcq_eligible"]
        )
        if accepted and hasattr(author, "record_accepted"):
            author.record_accepted(
                family_id=candidate["source"]["paper_family_id"],
                item_id=candidate["item_id"],
            )
        elif validation["final_label"] == "machine_accepted_unverified":
            with db.transaction():
                db.connection.execute(
                    "UPDATE candidates SET status='incomplete_non_mcq',updated_at=? WHERE item_id=?",
                    (now(), candidate["item_id"]),
                )
        exported = export_run(
            db,
            paths.namespace,
            args.campaign_id or args.run_id,
            seed="streaming-20260912",
        )
        return {
            "state": "completed",
            "targeted_regression": True,
            "source_item_id": args.resume_distractors_item_id,
            "item_id": candidate["item_id"],
            "validation": validation,
            "export": exported,
        }
    return run_stream(
        db,
        paths.namespace,
        run_id=args.run_id,
        campaign_id=args.campaign_id or args.run_id,
        access_run_dir=args.access_run_dir.resolve(),
        eligibility_run_dir=args.eligibility_run_dir.resolve(),
        author=author,
        verifier=verifier,
        max_papers=args.max_papers,
        progress_file=args.progress_file.resolve() if args.progress_file else None,
        eligibility_prompt_file=args.eligibility_prompt_file.resolve(),
        eligibility_schema_file=args.eligibility_schema_file.resolve(),
        eligibility_policy_file=(
            args.eligibility_policy_file.resolve()
            if args.eligibility_policy_file
            else None
        ),
    )


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
