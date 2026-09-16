"""Build and cut over the chapter 3 paper-concurrency activation set.

Usage (from the task worktree; the runtime's own devshell runs every check,
the transition and the producer):

    python build-concurrency.py <commit> prepare
    python build-concurrency.py <commit> apply-transition
    python build-concurrency.py <commit> launch
    python build-concurrency.py <commit> observe [seconds]

The shape follows ``build-expansion.py`` of ``arctic-ch3-expansion-200-r1``.
This activation differs in these ways:

- The run continues: same run id, same campaign, same streaming input, same
  eligibility run directory, no replay.
- One chained v2 policy transition moves the two request-rate limits of the
  policy and nothing else: the concurrency slots from 2 to 8 and the minute
  window from 10 to 40 (``model_broker.CHAPTER3_CONCURRENCY_CHANGE``). Every
  money ceiling, the per-request cap, the paper cost cap and every project
  design count stay exactly as the expansion left them.
- The producer runs several papers at once: ``--paper-workers`` papers in
  flight, each on its own thread, and ``--option-workers`` option verdicts in
  flight inside one paper.

``prepare`` reads only. ``apply-transition`` writes one immutable transition
event and makes no paid call. ``launch`` stops the running producer at a
zero-in-flight boundary and starts the new one. ``observe`` reads only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

TASK = "arctic-ch3-paper-concurrency-r1"
FM_DATA = Path("/home/ben/.treehouse/firstmate-c40011/6/firstmate/data")
DATA = FM_DATA / TASK
EXPANSION = FM_DATA / "arctic-ch3-expansion-200-r1"
WORKTREE = Path("/home/ben/.treehouse/arctic-qa-e841b2/33/arctic-qa")
NAMESPACE = Path("/mnt/crdata/research-abstention/arctic-qa")
CHAPTER3_ROOT = NAMESPACE / "chapter3"
STREAMING = NAMESPACE / "streaming-dataset-r1"
LEDGER = STREAMING / "shared-paid-call-ledger.json"
LEDGER_STATUS = STREAMING / "shared-paid-call-ledger.status.json"
RECEIPTS = STREAMING / "model-receipts"
IDENTITY = LEDGER.with_name(f".{LEDGER.name}.identity.json")
PROGRESS = STREAMING / "progress.json"
STATE_DB = NAMESPACE / "state.sqlite3"
CREDENTIAL = Path("/home/ben/.config/arctic-qa/gemini-api-key")

POLICY_V10 = EXPANSION / "streaming-dataset-budget-policy-v10-chapter3-expansion.json"
POLICY_V11 = DATA / "streaming-dataset-budget-policy-v11-chapter3-concurrency.json"

TMUX_SESSION = "arctic-ch3-production-r1"
MAX_PAPERS = 4420
CAMPAIGN = "arctic-qa-production-campaign-003"
RUN_ID = os.environ.get("CH3_RUN_ID", "chapter3-7dc6485-r3")
STREAM_INPUT_RUN_ID = "chapter3-7dc6485-r1-input"
CEILING = "253.990121"
# Every relaunch of 2026-09-16 spent about 22 minutes in free replay
# before its first paid call, so the measurement waits that out.
REPLAY_WAIT_SECONDS = 2700
PAPER_WORKERS = int(os.environ.get("CH3_PAPER_WORKERS", "4"))
OPTION_WORKERS = int(os.environ.get("CH3_OPTION_WORKERS", "4"))
CHANGED_FIELDS = {
    "maximum_concurrent_generation_requests": {"from": 2, "to": 8},
    "maximum_generation_requests_per_minute": {"from": 10, "to": 40},
}
ELIGIBILITY_PROMPT = "config/gemini-eligibility-prompt-v8.txt"
ELIGIBILITY_SCHEMA = "schemas/gemini-eligibility.v4.schema.json"
RESCREEN_PROMPT = "config/gemini-eligibility-geography-rescreen-v2.txt"
REASON = (
    "Chapter 3 paper concurrency (captain order 2026-09-16 21:20 UTC): the "
    "producer runs several papers at once, each paper on its own thread, so "
    "the request rate the run can reach is no longer one call at a time. The "
    "two request-rate limits move together and nothing else moves: the "
    "concurrency slots from 2 to 8 and the minute window from 10 to 40. Every "
    "money ceiling, the per-request cap, the paper cost cap and every project "
    "design count stay exactly as the USD 200 expansion left them, so this "
    "transition authorizes no new spend and keeps the same cumulative tranche "
    f"of USD {CEILING}. Halt at exhaustion; no replay; no retry; no budget "
    "reset."
)


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


def canonical_sha256(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


class Activation:
    def __init__(self, commit: str) -> None:
        self.commit = run(
            ["git", "-C", str(WORKTREE), "rev-parse", commit]
        ).stdout.strip()
        self.short = self.commit[:7]
        self.run_id = RUN_ID
        self.archive = DATA / f"source-{self.short}-{TASK}.tar"
        self.runtime = DATA / "runtime" / f"app-{self.short}-{TASK}"
        self.review = DATA / f"implementation-review-{self.short}-ch3c.md"
        self.gate = DATA / f"live-execution-gate-{self.short}-ch3c.json"
        self.ledger_transition = (
            DATA / f"ledger-config-transition-{self.short}-ch3c.json"
        )
        self.launcher = DATA / f"launcher-{self.short}-ch3c.sh"
        self.log = DATA / f"launcher-{self.short}-ch3c.log"
        self.state = DATA / f"activation-state-{self.short}.json"
        self.receipt = DATA / f"activation-receipt-{self.short}-ch3c.json"
        self.stream_input = CHAPTER3_ROOT / "streaming-input" / STREAM_INPUT_RUN_ID
        self.eligibility_dir = CHAPTER3_ROOT / "gemini-eligibility" / self.run_id

    # ---- the live producer ------------------------------------------------

    @staticmethod
    def live_producer() -> tuple[int, list[str]] | None:
        """Return the running producer's pid and its argument vector."""
        marker = f"--run-id\0{RUN_ID}\0"
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                raw = (proc / "cmdline").read_bytes().decode()
            except OSError:
                continue
            if "arctic_qa" in raw and marker in raw:
                return int(proc.name), raw.split("\0")[:-1]
        return None

    @staticmethod
    def live_argument(arguments: list[str], flag: str) -> str:
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

    def validate_ledger(self, policy: Path, gate: Path, transition: Path) -> dict:
        """Construct the runtime's broker once: it validates every event."""
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
            "'limits','usage','remaining','count_requests')}))\n"
        )
        result = self.runtime_python("-c", script, check=False)
        if result.returncode != 0:
            raise SystemExit(
                "the runtime refuses the ledger:\n" + result.stderr[-6000:]
            )
        return json.loads(result.stdout.strip().splitlines()[-1])

    # ---- prepare ----------------------------------------------------------

    def prepare(self) -> None:
        live = self.live_producer()
        assert live is not None, "no producer runs; name the live gate by hand"
        _, arguments = live
        live_gate = Path(self.live_argument(arguments, "--execution-gate-file"))
        live_policy = Path(
            self.live_argument(arguments, "--streaming-budget-policy-file")
        )
        live_transition = Path(
            self.live_argument(arguments, "--ledger-config-transition-file")
        )
        assert live_policy == POLICY_V10, live_policy
        prior_gate = read_json(live_gate)
        assert (
            sha256_file(Path(prior_gate["review_record"]))
            == prior_gate["review_record_sha256"]
        ), "the live gate's review record changed"

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
        # The v11 policy is the v10 policy with exactly the two request-rate
        # fields changed. Nothing else may move.
        v10 = read_json(POLICY_V10)
        v11 = read_json(POLICY_V11)
        expected = dict(v10)
        for field, limits in CHANGED_FIELDS.items():
            assert v10[field] == limits["from"], field
            expected[field] = limits["to"]
        assert v11 == expected, "the v11 policy changes more than the request rate"
        constants = json.loads(
            self.runtime_python(
                "-c",
                "import json\nfrom arctic_qa import model_broker\n"
                "print(json.dumps({"
                "'chapter3_concurrency_change': model_broker.CHAPTER3_CONCURRENCY_CHANGE,"
                "'allowed_request_rates': [list(pair) for pair in model_broker.ALLOWED_REQUEST_RATES],"
                "'chapter3_expansion_cumulative_ceiling_usd': str(model_broker.CHAPTER3_EXPANSION_CUMULATIVE_CEILING_USD),"
                "}))",
            ).stdout
        )
        assert constants["chapter3_concurrency_change"] == CHANGED_FIELDS, constants
        assert [8, 40] in constants["allowed_request_rates"], constants
        assert constants["chapter3_expansion_cumulative_ceiling_usd"] == CEILING
        suite = read_json(DATA / "suite-result.json")
        assert suite["commit"] == self.short, suite["commit"]

        self.review.write_text(
            "\n".join(
                [
                    f"# Chapter 3 paper concurrency implementation review, commit {self.commit}",
                    "",
                    f"Successor of the reviewed gate `{live_gate}`",
                    f"(commit {prior_gate['integrated_code_commit']}), for the",
                    f"continuation of run {self.run_id} on campaign {CAMPAIGN}.",
                    "The run id, the campaign, the streaming input, the eligibility",
                    "inputs and the price config are unchanged.",
                    "",
                    "## What changed",
                    "",
                    "- `run_stream` runs several papers at once. Each paper keeps the",
                    "  whole existing chain, on its own thread. The frozen selection",
                    "  order is the pick-up order; the run result is sorted back into",
                    "  that order, so the completion order is not visible in it.",
                    "- The option verifier calls of one paper run in waves. The",
                    "  rank-order stop of `docs/STREAMING_DATASET.md` holds at the wave",
                    "  boundary: a wave is never wider than the options still needed.",
                    "- The broker admits one request at a time and releases the",
                    "  admission and the exclusive operation lock before the live call,",
                    "  which is the shape the evaluation phase already had. Every",
                    "  ledger write stays under the ledger lock, and orphan recovery",
                    "  still skips a request whose in-flight lock is held.",
                    "- The sqlite state database serialises every statement and every",
                    "  transaction behind one reentrant lock.",
                    "- The policy transition moves the two request-rate limits and",
                    "  nothing else:",
                    "  `maximum_concurrent_generation_requests` 2 to 8 and",
                    "  `maximum_generation_requests_per_minute` 10 to 40. It authorizes",
                    f"  no new spend and keeps the cumulative tranche of USD {CEILING}.",
                    "",
                    "## Test results on this commit",
                    "",
                    *[f"- {name}: {result}" for name, result in suite["parts"].items()],
                    f"- ruff check and ruff format --check: {suite['ruff']}",
                    "",
                    f"Task report: `data/{TASK}/report.md` on the branch.",
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
                # The gate records the policy this activation authorizes. The
                # broker reads the policy file itself, so these fields are the
                # record, not the control.
                "budget_policy_file": str(POLICY_V11),
                "budget_policy_sha256": sha256_file(POLICY_V11),
                "prior_budget_policy_file": str(POLICY_V10),
                "prior_budget_policy_sha256": sha256_file(POLICY_V10),
                "policy_changed_fields": CHANGED_FIELDS,
                "ledger_config_transition_file": str(self.ledger_transition),
                "paper_workers": PAPER_WORKERS,
                "option_workers": OPTION_WORKERS,
                # This gate authorizes the concurrency transition itself, and
                # names the gate the active expansion event was validated under.
                "supersedes_config_transition_review": {
                    "execution_gate_sha256": sha256_file(live_gate),
                    "integrated_code_commit": prior_gate["integrated_code_commit"],
                    "review_record": prior_gate["review_record"],
                    "review_record_sha256": prior_gate["review_record_sha256"],
                },
                "activation_state": "authorized_not_started",
            }
        )
        write_json(self.gate, gate)
        os.chmod(self.gate, 0o444)
        before = self.validate_ledger(POLICY_V10, self.gate, live_transition)
        assert before["integrity_valid"] is True, before
        assert before["halted"] is False, before
        assert before["limits"]["away_session_total_ceiling_usd"] == CEILING
        assert before["limits"]["maximum_concurrent_generation_requests"] == 2
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
            "prior_gate": str(live_gate),
            "prior_gate_sha256": sha256_file(live_gate),
            "prior_transition": str(live_transition),
            "prior_transition_sha256": sha256_file(live_transition),
            "price_config_sha256": sha256_file(price_config),
            "constants": constants,
            "ledger_before_transition": {
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

    # ---- transition -------------------------------------------------------

    def find_event(self, authorization: dict) -> Path:
        path = RECEIPTS / f"config-transition-{canonical_sha256(authorization)}.json"
        assert path.is_file(), path
        return path

    def apply_transition(self) -> None:
        state = read_json(self.state)
        assert "transition" not in state, "the transition is applied"
        ledger = read_json(LEDGER)
        assert not ledger["halted"] and ledger["inflight"] == 0, "the ledger is busy"
        price_config = self.runtime / "config" / "gemini-eligibility-v1.json"
        prior_event = RECEIPTS / (
            f"config-transition-{state['ledger_before_transition']['result']['config_transition_sha256']}.json"
        )
        assert prior_event.is_file(), prior_event
        authorization = {
            "schema": "shared-paid-call-config-transition-v2",
            "ledger_file": str(LEDGER),
            "from_price_config_sha256": sha256_file(price_config),
            "to_price_config_sha256": sha256_file(price_config),
            "from_config_transition_sha256": sha256_file(prior_event),
            "from_policy_file": str(POLICY_V10),
            "from_policy_sha256": sha256_file(POLICY_V10),
            "to_policy_sha256": sha256_file(POLICY_V11),
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
        after = self.validate_ledger(POLICY_V11, self.gate, self.ledger_transition)
        assert after["integrity_valid"] is True, after
        assert after["halted"] is False, after
        event = self.find_event(authorization)
        assert after["config_transition_sha256"] == sha256_file(event)
        assert after["limits"]["maximum_concurrent_generation_requests"] == 8
        assert after["limits"]["maximum_generation_requests_per_minute"] == 40
        assert after["limits"]["away_session_total_ceiling_usd"] == CEILING
        assert after["spent_usd"] == ledger["spent_usd"], "the transition moved money"
        assert after["policy_sha256"] == sha256_file(POLICY_V11)
        os.chmod(self.ledger_transition, 0o444)
        again = self.validate_ledger(POLICY_V11, self.gate, self.ledger_transition)
        assert again["config_transition_sha256"] == sha256_file(event)
        assert again["integrity_valid"] is True
        state["transition"] = {
            "authorization_file": str(self.ledger_transition),
            "authorization_file_sha256": sha256_file(self.ledger_transition),
            "event": str(event),
            "event_sha256": sha256_file(event),
            "predecessor_event": str(prior_event),
            "predecessor_event_sha256": sha256_file(prior_event),
            "status_after": after,
            "ledger_sha256_after": sha256_file(LEDGER),
            "applied_at_utc": now(),
        }
        write_json(self.state, state)
        print(json.dumps(state["transition"]["status_after"]["limits"], indent=2))

    # ---- launch -----------------------------------------------------------

    def write_launcher(self) -> None:
        self.launcher.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "",
                    f"release_runtime={self.runtime}",
                    f"release_gate={self.gate}",
                    f"release_transition={self.ledger_transition}",
                    f"release_policy={POLICY_V11}",
                    f"release_max_papers=${{1:-{MAX_PAPERS}}}",
                    f"release_paper_workers=${{2:-{PAPER_WORKERS}}}",
                    f"release_option_workers=${{3:-{OPTION_WORKERS}}}",
                    "",
                    'cd "$release_runtime"',
                    'exec nix develop "path:$release_runtime" -c env \\',
                    '  "PYTHONPATH=$release_runtime/src" \\',
                    "  python -m arctic_qa --json \\",
                    "  --data-root /mnt/crdata/research-abstention \\",
                    "  stream \\",
                    "  --phase away_production \\",
                    f"  --run-id {self.run_id} \\",
                    f"  --campaign-id {CAMPAIGN} \\",
                    f"  --access-run-dir {self.stream_input} \\",
                    f"  --eligibility-run-dir {self.eligibility_dir} \\",
                    f"  --eligibility-prompt-file {ELIGIBILITY_PROMPT} \\",
                    f"  --eligibility-schema-file {ELIGIBILITY_SCHEMA} \\",
                    f"  --eligibility-rescreen-prompt-file {RESCREEN_PROMPT} \\",
                    "  --eligibility-policy-file /mnt/crdata/research-abstention/arctic-qa/corpus-search-r1/protocol/protocol-v3.json \\",
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
        assert "transition" in state, "apply the transition first"
        live = self.live_producer()
        if live is not None:
            pid, _ = live
            # Stop at a zero-in-flight boundary: a request killed on the wire
            # leaves a liability the next start has to recover. Wait for the
            # boundary first, then stop at once.
            for _ in range(600):
                ledger = read_json(LEDGER)
                if int(ledger["inflight"]) == 0:
                    break
                time.sleep(0.5)
            ledger = read_json(LEDGER)
            assert int(ledger["inflight"]) == 0, (
                f"no zero-in-flight boundary; inflight {ledger['inflight']}"
            )
            print(f"stopping the live producer {pid}", file=sys.stderr)
            subprocess.run(["kill", str(pid)], check=False)
            for _ in range(120):
                if self.live_producer() is None:
                    break
                time.sleep(1)
            assert self.live_producer() is None, "the live producer did not stop"
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False)
        ledger = read_json(LEDGER)
        assert int(ledger["inflight"]) == 0, f"inflight {ledger['inflight']}"
        assert not ledger["halted"], "the ledger is halted"
        self.write_launcher()
        before = self.validate_ledger(POLICY_V11, self.gate, self.ledger_transition)
        assert before["integrity_valid"] is True and before["halted"] is False, before
        gate = read_json(self.gate)
        gate["activation_state"] = "started"
        os.chmod(self.gate, 0o644)
        write_json(self.gate, gate)
        os.chmod(self.gate, 0o444)
        # The gate hash is bound into every receipt, so the transition
        # authorization must still name it. Re-validate after the state change.
        after_gate = self.validate_ledger(POLICY_V11, self.gate, self.ledger_transition)
        assert after_gate["integrity_valid"] is True, after_gate
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
        for _ in range(60):
            live = self.live_producer()
            if live is not None:
                break
            time.sleep(1)
        assert live is not None, f"the producer did not start; see {self.log}"
        pid, _ = live
        state["launch"] = {
            "pid": pid,
            "launcher": str(self.launcher),
            "log": str(self.log),
            "paper_workers": PAPER_WORKERS,
            "option_workers": OPTION_WORKERS,
            "tmux_session": TMUX_SESSION,
            "ledger_before_launch": before,
            "launched_at_utc": now(),
        }
        write_json(self.state, state)
        print(json.dumps({"pid": pid, "launcher": str(self.launcher)}, indent=2))

    # ---- observe ----------------------------------------------------------

    def observe(self, seconds: int = 900) -> None:
        """Measure the window that starts at the first paid call.

        A relaunched producer replays the papers its eligibility run directory
        already holds before it makes its first paid call. That replay is free
        and took about 22 minutes on every relaunch of 2026-09-16, so a window
        that starts at launch measures the replay, not the run.
        """
        state = read_json(self.state)
        base = self.sample()
        waited = 0.0
        while waited < REPLAY_WAIT_SECONDS:
            current = self.sample()
            if current["generation_submissions"] > base["generation_submissions"]:
                break
            if not current["alive"]:
                raise SystemExit(f"the producer exited during replay; see {self.log}")
            time.sleep(15)
            waited += 15
        print(
            json.dumps(
                {
                    "replay_seconds": round(waited),
                    "first_paid_call_seen": waited < REPLAY_WAIT_SECONDS,
                }
            ),
            flush=True,
        )
        start = time.monotonic()
        first = self.sample()
        samples = [first]
        while time.monotonic() - start < seconds:
            time.sleep(30)
            samples.append(self.sample())
            last = samples[-1]
            print(
                json.dumps(
                    {
                        "at": last["at"],
                        "alive": last["alive"],
                        "spent_usd": last["spent_usd"],
                        "submissions": last["generation_submissions"],
                        "inflight": last["inflight"],
                        "accepted": last["accepted"],
                        "processed": last["processed"],
                    }
                ),
                flush=True,
            )
        last = samples[-1]
        minutes = (
            datetime.fromisoformat(last["at"].replace("Z", "+00:00"))
            - datetime.fromisoformat(first["at"].replace("Z", "+00:00"))
        ).total_seconds() / 60
        observation = {
            "window_minutes": round(minutes, 2),
            "requests": last["generation_submissions"]
            - first["generation_submissions"],
            "requests_per_minute": round(
                (last["generation_submissions"] - first["generation_submissions"])
                / max(minutes, 1e-9),
                2,
            ),
            "papers": last["processed"] - first["processed"],
            "papers_per_hour": round(
                (last["processed"] - first["processed"]) * 60 / max(minutes, 1e-9), 2
            ),
            "spend_usd": str(Decimal(last["spent_usd"]) - Decimal(first["spent_usd"])),
            "accepted": last["accepted"] - first["accepted"],
            "peak_inflight": max(sample["inflight"] for sample in samples),
            "halted": last["halted"],
            "alive": last["alive"],
            "samples": samples,
        }
        state.setdefault("observations", []).append(observation)
        write_json(self.state, state)
        print(
            json.dumps(
                {k: v for k, v in observation.items() if k != "samples"}, indent=2
            )
        )

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
    elif action == "apply-transition":
        activation.apply_transition()
    elif action == "launch":
        activation.launch()
    elif action == "observe":
        activation.observe(int(rest[0]) if rest else 900)
    else:
        raise SystemExit(f"unknown action: {action}")
