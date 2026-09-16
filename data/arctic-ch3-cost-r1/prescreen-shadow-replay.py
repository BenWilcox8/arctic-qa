"""Replay the structural pre-screen in shadow mode over the chapter 2 papers.

Read-only. The chapter 2 database and corpus are opened in read-only mode and
nothing is written outside the output file. Reports the fire rate of the
shadow verdict against the families that froze a finding and against the
eligible papers that froze nothing (chapter 2 yield audit, section 4.5 e).

Usage: python3 prescreen-shadow-replay.py <state.sqlite3> <namespace> <audit-evidence-dir> <out.json>
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from arctic_qa import generation  # noqa: E402
from arctic_qa.errors import CandidateRejectedError  # noqa: E402

DB, NAMESPACE, EVIDENCE, OUT = sys.argv[1:5]
CAMPAIGN = "arctic-qa-production-campaign-002"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def load_chunks(source_id: str) -> list[dict]:
    artifact = con.execute(
        "SELECT * FROM artifacts WHERE source_id=? AND kind='chunks' ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    metadata = json.loads(artifact["metadata_json"] or "{}")
    root = Path(metadata.get("corpus_root") or NAMESPACE)
    path = root / artifact["relative_path"]
    if not path.is_file():
        path = Path(NAMESPACE) / artifact["relative_path"]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


with_finding = {
    row["source_id"]
    for row in con.execute("SELECT DISTINCT source_id FROM findings WHERE run_id=?", (CAMPAIGN,))
}
rejected_sources = set()
for row in con.execute("SELECT source_id, detail_json FROM rejection_ledger WHERE stage='generation'"):
    if row["source_id"] and json.loads(row["detail_json"]).get("campaign_id") == CAMPAIGN:
        rejected_sources.add(row["source_id"])
without_finding = rejected_sources - with_finding

groups = {"froze_a_finding": sorted(with_finding), "froze_nothing": sorted(without_finding)}
report = {"contract_version": generation.FINDING_PRESCREEN_CONTRACT_VERSION, "groups": {}}
for name, source_ids in groups.items():
    verdicts = []
    errors = Counter()
    for source_id in source_ids:
        source = con.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if source is None:
            errors["missing_source"] += 1
            continue
        source = dict(source)
        try:
            chunks = load_chunks(source_id)
            _, scope_spans, _ = generation._eligible_generation_scope(source, chunks)
            if scope_spans is None:
                spans = {s["span_id"]: s for chunk in chunks for s in generation._finding_spans(chunk)}
            else:
                spans = {s["span_id"]: s for s in scope_spans}
            verdict = generation._structural_prescreen(spans, chunks)
        except CandidateRejectedError as error:
            errors[f"rejected:{error.reason_code}"] += 1
            continue
        except Exception as error:  # noqa: BLE001 - a measurement must report, not stop
            errors[f"error:{type(error).__name__}"] += 1
            continue
        verdicts.append({"source_id": source_id, **verdict})
    fired = [v for v in verdicts if v["would_reject"]]
    report["groups"][name] = {
        "papers": len(source_ids),
        "measured": len(verdicts),
        "errors": dict(errors),
        "would_reject": len(fired),
        "fire_rate": round(len(fired) / len(verdicts), 3) if verdicts else None,
        "fired_source_ids": [v["source_id"] for v in fired],
        "median_qualifying_spans": sorted(v["qualifying_spans"] for v in verdicts)[len(verdicts) // 2] if verdicts else None,
        "cyrillic_papers": sum(1 for v in verdicts if v["cyrillic_share"] > 0.3),
    }
json.dump(report, open(OUT, "w"), indent=1)
print(json.dumps(report, indent=1))
