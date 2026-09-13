"""Create a read-only quality report from an Arctic QA export."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


TARGET_VALIDITY_PERCENT = 95


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must contain a JSON object")
            rows.append(value)
    return rows


def _export_file(manifest: dict[str, Any], source_root: Path, name: str) -> Path | None:
    files = manifest.get("files")
    if not isinstance(files, dict) or name not in files:
        return None
    value = files[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"the export manifest file entry for {name} is invalid")
    path = Path(value)
    return path if path.is_absolute() else source_root / path


def _rate(passed: int, total: int) -> dict[str, Any]:
    return {
        "available": True,
        "passed": passed,
        "total": total,
        "percent": round(100 * passed / total, 1) if total else None,
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _has_evidence(row: dict[str, Any]) -> bool:
    evidence = row.get("evidence") or row.get("answer_evidence")
    return (
        isinstance(evidence, dict)
        and bool(evidence.get("quote"))
        and isinstance(evidence.get("locator"), dict)
    )


def _quality_result(row: dict[str, Any], names: Iterable[str]) -> bool | None:
    containers = (row, row.get("checks"), row.get("quality"), row.get("quality_checks"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        for name in names:
            value = container.get(name)
            if isinstance(value, bool):
                return value
    return None


def _observed_check(
    rows: Iterable[dict[str, Any]], names: Iterable[str]
) -> dict[str, Any]:
    values = [
        result for row in rows if (result := _quality_result(row, names)) is not None
    ]
    if not values:
        return _unavailable(
            "The selected export does not retain this automated result."
        )
    return _rate(sum(values), len(values))


def _source_ids(rows: Iterable[dict[str, Any]]) -> set[str]:
    result = set()
    for row in rows:
        source = row.get("source")
        if isinstance(source, dict) and isinstance(source.get("source_id"), str):
            result.add(source["source_id"])
    return result


def _source_identity(rows: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    source_ids: set[str] = set()
    content_hashes: set[str] = set()
    for row in rows:
        source = row.get("source")
        if not isinstance(source, dict):
            continue
        if isinstance(source.get("source_id"), str):
            source_ids.add(source["source_id"])
        for name in ("content_hash", "source_version"):
            if isinstance(source.get(name), str):
                content_hashes.add(source[name])
    return {
        "source_ids": sorted(source_ids),
        "source_content_hashes": sorted(content_hashes),
    }


def _fixture_input(manifest: dict[str, Any], status: dict[str, Any] | None) -> bool:
    indicators = ("fixture", "test_only", "test_mode", "synthetic")
    return any(manifest.get(name) is True for name in indicators) or bool(
        status and any(status.get(name) is True for name in indicators)
    )


def _spend(
    ledger: dict[str, Any] | None, status: dict[str, Any] | None
) -> dict[str, Any]:
    source = ledger or {}
    if not source and status:
        budget = status.get("budget")
        source = budget if isinstance(budget, dict) else status
    if not source:
        return _unavailable("No ledger or status budget was selected.")
    values = {
        name: source.get(name)
        for name in (
            "prior_construction_spend_usd",
            "spent_usd",
            "reserved_usd",
            "ambiguous_reserved_usd",
            "limit_value",
            "project_cap_usd",
            "run_allocation_usd",
        )
        if source.get(name) is not None
    }
    if not values:
        return _unavailable(
            "The selected ledger or status has no recognized spend fields."
        )
    result: dict[str, Any] = {"available": True, **values}
    if "prior_construction_spend_usd" in values and "spent_usd" in values:
        try:
            result["known_cumulative_spend_usd"] = str(
                Decimal(str(values["prior_construction_spend_usd"]))
                + Decimal(str(values["spent_usd"]))
            )
        except InvalidOperation:
            result["known_cumulative_spend_usd"] = None
    return result


def _configuration(
    manifest: dict[str, Any],
    ledger: dict[str, Any] | None,
    status: dict[str, Any] | None,
) -> dict[str, Any]:
    result = {
        name: manifest.get(name)
        for name in (
            "schema_version",
            "export_id",
            "run_id",
            "shuffle_seed",
            "release_label_ceiling",
        )
        if manifest.get(name) is not None
    }
    for source in (ledger, status):
        if not isinstance(source, dict):
            continue
        for name in ("schema", "policy_sha256", "price_config_sha256", "phase"):
            if source.get(name) is not None:
                result[name] = source[name]
    if isinstance(status, dict) and isinstance(status.get("provider_policy"), dict):
        result["provider_policy"] = status["provider_policy"]
    return result


def build_summary(
    export_manifest: Path,
    *,
    source_root: Path,
    ledger_file: Path | None = None,
    status_file: Path | None = None,
) -> dict[str, Any]:
    """Read selected inputs and return a report without changing those inputs."""
    manifest = _read_json(export_manifest)
    ledger = _read_json(ledger_file) if ledger_file else None
    status = _read_json(status_file) if status_file else None
    short_answer_file = _export_file(manifest, source_root, "short_answer")
    incomplete_file = _export_file(manifest, source_root, "incomplete_short_answer")
    mcq_file = _export_file(manifest, source_root, "mcq")
    rejection_file = _export_file(manifest, source_root, "rejections")
    short_answers = _read_jsonl(short_answer_file) if short_answer_file else []
    incomplete = _read_jsonl(incomplete_file) if incomplete_file else []
    mcqs = _read_jsonl(mcq_file) if mcq_file else []
    rejections = _read_jsonl(rejection_file) if rejection_file else []

    base_items = [*short_answers, *incomplete]
    source_evidence_rows = [row for row in base_items if _has_evidence(row)]
    reference_answers = [
        row
        for row in base_items
        if isinstance(row.get("reference_answers"), list) and row["reference_answers"]
    ]
    distractors = [
        option
        for row in mcqs
        for option in row.get("options", [])
        if isinstance(option, dict) and option.get("is_correct") is False
    ]
    documented_distractors = [
        option
        for option in distractors
        if isinstance(option.get("falsity_evidence"), dict)
        and option["falsity_evidence"].get("quote")
    ]
    reason_counts = Counter(
        str(row.get("reason_code", "unknown")) for row in rejections
    )
    stage_counts = Counter(str(row.get("stage", "unknown")) for row in rejections)
    disagreements = sum(
        "disagreement" in reason
        for reason in reason_counts
        for _ in range(reason_counts[reason])
    )
    all_export_rows = [*base_items, *mcqs]
    agreement = _observed_check(
        all_export_rows,
        ("reconstruction_agreement", "automated_agreement", "agreement"),
    )
    qa_pass = _observed_check(
        all_export_rows, ("qa_check_passed", "automated_check_passed")
    )
    fixture = _fixture_input(manifest, status)
    release_labels = Counter(
        str(row["release_label"])
        for row in all_export_rows
        if isinstance(row.get("release_label"), str)
    )

    return {
        "report_schema": "arctic-qa-quality-summary-v1",
        "input": {
            "export_manifest": str(export_manifest),
            "source_root": str(source_root),
            "ledger": str(ledger_file) if ledger_file else None,
            "status": str(status_file) if status_file else None,
            "evidence_mode": "fixture" if fixture else "selected_export",
        },
        "configuration": _configuration(manifest, ledger, status),
        "items": {
            "accepted_short_answer": len(short_answers),
            "incomplete_short_answer": len(incomplete),
            "mcq": len(mcqs),
            "unique_sources": len(_source_ids(all_export_rows)),
            "exported_release_labels": dict(sorted(release_labels.items())),
        },
        "source_identity": _source_identity(all_export_rows),
        "check_coverage": {
            "source_evidence": _rate(len(source_evidence_rows), len(base_items)),
            "reference_answers": _rate(len(reference_answers), len(base_items)),
            "distractor_falsity_evidence": _rate(
                len(documented_distractors), len(distractors)
            ),
            "recorded_qa_check_pass": qa_pass,
            "recorded_automated_agreement": agreement,
        },
        "rejections": {
            "available": rejection_file is not None,
            "count": len(rejections) if rejection_file else None,
            "by_stage": dict(sorted(stage_counts.items())),
            "by_reason": dict(sorted(reason_counts.items())),
            "recorded_disagreement_count": disagreements if rejection_file else None,
        },
        "spend": _spend(ledger, status),
        "validity": {
            "target_validity_percent": TARGET_VALIDITY_PERCENT,
            "observed_automated_check_pass_rate": qa_pass,
            "observed_automated_agreement_rate": agreement,
            "independently_established_scientific_accuracy": "unknown_not_yet_estimable",
            "missing_evidence": [
                "A sampled set with independently established reference judgments.",
                "A documented sampling plan and a defined accuracy unit.",
                "A blind comparison against those reference judgments.",
                "An uncertainty interval for the sampled accuracy estimate.",
            ],
        },
        "limitations": [
            "Automated check pass rates and model agreement do not establish scientific accuracy.",
            "Construction and verification models can share training data, prompts, or systematic blind spots.",
            "Missing fields are reported as unavailable. The reporter does not infer a pass from release membership.",
            *(
                ["This input is a fixture. It is not live research evidence."]
                if fixture
                else []
            ),
        ],
    }


def render_markdown(summary: dict[str, Any]) -> str:
    """Render a compact human-readable report from a summary object."""
    items = summary["items"]
    coverage = summary["check_coverage"]
    validity = summary["validity"]
    rejections = summary["rejections"]
    spend = summary["spend"]
    lines = ["# Arctic QA quality summary", ""]
    if summary["input"]["evidence_mode"] == "fixture":
        lines.extend(
            ["> Fixture input. This report is not live research evidence.", ""]
        )
    lines.extend(
        [
            "## Dataset",
            "",
            f"- Accepted short-answer items: {items['accepted_short_answer']}",
            f"- Incomplete short-answer items: {items['incomplete_short_answer']}",
            f"- MCQ items: {items['mcq']}",
            f"- Unique sources: {items['unique_sources']}",
            f"- Exported release labels: {json.dumps(items['exported_release_labels'], sort_keys=True)}",
            "",
            "## Automated coverage",
            "",
        ]
    )
    for name, value in coverage.items():
        label = name.replace("_", " ")
        if value["available"]:
            rate = (
                "not estimable" if value["percent"] is None else f"{value['percent']}%"
            )
            lines.append(f"- {label}: {value['passed']}/{value['total']} ({rate})")
        else:
            lines.append(f"- {label}: unavailable. {value['reason']}")
    lines.extend(["", "## Rejections and disagreement", ""])
    if rejections["available"]:
        lines.append(f"- Rejections: {rejections['count']}")
        lines.append(
            f"- Recorded disagreement rejections: {rejections['recorded_disagreement_count']}"
        )
        lines.append(
            f"- Rejection stages: {json.dumps(rejections['by_stage'], sort_keys=True)}"
        )
    else:
        lines.append("- Rejections: unavailable. The export has no rejection file.")
    lines.extend(["", "## Spend", ""])
    if spend["available"]:
        for name, value in spend.items():
            if name != "available":
                lines.append(f"- {name.replace('_', ' ')}: {value}")
    else:
        lines.append(f"- Unavailable. {spend['reason']}")
    lines.extend(
        [
            "",
            "## Validity statement",
            "",
            f"- Target validity: {validity['target_validity_percent']}%.",
            "- Independently established scientific accuracy: unknown and not yet estimable.",
            "- Automated pass and agreement values are process observations. They do not establish scientific accuracy.",
            "",
            "## Later bounded independent automated recheck",
            "",
            "1. Freeze the export manifest and record its hash.",
            "2. Draw a seeded random sample of accepted base questions. Record the seed and sample size before the recheck.",
            "3. Use a different model configuration. Hide construction answers and prior automated results from that model.",
            "4. Compare the recheck result with the frozen evidence and a prewritten rubric.",
            "5. Report the agreement proportion and a 95% Wilson interval. Label this result as automated agreement, not scientific accuracy.",
            "",
            "## Limitations",
            "",
            *[f"- {limit}" for limit in summary["limitations"]],
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write an offline Arctic QA quality summary."
    )
    parser.add_argument("--export-manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = build_summary(
        args.export_manifest,
        source_root=args.source_root,
        ledger_file=args.ledger,
        status_file=args.status,
    )
    args.json_out.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.markdown_out.write_text(render_markdown(summary), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
