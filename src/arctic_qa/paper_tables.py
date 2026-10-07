"""Reproduce the paper's result tables from the released response file.

Input is ``data/arcticqa-v1/responses.jsonl`` alone. Run it as
``python -m arctic_qa.paper_tables``; it writes ``data/arcticqa-v1/results/``.
With ``--check`` it writes nothing and fails when a committed result file
differs from a new computation.

The statistics are the ones of the frozen analysis of 2026-09-17:

- Condition abstention rates pool the three trials. Invalid (N0) responses are
  outside every denominator.
- The shift is the answer-absent rate minus the answer-present rate. Its 95%
  interval is a percentile bootstrap of 10,000 resamples of questions, seed 7.
  A resample keeps both conditions and all three trials of a drawn question.
- The per-model test is a two-sided sign-flip permutation test over the
  per-question paired differences, 20,000 draws, seed 7. Holm's method
  corrects over the eight model tests.
- The metrics (ACC, abstention rate, F1_abs, R-Acc) come from
  ``abstention_score.metrics_from_counts``. They are written both pooled over
  the three trials and as the median of the three per-trial values.
"""

from __future__ import annotations

import argparse
import csv
import functools
import io
import json
import operator
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .abstention_render import N0, N1, N2, N3, N4, N5
from .abstention_score import METRIC_NAMES, metrics_from_counts, tally
from .paper_release import MODEL_LABELS, MODEL_ORDER

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "arcticqa-v1"
RESAMPLES = 10_000
PERMUTATIONS = 20_000
SEED = 7
LEVEL = 0.95
# A per-question difference has a small denominator, so its smallest true
# non-zero size is 1/48. A mean over models can carry a residue near 1e-17.
SIGN_TOLERANCE = 1e-12
PRESENT, ABSENT = "answer_present", "answer_absent"
COUNT_KEYS = (N0, N1, N2, N3, N4, N5)
PAPER_METRICS = ("acc", "abstention_rate", "f1_abs", "r_acc")


def load_responses(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "responses.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def present_abstention(c: dict[str, int]) -> float | None:
    total = c[N1] + c[N2] + c[N3]
    return c[N3] / total if total else None


def absent_abstention(c: dict[str, int]) -> float | None:
    total = c[N4] + c[N5]
    return c[N5] / total if total else None


def shift(c: dict[str, int]) -> float | None:
    present, absent = present_abstention(c), absent_abstention(c)
    return None if present is None or absent is None else absent - present


def accuracy_when_answering(c: dict[str, int]) -> float | None:
    answered = c[N1] + c[N2]
    return c[N1] / answered if answered else None


@functools.cache
def bootstrap_multiplicities(
    item_count: int, resamples: int, seed: int
) -> tuple[tuple[int, ...], ...]:
    """How often each question is drawn in each resample.

    Each resample makes one ``rng.choice`` per question over the sorted
    question ids, which is the draw of ``abstention_score.bootstrap_intervals``.
    The draws depend only on the number of questions and the seed, so every
    model and every statistic with the same questions shares them.
    """
    rng = random.Random(seed)
    indices = range(item_count)
    resampled = []
    for _ in range(resamples):
        drawn = [0] * item_count
        for _ in indices:
            drawn[rng.choice(indices)] += 1
        resampled.append(tuple(drawn))
    return tuple(resampled)


def bootstrap(
    rows: list[dict[str, Any]],
    statistics_by_name: dict[str, Callable[[dict[str, int]], float | None]],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> dict[str, dict[str, float] | None]:
    """Percentile intervals from a paired bootstrap of questions, one per statistic."""
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_item[row["item_id"]].append(row)
    item_ids = sorted(by_item)
    if not item_ids:
        return dict.fromkeys(statistics_by_name)
    item_counts = [tally(by_item[i]) for i in item_ids]
    columns = {key: [counts[key] for counts in item_counts] for key in COUNT_KEYS}
    values: dict[str, list[float]] = {name: [] for name in statistics_by_name}
    for drawn in bootstrap_multiplicities(len(item_ids), resamples, seed):
        counts = {
            key: sum(map(operator.mul, drawn, column))
            for key, column in columns.items()
        }
        for name, statistic in statistics_by_name.items():
            value = statistic(counts)
            if value is not None:
                values[name].append(value)
    low = (1 - LEVEL) / 2
    intervals: dict[str, dict[str, float] | None] = {}
    for name, sample in values.items():
        sample.sort()
        intervals[name] = (
            {
                "low": sample[int(low * (len(sample) - 1))],
                "high": sample[int((1 - low) * (len(sample) - 1))],
            }
            if sample
            else None
        )
    return intervals


@functools.cache
def sign_flips(
    item_count: int, permutations: int, seed: int
) -> tuple[tuple[float, ...], ...]:
    """The random signs of each permutation draw: one ``rng.random()`` per question."""
    rng = random.Random(seed)
    return tuple(
        tuple(1.0 if rng.random() < 0.5 else -1.0 for _ in range(item_count))
        for _ in range(permutations)
    )


def sign_flip_p(differences: list[float], *, permutations: int, seed: int) -> float:
    """Two-sided p-value of the mean of paired differences under sign flips.

    A sign of +1.0 or -1.0 multiplies a difference exactly, so each total is
    the same sum in the same order as ``d if rng.random() < 0.5 else -d``.
    """
    observed = sum(differences) / len(differences)
    extreme = 0
    for signs in sign_flips(len(differences), permutations, seed):
        total = sum(map(operator.mul, signs, differences))
        if abs(total / len(differences)) >= abs(observed) - 1e-12:
            extreme += 1
    return (extreme + 1) / (permutations + 1)


def per_question_shift(rows: list[dict[str, Any]]) -> float | None:
    present = [r for r in rows if r["condition"] == PRESENT]
    absent = [r for r in rows if r["condition"] == ABSENT]
    if not present or not absent:
        return None
    return sum(r["outcome"] == N5 for r in absent) / len(absent) - sum(
        r["outcome"] == N3 for r in present
    ) / len(present)


def describe(differences: list[float]) -> dict[str, int]:
    return {
        "questions_shift_positive": sum(d > SIGN_TOLERANCE for d in differences),
        "questions_shift_negative": sum(d < -SIGN_TOLERANCE for d in differences),
        "questions_shift_zero": sum(abs(d) <= SIGN_TOLERANCE for d in differences),
    }


def model_shift_test(
    rows: list[dict[str, Any]], *, permutations: int = PERMUTATIONS
) -> dict[str, Any]:
    """Sign-flip test of one model's per-question shift, questions in id order."""
    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["outcome"] != N0:
            by_item[row["item_id"]].append(row)
    differences = [
        d
        for item_id in sorted(by_item)
        if (d := per_question_shift(by_item[item_id])) is not None
    ]
    return {
        "questions": len(differences),
        "mean_per_question_shift": sum(differences) / len(differences),
        **describe(differences),
        "p_value": sign_flip_p(differences, permutations=permutations, seed=SEED),
    }


def pooled_shift_test(
    rows: list[dict[str, Any]], *, permutations: int = PERMUTATIONS
) -> dict[str, Any]:
    """One test over all models: each question gives its mean shift over the eight models."""
    by_item: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        if row["outcome"] != N0:
            by_item[row["item_id"]][row["model"]].append(row)
    differences = []
    for item_id in sorted(by_item):
        shifts = [
            d
            for model in MODEL_ORDER
            if (d := per_question_shift(by_item[item_id].get(model, []))) is not None
        ]
        if shifts:
            differences.append(sum(shifts) / len(shifts))
    return {
        "questions": len(differences),
        "mean_shift_over_models": sum(differences) / len(differences),
        **describe(differences),
        "p_value": sign_flip_p(differences, permutations=permutations, seed=SEED),
        "permutations": permutations,
        "seed": SEED,
    }


def holm(p_values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(p_values.items(), key=lambda kv: kv[1])
    adjusted, running = {}, 0.0
    for index, (name, value) in enumerate(ordered):
        running = max(running, min(1.0, (len(ordered) - index) * value))
        adjusted[name] = running
    return adjusted


def fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def compute(
    rows: list[dict[str, Any]], *, resamples: int = RESAMPLES
) -> dict[str, Any]:
    """Return every result table as rows of strings, plus the summary."""
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_model[row["model"]].append(row)
    condition_rows, metric_rows, trial_rows, count_rows = [], [], [], []
    tests = {}
    for model in MODEL_ORDER:
        model_rows = by_model[model]
        valid = [r for r in model_rows if r["outcome"] != N0]
        counts = tally(model_rows)
        intervals = bootstrap(
            valid,
            {
                "present": present_abstention,
                "absent": absent_abstention,
                "shift": shift,
                "accuracy": accuracy_when_answering,
            },
            resamples=resamples,
        )
        tests[model] = model_shift_test(model_rows)
        condition_rows.append(
            {
                "model_id": model,
                "model": MODEL_LABELS[model],
                "questions": len({r["item_id"] for r in model_rows}),
                "present_valid_responses": counts[N1] + counts[N2] + counts[N3],
                "absent_valid_responses": counts[N4] + counts[N5],
                "present_abstention": fmt(present_abstention(counts)),
                "present_abstention_ci_low": fmt(intervals["present"]["low"]),
                "present_abstention_ci_high": fmt(intervals["present"]["high"]),
                "absent_abstention": fmt(absent_abstention(counts)),
                "absent_abstention_ci_low": fmt(intervals["absent"]["low"]),
                "absent_abstention_ci_high": fmt(intervals["absent"]["high"]),
                "shift": fmt(shift(counts)),
                "shift_ci_low": fmt(intervals["shift"]["low"]),
                "shift_ci_high": fmt(intervals["shift"]["high"]),
                "present_accuracy_when_answering": fmt(accuracy_when_answering(counts)),
                "present_accuracy_ci_low": fmt(intervals["accuracy"]["low"]),
                "present_accuracy_ci_high": fmt(intervals["accuracy"]["high"]),
            }
        )
        count_rows.append(
            {
                "model_id": model,
                "model": MODEL_LABELS[model],
                "responses": len(model_rows),
                **counts,
            }
        )
        per_trial = []
        for trial in (1, 2, 3):
            trial_counts = tally([r for r in model_rows if r["trial"] == trial])
            metrics = metrics_from_counts(trial_counts)
            per_trial.append(metrics)
            trial_rows.append(
                {
                    "model_id": model,
                    "model": MODEL_LABELS[model],
                    "trial": trial,
                    **trial_counts,
                    **{name: fmt(metrics[name]) for name in METRIC_NAMES},
                }
            )
        pooled = metrics_from_counts(counts)
        metric_rows.append(
            {
                "model_id": model,
                "model": MODEL_LABELS[model],
                **{f"{name}_pooled": fmt(pooled[name]) for name in PAPER_METRICS},
                **{
                    f"{name}_trial_median": fmt(
                        statistics.median(m[name] for m in per_trial)
                    )
                    for name in PAPER_METRICS
                },
            }
        )
    adjusted = holm({model: test["p_value"] for model, test in tests.items()})
    for row in condition_rows:
        test = tests[row["model_id"]]
        row["permutation_p"] = fmt(test["p_value"])
        row["holm_p"] = fmt(adjusted[row["model_id"]])
    pooled_test = pooled_shift_test(rows)
    model_shifts = [float(row["shift"]) for row in condition_rows]
    summary = {
        "source": "data/arcticqa-v1/responses.jsonl",
        "questions": len({r["item_id"] for r in rows}),
        "responses": len(rows),
        "valid_responses": sum(r["outcome"] != N0 for r in rows),
        "invalid_responses": sum(r["outcome"] == N0 for r in rows),
        "bootstrap": {
            "resamples": resamples,
            "seed": SEED,
            "level": LEVEL,
            "unit": "question",
        },
        "permutation": {
            "draws": PERMUTATIONS,
            "seed": SEED,
            "sides": 2,
            "correction": "Holm",
        },
        "per_model_shift_test": {
            model: {**test, "holm_p": adjusted[model]} for model, test in tests.items()
        },
        "pooled_shift_test": pooled_test,
        "mean_of_model_shifts": sum(model_shifts) / len(model_shifts),
        "present_abstention_range": [
            min(float(r["present_abstention"]) for r in condition_rows),
            max(float(r["present_abstention"]) for r in condition_rows),
        ],
    }
    return {
        "condition-shifts.csv": condition_rows,
        "metrics.csv": metric_rows,
        "trial-metrics.csv": trial_rows,
        "outcome-counts.csv": count_rows,
        "summary": summary,
    }


def csv_text(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def pct(value: str) -> str:
    return f"{100 * float(value):.1f}"


def tables_markdown(result: dict[str, Any]) -> str:
    """Render the two paper tables at the paper's precision."""
    summary = result["summary"]
    lines = [
        "# Paper tables, reproduced",
        "",
        "Generated by `python -m arctic_qa.paper_tables` from `responses.jsonl`. Do not edit by hand.",
        "",
        f"Questions: {summary['questions']}. Responses: {summary['responses']} "
        f"({summary['valid_responses']} valid, {summary['invalid_responses']} invalid).",
        "",
        "## Condition-specific abstention and the paired shift",
        "",
        "Rates pool the three trials. Shift = answer-absent minus answer-present, in percentage points.",
        "",
        "| Model | Present abst. | Absent abst. | Shift [95% CI] (pp) | Permutation p | Holm p |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result["condition-shifts.csv"]:
        lines.append(
            f"| {row['model']} | {pct(row['present_abstention'])}% | {pct(row['absent_abstention'])}% "
            f"| {100 * float(row['shift']):+.2f} [{pct(row['shift_ci_low'])}, {pct(row['shift_ci_high'])}] "
            f"| {float(row['permutation_p']):.5f} | {float(row['holm_p']):.4f} |"
        )
    pooled = summary["pooled_shift_test"]
    lines += [
        "",
        f"Mean per-question shift over the eight models: {100 * pooled['mean_shift_over_models']:+.2f} pp "
        f"(one sign-flip test with the question as the unit, p = {pooled['p_value']:.1e}; "
        f"{pooled['questions_shift_positive']} questions up, {pooled['questions_shift_negative']} down, "
        f"{pooled['questions_shift_zero']} unchanged).",
        f"Mean of the eight pooled model shifts: {100 * summary['mean_of_model_shifts']:+.2f} pp.",
        "",
        "## Metrics",
        "",
        "Pooled over the three trials, and the median of the three per-trial values.",
        "",
        "| Model | ACC | Abst. rate | F1_abs | R-Acc | ACC (median) | Abst. rate (median) "
        "| F1_abs (median) | R-Acc (median) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result["metrics.csv"]:
        pooled_cells = " | ".join(
            f"{float(row[f'{m}_pooled']):.3f}" for m in PAPER_METRICS
        )
        median_cells = " | ".join(
            f"{float(row[f'{m}_trial_median']):.3f}" for m in PAPER_METRICS
        )
        lines.append(f"| {row['model']} | {pooled_cells} | {median_cells} |")
    lines.append("")
    return "\n".join(lines)


def render_files(result: dict[str, Any]) -> dict[str, str]:
    files = {
        name: csv_text(rows) for name, rows in result.items() if name.endswith(".csv")
    }
    files["summary.json"] = json.dumps(result["summary"], indent=2) + "\n"
    files["TABLES.md"] = tables_markdown(result)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--out-dir", type=Path, default=None, help="default: <data-dir>/results"
    )
    parser.add_argument(
        "--check", action="store_true", help="compare with the committed files"
    )
    args = parser.parse_args(argv)
    out_dir = args.out_dir or args.data_dir / "results"
    files = render_files(compute(load_responses(args.data_dir)))
    if args.check:
        stale = [
            n
            for n, t in files.items()
            if not (out_dir / n).exists() or (out_dir / n).read_text() != t
        ]
        for name in stale:
            print(f"differs: {out_dir / name}")
        print("results match" if not stale else f"{len(stale)} result files differ")
        return 1 if stale else 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
        print(f"wrote {out_dir / name}")
    print(files["TABLES.md"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
