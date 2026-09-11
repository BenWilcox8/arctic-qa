from __future__ import annotations

import json
import csv
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .db import Database
from .util import atomic_write, canonical_json, normalize_doi, sha256_bytes, stable_id


USER_AGENT = "arctic-qa/0.1 (mailto:local-research@example.invalid)"


@dataclass(frozen=True)
class DiscoveryPage:
    adapter: str
    query: str
    page_number: int
    page_token: str | None
    next_token: str | None
    records: list[dict[str, Any]]
    raw: dict[str, Any]


def _get_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def crossref_exact_doi(doi: str, *, timeout: float = 20) -> DiscoveryPage:
    normalized = normalize_doi(doi)
    url = "https://api.crossref.org/works/" + urllib.parse.quote(normalized, safe="")
    raw = _get_json(url, timeout)
    return DiscoveryPage(
        "crossref", normalized, 1, None, None, [_crossref_record(raw["message"])], raw
    )


def crossref_reference_expansion(
    doi: str, *, limit: int, timeout: float = 20
) -> Iterator[DiscoveryPage]:
    parent = crossref_exact_doi(doi, timeout=timeout)
    yield parent
    references = parent.raw.get("message", {}).get("reference", [])
    emitted = 0
    for reference in references:
        child_doi = reference.get("DOI")
        if not child_doi:
            continue
        try:
            child = crossref_exact_doi(child_doi, timeout=timeout)
        except Exception:
            continue
        for record in child.records:
            record["provenance"]["reference_depth"] = 1
            record["provenance"]["parent_doi"] = normalize_doi(doi)
        emitted += 1
        yield DiscoveryPage(
            child.adapter,
            normalize_doi(doi),
            emitted + 1,
            str(emitted),
            None,
            child.records,
            child.raw,
        )
        if emitted >= limit:
            break


def openalex_exact_doi(doi: str, *, timeout: float = 20) -> DiscoveryPage:
    normalized = normalize_doi(doi)
    url = "https://api.openalex.org/works/" + urllib.parse.quote(
        f"https://doi.org/{normalized}", safe=""
    )
    raw = _get_json(url, timeout)
    return DiscoveryPage(
        "openalex", normalized, 1, None, None, [_openalex_record(raw)], raw
    )


def crossref_query(
    query: str, *, pages: int, per_page: int, timeout: float = 20
) -> Iterator[DiscoveryPage]:
    cursor = "*"
    for number in range(1, pages + 1):
        parameters = urllib.parse.urlencode(
            {
                "query.bibliographic": query,
                "rows": per_page,
                "cursor": cursor,
                "select": "DOI,title,author,published,URL,type,license,subject",
            }
        )
        raw = _get_json(f"https://api.crossref.org/works?{parameters}", timeout)
        message = raw["message"]
        next_cursor = message.get("next-cursor")
        yield DiscoveryPage(
            "crossref",
            query,
            number,
            cursor,
            next_cursor,
            [_crossref_record(item) for item in message.get("items", [])],
            raw,
        )
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor


def replay_pages(path: Path) -> Iterator[DiscoveryPage]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    pages = payload if isinstance(payload, list) else [payload]
    for index, page in enumerate(pages, start=1):
        if "records" in page:
            records = [
                record
                if record.get("source_id")
                else manual_record(record, page.get("adapter", "replay"))
                for record in page["records"]
            ]
        elif page.get("adapter") == "openalex":
            records = [_openalex_record(item) for item in page["raw"]["results"]]
        else:
            message = page.get("raw", page).get("message", {})
            records = [_crossref_record(item) for item in message.get("items", [])]
        yield DiscoveryPage(
            page.get("adapter", "replay"),
            page.get("query", "offline-replay"),
            page.get("page_number", index),
            page.get("page_token"),
            page.get("next_token"),
            records,
            page.get("raw", page),
        )


def ingest_pages(
    db: Database, pages: Iterator[DiscoveryPage], replay_dir: Path
) -> dict[str, int]:
    added = 0
    duplicates = 0
    events = 0
    replay_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        raw_bytes = (canonical_json(page.raw) + "\n").encode()
        response_hash = sha256_bytes(raw_bytes)
        replay_path = replay_dir / f"{response_hash}.json"
        atomic_write(replay_path, raw_bytes, immutable=True)
        for record in page.records:
            if db.upsert_source(record):
                added += 1
            else:
                duplicates += 1
        event_id = stable_id(
            "discovery", page.adapter, page.query, page.page_number, response_hash
        )
        with db.transaction():
            db.connection.execute(
                """INSERT OR IGNORE INTO discovery_events
                (event_id,adapter,query,requested_at,page_token,page_number,response_hash,replay_path,result_count)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    page.adapter,
                    page.query,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    page.page_token,
                    page.page_number,
                    response_hash,
                    str(replay_path.relative_to(replay_dir.parent)),
                    len(page.records),
                ),
            )
        events += 1
    return {"added": added, "duplicates": duplicates, "pages": events}


def catalog_records(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if str(row.get("private", "")).casefold() in {"1", "true", "yes"}:
                continue
            payload = {
                "stable_id": row.get("inventory_id") or row.get("item_key"),
                "title": row.get("title"),
                "retrieval_url": row.get("source_url"),
                "source_version": row.get("schema_version"),
                "license": row.get("availability"),
                "zotero": {
                    "item_key": row.get("item_key"),
                    "attachment_key": row.get("attachment_key"),
                },
                "custody": {
                    "archive_relpath": row.get("archive_relpath"),
                    "sha256": row.get("sha256"),
                    "size_bytes": row.get("size_bytes"),
                    "media_type": row.get("media_type"),
                    "original_filename": row.get("original_filename"),
                },
                "query": "library-originals/catalog.tsv",
            }
            if payload["stable_id"] and payload["title"]:
                record = manual_record(payload, "zotero_bridge")
                record["zotero"] = payload["zotero"]
                record["metadata"]["custody"] = payload["custody"]
                records.append(record)
    return records


def manual_record(
    payload: dict[str, Any], source_name: str = "manual"
) -> dict[str, Any]:
    doi = normalize_doi(payload["doi"]) if payload.get("doi") else None
    stable = doi or payload.get("stable_id")
    if not stable:
        raise ValueError("manual metadata requires doi or stable_id")
    title = str(payload.get("title", "")).strip()
    if not title:
        raise ValueError("manual metadata requires title")
    return {
        "source_id": stable_id("src", stable),
        "stable_id": stable,
        "doi": doi,
        "title": title,
        "authors": payload.get("authors", []),
        "published_date": payload.get("published_date"),
        "year": payload.get("year"),
        "source_version": payload.get("source_version"),
        "retrieval_url": payload.get("retrieval_url"),
        "license": payload.get("license"),
        "paper_family_id": payload.get("paper_family_id")
        or stable_id("family", stable),
        "provenance": {
            "adapter": source_name,
            "query": payload.get("query", stable),
            "query_date": payload.get("query_date")
            or datetime.now(UTC).date().isoformat(),
            "reference_depth": payload.get("reference_depth", 0),
        },
        "metadata": payload,
        "discipline": payload.get("discipline"),
    }


def _date_parts(message: dict[str, Any]) -> tuple[str | None, int | None]:
    date_parts = message.get("published", {}).get("date-parts", [[]])
    parts = date_parts[0] if date_parts else []
    if not parts:
        return None, None
    value = "-".join(str(part).zfill(2) for part in parts)
    return value, int(parts[0])


def _crossref_record(message: dict[str, Any]) -> dict[str, Any]:
    doi = normalize_doi(message["DOI"]) if message.get("DOI") else None
    stable = doi or message.get("URL") or stable_id("unknown", message.get("title", []))
    published, year = _date_parts(message)
    licenses = message.get("license") or []
    return manual_record(
        {
            "doi": doi,
            "stable_id": stable,
            "title": (message.get("title") or ["Untitled"])[0],
            "authors": [
                {
                    "given": author.get("given"),
                    "family": author.get("family"),
                    "orcid": author.get("ORCID"),
                }
                for author in message.get("author", [])
            ],
            "published_date": published,
            "year": year,
            "retrieval_url": message.get("URL"),
            "license": licenses[0].get("URL") if licenses else None,
            "type": message.get("type"),
            "subjects": message.get("subject", []),
            "discipline": (message.get("subject") or [None])[0],
        },
        "crossref",
    )


def _openalex_record(message: dict[str, Any]) -> dict[str, Any]:
    doi = normalize_doi(message["doi"]) if message.get("doi") else None
    stable = doi or message.get("id")
    primary = message.get("primary_location") or {}
    return manual_record(
        {
            "doi": doi,
            "stable_id": stable,
            "title": message.get("title") or "Untitled",
            "authors": [
                {
                    "name": authorship.get("author", {}).get("display_name"),
                    "orcid": authorship.get("author", {}).get("orcid"),
                }
                for authorship in message.get("authorships", [])
            ],
            "published_date": message.get("publication_date"),
            "year": message.get("publication_year"),
            "retrieval_url": primary.get("landing_page_url")
            or message.get("doi")
            or message.get("id"),
            "license": primary.get("license"),
            "open_access": message.get("open_access"),
            "openalex_id": message.get("id"),
        },
        "openalex",
    )
