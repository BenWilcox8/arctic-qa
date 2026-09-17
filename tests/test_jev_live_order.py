"""The live Jev order: a ranking written during a run changes the next pick.

The captain's rule is that a paper the prescreen has scored reaches the
pipeline at once, instead of waiting for the whole corpus to be scanned. These
tests drive `streaming.PaperPicker`, which is the producer's own pick-up, and
the prescreen's `LiveRanking`, which is the file it reads.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path

from arctic_qa import jev_prescreen as jev
from arctic_qa.streaming import JEV_LIVE_RANKING_SCHEMA, PaperPicker


def _ready(count: int) -> list[dict]:
    return [{"candidate_key": f"10.1/p{index}"} for index in range(count)]


def _write_ranking(path: Path, scores: dict[str, str]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": JEV_LIVE_RANKING_SCHEMA,
                "count": len(scores),
                "records": [
                    {
                        "candidate_key": key,
                        "rank_probability": value,
                        "scan_position": position,
                    }
                    for position, (key, value) in enumerate(scores.items(), start=1)
                ],
            }
        ),
        encoding="utf-8",
    )


def _keys(picker: PaperPicker, count: int) -> list[str]:
    taken = []
    for _ in range(count):
        item = picker.take()
        if item is None:
            break
        taken.append(item["candidate_key"])
    return taken


# --------------------------------------------------------------------------
# The picker


def test_with_no_ranking_the_frozen_order_is_the_pick_up_order():
    picker = PaperPicker(_ready(4))
    assert _keys(picker, 5) == ["10.1/p0", "10.1/p1", "10.1/p2", "10.1/p3"]
    # Every paper is handed out once and the picker then reports exhaustion.
    assert picker.take() is None


def test_a_scored_paper_is_taken_before_an_unscored_one(tmp_path):
    ranking = tmp_path / "live-ranking.json"
    _write_ranking(ranking, {"10.1/p3": "0.40"})
    picker = PaperPicker(_ready(4), ranking_file=ranking)
    # p3 is last in the frozen order and the only scored paper, so it is first.
    # The rest follow in frozen order.
    assert _keys(picker, 4) == ["10.1/p3", "10.1/p0", "10.1/p1", "10.1/p2"]


def test_scored_papers_are_taken_by_descending_score(tmp_path):
    ranking = tmp_path / "live-ranking.json"
    _write_ranking(ranking, {"10.1/p0": "0.10", "10.1/p1": "0.90", "10.1/p2": "0.50"})
    picker = PaperPicker(_ready(4), ranking_file=ranking)
    assert _keys(picker, 4) == ["10.1/p1", "10.1/p2", "10.1/p0", "10.1/p3"]


def test_a_ranking_written_mid_run_changes_the_next_pick(tmp_path):
    """This is the behaviour the captain asked for."""
    ranking = tmp_path / "live-ranking.json"
    _write_ranking(ranking, {"10.1/p1": "0.90"})
    picker = PaperPicker(_ready(5), ranking_file=ranking)
    assert picker.take()["candidate_key"] == "10.1/p1"
    # The scan continues and scores two more papers, one of them better than
    # anything the picker has seen. The producer has not restarted.
    _write_ranking(ranking, {"10.1/p1": "0.90", "10.1/p4": "0.95", "10.1/p2": "0.30"})
    # The next pick is the newly scored p4, not the frozen-order p0.
    assert picker.take()["candidate_key"] == "10.1/p4"
    assert picker.take()["candidate_key"] == "10.1/p2"
    assert _keys(picker, 2) == ["10.1/p0", "10.1/p3"]
    assert picker.reloads == 2


def test_a_paper_already_picked_is_never_picked_again(tmp_path):
    ranking = tmp_path / "live-ranking.json"
    _write_ranking(ranking, {"10.1/p2": "0.90"})
    picker = PaperPicker(_ready(3), ranking_file=ranking)
    assert picker.take()["candidate_key"] == "10.1/p2"
    # The scan raises that same paper's score. It is already in flight, so it
    # must not come back.
    _write_ranking(ranking, {"10.1/p2": "0.99", "10.1/p0": "0.50"})
    assert _keys(picker, 3) == ["10.1/p0", "10.1/p1"]


def test_the_ranking_is_read_again_only_when_it_changes(tmp_path):
    ranking = tmp_path / "live-ranking.json"
    _write_ranking(ranking, {"10.1/p1": "0.90"})
    picker = PaperPicker(_ready(4), ranking_file=ranking)
    picker.take()
    picker.take()
    picker.take()
    # One read for an unchanged file, however many papers are taken.
    assert picker.reloads == 1


def test_a_half_written_ranking_never_stops_the_pick_up(tmp_path):
    ranking = tmp_path / "live-ranking.json"
    _write_ranking(ranking, {"10.1/p2": "0.90"})
    picker = PaperPicker(_ready(3), ranking_file=ranking)
    assert picker.take()["candidate_key"] == "10.1/p2"
    # A reader that catches the file mid-replacement keeps the scores it has.
    ranking.write_text(
        '{"schema": "jev-live-ranking-v1", "records": [{"cand', encoding="utf-8"
    )
    assert picker.take()["candidate_key"] == "10.1/p0"
    # A file of another contract is ignored too.
    ranking.write_text(json.dumps({"schema": "something-else"}), encoding="utf-8")
    assert picker.take()["candidate_key"] == "10.1/p1"


def test_a_missing_ranking_file_is_simply_the_frozen_order(tmp_path):
    picker = PaperPicker(_ready(3), ranking_file=tmp_path / "absent.json")
    assert _keys(picker, 3) == ["10.1/p0", "10.1/p1", "10.1/p2"]
    assert picker.reloads == 0


def test_a_ranking_row_for_a_paper_outside_this_run_is_ignored(tmp_path):
    ranking = tmp_path / "live-ranking.json"
    _write_ranking(ranking, {"10.1/absent": "0.99", "10.1/p1": "0.50"})
    picker = PaperPicker(_ready(3), ranking_file=ranking)
    assert _keys(picker, 3) == ["10.1/p1", "10.1/p0", "10.1/p2"]


def test_concurrent_workers_never_take_the_same_paper(tmp_path):
    ranking = tmp_path / "live-ranking.json"
    _write_ranking(ranking, {f"10.1/p{i}": f"0.{i:02d}" for i in range(40)})
    picker = PaperPicker(_ready(40), ranking_file=ranking)
    taken: list[str] = []
    guard = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        while True:
            item = picker.take()
            if item is None:
                return
            with guard:
                taken.append(item["candidate_key"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(taken) == 40
    assert len(set(taken)) == 40


# --------------------------------------------------------------------------
# The prescreen's half: the file the producer reads


def test_the_live_ranking_writes_in_batches_and_flushes_the_remainder(tmp_path):
    live = jev.LiveRanking(tmp_path / "live-ranking.json", every=3)
    live.add("10.1/a", "0.10", 1)
    live.add("10.1/b", "0.90", 2)
    assert not live.path.exists()
    live.add("10.1/c", "0.50", 3)
    assert live.writes == 1
    document = json.loads(live.path.read_text())
    assert document["schema"] == JEV_LIVE_RANKING_SCHEMA
    assert document["count"] == 3
    # Best first, so the producer reads the order it should pick up.
    assert [row["candidate_key"] for row in document["records"]] == [
        "10.1/b",
        "10.1/c",
        "10.1/a",
    ]
    assert document["records"][0]["scan_position"] == 2
    live.add("10.1/d", "0.99", 4)
    live.flush()
    assert live.writes == 2
    assert json.loads(live.path.read_text())["count"] == 4


def test_the_live_ranking_is_what_the_picker_reads(tmp_path):
    live = jev.LiveRanking(tmp_path / "live-ranking.json", every=1)
    live.add("10.1/p2", "0.80", 1)
    picker = PaperPicker(_ready(3), ranking_file=live.path)
    assert picker.take()["candidate_key"] == "10.1/p2"
    # The scan scores a better paper while the producer runs.
    live.add("10.1/p1", "0.95", 2)
    assert picker.take()["candidate_key"] == "10.1/p1"


def test_a_rescored_paper_replaces_its_own_row(tmp_path):
    live = jev.LiveRanking(tmp_path / "live-ranking.json", every=1)
    live.add("10.1/a", "0.10", 1)
    live.add("10.1/a", "0.90", 2)
    document = json.loads(live.path.read_text())
    assert document["count"] == 1
    assert Decimal(document["records"][0]["rank_probability"]) == Decimal("0.90")


def test_a_resumed_scan_seeds_the_live_ranking_from_what_it_already_bought(tmp_path):
    """A paper an earlier pass scored must not look unscored to the producer."""
    from test_jev_prescreen import (
        FakeClient,
        _disposition,
        _extraction,
        _frozen_row,
        _write_jsonl,
    )

    frozen, dispositions = [], []
    for index in range(3):
        path = _extraction(tmp_path, f"p{index}", f"Article {index}. " * 200)
        frozen.append(_frozen_row(f"10.1/p{index}", index + 1, path))
        dispositions.append(_disposition(f"10.1/p{index}", "retained_article_type"))
    manifest = Path(
        jev.build_manifest(
            dispositions_file=_write_jsonl(tmp_path / "d.ndjson", dispositions),
            freeze_manifest_file=_write_jsonl(tmp_path / "freeze.jsonl", frozen),
            output_dir=tmp_path / "manifest",
        )["manifest_file"]
    )
    first = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=FakeClient(),
        ceiling_usd=Decimal("1.00"),
        workers=1,
    )
    assert first["counts"]["completed"] == 3
    ranking = Path(first["live_ranking_file"])
    assert json.loads(ranking.read_text())["count"] == 3

    # The scan is run again. Every paper replays free, and the ranking must
    # still name all three, not zero.
    ranking.unlink()
    second = jev.run_screen(
        manifest_file=manifest,
        output_dir=tmp_path / "run",
        client=FakeClient(),
        ceiling_usd=Decimal("1.00"),
        workers=1,
    )
    assert second["counts"]["already_recorded"] == 3
    assert second["counts"]["completed"] == 0
    document = json.loads(Path(second["live_ranking_file"]).read_text())
    assert document["count"] == 3
    assert document["schema"] == JEV_LIVE_RANKING_SCHEMA
    # And the producer reads it as a real order.
    picker = PaperPicker(_ready(3), ranking_file=Path(second["live_ranking_file"]))
    assert picker.take() is not None
    assert picker.scored >= 0
