"""Chapter 2 corpus: column-aware extraction, sentence chunks and the re-freeze.

Every test here pins a defect the r15 holistic acceptance audit named in section
4.3 and in the eligibility stage analysis, findings E3 and E5.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from arctic_qa.chapter2_corpus import (
    CHAPTER2_DIRECTORY,
    chapter2_root,
    freeze,
    index_record,
    prepare_root,
    reextract,
)
from arctic_qa.db import Database
from arctic_qa.extraction import (
    build_records,
    extract_document,
    extract_source,
    load_chunks,
)
from arctic_qa.extraction_quality import (
    legacy_chunks,
    measure_chunks,
    measure_text,
    quality_report,
)
from arctic_qa.pdf_layout import extract_layout, normalize_presentation
from arctic_qa.storage import store_original
from arctic_qa.text_structure import drop_running_heads, sentence_spans
from arctic_qa.util import canonical_json, jsonl_bytes, sha256_bytes

from pdf_fixture import two_column_pdf


LEFT = [
    "Sea ice cover declined over the Chukchi",
    "Sea during the survey. Sampling ran",
    "from June to August 2019 at 71.2 N.",
    "The mean was 12.4 mg per litre.",
]
RIGHT = [
    "A second column states a different",
    "result. The southern transect recorded",
    "3.1 mg per litre in the same season,",
    "which the survey treats separately.",
]
GUTTER = re.compile(r"[A-Za-z,.;:)][ ]{3,}[A-Za-z(]")


def _two_column(tmp_path: Path, name: str = "source.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(two_column_pdf(LEFT, RIGHT, heading="Results"))
    return path


def test_two_column_page_is_read_one_column_at_a_time(tmp_path: Path) -> None:
    """Audit 4.3: 81 of 95 evidence quotes carried a column gutter."""
    path = _two_column(tmp_path)

    legacy = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert any(GUTTER.search(line) for line in legacy.splitlines())

    blocks, columns = extract_layout(path)
    assert columns == {1}
    texts = [block["text"] for block in blocks]
    assert not any(GUTTER.search(text) for text in texts)
    assert "Sea ice cover declined over the Chukchi Sea during the survey." in " ".join(
        texts
    )
    assert "The southern transect recorded 3.1 mg per litre" in " ".join(texts)
    left = next(text for text in texts if text.startswith("Sea ice cover"))
    assert "A second column" not in left


def test_a_word_split_by_a_line_wrap_is_joined(tmp_path: Path) -> None:
    """Audit 4.3: eligibility spans carried mid-word line breaks."""
    path = tmp_path / "wrap.pdf"
    path.write_bytes(
        two_column_pdf(
            ["Arctic per-", "mafrost thawed in the A-1 female popula-", "tion."],
            [],
        )
    )
    blocks, _ = extract_layout(path)
    joined = " ".join(block["text"] for block in blocks)
    assert "Arctic permafrost" in joined
    assert "A-1 female population" in joined
    assert "per- mafrost" not in joined


def test_ligatures_and_soft_hyphens_are_folded() -> None:
    """Audit 4.3: the scope phrase 'Baffin Island' bound as a ligature."""
    damaged = "South Baf" + chr(0xFB01) + "n Island" + chr(0x00AD) + "s   were"
    assert normalize_presentation(damaged) == "South Baffin Islands were"


def test_a_repeated_running_head_is_removed() -> None:
    """A journal running head interrupts every section and lands in quotes."""
    blocks = []
    for page in range(1, 6):
        blocks.append(
            {
                "page": page,
                "page_height": 800.0,
                "y0": 10.0,
                "y1": 20.0,
                "x0": 50.0,
                "x1": 200.0,
                "line_count": 1,
                "text": f"The Cryosphere, 18, 1911-1924, 2024 {page}",
            }
        )
        blocks.append(
            {
                "page": page,
                "page_height": 800.0,
                "y0": 100.0,
                "y1": 400.0,
                "x0": 50.0,
                "x1": 500.0,
                "line_count": 20,
                "text": f"Body text of page {page} with a real result in it.",
            }
        )
    kept, removed = drop_running_heads(blocks)
    assert removed == 5
    assert all("Cryosphere" not in block["text"] for block in kept)


def test_chunks_start_and_end_on_a_sentence(tmp_path: Path) -> None:
    """Audit E5: a scope window that ends mid-clause killed a good item."""
    sections, _, _ = extract_document(_two_column(tmp_path), "application/pdf")
    _, chunks = build_records("src-test", "a" * 64, sections, 120, 40)
    assert chunks
    for chunk in chunks:
        assert chunk["sentence_complete"] is True
        assert chunk["text"] == chunk["text"].strip()
    tails = [chunk["text"][-1] for chunk in chunks]
    assert all(tail in ".!?" for tail in tails if tail not in ")\"'")


def test_the_sentence_splitter_keeps_abbreviations_and_decimals() -> None:
    text = (
        "Samples came from 78 N (Fig. 1). The mean was 3.4 mg/L, per Smith et al. 2020."
    )
    spans = [text[start:stop] for start, stop in sentence_spans(text)]
    assert spans == [
        "Samples came from 78 N (Fig. 1).",
        "The mean was 3.4 mg/L, per Smith et al. 2020.",
    ]


def test_chunks_keep_page_and_heading_locators(tmp_path: Path) -> None:
    sections, _, coverage = extract_document(_two_column(tmp_path), "application/pdf")
    assert coverage["pages"] == 1
    assert coverage["column_pages"] == 1
    rows, chunks = build_records("src-test", "b" * 64, sections, 6000, 500)
    assert any(row["heading"] == "Results" for row in rows)
    for chunk in chunks:
        assert chunk["page"] == 1
        assert chunk["page_start"] == 1 and chunk["page_end"] == 1
        assert isinstance(chunk["heading_path"], list)


def test_no_block_is_lost_between_the_pages_and_the_sections(tmp_path: Path) -> None:
    """A heading block must stay in the text, not become metadata only."""
    path = tmp_path / "headings.pdf"
    path.write_bytes(
        two_column_pdf(
            [
                "Methods",
                "We sampled the Chukchi Sea in 2019.",
                "Results",
                "The mean was 12.4 mg per litre.",
                "Discussion",
                "The mean is higher than the earlier survey.",
            ],
            [],
        )
    )
    sections, _, coverage = extract_document(path, "application/pdf")
    assert sum(len(section["blocks"]) for section in sections) == coverage["blocks"]
    text = "\n".join(
        block["text"] for section in sections for block in section["blocks"]
    )
    assert len(text) == coverage["characters"]
    for heading in ("Methods", "Results", "Discussion"):
        assert heading in text
    assert [section["heading"] for section in sections][-3:] == [
        "Methods",
        "Results",
        "Discussion",
    ]


def _namespace(tmp_path: Path) -> tuple[Path, Database]:
    namespace = tmp_path / "arctic-qa"
    for name in ("originals", "parsed", "chunks", "manifests", "backups"):
        (namespace / name).mkdir(parents=True, exist_ok=True)
    database = Database(namespace / "state.sqlite3")
    database.migrate(namespace / "backups")
    return namespace, database


def _source(database: Database, namespace: Path, path: Path) -> str:
    from arctic_qa.discovery import manual_record

    record = manual_record(
        {"doi": "10.1234/ch2", "title": "A chapter two paper", "authors": []},
        "test",
    )
    database.upsert_source(record)
    store_original(
        database,
        namespace,
        record["source_id"],
        path.read_bytes(),
        "application/pdf",
        "https://example.org/ch2.pdf",
    )
    return record["source_id"]


def test_extraction_writes_under_the_chapter_two_root(tmp_path: Path) -> None:
    namespace, database = _namespace(tmp_path)
    source_id = _source(database, namespace, _two_column(tmp_path))
    root = namespace / CHAPTER2_DIRECTORY
    prepare_root(
        root,
        access_run_dir=tmp_path,
        legacy_freeze_dir=tmp_path,
        code_commit="test",
    )
    assert chapter2_root(namespace) == root

    before = sorted(p.name for p in (namespace / "chunks").iterdir())
    metadata = extract_source(database, namespace, source_id)
    database.close()

    assert metadata["corpus_root"] == str(root)
    assert (
        (root / metadata["chunk_relative_path"]).is_file()
        if metadata.get("chunk_relative_path")
        else True
    )
    assert sorted(p.name for p in (namespace / "chunks").iterdir()) == before
    assert list((root / "chunks").rglob("chunks.jsonl"))
    assert list((root / "parsed").rglob("sections.jsonl"))


def test_extraction_reuses_the_frozen_chapter_two_parse(tmp_path: Path) -> None:
    """The run must read the exact objects the freeze receipt hashed."""
    namespace, database = _namespace(tmp_path)
    pdf = tmp_path / "paper-1.pdf"
    pdf.write_bytes(two_column_pdf(LEFT, RIGHT, heading="Results 1"))
    pdf.with_suffix(".txt").write_text("", encoding="utf-8")
    access = _access_run(tmp_path, [pdf])
    root = namespace / CHAPTER2_DIRECTORY
    prepare_root(
        root,
        access_run_dir=access,
        legacy_freeze_dir=_legacy_freeze(tmp_path, ["10.1234/paper-1"]),
        code_commit="test",
    )
    reextract(root, access_run_dir=access, jobs=1)
    frozen = index_record(root, sha256_bytes(pdf.read_bytes()))
    assert frozen is not None

    from arctic_qa.discovery import manual_record

    record = manual_record(
        {"doi": "10.1234/paper-1", "title": "Paper 1", "authors": []}, "test"
    )
    assert record["source_id"] == frozen["source_id"]
    database.upsert_source(record)
    store_original(
        database,
        namespace,
        record["source_id"],
        pdf.read_bytes(),
        "application/pdf",
        "https://example.org/paper.pdf",
    )
    metadata = extract_source(database, namespace, record["source_id"])
    chunks = load_chunks(database, namespace, record["source_id"])
    database.close()

    assert metadata["frozen_corpus_parse"] is True
    assert metadata["parse_sha256"] == frozen["parse_sha256"]
    assert metadata["chunk_sha256"] == frozen["chunk_sha256"]
    assert len(chunks) == frozen["chunks"]
    assert all(row["sentence_complete"] for row in chunks)


def _access_run(tmp_path: Path, pdfs: list[Path]) -> Path:
    run = tmp_path / "access"
    (run / "items").mkdir(parents=True)
    selection = []
    for position, pdf in enumerate(pdfs, start=1):
        key = f"10.1234/paper-{position}"
        selection.append(
            {
                "position": position,
                "candidate_key": key,
                "subgroup": "retained_article_type",
                "doi": key,
                "title": f"Paper {position}",
            }
        )
        (run / "items" / f"item-{position:06d}.json").write_text(
            canonical_json(
                {
                    "schema": "article-access-item-v1",
                    "run_id": "legacy",
                    "position": position,
                    "candidate_key": key,
                    "doi": key,
                    "title": f"Paper {position}",
                    "access_state": "full_text_ready",
                    "identity_verified": True,
                    "media_type": "application/pdf",
                    "final_url": "https://example.org/paper.pdf",
                    "subgroup": "retained_article_type",
                    "source_path": str(pdf),
                    "source_content_hash": sha256_bytes(pdf.read_bytes()),
                    "extraction_path": str(pdf.with_suffix(".txt")),
                    "extraction_sha256": sha256_bytes(b""),
                    "extraction_coverage": {"article_body_recognized": True},
                }
            ),
            encoding="utf-8",
        )
    (run / "run-manifest.json").write_text(
        canonical_json(
            {
                "schema": "article-access-manifest-v1",
                "run_id": "legacy",
                "policy_id": "p",
                "protocol_id": "q",
                "inputs": {},
                "target_counts": {},
                "target_total": len(selection),
                "selection_keys_sha256": "0" * 64,
                "limits": {},
                "smoke_sizes": [],
                "selection": selection,
            }
        ),
        encoding="utf-8",
    )
    (run / "progress.json").write_text(
        canonical_json({"schema": "article-access-progress-v1", "state": "completed"}),
        encoding="utf-8",
    )
    (run / "run-receipt.json").write_text(
        canonical_json({"schema": "article-access-run-receipt-v1", "run_id": "legacy"}),
        encoding="utf-8",
    )
    return run


def _legacy_freeze(tmp_path: Path, keys: list[str]) -> Path:
    directory = tmp_path / "legacy-freeze"
    directory.mkdir()
    (directory / "full-text-ready-manifest.jsonl").write_bytes(
        jsonl_bytes(
            [
                {"manifest_position": position, "candidate_key": key}
                for position, key in enumerate(keys, start=1)
            ]
        )
    )
    (directory / "freeze-receipt.json").write_text(
        canonical_json({"freeze_id": "legacy"}), encoding="utf-8"
    )
    return directory


@pytest.fixture
def corpus(tmp_path: Path) -> dict[str, Any]:
    pdfs = []
    for index in range(1, 4):
        path = tmp_path / f"paper-{index}.pdf"
        path.write_bytes(two_column_pdf(LEFT, RIGHT, heading=f"Results {index}"))
        path.with_suffix(".txt").write_text("", encoding="utf-8")
        pdfs.append(path)
    access = _access_run(tmp_path, pdfs)
    legacy = _legacy_freeze(tmp_path, [f"10.1234/paper-{i}" for i in (1, 2, 3)])
    root = tmp_path / "arctic-qa" / CHAPTER2_DIRECTORY
    prepare_root(
        root, access_run_dir=access, legacy_freeze_dir=legacy, code_commit="test"
    )
    return {"root": root, "access": access, "legacy": legacy, "pdfs": pdfs}


def test_reextraction_is_resumable_and_hashes_every_object(corpus) -> None:
    first = reextract(corpus["root"], access_run_dir=corpus["access"], jobs=1, limit=2)
    assert first == {"total": 2, "resumed": 0, "failed": 0, "extracted": 2}

    second = reextract(corpus["root"], access_run_dir=corpus["access"], jobs=1)
    assert second["resumed"] == 2
    assert second["extracted"] == 1
    assert second["failed"] == 0

    digest = sha256_bytes(corpus["pdfs"][0].read_bytes())
    record = index_record(corpus["root"], digest)
    assert record is not None
    assert record["chunks"] >= 1
    assert record["sentence_complete_chunks"] == record["chunks"]
    assert record["coverage"]["column_pages"] == 1
    assert (
        sha256_bytes(Path(record["extraction_path"]).read_bytes())
        == record["extraction_sha256"]
    )


def test_the_freeze_writes_an_order_a_receipt_and_an_access_run(corpus) -> None:
    reextract(corpus["root"], access_run_dir=corpus["access"], jobs=1)
    receipt = freeze(
        corpus["root"],
        access_run_dir=corpus["access"],
        legacy_freeze_dir=corpus["legacy"],
        freeze_id="chapter2-test-r1",
        run_id="chapter2-test",
        code_commit="test",
    )
    assert receipt["counts"]["manifest_records"] == 3
    assert receipt["checks"]["ready_count_exact"] is True
    assert receipt["extractor"]["command"] == "pdftotext -bbox-layout -nodiag"
    assert receipt["derives_from"]["legacy_manifest"]["sha256"]
    assert len(receipt["hashes"]["order_sha256"]) == 64

    freeze_dir = corpus["root"] / "corpus-freeze" / "chapter2-test-r1"
    records = [
        json.loads(line)
        for line in (freeze_dir / "chapter2-corpus-manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert [row["manifest_position"] for row in records] == [1, 2, 3]
    for row in records:
        assert len(row["parse_sha256"]) == 64
        assert len(row["chunk_sha256"]) == 64
        assert len(row["extraction_sha256"]) == 64


def test_the_chapter_two_access_run_matches_the_streaming_contract(corpus) -> None:
    """The streaming CLI must accept the chapter 2 directory as --access-run-dir."""
    reextract(corpus["root"], access_run_dir=corpus["access"], jobs=1)
    freeze(
        corpus["root"],
        access_run_dir=corpus["access"],
        legacy_freeze_dir=corpus["legacy"],
        freeze_id="chapter2-test-r1",
        run_id="chapter2-test",
        code_commit="test",
    )
    access_dir = corpus["root"] / "article-access" / "chapter2-test-r1"
    manifest = json.loads((access_dir / "run-manifest.json").read_text())
    assert manifest["target_total"] == len(manifest["selection"])
    assert json.loads((access_dir / "progress.json").read_text())["state"] == (
        "completed"
    )
    assert (access_dir / "run-receipt.json").is_file()

    items = {
        json.loads(path.read_text())["candidate_key"]: json.loads(path.read_text())
        for path in sorted((access_dir / "items").glob("*.json"))
    }
    for position, selected in enumerate(manifest["selection"], start=1):
        item = items[selected["candidate_key"]]
        assert item["schema"] == "article-access-item-v1"
        assert item["run_id"] == manifest["run_id"]
        assert item["position"] == position == selected["position"]
        assert item["subgroup"] == selected["subgroup"]
        assert item["identity_verified"] is True
        assert item["access_state"] == "full_text_ready"
        assert item["extraction_coverage"]["article_body_recognized"] is True
        text = Path(item["extraction_path"])
        assert sha256_bytes(text.read_bytes()) == item["extraction_sha256"]

    # run_stream validates the access run directory before it does anything with
    # a provider. Call it with providers it cannot use: the run must get past
    # every access check and fail later, never on the access contract.
    from arctic_qa.streaming import run_stream

    access_contract_errors = {
        "the article-access run is not complete",
        "the article-access completion receipt is missing",
        "the article-access selection is missing",
        "the ordered selection count is inconsistent",
        "max papers cannot exceed the ordered selection count",
        "the ordered selection does not match its access item",
    }
    with pytest.raises(Exception) as failure:  # noqa: B017 - any later failure
        run_stream(
            None,
            corpus["root"],
            run_id="chapter2-offline",
            campaign_id="chapter2-offline",
            access_run_dir=access_dir,
            eligibility_run_dir=corpus["root"] / "no-eligibility-run",
            author=object(),
            verifier=object(),
            max_papers=len(manifest["selection"]),
        )
    assert str(failure.value) not in access_contract_errors


def test_the_chapter_two_build_writes_nothing_outside_its_own_root(corpus) -> None:
    namespace = corpus["root"].parent
    legacy_before = {
        str(path): path.stat().st_mtime
        for path in corpus["legacy"].rglob("*")
        if path.is_file()
    }
    access_before = {
        str(path): path.stat().st_mtime
        for path in corpus["access"].rglob("*")
        if path.is_file()
    }
    reextract(corpus["root"], access_run_dir=corpus["access"], jobs=1)
    freeze(
        corpus["root"],
        access_run_dir=corpus["access"],
        legacy_freeze_dir=corpus["legacy"],
        freeze_id="chapter2-test-r1",
        run_id="chapter2-test",
        code_commit="test",
    )
    assert {
        str(path): path.stat().st_mtime
        for path in corpus["legacy"].rglob("*")
        if path.is_file()
    } == legacy_before
    assert {
        str(path): path.stat().st_mtime
        for path in corpus["access"].rglob("*")
        if path.is_file()
    } == access_before
    written = {path for path in namespace.rglob("*") if path.is_file()}
    assert all(corpus["root"] in path.parents for path in written)


def test_the_quality_report_compares_the_two_extractions(corpus) -> None:
    for pdf in corpus["pdfs"]:
        legacy = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        text_path = pdf.with_suffix(".txt")
        text_path.write_text(legacy, encoding="utf-8")
    reextract(corpus["root"], access_run_dir=corpus["access"], jobs=1)
    report = quality_report(
        corpus["root"], access_run_dir=corpus["access"], sample_size=3
    )
    assert report["sample_size"] == 3
    assert report["legacy"]["gutter_lines"] > 0
    assert report["chapter2"]["gutter_lines"] == 0
    assert report["documents_with_any_gutter_line"]["chapter2"] == 0
    assert report["word_retention"]["median"] > 0.9
    assert report["word_retention"]["documents_below_0_9"] == 0
    assert report["legacy_chunks"]["gutter_chunks"] > 0
    assert report["chapter2_chunks"]["gutter_chunks"] == 0
    assert (
        report["chapter2_chunks"]["sentence_complete_rate"]
        >= report["legacy_chunks"]["sentence_complete_rate"]
    )


def test_the_quality_measures_count_the_audited_defects() -> None:
    legacy = (
        "left column text here      right column text here\n"
        "a word split at the line wrap-\nover to the next line\n"
        "a ligature " + chr(0xFB01) + "nal and a soft " + chr(0x00AD) + "hyphen\n"
    )
    measured = measure_text(legacy)
    assert measured["gutter_lines"] == 1
    assert measured["mid_word_breaks"] == 1
    assert measured["presentation_marks"] == 2

    clean = measure_chunks(
        [{"text": "A complete sentence."}, {"text": "An incomplete one"}]
    )
    assert clean["sentence_complete_rate"] == 0.5


def test_the_legacy_chunker_is_reproduced_for_the_comparison() -> None:
    """The chapter 1 chunker cut a fixed window, so a chunk could end mid-word."""
    text = "word " * 2000
    chunks = legacy_chunks(text, cap=100, overlap=20)
    assert len(chunks) > 1
    assert not any(chunk["text"].strip().endswith(".") for chunk in chunks)
