"""Subscription-billed evaluation providers: Claude Code and Codex.

Both providers answer one rendered trial with one isolated subprocess call of
the installed harness binary, billed to the captain's claude.ai or ChatGPT
login. No API key is read. The rendered prompt bytes are the ones the Gemini
provider sends; only the transport differs.

Claude: ``claude -p`` with every tool, setting source, slash command and MCP
server switched off, the default system prompt replaced by the evaluation
system text, no session persistence, and the ``--effort`` preset as the
thinking arm. The output is plain text, parsed by the strict single-letter
parser (``--json-schema`` would add a StructuredOutput tool).

ChatGPT: ``codex exec`` with a private ``CODEX_HOME`` that holds one strict
``config.toml`` (web search disabled, shell and subagent tools off, no
AGENTS.md, no host skills) and a symlink to the real ``auth.json``, an
ephemeral session, the ``model_reasoning_effort`` preset as the thinking arm,
and ``--output-schema`` with one enum field ``letter`` as the decoder
constraint.

Calls do not enter the shared paid-call ledger, whose validators are Gemini
only. They enter a sibling subscription ledger that applies the same
evaluation policy file (per-item cap, per-minute pace, a per-vendor limit on
the calls in flight, no retries, stop on the first error), binds the same
gate schema, and writes one immutable receipt per request key with USD 0 and
the token counts. A submitted request holds an in-flight lock file while its
harness runs, so N calls of one vendor can run at once and a request that a
crashed process left behind is settled as ``interrupted`` on the next open.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .abstention_providers import (
    COMPLETED,
    EvaluationRequest,
    EvaluationResponse,
    run_binding_record,
    scripted_letter,
)
from .abstention_render import ABSTENTION_OPTION_TEXT, PROMPT_VERSION, prompt_sha256
from .abstention_set import manifest_sha256
from .model_broker import (
    EVALUATION_GATE_SCHEMA,
    EVALUATION_PHASE,
    EVALUATION_POLICY_SCHEMA,
    broker_request_key,
    evaluation_stage,
)
from .util import atomic_json, canonical_json, redact, sha256_bytes, sha256_file


PROVIDER_ANTHROPIC_CLAUDE_CODE = "anthropic_claude_code"
PROVIDER_OPENAI_CODEX = "openai_codex"
SUBSCRIPTION_PROVIDER_NAMES = (PROVIDER_ANTHROPIC_CLAUDE_CODE, PROVIDER_OPENAI_CODEX)
SUBSCRIPTION_MODELS_SCHEMA = "benchmark-evaluation-subscription-models-v1"
SUBSCRIPTION_LEDGER_SCHEMA = "benchmark-evaluation-subscription-ledger-v1"
SUBSCRIPTION_RECEIPT_SCHEMA = "benchmark-evaluation-subscription-receipt-v1"
DEFAULT_SUBSCRIPTION_MODELS_FILE = Path(
    "config/benchmark-evaluation-subscription-models-v1.json"
)
SUBSCRIPTION_COST_USD = "0"
LEDGER_FILENAME = "subscription-ledger.json"
BINDING_FILENAME = "subscription-binding.json"
RECEIPTS_DIRNAME = "receipts"
SCRATCH_DIRNAME = "scratch"
CODEX_HOME_DIRNAME = "codex-home"
STATE_FAILED = "failed"
STATE_TIMEOUT = "timeout"
STATE_POLICY_STOP = "policy_stop"
STATE_INTERRUPTED = "interrupted"
INFLIGHT_DIRNAME = ".inflight"
SLOT_WAIT_SECONDS = 0.2
# Environment variables that must never reach the harness: a parent Claude
# Code session, an API key that would change the billing, or a Codex home.
STRIPPED_ENV_PREFIXES = ("CLAUDE", "ANTHROPIC_", "OPENAI_", "CODEX_")
KEPT_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "TZ", "TERM")

CLAUDE_ISOLATION_FLAGS = (
    "--tools",
    "",
    "--setting-sources",
    "",
    "--no-session-persistence",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--permission-mode",
    "dontAsk",
    "--permission-prompts",
    "none",
)
CLAUDE_EXTRA_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
CODEX_ISOLATION_FLAGS = (
    "--ephemeral",
    "--strict-config",
    "--skip-git-repo-check",
    "--ignore-rules",
)
# The static part of the private Codex config. Every key was accepted by
# ``--strict-config`` of codex-cli 0.154.0 and the effect was verified on a
# captured request (research/arctic-abstention-subscription-providers-r1/report.md).
CODEX_CONFIG_TOML = """# Written by arctic_qa.abstention_subscription; do not edit.
project_doc_max_bytes = 0
sandbox_mode = "read-only"
approval_policy = "never"
web_search = "disabled"
hide_agent_reasoning = true

[agents]
enabled = false

[skills]
max_context_tokens = 1

[features]
shell_tool = false
unified_exec = false
apps = false
computer_use = false
browser_use = false
image_generation = false
skill_search = false
tool_suggest = false
sleep_tool = false
multi_agent = false
view_image = false
goals = false
memories = false
in_app_browser = false
plugins = false
remote_plugin = false
skill_mcp_dependency_install = false
"""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- Model registry ------------------------------------------------------------


def load_subscription_models(path: Path) -> dict[str, Any]:
    """Read and validate the subscription model registry."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != SUBSCRIPTION_MODELS_SCHEMA:
        raise ValueError("unsupported subscription models schema")
    if value.get("billing") != "subscription" or value.get("cost_usd_per_call") != "0":
        raise ValueError("the subscription models file must bill at USD 0")
    vendors = value.get("vendors")
    if not isinstance(vendors, dict) or set(vendors) - set(SUBSCRIPTION_PROVIDER_NAMES):
        raise ValueError("the subscription models file lists an unknown vendor")
    for vendor, entry in vendors.items():
        for field in ("binary", "call_timeout_seconds", "models"):
            if field not in entry:
                raise ValueError(f"the vendor {vendor} lacks {field}")
        timeout = entry["call_timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise ValueError(f"the vendor {vendor} has an invalid call timeout")
        for model, record in entry["models"].items():
            evaluation_stage(model)
            presets = record.get("presets")
            if not isinstance(presets, list) or not presets:
                raise ValueError(f"the model {model} lists no preset")
    return value


def vendor_entry(config: dict[str, Any], vendor: str) -> dict[str, Any]:
    if vendor not in SUBSCRIPTION_PROVIDER_NAMES:
        raise ValueError(f"unsupported subscription provider: {vendor}")
    entry = (config.get("vendors") or {}).get(vendor)
    if not isinstance(entry, dict):
        raise ValueError(f"the subscription models file has no entry for {vendor}")
    return entry


def subscription_decoding_record(
    config: dict[str, Any],
    vendor: str,
    models: list[str],
    arms: list[str],
    *,
    binary: str | None = None,
    binary_version: str | None = None,
) -> dict[str, Any]:
    """Return the decoding record that the gate and the run binding share."""
    entry = vendor_entry(config, vendor)
    for model in models:
        record = entry["models"].get(model)
        if record is None:
            raise ValueError(
                f"the evaluated model is not registered for {vendor}: {model}"
            )
        for arm in arms:
            if arm not in record["presets"]:
                raise ValueError(
                    f"the thinking arm {arm} is not an official preset of {model}"
                )
    if vendor == PROVIDER_ANTHROPIC_CLAUDE_CODE:
        thinking = "claude -p --effort <arm> (adaptive thinking at the preset)"
        constraint = str(entry.get("output_constraint") or "none")
        isolation = list(CLAUDE_ISOLATION_FLAGS) + [
            "--system-prompt <evaluation system text>"
        ]
        env = dict(CLAUDE_EXTRA_ENV)
    else:
        thinking = "codex exec -c model_reasoning_effort=<arm>"
        constraint = str(entry.get("output_constraint") or "none")
        isolation = list(CODEX_ISOLATION_FLAGS) + [
            "--output-schema {letter: enum}",
            "-c model_instructions_file=<evaluation system text>",
            "private CODEX_HOME with the strict config.toml",
            "empty HOME",
        ]
        env = {"HOME": "<empty directory>", "CODEX_HOME": "<private codex home>"}
    return {
        "billing": "subscription",
        "vendor": vendor,
        "cost_usd_per_call": SUBSCRIPTION_COST_USD,
        "harness_binary": binary or str(entry["binary"]),
        "harness_version": binary_version
        or str(entry.get("binary_version_checked") or ""),
        "candidate_count": 1,
        "temperature_rule": "not exposed by the harness; provider default",
        "temperature_by_model": {model: None for model in sorted(models)},
        "thinking_control": thinking,
        "presets_by_model": {
            model: list(entry["models"][model]["presets"]) for model in sorted(models)
        },
        "output_constraint": constraint,
        "isolation_flags": isolation,
        "isolation_env": env,
        "stripped_env_prefixes": list(STRIPPED_ENV_PREFIXES),
        "prompt_bytes": "identical to the Gemini rendering (system_text, user_text)",
        "codex_config_toml_sha256": (
            sha256_bytes(CODEX_CONFIG_TOML.encode())
            if vendor == PROVIDER_OPENAI_CODEX
            else None
        ),
    }


# --- Transports ----------------------------------------------------------------


class SubscriptionTransport(Protocol):
    """Run one harness invocation and return its exit code and streams."""

    def run(self, invocation: dict[str, Any]) -> dict[str, Any]: ...


class SubprocessTransport:
    """Run the harness binary as a child process from the scratch directory."""

    def run(self, invocation: dict[str, Any]) -> dict[str, Any]:
        try:
            result = subprocess.run(
                invocation["argv"],
                input=invocation.get("stdin"),
                cwd=invocation["cwd"],
                env=invocation["env"],
                capture_output=True,
                text=True,
                timeout=invocation["timeout_seconds"],
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return {
                "returncode": None,
                "stdout": (error.stdout or b"").decode("utf-8", "replace")
                if isinstance(error.stdout, bytes)
                else (error.stdout or ""),
                "stderr": (error.stderr or b"").decode("utf-8", "replace")
                if isinstance(error.stderr, bytes)
                else (error.stderr or ""),
                "timed_out": True,
            }
        except OSError as error:
            return {
                "returncode": None,
                "stdout": "",
                "stderr": f"{type(error).__name__}: {error}",
                "timed_out": False,
            }
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": False,
        }


def subscription_script_key(
    vendor: str, model: str, arm: str, system_text: str, user_text: str
) -> str:
    """Key one scripted answer by the vendor, model, arm and exact prompt bytes."""
    return sha256_bytes(
        canonical_json(
            {
                "vendor": vendor,
                "model": model,
                "arm": arm,
                "system_text": system_text,
                "user_text": user_text,
            }
        ).encode()
    )


def claude_result_json(event: dict[str, Any], *, model: str) -> str:
    """Render one scripted event as the ``claude -p --output-format json`` result."""
    prompt = int(event.get("prompt_tokens", 200))
    candidates = int(event.get("candidate_tokens", 1))
    thoughts = int(event.get("thinking_tokens", 40))
    stop = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}.get(
        str(event.get("finish_reason", "STOP")), str(event.get("finish_reason"))
    )
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": event.get("text") if event.get("text") is not None else "",
        "stop_reason": stop,
        "num_turns": 1,
        "duration_ms": 1000,
        "duration_api_ms": 900,
        "session_id": "scripted-session",
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": prompt - 2,
            "cache_read_input_tokens": 0,
            "output_tokens": candidates + thoughts,
            "output_tokens_details": {"thinking_tokens": thoughts},
        },
        "modelUsage": {
            model: {
                "inputTokens": 2,
                "outputTokens": candidates + thoughts,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": prompt - 2,
                "thinkingTokens": thoughts,
                "costUSD": 0.0,
                "canonicalModel": model,
                "costBasis": "list",
            }
        },
        "total_cost_usd": 0.0,
    }
    return canonical_json(result) + "\n"


def codex_events_jsonl(event: dict[str, Any]) -> str:
    """Render one scripted event as the ``codex exec --json`` event stream."""
    prompt = int(event.get("prompt_tokens", 200))
    candidates = int(event.get("candidate_tokens", 1))
    thoughts = int(event.get("thinking_tokens", 40))
    if "final_text" in event:
        final = event["final_text"]
    elif event.get("text") is not None:
        final = canonical_json({"letter": event["text"]})
    else:
        final = None
    rows: list[dict[str, Any]] = [
        {"type": "thread.started", "thread_id": "scripted-thread"},
        {"type": "turn.started"},
    ]
    if final is not None:
        rows.append(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": final},
            }
        )
    rows.append(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": prompt,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": candidates + thoughts,
                "reasoning_output_tokens": thoughts,
            },
        }
    )
    return "".join(canonical_json(row) + "\n" for row in rows)


class ScriptedSubscriptionTransport:
    """Answer harness invocations offline from a prepared event map.

    ``answers`` maps :func:`subscription_script_key` to a scripted event of the
    same shape as the Gemini scripted events (``text``, ``finish_reason``,
    token counts, ``raise``). The map is keyed by the exact prompt bytes, not
    by call order.
    """

    def __init__(
        self,
        answers: dict[str, dict[str, Any]],
        *,
        version: str = "scripted",
        latency_seconds: float = 0.0,
    ) -> None:
        self.answers = answers
        self.version = version
        self.latency_seconds = latency_seconds
        self.invocations: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def run(self, invocation: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.invocations.append(invocation)
        if invocation.get("purpose") == "version":
            return {
                "returncode": 0,
                "stdout": self.version + "\n",
                "stderr": "",
                "timed_out": False,
            }
        key = subscription_script_key(**invocation["script"])
        event = self.answers.get(key)
        if event is None:
            raise ValueError("the scripted transport has no answer for this prompt")
        latency = float(event.get("latency_seconds") or self.latency_seconds or 0)
        if latency > 0:
            time.sleep(latency)
        if event.get("raise") == "timeout":
            return {"returncode": None, "stdout": "", "stderr": "", "timed_out": True}
        if event.get("raise") == "exit":
            return {
                "returncode": int(event.get("returncode", 1)),
                "stdout": "",
                "stderr": str(event.get("stderr", "scripted harness error")),
                "timed_out": False,
            }
        if invocation["vendor"] == PROVIDER_ANTHROPIC_CLAUDE_CODE:
            stdout = claude_result_json(event, model=invocation["script"]["model"])
        else:
            stdout = codex_events_jsonl(event)
        return {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}


def scripted_subscription_answers(
    vendor: str,
    trials: list[dict[str, Any]],
    *,
    policy: str,
    seed: str,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Map every planned trial to its scripted event, keyed by prompt bytes."""
    answers: dict[str, dict[str, Any]] = {}
    for trial in trials:
        event = (overrides or {}).get(trial["trial_id"]) or scripted_letter(
            trial, policy, seed=seed
        )
        answers[
            subscription_script_key(
                vendor,
                trial["model"],
                trial["arm"],
                trial["system_text"],
                trial["user_text"],
            )
        ] = event
    return answers


# --- Output parsing ------------------------------------------------------------


def _normalized_usage(
    prompt: int, candidates: int, thoughts: int, native: dict[str, Any]
) -> dict[str, Any]:
    return {
        "promptTokenCount": prompt,
        "candidatesTokenCount": candidates,
        "thoughtsTokenCount": thoughts,
        "totalTokenCount": prompt + candidates + thoughts,
        "native": native,
    }


def parse_claude_output(stdout: str, *, model: str) -> dict[str, Any]:
    """Read the ``claude -p --output-format json`` result into a response record."""
    try:
        result = json.loads(stdout.strip() or "null")
    except json.JSONDecodeError:
        return {"state": STATE_FAILED, "error": "claude printed no JSON result"}
    if not isinstance(result, dict) or result.get("type") != "result":
        return {"state": STATE_FAILED, "error": "claude printed no result object"}
    if result.get("is_error") or result.get("subtype") != "success":
        return {
            "state": STATE_FAILED,
            "error": f"claude result subtype {result.get('subtype')}: "
            f"{str(result.get('result') or result.get('error') or '')[:500]}",
            "final_text": result.get("result"),
        }
    usage = result.get("usage") or {}
    thoughts = int(
        (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0
    )
    output = int(usage.get("output_tokens", 0) or 0)
    prompt = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
    )
    model_usage = result.get("modelUsage") or {}
    side_calls = sorted(name for name in model_usage if name != model)
    stop = str(result.get("stop_reason") or "")
    finish = "STOP" if stop == "end_turn" else (stop or "unknown")
    return {
        "state": COMPLETED,
        "final_text": result.get("result"),
        "raw_text": result.get("result"),
        "finish_reason": finish,
        "usage": _normalized_usage(prompt, max(output - thoughts, 0), thoughts, usage),
        "model_usage": model_usage,
        "side_call_models": side_calls,
        "num_turns": result.get("num_turns"),
        "session_id": result.get("session_id"),
        "duration_api_ms": result.get("duration_api_ms"),
        "harness_cost_estimate_usd": result.get("total_cost_usd"),
        "model_version": model if model in model_usage else None,
    }


def parse_codex_output(stdout: str, *, model: str) -> dict[str, Any]:
    """Read the ``codex exec --json`` event stream into a response record."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not events:
        return {"state": STATE_FAILED, "error": "codex printed no JSON event"}
    messages = [
        row["item"]["text"]
        for row in events
        if row.get("type") == "item.completed"
        and isinstance(row.get("item"), dict)
        and row["item"].get("type") == "agent_message"
    ]
    tool_items = sorted(
        {
            str(row["item"].get("type"))
            for row in events
            if row.get("type") in {"item.started", "item.completed"}
            and isinstance(row.get("item"), dict)
            and row["item"].get("type") not in {"agent_message", "reasoning", "error"}
        }
    )
    errors = [
        str(row.get("message") or row.get("item", {}).get("message") or "")
        for row in events
        if row.get("type") == "error"
        or (
            row.get("type") == "item.completed"
            and (row.get("item") or {}).get("type") == "error"
        )
    ]
    completed = next(
        (row for row in events if row.get("type") == "turn.completed"), None
    )
    failed = next((row for row in events if row.get("type") == "turn.failed"), None)
    if failed is not None or completed is None:
        detail = failed.get("error") if isinstance(failed, dict) else None
        return {
            "state": STATE_FAILED,
            "error": "codex turn did not complete: "
            + (
                json.dumps(detail)
                if detail
                else "; ".join(errors) or "no turn.completed event"
            ),
            "final_text": messages[-1] if messages else None,
            "harness_errors": errors,
        }
    if tool_items:
        # A tool ran. The call is void: the model had access it must not have.
        return {
            "state": STATE_FAILED,
            "error": f"codex ran a tool item: {', '.join(tool_items)}",
            "final_text": messages[-1] if messages else None,
            "harness_errors": errors,
            "tool_items": tool_items,
        }
    final = messages[-1] if messages else None
    raw_text: str | None = None
    constraint_honoured = False
    if final is not None:
        try:
            decoded = json.loads(final)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict) and isinstance(decoded.get("letter"), str):
            raw_text = decoded["letter"]
            constraint_honoured = True
        else:
            raw_text = final
    usage = completed.get("usage") or {}
    prompt = int(usage.get("input_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    thoughts = int(usage.get("reasoning_output_tokens", 0) or 0)
    return {
        "state": COMPLETED,
        "final_text": final,
        "raw_text": raw_text,
        "finish_reason": "STOP" if final is not None else "no_agent_message",
        "usage": _normalized_usage(prompt, max(output - thoughts, 0), thoughts, usage),
        "schema_honoured": constraint_honoured,
        "harness_errors": errors,
        "thread_id": next(
            (
                row.get("thread_id")
                for row in events
                if row.get("type") == "thread.started"
            ),
            None,
        ),
        "model_version": None,
    }


# --- Subscription ledger -------------------------------------------------------


def _validate_subscription_policy(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != EVALUATION_POLICY_SCHEMA:
        raise ValueError("unsupported benchmark evaluation policy schema")
    for field in (
        "maximum_calls_per_item_condition_model_arm",
        "maximum_concurrent_requests",
        "maximum_requests_per_minute",
    ):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(f"the benchmark evaluation policy {field} is invalid")
    if value.get("automatic_transport_retries") != 0:
        raise ValueError("the benchmark evaluation policy permits retries")
    for field, expected in {
        "automatic_model_fallback": False,
        "re_ask_on_invalid_response": False,
        "automatic_budget_rearm": False,
        "stop_on_first_infrastructure_error_or_ambiguous_charge": True,
    }.items():
        if value.get(field) is not expected:
            raise ValueError(f"benchmark evaluation control changed: {field}")
    vendors = value.get("vendors") or {}
    if not isinstance(vendors, dict):
        raise ValueError("the benchmark evaluation policy vendors block is invalid")
    for vendor, overrides in vendors.items():
        if not isinstance(overrides, dict):
            raise ValueError(
                f"the benchmark evaluation policy vendor {vendor} is invalid"
            )
        for field in ("maximum_concurrent_requests", "maximum_requests_per_minute"):
            if field not in overrides:
                continue
            item = overrides[field]
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise ValueError(
                    f"the benchmark evaluation policy {vendor} {field} is invalid"
                )
    return value


def vendor_policy_limit(policy: dict[str, Any], vendor: str, field: str) -> int:
    """Return one pacing limit of a vendor: its override, else the policy value."""
    overrides = (policy.get("vendors") or {}).get(vendor) or {}
    return int(overrides.get(field, policy[field]))


def _validate_subscription_gate(path: Path, *, vendor: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != EVALUATION_GATE_SCHEMA:
        raise ValueError("unsupported benchmark evaluation gate schema")
    if value.get("evaluation_enabled") is not True:
        raise ValueError("benchmark evaluation is disabled")
    if value.get("allowed_phase") != EVALUATION_PHASE:
        raise ValueError("the benchmark evaluation gate does not allow this phase")
    if value.get("independent_review_verdict") != "pass":
        raise ValueError("the benchmark evaluation review did not pass")
    if value.get("provider") != vendor:
        raise ValueError("the benchmark evaluation gate binds another provider")
    for field in (
        "eval_set_id",
        "eval_set_manifest_sha256",
        "prompt_sha256",
        "abstention_option_text",
        "models",
        "arms",
        "decoding",
        "repeats_maximum",
        "authorized_run_id",
        "evaluation_policy_sha256",
        "evaluation_price_config_sha256",
        "integrated_code_commit",
    ):
        if field not in value:
            raise ValueError(f"the benchmark evaluation gate lacks {field}")
    if not isinstance(value["models"], list) or not value["models"]:
        raise ValueError("the benchmark evaluation gate lists no model")
    if not isinstance(value["arms"], list) or not value["arms"]:
        raise ValueError("the benchmark evaluation gate lists no thinking arm")
    if (value["decoding"] or {}).get("billing") != "subscription":
        raise ValueError(
            "the benchmark evaluation gate decoding record is not a subscription record"
        )
    return value


class SubscriptionLedger:
    """Count, pace and receipt subscription calls under the evaluation policy."""

    def __init__(
        self,
        *,
        ledger_dir: Path,
        vendor: str,
        policy_file: Path,
        gate_file: Path,
        models_file: Path,
        sleep: Any = time.sleep,
        clock: Any = time.time,
    ) -> None:
        self.ledger_dir = ledger_dir
        self.vendor = vendor
        self.policy_file = policy_file
        self.gate_file = gate_file
        self.models_file = models_file
        self._sleep = sleep
        self._clock = clock
        self.policy = _validate_subscription_policy(policy_file)
        self.gate = _validate_subscription_gate(gate_file, vendor=vendor)
        if self.gate["evaluation_policy_sha256"] != sha256_file(policy_file):
            raise ValueError("the evaluation gate binds another evaluation policy")
        if self.gate["evaluation_price_config_sha256"] != sha256_file(models_file):
            raise ValueError(
                "the evaluation gate binds another subscription models file"
            )
        self.gate_sha256 = sha256_file(gate_file)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir = ledger_dir / RECEIPTS_DIRNAME
        self.receipts_dir.mkdir(exist_ok=True)
        self.inflight_dir = self.receipts_dir / INFLIGHT_DIRNAME
        self.inflight_dir.mkdir(exist_ok=True)
        self._inflight: dict[str, Any] = {}
        self._inflight_guard = threading.Lock()
        self.ledger_file = ledger_dir / LEDGER_FILENAME
        if self.ledger_file.is_file():
            existing = self._read()
            if existing.get("vendor") != vendor:
                raise ValueError("the subscription ledger belongs to another vendor")
            if existing.get("policy_sha256") != sha256_file(policy_file):
                raise ValueError(
                    "the subscription ledger was opened under another evaluation policy"
                )
        else:
            atomic_json(
                self.ledger_file,
                {
                    "schema": SUBSCRIPTION_LEDGER_SCHEMA,
                    "vendor": vendor,
                    "phase": EVALUATION_PHASE,
                    "billing": "subscription",
                    "policy_id": self.policy["policy_id"],
                    "policy_sha256": sha256_file(policy_file),
                    "gate_sha256": sha256_file(gate_file),
                    "created_at_utc": _utc_now(),
                    "requests": {},
                },
            )
        self.binding: dict[str, Any] | None = None

    # -- binding --------------------------------------------------------------

    def bind(self, binding: dict[str, Any], *, eval_set_manifest_file: Path) -> None:
        """Make sure that the run agrees with the gate on every bound field."""
        gate = self.gate
        checks = {
            "eval_set_id": (binding["eval_set_id"], gate["eval_set_id"]),
            "eval_set_manifest_sha256": (
                sha256_file(eval_set_manifest_file),
                gate["eval_set_manifest_sha256"],
            ),
            "prompt_version": (binding["prompt_version"], gate.get("prompt_version")),
            "prompt_sha256": (binding["prompt_sha256"], gate["prompt_sha256"]),
            "abstention_option_text": (
                binding["abstention_option_text"],
                gate["abstention_option_text"],
            ),
            "run_id": (binding["run_id"], gate["authorized_run_id"]),
            "decoding": (binding["decoding"], gate["decoding"]),
        }
        for name, (actual, expected) in checks.items():
            if actual != expected:
                raise ValueError(f"the evaluation gate binds another {name}")
        if set(binding["models"]) - set(gate["models"]):
            raise ValueError("the evaluation gate does not allow one of the models")
        if set(binding["arms"]) - set(gate["arms"]):
            raise ValueError(
                "the evaluation gate does not allow one of the thinking arms"
            )
        if int(binding["repeats"]) > int(gate["repeats_maximum"]):
            raise ValueError("the evaluation gate allows fewer repeats")
        atomic_json(self.ledger_dir / BINDING_FILENAME, binding)
        self.binding = binding

    # -- ledger file ----------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        return json.loads(self.ledger_file.read_text(encoding="utf-8"))

    def _lock_path(self) -> Path:
        return self.ledger_file.with_name(f".{self.ledger_file.name}.lock")

    def receipt_path(self, request_key: str) -> Path:
        return self.receipts_dir / f"{request_key}.json"

    def limit(self, field: str) -> int:
        """Return the pacing limit of this vendor under the evaluation policy."""
        return vendor_policy_limit(self.policy, self.vendor, field)

    # -- in-flight locks ------------------------------------------------------

    def _inflight_path(self, request_key: str) -> Path:
        return self.inflight_dir / f"{request_key}.lock"

    def _hold_inflight(self, request_key: str) -> None:
        handle = self._inflight_path(request_key).open("a+")
        fcntl.flock(handle, fcntl.LOCK_EX)
        with self._inflight_guard:
            self._inflight[request_key] = handle

    def _release_inflight(self, request_key: str) -> None:
        with self._inflight_guard:
            handle = self._inflight.pop(request_key, None)
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        self._inflight_path(request_key).unlink(missing_ok=True)

    def _inflight_held(self, request_key: str) -> bool:
        path = self._inflight_path(request_key)
        if not path.exists():
            return False
        with path.open("a+") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe, fcntl.LOCK_UN)
        return False

    def _recover_stale(self, ledger: dict[str, Any]) -> bool:
        """Settle every submitted row whose holder is gone. Return whether any."""
        changed = False
        for request_key, row in ledger["requests"].items():
            if row["state"] != "submitted" or self._inflight_held(request_key):
                continue
            receipt = {
                "schema": SUBSCRIPTION_RECEIPT_SCHEMA,
                "request_key": request_key,
                "request_sha256": None,
                "phase": EVALUATION_PHASE,
                "vendor": self.vendor,
                "model": row["model"],
                "arm": row["arm"],
                "trial": {"trial_id": row["trial_id"]},
                "billing": "subscription",
                "cost_usd": SUBSCRIPTION_COST_USD,
                "state": STATE_INTERRUPTED,
                "error": "the harness process did not settle this request",
                "usage": None,
                "submitted_at_utc": row["submitted_at_utc"],
                "completed_at_utc": _utc_now(),
            }
            path = self.receipt_path(request_key)
            if not path.is_file():
                atomic_json(path, receipt, immutable=True)
            row["state"] = STATE_INTERRUPTED
            row["completed_at_utc"] = receipt["completed_at_utc"]
            row["usage"] = {
                "promptTokenCount": 0,
                "candidatesTokenCount": 0,
                "thoughtsTokenCount": 0,
            }
            row["receipt_sha256"] = sha256_file(path)
            self._inflight_path(request_key).unlink(missing_ok=True)
            changed = True
        return changed

    @staticmethod
    def _inflight_count(ledger: dict[str, Any]) -> int:
        return sum(
            1 for row in ledger["requests"].values() if row["state"] == "submitted"
        )

    def _item_key(self, trial: dict[str, Any]) -> str:
        return "/".join(
            (trial["item_id"], trial["condition"], trial["model"], trial["arm"])
        )

    def status(self) -> dict[str, Any]:
        ledger = self._read()
        requests = ledger["requests"].values()
        completed = [row for row in requests if row["state"] == COMPLETED]
        return {
            "vendor": self.vendor,
            "phase": EVALUATION_PHASE,
            "billing": "subscription",
            "policy_id": ledger["policy_id"],
            "gate_sha256": ledger["gate_sha256"],
            "submissions": len(ledger["requests"]),
            "completed": len(completed),
            "inflight": self._inflight_count(ledger),
            "maximum_concurrent_requests": self.limit("maximum_concurrent_requests"),
            "maximum_requests_per_minute": self.limit("maximum_requests_per_minute"),
            "input_tokens": sum(
                int(row["usage"]["promptTokenCount"]) for row in completed
            ),
            "output_tokens": sum(
                int(row["usage"]["candidatesTokenCount"]) for row in completed
            ),
            "thinking_tokens": sum(
                int(row["usage"]["thoughtsTokenCount"]) for row in completed
            ),
            "spent_usd": SUBSCRIPTION_COST_USD,
        }

    def _admit(self, request_key: str, trial: dict[str, Any], now: float) -> str | None:
        """Return a policy stop reason, or None when the call may go out."""
        ledger = self._read()
        if request_key in ledger["requests"]:
            return "duplicate request key"
        item_key = self._item_key(trial)
        used = sum(
            1 for row in ledger["requests"].values() if row["item_key"] == item_key
        )
        cap = int(self.policy["maximum_calls_per_item_condition_model_arm"])
        if used >= cap:
            return f"the per-item call cap of {cap} is reached for {item_key}"
        return None

    def _window_wait(self, ledger: dict[str, Any], now: float) -> float:
        """Return how long the per-minute window needs before one more call."""
        limit = self.limit("maximum_requests_per_minute")
        recent = sorted(
            float(row["submitted_at_epoch"])
            for row in ledger["requests"].values()
            if now - float(row["submitted_at_epoch"]) < 60.0
        )
        if len(recent) < limit:
            return 0.0
        return max(60.0 - (now - recent[0]) + 0.05, 0.05)

    def _pace(self, now: float) -> None:
        """Wait until the per-minute window has room (the ledger lock is free)."""
        while True:
            with _FileLock(self._lock_path()):
                wait = self._window_wait(self._read(), now)
            if wait <= 0:
                return
            self._sleep(wait)
            now = self._clock()

    def submit(self, request_key: str, trial: dict[str, Any]) -> dict[str, Any] | None:
        """Register one outgoing call, or return a policy stop record.

        The admission runs under the ledger lock: stale rows are settled, the
        per-item cap and the duplicate key are checked, the vendor's in-flight
        limit and its minute window are applied, and the row is written with
        the in-flight lock held. The harness call itself runs outside the
        ledger lock, so other calls of the vendor proceed in parallel.
        """
        now = self._clock()
        stop = self._admit(request_key, trial, now)
        if stop is not None:
            return {"state": STATE_POLICY_STOP, "error": stop}
        deadline = now + 3600.0
        while True:
            with _FileLock(self._lock_path()):
                ledger = self._read()
                if self._recover_stale(ledger):
                    atomic_json(self.ledger_file, ledger)
                if request_key in ledger["requests"]:
                    return {
                        "state": STATE_POLICY_STOP,
                        "error": "duplicate request key",
                    }
                wait = self._window_wait(ledger, now)
                slots = self.limit("maximum_concurrent_requests")
                if wait <= 0 and self._inflight_count(ledger) < slots:
                    ledger["requests"][request_key] = {
                        "trial_id": trial["trial_id"],
                        "item_key": self._item_key(trial),
                        "model": trial["model"],
                        "arm": trial["arm"],
                        "state": "submitted",
                        "submitted_at_utc": _utc_now(),
                        "submitted_at_epoch": self._clock(),
                        "gate_sha256": self.gate_sha256,
                        "cost_usd": SUBSCRIPTION_COST_USD,
                    }
                    self._hold_inflight(request_key)
                    atomic_json(self.ledger_file, ledger)
                    return None
            if now >= deadline:
                return {
                    "state": STATE_POLICY_STOP,
                    "error": "the vendor in-flight limit stayed full for an hour",
                }
            self._sleep(wait if wait > 0 else SLOT_WAIT_SECONDS)
            now = self._clock()

    def settle(self, request_key: str, receipt: dict[str, Any]) -> Path:
        """Write the immutable receipt, close the ledger row, free the slot."""
        path = self.receipt_path(request_key)
        atomic_json(path, receipt, immutable=True)
        try:
            with _FileLock(self._lock_path()):
                ledger = self._read()
                row = ledger["requests"][request_key]
                row["state"] = receipt["state"]
                row["completed_at_utc"] = receipt["completed_at_utc"]
                row["usage"] = receipt.get("usage") or {
                    "promptTokenCount": 0,
                    "candidatesTokenCount": 0,
                    "thoughtsTokenCount": 0,
                }
                row["receipt_sha256"] = sha256_file(path)
                atomic_json(self.ledger_file, ledger)
        finally:
            self._release_inflight(request_key)
        return path


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "_FileLock":
        self.handle = self.path.open("a+")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


# --- Provider ------------------------------------------------------------------


def harness_environment(
    vendor: str, *, home: str, codex_home: str | None = None
) -> dict[str, str]:
    """Return the minimal child environment: no parent session, no API key."""
    env = {
        name: os.environ[name]
        for name in KEPT_ENV_NAMES
        if name in os.environ and not name.startswith(STRIPPED_ENV_PREFIXES)
    }
    env["HOME"] = home
    if vendor == PROVIDER_ANTHROPIC_CLAUDE_CODE:
        env.update(CLAUDE_EXTRA_ENV)
    else:
        if codex_home is None:
            raise ValueError("the Codex provider needs a private CODEX_HOME")
        env["CODEX_HOME"] = codex_home
    return env


def binary_version(
    vendor: str, binary: str, transport: SubscriptionTransport, *, cwd: Path
) -> str:
    """Return the version line that the harness binary prints."""
    result = transport.run(
        {
            "purpose": "version",
            "vendor": vendor,
            "argv": [binary, "--version"],
            "env": harness_environment(
                vendor,
                home=os.environ.get("HOME", str(cwd)),
                codex_home=os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")),
            ),
            "cwd": str(cwd),
            "stdin": None,
            "timeout_seconds": 60,
        }
    )
    if result["returncode"] != 0:
        raise ValueError(f"{binary} --version failed: {result['stderr'][:200]}")
    return result["stdout"].strip().splitlines()[0] if result["stdout"].strip() else ""


class SubscriptionEvaluationProvider:
    """Answer trials through one isolated harness subprocess per trial."""

    def __init__(
        self,
        *,
        vendor: str,
        ledger: SubscriptionLedger,
        models_config: dict[str, Any],
        decoding: dict[str, Any],
        run_id: str,
        scratch_root: Path,
        transport: SubscriptionTransport | None = None,
        binary: str | None = None,
    ) -> None:
        if vendor not in SUBSCRIPTION_PROVIDER_NAMES:
            raise ValueError(f"unsupported subscription provider: {vendor}")
        self.name = vendor
        self.vendor = vendor
        self.ledger = ledger
        self.entry = vendor_entry(models_config, vendor)
        self._decoding = decoding
        self.run_id = run_id
        self.transport = transport or SubprocessTransport()
        self.binary = binary or str(self.entry["binary"])
        self.timeout = int(self.entry["call_timeout_seconds"])
        self.scratch_root = scratch_root
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self.empty_home = scratch_root / "empty-home"
        self.empty_home.mkdir(exist_ok=True)
        self.codex_home: Path | None = None
        if vendor == PROVIDER_OPENAI_CODEX:
            self.codex_home = self._prepare_codex_home()

    # -- setup ------------------------------------------------------------------

    def _prepare_codex_home(self) -> Path:
        home = self.ledger.ledger_dir / CODEX_HOME_DIRNAME
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(CODEX_CONFIG_TOML, encoding="utf-8")
        auth = home / "auth.json"
        source = Path(
            str(self.entry.get("auth_file") or "~/.codex/auth.json")
        ).expanduser()
        if auth.is_symlink() or auth.exists():
            if not auth.is_symlink() or os.readlink(auth) != str(source):
                auth.unlink()
        if not auth.exists() and not auth.is_symlink():
            auth.symlink_to(source)
        return home

    def supports_enum_output(self, model: str) -> bool:
        return self.vendor == PROVIDER_OPENAI_CODEX

    def decoding(self, model: str, arm: str) -> dict[str, Any]:
        presets = self._decoding["presets_by_model"].get(model)
        if presets is None or arm not in presets:
            raise ValueError("the run decoding record lacks this model or arm")
        return {
            "temperature": None,
            "max_output_tokens": None,
            "thinking": {"effort": arm},
            "output_constraint": self._decoding["output_constraint"],
            "billing": "subscription",
        }

    # -- invocation -------------------------------------------------------------

    def invocation(self, trial: dict[str, Any], call_dir: Path) -> dict[str, Any]:
        """Build the exact subprocess invocation of one trial."""
        letters = list(trial["letters"])
        script = {
            "vendor": self.vendor,
            "model": trial["model"],
            "arm": trial["arm"],
            "system_text": trial["system_text"],
            "user_text": trial["user_text"],
        }
        if self.vendor == PROVIDER_ANTHROPIC_CLAUDE_CODE:
            argv = [
                self.binary,
                "-p",
                "--model",
                trial["model"],
                "--effort",
                trial["arm"],
                "--system-prompt",
                trial["system_text"],
                *CLAUDE_ISOLATION_FLAGS,
                "--output-format",
                "json",
            ]
            env = harness_environment(
                self.vendor, home=os.environ.get("HOME", str(call_dir))
            )
            files: dict[str, str] = {}
        else:
            system_path = call_dir / "system.txt"
            schema_path = call_dir / "schema.json"
            system_path.write_text(trial["system_text"], encoding="utf-8")
            schema = {
                "type": "object",
                "properties": {"letter": {"type": "string", "enum": letters}},
                "required": ["letter"],
                "additionalProperties": False,
            }
            schema_path.write_text(canonical_json(schema) + "\n", encoding="utf-8")
            argv = [
                self.binary,
                "exec",
                "-m",
                trial["model"],
                *CODEX_ISOLATION_FLAGS,
                "-c",
                f'model_reasoning_effort="{trial["arm"]}"',
                "-c",
                f'model_instructions_file="{system_path}"',
                "-C",
                str(call_dir),
                "--output-schema",
                str(schema_path),
                "--json",
                "-",
            ]
            env = harness_environment(
                self.vendor, home=str(self.empty_home), codex_home=str(self.codex_home)
            )
            files = {
                "system_text_sha256": sha256_bytes(trial["system_text"].encode()),
                "schema": canonical_json(schema),
            }
        return {
            "purpose": "trial",
            "vendor": self.vendor,
            "argv": argv,
            "argv_display": [redact(item) for item in argv],
            "env": env,
            "cwd": str(call_dir),
            "stdin": trial["user_text"],
            "timeout_seconds": self.timeout,
            "script": script,
            "files": files,
        }

    def request_key(self, request: EvaluationRequest) -> str:
        trial = request.trial
        payload = {
            "vendor": self.vendor,
            "system_text": trial["system_text"],
            "user_text": trial["user_text"],
            "letters": trial["letters"],
            "arm": trial["arm"],
            "output_constraint": self._decoding["output_constraint"],
        }
        return broker_request_key(
            model=trial["model"],
            run_id=request.run_id,
            phase=EVALUATION_PHASE,
            stage=evaluation_stage(trial["model"]),
            paper_id=request.identity["paper_id"],
            family_id=request.identity["family_id"],
            source_version_id=request.identity["source_version_id"],
            payload=payload,
            trial_id=trial["trial_id"],
        )

    # -- answer -----------------------------------------------------------------

    def _response(
        self, receipt: dict[str, Any], *, path: Path | None, resumed: bool
    ) -> EvaluationResponse:
        parsed = receipt.get("parsed") or {}
        state = str(receipt["state"])
        return EvaluationResponse(
            state=state,
            raw_text=parsed.get("raw_text") if state == COMPLETED else None,
            finish_reason=parsed.get("finish_reason") if state == COMPLETED else None,
            usage=receipt.get("usage") if state == COMPLETED else None,
            cost_usd=SUBSCRIPTION_COST_USD if state == COMPLETED else None,
            latency_seconds=receipt.get("latency_seconds"),
            request_key=receipt["request_key"],
            request_sha256=receipt["request_sha256"],
            receipt_sha256=sha256_file(path) if path else None,
            receipt_file=str(path) if path else None,
            model_version=parsed.get("model_version") or receipt["model"],
            response_id=parsed.get("session_id") or parsed.get("thread_id"),
            error=receipt.get("error") if state != COMPLETED else None,
            resumed=resumed,
            harness=receipt.get("harness"),
        )

    def answer(self, request: EvaluationRequest) -> EvaluationResponse:
        trial = request.trial
        request_key = self.request_key(request)
        receipt_path = self.ledger.receipt_path(request_key)
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            return self._response(receipt, path=receipt_path, resumed=True)
        call_dir = self.scratch_root / request_key[:16]
        if call_dir.exists():
            shutil.rmtree(call_dir)
        call_dir.mkdir()
        try:
            invocation = self.invocation(trial, call_dir)
            request_sha256 = sha256_bytes(
                canonical_json(
                    {
                        "argv": invocation["argv"],
                        "stdin": invocation["stdin"],
                        "files": invocation["files"],
                    }
                ).encode()
            )
            stop = self.ledger.submit(request_key, trial)
            if stop is not None:
                receipt = self._receipt(
                    trial,
                    request_key,
                    request_sha256,
                    invocation,
                    result=None,
                    parsed=stop,
                    latency=None,
                    submitted_at=_utc_now(),
                )
                return self._response(receipt, path=None, resumed=False)
            submitted_at = _utc_now()
            started = time.monotonic()
            try:
                result = self.transport.run(invocation)
            except BaseException:
                # The row stays submitted; the next open settles it as
                # interrupted. Nothing is retried.
                self.ledger._release_inflight(request_key)
                raise
            latency = time.monotonic() - started
            if result.get("timed_out"):
                parsed: dict[str, Any] = {
                    "state": STATE_TIMEOUT,
                    "error": f"the harness did not finish within {self.timeout} s",
                }
            elif result.get("returncode") != 0:
                parsed = {
                    "state": STATE_FAILED,
                    "error": f"the harness exited with {result.get('returncode')}: "
                    f"{redact(str(result.get('stderr') or ''))[-500:]}",
                }
            elif self.vendor == PROVIDER_ANTHROPIC_CLAUDE_CODE:
                parsed = parse_claude_output(result["stdout"], model=trial["model"])
            else:
                parsed = parse_codex_output(result["stdout"], model=trial["model"])
            receipt = self._receipt(
                trial,
                request_key,
                request_sha256,
                invocation,
                result=result,
                parsed=parsed,
                latency=latency,
                submitted_at=submitted_at,
            )
            path = self.ledger.settle(request_key, receipt)
            return self._response(receipt, path=path, resumed=False)
        finally:
            shutil.rmtree(call_dir, ignore_errors=True)

    def _receipt(
        self,
        trial: dict[str, Any],
        request_key: str,
        request_sha256: str,
        invocation: dict[str, Any],
        *,
        result: dict[str, Any] | None,
        parsed: dict[str, Any],
        latency: float | None,
        submitted_at: str,
    ) -> dict[str, Any]:
        state = str(parsed.get("state") or STATE_FAILED)
        harness = {
            "vendor": self.vendor,
            "binary": self.binary,
            "harness_version": self._decoding.get("harness_version"),
            "model": trial["model"],
            "preset": trial["arm"],
            "argv": invocation["argv_display"],
            "env": {
                name: ("<home>" if name == "HOME" else value)
                for name, value in invocation["env"].items()
                if name != "PATH"
            },
            "cwd": "<empty scratch directory>",
            "stdin": "user_text",
            "final_text": parsed.get("final_text"),
            "side_call_models": parsed.get("side_call_models"),
            "schema_honoured": parsed.get("schema_honoured"),
            "harness_errors": parsed.get("harness_errors"),
            "harness_cost_estimate_usd": parsed.get("harness_cost_estimate_usd"),
            "returncode": result.get("returncode") if result else None,
        }
        return {
            "schema": SUBSCRIPTION_RECEIPT_SCHEMA,
            "request_key": request_key,
            "request_sha256": request_sha256,
            "phase": EVALUATION_PHASE,
            "stage": evaluation_stage(trial["model"]),
            "run_id": self.run_id,
            "vendor": self.vendor,
            "model": trial["model"],
            "arm": trial["arm"],
            "trial": {
                name: trial[name]
                for name in (
                    "trial_id",
                    "eval_set_id",
                    "item_id",
                    "condition",
                    "arm",
                    "repeat",
                )
            },
            "billing": "subscription",
            "cost_usd": SUBSCRIPTION_COST_USD,
            "state": state,
            "error": parsed.get("error"),
            "usage": parsed.get("usage"),
            "parsed": {
                key: value
                for key, value in parsed.items()
                if key not in {"usage", "state", "error"}
            },
            "harness": harness,
            "invocation": {
                "argv": invocation["argv_display"],
                "cwd_kind": "empty scratch directory",
                "stdin_sha256": sha256_bytes(str(invocation["stdin"]).encode()),
                "files": invocation["files"],
                "timeout_seconds": invocation["timeout_seconds"],
            },
            "stdout": (result or {}).get("stdout"),
            "stderr_tail": redact(str((result or {}).get("stderr") or ""))[-2000:],
            "latency_seconds": latency,
            "submitted_at_utc": submitted_at,
            "completed_at_utc": _utc_now(),
        }


# --- Run wiring --------------------------------------------------------------------


def build_subscription_provider(
    *,
    vendor: str,
    set_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats: int,
    ledger_dir: Path,
    evaluation_policy_file: Path,
    evaluation_gate_file: Path,
    subscription_models_file: Path,
    scratch_root: Path | None = None,
    transport: SubscriptionTransport | None = None,
    binary: str | None = None,
) -> tuple[SubscriptionEvaluationProvider, dict[str, Any]]:
    """Bind the run to the gate through the subscription ledger."""
    from .abstention_set import load_eval_set

    config = load_subscription_models(subscription_models_file)
    entry = vendor_entry(config, vendor)
    binary = binary or str(entry["binary"])
    transport = transport or SubprocessTransport()
    ledger_dir.mkdir(parents=True, exist_ok=True)
    version = binary_version(vendor, binary, transport, cwd=ledger_dir)
    decoding = subscription_decoding_record(
        config, vendor, models, arms, binary=binary, binary_version=version
    )
    manifest, _ = load_eval_set(set_dir)
    binding = run_binding_record(
        eval_set_id=manifest["eval_set_id"],
        run_id=run_id,
        models=models,
        arms=arms,
        repeats=repeats,
        decoding=decoding,
    )
    ledger = SubscriptionLedger(
        ledger_dir=ledger_dir,
        vendor=vendor,
        policy_file=evaluation_policy_file,
        gate_file=evaluation_gate_file,
        models_file=subscription_models_file,
    )
    ledger.bind(binding, eval_set_manifest_file=set_dir / "manifest.json")
    provider = SubscriptionEvaluationProvider(
        vendor=vendor,
        ledger=ledger,
        models_config=config,
        decoding=decoding,
        run_id=run_id,
        scratch_root=scratch_root or (ledger_dir / SCRATCH_DIRNAME),
        transport=transport,
        binary=binary,
    )
    return provider, decoding


def subscription_gate_record(
    *,
    vendor: str,
    set_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats_maximum: int,
    decoding: dict[str, Any],
    evaluation_policy_file: Path,
    subscription_models_file: Path,
    integrated_code_commit: str,
    review_record: Path,
    review_verdict: str,
) -> dict[str, Any]:
    """Return one subscription evaluation gate (same schema as the Gemini gate)."""
    from .abstention_set import load_eval_set

    manifest, _ = load_eval_set(set_dir)
    return {
        "schema": EVALUATION_GATE_SCHEMA,
        "provider": vendor,
        "evaluation_enabled": review_verdict == "pass",
        "allowed_phase": EVALUATION_PHASE,
        "eval_set_id": manifest["eval_set_id"],
        "eval_set_manifest_sha256": manifest_sha256(set_dir),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "abstention_option_text": ABSTENTION_OPTION_TEXT,
        "models": sorted(models),
        "arms": sorted(arms),
        "decoding": decoding,
        "repeats_maximum": repeats_maximum,
        "authorized_run_id": run_id,
        "evaluation_policy_sha256": sha256_file(evaluation_policy_file),
        "evaluation_price_config_sha256": sha256_file(subscription_models_file),
        "price_config_slot": "subscription models file (USD 0 per call)",
        "integrated_code_commit": integrated_code_commit,
        "independent_review_verdict": review_verdict,
        "review_record": str(review_record.resolve()),
        "review_record_sha256": sha256_file(review_record),
        "written_at_utc": _utc_now(),
    }


# --- Model enumeration -----------------------------------------------------------


def codex_model_catalog(
    binary: str, *, bundled: bool = False, timeout: float = 120.0
) -> list[dict[str, Any]]:
    """Return the raw Codex model catalog (``codex debug models``)."""
    argv = [binary, "debug", "models"]
    if bundled:
        argv.append("--bundled")
    env = harness_environment(
        PROVIDER_OPENAI_CODEX,
        home=os.environ.get("HOME", "/"),
        codex_home=os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")),
    )
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )
    if result.returncode != 0:
        raise ValueError(f"codex debug models failed: {redact(result.stderr)[-300:]}")
    text = (
        result.stdout[result.stdout.find("{") :]
        if "{" in result.stdout
        else result.stdout
    )
    value = json.loads(text)
    models = value.get("models") if isinstance(value, dict) else value
    return list(models or [])


def annotate_subscription_models(
    config: dict[str, Any], vendor: str, catalog: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Join the registry of one vendor with the live catalog of its binary."""
    entry = vendor_entry(config, vendor)
    rows = []
    seen = set()
    for slug, record in entry["models"].items():
        live = next((row for row in catalog or [] if row.get("slug") == slug), None)
        seen.add(slug)
        rows.append(
            {
                "model": slug,
                "display_name": record.get("display_name"),
                "tier": record.get("tier"),
                "registered": True,
                "presets": list(record["presets"]),
                "in_live_catalog": live is not None if catalog is not None else None,
                "live_presets": (
                    [
                        row.get("effort")
                        for row in live.get("supported_reasoning_levels") or []
                    ]
                    if live
                    else None
                ),
                "live_default_preset": live.get("default_reasoning_level")
                if live
                else None,
                "live_visibility": live.get("visibility") if live else None,
                "context_window": live.get("context_window") if live else None,
                "temperature": None,
                "cost_usd_per_call": SUBSCRIPTION_COST_USD,
            }
        )
    for row in catalog or []:
        slug = str(row.get("slug") or "")
        if slug in seen or not slug:
            continue
        rows.append(
            {
                "model": slug,
                "display_name": row.get("display_name"),
                "tier": None,
                "registered": False,
                "presets": None,
                "in_live_catalog": True,
                "live_presets": [
                    level.get("effort")
                    for level in row.get("supported_reasoning_levels") or []
                ],
                "live_default_preset": row.get("default_reasoning_level"),
                "live_visibility": row.get("visibility"),
                "context_window": row.get("context_window"),
                "temperature": None,
                "cost_usd_per_call": SUBSCRIPTION_COST_USD,
            }
        )
    return rows


# --- Dry run with the scripted transport ----------------------------------------


def subscription_dry_run_provider(
    *,
    vendor: str,
    set_dir: Path,
    ledger_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats: int,
    trials: list[dict[str, Any]],
    evaluation_policy_file: Path,
    subscription_models_file: Path,
    policy: str = "random",
    seed: str = "dry-run",
    overrides: dict[str, dict[str, Any]] | None = None,
    latency_seconds: float = 0.0,
    code_commit: str | None = None,
    binary: str | None = None,
) -> tuple[SubscriptionEvaluationProvider, dict[str, Any], Any, Path]:
    """Build the private dry-run gate, ledger and scripted provider of a vendor."""
    from .abstention_run import dry_run_policy

    ledger_dir.mkdir(parents=True, exist_ok=True)
    review = ledger_dir / "dry-run-review.md"
    if not review.is_file():
        review.write_text(
            "Dry run: scripted harness transport, no subprocess, no login read.\n",
            encoding="utf-8",
        )
    config = load_subscription_models(subscription_models_file)
    binary = binary or str(vendor_entry(config, vendor)["binary"])
    transport = ScriptedSubscriptionTransport(
        scripted_subscription_answers(
            vendor, trials, policy=policy, seed=seed, overrides=overrides
        ),
        version="dry-run",
        latency_seconds=latency_seconds,
    )
    decoding = subscription_decoding_record(
        config, vendor, models, arms, binary=binary, binary_version="dry-run"
    )
    evaluation_policy_file = dry_run_policy(
        evaluation_policy_file, ledger_dir / "evaluation-policy-dry-run.json"
    )
    gate_path = ledger_dir / "evaluation-gate.json"
    atomic_json(
        gate_path,
        subscription_gate_record(
            vendor=vendor,
            set_dir=set_dir,
            run_id=run_id,
            models=models,
            arms=arms,
            repeats_maximum=max(repeats, 1),
            decoding=decoding,
            evaluation_policy_file=evaluation_policy_file,
            subscription_models_file=subscription_models_file,
            integrated_code_commit=code_commit or "dry-run",
            review_record=review,
            review_verdict="pass",
        ),
    )
    provider, bound_decoding = build_subscription_provider(
        vendor=vendor,
        set_dir=set_dir,
        run_id=run_id,
        models=models,
        arms=arms,
        repeats=repeats,
        ledger_dir=ledger_dir,
        evaluation_policy_file=evaluation_policy_file,
        evaluation_gate_file=gate_path,
        subscription_models_file=subscription_models_file,
        transport=transport,
        binary=binary,
    )
    return provider, bound_decoding, transport, gate_path


def subscription_dry_run(
    *,
    vendor: str,
    set_dir: Path,
    output_dir: Path,
    run_id: str,
    models: list[str],
    arms: list[str],
    repeats: int,
    evaluation_policy_file: Path,
    subscription_models_file: Path,
    policy: str = "random",
    seed: str = "dry-run",
    overrides: dict[str, dict[str, Any]] | None = None,
    max_calls: int | None = None,
    item_limit: int | None = None,
    code_commit: str | None = None,
    binary: str | None = None,
) -> dict[str, Any]:
    """Run the whole subscription path offline: gate, ledger, receipts, scoring."""
    from .abstention_run import plan_trials, run_evaluation
    from .abstention_set import load_eval_set

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest, items_all = load_eval_set(set_dir)
    items = items_all[: int(item_limit)] if item_limit is not None else items_all
    trials = plan_trials(manifest, items, models=models, arms=arms, repeats=repeats)
    provider, bound_decoding, transport, _ = subscription_dry_run_provider(
        vendor=vendor,
        set_dir=set_dir,
        ledger_dir=output_dir / "ledger",
        run_id=run_id,
        models=models,
        arms=arms,
        repeats=repeats,
        trials=trials,
        evaluation_policy_file=evaluation_policy_file,
        subscription_models_file=subscription_models_file,
        policy=policy,
        seed=seed,
        overrides=overrides,
        code_commit=code_commit,
        binary=binary,
    )
    summary = run_evaluation(
        set_dir=set_dir,
        output_dir=output_dir,
        run_id=run_id,
        models=models,
        arms=arms,
        repeats=repeats,
        provider=provider,
        decoding=bound_decoding,
        max_calls=max_calls,
        code_commit=code_commit,
        item_limit=item_limit,
    )
    summary["dry_run"] = {
        "ledger_file": str(provider.ledger.ledger_file),
        "scripted_policy": policy,
        "transport_calls": len(
            [row for row in transport.invocations if row.get("purpose") == "trial"]
        ),
        "ledger": provider.ledger.status(),
    }
    atomic_json(output_dir / "run-summary.json", summary)
    return summary
