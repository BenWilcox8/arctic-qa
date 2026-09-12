from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import pytest

from arctic_qa.access_readiness import (
    HostPacer,
    _PinnedHTTPConnection,
    _persist_artifacts,
    _process,
    _public_url,
    _request_url,
    _receipts,
    _reuse_access,
    prepare_access_run,
    supervise_access_readiness,
)
from arctic_qa.source_pass import _identity_resolves


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


def test_request_url_encodes_spaces_and_rejects_control_characters():
    assert (
        _request_url("https://example.org/a file.pdf?filename=Arctic Study.pdf")
        == "https://example.org/a%20file.pdf?filename=Arctic%20Study.pdf"
    )
    with pytest.raises(ValueError, match="control character"):
        _request_url("https://example.org/source.pdf\nX-Injected: value")


def test_prepare_access_run_accepts_relocated_identical_input(
    tmp_path: Path,
) -> None:
    queue = tmp_path / "queue.ndjson"
    candidates = tmp_path / "candidates.json"
    protocol = tmp_path / "protocol.json"
    original_policy = tmp_path / "original" / "policy.json"
    relocated_policy = tmp_path / "runtime-copy" / "policy.json"
    output = tmp_path / "run"
    for path, value in (
        (queue, "queue"),
        (candidates, "candidates"),
        (protocol, "protocol"),
        (original_policy, "policy"),
        (relocated_policy, "policy"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    policy = {
        "policy_id": "fixture-policy",
        "source_protocol_id": "fixture-protocol",
        "smoke_sizes": [1],
        "target_dispositions": {
            "priority_seed": 0,
            "retained_article_type": 1,
        },
    }
    selection = [{"candidate_key": "candidate-1"}]
    with (
        patch("arctic_qa.access_readiness._read_json") as read_json,
        patch("arctic_qa.access_readiness._validate_policy"),
        patch("arctic_qa.access_readiness._load_target", return_value=selection),
    ):
        read_json.side_effect = lambda path: (
            policy
            if Path(path).name == "policy.json"
            else {"protocol_id": "fixture-protocol"}
        )
        prepare_access_run(
            queue_file=queue,
            candidates_file=candidates,
            protocol_file=protocol,
            policy_file=original_policy,
            output_dir=output,
            run_id="fixture-run",
            code_commit="fixture-commit",
        )
    immutable_manifest = json.loads((output / "run-manifest.json").read_text())
    with (
        patch("arctic_qa.access_readiness._read_json") as read_json,
        patch("arctic_qa.access_readiness._validate_policy"),
        patch("arctic_qa.access_readiness._load_target", return_value=selection),
    ):
        read_json.side_effect = lambda path: (
            immutable_manifest
            if Path(path).name == "run-manifest.json"
            else policy
            if Path(path).name == "policy.json"
            else {"protocol_id": "fixture-protocol"}
        )
        resumed = prepare_access_run(
            queue_file=queue,
            candidates_file=candidates,
            protocol_file=protocol,
            policy_file=relocated_policy,
            output_dir=output,
            run_id="fixture-run",
            code_commit="fixture-commit",
        )
    assert resumed["inputs"]["policy"]["path"] == str(relocated_policy)
    assert json.loads((output / "run-manifest.json").read_text()) == immutable_manifest
    relocated_policy.write_text("changed policy", encoding="utf-8")
    with (
        patch("arctic_qa.access_readiness._read_json") as read_json,
        patch("arctic_qa.access_readiness._validate_policy"),
        patch("arctic_qa.access_readiness._load_target", return_value=selection),
    ):
        read_json.side_effect = lambda path: (
            immutable_manifest
            if Path(path).name == "run-manifest.json"
            else policy
            if Path(path).name == "policy.json"
            else {"protocol_id": "fixture-protocol"}
        )
        with pytest.raises(ValueError, match="manifest changed"):
            prepare_access_run(
                queue_file=queue,
                candidates_file=candidates,
                protocol_file=protocol,
                policy_file=relocated_policy,
                output_dir=output,
                run_id="fixture-run",
                code_commit="fixture-commit",
            )


def test_access_supervisor_continues_bounded_invocations(tmp_path: Path) -> None:
    progress = tmp_path / "run" / "progress.json"

    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    write_json(progress, {"counts": {"checked": 0}})
    results = iter(
        [
            {
                "state": "paused",
                "message": "The invocation stopped at its bounded checkpoint.",
                "counts": {
                    "target": 2,
                    "checked": 1,
                    "full_text_ready": 1,
                    "unchecked": 1,
                },
            },
            {
                "state": "completed",
                "message": "Access readiness checked every record.",
                "counts": {
                    "target": 2,
                    "checked": 2,
                    "full_text_ready": 1,
                    "unchecked": 0,
                },
            },
        ]
    )

    def fake_run(**kwargs):
        result = next(results)
        write_json(progress, {"counts": result["counts"]})
        return result

    with patch("arctic_qa.access_readiness.run_access_readiness", fake_run):
        status = supervise_access_readiness(
            queue_file=tmp_path / "queue",
            candidates_file=tmp_path / "candidates",
            protocol_file=tmp_path / "protocol",
            policy_file=tmp_path / "policy",
            output_dir=tmp_path / "run",
            run_id="run-r1",
            manifest_code_commit="manifest-commit",
            runner_code_commit="runner-commit",
            status_file=tmp_path / "supervisor.json",
            service_unit="fixture.service",
            max_network_seconds=1,
        )
    assert status["state"] == "completed"
    assert status["invocation_count"] == 2
    assert status["counts"]["checked"] == 2
    assert len(list((tmp_path / "access-supervisor-events").glob("*.json"))) == 2


def test_access_supervisor_records_failure_without_restarting(tmp_path: Path) -> None:
    with patch(
        "arctic_qa.access_readiness.run_access_readiness",
        side_effect=RuntimeError("synthetic network stop"),
    ):
        with pytest.raises(RuntimeError, match="synthetic network stop"):
            supervise_access_readiness(
                queue_file=tmp_path / "queue",
                candidates_file=tmp_path / "candidates",
                protocol_file=tmp_path / "protocol",
                policy_file=tmp_path / "policy",
                output_dir=tmp_path / "run",
                run_id="run-r1",
                manifest_code_commit="manifest-commit",
                runner_code_commit="runner-commit",
                status_file=tmp_path / "supervisor.json",
                service_unit="fixture.service",
                max_network_seconds=1,
            )
    status = json.loads((tmp_path / "supervisor.json").read_text())
    assert status["state"] == "error"
    assert status["terminal"] is True
    assert status["error_type"] == "RuntimeError"


def test_connection_uses_only_the_validated_public_address():
    connected = []

    class Socket:
        pass

    with (
        patch(
            "arctic_qa.access_readiness._public_addresses",
            return_value=["93.184.216.34"],
        ),
        patch(
            "arctic_qa.access_readiness.socket.create_connection",
            side_effect=lambda target, *args: connected.append(target) or Socket(),
        ),
    ):
        connection = _PinnedHTTPConnection("example.org", timeout=1)
        connection.connect()
    assert connected == [("93.184.216.34", 80)]


def manifest(tmp_path: Path) -> dict:
    return {
        "run_id": "fixture-run",
        "reuse_source_run_dir": None,
        "limits": {
            "maximum_bytes_per_source": 1024 * 1024,
            "request_timeout_seconds": 1,
            "maximum_redirects": 2,
            "maximum_retry_after_seconds": 0,
            "maximum_network_seconds_per_candidate": 10,
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
        body = (
            b"<article><title>A sufficiently long scientific article title</title>"
            b"<body><p>10.1/example "
            + b"substantive evidence " * 160
            + b"</p></body></article>"
        )
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
            "body": b"<article><title>A different publication</title><body><p>"
            + b"Unrelated text " * 200
            + b"</p></body></article>",
            "media_type": "application/xml",
            "final_url": url,
            "checked_at_utc": "2026-09-12T00:00:00Z",
        }

    result = _process(
        candidate(), manifest(tmp_path), tmp_path / "wrong", wrong, HostPacer(0)
    )
    assert result["access_state"] == "identity_pending"


def test_metadata_xml_and_landing_cycle_do_not_become_ready(tmp_path: Path):
    calls = []

    def metadata(url, **kwargs):
        calls.append(url)
        body = b"<record><title>A sufficiently long scientific article title</title><doi>10.1/example</doi></record>"
        return {
            "state": "downloaded",
            "body": body,
            "media_type": "application/xml",
            "final_url": url,
            "checked_at_utc": "2026-09-12T00:00:00Z",
        }

    result = _process(
        candidate(), manifest(tmp_path), tmp_path / "metadata", metadata, HostPacer(0)
    )
    assert result["access_state"] == "extraction_pending"

    item = candidate()
    item["doi"] = None

    def cycle(url, **kwargs):
        calls.append(url)
        return {
            "state": "downloaded",
            "body": b'<html><a href="/loop.pdf">loop</a></html>',
            "media_type": "text/html",
            "final_url": "https://example.org/loop.pdf",
            "checked_at_utc": "2026-09-12T00:00:00Z",
        }

    before = len(calls)
    result = _process(item, manifest(tmp_path), tmp_path / "cycle", cycle, HostPacer(0))
    assert result["access_state"] == "working_landing_page_only"
    assert len(calls) - before == 2


def test_missing_url_stays_distinct_from_exclusion(tmp_path: Path):
    item = candidate()
    item["doi"] = None
    item["open_access"] = {}
    result = _process(
        item, manifest(tmp_path), tmp_path, lambda *a, **k: {}, HostPacer(0)
    )
    assert result["access_state"] == "no_source_found"
    assert "eligibility" not in result


def test_prior_access_receipt_is_reused_without_network(tmp_path: Path):
    prior = tmp_path / "prior"
    receipt = {
        "candidate_key": "10.1/example",
        "access_state": "working_landing_page_only",
        "reason_code": "working_page_has_no_retrieved_full_text",
        "new_bytes": 0,
    }
    path = prior / "items" / "item-000001.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    reused = _reuse_access(candidate(), prior)
    assert reused is not None
    assert reused["access_state"] == "working_landing_page_only"
    assert reused["reused_from"] == str(path)


def test_ready_reuse_requires_current_identity_and_coverage(tmp_path: Path):
    prior = tmp_path / "prior"
    source = prior / "objects" / "source.xml"
    extraction = prior / "objects" / "text.txt"
    source.parent.mkdir(parents=True)
    source_body = b"<article><body>stored body</body></article>"
    extracted = "Unrelated article text " * 150
    source.write_bytes(source_body)
    extraction.write_text(extracted, encoding="utf-8")
    receipt = {
        "candidate_key": "10.1/example",
        "access_state": "full_text_ready",
        "identity_verified": False,
        "source_path": str(source),
        "source_content_hash": sha256(source_body).hexdigest(),
        "extraction_path": str(extraction),
        "extraction_sha256": sha256(extracted.encode()).hexdigest(),
        "extraction_coverage": {"article_body_recognized": True},
        "new_bytes": len(source_body),
    }
    path = prior / "items" / "item-000001.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt), encoding="utf-8")

    assert _reuse_access(candidate(), prior) is None

    receipt["identity_verified"] = True
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert _reuse_access(candidate(), prior) is None


def test_identity_uses_exact_doi_and_ignores_reference_only_mentions():
    item = {
        "doi": "https://doi.org/10.1234/example",
        "title": "The target Arctic scientific article",
    }
    assert _identity_resolves(item, "doi:10.1234/example. Article body")
    assert not _identity_resolves(
        item,
        "A different publication 10.1234/example-other " + "evidence " * 2000,
    )
    assert not _identity_resolves(
        item,
        "A different publication\nReferences\n10.1234/example",
    )


def test_xml_reference_list_doi_does_not_verify_article_identity(tmp_path: Path):
    target = candidate()
    target["doi"] = "10.1234/example"
    target["title"] = "The target Arctic scientific article"

    def fetcher(url, **kwargs):
        body = (
            b"<article><front><article-title>A different study</article-title></front>"
            b"<body><p>Substantive unrelated evidence. "
            + b"More evidence. "
            * 180
            + b"</p></body><back><ref-list><ref>10.1234/example</ref>"
            b"</ref-list></back></article>"
        )
        return {
            "state": "downloaded",
            "body": body,
            "media_type": "application/xml",
            "final_url": url,
            "checked_at_utc": "2026-09-12T00:00:00Z",
        }

    result = _process(target, manifest(tmp_path), tmp_path, fetcher, HostPacer(0))
    assert result["access_state"] == "identity_pending"
    assert result["identity_verified"] is False


def test_new_byte_cap_precedes_durable_source_writes(tmp_path: Path):
    def fetcher(url, **kwargs):
        body = (
            b"<article><title>A sufficiently long scientific article title</title>"
            b"<body><p>10.1234/example "
            + b"substantive evidence " * 160
            + b"</p></body></article>"
        )
        return {
            "state": "downloaded",
            "body": body,
            "media_type": "application/xml",
            "final_url": url,
            "checked_at_utc": "2026-09-12T00:00:00Z",
        }

    item = candidate()
    item["doi"] = "10.1234/example"
    result = _process(item, manifest(tmp_path), tmp_path, fetcher, HostPacer(0))
    source = Path(result["source_path"])
    extraction = Path(result["extraction_path"])
    assert not source.exists()
    assert not extraction.exists()

    assert result["new_bytes"] == len(result["_source_body"]) + len(
        result["_extraction_body"]
    )

    with pytest.raises(ValueError, match="before source write"):
        _persist_artifacts(
            result, committed_bytes=0, byte_cap=int(result["new_bytes"]) - 1
        )
    assert not source.exists()
    assert not extraction.exists()

    stored = _persist_artifacts(
        result, committed_bytes=0, byte_cap=int(result["new_bytes"])
    )
    assert source.is_file()
    assert extraction.is_file()
    assert "_source_body" not in stored
    assert "_extraction_body" not in stored


def test_resume_rejects_a_receipt_with_the_wrong_identity(tmp_path: Path):
    output = tmp_path / "run"
    path = output / "items" / "item-000001.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": "wrong",
                "run_id": "wrong",
                "position": 1,
                "candidate_key": "wrong",
                "subgroup": "retained_article_type",
                "access_state": "access_pending",
            }
        ),
        encoding="utf-8",
    )
    selected = candidate()
    run_manifest = {"run_id": "fixture-run", "target_total": 1, "selection": [selected]}
    with pytest.raises(ValueError, match="does not match"):
        _receipts(output, run_manifest)
