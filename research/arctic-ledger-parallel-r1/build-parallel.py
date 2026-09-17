#!/usr/bin/env python
"""Activation of the chapter 3 producer on the parallel bookkeeping store.

Captain order 2026-09-17 03:35 UTC: sixteen papers in flight. The shared
paid-call ledger becomes the snapshot-plus-journal store, and policy v12 moves
the two request-rate limits from 8 and 40 to 16 and 100. Nothing about money
moves: every ceiling, the per-request cap, the paper cost cap and every project
design count stay exactly as the USD 200 expansion left them.

Actions, in order::

    prepare          snapshot the commit, check the inputs, write the gate,
                     the review and the launcher; make no change to the ledger
    stop             stop the evaluator and the producer at a settled boundary
    migrate          convert the one-file ledger into the store, prove equality
    apply-transition write the reviewed v12 transition and prove the ledger
    launch           start the producer with sixteen paper workers
    evaluator        after the first paid construction call: successor
                     authorization for the evaluator on this snapshot, restart
    readers          restart the cost guard and the website viewer on this
                     snapshot, so they read the store
    observe          sample for a measured window (default 15 minutes)

Every path of the live run is read from the running producer's own argument
vector, from the live units, or from the predecessor activation; never
guessed. Every ledger read of this script goes through the store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

TASK = "arctic-ledger-parallel-r1"
FM_DATA = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data")
FM_STATE = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/state")
DATA = FM_DATA / TASK
WORKTREE = Path("/home/ben/.treehouse/arctic-qa-e841b2/34/arctic-qa")
NAMESPACE = Path("/mnt/crdata/research-abstention/arctic-qa")
CHAPTER3_ROOT = NAMESPACE / "chapter3"
STREAMING = NAMESPACE / "streaming-dataset-r1"
LEDGER = STREAMING / "shared-paid-call-ledger.json"
IDENTITY = LEDGER.with_name(f".{LEDGER.name}.identity.json")
RECEIPTS = STREAMING / "model-receipts"
PROGRESS = STREAMING / "progress.json"
CREDENTIAL = Path("/home/ben/.config/arctic-qa/gemini-api-key")
JEV_RANKING = NAMESPACE / "jev-prescreen-r1" / "full-screen" / "live-ranking.json"

TMUX_SESSION = "arctic-ch3-production-r1"
RUN_ID = os.environ.get("CH3_RUN_ID", "chapter3-7dc6485-r3")
CAMPAIGN = "arctic-qa-production-campaign-003"
STREAM_INPUT_RUN_ID = "chapter3-7dc6485-r1-input"
MAX_PAPERS = 4420
PAPER_WORKERS = int(os.environ.get("CH3_PAPER_WORKERS", "16"))
OPTION_WORKERS = int(os.environ.get("CH3_OPTION_WORKERS", "4"))
ELIGIBILITY_PROMPT = "config/gemini-eligibility-prompt-v8.txt"
ELIGIBILITY_SCHEMA = "schemas/gemini-eligibility.v4.schema.json"
RESCREEN_PROMPT = "config/gemini-eligibility-geography-rescreen-v2.txt"
CEILING = "253.990121"
POLICY_V12 = DATA / "streaming-dataset-budget-policy-v12-chapter3-parallel.json"
CHANGED_FIELDS = {
    "maximum_concurrent_generation_requests": {"from": 8, "to": 16},
    "maximum_generation_requests_per_minute": {"from": 40, "to": 100},
}
REASON = (
    "Chapter 3 parallel bookkeeping (captain order 2026-09-17 03:35 UTC): "
    "sixteen papers in flight. The shared paid-call ledger is now a compacted "
    "snapshot and an append-only journal, so one paid call no longer pays for "
    "the whole history, and the request rate the run can reach moves with it. "
    "The two request-rate limits move together and nothing else moves: the "
    "concurrency slots from 8 to 16 and the minute window from 40 to 100. "
    "Every money ceiling, the per-request cap, the paper cost cap and every "
    "project design count stay exactly as the USD 200 expansion left them, so "
    "this transition authorizes no new spend and keeps the same cumulative "
    f"tranche of USD {CEILING}. Halt at exhaustion; no replay; no retry; no "
    "budget reset."
)

# The streaming abstention evaluator, which shares the ledger.
EVAL_UNIT = "arctic-abstention-stream-r3"
EVAL_DATA = FM_DATA / "arctic-eval-authorization-r8"
EVAL_PREDECESSOR_COMMIT = "739e5c1"
EVAL_AUTHORIZATION = EVAL_DATA / "streaming-eval-r11-authorization-739e5c1.json"
EVAL_LAUNCHER = EVAL_DATA / "streaming-eval-r11-launcher-739e5c1.sh"
EVAL_BOUND_CONFIGS = (
    "config/benchmark-evaluation-plan-high-v1.json",
    "config/live-dataset-current-contract-v1.json",
    "config/benchmark-evaluation-policy-v3.json",
    "config/benchmark-evaluation-prices-v1.json",
    "config/benchmark-evaluation-subscription-models-v1.json",
)
# The readers that run as user units on their own snapshots.
GUARD_UNIT = "arctic-benchmark-guard-r1"
VIEWER_UNIT = "arctic-corpus-stage-r1-formatting"

sys.path.insert(0, str(WORKTREE / "src"))
from arctic_qa import ledger_store  # noqa: E402


def sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha256(value: object) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_ledger() -> dict:
    """The ledger as the store materializes it: snapshot plus journal."""
    return ledger_store.read_ledger(LEDGER)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(command)}\n{result.stderr}")
    return result


def unit_state(unit: str) -> str:
    return subprocess.run(
        ["systemctl", "--user", "is-active", unit], capture_output=True, text=True
    ).stdout.strip()


def unit_argv(unit: str) -> tuple[Path, list[str]] | None:
    """The python argument vector of a user unit, and its working directory."""
    pid = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not pid or pid == "0":
        return None
    children = subprocess.run(
        ["pgrep", "-P", pid], capture_output=True, text=True
    ).stdout.split()
    for candidate in [*children, pid]:
        try:
            raw = Path(f"/proc/{candidate}/cmdline").read_bytes().decode()
            cwd = Path(os.readlink(f"/proc/{candidate}/cwd"))
        except OSError:
            continue
        argv = raw.split("\0")[:-1]
        if "arctic_qa" in raw and argv and argv[0].endswith("python"):
            return cwd, argv
    return None


class Activation:
    def __init__(self, commit: str) -> None:
        self.commit = run(
            ["git", "-C", str(WORKTREE), "rev-parse", commit]
        ).stdout.strip()
        self.short = self.commit[:7]
        self.archive = DATA / f"source-{self.short}-{TASK}.tar"
        self.runtime = DATA / "runtime" / f"app-{self.short}-{TASK}"
        self.review = DATA / f"implementation-review-{self.short}-ch3lp.md"
        self.gate = DATA / f"live-execution-gate-{self.short}-ch3lp.json"
        self.ledger_transition = DATA / f"ledger-config-transition-{self.short}-ch3lp.json"
        self.launcher = DATA / f"launcher-{self.short}-ch3lp.sh"
        self.log = DATA / f"launcher-{self.short}-ch3lp.log"
        self.state = DATA / f"activation-state-{self.short}.json"
        self.stream_input = CHAPTER3_ROOT / "streaming-input" / STREAM_INPUT_RUN_ID
        self.eligibility_dir = CHAPTER3_ROOT / "gemini-eligibility" / RUN_ID

    # ---- the live producer ------------------------------------------------

    @staticmethod
    def live_producer() -> tuple[int, list[str]] | None:
        marker = f"--run-id\0{RUN_ID}\0"
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                raw = (proc / "cmdline").read_bytes().decode()
            except OSError:
                continue
            if "arctic_qa" in raw and marker in raw and "\0stream\0" in raw:
                return int(proc.name), raw.split("\0")[:-1]
        return None

    @staticmethod
    def live_argument(arguments: list[str], flag: str) -> str | None:
        if flag in arguments:
            return arguments[arguments.index(flag) + 1]
        return None

    @staticmethod
    def our_inflight(ledger: dict) -> int:
        """This run's requests between two durable states, counting rows too."""
        return sum(
            1
            for request in ledger.get("requests", {}).values()
            if request.get("state") in {"submitted", "counting"}
            and str(request.get("run_id")) == RUN_ID
        )

    @staticmethod
    def evaluator_inflight(ledger: dict) -> int:
        return sum(
            1
            for request in ledger.get("requests", {}).values()
            if request.get("state") in {"submitted", "counting"}
            and request.get("phase") == "benchmark_evaluation"
        )

    @staticmethod
    def our_submissions(ledger: dict) -> int:
        return sum(
            1
            for request in ledger.get("requests", {}).values()
            if str(request.get("run_id")) == RUN_ID and request.get("submitted_at_utc")
        )

    def runtime_python(self, *arguments: str, check: bool = True):
        command = [
            "nix",
            "develop",
            f"path:{self.runtime}",
            "-c",
            "env",
            f"PYTHONPATH={self.runtime / 'src'}",
            "python",
            *arguments,
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, cwd=str(self.runtime)
        )
        if check and result.returncode != 0:
            raise SystemExit(
                f"command failed: {' '.join(command)}\n{result.stdout}\n{result.stderr}"
            )
        return result

    # ---- the run this succeeds --------------------------------------------

    def live_inputs(self) -> tuple[Path, Path, Path, Path | None]:
        """The gate, policy, transition and Jev ranking of the run this succeeds."""
        live = self.live_producer()
        if live is not None:
            _, arguments = live
            return (
                Path(self.live_argument(arguments, "--execution-gate-file")),
                Path(self.live_argument(arguments, "--streaming-budget-policy-file")),
                Path(self.live_argument(arguments, "--ledger-config-transition-file")),
                Path(self.live_argument(arguments, "--jev-ranking-file") or JEV_RANKING),
            )
        state = self.state if self.state.is_file() else None
        if state is not None:
            values = read_json(state)
            return (
                Path(values["prior_gate"]),
                Path(values["prior_policy"]),
                Path(values["prior_transition"]),
                Path(values["jev_ranking_file"]),
            )
        gate = os.environ.get("CH3_LIVE_GATE")
        policy = os.environ.get("CH3_LIVE_POLICY")
        transition = os.environ.get("CH3_LIVE_TRANSITION")
        assert gate and policy and transition, (
            "no live producer and no activation state: set CH3_LIVE_GATE, "
            "CH3_LIVE_POLICY and CH3_LIVE_TRANSITION"
        )
        return Path(gate), Path(policy), Path(transition), JEV_RANKING

    def validate_ledger(self, policy: Path, gate: Path, transition: Path) -> dict:
        price_config = self.runtime / "config" / "gemini-eligibility-v1.json"
        script = (
            "import json\n"
            "from decimal import Decimal\n"
            "from pathlib import Path\n"
            "from arctic_qa.model_broker import SharedGeminiBroker\n"
            "broker = SharedGeminiBroker(\n"
            f"    policy_file=Path({str(policy)!r}),\n"
            f"    price_config_file=Path({str(price_config)!r}),\n"
            f"    execution_gate_file=Path({str(gate)!r}),\n"
            f"    ledger_file=Path({str(LEDGER)!r}),\n"
            f"    receipts_dir=Path({str(RECEIPTS)!r}),\n"
            f"    credential_file=Path({str(CREDENTIAL)!r}),\n"
            "    prior_construction_spend_usd=Decimal('0'),\n"
            f"    config_transition_file=Path({str(transition)!r}),\n"
            ")\n"
            "status = broker.status()\n"
            "print(json.dumps({k: status[k] for k in ('spent_usd','reserved_usd',"
            "'ambiguous_reserved_usd','halted','integrity_valid','status_state',"
            "'inflight','generation_submissions','config_transition_sha256',"
            "'policy_sha256','limits')}))\n"
        )
        for _ in range(30):
            result = self.runtime_python("-c", script, check=False)
            if result.returncode == 0:
                return json.loads(result.stdout.strip().splitlines()[-1])
            if "requires a settled ledger" not in result.stderr:
                raise SystemExit(
                    "the runtime refuses the ledger:\n" + result.stderr[-6000:]
                )
            time.sleep(5)
        raise SystemExit("every settled window was lost to a concurrent call")

    # ---- prepare -----------------------------------------------------------

    def prepare(self) -> None:
        live_gate, live_policy, live_transition, jev_ranking = self.live_inputs()
        assert live_gate.is_file(), live_gate
        assert live_transition.is_file(), live_transition
        assert jev_ranking.is_file(), jev_ranking
        prior_gate = read_json(live_gate)
        transition = read_json(live_transition)
        assert (
            sha256_file(Path(prior_gate["review_record"]))
            == prior_gate["review_record_sha256"]
        ), "the live gate's review record changed"
        assert prior_gate["budget_policy_sha256"] == sha256_file(live_policy), (
            "the live policy is not the one the live gate records"
        )
        assert transition["to_policy_sha256"] == sha256_file(live_policy), (
            "the live transition does not end at the live policy"
        )
        # A successor of a start that already applied the v12 transition
        # (the 05:01 UTC apply of 2026-09-17 on commit 948c860) binds that
        # transition and writes no new one: the policy is already v12.
        successor = sha256_file(live_policy) == sha256_file(POLICY_V12)
        if successor:
            self.ledger_transition = live_transition

        DATA.mkdir(parents=True, exist_ok=True)
        if not self.archive.exists():
            run(
                [
                    "git",
                    "-C",
                    str(WORKTREE),
                    "archive",
                    "--format=tar",
                    "-o",
                    str(self.archive),
                    self.commit,
                ]
            )
            os.chmod(self.archive, 0o444)
        archive_sha = sha256_file(self.archive)
        if not (self.runtime / "flake.nix").exists():
            self.runtime.mkdir(parents=True, exist_ok=True)
            run(["tar", "-xf", str(self.archive), "-C", str(self.runtime)])
        runtime_hash = run(
            [
                "bash",
                "-c",
                f"cd '{self.runtime}' && find . -type f -print0 | sort -z "
                "| xargs -0 sha256sum | sha256sum | cut -d' ' -f1",
            ]
        ).stdout.strip()
        config = self.runtime / "config"
        price_config = config / "gemini-eligibility-v1.json"
        assert sha256_file(price_config) == prior_gate["model_config_sha256"], (
            "the price config changed; a price transition is needed"
        )
        for name, key in (
            (ELIGIBILITY_PROMPT, "eligibility_prompt_sha256"),
            (ELIGIBILITY_SCHEMA, "eligibility_schema_sha256"),
            (RESCREEN_PROMPT, "eligibility_rescreen_prompt_sha256"),
        ):
            assert sha256_file(self.runtime / name) == prior_gate[key], name
        # The v12 policy is the v11 policy with exactly the two request-rate
        # fields changed. Nothing else may move.
        if not successor:
            v11 = read_json(live_policy)
            v12 = read_json(POLICY_V12)
            expected = dict(v11)
            for field, limits in CHANGED_FIELDS.items():
                assert v11[field] == limits["from"], (field, v11[field])
                expected[field] = limits["to"]
            assert v12 == expected, "the v12 policy moves more than the two rate fields"
        # The runtime knows the store and the rate pair.
        constants = json.loads(
            self.runtime_python(
                "-c",
                "import json\n"
                "from arctic_qa import ledger_store, model_broker\n"
                "print(json.dumps({"
                "'parallel_requests': model_broker.CHAPTER3_PARALLEL_REQUESTS,"
                "'parallel_per_minute': model_broker.CHAPTER3_PARALLEL_REQUESTS_PER_MINUTE,"
                "'rate_registered': (16, 100) in model_broker.ALLOWED_REQUEST_RATES,"
                "'change_registered': model_broker.CHAPTER3_PARALLEL_CHANGE in "
                "model_broker.POLICY_TRANSITION_CHANGES,"
                "'compaction_interval_seconds': ledger_store.COMPACTION_INTERVAL_SECONDS,"
                "'journal_schema': ledger_store.JOURNAL_RECORD_SCHEMA,"
                "}))",
            ).stdout
        )
        assert constants["rate_registered"] and constants["change_registered"], constants
        assert constants["parallel_requests"] == 16, constants

        self.review.write_text(
            "\n".join(
                [
                    f"# Chapter 3 parallel bookkeeping implementation review, commit {self.commit}",
                    "",
                    f"Successor of the reviewed gate `{live_gate}`",
                    f"(commit {prior_gate['integrated_code_commit']}), for the",
                    f"continuation of run {RUN_ID} on campaign {CAMPAIGN}.",
                    "The run id, the campaign, the streaming input, the eligibility",
                    "inputs, the price config and the Jev ranking are unchanged.",
                    "",
                    "## What changed",
                    "",
                    "- The shared paid-call ledger is a compacted snapshot and an",
                    "  append-only journal (`shared-paid-call-ledger-journal-v1`).",
                    "  One paid call reads the records its peers appended, proves the",
                    "  rows it moved, appends one line and shares one fsync with every",
                    "  concurrent call. The snapshot is bound to the journal by a base",
                    "  record; a snapshot changed outside the store fails closed.",
                    "- Every reader of this repository reads the store: the producer,",
                    "  the evaluator, the cost guard, the website viewer.",
                    "- The migration converts the one-file ledger once, with the",
                    "  producer and the evaluator stopped, keeps the file as a frozen",
                    "  archive and proves every field identical before and after.",
                    "- Policy v12 moves the two request-rate limits from 8 and 40 to",
                    "  16 and 100 (`CHAPTER3_PARALLEL_CHANGE`). Nothing else moves.",
                    "- The producer runs sixteen paper workers.",
                    "",
                    "## Measurement",
                    "",
                    "`research/arctic-ledger-parallel-r1/report.md` on the branch:",
                    "a durable write on the data disk costs 150 to 470 ms whatever",
                    "its size, the receipts directory was walked eight times per",
                    "proof, and the whole ledger was parsed and proved on every read.",
                    "On a copy of the live ledger the store reads in 10 ms, proves a",
                    "delta in 10 ms and commits in 57 ms a commit from sixteen threads.",
                    "",
                    "## Tests",
                    "",
                    "- `tests/test_ledger_store.py`: sixteen threads admit, settle and",
                    "  prove exact totals; a wave shares its flushes; the compactor",
                    "  writes a snapshot the full proof accepts; every reader sees the",
                    "  committed state before a compaction; the migration keeps every",
                    "  field and freezes the archive; the rate pair is registered.",
                    "- The migration was proved on a copy of the live ledger at 6,320",
                    "  rows: identical, canonical hash unchanged.",
                    "- The whole release suite runs after the relaunch, per the",
                    "  captain's order of 04:37 UTC.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        gate = dict(prior_gate)
        gate.update(
            {
                "integrated_code_commit": self.commit,
                "review_record": str(self.review),
                "review_record_sha256": sha256_file(self.review),
                "roles_file_sha256": sha256_file(config / "roles.v1.json"),
                "model_config_sha256": sha256_file(price_config),
                "live_dataset_contract_sha256": sha256_file(
                    config / "live-dataset-current-contract-v1.json"
                ),
                "source_archive_sha256": archive_sha,
                "runtime_snapshot_sha256": runtime_hash,
                "runtime_snapshot": str(self.runtime),
                "prior_activation_gate": str(live_gate),
                "prior_activation_gate_sha256": sha256_file(live_gate),
                "budget_policy_file": str(POLICY_V12),
                "budget_policy_sha256": sha256_file(POLICY_V12),
                "prior_budget_policy_file": (
                    prior_gate.get("prior_budget_policy_file")
                    if successor
                    else str(live_policy)
                ),
                "prior_budget_policy_sha256": (
                    prior_gate.get("prior_budget_policy_sha256")
                    if successor
                    else sha256_file(live_policy)
                ),
                "policy_changed_fields": CHANGED_FIELDS,
                "ledger_config_transition_file": str(self.ledger_transition),
                "successor_of_applied_transition": successor,
                "ledger_store_schema": constants["journal_schema"],
                "ledger_store_compaction_interval_seconds": constants[
                    "compaction_interval_seconds"
                ],
                "jev_ranking_file": str(jev_ranking),
                "paper_workers": PAPER_WORKERS,
                "option_workers": OPTION_WORKERS,
                # Inherited, not rewritten: the review of the ACTIVE (v11)
                # transition, which the live gate already carries.
                "supersedes_config_transition_review": {
                    field: transition[field]
                    for field in (
                        "execution_gate_sha256",
                        "integrated_code_commit",
                        "review_record",
                        "review_record_sha256",
                    )
                },
                "activation_state": "authorized_not_started",
            }
        )
        if self.gate.exists():
            os.chmod(self.gate, 0o644)
        write_json(self.gate, gate)
        os.chmod(self.gate, 0o444)
        self.write_launcher(jev_ranking)
        state = {
            "task": TASK,
            "commit": self.commit,
            "run_id": RUN_ID,
            "campaign_id": CAMPAIGN,
            "runtime": str(self.runtime),
            "archive_sha256": archive_sha,
            "runtime_sha256": runtime_hash,
            "gate": str(self.gate),
            "gate_sha256": sha256_file(self.gate),
            "prior_gate": str(live_gate),
            "prior_gate_sha256": sha256_file(live_gate),
            "prior_policy": str(live_policy),
            "prior_policy_sha256": sha256_file(live_policy),
            "prior_transition": str(live_transition),
            "prior_transition_sha256": sha256_file(live_transition),
            "policy": str(POLICY_V12),
            "policy_sha256": sha256_file(POLICY_V12),
            "transition": str(self.ledger_transition),
            "successor_of_applied_transition": successor,
            "jev_ranking_file": str(jev_ranking),
            "paper_workers": PAPER_WORKERS,
            "option_workers": OPTION_WORKERS,
            "price_config_sha256": sha256_file(price_config),
            "constants": constants,
            "launcher": str(self.launcher),
            "ceiling_usd": CEILING,
            "eligibility_dir": str(self.eligibility_dir),
            "prepared_at_utc": now(),
        }
        write_json(self.state, state)
        print(
            json.dumps(
                {k: state[k] for k in ("commit", "gate_sha256", "runtime_sha256")},
                indent=2,
            )
        )

    def write_launcher(self, jev_ranking: Path) -> None:
        if self.launcher.exists():
            os.chmod(self.launcher, 0o644)
        self.launcher.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "",
                    f"release_runtime={self.runtime}",
                    f"release_gate={self.gate}",
                    f"release_transition={self.ledger_transition}",
                    f"release_policy={POLICY_V12}",
                    "release_max_papers=${1:-" + str(MAX_PAPERS) + "}",
                    "release_paper_workers=${2:-" + str(PAPER_WORKERS) + "}",
                    "release_option_workers=${3:-" + str(OPTION_WORKERS) + "}",
                    "",
                    'cd "$release_runtime"',
                    'exec nix develop "path:$release_runtime" -c env \\',
                    '  "PYTHONPATH=$release_runtime/src" \\',
                    "  python -m arctic_qa --json \\",
                    "  --data-root /mnt/crdata/research-abstention \\",
                    "  stream \\",
                    "  --phase away_production \\",
                    f"  --run-id {RUN_ID} \\",
                    f"  --campaign-id {CAMPAIGN} \\",
                    f"  --access-run-dir {self.stream_input} \\",
                    f"  --eligibility-run-dir {self.eligibility_dir} \\",
                    f"  --eligibility-prompt-file {ELIGIBILITY_PROMPT} \\",
                    f"  --eligibility-schema-file {ELIGIBILITY_SCHEMA} \\",
                    f"  --eligibility-rescreen-prompt-file {RESCREEN_PROMPT} \\",
                    "  --eligibility-policy-file /mnt/crdata/research-abstention/"
                    "arctic-qa/corpus-search-r1/protocol/protocol-v3.json \\",
                    f"  --credential-file {CREDENTIAL} \\",
                    "  --prior-construction-spend-usd 0 \\",
                    f"  --shared-ledger-file {LEDGER} \\",
                    f"  --model-receipts-dir {RECEIPTS} \\",
                    '  --streaming-budget-policy-file "$release_policy" \\',
                    "  --price-config-file config/gemini-eligibility-v1.json \\",
                    '  --execution-gate-file "$release_gate" \\',
                    '  --ledger-config-transition-file "$release_transition" \\',
                    "  --roles-file config/roles.v1.json \\",
                    "  --role-profile gemini_separated \\",
                    f"  --code-commit {self.short} \\",
                    '  --max-papers "$release_max_papers" \\',
                    '  --paper-workers "$release_paper_workers" \\',
                    '  --option-workers "$release_option_workers" \\',
                    f"  --jev-ranking-file {jev_ranking}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chmod(self.launcher, 0o555)

    # ---- stop --------------------------------------------------------------

    def stop(self) -> None:
        """Stop the evaluator, then the producer, each at its own boundary.

        The migration and the transition both need a settled ledger: nothing
        submitted, nothing counting, of any caller.
        """
        state = read_json(self.state)
        stopped = {"stopped_at_utc": now()}
        if unit_state(EVAL_UNIT) == "active":
            for _ in range(600):
                if self.evaluator_inflight(read_ledger()) == 0:
                    break
                time.sleep(1)
            assert self.evaluator_inflight(read_ledger()) == 0, (
                "the evaluator kept a call on the wire"
            )
            subprocess.run(["systemctl", "--user", "stop", EVAL_UNIT], check=False)
            for _ in range(120):
                if unit_state(EVAL_UNIT) != "active":
                    break
                time.sleep(1)
            assert unit_state(EVAL_UNIT) != "active", "the evaluator did not stop"
            stopped["evaluator_stopped_at_utc"] = now()
            print("evaluator stopped", file=sys.stderr)
        live = self.live_producer()
        if live is None:
            print("no live producer", file=sys.stderr)
        else:
            pid, _ = live
            for _ in range(1200):
                if self.our_inflight(read_ledger()) == 0:
                    break
                time.sleep(0.5)
            assert self.our_inflight(read_ledger()) == 0, "no boundary was reached"
            print(f"stopping the live producer {pid}", file=sys.stderr)
            subprocess.run(["kill", str(pid)], check=False)
            for _ in range(120):
                if self.live_producer() is None:
                    break
                time.sleep(1)
            assert self.live_producer() is None, "the live producer did not stop"
            stopped["producer_stopped_at_utc"] = now()
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
        ledger = read_ledger()
        stopped["ledger"] = {
            "inflight": ledger["inflight"],
            "our_inflight": self.our_inflight(ledger),
            "evaluator_inflight": self.evaluator_inflight(ledger),
            "spent_usd": ledger["spent_usd"],
            "halted": ledger["halted"],
        }
        assert ledger["inflight"] == 0, stopped
        state["stop"] = stopped
        write_json(self.state, state)
        print(json.dumps(stopped, indent=2))

    # ---- migrate -----------------------------------------------------------

    def migrate(self) -> None:
        state = read_json(self.state)
        assert "stop" in state, "stop first"
        assert self.live_producer() is None and unit_state(EVAL_UNIT) != "active"
        before = read_ledger()
        assert before["inflight"] == 0 and not before["halted"], "the ledger is busy"
        started = now()
        dry = json.loads(
            self.runtime_python(
                "-m", "arctic_qa", "--json", "migrate-ledger-store",
                "--shared-ledger-file", str(LEDGER),
            ).stdout.strip().splitlines()[-1]
        )
        assert dry["identical"] is True, dry
        applied = json.loads(
            self.runtime_python(
                "-m", "arctic_qa", "--json", "migrate-ledger-store",
                "--shared-ledger-file", str(LEDGER), "--apply",
            ).stdout.strip().splitlines()[-1]
        )
        assert applied["identical"] is True, applied
        assert applied["action"] == "migrate", applied
        check = json.loads(
            self.runtime_python(
                "-m", "arctic_qa", "--json", "migrate-ledger-store",
                "--shared-ledger-file", str(LEDGER), "--action", "check",
            ).stdout.strip().splitlines()[-1]
        )
        assert check["identical"] is True, check
        after = read_ledger()
        for name in ("spent_usd", "reserved_usd", "ambiguous_reserved_usd",
                     "inflight", "halted", "count_requests",
                     "generation_submissions", "policy_sha256"):
            assert before[name] == after[name], (name, before[name], after[name])
        assert len(before["requests"]) == len(after["requests"])
        state["migration"] = {
            "started_at_utc": started,
            "finished_at_utc": now(),
            "archive_file": applied["archive_file"],
            "archive_sha256": applied["archive_sha256"],
            "request_rows": applied["request_rows"],
            "canonical_sha256": applied["canonical_sha256_after"],
            "money_fields": applied["money_fields"],
            "check": {k: check[k] for k in ("identical", "journal_applied_seq")},
        }
        write_json(self.state, state)
        print(json.dumps(state["migration"], indent=2))

    # ---- transition --------------------------------------------------------

    def find_event(self, authorization: dict) -> Path:
        path = RECEIPTS / f"config-transition-{canonical_sha256(authorization)}.json"
        assert path.is_file(), path
        return path

    def apply_transition(self) -> None:
        state = read_json(self.state)
        assert "migration" in state, "migrate first"
        assert "transition_applied" not in state, "the transition is applied"
        ledger = read_ledger()
        assert not ledger["halted"] and ledger["inflight"] == 0, "the ledger is busy"
        price_config = self.runtime / "config" / "gemini-eligibility-v1.json"
        live_policy = Path(state["prior_policy"])
        live_transition = Path(state["prior_transition"])
        # The v11 pair is validated first, under the live inputs, with this
        # gate; that binds the gate and settles the store's snapshot.
        before = self.validate_ledger(live_policy, self.gate, live_transition)
        assert before["integrity_valid"] is True, before
        assert before["halted"] is False, before
        assert before["limits"]["maximum_concurrent_generation_requests"] == 8
        active = before["config_transition_sha256"]
        prior_events = [
            path
            for path in RECEIPTS.glob("config-transition-*.json")
            if sha256_file(path) == active
        ]
        assert len(prior_events) == 1, f"active transition event {active}: {prior_events}"
        prior_event = prior_events[0]
        authorization = {
            "schema": "shared-paid-call-config-transition-v2",
            "ledger_file": str(LEDGER),
            "from_price_config_sha256": sha256_file(price_config),
            "to_price_config_sha256": sha256_file(price_config),
            "from_config_transition_sha256": sha256_file(prior_event),
            "from_policy_file": str(live_policy),
            "from_policy_sha256": sha256_file(live_policy),
            "to_policy_sha256": sha256_file(POLICY_V12),
            "changed_policy_fields": CHANGED_FIELDS,
            "maximum_authorized_cumulative_tranche_usd": CEILING,
            "expected_ledger_sha256": sha256_file(LEDGER),
            "expected_identity_sha256": sha256_file(IDENTITY),
            "execution_gate_sha256": sha256_file(self.gate),
            "integrated_code_commit": self.commit,
            "review_record": str(self.review),
            "review_record_sha256": sha256_file(self.review),
            "reason": REASON,
            "authorized_at_utc": now(),
        }
        write_json(self.ledger_transition, authorization)
        after = self.validate_ledger(POLICY_V12, self.gate, self.ledger_transition)
        assert after["integrity_valid"] is True, after
        assert after["halted"] is False, after
        event = self.find_event(authorization)
        assert after["config_transition_sha256"] == sha256_file(event)
        assert after["limits"]["maximum_concurrent_generation_requests"] == 16
        assert after["limits"]["maximum_generation_requests_per_minute"] == 100
        assert after["limits"]["away_session_total_ceiling_usd"] == CEILING
        assert after["spent_usd"] == ledger["spent_usd"], "the transition moved money"
        assert after["policy_sha256"] == sha256_file(POLICY_V12)
        os.chmod(self.ledger_transition, 0o444)
        again = self.validate_ledger(POLICY_V12, self.gate, self.ledger_transition)
        assert again["config_transition_sha256"] == sha256_file(event)
        assert again["integrity_valid"] is True
        state["transition_applied"] = {
            "authorization_file": str(self.ledger_transition),
            "authorization_file_sha256": sha256_file(self.ledger_transition),
            "event": str(event),
            "event_sha256": sha256_file(event),
            "predecessor_event": str(prior_event),
            "predecessor_event_sha256": sha256_file(prior_event),
            "applied_at_utc": now(),
            "result": after,
        }
        write_json(self.state, state)
        print(json.dumps({k: v for k, v in state["transition_applied"].items()
                          if k != "result"}, indent=2))

    # ---- launch ------------------------------------------------------------

    def launch(self) -> None:
        state = read_json(self.state)
        assert "transition_applied" in state or state.get(
            "successor_of_applied_transition"
        ), "apply the transition first"
        assert self.live_producer() is None, "a producer of this run is already live"
        if not state.get("successor_of_applied_transition"):
            # A start that applies a transition is revalidated until its first
            # construction request and needs inflight 0, so a live evaluator
            # must wait for that call. A successor start applies none.
            assert unit_state(EVAL_UNIT) != "active", (
                "the evaluator must stay stopped until the first paid construction call"
            )
        ledger = read_ledger()
        assert not ledger["halted"], "the ledger is halted"
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
        run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                TMUX_SESSION,
                "-c",
                str(self.runtime),
                f"bash {self.launcher} >> {self.log} 2>&1",
            ]
        )
        for _ in range(120):
            time.sleep(1)
            if self.live_producer() is not None:
                break
        live = self.live_producer()
        assert live is not None, "the producer did not start"
        state["launch"] = {
            "launched_at_utc": now(),
            "pid": live[0],
            "tmux_session": TMUX_SESSION,
            "log": str(self.log),
            "paper_workers": PAPER_WORKERS,
            "option_workers": OPTION_WORKERS,
            "submissions_at_launch": self.our_submissions(ledger),
        }
        write_json(self.state, state)
        print(json.dumps(state["launch"], indent=2))

    def wait_first_paid_call(self, seconds: int = 3600) -> dict:
        """Wait for the first paid construction call of the relaunched producer."""
        state = read_json(self.state)
        start = state["launch"]["submissions_at_launch"]
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ledger = read_ledger()
            if self.our_submissions(ledger) > start:
                return {"first_paid_call_at_utc": now(),
                        "submissions": self.our_submissions(ledger)}
            assert self.live_producer() is not None, "the producer exited before its first paid call"
            time.sleep(10)
        raise SystemExit("no paid construction call inside the window")

    # ---- the evaluator -----------------------------------------------------

    def evaluator(self) -> None:
        state = read_json(self.state)
        assert "launch" in state, "launch first"
        first = self.wait_first_paid_call()
        for name in EVAL_BOUND_CONFIGS:
            changed = run(
                ["git", "-C", str(WORKTREE), "diff", "--name-only",
                 EVAL_PREDECESSOR_COMMIT, self.commit, "--", name]
            ).stdout.strip()
            assert not changed, f"a bound configuration changed: {name}"
        eval_runtime = EVAL_DATA / "runtime" / f"app-{self.short}-arctic-eval-authorization-r8"
        eval_archive = EVAL_DATA / f"source-{self.short}-arctic-eval-authorization-r8.tar"
        review = EVAL_DATA / f"streaming-eval-r11-review-{self.short}.md"
        successor = EVAL_DATA / f"streaming-eval-r11-authorization-{self.short}.json"
        launcher = EVAL_DATA / f"streaming-eval-r11-launcher-{self.short}.sh"
        prior = read_json(EVAL_AUTHORIZATION)
        assert prior["integrated_code_commit"] == EVAL_PREDECESSOR_COMMIT
        if not eval_archive.exists():
            run(["git", "-C", str(WORKTREE), "archive", "--format=tar",
                 "-o", str(eval_archive), self.commit])
            os.chmod(eval_archive, 0o444)
        if not (eval_runtime / "flake.nix").exists():
            eval_runtime.mkdir(parents=True, exist_ok=True)
            run(["tar", "-xf", str(eval_archive), "-C", str(eval_runtime)])
        review.write_text(
            "\n".join(
                [
                    f"# Streaming abstention evaluator, snapshot {self.short}",
                    "",
                    f"Successor of `{EVAL_AUTHORIZATION}` (commit {EVAL_PREDECESSOR_COMMIT}).",
                    "Same campaign, same run-id prefix, same work directory, same plan,",
                    "policy, prices, contract and subscription models: the five files the",
                    "authorization binds are byte-identical between the two commits, and",
                    "the cutover refuses otherwise. No new bound and no new spend.",
                    "",
                    "## Why the evaluator moves",
                    "",
                    "The shared paid-call ledger is now a snapshot and an append-only",
                    "journal (`docs/SHARED_MODEL_BROKER.md`, \"Parallel bookkeeping\").",
                    "An evaluator on the predecessor snapshot would read the plain file",
                    "as the whole ledger and write it whole, which the store refuses as",
                    "a snapshot changed outside it. On this snapshot the evaluator reads",
                    "the store, appends to the journal and defers its own snapshot to the",
                    "compactor, so it never holds the shared ledger lock for a durable",
                    "8 MB write the producer would wait for.",
                    "",
                    f"`research/arctic-ledger-parallel-r1/report.md` at {self.short}.",
                    "The evaluation path itself is unchanged: no plan, prompt, model,",
                    "gate, price or bound moves.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        record = dict(prior)
        record["integrated_code_commit"] = self.short
        record["review_record"] = str(review.resolve())
        record["review_record_sha256"] = sha256_file(review)
        record["written_at_utc"] = now()
        record["supersedes"] = {
            "authorization_file": str(EVAL_AUTHORIZATION),
            "authorization_sha256": sha256_file(EVAL_AUTHORIZATION),
            "integrated_code_commit": EVAL_PREDECESSOR_COMMIT,
            "review_record": prior["review_record"],
            "review_record_sha256": prior["review_record_sha256"],
        }
        successor.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
        text = EVAL_LAUNCHER.read_text(encoding="utf-8")
        text = text.replace(
            f"app-{EVAL_PREDECESSOR_COMMIT}-arctic-eval-authorization-r8",
            f"app-{self.short}-arctic-eval-authorization-r8",
        )
        text = text.replace(str(EVAL_AUTHORIZATION), str(successor))
        text = text.replace(f"--code-commit {EVAL_PREDECESSOR_COMMIT}",
                            f"--code-commit {self.short}")
        assert str(successor) in text and f"--code-commit {self.short}" in text
        launcher.write_text(text, encoding="utf-8")
        os.chmod(launcher, 0o555)
        check = subprocess.run(
            ["nix", "develop", f"path:{eval_runtime}", "-c", "env",
             f"PYTHONPATH={eval_runtime / 'src'}", "python", "-c",
             "import json,sys\n"
             "from pathlib import Path\n"
             "from arctic_qa.abstention_watch import validate_authorization\n"
             f"value = validate_authorization(Path({str(successor)!r}),\n"
             f"  plan_file=Path({str(eval_runtime / EVAL_BOUND_CONFIGS[0])!r}),\n"
             f"  contract_file=Path({str(eval_runtime / EVAL_BOUND_CONFIGS[1])!r}),\n"
             f"  evaluation_policy_file=Path({str(eval_runtime / EVAL_BOUND_CONFIGS[2])!r}),\n"
             f"  evaluation_price_config_file=Path({str(eval_runtime / EVAL_BOUND_CONFIGS[3])!r}),\n"
             f"  subscription_models_file=Path({str(eval_runtime / EVAL_BOUND_CONFIGS[4])!r}),\n"
             f"  code_commit={self.short!r})\n"
             "print(json.dumps({'integrated_code_commit': value['integrated_code_commit'],"
             " 'enabled': value['authorization_enabled']}))\n"],
            capture_output=True, text=True, cwd=str(eval_runtime),
        )
        if check.returncode != 0:
            raise SystemExit("the successor authorization does not validate:\n"
                             + check.stderr[-4000:])
        assert unit_state(EVAL_UNIT) != "active"
        run(["systemd-run", "--user", f"--unit={EVAL_UNIT}",
             f"--working-directory={eval_runtime}", str(launcher)])
        time.sleep(5)
        state["evaluator"] = {
            **first,
            "runtime": str(eval_runtime),
            "authorization": str(successor),
            "launcher": str(launcher),
            "authorization_check": json.loads(check.stdout.strip().splitlines()[-1]),
            "unit_state": unit_state(EVAL_UNIT),
            "restarted_at_utc": now(),
        }
        write_json(self.state, state)
        print(json.dumps(state["evaluator"], indent=2))

    # ---- the readers -------------------------------------------------------

    def readers(self) -> None:
        """Restart the guard and the viewer on this snapshot.

        Both run old code that reads the ledger file whole. On this snapshot
        each reads the store. Every argument is the one the live unit runs
        with; only the runtime moves.
        """
        state = read_json(self.state)
        restarted = {}
        for unit in (GUARD_UNIT, VIEWER_UNIT):
            found = unit_argv(unit)
            assert found is not None, f"{unit} has no live python process"
            cwd, argv = found
            assert argv[:2] == ["python", "-m"], argv[:3]
            subprocess.run(["systemctl", "--user", "stop", unit], check=False)
            for _ in range(60):
                if unit_state(unit) != "active":
                    break
                time.sleep(1)
            command = [
                "systemd-run", "--user", f"--unit={unit}",
                f"--working-directory={self.runtime}",
                "/run/current-system/sw/bin/nix", "develop", f"path:{self.runtime}",
                "-c", "env", f"PYTHONPATH={self.runtime / 'src'}",
                *argv,
            ]
            run(command)
            time.sleep(3)
            restarted[unit] = {
                "argv": argv,
                "previous_cwd": str(cwd),
                "unit_state": unit_state(unit),
                "restarted_at_utc": now(),
            }
        state["readers"] = restarted
        write_json(self.state, state)
        print(json.dumps({k: v["unit_state"] for k, v in restarted.items()}, indent=2))

    # ---- observe -----------------------------------------------------------

    def sample(self) -> dict:
        ledger = read_ledger()
        progress = read_json(PROGRESS) if PROGRESS.is_file() else {}
        jobs = self.eligibility_dir / "jobs"
        ours = [
            request
            for request in ledger["requests"].values()
            if str(request.get("run_id")) == RUN_ID
        ]
        return {
            "at_utc": now(),
            "our_submissions": self.our_submissions(ledger),
            "our_inflight": sum(1 for r in ours if r.get("state") == "submitted"),
            "ledger_inflight": ledger["inflight"],
            "spent_usd": ledger["spent_usd"],
            "halted": ledger["halted"],
            "http_429": sum(1 for r in ours if r.get("http_status") == 429),
            "http_503": sum(1 for r in ours if r.get("http_status") == 503),
            "ambiguous": sum(1 for r in ours if r.get("state") == "ambiguous_charge"),
            "screened_papers": len(list(jobs.glob("*.json"))) if jobs.is_dir() else 0,
            "counts": progress.get("counts", {}),
            "message": progress.get("message"),
            "live": self.live_producer() is not None,
            "journal_seq": ledger_store.snapshot_applied_seq(LEDGER),
        }

    def observe(self, seconds: int = 900) -> None:
        state = read_json(self.state)
        first = self.sample()
        samples = [first]
        peak = first["our_inflight"]
        deadline = time.monotonic() + seconds
        next_sample = time.monotonic() + 60
        while time.monotonic() < deadline:
            time.sleep(5)
            inflight = sum(
                1
                for r in read_ledger()["requests"].values()
                if r.get("state") == "submitted" and str(r.get("run_id")) == RUN_ID
            )
            peak = max(peak, inflight)
            if time.monotonic() >= next_sample:
                samples.append(self.sample())
                print(json.dumps(samples[-1]), flush=True)
                next_sample += 60
        last = samples[-1]
        minutes = seconds / 60.0
        summary = {
            "window_seconds": seconds,
            "requests": last["our_submissions"] - first["our_submissions"],
            "requests_per_minute": round(
                (last["our_submissions"] - first["our_submissions"]) / minutes, 2
            ),
            "papers": last["screened_papers"] - first["screened_papers"],
            "papers_per_hour": round(
                (last["screened_papers"] - first["screened_papers"]) * 60.0 / minutes, 1
            ),
            "peak_in_flight": peak,
            "http_429": last["http_429"] - first["http_429"],
            "http_503": last["http_503"] - first["http_503"],
            "ambiguous": last["ambiguous"] - first["ambiguous"],
            "candidate_processing_fault": last["counts"].get(
                "candidate_processing_fault", 0
            )
            - first["counts"].get("candidate_processing_fault", 0),
            "first": first,
            "last": last,
        }
        state.setdefault("observations", []).append(
            {"summary": summary, "samples": samples}
        )
        write_json(self.state, state)
        print(json.dumps(summary, indent=2))


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: build-parallel.py <commit> <action> [seconds]")
    activation = Activation(sys.argv[1])
    action = sys.argv[2]
    actions = {
        "prepare": activation.prepare,
        "stop": activation.stop,
        "migrate": activation.migrate,
        "apply-transition": activation.apply_transition,
        "launch": activation.launch,
        "evaluator": activation.evaluator,
        "readers": activation.readers,
    }
    if action == "observe":
        activation.observe(int(sys.argv[3]) if len(sys.argv) > 3 else 900)
    elif action in actions:
        actions[action]()
    else:
        raise SystemExit(f"unknown action: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
