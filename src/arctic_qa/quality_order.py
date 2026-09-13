"""Create a deterministic, outcome-blind quality-first source order."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .full_run_plan import materialize_frozen_access_run
from .util import atomic_json, atomic_write, jsonl_bytes, sha256_file


ORDER_VERSION = "arctic-qa-quality-order-v1"
TEXT_SAMPLE_LIMIT = 16_000


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _text_features(path: Path) -> tuple[int, bool, bool, bool]:
    if not path.is_file():
        return 0, False, False, False
    with path.open(encoding="utf-8", errors="replace") as handle:
        text = handle.read(TEXT_SAMPLE_LIMIT).casefold()
    methods = "methods" in text or "methodology" in text or "materials and methods" in text
    results = "results" in text or "findings" in text
    evidence = "table" in text and ("figure" in text or "data" in text)
    return len(text), methods, results, evidence


def _rank_row(row: dict[str, Any], seed: str) -> dict[str, Any]:
    receipt = row.get("access_receipt")
    if not isinstance(receipt, dict):
        raise ValueError("the frozen row has no access receipt")
    chars, methods, results, evidence = _text_features(Path(str(receipt.get("extraction_path", ""))))
    if chars >= 8_000 and methods and results and evidence:
        band, reasons = "high", ["readable_text", "methods_and_results", "evidence_markers"]
    elif chars >= 2_500 and methods and results:
        band, reasons = "medium", ["readable_text", "methods_and_results"]
    else:
        band, reasons = "lower", ["limited_readability_or_structure"]
    if chars < 2_500:
        reasons.append("short_or_damaged_extraction")
    if not evidence:
        reasons.append("limited_evidence_markers")
    priority_tier = row.get("priority_tier")
    arctic_priority_cue = priority_tier == "positive_arctic_or_marine_cue"
    if arctic_priority_cue:
        reasons.append("frozen_arctic_priority_cue")
    else:
        reasons.append("remaining_full_text_ready")
    key = hashlib.sha256(f"{seed}\0{row['family_key']}\0{row['candidate_key']}".encode()).hexdigest()
    return {
        "candidate_key": row["candidate_key"],
        "doi": row.get("doi"),
        "family_key": row.get("family_key"),
        "title": row.get("title"),
        "original_manifest_position": row["manifest_position"],
        "source_content_hash": receipt.get("source_sha256"),
        "extraction_sha256": receipt.get("extraction_sha256"),
        "quality_band": band,
        "priority_tier": priority_tier,
        "arctic_priority_cue": arctic_priority_cue,
        "reasons": reasons,
        "text_sample_characters": chars,
        "has_methods_marker": methods,
        "has_results_marker": results,
        "has_evidence_markers": evidence,
        "within_band_seeded_key": key,
    }


def produce_quality_order(
    *, source_manifest_file: Path, descriptor_file: Path, output_dir: Path, seed: str, limit: int = 800
) -> dict[str, Any]:
    """Write full ranking and a materialized top prefix without provider calls."""
    descriptor = _read_json(descriptor_file)
    rows = [json.loads(line) for line in source_manifest_file.read_text(encoding="utf-8").splitlines()]
    if descriptor.get("schema") != "full-text-ready-freeze-descriptor-v1" or len(rows) != (descriptor.get("counts") or {}).get("manifest_records"):
        raise ValueError("the frozen manifest and descriptor do not match")
    if limit < 1 or limit > len(rows):
        raise ValueError("the selected count is outside the frozen manifest")
    ranked = [_rank_row(row, seed) for row in rows]
    band_rank = {"high": 0, "medium": 1, "lower": 2}
    ranked.sort(
        key=lambda row: (
            band_rank[row["quality_band"]],
            not row["arctic_priority_cue"],
            row["within_band_seeded_key"],
        )
    )
    for position, row in enumerate(ranked, start=1):
        row["rank"] = position
    selected_keys = {row["candidate_key"] for row in ranked[:limit]}
    by_key = {row["candidate_key"]: row for row in rows}
    selected: list[dict[str, Any]] = []
    for position, rank in enumerate(ranked[:limit], start=1):
        source = dict(by_key[rank["candidate_key"]])
        source["manifest_position"] = position
        source["original_manifest_position"] = rank["original_manifest_position"]
        source["quality_order"] = rank
        selected.append(source)
    if len(selected_keys) != limit:
        raise ValueError("the ranked selection has duplicate candidates")
    output_dir = output_dir.resolve()
    ranked_file = output_dir / "quality-order.json"
    selected_file = output_dir / f"ranked-top-{limit}.jsonl"
    selected_descriptor = output_dir / f"ranked-top-{limit}-descriptor.json"
    atomic_json(
        ranked_file,
        {
            "schema": ORDER_VERSION,
            "seed": seed,
            "source_manifest": str(source_manifest_file.resolve()),
            "source_manifest_sha256": sha256_file(source_manifest_file),
            "descriptor_sha256": sha256_file(descriptor_file),
            "ranking_inputs": "frozen_metadata_and_local_extraction_structure_only",
            "text_sample_limit_characters": TEXT_SAMPLE_LIMIT,
            "excluded_inputs": [
                "trial_qa_outcomes",
                "acceptance_labels",
                "author_prestige",
                "citation_count",
                "model_familiarity",
            ],
            "records": ranked,
        },
        immutable=True,
    )
    atomic_write(selected_file, jsonl_bytes(selected), immutable=True)
    derived_descriptor = {
        "schema": "full-text-ready-freeze-descriptor-v1",
        "state": "frozen_offline",
        "freeze_id": f"{descriptor['freeze_id']}-quality-order-{limit}-r1",
        "counts": {"manifest_records": limit, "unique_paper_families": limit},
        "source_freeze_id": descriptor["freeze_id"],
        "source_manifest_sha256": sha256_file(source_manifest_file),
        "quality_order_file": str(ranked_file),
        "quality_order_sha256": sha256_file(ranked_file),
    }
    atomic_json(selected_descriptor, derived_descriptor, immutable=True)
    access = materialize_frozen_access_run(source_manifest_file=selected_file, descriptor_file=selected_descriptor, output_dir=output_dir / f"materialized-top-{limit}")
    return {"ranking": str(ranked_file), "selected_manifest": str(selected_file), "selected_descriptor": str(selected_descriptor), "access_run": access}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an outcome-blind quality-first source order.")
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--descriptor", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--limit", type=int, default=800)
    args = parser.parse_args(argv)
    print(json.dumps(produce_quality_order(source_manifest_file=args.source_manifest, descriptor_file=args.descriptor, output_dir=args.output_dir, seed=args.seed, limit=args.limit), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
