from __future__ import annotations

import hashlib
import json
from pathlib import Path

from arctic_qa.quality_order import produce_quality_order


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frozen_row(
    *, position: int, source: Path, extraction: Path, priority_tier: str, title: str
) -> dict:
    return {
        "schema": "full-text-ready-manifest-item-v1",
        "manifest_position": position,
        "candidate_key": f"10.1234/source-{position}",
        "doi": f"10.1234/source-{position}",
        "family_key": f"10.1234/source-{position}",
        "title": title,
        "authors": ["A. Author"],
        "year": 2025,
        "priority_tier": priority_tier,
        "tier_position": position,
        "scientific_eligibility": "unresolved",
        "access_receipt": {
            "access_state": "full_text_ready",
            "identity_verified": True,
            "source_path": str(source),
            "source_sha256": _sha256(source),
            "source_bytes": source.stat().st_size,
            "extraction_path": str(extraction),
            "extraction_sha256": _sha256(extraction),
            "extraction_bytes": extraction.stat().st_size,
            "media_type": "application/pdf",
            "item_sha256": "a" * 64,
        },
    }


def test_quality_order_is_deterministic_and_materializes_without_provider_calls(
    tmp_path: Path,
) -> None:
    originals = tmp_path / "originals"
    originals.mkdir()
    sources = [originals / f"source-{position}.pdf" for position in range(1, 4)]
    for source in sources:
        source.write_bytes(b"%PDF-1.4\nfixture\n")
    extractions = [originals / f"source-{position}.txt" for position in range(1, 4)]
    complete_text = ("Methods Results Table Figure Arctic evidence. " * 250).strip()
    extractions[0].write_text(complete_text, encoding="utf-8")
    extractions[1].write_text(complete_text, encoding="utf-8")
    extractions[2].write_text("Editorial front matter.", encoding="utf-8")
    rows = [
        _frozen_row(
            position=1,
            source=sources[0],
            extraction=extractions[0],
            priority_tier="positive_arctic_or_marine_cue",
            title="Arctic empirical study",
        ),
        _frozen_row(
            position=2,
            source=sources[1],
            extraction=extractions[1],
            priority_tier="remaining_full_text_ready",
            title="Other empirical study",
        ),
        _frozen_row(
            position=3,
            source=sources[2],
            extraction=extractions[2],
            priority_tier="positive_arctic_or_marine_cue",
            title="Short Arctic record",
        ),
    ]
    frozen = tmp_path / "freeze" / "manifest.jsonl"
    frozen.parent.mkdir()
    frozen.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    descriptor = frozen.with_name("descriptor.json")
    descriptor.write_text(
        json.dumps(
            {
                "schema": "full-text-ready-freeze-descriptor-v1",
                "state": "frozen_offline",
                "freeze_id": "fixture-freeze-r1",
                "counts": {"manifest_records": 3, "unique_paper_families": 3},
            }
        ),
        encoding="utf-8",
    )
    before = {
        path: path.read_bytes() for path in [*sources, *extractions, frozen, descriptor]
    }
    first = produce_quality_order(
        source_manifest_file=frozen,
        descriptor_file=descriptor,
        output_dir=tmp_path / "output",
        seed="quality-fixture-r1",
        limit=2,
    )
    second = produce_quality_order(
        source_manifest_file=frozen,
        descriptor_file=descriptor,
        output_dir=tmp_path / "output",
        seed="quality-fixture-r1",
        limit=2,
    )

    assert first == second
    assert {path: path.read_bytes() for path in before} == before
    ranked = json.loads(Path(first["ranking"]).read_text(encoding="utf-8"))["records"]
    assert [row["candidate_key"] for row in ranked] == [
        "10.1234/source-1",
        "10.1234/source-2",
        "10.1234/source-3",
    ]
    selected = [
        json.loads(line)
        for line in Path(first["selected_manifest"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["original_manifest_position"] for row in selected] == [1, 2]
    assert (Path(first["access_run"]["access_run_dir"]) / "run-manifest.json").is_file()
