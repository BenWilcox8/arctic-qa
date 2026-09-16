"""Build and cut over the chapter 3 paper-completion activation set.

Usage (from the task worktree; the runtime's own devshell runs every check,
the batch label and the producer):

    python build-completion.py <commit> prepare
    python build-completion.py <commit> dry-run
    python build-completion.py <commit> launch
    python build-completion.py <commit> observe [seconds]

The shape follows ``build-concurrency.py`` of ``arctic-ch3-paper-concurrency-r1``.
This activation differs in these ways:

- The run continues: same run id, same campaign, same streaming input, same
  eligibility run directory, same budget policy, same ledger transition and
  the same worker counts as the concurrency activation it succeeds. No policy
  field moves, so no ledger transition is written and no new spend is
  authorized.
- The producer labels each paper it finishes in ``paper_completions`` of the
  state database and skips a labelled paper at startup before it reads one
  receipt of it. ``launch`` applies the one-time batch label while the
  producer is stopped, then starts the new one.
- The launcher names the commit with ``--code-commit``, because a runtime
  snapshot is a ``git archive`` and ``git rev-parse`` inside it answers with
  the parent repository's commit.

``prepare`` reads only. ``dry-run`` reads only and writes one report file.
``launch`` stops the running producer at a zero-in-flight boundary, applies
the batch label, starts the new producer and measures launch-to-first-paid-
call. ``observe`` reads only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

TASK = "arctic-ch3-paper-completion-r1"
FM_DATA = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data")
DATA = FM_DATA / TASK
CONCURRENCY = FM_DATA / "arctic-ch3-paper-concurrency-r1"
WORKTREE = Path("/home/ben/.treehouse/arctic-qa-e841b2/38/arctic-qa")
NAMESPACE = Path("/mnt/crdata/research-abstention/arctic-qa")
DATA_ROOT = NAMESPACE.parent
CHAPTER3_ROOT = NAMESPACE / "chapter3"
STREAMING = NAMESPACE / "streaming-dataset-r1"
LEDGER = STREAMING / "shared-paid-call-ledger.json"
RECEIPTS = STREAMING / "model-receipts"
IDENTITY = LEDGER.with_name(f".{LEDGER.name}.identity.json")
PROGRESS = STREAMING / "progress.json"
STATE_DB = NAMESPACE / "state.sqlite3"
CREDENTIAL = Path("/home/ben/.config/arctic-qa/gemini-api-key")
ELIGIBILITY_POLICY = NAMESPACE / "corpus-search-r1" / "protocol" / "protocol-v3.json"

TMUX_SESSION = "arctic-ch3-production-r1"
MAX_PAPERS = 4420
CAMPAIGN = "arctic-qa-production-campaign-003"
RUN_ID = os.environ.get("CH3_RUN_ID", "chapter3-7dc6485-r3")
STREAM_INPUT_RUN_ID = "chapter3-7dc6485-r1-input"
# The producer replayed for about 22 minutes before its first paid call on
# every relaunch of 2026-09-16. The label removes that replay, so the wait is
# a bound on the measurement, not an expectation.
FIRST_CALL_WAIT_SECONDS = 2700
ELIGIBILITY_PROMPT = "config/gemini-eligibility-prompt-v8.txt"
ELIGIBILITY_SCHEMA = "schemas/gemini-eligibility.v4.schema.json"
RESCREEN_PROMPT = "config/gemini-eligibility-geography-rescreen-v2.txt"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
        self.run_id = RUN_ID
        self.archive = DATA / f"source-{self.short}-{TASK}.tar"
        self.runtime = DATA / "runtime" / f"app-{self.short}-{TASK}"
        self.review = DATA / f"implementation-review-{self.short}-ch3pc.md"
        self.gate = DATA / f"live-execution-gate-{self.short}-ch3pc.json"
        self.launcher = DATA / f"launcher-{self.short}-ch3pc.sh"
        self.log = DATA / f"launcher-{self.short}-ch3pc.log"
        self.state = DATA / f"activation-state-{self.short}.json"
        self.stream_input = CHAPTER3_ROOT / "streaming-input" / STREAM_INPUT_RUN_ID
        self.eligibility_dir = CHAPTER3_ROOT / "gemini-eligibility" / self.run_id

    # ---- the live producer ------------------------------------------------

    @staticmethod
    def live_producer() -> tuple[int, list[str]] | None:
        """Return the running producer's pid and its argument vector.

        The run id alone does not name the producer: ``label-completed-papers``
        takes the same ``--run-id``, so a match on the run id finds this
        script's own label subprocess and would stop it as if it were the
        producer. The subcommand is what tells them apart.
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
    def live_argument(arguments: list[str], flag: str, default: str | None = None):
        if flag not in arguments:
            if default is None:
                raise SystemExit(f"the live producer names no {flag}")
            return default
        return arguments[arguments.index(flag) + 1]

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

    @staticmethod
    def our_inflight(ledger: dict) -> int:
        """Count the requests of THIS run that are on the wire.

        ``inflight`` counts every caller of the shared ledger, and the
        benchmark evaluator keeps calling. A producer is stopped safely when
        none of ITS requests is submitted, whatever the evaluator is doing.
        """
        return sum(
            1
            for request in ledger.get("requests", {}).values()
            if request.get("state") == "submitted"
            and str(request.get("run_id")) == RUN_ID
        )

    @staticmethod
    def wait_for_settled_ledger(timeout: float = 600.0) -> dict:
        """Wait for a moment with nothing in flight, and return the ledger.

        An applied policy transition is validated again on every broker
        construction, and that validation refuses a ledger with a request in
        flight ("a configuration transition requires a settled ledger"). The
        benchmark evaluator shares this ledger and keeps calling, so the
        producer can only start inside one of its gaps. Measured on
        2026-09-16: the ledger was settled about half the time, in windows of
        about thirty seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            ledger = read_json(LEDGER)
            if int(ledger["inflight"]) == 0 and not ledger["halted"]:
                return ledger
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"the ledger stayed busy for {timeout:.0f}s; "
                    f"inflight {ledger['inflight']}, halted {ledger['halted']}"
                )
            time.sleep(1)

    def validate_ledger(self, policy: Path, gate: Path, transition: Path) -> dict:
        """Construct the runtime's broker once: it validates every event.

        The construction is attempted inside a settled window, and a refusal
        that names the unsettled ledger is retried rather than reported: it is
        the evaluator's call in flight, never this activation.
        """
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
            "'config_transition_sha256','price_config_sha256','policy_sha256',"
            "'limits','usage','remaining','count_requests','inflight',"
            "'generation_submissions')}))\n"
        )
        for _ in range(30):
            self.wait_for_settled_ledger()
            result = self.runtime_python("-c", script, check=False)
            if result.returncode == 0:
                return json.loads(result.stdout.strip().splitlines()[-1])
            if "requires a settled ledger" not in result.stderr:
                raise SystemExit(
                    "the runtime refuses the ledger:\n" + result.stderr[-6000:]
                )
        raise SystemExit("every settled window was lost to a concurrent call")

    # ---- the run this succeeds ------------------------------------------

    def live_inputs(self) -> dict:
        """Return the gate, policy, transition and workers of the run this succeeds.

        A running producer names them itself. A producer that exited names
        them nowhere, so they come from the environment instead; the run
        continues under the last authorized gate either way.
        """
        live = self.live_producer()
        if live is not None:
            _, arguments = live
            return {
                "gate": Path(self.live_argument(arguments, "--execution-gate-file")),
                "policy": Path(
                    self.live_argument(arguments, "--streaming-budget-policy-file")
                ),
                "transition": Path(
                    self.live_argument(arguments, "--ledger-config-transition-file")
                ),
                "paper_workers": int(
                    self.live_argument(arguments, "--paper-workers", "1")
                ),
                "option_workers": int(
                    self.live_argument(arguments, "--option-workers", "1")
                ),
            }
        gate = os.environ.get("CH3_LIVE_GATE")
        transition = os.environ.get("CH3_LIVE_TRANSITION")
        policy = os.environ.get("CH3_LIVE_POLICY")
        assert gate and transition and policy, (
            "no producer runs; name CH3_LIVE_GATE, CH3_LIVE_TRANSITION and "
            "CH3_LIVE_POLICY"
        )
        return {
            "gate": Path(gate),
            "policy": Path(policy),
            "transition": Path(transition),
            "paper_workers": int(os.environ.get("CH3_PAPER_WORKERS", "4")),
            "option_workers": int(os.environ.get("CH3_OPTION_WORKERS", "4")),
        }

    # ---- prepare ----------------------------------------------------------

    def prepare(self) -> None:
        inputs = self.live_inputs()
        prior_gate = read_json(inputs["gate"])
        transition_authorization = read_json(inputs["transition"])
        assert (
            sha256_file(Path(transition_authorization["review_record"]))
            == transition_authorization["review_record_sha256"]
        ), "the active transition's review record changed"
        assert (
            sha256_file(Path(prior_gate["review_record"]))
            == prior_gate["review_record_sha256"]
        ), "the live gate's review record changed"
        assert prior_gate["budget_policy_sha256"] == sha256_file(inputs["policy"]), (
            "the live policy is not the one the live gate records"
        )

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
        for name in (ELIGIBILITY_PROMPT, ELIGIBILITY_SCHEMA, RESCREEN_PROMPT):
            key = {
                ELIGIBILITY_PROMPT: "eligibility_prompt_sha256",
                ELIGIBILITY_SCHEMA: "eligibility_schema_sha256",
                RESCREEN_PROMPT: "eligibility_rescreen_prompt_sha256",
            }[name]
            assert sha256_file(self.runtime / name) == prior_gate[key], name
        # The runtime knows the label: the table, the command and the skip.
        constants = json.loads(
            self.runtime_python(
                "-c",
                "import json\n"
                "from arctic_qa import paper_completion, streaming, db\n"
                "print(json.dumps({"
                "'completion_schema': paper_completion.COMPLETION_SCHEMA,"
                "'outcome_classes': list(paper_completion.OUTCOME_CLASSES),"
                "'table_in_schema': 'paper_completions' in db.SCHEMA,"
                "'schema_version': db.SCHEMA_VERSION,"
                "'stored_outcome_owner': streaming._stored_generation_outcome.__name__,"
                "}))",
            ).stdout
        )
        assert constants["table_in_schema"] is True, constants
        assert constants["schema_version"] == 5, constants
        suite = read_json(DATA / "suite-result.json")
        if suite["commit"] != self.short:
            # A later commit that touches only the task record does not
            # invalidate the suite. Prove the tested code is this code.
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
        # The review record names exactly which tests ran. A launch ordered
        # before the whole suite says so, and the whole suite follows it.
        assert suite.get("scope"), "the suite result must name its scope"

        self.review.write_text(
            "\n".join(
                [
                    f"# Chapter 3 paper completion implementation review, commit {self.commit}",
                    "",
                    f"Successor of the reviewed gate `{inputs['gate']}`",
                    f"(commit {prior_gate['integrated_code_commit']}), for the",
                    f"continuation of run {self.run_id} on campaign {CAMPAIGN}.",
                    "The run id, the campaign, the streaming input, the eligibility",
                    "inputs, the price config, the budget policy, the ledger",
                    "transition and the worker counts are unchanged.",
                    "",
                    "## What changed",
                    "",
                    "- A paper the run finished carries a completion label in",
                    "  `paper_completions` of the state database, keyed by the",
                    "  invocation run id and the candidate key. The producer writes",
                    "  it the moment a paper reaches a terminal outcome.",
                    "- At startup the producer skips a labelled paper before it reads",
                    "  one receipt of it. A paper without a label keeps today's walk.",
                    "- `label-completed-papers` labels every analyzed paper of the run",
                    "  in one transaction, from stored state alone. The rule is the",
                    '  "Paper completion labels" section of `docs/STREAMING_DATASET.md`.',
                    "- The terminal rungs of the generation ladder moved out of",
                    "  `_progress_generation` into `_stored_generation_outcome`, so the",
                    "  producer and the label read one owner of that rule.",
                    "- No receipt is altered or deleted. No policy field moves. This",
                    "  activation authorizes no new spend.",
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
                "paper_completion_schema": constants["completion_schema"],
                # The active policy transition binds the gate that authorized
                # it. This gate succeeds that one and names its review, so the
                # transition receipt stays exactly as it was written
                # (``model_broker._gate_succeeds_transition_review``).
                "supersedes_config_transition_review": {
                    field: transition_authorization[field]
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
        state = {
            "task": TASK,
            "commit": self.commit,
            "run_id": self.run_id,
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
            "paper_workers": inputs["paper_workers"],
            "option_workers": inputs["option_workers"],
            "price_config_sha256": sha256_file(price_config),
            "constants": constants,
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

    # ---- the batch label --------------------------------------------------

    def label(self, *, apply: bool, stamp: str) -> dict:
        """Run the batch through the runtime, on the live state database."""
        output = DATA / f"label-{'apply' if apply else 'dry-run'}-{stamp}.json"
        result = self.runtime_python(
            "-m",
            "arctic_qa",
            "--json",
            "--data-root",
            str(DATA_ROOT),
            "label-completed-papers",
            "--run-id",
            self.run_id,
            "--campaign-id",
            CAMPAIGN,
            "--access-run-dir",
            str(self.stream_input),
            "--eligibility-run-dir",
            str(self.eligibility_dir),
            "--eligibility-prompt-file",
            ELIGIBILITY_PROMPT,
            "--eligibility-schema-file",
            ELIGIBILITY_SCHEMA,
            "--eligibility-rescreen-prompt-file",
            RESCREEN_PROMPT,
            "--eligibility-policy-file",
            str(ELIGIBILITY_POLICY),
            "--code-commit",
            self.short,
            "--output-file",
            str(output),
            *(["--apply"] if apply else []),
        )
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        summary["report_file"] = str(output)
        return summary

    def dry_run(self) -> None:
        state = read_json(self.state)
        summary = self.label(apply=False, stamp=now().replace(":", ""))
        state.setdefault("dry_runs", []).append(summary)
        write_json(self.state, state)
        print(json.dumps(summary, indent=2))

    # ---- launch -----------------------------------------------------------

    def write_launcher(self, state: dict) -> None:
        self.launcher.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "",
                    f"release_runtime={self.runtime}",
                    f"release_gate={self.gate}",
                    f"release_transition={state['transition']}",
                    f"release_policy={state['policy']}",
                    f"release_max_papers=${{1:-{MAX_PAPERS}}}",
                    f"release_paper_workers=${{2:-{state['paper_workers']}}}",
                    f"release_option_workers=${{3:-{state['option_workers']}}}",
                    "",
                    'cd "$release_runtime"',
                    'exec nix develop "path:$release_runtime" -c env \\',
                    '  "PYTHONPATH=$release_runtime/src" \\',
                    "  python -m arctic_qa --json \\",
                    f"  --data-root {DATA_ROOT} \\",
                    "  stream \\",
                    "  --phase away_production \\",
                    f"  --run-id {self.run_id} \\",
                    f"  --campaign-id {CAMPAIGN} \\",
                    f"  --access-run-dir {self.stream_input} \\",
                    f"  --eligibility-run-dir {self.eligibility_dir} \\",
                    f"  --eligibility-prompt-file {ELIGIBILITY_PROMPT} \\",
                    f"  --eligibility-schema-file {ELIGIBILITY_SCHEMA} \\",
                    f"  --eligibility-rescreen-prompt-file {RESCREEN_PROMPT} \\",
                    f"  --eligibility-policy-file {ELIGIBILITY_POLICY} \\",
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

    def launch(self) -> None:
        state = read_json(self.state)
        assert "launch" not in state, "this activation was launched"
        live = self.live_producer()
        if live is not None:
            pid, _ = live
            # Stop at a zero-in-flight boundary: a request killed on the wire
            # leaves a liability the next start has to recover. Wait for the
            # boundary first, then stop at once.
            for _ in range(600):
                ledger = read_json(LEDGER)
                if self.our_inflight(ledger) == 0:
                    break
                time.sleep(0.5)
            ledger = read_json(LEDGER)
            assert self.our_inflight(ledger) == 0, (
                "no zero-in-flight boundary for this run; "
                f"{self.our_inflight(ledger)} submitted"
            )
            print(f"stopping the live producer {pid}", file=sys.stderr)
            subprocess.run(["kill", str(pid)], check=False)
            for _ in range(120):
                if self.live_producer() is None:
                    break
                time.sleep(1)
            assert self.live_producer() is None, "the live producer did not stop"
        stopped_at = now()
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
        ledger = read_json(LEDGER)
        assert not ledger["halted"], "the ledger is halted"
        # In-flight requests here are the benchmark evaluator's: it shares this
        # ledger and keeps calling. What this launch needs is that OUR producer
        # is stopped, which is checked above. The label touches the state
        # database alone, and every broker construction below waits for its own
        # settled window.
        # The one-time batch label, while nothing writes the table.
        stamp = stopped_at.replace(":", "")
        dry = self.label(apply=False, stamp=stamp)
        applied = self.label(apply=True, stamp=stamp)
        assert applied["written"] == dry["counts"]["to_label"], (dry, applied)
        self.write_launcher(state)
        before = self.validate_ledger(
            Path(state["policy"]), self.gate, Path(state["transition"])
        )
        assert before["integrity_valid"] is True and before["halted"] is False, before
        gate = read_json(self.gate)
        gate["activation_state"] = "started"
        os.chmod(self.gate, 0o644)
        write_json(self.gate, gate)
        os.chmod(self.gate, 0o444)
        after_gate = self.validate_ledger(
            Path(state["policy"]), self.gate, Path(state["transition"])
        )
        assert after_gate["integrity_valid"] is True, after_gate
        # The producer validates the applied transition when it constructs its
        # broker, and that refuses a ledger with a request in flight. The
        # evaluator shares the ledger, so the start is aimed at a settled
        # window and retried when it loses the race.
        launched_at = None
        launch_clock = None
        for attempt in range(1, 21):
            self.wait_for_settled_ledger()
            launched_at = now()
            launch_clock = time.monotonic()
            log_before = self.log.stat().st_size if self.log.exists() else 0
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
            live = None
            for _ in range(45):
                live = self.live_producer()
                if live is not None:
                    break
                time.sleep(1)
            if live is not None:
                # Past construction: the process is up and the broker accepted
                # the ledger. Give it a moment to prove it stays up.
                time.sleep(20)
                live = self.live_producer()
            if live is not None:
                print(f"the producer started on attempt {attempt}", file=sys.stderr)
                break
            tail = ""
            if self.log.exists():
                with self.log.open("rb") as handle:
                    handle.seek(log_before)
                    tail = handle.read().decode(errors="replace")
            subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
            if "requires a settled ledger" not in tail:
                raise SystemExit(f"the producer did not start:\n{tail[-4000:]}")
            print(
                f"attempt {attempt} lost the settled window; retrying",
                file=sys.stderr,
            )
        assert live is not None, f"the producer did not start; see {self.log}"
        pid, _ = live
        state["launch"] = {
            "pid": pid,
            "launcher": str(self.launcher),
            "log": str(self.log),
            "paper_workers": state["paper_workers"],
            "option_workers": state["option_workers"],
            "tmux_session": TMUX_SESSION,
            "stopped_prior_at_utc": stopped_at,
            "batch_dry_run": dry,
            "batch_applied": applied,
            "ledger_before_launch": before,
            "launched_at_utc": launched_at,
        }
        write_json(self.state, state)
        print(json.dumps({"pid": pid, "launcher": str(self.launcher)}, indent=2))
        # Launch to first paid call: the number the label is for.
        base = int(before["generation_submissions"])
        waited = 0.0
        while waited < FIRST_CALL_WAIT_SECONDS:
            ledger = read_json(LEDGER)
            if int(ledger["generation_submissions"]) > base:
                break
            if self.live_producer() is None:
                raise SystemExit(f"the producer exited before a paid call; see {self.log}")
            time.sleep(2)
            waited = time.monotonic() - launch_clock
        elapsed = time.monotonic() - launch_clock
        progress = read_json(PROGRESS) if PROGRESS.is_file() else {}
        state["launch"]["first_paid_call"] = {
            "seen": waited < FIRST_CALL_WAIT_SECONDS,
            "seconds_after_launch": round(elapsed, 1),
            "at_utc": now(),
            "generation_submissions_before": base,
            "generation_submissions_after": int(read_json(LEDGER)["generation_submissions"]),
            "progress_counts": progress.get("counts"),
            "progress_message": progress.get("message"),
        }
        write_json(self.state, state)
        print(json.dumps(state["launch"]["first_paid_call"], indent=2))

    # ---- observe ----------------------------------------------------------

    def observe(self, seconds: int = 600) -> None:
        state = read_json(self.state)
        samples = [self.sample()]
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            time.sleep(30)
            samples.append(self.sample())
            print(json.dumps(samples[-1]), flush=True)
        state.setdefault("observations", []).append(
            {"seconds": seconds, "samples": samples}
        )
        write_json(self.state, state)

    def sample(self) -> dict:
        ledger = read_json(LEDGER)
        progress = read_json(PROGRESS) if PROGRESS.is_file() else {}
        counts = progress.get("counts", {})
        return {
            "at": now(),
            "alive": self.live_producer() is not None,
            "spent_usd": str(ledger["spent_usd"]),
            "generation_submissions": int(ledger["generation_submissions"]),
            "inflight": int(ledger["inflight"]),
            "halted": bool(ledger["halted"]),
            "accepted": int(counts.get("accepted_qa", 0)),
            "processed": int(counts.get("eligibility_completed", 0)),
            "message": progress.get("message"),
        }


if __name__ == "__main__":
    commit, action, *rest = sys.argv[1:]
    activation = Activation(commit)
    if action == "prepare":
        activation.prepare()
    elif action == "dry-run":
        activation.dry_run()
    elif action == "launch":
        activation.launch()
    elif action == "observe":
        activation.observe(int(rest[0]) if rest else 600)
    else:
        raise SystemExit(f"unknown action: {action}")
