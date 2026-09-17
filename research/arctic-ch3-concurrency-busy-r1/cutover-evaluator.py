#!/usr/bin/env python
"""Move the streaming abstention evaluator onto the ledger-session code.

The evaluator shares the paid-call ledger, and its lock file, with the chapter
3 producer. On the snapshot it runs it proves 5,393 ledger rows against 21,508
receipt files on every ledger read, so it holds the shared ledger lock for
seconds at a time: a median of 2.35 s and a maximum of 6.56 s, measured on
2026-09-17 while both ran. The producer waits for exactly that, which is what
is left of its admission.

This script moves the evaluator to a snapshot of this branch, where the same
proof is kept per row and skipped while nothing changed. It changes no bound,
no plan, no policy and no price: the five files the authorization binds are
byte-identical between the running commit and this one, and the script refuses
unless they are.

    cutover-evaluator.py <commit>            # check and print, change nothing
    cutover-evaluator.py <commit> --apply    # do it

The successor authorization names the predecessor exactly as a successor
execution gate of the producer names the gate it follows. It carries no new
bound: the same campaign, the same run-id prefix, the same item and USD bounds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

FM_DATA = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data")
EVAL_DATA = FM_DATA / "arctic-eval-authorization-r8"
WORKTREE = Path("/home/ben/.treehouse/arctic-qa-e841b2/33/arctic-qa")
LEDGER = Path(
    "/mnt/crdata/research-abstention/arctic-qa/streaming-dataset-r1"
    "/shared-paid-call-ledger.json"
)
UNIT = "arctic-abstention-stream-r3"
PREDECESSOR_COMMIT = "5535f28"
AUTHORIZATION = EVAL_DATA / "streaming-eval-r11-authorization.json"
LAUNCHER = EVAL_DATA / "streaming-eval-r11-launcher.sh"
BOUND_CONFIGS = (
    "config/benchmark-evaluation-plan-high-v1.json",
    "config/live-dataset-current-contract-v1.json",
    "config/benchmark-evaluation-policy-v3.json",
    "config/benchmark-evaluation-prices-v1.json",
    "config/benchmark-evaluation-subscription-models-v1.json",
)


def sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(command)}\n{result.stderr}")
    return result


def evaluator_inflight(ledger: dict) -> int:
    """Count the evaluator's own paid calls that are on the wire."""
    return sum(
        1
        for request in ledger.get("requests", {}).values()
        if request.get("state") == "submitted"
        and request.get("phase") == "benchmark_evaluation"
    )


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: cutover-evaluator.py <commit> [--apply]")
    apply = "--apply" in sys.argv
    commit = run(["git", "-C", str(WORKTREE), "rev-parse", sys.argv[1]]).stdout.strip()
    short = commit[:7]

    # Every bound the authorization carries must be the bound it already has.
    for name in BOUND_CONFIGS:
        changed = run(
            ["git", "-C", str(WORKTREE), "diff", "--name-only",
             PREDECESSOR_COMMIT, commit, "--", name]
        ).stdout.strip()
        if changed:
            raise SystemExit(f"a bound configuration changed: {name}")

    runtime = EVAL_DATA / "runtime" / f"app-{short}-arctic-eval-authorization-r8"
    archive = EVAL_DATA / f"source-{short}-arctic-eval-authorization-r8.tar"
    review = EVAL_DATA / f"streaming-eval-r11-review-{short}.md"
    successor = EVAL_DATA / f"streaming-eval-r11-authorization-{short}.json"
    launcher = EVAL_DATA / f"streaming-eval-r11-launcher-{short}.sh"
    prior = json.loads(AUTHORIZATION.read_text(encoding="utf-8"))
    if prior["integrated_code_commit"] != PREDECESSOR_COMMIT:
        raise SystemExit("the live authorization is not the one this succeeds")

    plan = {
        "commit": commit,
        "runtime": str(runtime),
        "authorization": str(successor),
        "launcher": str(launcher),
        "predecessor_authorization": str(AUTHORIZATION),
        "predecessor_commit": PREDECESSOR_COMMIT,
        "apply": apply,
    }
    if not apply:
        print(json.dumps(plan, indent=2))
        return 0

    if not archive.exists():
        run(["git", "-C", str(WORKTREE), "archive", "--format=tar",
             "-o", str(archive), commit])
        os.chmod(archive, 0o444)
    if not (runtime / "flake.nix").exists():
        runtime.mkdir(parents=True, exist_ok=True)
        run(["tar", "-xf", str(archive), "-C", str(runtime)])

    review.write_text(
        "\n".join(
            [
                f"# Streaming abstention evaluator, snapshot {short}",
                "",
                f"Successor of `{AUTHORIZATION}` (commit {PREDECESSOR_COMMIT}).",
                "Same campaign, same run-id prefix, same work directory, same plan,",
                "policy, prices, contract and subscription models: the five files the",
                "authorization binds are byte-identical between the two commits, and",
                "the cutover refuses otherwise. No new bound and no new spend.",
                "",
                "## Why the evaluator moves",
                "",
                "The evaluator shares the paid-call ledger and its lock with the",
                "chapter 3 producer. On the predecessor snapshot it proves every",
                "ledger row against every receipt on each ledger read, which at 5,393",
                "rows and 21,508 receipts takes about 2.4 seconds under the shared",
                "lock. Measured while both ran on 2026-09-17, the producer waited a",
                "median of 2.35 seconds and up to 6.56 seconds for that lock, three",
                "times a paid call.",
                "",
                "On this snapshot the proof is kept per ledger row and replayed only",
                "for a row that moved, a full pass still runs on construction and",
                "every five minutes, and the ledger's own consistency is still proved",
                "on every read. The evaluator therefore holds the shared lock for a",
                "fraction of the time and stops starving the producer.",
                "",
                "## Scope of the code change",
                "",
                f"`research/arctic-ch3-concurrency-busy-r1/report.md` at {short}.",
                "The evaluation path itself is unchanged: no plan, prompt, model,",
                "gate, price or bound moves.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    record = dict(prior)
    record["integrated_code_commit"] = short
    record["review_record"] = str(review.resolve())
    record["review_record_sha256"] = sha256_file(review)
    record["written_at_utc"] = now()
    record["supersedes"] = {
        "authorization_file": str(AUTHORIZATION),
        "authorization_sha256": sha256_file(AUTHORIZATION),
        "integrated_code_commit": PREDECESSOR_COMMIT,
        "review_record": prior["review_record"],
        "review_record_sha256": prior["review_record_sha256"],
    }
    successor.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    text = LAUNCHER.read_text(encoding="utf-8")
    text = text.replace(
        f"app-{PREDECESSOR_COMMIT}-arctic-eval-authorization-r8",
        f"app-{short}-arctic-eval-authorization-r8",
    )
    text = text.replace(str(AUTHORIZATION), str(successor))
    text = text.replace(f"--code-commit {PREDECESSOR_COMMIT}", f"--code-commit {short}")
    launcher.write_text(text, encoding="utf-8")
    os.chmod(launcher, 0o555)

    # The successor must validate before the unit is touched.
    check = subprocess.run(
        ["nix", "develop", f"path:{runtime}", "-c", "env",
         f"PYTHONPATH={runtime / 'src'}", "python", "-c",
         "import json,sys\n"
         "from pathlib import Path\n"
         "from arctic_qa.abstention_watch import validate_authorization\n"
         f"value = validate_authorization(Path({str(successor)!r}),\n"
         f"  plan_file=Path({str(runtime / BOUND_CONFIGS[0])!r}),\n"
         f"  contract_file=Path({str(runtime / BOUND_CONFIGS[1])!r}),\n"
         f"  evaluation_policy_file=Path({str(runtime / BOUND_CONFIGS[2])!r}),\n"
         f"  evaluation_price_config_file=Path({str(runtime / BOUND_CONFIGS[3])!r}),\n"
         f"  subscription_models_file=Path({str(runtime / BOUND_CONFIGS[4])!r}),\n"
         f"  code_commit={short!r})\n"
         "print(json.dumps({'integrated_code_commit': "
         "value['integrated_code_commit'], 'enabled': "
         "value['authorization_enabled']}))\n"],
        capture_output=True, text=True, cwd=str(runtime),
    )
    if check.returncode != 0:
        raise SystemExit("the successor authorization does not validate:\n"
                         + check.stderr[-4000:])
    plan["authorization_check"] = json.loads(check.stdout.strip().splitlines()[-1])

    # Stop at a boundary with none of the evaluator's own calls on the wire.
    for _ in range(600):
        if evaluator_inflight(json.loads(LEDGER.read_text(encoding="utf-8"))) == 0:
            break
        time.sleep(1)
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    if evaluator_inflight(ledger) != 0:
        raise SystemExit("the evaluator kept a call on the wire")
    subprocess.run(["systemctl", "--user", "stop", UNIT], check=False)
    for _ in range(120):
        state = subprocess.run(
            ["systemctl", "--user", "is-active", UNIT],
            capture_output=True, text=True,
        ).stdout.strip()
        if state != "active":
            break
        time.sleep(1)
    run(["systemd-run", "--user", f"--unit={UNIT}",
         f"--working-directory={runtime}", str(launcher)])
    time.sleep(5)
    plan["unit_state"] = subprocess.run(
        ["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True
    ).stdout.strip()
    plan["restarted_at_utc"] = now()
    receipt = EVAL_DATA / f"cutover-receipt-{short}-ch3cb.json"
    receipt.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
    plan["receipt"] = str(receipt)
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
