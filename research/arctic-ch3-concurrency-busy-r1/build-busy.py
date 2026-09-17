#!/usr/bin/env python
"""Activation of the chapter 3 producer with the operation lock as a queue.

The run, the campaign, the streaming input, the eligibility inputs, the price
config, the budget policy and the applied ledger transition are the ones the
predecessor activation left. Nothing about money moves, so this activation needs
no policy transition and the benchmark evaluator keeps running through it.

Actions::

    prepare   snapshot the commit, write the successor gate and the launcher
    stop      stop the live producer at a boundary with none of ITS calls live
    launch    start the producer in the tmux session, from the snapshot
    observe   sample the ledger and the run directory for a measured window

Every path of the live run is read from the running producer or from the
predecessor activation state, never guessed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

TASK = "arctic-ch3-concurrency-busy-r1"
PREDECESSOR = "arctic-ch3-paper-completion-r1"
FM_DATA = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data")
DATA = FM_DATA / TASK
WORKTREE = Path("/home/ben/.treehouse/arctic-qa-e841b2/33/arctic-qa")
NAMESPACE = Path("/mnt/crdata/research-abstention/arctic-qa")
CHAPTER3_ROOT = NAMESPACE / "chapter3"
STREAMING = NAMESPACE / "streaming-dataset-r1"
LEDGER = STREAMING / "shared-paid-call-ledger.json"
IDENTITY = LEDGER.with_name(f".{LEDGER.name}.identity.json")
RECEIPTS = STREAMING / "model-receipts"
PROGRESS = STREAMING / "progress.json"
CREDENTIAL = Path("/home/ben/.config/arctic-qa/gemini-api-key")

TMUX_SESSION = "arctic-ch3-production-r1"
RUN_ID = os.environ.get("CH3_RUN_ID", "chapter3-7dc6485-r3")
CAMPAIGN = "arctic-qa-production-campaign-003"
STREAM_INPUT_RUN_ID = "chapter3-7dc6485-r1-input"
MAX_PAPERS = 4420
PAPER_WORKERS = int(os.environ.get("CH3_PAPER_WORKERS", "4"))
OPTION_WORKERS = int(os.environ.get("CH3_OPTION_WORKERS", "4"))
ELIGIBILITY_PROMPT = "config/gemini-eligibility-prompt-v8.txt"
ELIGIBILITY_SCHEMA = "schemas/gemini-eligibility.v4.schema.json"
RESCREEN_PROMPT = "config/gemini-eligibility-geography-rescreen-v2.txt"


def sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(command)}\n{result.stderr}")
    return result


class Activation:
    def __init__(self, commit: str) -> None:
        self.commit = run(
            ["git", "-C", str(WORKTREE), "rev-parse", commit]
        ).stdout.strip()
        self.short = self.commit[:7]
        self.archive = DATA / f"source-{self.short}-{TASK}.tar"
        self.runtime = DATA / "runtime" / f"app-{self.short}-{TASK}"
        self.review = DATA / f"implementation-review-{self.short}-ch3cb.md"
        self.gate = DATA / f"live-execution-gate-{self.short}-ch3cb.json"
        self.launcher = DATA / f"launcher-{self.short}-ch3cb.sh"
        self.log = DATA / f"launcher-{self.short}-ch3cb.log"
        self.state = DATA / f"activation-state-{self.short}.json"
        self.stream_input = CHAPTER3_ROOT / "streaming-input" / STREAM_INPUT_RUN_ID
        self.eligibility_dir = CHAPTER3_ROOT / "gemini-eligibility" / RUN_ID

    # ---- the live producer ------------------------------------------------

    @staticmethod
    def live_producer() -> tuple[int, list[str]] | None:
        """Return the running producer's pid and argument vector.

        The subcommand tells the producer apart from another command that takes
        the same run id.
        """
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
    def our_inflight(ledger: dict) -> int:
        """Count the requests of THIS run that are between two durable states.

        ``inflight`` counts every caller of the shared ledger and the benchmark
        evaluator keeps calling, so the producer's own boundary is this count.

        A ``counting`` row counts too. It has no reservation and no charge, but
        a stop that leaves one makes the next start walk back to that same
        call: it ended the producer at 02:32:10 UTC on 2026-09-17, before the
        broker learned to reuse such a row.
        """
        return sum(
            1
            for request in ledger.get("requests", {}).values()
            if request.get("state") in {"submitted", "counting"}
            and str(request.get("run_id")) == RUN_ID
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

    def predecessor(self) -> dict:
        state = sorted((FM_DATA / PREDECESSOR).glob("activation-state-*.json"))[-1]
        values = read_json(state)
        return {
            "state_file": str(state),
            "gate": Path(values["gate"]),
            "policy": Path(values["policy"]),
            "transition": Path(values["transition"]),
            "paper_workers": int(values.get("paper_workers") or PAPER_WORKERS),
            "option_workers": int(values.get("option_workers") or OPTION_WORKERS),
        }

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
            "'inflight','generation_submissions')}))\n"
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
        inputs = self.predecessor()
        prior_gate = read_json(inputs["gate"])
        transition = read_json(inputs["transition"])
        assert (
            sha256_file(Path(prior_gate["review_record"]))
            == prior_gate["review_record_sha256"]
        ), "the live gate's review record changed"
        assert prior_gate["budget_policy_sha256"] == sha256_file(inputs["policy"]), (
            "the live policy is not the one the live gate records"
        )

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
        # The runtime knows the queue and the completion date.
        constants = json.loads(
            self.runtime_python(
                "-c",
                "import json\n"
                "from arctic_qa import db, model_broker, paper_completion\n"
                "print(json.dumps({"
                "'queue_ceiling_seconds': "
                "model_broker.OPERATION_LOCK_QUEUE_CEILING_SECONDS,"
                "'queue_rounds': model_broker.OPERATION_LOCK_QUEUE_ROUNDS,"
                "'heartbeat_seconds': model_broker.OPERATION_LOCK_HEARTBEAT_SECONDS,"
                "'completion_column': ('paper_completions','completed_at_utc','TEXT')"
                " in db.ADDED_COLUMNS,"
                "'schema_version': db.SCHEMA_VERSION,"
                "'completion_schema': paper_completion.COMPLETION_SCHEMA,"
                "}))",
            ).stdout
        )
        assert constants["queue_ceiling_seconds"] >= 300, constants
        assert constants["completion_column"] is True, constants
        assert constants["schema_version"] == 5, constants
        suite = read_json(DATA / "suite-result.json")
        if suite["commit"] != self.short:
            changed = run(
                [
                    "git",
                    "-C",
                    str(WORKTREE),
                    "diff",
                    "--name-only",
                    suite["commit"],
                    self.commit,
                    "--",
                    "src",
                    "tests",
                    "config",
                    "schemas",
                ]
            ).stdout.strip()
            assert not changed, (
                f"the suite ran on {suite['commit']}, which differs in: {changed}"
            )
        assert suite["passed"] is True, suite
        assert suite.get("scope"), "the suite result must name its scope"

        self.review.write_text(
            "\n".join(
                [
                    f"# Chapter 3 lock-queue implementation review, commit {self.commit}",
                    "",
                    f"Successor of the reviewed gate `{inputs['gate']}`",
                    f"(commit {prior_gate['integrated_code_commit']}), for the",
                    f"continuation of run {RUN_ID} on campaign {CAMPAIGN}.",
                    "The run id, the campaign, the streaming input, the eligibility",
                    "inputs, the price config, the budget policy, the ledger",
                    "transition and the worker counts are unchanged. This activation",
                    "authorizes no new spend and needs no policy transition.",
                    "",
                    "## What changed",
                    "",
                    "- The exclusive broker operation lock is a queue for a",
                    "  concurrent construction request: it waits for the queue in",
                    "  front of it with a sanity ceiling of",
                    f"  {constants['queue_ceiling_seconds']:.0f} seconds and",
                    f"  {constants['queue_rounds']} rounds. A reached ceiling retries",
                    "  the same request key; only an exhausted last round raises",
                    "  `BrokerOperationBusyError`, which the producer still contains",
                    "  against one paper.",
                    "- The exclusive section holds the ledger mutation alone: the",
                    "  orphan recovery, the count registration, the reservation and",
                    "  the receipt it makes durable. The free token count, its",
                    "  retries and the pacing wait are outside it. Both were inside",
                    "  it before, which is what made the lock contended.",
                    "- Every wait and every hold is measured on stderr, with a",
                    f"  heartbeat every {constants['heartbeat_seconds']:.0f} seconds",
                    "  of waiting.",
                    "- The producer records the paper's completion time: the",
                    "  progress row carries `state_changed_at_utc` and the completion",
                    "  label carries `completed_at_utc`, a nullable column added",
                    "  under the current schema version",
                    f"  ({constants['schema_version']}).",
                    "- `repair-concurrency-faults` cleared the 19 papers the lock",
                    "  refused and filled 224 completion dates. No receipt and no",
                    "  ledger row was altered.",
                    "",
                    "## Test results on this commit",
                    "",
                    f"- scope: {suite['scope']}",
                    f"- {suite['summary']}",
                    f"- ruff check and ruff format --check: {suite['ruff']}",
                    "",
                    f"Task report: `research/{TASK}/report.md` on the branch.",
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
                "prior_activation_gate": str(inputs["gate"]),
                "prior_activation_gate_sha256": sha256_file(inputs["gate"]),
                "operation_lock_queue_ceiling_seconds": constants[
                    "queue_ceiling_seconds"
                ],
                "operation_lock_queue_rounds": constants["queue_rounds"],
                # The active policy transition binds the gate that authorized
                # it. This gate succeeds that one and names its review, so the
                # transition receipt stays exactly as it was written.
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
        write_json(self.gate, gate)
        os.chmod(self.gate, 0o444)
        before = self.validate_ledger(inputs["policy"], self.gate, inputs["transition"])
        assert before["integrity_valid"] is True, before
        assert before["halted"] is False, before
        self.write_launcher(inputs)
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
            "prior_gate": str(inputs["gate"]),
            "prior_gate_sha256": sha256_file(inputs["gate"]),
            "policy": str(inputs["policy"]),
            "policy_sha256": sha256_file(inputs["policy"]),
            "transition": str(inputs["transition"]),
            "transition_sha256": sha256_file(inputs["transition"]),
            "transition_applied_by_predecessor": True,
            "paper_workers": inputs["paper_workers"],
            "option_workers": inputs["option_workers"],
            "price_config_sha256": sha256_file(price_config),
            "constants": constants,
            "launcher": str(self.launcher),
            "ledger_at_prepare": {
                "ledger_sha256": sha256_file(LEDGER),
                "identity_sha256": sha256_file(IDENTITY),
                "result": before,
            },
            "prepared_at_utc": now(),
        }
        write_json(self.state, state)
        print(
            json.dumps(
                {k: state[k] for k in ("commit", "gate_sha256", "runtime_sha256")},
                indent=2,
            )
        )

    def write_launcher(self, inputs: dict) -> None:
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
                    f"release_transition={inputs['transition']}",
                    f"release_policy={inputs['policy']}",
                    "release_max_papers=${1:-" + str(MAX_PAPERS) + "}",
                    "release_paper_workers=${2:-" + str(inputs["paper_workers"]) + "}",
                    "release_option_workers=${3:-"
                    + str(inputs["option_workers"])
                    + "}",
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
                    '  --option-workers "$release_option_workers"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chmod(self.launcher, 0o555)

    # ---- stop and launch ---------------------------------------------------

    def stop(self) -> None:
        live = self.live_producer()
        if live is None:
            print("no live producer", file=sys.stderr)
        else:
            pid, _ = live
            # A request killed on the wire leaves a liability the next start
            # has to recover, so the stop waits for a moment with none of this
            # run's calls live. The evaluator's calls are not this run's.
            for _ in range(1200):
                if self.our_inflight(read_json(LEDGER)) == 0:
                    break
                time.sleep(0.5)
            assert self.our_inflight(read_json(LEDGER)) == 0, "no boundary was reached"
            print(f"stopping the live producer {pid}", file=sys.stderr)
            subprocess.run(["kill", str(pid)], check=False)
            for _ in range(120):
                if self.live_producer() is None:
                    break
                time.sleep(1)
            assert self.live_producer() is None, "the live producer did not stop"
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)

    def launch(self) -> None:
        state = read_json(self.state)
        assert self.live_producer() is None, "a producer of this run is already live"
        ledger = read_json(LEDGER)
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
            "submissions_at_launch": self.our_submissions(ledger),
            "evaluator_stopped": False,
            "evaluator_note": (
                "The start applies no policy transition, so the evaluator was "
                "left running."
            ),
        }
        write_json(self.state, state)
        print(json.dumps(state["launch"], indent=2))

    # ---- observe -----------------------------------------------------------

    def sample(self) -> dict:
        ledger = read_json(LEDGER)
        progress = read_json(PROGRESS) if PROGRESS.is_file() else {}
        jobs = self.eligibility_dir / "jobs"
        return {
            "at_utc": now(),
            "our_submissions": self.our_submissions(ledger),
            "our_inflight": self.our_inflight(ledger),
            "spent_usd": ledger["spent_usd"],
            "halted": ledger["halted"],
            "screened_papers": len(list(jobs.glob("*.json"))) if jobs.is_dir() else 0,
            "counts": progress.get("counts", {}),
            "message": progress.get("message"),
            "live": self.live_producer() is not None,
        }

    def observe(self, seconds: int = 900) -> None:
        state = read_json(self.state)
        first = self.sample()
        samples = [first]
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(60)
            samples.append(self.sample())
            print(json.dumps(samples[-1]), flush=True)
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
            "candidate_processing_fault": last["counts"].get(
                "candidate_processing_fault", 0
            )
            - first["counts"].get("candidate_processing_fault", 0),
            "first": first,
            "last": last,
        }
        state["observation"] = {"summary": summary, "samples": samples}
        write_json(self.state, state)
        print(json.dumps(summary, indent=2))


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: build-busy.py <commit> <action> [seconds]")
    activation = Activation(sys.argv[1])
    action = sys.argv[2]
    if action == "prepare":
        activation.prepare()
    elif action == "stop":
        activation.stop()
    elif action == "launch":
        activation.launch()
    elif action == "observe":
        activation.observe(int(sys.argv[3]) if len(sys.argv) > 3 else 900)
    else:
        raise SystemExit(f"unknown action: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
