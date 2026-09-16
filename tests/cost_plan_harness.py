"""Seed the public fixture source and drive generate_candidate in-process.

The CLI tests reach ``generate_candidate`` through the ``smoke`` command. The
cost call-plan tests need the same seeded source with scripted providers of
their own, so this module repeats the seeding steps of ``cli._smoke`` without
the subprocess.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from arctic_qa.db import Database
from arctic_qa.discovery import manual_record
from arctic_qa.extraction import extract_source, load_chunks
from arctic_qa.generation import generate_candidate
from arctic_qa.paths import DataPaths
from arctic_qa.providers import FakeProvider
from arctic_qa.screening import screen_source
from arctic_qa.storage import store_original

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"
SITE_QUOTE = "The complete study site was at 71.3 N."


def fixture_events(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_script(path: Path, events: list[dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


class Seeded:
    """One seeded fixture source inside a temporary data root."""

    def __init__(self, root: Path, *, char_cap: int = 2000, overlap_chars: int = 100):
        self.paths = DataPaths.open(root, test_mode=True)
        self.db = Database(self.paths.database)
        self.db.migrate(self.paths.namespace / "backups")
        fixture = manual_record(
            json.loads(
                (FIXTURES / "public-source-metadata.json").read_text(encoding="utf-8")
            ),
            "public_fixture",
        )
        self.db.upsert_source(fixture)
        self.source_id = fixture["source_id"]
        stored = store_original(
            self.db,
            self.paths.namespace,
            self.source_id,
            (FIXTURES / "public-source.html").read_bytes(),
            "text/html",
            fixture["retrieval_url"],
        )
        extract_source(
            self.db,
            self.paths.namespace,
            self.source_id,
            char_cap=char_cap,
            overlap_chars=overlap_chars,
        )
        chunk = next(
            row for row in self.chunks() if SITE_QUOTE in row["text"]
        )
        start = chunk["text"].index(SITE_QUOTE)
        screen_source(
            self.db,
            self.source_id,
            {
                "evidence_kind": "site_coordinates",
                "latitudes": [71.3],
                "named_regions": [],
                "source_content_hash": stored["sha256"],
                "evidence_quote": SITE_QUOTE,
                "locator": {
                    "chunk_id": chunk["chunk_id"],
                    "start_offset": start,
                    "end_offset": start + len(SITE_QUOTE),
                },
                "site_coverage": "complete",
                "test_only": True,
            },
            self.paths.namespace,
        )
        self.source = self.db.one(
            "SELECT * FROM sources WHERE source_id=?", (self.source_id,)
        )

    def chunks(self) -> list[dict[str, Any]]:
        return load_chunks(self.db, self.paths.namespace, self.source_id)

    def generate(
        self,
        run_id: str,
        author_events: list[dict[str, Any]],
        verifier_events: list[dict[str, Any]],
        *,
        generation_attempt: dict[str, Any] | None = None,
        author_model: str = "gemini-3.8-flash",
        verifier_model: str = "gemini-3.1-pro-preview",
    ) -> dict[str, Any]:
        scripts = self.paths.namespace / "scripts"
        scripts.mkdir(exist_ok=True)
        author = FakeProvider(
            author_model, write_script(scripts / f"{run_id}-author.jsonl", author_events)
        )
        verifier = FakeProvider(
            verifier_model,
            write_script(scripts / f"{run_id}-verifier.jsonl", verifier_events),
        )
        return generate_candidate(
            self.db,
            self.paths.namespace,
            source_id=self.source_id,
            run_id=run_id,
            arm="answer_first",
            author=author,
            verifier=verifier,
            budget_mode="tokens",
            budget_limit=Decimal("100000"),
            reservation=Decimal("100"),
            timeout=1,
            retries=0,
            rate_limit_seconds=0,
            generation_attempt=generation_attempt,
        )

    def call_roles(self, run_id: str) -> list[str]:
        return [
            row["role"]
            for row in self.db.rows(
                "SELECT role FROM calls WHERE run_id=? ORDER BY rowid", (run_id,)
            )
        ]
