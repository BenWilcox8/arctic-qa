"""Fixed random distractor order for every benchmark item.

Captain decision (2026-09-16): "Order the existing distractors at random but
make sure that they're still ordered. And then drop the last one at test time."

The order is a seeded pseudo-random permutation of the distractor texts. The
seed is recorded with the order, so the permutation can be recomputed and
audited. A new candidate records the order at generation time. An older
candidate without the field gets the same deterministic assignment keyed on
its item id; the evaluation set builder records that assignment in its
manifest.

The gold-present condition of the abstention evaluation drops the LAST
distractor of the order, so the two conditions show the same number of
options. No confidence measure enters the order, and there is no rotation.
"""

from __future__ import annotations

from typing import Any

from .util import stable_id


DISTRACTOR_ORDER_CONTRACT_VERSION = "distractor-order-v1"
# The seed of an order that the generation path assigns at candidate time.
GENERATION_SEED_LABEL = "distractor-order-generation"
# The seed of an order that the evaluation set builder assigns to an older
# candidate that carries no order field.
ASSIGNED_SEED_LABEL = "distractor-order-assigned"


def order_seed(item_id: str, *, assigned: bool = False) -> str:
    """Return the recorded seed of one item's distractor order."""
    label = ASSIGNED_SEED_LABEL if assigned else GENERATION_SEED_LABEL
    return stable_id(label, item_id)


def ordered_texts(texts: list[str], seed: str) -> list[str]:
    """Return the texts in the fixed pseudo-random order that the seed defines."""
    if len(set(texts)) != len(texts):
        raise ValueError("distractor texts must be distinct before they are ordered")
    return sorted(texts, key=lambda text: stable_id("distractor-position", seed, text))


def distractor_order_record(
    item_id: str, distractors: list[dict[str, Any]], *, assigned: bool = False
) -> dict[str, Any]:
    """Return the persisted order record for one candidate's distractor list."""
    texts = [str(item["text"]) for item in distractors]
    seed = order_seed(item_id, assigned=assigned)
    return {
        "contract_version": DISTRACTOR_ORDER_CONTRACT_VERSION,
        "seed": seed,
        "assignment": "assigned_by_evaluation_set_builder"
        if assigned
        else "recorded_at_generation",
        "order": ordered_texts(texts, seed),
    }


def validate_order_record(record: Any, distractors: list[dict[str, Any]]) -> None:
    """Reject an order record that does not permute the candidate's distractors."""
    if not isinstance(record, dict):
        raise ValueError("the distractor order record is not an object")
    if record.get("contract_version") != DISTRACTOR_ORDER_CONTRACT_VERSION:
        raise ValueError("the distractor order contract version is unsupported")
    order = record.get("order")
    seed = record.get("seed")
    if not isinstance(order, list) or not isinstance(seed, str) or not seed:
        raise ValueError("the distractor order record is incomplete")
    texts = [str(item["text"]) for item in distractors]
    if sorted(order) != sorted(texts):
        raise ValueError("the distractor order does not permute the distractors")
    if order != ordered_texts(texts, seed):
        raise ValueError("the distractor order does not match its seed")


def resolve_order(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return the candidate's order record, or assign one for an older candidate."""
    distractors = [
        item for item in candidate.get("distractors") or [] if isinstance(item, dict)
    ]
    record = candidate.get("distractor_order")
    if record is None:
        return distractor_order_record(
            str(candidate["item_id"]), distractors, assigned=True
        )
    validate_order_record(record, distractors)
    return record


def apply_order(
    order: dict[str, Any], rows: list[dict[str, Any]], *, text_key: str = "text"
) -> list[dict[str, Any]]:
    """Return the rows sorted by the fixed order; a row outside the order is rejected."""
    position = {text: index for index, text in enumerate(order["order"])}
    missing = [row for row in rows if str(row.get(text_key)) not in position]
    if missing:
        raise ValueError("a distractor row is outside the recorded order")
    return sorted(rows, key=lambda row: position[str(row[text_key])])
