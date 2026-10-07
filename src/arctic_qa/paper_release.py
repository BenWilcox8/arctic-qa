"""Build the released ArcticQA and ArcticAbstain files from the frozen evaluation snapshot.

The paper analyses 194 questions and 9,312 recorded responses. Those records
live in a frozen analysis snapshot outside the repository. This module reads
that snapshot and writes plain, documented files under ``data/arcticqa-v1/``.

Run it as ``python -m arctic_qa.paper_release --snapshot <dir> --out <dir>``.
It makes no network call and no provider call.

The output holds no source evidence passage, no paper text, no provider
receipt, no credential and no absolute path of this machine. Every condition
rendering is rebuilt with ``abstention_render.render_trial`` and must hash to
the stimulus that the evaluator recorded for each of the 9,312 calls.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from . import abstention_render as render

RELEASE_ID = "arcticqa-v1"
SNAPSHOT_UTC = "2026-09-17T18:02:42Z"
VENDOR_DIRS = ("google_gemini", "anthropic_claude_code", "openai_codex")
MODEL_ORDER = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "claude-fable-5-1",
    "claude-opus-5",
    "claude-sonnet-5",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
)
MODEL_LABELS = {
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "gemini-3.7-flash": "Gemini 3.7 Flash",
    "claude-fable-5-1": "Claude Fable 5.1",
    "claude-opus-5": "Claude Opus 5",
    "claude-sonnet-5": "Claude Sonnet 5",
    "gpt-6-astra": "ChatGPT Astra",
    "gpt-5.6-sol": "ChatGPT 5.6 Sol",
    "gpt-5.6-terra": "ChatGPT 5.6 Terra",
}
MODEL_FAMILY = {
    "google_gemini": "Gemini",
    "anthropic_claude_code": "Claude",
    "openai_codex": "ChatGPT",
}
ACCESS_ROUTE = {
    "google_gemini": "Gemini API",
    "anthropic_claude_code": "Claude Code CLI",
    "openai_codex": "Codex CLI",
}
REPEATS = 3
PLANNED_RESPONSES_PER_ITEM = len(MODEL_ORDER) * len(render.CONDITIONS) * REPEATS
EXPECTED_ITEMS = 194
EXPECTED_RESPONSES = EXPECTED_ITEMS * PLANNED_RESPONSES_PER_ITEM
EXCLUDED_ITEM_ID = "aqa-7f09e4bdf6bac5c50d4c"
EXCLUDED_REASON = (
    "The evaluator recorded 40 of its 48 planned responses under an earlier run id "
    "(abstention-stream-r10). The no-retry rule of the evaluation forbids asking a "
    "trial again, so the 8 missing responses were never collected. The analysis "
    "takes only questions with all 48 responses."
)

# The paper says "answer-present" and "answer-absent". The code says
# gold_present and gold_absent. The released files use the paper's words.
CONDITION_NAMES = {
    render.GOLD_PRESENT: "answer_present",
    render.GOLD_ABSENT: "answer_absent",
}
OUTCOME_LABELS = {
    render.N0: "invalid response (excluded from metrics)",
    render.N1: "answer present, correct answer chosen",
    render.N2: "answer present, distractor chosen",
    render.N3: "answer present, abstained (false abstention)",
    render.N4: "answer absent, distractor chosen (false commitment)",
    render.N5: "answer absent, abstained (correct abstention)",
}

_HOME = re.compile(r"/home/[^/\s'\"]+")
_DATA_ROOT = re.compile(r"/mnt/[^\s'\"]+")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scrub(text: Any) -> Any:
    """Replace the absolute paths of the evaluation machine in an error message."""
    if not isinstance(text, str):
        return text
    return _DATA_ROOT.sub("<data-root>", _HOME.sub("<home>", text))


def invalid_cause(row: dict[str, Any]) -> str | None:
    """Return which of three causes made an N0 response invalid.

    A response the provider never produced is an infrastructure fault. A
    completed response that the provider ended as a refusal is a vendor
    safeguard. What is left is the model breaking the one-letter contract.
    """
    if row["outcome"] != render.N0:
        return None
    response = row.get("response") or {}
    if str(response.get("state")) != "completed":
        return "infrastructure_no_provider_answer"
    if str(response.get("finish_reason")) == "refusal":
        return "provider_safeguard_refusal"
    return "model_format_violation"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def jsonl_text(rows: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n" for row in rows
    )


def load_sets(
    snapshot: Path, hashes: list[tuple[str, str]]
) -> dict[str, dict[str, Any]]:
    """Return item_id -> (frozen item, eval set id), with each set's binding checked."""
    sets: dict[str, dict[str, Any]] = {}
    for set_dir in sorted((snapshot / "evaluation" / "sets").iterdir()):
        manifest_path = set_dir / "manifest.json"
        items_path = set_dir / "items.jsonl"
        manifest = json.loads(manifest_path.read_text())
        digest = sha256_file(items_path)
        if digest != manifest["items_sha256"]:
            raise ValueError(
                f"{items_path.name} of {set_dir.name} does not match its manifest"
            )
        if manifest["prompt_contract"]["prompt_sha256"] != render.prompt_sha256():
            raise ValueError(f"{set_dir.name} binds another prompt contract")
        for path in (manifest_path, items_path):
            hashes.append((str(path.relative_to(snapshot)), sha256_file(path)))
        for item in read_jsonl(items_path):
            if item["item_id"] in sets:
                raise ValueError(f"item {item['item_id']} is in two evaluation sets")
            sets[item["item_id"]] = {
                "item": item,
                "eval_set_id": manifest["eval_set_id"],
            }
    return sets


def load_responses(
    runs_dir: Path, snapshot: Path, hashes: list[tuple[str, str]] | None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item_dir in sorted(runs_dir.iterdir()):
        if not item_dir.is_dir():
            continue
        for vendor in VENDOR_DIRS:
            path = item_dir / vendor / "responses.jsonl"
            if not path.exists():
                continue
            if hashes is not None:
                hashes.append((str(path.relative_to(snapshot)), sha256_file(path)))
            for row in read_jsonl(path):
                row["vendor"] = vendor
                rows.append(row)
    return rows


def source_metadata(snapshot: Path, item_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return the status and the bibliographic record of each item's paper."""
    db = snapshot / "production" / "state.sqlite3"
    wal = Path(f"{db}-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("the frozen state database has an unmerged write-ahead log")
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        out = {}
        for item_id in item_ids:
            row = conn.execute(
                "select c.status, c.run_id, s.doi, s.title, s.year "
                "from candidates c join sources s on s.source_id = c.source_id "
                "where c.item_id = ?",
                (item_id,),
            ).fetchall()
            if len(row) != 1:
                raise ValueError(f"item {item_id} has {len(row)} source rows")
            status, run_id, doi, title, year = row[0]
            out[item_id] = {
                "status": status,
                "run_id": run_id,
                "doi": doi,
                "title": title,
                "year": year,
            }
        return out
    finally:
        conn.close()


def item_record(
    item: dict[str, Any], eval_set_id: str, meta: dict[str, Any]
) -> dict[str, Any]:
    distractors = sorted(item["distractors"], key=lambda row: row["order_rank"])
    # The recorded order also ranks the candidates that failed verification, so
    # the four accepted distractors must keep their relative order in it.
    order = iter(item["distractor_order"]["order"])
    if not all(row["text"] in order for row in distractors):
        raise ValueError(f"item {item['item_id']} has an inconsistent distractor order")
    roles = item.get("construction_roles") or {}
    return {
        "item_id": item["item_id"],
        "question": item["question"],
        "question_context": item.get("question_context") or "",
        "gold_answer": item["gold_text"],
        "distractors": [
            {
                "rank": row["order_rank"],
                "text": row["text"],
                "type": row["type"],
                "verification": row["verification_label"],
            }
            for row in distractors
        ],
        "answer_present_drops": distractors[-1]["text"],
        "status": meta["status"],
        "language_script": item["strata"]["script"],
        "numeric_answer": bool(item["strata"]["numeric"]),
        "paper": {
            "doi": meta["doi"],
            "title": meta["title"],
            "year": meta["year"],
            "paper_family_id": item["family_id"],
        },
        "construction": {
            "writer_model": roles.get("writer_model"),
            "option_verifier_models": roles.get("option_verifier_models"),
            "generation_prompt_version": item["generation_prompt_version"],
        },
        "eval_set_id": eval_set_id,
        "candidate_hash": item["candidate_hash"],
    }


def condition_records(
    item: dict[str, Any], record: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = []
    for condition in render.CONDITIONS:
        options, dropped = render.condition_options(
            item, condition, render.DEFAULT_CONTENT_OPTION_COUNT
        )
        present = condition == render.GOLD_PRESENT
        rows.append(
            {
                "item_id": record["item_id"],
                "condition": CONDITION_NAMES[condition],
                "question": record["question"],
                "question_context": record["question_context"],
                "options": [
                    {
                        "option_id": option["option_id"],
                        "kind": option["kind"],
                        "text": option["text"],
                    }
                    for option in options
                ],
                "correct_action": "choose_gold_answer" if present else "abstain",
                "correct_option_text": record["gold_answer"]
                if present
                else render.ABSTENTION_OPTION_TEXT,
                "dropped_distractor": dropped["text"] if dropped else None,
                "option_order": (
                    "shuffled per call; responses.jsonl holds the letters each call showed"
                ),
            }
        )
    return rows


def chosen_kind(row: dict[str, Any]) -> str | None:
    letter = row.get("parsed_letter")
    if letter is None:
        return None
    return next(
        option["kind"] for option in row["options"] if option["letter"] == letter
    )


def response_record(row: dict[str, Any], rendered: dict[str, Any]) -> dict[str, Any]:
    response = row.get("response") or {}
    harness = response.get("harness") or {}
    usage = response.get("usage") or {}
    return {
        "trial_id": row["trial_id"],
        "item_id": row["item_id"],
        "model": row["model"],
        "model_label": MODEL_LABELS[row["model"]],
        "model_family": MODEL_FAMILY[row["vendor"]],
        "access_route": ACCESS_ROUTE[row["vendor"]],
        "harness_version": harness.get("harness_version"),
        "reasoning_effort": row["arm"],
        "condition": CONDITION_NAMES[row["condition"]],
        "trial": int(row["repeat"]),
        "options": [
            {"letter": option["letter"], "kind": option["kind"], "text": option["text"]}
            for option in row["options"]
        ],
        "correct_letter": row["correct_letter"],
        "gold_letter": row["gold_letter"],
        "abstain_letter": row["abstain_letter"],
        "prompt_user_text": rendered["user_text"],
        "stimulus_sha256": row["stimulus_sha256"],
        "shuffle_seed": row["shuffle_seed"],
        "raw_response_text": response.get("raw_text"),
        "chosen_letter": row.get("parsed_letter"),
        "chosen_kind": chosen_kind(row),
        "outcome": row["outcome"],
        "outcome_label": OUTCOME_LABELS[row["outcome"]],
        "valid": bool(row["valid"]),
        "invalid_reason": row.get("invalid_reason"),
        "invalid_cause": invalid_cause(row),
        "response_state": response.get("state"),
        "finish_reason": response.get("finish_reason"),
        "error": scrub(response.get("error")),
        "model_version": response.get("model_version"),
        "usage": {
            "prompt_tokens": usage.get("promptTokenCount"),
            "thinking_tokens": usage.get("thoughtsTokenCount"),
            "output_tokens": usage.get("candidatesTokenCount"),
            "total_tokens": usage.get("totalTokenCount"),
        },
        "latency_seconds": response.get("latency_seconds"),
        "cost_usd": response.get("cost_usd"),
        "recorded_at_utc": row["recorded_at_utc"],
        "evaluator_commit": row.get("code_commit"),
        "eval_set_id": row["eval_set_id"],
    }


RESPONSE_CSV_FIELDS = (
    "trial_id",
    "item_id",
    "model",
    "model_label",
    "condition",
    "trial",
    "option_A",
    "option_B",
    "option_C",
    "option_D",
    "option_E",
    "correct_letter",
    "gold_letter",
    "abstain_letter",
    "raw_response_text",
    "chosen_letter",
    "chosen_kind",
    "outcome",
    "valid",
    "invalid_cause",
    "finish_reason",
    "recorded_at_utc",
)


def responses_csv(records: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=RESPONSE_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        row = {key: record.get(key) for key in RESPONSE_CSV_FIELDS}
        for option in record["options"]:
            row[f"option_{option['letter']}"] = option["text"]
        writer.writerow(row)
    return buffer.getvalue()


def response_sort_key(record: dict[str, Any]) -> tuple:
    return (
        record["item_id"],
        MODEL_ORDER.index(record["model"]),
        0 if record["condition"] == "answer_present" else 1,
        record["trial"],
    )


def build(snapshot: Path, out: Path) -> dict[str, Any]:
    """Write every release file under ``out`` and return the manifest."""
    source_hashes: list[tuple[str, str]] = []
    sets = load_sets(snapshot, source_hashes)
    raw = load_responses(snapshot / "evaluation" / "runs", snapshot, source_hashes)
    keys = Counter(
        (r["item_id"], r["model"], r["arm"], r["condition"], int(r["repeat"]))
        for r in raw
    )
    if any(count > 1 for count in keys.values()):
        raise ValueError("a trial has more than one recorded response")
    per_item = Counter(r["item_id"] for r in raw)
    complete = sorted(i for i, n in per_item.items() if n == PLANNED_RESPONSES_PER_ITEM)
    if complete != sorted(per_item) or sorted(sets) != complete:
        raise ValueError("the snapshot holds a question without all 48 responses")
    if len(complete) != EXPECTED_ITEMS or len(raw) != EXPECTED_RESPONSES:
        raise ValueError(
            f"expected {EXPECTED_ITEMS} items and {EXPECTED_RESPONSES} responses"
        )

    meta = source_metadata(snapshot, complete)
    items = [
        item_record(sets[i]["item"], sets[i]["eval_set_id"], meta[i]) for i in complete
    ]
    if {row["status"] for row in items} != {"machine_accepted_unverified"}:
        raise ValueError("an analysed item is not machine_accepted_unverified")
    conditions = [
        row
        for i, record in zip(complete, items)
        for row in condition_records(sets[i]["item"], record)
    ]

    # Re-render every call from the frozen item and its recorded seed inputs.
    # The rebuilt prompt must hash to the stimulus that the evaluator recorded.
    system_texts = set()
    records = []
    for row in raw:
        entry = sets[row["item_id"]]
        rendered = render.render_trial(
            entry["item"],
            eval_set_id=entry["eval_set_id"],
            condition=row["condition"],
            repeat=int(row["repeat"]),
            model=row["model"],
            arm=row["arm"],
        )
        if rendered["stimulus_sha256"] != row["stimulus_sha256"]:
            raise ValueError(
                f"trial {row['trial_id']} does not re-render to its stimulus"
            )
        if rendered["shuffle_seed"] != row["shuffle_seed"] or [
            (o["letter"], o["kind"], o["text"]) for o in rendered["options"]
        ] != [(o["letter"], o["kind"], o["text"]) for o in row["options"]]:
            raise ValueError(f"trial {row['trial_id']} shows another option order")
        system_texts.add(rendered["system_text"])
        records.append(response_record(row, rendered))
    if len(system_texts) != 1:
        raise ValueError("the calls do not share one system instruction")
    records.sort(key=response_sort_key)

    excluded_rows = load_responses(snapshot / "evaluation-r10" / "runs", snapshot, None)
    excluded_rows = [r for r in excluded_rows if r["item_id"] == EXCLUDED_ITEM_ID]
    excluded_by_model = Counter(r["model"] for r in excluded_rows)
    excluded_meta = source_metadata(snapshot, [EXCLUDED_ITEM_ID])[EXCLUDED_ITEM_ID]
    excluded = [
        {
            "item_id": EXCLUDED_ITEM_ID,
            "status": excluded_meta["status"],
            "reason": EXCLUDED_REASON,
            "responses_recorded": len(excluded_rows),
            "responses_planned": PLANNED_RESPONSES_PER_ITEM,
            "responses_by_model": {m: excluded_by_model.get(m, 0) for m in MODEL_ORDER},
            "in_released_responses": False,
        }
    ]
    if len(excluded_rows) != 40:
        raise ValueError("the excluded question no longer holds 40 responses")

    prompt = {
        "prompt_version": render.PROMPT_VERSION,
        "prompt_sha256": render.prompt_sha256(),
        "system_text": system_texts.pop(),
        "user_template": render.USER_CONTENT_TEMPLATE,
        "no_context_text": render.NO_CONTEXT_TEXT,
        "abstention_option_text": render.ABSTENTION_OPTION_TEXT,
        "letters": render.letters_for(render.DEFAULT_CONTENT_OPTION_COUNT + 1),
        "output_contract": "exactly one uppercase option letter; no re-ask",
        "order_scope": render.ORDER_SCOPE,
        "renderer": "src/arctic_qa/abstention_render.py (render_trial)",
    }

    out.mkdir(parents=True, exist_ok=True)
    files = {
        "items.jsonl": jsonl_text(items),
        "conditions.jsonl": jsonl_text(conditions),
        "responses.jsonl": jsonl_text(records),
        "responses.csv": responses_csv(records),
        "excluded-items.json": json.dumps(excluded, indent=2, ensure_ascii=False)
        + "\n",
        "prompt.json": json.dumps(prompt, indent=2, ensure_ascii=False) + "\n",
        "source-hashes.txt": "".join(
            f"{digest}  {path}\n" for path, digest in sorted(source_hashes)
        ),
    }
    for text in files.values():
        if "/home/" in text or "/mnt/" in text:
            raise ValueError(
                "a release file holds an absolute path of the build machine"
            )
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")

    invalid = Counter(r["invalid_cause"] for r in records if not r["valid"])
    manifest = {
        "release_id": RELEASE_ID,
        "snapshot_utc": SNAPSHOT_UTC,
        "builder": "src/arctic_qa/paper_release.py",
        "builder_sha256": sha256_file(Path(__file__)),
        "renderer_sha256": sha256_file(Path(render.__file__)),
        "counts": {
            "items": len(items),
            "condition_renderings": len(conditions),
            "responses": len(records),
            "valid_responses": sum(1 for r in records if r["valid"]),
            "invalid_responses": sum(1 for r in records if not r["valid"]),
            "invalid_by_cause": dict(sorted(invalid.items())),
            "models": len(MODEL_ORDER),
            "trials_per_condition": REPEATS,
            "excluded_items": len(excluded),
        },
        "source_files": len(source_hashes),
        "source_digest": sha256_text(files["source-hashes.txt"]),
        "files": {name: sha256_text(text) for name, text in sorted(files.items())},
        "withheld": [
            "source evidence passages and paper text (rights cleared only for private analysis)",
            "provider receipts, request keys and harness command lines",
            "credentials and absolute paths of the evaluation machine",
        ],
    }
    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--snapshot",
        required=True,
        type=Path,
        help="the frozen snapshot-final directory",
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="the release directory to write"
    )
    args = parser.parse_args(argv)
    manifest = build(args.snapshot, args.out)
    print(json.dumps(manifest["counts"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
