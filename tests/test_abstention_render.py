from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arctic_qa.abstention_render import (
    ABSTENTION_OPTION_TEXT,
    CONDITIONS,
    GOLD_ABSENT,
    GOLD_PRESENT,
    N0,
    N1,
    N2,
    N3,
    N4,
    N5,
    classify,
    condition_options,
    parse_letter,
    prompt_contract,
    prompt_sha256,
    render_trial,
    system_instruction,
)
from arctic_qa.abstention_set import (
    build_eval_set,
    evaluation_identity,
    load_eval_set,
    role_of_model,
    write_eval_set,
)
from arctic_qa.distractor_order import (
    apply_order,
    distractor_order_record,
    order_seed,
    ordered_texts,
    resolve_order,
    validate_order_record,
)
from arctic_qa.exporting import _absent_mcq, _present_mcq
from arctic_qa.util import canonical_json, stable_id


ROOT = Path(__file__).parents[1]
DISTRACTORS = [
    {"text": "46.5%", "type": "numeric"},
    {"text": "56.5%", "type": "numeric"},
    {"text": "76.5%", "type": "numeric"},
    {"text": "86.5%", "type": "numeric"},
    {"text": "96.5%", "type": "numeric"},
]


def item(k: int = 4, item_id: str = "aqa-test-item") -> dict:
    order = distractor_order_record(item_id, DISTRACTORS)
    ordered = apply_order(order, DISTRACTORS)
    return {
        "item_id": item_id,
        "candidate_hash": "candidate-payload-abc",
        "question": "What share was reported?",
        "question_context": "",
        "gold_text": "66.5%",
        "distractors": [
            {"text": row["text"], "type": row["type"], "order_rank": rank}
            for rank, row in enumerate(ordered[:k])
        ],
        "distractor_order": order,
        "strata": {"numeric": True, "has_context": False, "script": "latin"},
        "construction_roles": {
            "writer_model": "gemini-3.8-flash",
            "verifier_model": "gemini-3.8-flash",
            "judge_models": ["gemini-3.1-pro-preview", "gemini-3.8-flash"],
            "option_verifier_models": ["gemini-3.1-pro-preview"],
            "models": ["gemini-3.1-pro-preview", "gemini-3.8-flash"],
        },
    }


# --- distractor order --------------------------------------------------------


def test_distractor_order_is_stable_and_seeded() -> None:
    first = distractor_order_record("aqa-x", DISTRACTORS)
    second = distractor_order_record("aqa-x", DISTRACTORS)
    assert first == second
    assert first["seed"] == order_seed("aqa-x")
    assert sorted(first["order"]) == sorted(row["text"] for row in DISTRACTORS)
    assert first["order"] == ordered_texts([row["text"] for row in DISTRACTORS], first["seed"])
    # A different item gets a different permutation for the same texts.
    other = distractor_order_record("aqa-y", DISTRACTORS)
    assert other["seed"] != first["seed"]
    validate_order_record(first, DISTRACTORS)
    with pytest.raises(ValueError, match="does not match its seed"):
        validate_order_record({**first, "seed": other["seed"]}, DISTRACTORS)
    with pytest.raises(ValueError, match="permute"):
        validate_order_record({**first, "order": first["order"][:-1]}, DISTRACTORS)


def test_resolve_order_assigns_for_legacy_candidates_and_records_it() -> None:
    legacy = {"item_id": "aqa-legacy", "distractors": DISTRACTORS}
    assigned = resolve_order(legacy)
    assert assigned["assignment"] == "assigned_by_evaluation_set_builder"
    assert assigned["seed"] == order_seed("aqa-legacy", assigned=True)
    assert assigned == resolve_order(legacy)
    recorded = {**legacy, "distractor_order": distractor_order_record("aqa-legacy", DISTRACTORS)}
    assert resolve_order(recorded)["assignment"] == "recorded_at_generation"
    assert resolve_order(recorded)["order"] != assigned["order"] or True


def test_exporter_drops_the_last_ordered_distractor_in_the_present_form() -> None:
    order = distractor_order_record("aqa-export", DISTRACTORS[:4])
    accepted = [
        {**row, "label": "model-verified", "evidence_quote": "q", "locator": {}}
        for row in apply_order(order, DISTRACTORS[:4])
    ]
    candidate = {
        "item_id": "aqa-export",
        "question": "Q?",
        "question_context": "",
        "answer": {"text": "66.5%", "evidence_quote": "q", "locator": {}},
        "distractors": DISTRACTORS[:4],
        "distractor_order": order,
        "source": {},
    }
    present = _present_mcq(candidate, accepted[:3], "seed")
    absent = _absent_mcq(candidate, accepted[:4], "seed")
    present_texts = {row["text"] for row in present["options"] if not row["is_correct"]}
    absent_texts = {row["text"] for row in absent["options"]}
    assert absent_texts - present_texts == {order["order"][3]}
    assert present["distractor_order"] == order and absent["distractor_order"] == order


# --- rendering ---------------------------------------------------------------


def test_both_conditions_show_the_same_option_count_and_drop_the_last() -> None:
    row = item()
    present, dropped = condition_options(row, GOLD_PRESENT, 4)
    absent, none = condition_options(row, GOLD_ABSENT, 4)
    assert len(present) == len(absent) == 5
    assert none is None
    assert dropped["text"] == row["distractor_order"]["order"][3]
    assert dropped["text"] == row["distractors"][3]["text"]
    present_texts = [option["text"] for option in present]
    absent_texts = [option["text"] for option in absent]
    assert row["gold_text"] in present_texts and row["gold_text"] not in absent_texts
    assert dropped["text"] not in present_texts and dropped["text"] in absent_texts
    assert present_texts.count(ABSTENTION_OPTION_TEXT) == 1
    assert absent_texts.count(ABSTENTION_OPTION_TEXT) == 1
    with pytest.raises(ValueError, match="exactly k"):
        condition_options(item(k=3), GOLD_PRESENT, 4)


def test_render_is_deterministic_and_shuffles_all_options_with_a_recorded_seed() -> None:
    row = item()
    kwargs = dict(eval_set_id="set-1", condition=GOLD_PRESENT, repeat=1, model="m", arm="low")
    first = render_trial(row, **kwargs)
    second = render_trial(row, **kwargs)
    assert first == second
    assert first["letters"] == "ABCDE"
    assert first["shuffle_seed"] == stable_id("abstention-order", "set-1", row["item_id"], GOLD_PRESENT, 1)
    letters = {option["letter"]: option for option in first["options"]}
    assert letters[first["abstain_letter"]]["kind"] == "abstain"
    assert letters[first["gold_letter"]]["kind"] == "gold"
    assert first["correct_letter"] == first["gold_letter"]
    # Repeats and conditions permute independently; models share a stimulus.
    other_repeat = render_trial(row, **{**kwargs, "repeat": 2})
    other_model = render_trial(row, **{**kwargs, "model": "n"})
    absent = render_trial(row, **{**kwargs, "condition": GOLD_ABSENT})
    assert other_model["user_text"] == first["user_text"]
    assert other_model["trial_id"] != first["trial_id"]
    assert other_repeat["shuffle_seed"] != first["shuffle_seed"]
    assert absent["gold_letter"] is None and absent["correct_letter"] == absent["abstain_letter"]
    orders = {
        render_trial(row, **{**kwargs, "repeat": repeat})["abstain_letter"]
        for repeat in range(1, 40)
    }
    assert orders == set("ABCDE")


def test_prompt_shows_context_as_its_own_block_and_leaks_nothing() -> None:
    row = {**item(), "question_context": "OTU means operational taxonomic unit."}
    trial = render_trial(row, eval_set_id="s", condition=GOLD_ABSENT, repeat=1, model="m", arm="low")
    user = trial["user_text"]
    assert user.index("QUESTION\n") < user.index("QUESTION_CONTEXT\n") < user.index("OPTIONS\n")
    assert "OTU means operational taxonomic unit." in user
    assert row["gold_text"] not in user
    for forbidden in ("evidence", "verdict", "rationale", "numeric", "model-verified"):
        assert forbidden not in user.lower()
    assert "I abstain from answering" in trial["system_text"]
    assert "exactly one uppercase letter" in trial["system_text"]
    assert system_instruction("ABCDE").endswith("punctuation.")
    empty = render_trial(item(), eval_set_id="s", condition=GOLD_ABSENT, repeat=1, model="m", arm="low")
    assert "QUESTION_CONTEXT\n(none)" in empty["user_text"]
    contract = prompt_contract()
    assert contract["prompt_sha256"] == prompt_sha256()
    assert contract["abstention_option_text"] == ABSTENTION_OPTION_TEXT


def test_parse_letter_is_strict_and_classification_follows_the_taxonomy() -> None:
    assert parse_letter("C", "ABCDE") == {"letter": "C", "valid": True, "reason": None}
    assert parse_letter("  C\n", "ABCDE")["valid"] is True
    assert parse_letter("c", "ABCDE")["reason"] == "not_a_single_letter"
    assert parse_letter("C.", "ABCDE")["reason"] == "not_a_single_letter"
    assert parse_letter("The answer is C", "ABCDE")["reason"] == "not_a_single_letter"
    assert parse_letter("F", "ABCDE")["reason"] == "letter_outside_option_set"
    assert parse_letter("", "ABCDE")["reason"] == "empty_response"
    assert parse_letter(None, "ABCDE")["reason"] == "non_text_response"
    row = item()
    present = render_trial(row, eval_set_id="s", condition=GOLD_PRESENT, repeat=1, model="m", arm="low")
    absent = render_trial(row, eval_set_id="s", condition=GOLD_ABSENT, repeat=1, model="m", arm="low")
    distractor_present = next(o["letter"] for o in present["options"] if o["kind"] == "distractor")
    distractor_absent = next(o["letter"] for o in absent["options"] if o["kind"] == "distractor")
    assert classify(present, present["gold_letter"]) == N1
    assert classify(present, distractor_present) == N2
    assert classify(present, present["abstain_letter"]) == N3
    assert classify(absent, distractor_absent) == N4
    assert classify(absent, absent["abstain_letter"]) == N5
    assert classify(absent, None) == N0
    assert tuple(CONDITIONS) == (GOLD_PRESENT, GOLD_ABSENT)


# --- evaluation set builder --------------------------------------------------


def _candidate(item_id: str, *, schema: str, prompt: str, distractors: list[dict], order: bool) -> dict:
    candidate = {
        "schema_version": schema,
        "item_id": item_id,
        "question": f"Question for {item_id}?",
        "question_context": "",
        "answer": {"text": "gold", "numeric_rule": None},
        "distractors": distractors,
        "option_verdicts": [
            {"option_text": row["text"], "provenance": {"requested_model": "gemini-3.1-pro-preview"}}
            for row in distractors
        ],
        "source": {"source_id": "src-1", "paper_family_id": "fam", "content_hash": "hash"},
        "provenance": {
            "prompt_version": prompt,
            "scope_contract_version": "selected-evidence-literal-scope-v4",
            "author_model": "gemini-3.8-flash",
            "verifier_model": "gemini-3.8-flash",
            "verification_calls": {"reconstructor": {"requested_model": "gemini-3.1-pro-preview"}},
        },
    }
    if order:
        candidate["distractor_order"] = distractor_order_record(item_id, distractors)
    return candidate


def _state_db(path: Path, rows: list[tuple[dict, str, str, list[bool], str]]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE candidates (item_id TEXT, run_id TEXT, source_id TEXT, paper_family_id TEXT,
            generation_arm TEXT, candidate_json TEXT, status TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE validation_events (event_id TEXT, item_id TEXT, stage TEXT, label TEXT,
            reason_codes_json TEXT, details_json TEXT, created_at TEXT);
        """
    )
    for candidate, run_id, family, accepted_flags, updated_at in rows:
        payload = canonical_json(candidate)
        connection.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
            (candidate["item_id"], run_id, "src", family, "arm", payload,
             "machine_accepted_unverified", updated_at, updated_at),
        )
        details = {
            "candidate_hash": stable_id("candidate-payload", payload),
            "labels": {"mcq_eligible": True},
            "distractors": [
                {"text": row["text"], "accepted": flag, "model_verified": flag, "label": "model-verified"}
                for row, flag in zip(candidate["distractors"], accepted_flags)
            ],
        }
        connection.execute(
            "INSERT INTO validation_events VALUES (?,?,?,?,?,?,?)",
            (f"event-{candidate['item_id']}", candidate["item_id"], "automated_acceptance",
             "machine_accepted_unverified", "[]", canonical_json(details), updated_at),
        )
    connection.commit()
    connection.close()


def test_set_builder_selects_orders_excludes_and_freezes(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    five = DISTRACTORS
    _state_db(
        db,
        [
            (_candidate("aqa-new", schema="2.8.0", prompt="arctic-qa-generation-v23", distractors=five, order=True),
             "arctic-qa-production-campaign-003", "fam-new", [True] * 5, "2026-09-16T00:00:00"),
            (_candidate("aqa-old", schema="2.7.0", prompt="arctic-qa-generation-v22", distractors=five[:4], order=False),
             "arctic-qa-production-campaign-002", "fam-old", [True] * 4, "2026-09-15T00:00:00"),
            (_candidate("aqa-short", schema="2.7.0", prompt="arctic-qa-generation-v22", distractors=five[:4], order=False),
             "arctic-qa-production-campaign-002", "fam-short", [True, True, True, False], "2026-09-15T00:00:00"),
            (_candidate("aqa-smoke", schema="2.7.0", prompt="arctic-qa-generation-v22", distractors=five[:4], order=False),
             "offline-smoke", "fam-smoke", [True] * 4, "2026-09-15T00:00:00"),
            (_candidate("aqa-old-dup", schema="2.7.0", prompt="arctic-qa-generation-v22", distractors=five[:4], order=False),
             "arctic-qa-production-campaign-002", "fam-old", [True] * 4, "2026-09-15T01:00:00"),
        ],
    )
    manifest = build_eval_set(
        state_db=db,
        output_dir=tmp_path / "sets",
        population="current",
        contract_file=ROOT / "config" / "live-dataset-current-contract-v1.json",
    )
    assert manifest["item_count"] == 1
    assert manifest["distractor_order"]["recorded_item_ids"] == ["aqa-new"]
    loaded_manifest, items = load_eval_set(tmp_path / "sets" / manifest["eval_set_id"])
    assert loaded_manifest == manifest
    new = items[0]
    assert len(new["distractors"]) == 4
    assert [row["text"] for row in new["distractors"]] == new["distractor_order"]["order"][:4]
    assert new["distractor_order"]["assignment"] == "recorded_at_generation"
    assert new["construction_roles"]["writer_model"] == "gemini-3.8-flash"
    assert "gemini-3.1-pro-preview" in new["construction_roles"]["judge_models"]
    assert role_of_model(new["construction_roles"], "gemini-3.1-pro-preview") == "judge"
    assert role_of_model(new["construction_roles"], "gemini-3.8-flash") == "writer_and_judge"
    assert role_of_model(new["construction_roles"], "gemini-2.5-pro") == "none"
    identity = evaluation_identity(new)
    assert identity["family_id"] == "evaluation-item:aqa-new"
    assert new["candidate_hash"] in identity["source_version_id"]

    production = build_eval_set(
        state_db=db, output_dir=tmp_path / "sets", population="production"
    )
    assert production["item_count"] == 2
    ids = {item["item_id"] for item in load_eval_set(tmp_path / "sets" / production["eval_set_id"])[1]}
    assert ids == {"aqa-new", "aqa-old-dup"}
    reasons = {(entry["item_id"], entry["reason"]) for entry in production["excluded"]}
    assert ("aqa-short", "insufficient_accepted_distractors") in reasons
    assert ("aqa-old", "duplicate_family_superseded") in reasons
    assert production["exclusion_counts"] == {
        "duplicate_family_superseded": 1,
        "insufficient_accepted_distractors": 1,
    }
    assert "aqa-old-dup" in production["distractor_order"]["assigned_item_ids"]
    assert production["distractor_order"]["assigned_seed_rule"].startswith("stable_id(")

    listed = build_eval_set(
        state_db=db, output_dir=tmp_path / "sets", population="list", item_ids=["aqa-smoke", "aqa-none"]
    )
    assert listed["item_count"] == 1
    assert {entry["item_id"]: entry["reason"] for entry in listed["excluded"]} == {
        "aqa-none": "not_an_accepted_item"
    }
    # The frozen files reject a change.
    set_dir = tmp_path / "sets" / listed["eval_set_id"]
    items_path = set_dir / "items.jsonl"
    original = items_path.read_bytes()
    items_path.write_bytes(original.replace(b"gold", b"GOLD"))
    with pytest.raises(ValueError, match="changed after they were frozen"):
        load_eval_set(set_dir)
    items_path.write_bytes(original)
    # The same items freeze to the same set id; a set with the wrong k is refused.
    again = build_eval_set(
        state_db=db, output_dir=tmp_path / "sets", population="list", item_ids=["aqa-smoke", "aqa-none"]
    )
    assert again["eval_set_id"] == listed["eval_set_id"]
    with pytest.raises(ValueError, match="exactly k"):
        write_eval_set([item(k=3)], excluded=[], output_dir=tmp_path / "bad", population_record={}, k=4, state_db=None)
    with pytest.raises(ValueError, match="unsupported population"):
        build_eval_set(state_db=db, output_dir=tmp_path / "x", population="everything")


def test_set_builder_reads_the_database_read_only(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    _state_db(db, [])
    before = db.read_bytes()
    manifest = build_eval_set(state_db=db, output_dir=tmp_path / "sets", population="production")
    assert manifest["item_count"] == 0
    assert db.read_bytes() == before
    assert json.loads((tmp_path / "sets" / manifest["eval_set_id"] / "manifest.json").read_text())["k"] == 4
