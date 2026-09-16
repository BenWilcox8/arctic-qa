"""Several papers of one run, in flight at the same time.

The chapter 3 producer handled one paper and one model call at a time. This
file holds the concurrent producer to the sequential producer's result: four
papers run at once through ``run_stream``, every chain completes, and the
counts, the paper results and the state database agree with the one-at-a-time
run of the same four papers.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from arctic_qa.db import Database
from arctic_qa.paths import DataPaths
from arctic_qa.providers import FakeProvider, ProviderResult
from arctic_qa.streaming import run_stream

from test_streaming import FIXTURES, streaming_fixture, write_json


PAPER_COUNT = 4


class _MarkerScript:
    """One paper's scripted provider, matched by role and prompt markers.

    ``FakeProvider`` answers strictly in script order, so a concurrent option
    wave would hand an event to the wrong call. This script picks the first
    unused event whose role and ``require_prompt_contains`` markers match the
    call, which is the same answer the sequential run receives and is
    independent of the order the calls arrive in.
    """

    name = "fake"

    def __init__(self, model: str, script: Path, meter: _Meter) -> None:
        self.model = model
        self._provider = FakeProvider(model, script)
        self._used: set[int] = set()
        self._lock = threading.Lock()
        self._meter = meter

    def invoke(
        self,
        role: str,
        system: str,
        prompt: str,
        parameters: dict[str, Any],
        timeout: float,
    ) -> ProviderResult:
        # A role's static instructions ride in the system instruction, so a
        # marker may sit in either part, exactly as ``FakeProvider`` reads it.
        request_text = f"{system}\n{prompt}"
        with self._meter.call():
            with self._lock:
                index = next(
                    position
                    for position, event in enumerate(self._provider.events)
                    if position not in self._used
                    and event.get("role") in (None, role)
                    and all(
                        marker in request_text
                        for marker in event.get("require_prompt_contains", [])
                    )
                    and not any(
                        marker in request_text
                        for marker in event.get("forbid_prompt_contains", [])
                    )
                )
                self._used.add(index)
                self._provider.position = index
                return self._provider.invoke(role, system, prompt, parameters, timeout)


class _Meter:
    """Count the calls that overlap, so the test can prove they overlapped."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = 0

    def call(self) -> _Meter:
        return self

    def __enter__(self) -> _Meter:
        with self._lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        # A short hold makes the overlap real rather than incidental.
        time.sleep(0.02)
        return self

    def __exit__(self, *_: object) -> None:
        with self._lock:
            self.active -= 1


class _PerPaperProvider:
    """Hand every paper its own scripted provider.

    ``run_stream`` binds a provider to each paper, so the binding is the
    natural place to give a paper its own script. Each paper then consumes its
    script exactly as the sequential run does.
    """

    name = "fake"
    model = "fake-gemini-3.8-flash"

    def __init__(self, script: Path, meter: _Meter) -> None:
        self._script = script
        self._meter = meter
        self._papers: dict[str, _MarkerScript] = {}
        self._lock = threading.Lock()

    def bind(self, *, paper_id: str, family_id: str, source_version_id: str):
        with self._lock:
            return self._papers.setdefault(
                paper_id, _MarkerScript(self.model, self._script, self._meter)
            )

    def invoke(self, *arguments: Any, **keywords: Any) -> ProviderResult:
        raise AssertionError("every streaming call runs through a bound paper")


def _multi_paper_fixture(tmp_path: Path, count: int) -> tuple[Path, Path]:
    """Grow the one-paper streaming fixture to ``count`` identical papers.

    The papers share one source object and one extraction, because the scripted
    answers are bound to that exact text. They differ in candidate key, so each
    one is its own source row, its own paper family and its own chain.
    """
    access, eligibility = streaming_fixture(tmp_path)
    manifest = json.loads((access / "run-manifest.json").read_text(encoding="utf-8"))
    item = json.loads(
        (access / "items" / "item-000001.json").read_text(encoding="utf-8")
    )
    job_path = next((eligibility / "jobs").glob("*.json"))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    for position in range(2, count + 1):
        candidate_key = f"test-only:streaming-paper-{position}"
        write_json(
            access / "items" / f"item-{position:06d}.json",
            {**item, "position": position, "candidate_key": candidate_key},
        )
        manifest["selection"].append(
            {
                **manifest["selection"][0],
                "position": position,
                "candidate_key": candidate_key,
            }
        )
        write_json(
            eligibility / "jobs" / f"fixture-job-{position}.json",
            {
                **job,
                "job_key": f"fixture-job-{position}",
                "candidate_key": candidate_key,
                "parsed_response": {
                    **job["parsed_response"],
                    "request_id": f"fixture-job-{position}",
                },
            },
        )
    manifest["target_total"] = count
    write_json(access / "run-manifest.json", manifest)
    return access, eligibility


def _run(tmp_path: Path, *, paper_workers: int, option_workers: int) -> dict[str, Any]:
    access, eligibility = _multi_paper_fixture(tmp_path, PAPER_COUNT)
    paths = DataPaths.open(tmp_path, test_mode=True)
    database = Database(paths.database)
    database.migrate(paths.namespace / "backups")
    meter = _Meter()
    author = _PerPaperProvider(FIXTURES / "fake-author.jsonl", meter)
    verifier = _PerPaperProvider(FIXTURES / "fake-verifier.jsonl", meter)
    result = run_stream(
        database,
        paths.namespace,
        run_id="paper-concurrency",
        campaign_id="paper-concurrency-campaign",
        access_run_dir=access,
        eligibility_run_dir=eligibility,
        author=author,
        verifier=verifier,
        max_papers=PAPER_COUNT,
        paper_workers=paper_workers,
        option_workers=option_workers,
    )
    candidates = database.rows(
        "SELECT source_id,status FROM candidates WHERE run_id=?",
        ("paper-concurrency-campaign",),
    )
    database.close()
    return {"result": result, "meter": meter, "candidates": candidates}


def test_four_papers_run_at_once_and_match_the_sequential_run(
    tmp_path: Path,
) -> None:
    concurrent = _run(tmp_path / "concurrent", paper_workers=4, option_workers=4)
    sequential = _run(tmp_path / "sequential", paper_workers=1, option_workers=1)

    # Every paper's chain completed, and the concurrent run reached the same
    # disposition for every paper as the one-at-a-time run.
    counts = concurrent["result"]["counts"]
    assert counts["processed"] == PAPER_COUNT
    assert counts["accepted_base_questions"] == PAPER_COUNT
    assert counts["candidate_processing_fault"] == 0
    assert counts == sequential["result"]["counts"]
    assert (
        concurrent["result"]["paper_results"] == (sequential["result"]["paper_results"])
    )
    # The frozen selection order survives a different completion order.
    assert [row["candidate_key"] for row in concurrent["result"]["paper_results"]] == [
        "test-only:streaming-paper",
        *[f"test-only:streaming-paper-{index}" for index in range(2, PAPER_COUNT + 1)],
    ]
    # The state database holds one accepted candidate per paper, as the
    # sequential run does, and the export counted the same questions.
    assert sorted(
        (row["source_id"], row["status"]) for row in concurrent["candidates"]
    ) == sorted((row["source_id"], row["status"]) for row in sequential["candidates"])
    accepted = [
        row
        for row in concurrent["candidates"]
        if row["status"] == "machine_accepted_unverified"
    ]
    assert len(accepted) == PAPER_COUNT
    assert {
        key: concurrent["result"]["export"][key]
        for key in ("short_answer_count", "mcq_count")
    } == {
        key: sequential["result"]["export"][key]
        for key in ("short_answer_count", "mcq_count")
    }
    assert concurrent["result"]["export"]["mcq_count"] >= PAPER_COUNT
    assert concurrent["result"]["concurrency"] == {
        "paper_workers": 4,
        "option_workers": 4,
    }
    # The calls really overlapped; the sequential run never had two in flight.
    assert concurrent["meter"].peak > 1
    assert sequential["meter"].peak == 1
    assert concurrent["meter"].calls == sequential["meter"].calls
