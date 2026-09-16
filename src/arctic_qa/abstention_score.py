"""Score one abstention evaluation run.

Reproduces the previous paper's evaluation (Original Paper, evaluation
metrics): the N1 to N5 taxonomy, ACC, Precision_abs, Recall_abs, F1_abs,
Abstention Rate, R-Acc and SSR, the uniform random baseline, and a paired
item bootstrap for confidence intervals. Invalid responses (N0) are counted,
reported per model, and excluded from the metrics. There is no re-ask.

Repeats are within-item measurements. Every item contributes the same number
of trials per condition, so pooled counts equal the mean over repeats; the
bootstrap resamples items, never trials, so repeats never inflate n.
"""

from __future__ import annotations

import csv
import json
import random
from fractions import Fraction
from pathlib import Path
from typing import Any

from .abstention_render import GOLD_ABSENT, GOLD_PRESENT, N0, N1, N2, N3, N4, N5
from .abstention_set import role_of_model
from .util import atomic_json


COUNTS = (N0, N1, N2, N3, N4, N5)
METRIC_NAMES = (
    "acc",
    "precision_abs",
    "recall_abs",
    "f1_abs",
    "abstention_rate",
    "r_acc",
    "ssr",
)
SECONDARY_METRIC_NAMES = ("recall_abs_absent_only", "ideal_pair_rate")
METRIC_LABELS = {
    "acc": "ACC",
    "precision_abs": "Precision_abs",
    "recall_abs": "Recall_abs",
    "f1_abs": "F1_abs",
    "abstention_rate": "Abstention Rate",
    "r_acc": "R-Acc",
    "ssr": "SSR",
    "recall_abs_absent_only": "Recall_abs (absent only)",
    "ideal_pair_rate": "Ideal pair rate",
}
METRIC_LATEX = {
    "acc": r"ACC",
    "precision_abs": r"$\text{Precision}_{\text{abs}}$",
    "recall_abs": r"$\text{Recall}_{\text{abs}}$",
    "f1_abs": r"$\text{F1}_{\text{abs}}$",
    "abstention_rate": r"Abst. Rate",
    "r_acc": r"R-Acc",
    "ssr": r"SSR",
}


def _ratio(numerator: Any, denominator: Any) -> float | None:
    return None if not denominator else float(numerator) / float(denominator)


def metrics_from_counts(counts: dict[str, Any]) -> dict[str, float | None]:
    """The seven paper metrics plus the narrow recall, from N1 to N5 counts."""
    n1, n2, n3, n4, n5 = (counts[name] for name in (N1, N2, N3, N4, N5))
    total = n1 + n2 + n3 + n4 + n5
    precision = _ratio(n5, n3 + n5)
    recall = _ratio(n5, n2 + n4 + n5)
    f1 = (
        None
        if precision is None or recall is None or precision + recall == 0
        else 2 * precision * recall / (precision + recall)
    )
    return {
        "acc": _ratio(n1 + n5, total),
        "precision_abs": precision,
        "recall_abs": recall,
        "f1_abs": f1,
        "abstention_rate": _ratio(n3 + n5, total),
        "r_acc": _ratio(n1, n1 + n2 + n4),
        "ssr": _ratio(n1 + n3 + n5, total),
        "recall_abs_absent_only": _ratio(n5, n4 + n5),
    }


def tally(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in COUNTS}
    for row in rows:
        counts[row["outcome"]] += 1
    return counts


def uniform_baseline(gold_present_options: int, gold_absent_options: int) -> dict[str, Any]:
    """Expected counts and metrics of a uniform guess over the displayed options.

    One item per condition. Gold-present with n options: N1 = 1/n, N3 = 1/n,
    N2 = (n-2)/n. Gold-absent with m options: N5 = 1/m, N4 = (m-1)/m.
    """
    n = Fraction(1, gold_present_options)
    m = Fraction(1, gold_absent_options)
    counts = {
        N0: 0,
        N1: n,
        N2: 1 - 2 * n,
        N3: n,
        N4: 1 - m,
        N5: m,
    }
    return {
        "gold_present_options": gold_present_options,
        "gold_absent_options": gold_absent_options,
        "expected_counts_per_item": {name: float(value) for name, value in counts.items()},
        "metrics": metrics_from_counts(counts),
    }


def random_baseline(k: int) -> dict[str, Any]:
    """The design's baseline: k content options plus abstain in both conditions."""
    return uniform_baseline(k + 1, k + 1)


def ideal_pair_rate(rows: list[dict[str, Any]]) -> float | None:
    """Share of (item, repeat) pairs that answered gold in GP and abstained in GA."""
    pairs: dict[tuple[str, int], dict[str, str]] = {}
    for row in rows:
        pairs.setdefault((row["item_id"], row["repeat"]), {})[row["condition"]] = row[
            "outcome"
        ]
    complete = [
        pair
        for pair in pairs.values()
        if GOLD_PRESENT in pair and GOLD_ABSENT in pair
        and pair[GOLD_PRESENT] != N0
        and pair[GOLD_ABSENT] != N0
    ]
    if not complete:
        return None
    ideal = sum(
        1 for pair in complete if pair[GOLD_PRESENT] == N1 and pair[GOLD_ABSENT] == N5
    )
    return ideal / len(complete)


def letter_distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Positional bias check: share of each chosen letter and of the abstain letter."""
    valid = [row for row in rows if row["valid"]]
    letters: dict[str, int] = {}
    for row in valid:
        letters[row["parsed_letter"]] = letters.get(row["parsed_letter"], 0) + 1
    abstain_positions: dict[str, int] = {}
    for row in rows:
        abstain_positions[row["abstain_letter"]] = (
            abstain_positions.get(row["abstain_letter"], 0) + 1
        )
    return {
        "valid_responses": len(valid),
        "chosen_letter_counts": dict(sorted(letters.items())),
        "chosen_letter_share": {
            letter: round(count / len(valid), 4) for letter, count in sorted(letters.items())
        }
        if valid
        else {},
        "abstain_letter_positions": dict(sorted(abstain_positions.items())),
    }


def _group(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["model"], row["arm"]), []).append(row)
    return dict(sorted(groups.items()))


def bootstrap_intervals(
    rows: list[dict[str, Any]], *, resamples: int, seed: int, level: float = 0.95
) -> dict[str, Any]:
    """Percentile intervals from a paired item bootstrap (items resampled, trials kept)."""
    by_item: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_item.setdefault(row["item_id"], []).append(row)
    item_ids = sorted(by_item)
    if not item_ids or resamples < 1:
        return {name: None for name in (*METRIC_NAMES, *SECONDARY_METRIC_NAMES)}
    item_counts = {item_id: tally(by_item[item_id]) for item_id in item_ids}
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {
        name: [] for name in (*METRIC_NAMES, *SECONDARY_METRIC_NAMES)
    }
    for _ in range(resamples):
        drawn = [rng.choice(item_ids) for _ in item_ids]
        counts = {name: 0 for name in COUNTS}
        drawn_rows: list[dict[str, Any]] = []
        for item_id in drawn:
            for name in COUNTS:
                counts[name] += item_counts[item_id][name]
            drawn_rows.extend(by_item[item_id])
        values = metrics_from_counts(counts)
        values["ideal_pair_rate"] = ideal_pair_rate(drawn_rows)
        for name, value in values.items():
            if value is not None:
                samples[name].append(value)
    lower = (1 - level) / 2
    result: dict[str, Any] = {}
    for name, values in samples.items():
        if not values:
            result[name] = None
            continue
        ordered = sorted(values)
        low = ordered[int(lower * (len(ordered) - 1))]
        high = ordered[int((1 - lower) * (len(ordered) - 1))]
        result[name] = {"low": low, "high": high, "samples": len(values)}
    return result


def score_group(
    rows: list[dict[str, Any]], *, resamples: int, seed: int
) -> dict[str, Any]:
    counts = tally(rows)
    valid_rows = [row for row in rows if row["outcome"] != N0]
    metrics = metrics_from_counts(counts)
    metrics["ideal_pair_rate"] = ideal_pair_rate(rows)
    # Sensitivity row (design D9): invalid responses counted as abstention.
    sensitivity = dict(counts)
    for row in rows:
        if row["outcome"] == N0:
            sensitivity[N3 if row["condition"] == GOLD_PRESENT else N5] += 1
    sensitivity[N0] = 0
    per_condition = {
        GOLD_PRESENT: {
            "trials": sum(1 for row in rows if row["condition"] == GOLD_PRESENT),
            "gold_rate": _ratio(counts[N1], counts[N1] + counts[N2] + counts[N3]),
            "false_abstention_rate": _ratio(
                counts[N3], counts[N1] + counts[N2] + counts[N3]
            ),
        },
        GOLD_ABSENT: {
            "trials": sum(1 for row in rows if row["condition"] == GOLD_ABSENT),
            "abstention_rate": _ratio(counts[N5], counts[N4] + counts[N5]),
            "false_commitment_rate": _ratio(counts[N4], counts[N4] + counts[N5]),
        },
    }
    invalid_reasons: dict[str, int] = {}
    for row in rows:
        if row["outcome"] == N0:
            reason = str(row["invalid_reason"])
            invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
    return {
        "trials": len(rows),
        "items": len({row["item_id"] for row in rows}),
        "counts": counts,
        "invalid_rate": _ratio(counts[N0], len(rows)),
        "invalid_reasons": invalid_reasons,
        "metrics": metrics,
        "intervals": bootstrap_intervals(valid_rows, resamples=resamples, seed=seed),
        "sensitivity_invalid_as_abstention": {
            "counts": sensitivity,
            "metrics": metrics_from_counts(sensitivity),
        },
        "per_condition": per_condition,
        "letters": letter_distribution(rows),
    }


def contamination_table(
    rows: list[dict[str, Any]], items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per model and arm: construction role counts and metrics with/without them."""
    roles_by_item = {item["item_id"]: item["construction_roles"] for item in items}
    table = []
    for (model, arm), group in _group(rows).items():
        role_counts: dict[str, int] = {}
        for item_id in {row["item_id"] for row in group}:
            role = role_of_model(roles_by_item.get(item_id, {}), model)
            role_counts[role] = role_counts.get(role, 0) + 1
        clean = [
            row
            for row in group
            if role_of_model(roles_by_item.get(row["item_id"], {}), model) == "none"
        ]
        table.append(
            {
                "model": model,
                "arm": arm,
                "items_by_role": dict(sorted(role_counts.items())),
                "items_without_role": len({row["item_id"] for row in clean}),
                "metrics_all_items": metrics_from_counts(tally(group)),
                "metrics_without_role": metrics_from_counts(tally(clean)) if clean else None,
            }
        )
    return table


def strata_table(
    rows: list[dict[str, Any]], items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Metrics per model, arm and item stratum (numeric, context, script, contract)."""
    strata_by_item = {item["item_id"]: item["strata"] for item in items}
    table = []
    for (model, arm), group in _group(rows).items():
        for stratum in ("numeric", "has_context", "script", "schema_version"):
            buckets: dict[str, list[dict[str, Any]]] = {}
            for row in group:
                value = strata_by_item.get(row["item_id"], {}).get(stratum)
                buckets.setdefault(str(value), []).append(row)
            for value, bucket in sorted(buckets.items()):
                table.append(
                    {
                        "model": model,
                        "arm": arm,
                        "stratum": stratum,
                        "value": value,
                        "items": len({row["item_id"] for row in bucket}),
                        "trials": len(bucket),
                        "counts": tally(bucket),
                        "metrics": metrics_from_counts(tally(bucket)),
                    }
                )
    return table


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _latex_escape(value: str) -> str:
    return value.replace("_", r"\_").replace("&", r"\&").replace("%", r"\%")


def main_table_rows(scores: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, group in scores["groups"].items():
        model, arm = key.split("/", 1)
        row: dict[str, Any] = {
            "model": model,
            "arm": arm,
            "items": group["items"],
            "trials": group["trials"],
            **{name: group["counts"][name] for name in COUNTS},
            "invalid_rate": _fmt(group["invalid_rate"]),
        }
        for name in METRIC_NAMES:
            row[name] = _fmt(group["metrics"][name])
            interval = group["intervals"].get(name)
            row[f"{name}_ci_low"] = _fmt(interval["low"]) if interval else "n/a"
            row[f"{name}_ci_high"] = _fmt(interval["high"]) if interval else "n/a"
        row["recall_abs_absent_only"] = _fmt(group["metrics"]["recall_abs_absent_only"])
        row["ideal_pair_rate"] = _fmt(group["metrics"]["ideal_pair_rate"])
        rows.append(row)
    baseline = scores["random_baseline"]
    rows.append(
        {
            "model": "Random baseline",
            "arm": f"uniform over {baseline['gold_present_options']} options",
            "items": "",
            "trials": "",
            **{name: "" for name in COUNTS},
            "invalid_rate": "",
            **{name: _fmt(baseline["metrics"][name]) for name in METRIC_NAMES},
            **{f"{name}_ci_low": "" for name in METRIC_NAMES},
            **{f"{name}_ci_high": "" for name in METRIC_NAMES},
            "recall_abs_absent_only": _fmt(baseline["metrics"]["recall_abs_absent_only"]),
            "ideal_pair_rate": "",
        }
    )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for name in row:
            if name not in fields:
                fields.append(name)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fields})


def main_table_latex(scores: dict[str, Any]) -> str:
    """A booktabs table in the previous paper's layout, with 95% intervals."""
    header = (
        r"\begin{tabular}{llrrrrrr" + "c" * len(METRIC_NAMES) + "}\n"
        r"\toprule" "\n"
        r"\textbf{Model} & \textbf{Arm} & $N_0$ & $N_1$ & $N_2$ & $N_3$ & $N_4$ & $N_5$ & "
        + " & ".join(r"\textbf{" + METRIC_LATEX[name] + "}" for name in METRIC_NAMES)
        + r" \\" "\n" r"\midrule" "\n"
    )
    lines = []
    for key, group in scores["groups"].items():
        model, arm = key.split("/", 1)
        cells = [
            r"\textbf{" + _latex_escape(model) + "}",
            _latex_escape(arm),
            *[str(group["counts"][name]) for name in COUNTS],
        ]
        for name in METRIC_NAMES:
            interval = group["intervals"].get(name)
            point = _fmt(group["metrics"][name])
            if interval:
                point += (
                    r" {\scriptsize[" + _fmt(interval["low"]) + ", " + _fmt(interval["high"]) + "]}"
                )
            cells.append(point)
        lines.append(" & ".join(cells) + r" \\")
    baseline = scores["random_baseline"]
    lines.append(r"\midrule")
    lines.append(
        " & ".join(
            [
                r"\textbf{Random Baseline}",
                f"uniform, {baseline['gold_present_options']} options",
                *["--"] * len(COUNTS),
                *[_fmt(baseline["metrics"][name]) for name in METRIC_NAMES],
            ]
        )
        + r" \\"
    )
    caption = (
        r"\caption{ArcticQA abstention results. Counts follow the response taxonomy; "
        r"$N_0$ (invalid responses) is excluded from every metric. Brackets hold "
        r"95\% percentile intervals from a paired item bootstrap.}"
    )
    return header + "\n".join(lines) + "\n" + r"\bottomrule" + "\n" + r"\end{tabular}" + "\n" + caption + "\n"


def contamination_latex(table: list[dict[str, Any]]) -> str:
    lines = [
        r"\begin{tabular}{llrrrrcc}",
        r"\toprule",
        r"\textbf{Model} & \textbf{Arm} & writer & judge & both & none & SSR (all) & SSR (no role) \\",
        r"\midrule",
    ]
    for row in table:
        roles = row["items_by_role"]
        lines.append(
            " & ".join(
                [
                    r"\textbf{" + _latex_escape(row["model"]) + "}",
                    _latex_escape(row["arm"]),
                    str(roles.get("writer", 0)),
                    str(roles.get("judge", 0)),
                    str(roles.get("writer_and_judge", 0)),
                    str(roles.get("none", 0)),
                    _fmt(row["metrics_all_items"]["ssr"]),
                    _fmt((row["metrics_without_role"] or {}).get("ssr")),
                ]
            )
            + r" \\"
        )
    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        r"\caption{Construction roles of each evaluated model over the evaluated items "
        r"(which model wrote or judged each item), with SSR over all items and over the "
        r"items in which the model had no construction role.}",
    ]
    return "\n".join(lines) + "\n"


def score_run(
    *,
    responses_file: Path,
    items: list[dict[str, Any]],
    k: int,
    output_dir: Path,
    resamples: int = 2000,
    seed: int = 7,
    run_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one responses file and write the paper-ready tables."""
    rows = [
        json.loads(line)
        for line in responses_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    groups = {
        f"{model}/{arm}": score_group(group, resamples=resamples, seed=seed)
        for (model, arm), group in _group(rows).items()
    }
    scores = {
        "schema": "abstention-eval-scores-v1",
        "responses_file": str(responses_file),
        "run_id": (run_manifest or {}).get("run_id"),
        "eval_set_id": (run_manifest or {}).get("eval_set_id"),
        "k": k,
        "displayed_options": k + 1,
        "trials": len(rows),
        "bootstrap": {"resamples": resamples, "seed": seed, "unit": "item", "level": 0.95},
        "taxonomy": {
            N0: "invalid response (excluded from metrics)",
            N1: "gold present, gold chosen",
            N2: "gold present, distractor chosen",
            N3: "gold present, abstained",
            N4: "gold absent, distractor chosen",
            N5: "gold absent, abstained",
        },
        "random_baseline": random_baseline(k),
        "always_abstain_baseline": {
            "metrics": metrics_from_counts({N0: 0, N1: 0, N2: 0, N3: 1, N4: 0, N5: 1})
        },
        "groups": groups,
        "contamination": contamination_table(rows, items),
        "strata": strata_table(rows, items),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(output_dir / "scores.json", scores)
    write_csv(output_dir / "main-table.csv", main_table_rows(scores))
    (output_dir / "main-table.tex").write_text(main_table_latex(scores), encoding="utf-8")
    write_csv(
        output_dir / "condition-table.csv",
        [
            {
                "model": key.split("/", 1)[0],
                "arm": key.split("/", 1)[1],
                "gp_trials": group["per_condition"][GOLD_PRESENT]["trials"],
                "gp_gold_rate": _fmt(group["per_condition"][GOLD_PRESENT]["gold_rate"]),
                "gp_false_abstention_rate": _fmt(
                    group["per_condition"][GOLD_PRESENT]["false_abstention_rate"]
                ),
                "ga_trials": group["per_condition"][GOLD_ABSENT]["trials"],
                "ga_abstention_rate": _fmt(
                    group["per_condition"][GOLD_ABSENT]["abstention_rate"]
                ),
                "ga_false_commitment_rate": _fmt(
                    group["per_condition"][GOLD_ABSENT]["false_commitment_rate"]
                ),
                "invalid_rate": _fmt(group["invalid_rate"]),
            }
            for key, group in groups.items()
        ],
    )
    write_csv(
        output_dir / "letters.csv",
        [
            {
                "model": key.split("/", 1)[0],
                "arm": key.split("/", 1)[1],
                **{
                    f"chosen_{letter}": share
                    for letter, share in group["letters"]["chosen_letter_share"].items()
                },
            }
            for key, group in groups.items()
        ],
    )
    write_csv(
        output_dir / "contamination.csv",
        [
            {
                "model": row["model"],
                "arm": row["arm"],
                **{
                    f"items_{role}": row["items_by_role"].get(role, 0)
                    for role in ("writer", "judge", "writer_and_judge", "none")
                },
                "ssr_all_items": _fmt(row["metrics_all_items"]["ssr"]),
                "ssr_without_role": _fmt((row["metrics_without_role"] or {}).get("ssr")),
                "acc_all_items": _fmt(row["metrics_all_items"]["acc"]),
                "acc_without_role": _fmt((row["metrics_without_role"] or {}).get("acc")),
            }
            for row in scores["contamination"]
        ],
    )
    (output_dir / "contamination.tex").write_text(
        contamination_latex(scores["contamination"]), encoding="utf-8"
    )
    write_csv(
        output_dir / "strata.csv",
        [
            {
                "model": row["model"],
                "arm": row["arm"],
                "stratum": row["stratum"],
                "value": row["value"],
                "items": row["items"],
                "trials": row["trials"],
                **{name: _fmt(row["metrics"][name]) for name in METRIC_NAMES},
            }
            for row in scores["strata"]
        ],
    )
    return scores
