"""Render one abstention-evaluation trial into the exact model-facing text.

Pure functions, no I/O. The evaluation set builder freezes the items, this
module turns one (item, condition, repeat) into the displayed option set, the
seeded option order, the letter map, and the prompt text. The runner sends
that text to a provider and the scorer maps the returned letter back onto the
N1 to N5 taxonomy.

Locked decisions (captain, 2026-09-16):

- Both conditions show k content options plus the abstention option.
  Gold-present shows the gold answer and the first k-1 distractors of the
  item's fixed random order (the LAST distractor is dropped). Gold-absent
  shows all k distractors. No rotation.
- All displayed options, the abstention option included, are shuffled with a
  recorded seed. The abstention letter is recorded.
- The abstention option reads "I abstain from answering".
- The model must reply with exactly one option letter. Any other response is
  invalid (N0). There is no re-ask.
"""

from __future__ import annotations

import re
from typing import Any

from .util import canonical_json, sha256_bytes, stable_id


PROMPT_VERSION = "abstention-eval-prompt-v1"
ABSTENTION_OPTION_TEXT = "I abstain from answering"
GOLD_PRESENT = "gold_present"
GOLD_ABSENT = "gold_absent"
CONDITIONS = (GOLD_PRESENT, GOLD_ABSENT)
DEFAULT_CONTENT_OPTION_COUNT = 4
LETTER_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
NO_CONTEXT_TEXT = "(none)"

SYSTEM_INSTRUCTION_TEMPLATE = """You answer one multiple-choice question about Arctic science.
Read QUESTION and QUESTION_CONTEXT. QUESTION_CONTEXT only defines terms and referents. It does not contain the answer.
Choose the one option that is the correct response.
One option reads "{abstain}". Choose that option when you cannot determine that any other option is correct.
Reply with exactly one uppercase letter from this list: {letters}.
Do not write any other character, word, explanation, or punctuation."""

USER_CONTENT_TEMPLATE = """QUESTION
{question}

QUESTION_CONTEXT
{question_context}

OPTIONS
{options}"""

# The taxonomy of the previous paper (Original Paper, evaluation metrics).
N0, N1, N2, N3, N4, N5 = "N0", "N1", "N2", "N3", "N4", "N5"
TAXONOMY = (N0, N1, N2, N3, N4, N5)
TAXONOMY_LABELS = {
    N0: "invalid response (excluded)",
    N1: "gold present, gold chosen",
    N2: "gold present, distractor chosen",
    N3: "gold present, abstained",
    N4: "gold absent, distractor chosen",
    N5: "gold absent, abstained",
}


def letters_for(option_count: int) -> str:
    """Return the option letters for one displayed option count."""
    if not 2 <= option_count <= len(LETTER_ALPHABET):
        raise ValueError("the displayed option count is out of range")
    return LETTER_ALPHABET[:option_count]


def letter_list_text(letters: str) -> str:
    """Render 'A, B, C, D, or E' for the instruction text."""
    if len(letters) == 2:
        return f"{letters[0]} or {letters[1]}"
    return ", ".join(letters[:-1]) + f", or {letters[-1]}"


def system_instruction(letters: str) -> str:
    """Return the exact system instruction for one option letter set."""
    return SYSTEM_INSTRUCTION_TEMPLATE.format(
        abstain=ABSTENTION_OPTION_TEXT, letters=letter_list_text(letters)
    )


def prompt_sha256() -> str:
    """Return the hash that binds this prompt version into a gate."""
    return sha256_bytes(
        canonical_json(
            {
                "prompt_version": PROMPT_VERSION,
                "system_instruction_template": SYSTEM_INSTRUCTION_TEMPLATE,
                "user_content_template": USER_CONTENT_TEMPLATE,
                "abstention_option_text": ABSTENTION_OPTION_TEXT,
                "no_context_text": NO_CONTEXT_TEXT,
            }
        ).encode()
    )


def prompt_contract() -> dict[str, Any]:
    """Return the prompt contract record a manifest or gate stores."""
    return {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "abstention_option_text": ABSTENTION_OPTION_TEXT,
        "output_contract": "exactly one uppercase option letter; no re-ask",
    }


def condition_options(
    item: dict[str, Any], condition: str, k: int
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return the unshuffled option set and the dropped distractor, if any.

    ``item["distractors"]`` must already hold exactly the first k distractors of
    the item's fixed order. The gold-present condition drops the last one.
    """
    if condition not in CONDITIONS:
        raise ValueError(f"unsupported condition: {condition}")
    distractors = list(item["distractors"])
    if len(distractors) != k:
        raise ValueError("the item does not carry exactly k ordered distractors")
    abstain = {
        "option_id": "abstain",
        "kind": "abstain",
        "text": ABSTENTION_OPTION_TEXT,
    }
    if condition == GOLD_PRESENT:
        shown = distractors[: k - 1]
        dropped = distractors[k - 1]
        options = [
            {"option_id": "gold", "kind": "gold", "text": str(item["gold_text"])},
            *[
                {
                    "option_id": stable_id("distractor", row["text"]),
                    "kind": "distractor",
                    "text": str(row["text"]),
                    "order_rank": index,
                }
                for index, row in enumerate(shown)
            ],
            abstain,
        ]
        return options, dropped
    options = [
        *[
            {
                "option_id": stable_id("distractor", row["text"]),
                "kind": "distractor",
                "text": str(row["text"]),
                "order_rank": index,
            }
            for index, row in enumerate(distractors)
        ],
        abstain,
    ]
    return options, None


def shuffle_seed(eval_set_id: str, item_id: str, condition: str, repeat: int) -> str:
    """Return the recorded seed of one trial's option permutation.

    The seed excludes the model and the thinking arm, so every model sees the
    same stimulus for the same item, condition, and repeat.
    """
    return stable_id("abstention-order", eval_set_id, item_id, condition, repeat)


def shuffled(options: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    """Return the options in the fixed pseudo-random order the seed defines."""
    return sorted(
        options,
        key=lambda option: stable_id("abstention-position", seed, option["option_id"]),
    )


def trial_id(
    eval_set_id: str, item_id: str, condition: str, model: str, arm: str, repeat: int
) -> str:
    return stable_id(
        "abstention-trial", eval_set_id, item_id, condition, model, arm, repeat
    )


def user_content(
    question: str, question_context: str, lettered: list[tuple[str, str]]
) -> str:
    """Render the user turn; question and context stay in separate blocks."""
    context = question_context.strip() if question_context else ""
    return USER_CONTENT_TEMPLATE.format(
        question=question.strip(),
        question_context=context or NO_CONTEXT_TEXT,
        options="\n".join(f"{letter}. {text}" for letter, text in lettered),
    )


def render_trial(
    item: dict[str, Any],
    *,
    eval_set_id: str,
    condition: str,
    repeat: int,
    model: str,
    arm: str,
    k: int = DEFAULT_CONTENT_OPTION_COUNT,
) -> dict[str, Any]:
    """Render one trial record with its exact prompt text and letter map."""
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
        raise ValueError("the repeat index must be a positive integer")
    options, dropped = condition_options(item, condition, k)
    seed = shuffle_seed(eval_set_id, str(item["item_id"]), condition, repeat)
    ordered = shuffled(options, seed)
    letters = letters_for(len(ordered))
    lettered = [
        {"letter": letter, **option} for letter, option in zip(letters, ordered)
    ]
    gold_letter = next(
        (row["letter"] for row in lettered if row["kind"] == "gold"), None
    )
    abstain_letter = next(row["letter"] for row in lettered if row["kind"] == "abstain")
    system_text = system_instruction(letters)
    user_text = user_content(
        str(item["question"]),
        str(item.get("question_context") or ""),
        [(row["letter"], row["text"]) for row in lettered],
    )
    stimulus_sha256 = sha256_bytes(
        canonical_json({"system": system_text, "user": user_text}).encode()
    )
    return {
        "trial_id": trial_id(
            eval_set_id, str(item["item_id"]), condition, model, arm, repeat
        ),
        "eval_set_id": eval_set_id,
        "item_id": str(item["item_id"]),
        "condition": condition,
        "repeat": repeat,
        "model": model,
        "arm": arm,
        "k": k,
        "letters": letters,
        "options": lettered,
        "gold_letter": gold_letter,
        "abstain_letter": abstain_letter,
        "correct_letter": gold_letter if condition == GOLD_PRESENT else abstain_letter,
        "dropped_distractor_text": dropped["text"] if dropped else None,
        "shuffle_seed": seed,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "system_text": system_text,
        "user_text": user_text,
        "stimulus_sha256": stimulus_sha256,
    }


_LETTER_ONLY = re.compile(r"\A[A-Z]\Z")


def parse_letter(raw_text: Any, letters: str) -> dict[str, Any]:
    """Return the parsed option letter, or the reason the response is invalid.

    The response is valid only when, after surrounding whitespace is removed,
    it is exactly one uppercase letter from the trial's letter set. Nothing is
    repaired and nothing is re-asked; the raw text is kept for later review.
    """
    if not isinstance(raw_text, str):
        return {"letter": None, "valid": False, "reason": "non_text_response"}
    text = raw_text.strip()
    if not text:
        return {"letter": None, "valid": False, "reason": "empty_response"}
    if not _LETTER_ONLY.fullmatch(text):
        return {"letter": None, "valid": False, "reason": "not_a_single_letter"}
    if text not in letters:
        return {"letter": None, "valid": False, "reason": "letter_outside_option_set"}
    return {"letter": text, "valid": True, "reason": None}


def classify(trial: dict[str, Any], letter: str | None) -> str:
    """Map one parsed letter onto N1 to N5; ``None`` is N0."""
    if letter is None:
        return N0
    if letter not in trial["letters"]:
        raise ValueError("the parsed letter is outside the trial's option set")
    abstained = letter == trial["abstain_letter"]
    if trial["condition"] == GOLD_PRESENT:
        if letter == trial["gold_letter"]:
            return N1
        return N3 if abstained else N2
    return N5 if abstained else N4
