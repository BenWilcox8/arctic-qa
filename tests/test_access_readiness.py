from __future__ import annotations

from pathlib import Path

import pytest

from arctic_qa.access_readiness import HostPacer, _process, _public_url


def public_resolver(host, port, type):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def private_resolver(host, port, type):
    return [(2, 1, 6, "", ("127.0.0.1", port))]


def test_public_url_accepts_http_and_rejects_private_resolution():
    _public_url("http://example.org/article", public_resolver)
    _public_url("https://example.org/article", public_resolver)
    with pytest.raises(ValueError, match="non-public"):
        _public_url("https://example.org/article", private_resolver)
    with pytest.raises(ValueError, match="credentials"):
        _public_url("https://name:secret@example.org/article", public_resolver)
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        _public_url("file:///tmp/source.pdf", public_resolver)


def manifest(tmp_path: Path) -> dict:
    return {
        "run_id": "fixture-run",
        "reuse_source_run_dir": None,
        "limits": {
            "maximum_bytes_per_source": 1024 * 1024,
            "request_timeout_seconds": 1,
            "maximum_redirects": 2,
            "maximum_retry_after_seconds": 0,
        },
    }


def candidate() -> dict:
    return {
        "position": 1,
        "candidate_key": "10.1/example",
        "subgroup": "retained_article_type",
        "title": "A sufficiently long scientific article title",
        "doi": "10.1/example",
        "open_access": {"url": "https://example.org/source.xml"},
        "landing_url": None,
    }


def test_process_requires_full_text_and_identity(tmp_path: Path):
    def fetcher(url, **kwargs):
        body = b"<article><title>A sufficiently long scientific article title</title><p>10.1/example</p></article>"
        return {
            "state": "downloaded",
            "body": body,
            "media_type": "application/xml",
            "final_url": url,
            "checked_at_utc": "2026-09-12T00:00:00Z",
        }

    result = _process(candidate(), manifest(tmp_path), tmp_path, fetcher, HostPacer(0))
    assert result["access_state"] == "full_text_ready"
    assert result["identity_verified"] is True
    assert result["source_content_hash"]
    assert result["extraction_sha256"]


def test_landing_page_and_wrong_identity_are_not_ready(tmp_path: Path):
    def landing(url, **kwargs):
        return {
            "state": "downloaded",
            "body": b"<html><body>Publisher page</body></html>",
            "media_type": "text/html",
            "final_url": url,
            "checked_at_utc": "2026-09-12T00:00:00Z",
        }

    result = _process(
        candidate(), manifest(tmp_path), tmp_path / "landing", landing, HostPacer(0)
    )
    assert result["access_state"] == "working_landing_page_only"

    def wrong(url, **kwargs):
        return {
            "state": "downloaded",
            "body": b"<article><title>A different publication</title><p>Unrelated text</p></article>",
            "media_type": "application/xml",
            "final_url": url,
            "checked_at_utc": "2026-09-12T00:00:00Z",
        }

    result = _process(
        candidate(), manifest(tmp_path), tmp_path / "wrong", wrong, HostPacer(0)
    )
    assert result["access_state"] == "identity_pending"


def test_missing_url_stays_distinct_from_exclusion(tmp_path: Path):
    item = candidate()
    item["doi"] = None
    item["open_access"] = {}
    result = _process(
        item, manifest(tmp_path), tmp_path, lambda *a, **k: {}, HostPacer(0)
    )
    assert result["access_state"] == "no_source_found"
    assert "eligibility" not in result
