from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from arctic_qa.abstention_providers import (
    COMPLETED,
    EvaluationRequest,
    evaluation_payload,
)
from arctic_qa.abstention_render import GOLD_ABSENT, GOLD_PRESENT, N0, N1, N5
from arctic_qa.abstention_run import (
    RESPONSES_FILENAME,
    RUN_MANIFEST_FILENAME,
    plan_trials,
    run_evaluation,
)
from arctic_qa.abstention_set import evaluation_identity, load_eval_set, write_eval_set
from arctic_qa.abstention_subscription import (
    CLAUDE_ISOLATION_FLAGS,
    CODEX_CONFIG_TOML,
    CODEX_ISOLATION_FLAGS,
    PROVIDER_ANTHROPIC_CLAUDE_CODE,
    PROVIDER_OPENAI_CODEX,
    STATE_FAILED,
    STATE_POLICY_STOP,
    STATE_TIMEOUT,
    ScriptedSubscriptionTransport,
    SubprocessTransport,
    SubscriptionLedger,
    annotate_subscription_models,
    build_subscription_provider,
    load_subscription_models,
    parse_claude_output,
    parse_codex_output,
    scripted_subscription_answers,
    subscription_decoding_record,
    subscription_dry_run,
    subscription_gate_record,
)
from arctic_qa.cli import main as cli_main
from arctic_qa.errors import HarnessUnavailableError
from arctic_qa.util import atomic_json
from test_abstention_render import item


ROOT = Path(__file__).parents[1]
MODELS_FILE = ROOT / "config" / "benchmark-evaluation-subscription-models-v1.json"
POLICY_FILE = ROOT / "config" / "benchmark-evaluation-policy-v1.json"
CLAUDE = "claude-opus-5"
CODEX = "gpt-5.6-terra"
VENDOR_MODEL = {PROVIDER_ANTHROPIC_CLAUDE_CODE: CLAUDE, PROVIDER_OPENAI_CODEX: CODEX}


def frozen_set(tmp_path: Path, count: int = 2) -> Path:
    manifest = write_eval_set(
        [item(item_id=f"aqa-{index}") for index in range(count)],
        excluded=[],
        output_dir=tmp_path / "sets",
        population_record={"population": "list"},
        k=4,
        state_db=None,
    )
    return tmp_path / "sets" / manifest["eval_set_id"]


def fixture(
    tmp_path: Path,
    vendor: str,
    *,
    policy: str = "gold",
    overrides: dict | None = None,
    repeats: int = 1,
    run_id: str = "sub-run-1",
    policy_changes: dict | None = None,
    gate_changes: dict | None = None,
    item_count: int = 2,
) -> dict:
    """A frozen set, a passing gate, a scripted transport and a bound provider."""
    model = VENDOR_MODEL[vendor]
    set_dir = frozen_set(tmp_path, item_count)
    manifest, items = load_eval_set(set_dir)
    trials = plan_trials(
        manifest, items, models=[model], arms=["medium"], repeats=repeats
    )
    transport = ScriptedSubscriptionTransport(
        scripted_subscription_answers(
            vendor, trials, policy=policy, seed="test", overrides=overrides
        ),
        version="test-version 1.0",
    )
    policy_file = tmp_path / "policy.json"
    policy_record = json.loads(POLICY_FILE.read_text())
    policy_record.update(policy_changes or {})
    atomic_json(policy_file, policy_record)
    config = load_subscription_models(MODELS_FILE)
    decoding = subscription_decoding_record(
        config,
        vendor,
        [model],
        ["medium"],
        binary="/bin/harness",
        binary_version="test-version 1.0",
    )
    review = tmp_path / "review.md"
    review.write_text("pass\n", encoding="utf-8")
    gate = subscription_gate_record(
        vendor=vendor,
        set_dir=set_dir,
        run_id=run_id,
        models=[model],
        arms=["medium"],
        repeats_maximum=repeats,
        decoding=decoding,
        evaluation_policy_file=policy_file,
        subscription_models_file=MODELS_FILE,
        integrated_code_commit="fixture",
        review_record=review,
        review_verdict="pass",
    )
    gate.update(gate_changes or {})
    gate_file = tmp_path / "gate.json"
    atomic_json(gate_file, gate)
    ledger_dir = tmp_path / "ledger"
    provider, bound = build_subscription_provider(
        vendor=vendor,
        set_dir=set_dir,
        run_id=run_id,
        models=[model],
        arms=["medium"],
        repeats=repeats,
        ledger_dir=ledger_dir,
        evaluation_policy_file=policy_file,
        evaluation_gate_file=gate_file,
        subscription_models_file=MODELS_FILE,
        transport=transport,
        binary="/bin/harness",
    )
    return {
        "set_dir": set_dir,
        "items": items,
        "trials": trials,
        "transport": transport,
        "provider": provider,
        "decoding": bound,
        "ledger_dir": ledger_dir,
        "model": model,
        "policy_file": policy_file,
        "gate_file": gate_file,
    }


def run(values: dict, tmp_path: Path, **changes: object) -> dict:
    options = {
        "set_dir": values["set_dir"],
        "output_dir": tmp_path / "run",
        "run_id": "sub-run-1",
        "models": [values["model"]],
        "arms": ["medium"],
        "repeats": 1,
        "provider": values["provider"],
        "decoding": values["decoding"],
    }
    options.update(changes)
    return run_evaluation(**options)


def rows_of(tmp_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (tmp_path / "run" / RESPONSES_FILENAME).read_text().splitlines()
    ]


# --- registry and decoding ---------------------------------------------------------


def test_registry_and_decoding_record_bind_presets_and_usd_zero() -> None:
    config = load_subscription_models(MODELS_FILE)
    for vendor, model in VENDOR_MODEL.items():
        record = subscription_decoding_record(config, vendor, [model], ["medium"])
        assert record["billing"] == "subscription"
        assert record["cost_usd_per_call"] == "0"
        assert record["temperature_by_model"] == {model: None}
        assert "medium" in record["presets_by_model"][model]
        assert record["prompt_bytes"].startswith("identical to the Gemini rendering")
    with pytest.raises(ValueError, match="not an official preset"):
        subscription_decoding_record(
            config, PROVIDER_OPENAI_CODEX, ["gpt-5.5"], ["ultra"]
        )
    with pytest.raises(ValueError, match="not registered"):
        subscription_decoding_record(
            config,
            PROVIDER_ANTHROPIC_CLAUDE_CODE,
            ["gemini-3.1-pro-preview"],
            ["medium"],
        )
    for vendor in VENDOR_MODEL:
        assert not [
            name
            for name in os.environ
            if name.startswith(("CLAUDE", "ANTHROPIC_", "OPENAI_", "CODEX_"))
            and name
            in subscription_decoding_record(
                config, vendor, [VENDOR_MODEL[vendor]], ["low"]
            )["isolation_env"]
        ]


def test_annotate_models_joins_registry_with_live_catalog() -> None:
    config = load_subscription_models(MODELS_FILE)
    catalog = [
        {
            "slug": CODEX,
            "display_name": "GPT-5.6-Terra",
            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}],
            "default_reasoning_level": "medium",
            "visibility": "list",
            "context_window": 272000,
        },
        {"slug": "gpt-unknown", "display_name": "Unknown", "visibility": "hide"},
    ]
    rows = annotate_subscription_models(config, PROVIDER_OPENAI_CODEX, catalog)
    by_model = {row["model"]: row for row in rows}
    assert by_model[CODEX]["registered"] and by_model[CODEX]["in_live_catalog"]
    assert by_model[CODEX]["live_presets"] == ["low", "medium"]
    assert by_model["gpt-unknown"]["registered"] is False
    assert all(
        row["cost_usd_per_call"] == "0" and row["temperature"] is None for row in rows
    )
    claude_rows = annotate_subscription_models(
        config, PROVIDER_ANTHROPIC_CLAUDE_CODE, None
    )
    assert {row["model"] for row in claude_rows} >= {CLAUDE}
    assert all(row["in_live_catalog"] is None for row in claude_rows)


# --- invocations -------------------------------------------------------------------


def test_claude_invocation_is_isolated_and_sends_the_gemini_prompt_bytes(
    tmp_path: Path,
) -> None:
    values = fixture(tmp_path, PROVIDER_ANTHROPIC_CLAUDE_CODE)
    summary = run(values, tmp_path)
    assert summary["complete"] is True and summary["total_cost_usd"] == "0"
    calls = [
        row for row in values["transport"].invocations if row["purpose"] == "trial"
    ]
    assert len(calls) == 4
    trial_by_prompt = {
        (row["system_text"], row["user_text"]): row for row in values["trials"]
    }
    for call in calls:
        argv = call["argv"]
        assert argv[:2] == ["/bin/harness", "-p"]
        assert argv[argv.index("--model") + 1] == CLAUDE
        assert argv[argv.index("--effort") + 1] == "medium"
        for flag in CLAUDE_ISOLATION_FLAGS:
            assert flag in argv
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--setting-sources") + 1] == ""
        assert "--json-schema" not in argv and "--bare" not in argv
        assert argv[argv.index("--output-format") + 1] == "json"
        trial = trial_by_prompt[
            (argv[argv.index("--system-prompt") + 1], call["stdin"])
        ]
        # The bytes are the ones the Gemini payload carries.
        payload = evaluation_payload(
            system_text=trial["system_text"],
            user_text=trial["user_text"],
            letters=trial["letters"],
            temperature="2.0",
            max_output_tokens=4096,
            arm="medium",
        )
        assert (
            payload["systemInstruction"]["parts"][0]["text"]
            == argv[argv.index("--system-prompt") + 1]
        )
        assert payload["contents"][0]["parts"][0]["text"] == call["stdin"]
        env = call["env"]
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert not [
            name for name in env if name.startswith(("ANTHROPIC_", "OPENAI_", "CODEX_"))
        ]
        assert [name for name in env if name.startswith("CLAUDE")] == [
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
        ]
        assert Path(call["cwd"]).parent == values["ledger_dir"] / "scratch"
        assert not Path(call["cwd"]).exists()
    rows = rows_of(tmp_path)
    assert {row["enum_output"] for row in rows} == {False}
    harness = rows[0]["response"]["harness"]
    assert harness["vendor"] == PROVIDER_ANTHROPIC_CLAUDE_CODE
    assert harness["model"] == CLAUDE and harness["preset"] == "medium"
    assert harness["final_text"] == rows[0]["response"]["raw_text"]
    assert harness["harness_version"] == "test-version 1.0"
    assert "--no-session-persistence" in harness["argv"]


def test_codex_invocation_is_isolated_with_private_home_and_schema(
    tmp_path: Path,
) -> None:
    values = fixture(tmp_path, PROVIDER_OPENAI_CODEX)
    summary = run(values, tmp_path)
    assert summary["complete"] is True
    calls = [
        row for row in values["transport"].invocations if row["purpose"] == "trial"
    ]
    assert len(calls) == 4
    codex_home = values["ledger_dir"] / "codex-home"
    assert (codex_home / "config.toml").read_text() == CODEX_CONFIG_TOML
    assert (codex_home / "auth.json").is_symlink()
    assert 'web_search = "disabled"' in CODEX_CONFIG_TOML
    assert (
        "shell_tool = false" in CODEX_CONFIG_TOML
        and "enabled = false" in CODEX_CONFIG_TOML
    )
    for call in calls:
        argv = call["argv"]
        assert argv[:4] == ["/bin/harness", "exec", "-m", CODEX]
        for flag in CODEX_ISOLATION_FLAGS:
            assert flag in argv
        assert 'model_reasoning_effort="medium"' in argv
        assert argv[-1] == "-" and "--json" in argv and "--output-schema" in argv
        assert call["env"]["CODEX_HOME"] == str(codex_home)
        assert call["env"]["HOME"] == str(
            values["ledger_dir"] / "scratch" / "empty-home"
        )
        assert not [
            name
            for name in call["env"]
            if name.startswith(("OPENAI_", "ANTHROPIC_", "CLAUDE"))
        ]
        schema = json.loads(call["files"]["schema"])
        assert schema["properties"]["letter"]["enum"] == list("ABCDE")
        trial = next(
            row for row in values["trials"] if row["user_text"] == call["stdin"]
        )
        assert call["script"]["system_text"] == trial["system_text"]
        assert not Path(call["cwd"]).exists()
    rows = rows_of(tmp_path)
    assert {row["enum_output"] for row in rows} == {True}
    assert rows[0]["response"]["harness"]["final_text"] == json.dumps(
        {"letter": rows[0]["response"]["raw_text"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    assert rows[0]["response"]["harness"]["schema_honoured"] is True


# --- parsing, N0 filing and accounting ------------------------------------------------


@pytest.mark.parametrize("vendor", list(VENDOR_MODEL))
def test_invalid_answers_are_filed_as_n0_at_usd_zero(
    vendor: str, tmp_path: Path
) -> None:
    probe = fixture(tmp_path / "probe", vendor)
    trials = probe["trials"]
    overrides = {
        trials[0]["trial_id"]: {"text": "I choose B."},
        trials[1]["trial_id"]: {
            "text": trials[1]["correct_letter"],
            "finish_reason": "MAX_TOKENS",
        },
        trials[2]["trial_id"]: {"text": "Z"},
    }
    if vendor == PROVIDER_OPENAI_CODEX:
        overrides[trials[3]["trial_id"]] = {"final_text": "not json at all"}
    values = fixture(tmp_path, vendor, overrides=overrides)
    summary = run(values, tmp_path)
    rows = {row["trial_id"]: row for row in rows_of(tmp_path)}
    assert rows[trials[0]["trial_id"]]["outcome"] == N0
    assert rows[trials[0]["trial_id"]]["invalid_reason"] == "not_a_single_letter"
    assert rows[trials[0]["trial_id"]]["response"]["raw_text"] == "I choose B."
    assert rows[trials[2]["trial_id"]]["invalid_reason"] == "letter_outside_option_set"
    if vendor == PROVIDER_ANTHROPIC_CLAUDE_CODE:
        assert rows[trials[1]["trial_id"]]["invalid_reason"] == "finish_reason_not_stop"
        assert rows[trials[1]["trial_id"]]["response"]["finish_reason"] == "max_tokens"
        assert rows[trials[3]["trial_id"]]["outcome"] in {N1, N5}
    else:
        assert rows[trials[3]["trial_id"]]["invalid_reason"] == "not_a_single_letter"
        assert (
            rows[trials[3]["trial_id"]]["response"]["harness"]["schema_honoured"]
            is False
        )
    per_model = summary["per_model_arm"][0]
    assert per_model["cost_usd"] == "0" and per_model["cost_per_call_usd"] == "0.000000"
    assert per_model["tokens"]["thoughtsTokenCount"] == 4 * 40
    assert per_model["tokens"]["promptTokenCount"] == 4 * 200
    assert per_model["tokens"]["candidatesTokenCount"] == 4
    status = values["provider"].ledger.status()
    assert status["spent_usd"] == "0" and status["submissions"] == 4
    assert status["input_tokens"] == 800 and status["thinking_tokens"] == 160
    for row in rows.values():
        assert row["response"]["cost_usd"] == "0"
        receipt = json.loads(Path(row["response"]["receipt_file"]).read_text())
        assert receipt["billing"] == "subscription" and receipt["cost_usd"] == "0"
        assert receipt["phase"] == "benchmark_evaluation"
        assert receipt["stage"] == f"evaluation_answer:{values['model']}"


def test_gold_and_abstain_policies_classify_like_gemini(tmp_path: Path) -> None:
    values = fixture(tmp_path, PROVIDER_OPENAI_CODEX, policy="gold")
    run(values, tmp_path)
    rows = rows_of(tmp_path)
    assert {row["outcome"] for row in rows if row["condition"] == GOLD_PRESENT} == {N1}
    assert {row["outcome"] for row in rows if row["condition"] == GOLD_ABSENT} == {N5}
    manifest = json.loads((tmp_path / "run" / RUN_MANIFEST_FILENAME).read_text())
    assert manifest["provider"] == PROVIDER_OPENAI_CODEX
    assert manifest["decoding"]["billing"] == "subscription"
    assert manifest["decoding_by_model_arm"][f"{CODEX}/medium"]["thinking"] == {
        "effort": "medium"
    }


def test_harness_failure_timeout_and_resume(tmp_path: Path) -> None:
    probe = fixture(tmp_path / "probe", PROVIDER_ANTHROPIC_CLAUDE_CODE)
    trials = probe["trials"]
    values = fixture(
        tmp_path,
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        overrides={trials[1]["trial_id"]: {"raise": "exit", "stderr": "login expired"}},
    )
    summary = run(values, tmp_path)
    assert summary["complete"] is False
    assert summary["stopped_on"]["state"] == STATE_FAILED
    assert "login expired" in summary["stopped_on"]["error"]
    assert summary["recorded_trials"] == 2
    rows = rows_of(tmp_path)
    assert rows[1]["outcome"] == N0 and rows[1]["invalid_reason"] == STATE_FAILED
    # A rerun never re-asks the failed trial: it stays N0 and the run goes on.
    again = run(values, tmp_path)
    assert again["calls_this_invocation"] == 2 and again["recorded_trials"] == 4
    assert (
        again["complete"] is False
        and again["stopped_on"]["trial_id"] == trials[1]["trial_id"]
    )
    # With the responses file gone, the trials resume from their receipts with
    # no call, and the resumed failed receipt stops the run again.
    (tmp_path / "run" / RESPONSES_FILENAME).unlink()
    resumed = run(values, tmp_path)
    assert resumed["calls_this_invocation"] == 0 and resumed["recorded_trials"] == 2
    assert all(row["response"]["resumed"] for row in rows_of(tmp_path))
    assert rows_of(tmp_path)[1]["response"]["state"] == STATE_FAILED
    # A timeout is its own state.
    timeout_values = fixture(
        tmp_path / "timeout",
        PROVIDER_OPENAI_CODEX,
        overrides={probe["trials"][0]["trial_id"]: {"raise": "timeout"}},
    )
    # The trial ids depend on the model, so look the Codex trial up by item.
    codex_trial = timeout_values["trials"][0]
    timeout_values["transport"].answers = scripted_subscription_answers(
        PROVIDER_OPENAI_CODEX,
        timeout_values["trials"],
        policy="gold",
        seed="test",
        overrides={codex_trial["trial_id"]: {"raise": "timeout"}},
    )
    summary = run(timeout_values, tmp_path / "timeout")
    assert summary["stopped_on"]["state"] == STATE_TIMEOUT


def test_ledger_enforces_the_per_item_cap_and_pace(tmp_path: Path) -> None:
    values = fixture(
        tmp_path,
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        repeats=2,
        policy_changes={"maximum_calls_per_item_condition_model_arm": 1},
    )
    summary = run(values, tmp_path, repeats=2)
    assert summary["stopped_on"]["state"] == STATE_POLICY_STOP
    assert "per-item call cap" in summary["stopped_on"]["error"]
    # Pace: a ledger with the per-minute limit already used sleeps before the call.
    sleeps: list[float] = []
    clock = {"now": 1000.0}
    ledger = SubscriptionLedger(
        ledger_dir=tmp_path / "paced",
        vendor=PROVIDER_ANTHROPIC_CLAUDE_CODE,
        policy_file=values["policy_file"],
        gate_file=values["gate_file"],
        models_file=MODELS_FILE,
        sleep=lambda seconds: (
            sleeps.append(seconds),
            clock.__setitem__("now", clock["now"] + seconds),
        ),
        clock=lambda: clock["now"],
    )
    trial = values["trials"][0]
    ledger.policy["maximum_requests_per_minute"] = 2
    ledger.policy["maximum_calls_per_item_condition_model_arm"] = 10
    ledger.policy["maximum_concurrent_requests"] = 10
    assert ledger.submit("k1", trial) is None
    assert ledger.submit("k2", trial) is None
    assert ledger.submit("k3", trial) is None
    assert sleeps and sleeps[0] > 0
    assert ledger.submit("k3", trial) == {
        "state": STATE_POLICY_STOP,
        "error": "duplicate request key",
    }


def test_gate_binding_refuses_another_run_provider_or_models_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="another run_id"):
        fixture(
            tmp_path / "a",
            PROVIDER_OPENAI_CODEX,
            gate_changes={"authorized_run_id": "other"},
        )
    with pytest.raises(ValueError, match="another provider"):
        fixture(
            tmp_path / "b",
            PROVIDER_OPENAI_CODEX,
            gate_changes={"provider": PROVIDER_ANTHROPIC_CLAUDE_CODE},
        )
    with pytest.raises(ValueError, match="not a subscription record"):
        fixture(
            tmp_path / "c",
            PROVIDER_OPENAI_CODEX,
            gate_changes={"decoding": {"billing": "paid"}},
        )
    with pytest.raises(ValueError, match="another subscription models file"):
        fixture(
            tmp_path / "d",
            PROVIDER_OPENAI_CODEX,
            gate_changes={"evaluation_price_config_sha256": "0" * 64},
        )
    with pytest.raises(ValueError, match="benchmark evaluation is disabled"):
        fixture(
            tmp_path / "e",
            PROVIDER_OPENAI_CODEX,
            gate_changes={"evaluation_enabled": False},
        )


def test_request_key_binds_run_trial_and_prompt(tmp_path: Path) -> None:
    values = fixture(tmp_path, PROVIDER_OPENAI_CODEX)
    provider = values["provider"]
    trial = values["trials"][0]
    identity = evaluation_identity(values["items"][0])
    first = provider.request_key(
        EvaluationRequest(trial=trial, identity=identity, run_id="sub-run-1")
    )
    other_run = provider.request_key(
        EvaluationRequest(trial=trial, identity=identity, run_id="sub-run-2")
    )
    other_trial = provider.request_key(
        EvaluationRequest(
            trial={**trial, "trial_id": "x"}, identity=identity, run_id="sub-run-1"
        )
    )
    assert len({first, other_run, other_trial}) == 3


# --- real harness output samples -------------------------------------------------------


CLAUDE_SAMPLE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 1268,
    "duration_api_ms": 1989,
    "num_turns": 1,
    "result": "A",
    "stop_reason": "end_turn",
    "session_id": "d7448a60-bf29-4724-9eaf-a661dc550c8b",
    "total_cost_usd": 0.01004,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 844,
        "cache_read_input_tokens": 0,
        "output_tokens": 17,
        "output_tokens_details": {"thinking_tokens": 14},
    },
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 1070,
            "outputTokens": 19,
            "costUSD": 0.001165,
        },
        "claude-opus-5": {
            "inputTokens": 2,
            "outputTokens": 17,
            "thinkingTokens": 14,
            "costUSD": 0.008875,
        },
    },
}

CODEX_SAMPLE = """{"type":"thread.started","thread_id":"01a0a8bd-c9de-75c3-bd94-3254fe9b1c52"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Exceeded skills context budget."}}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"{\\"letter\\":\\"A\\"}"}}
{"type":"turn.completed","usage":{"input_tokens":3800,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":125,"reasoning_output_tokens":108}}
"""


def test_parse_real_claude_result_reports_tokens_and_side_calls() -> None:
    parsed = parse_claude_output(json.dumps(CLAUDE_SAMPLE), model="claude-opus-5")
    assert parsed["state"] == "completed" and parsed["raw_text"] == "A"
    assert parsed["finish_reason"] == "STOP"
    assert parsed["usage"]["promptTokenCount"] == 846
    assert parsed["usage"]["thoughtsTokenCount"] == 14
    assert parsed["usage"]["candidatesTokenCount"] == 3
    assert parsed["side_call_models"] == ["claude-haiku-4-5-20251001"]
    assert parsed["model_version"] == "claude-opus-5"
    assert (
        parse_claude_output("not json", model="claude-opus-5")["state"] == STATE_FAILED
    )
    failed = parse_claude_output(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "x",
            }
        ),
        model="claude-opus-5",
    )
    assert (
        failed["state"] == STATE_FAILED and "error_during_execution" in failed["error"]
    )


def test_parse_real_codex_events_reports_tokens_and_voids_tool_use() -> None:
    parsed = parse_codex_output(CODEX_SAMPLE, model=CODEX)
    assert parsed["state"] == "completed" and parsed["raw_text"] == "A"
    assert (
        parsed["final_text"] == '{"letter":"A"}' and parsed["schema_honoured"] is True
    )
    assert parsed["usage"]["promptTokenCount"] == 3800
    assert parsed["usage"]["thoughtsTokenCount"] == 108
    assert parsed["usage"]["candidatesTokenCount"] == 17
    assert parsed["harness_errors"] == ["Exceeded skills context budget."]
    searched = CODEX_SAMPLE.replace(
        '{"type":"turn.started"}',
        '{"type":"turn.started"}\n{"type":"item.completed","item":{"id":"i","type":"web_search","query":"q"}}',
    )
    voided = parse_codex_output(searched, model=CODEX)
    assert voided["state"] == STATE_FAILED and "web_search" in voided["error"]
    no_turn = parse_codex_output(
        '{"type":"thread.started"}\n{"type":"error","message":"boom"}\n', model=CODEX
    )
    assert no_turn["state"] == STATE_FAILED and "boom" in no_turn["error"]


# --- dry run and CLI -----------------------------------------------------------------


def test_subscription_dry_run_scores_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_dir = frozen_set(tmp_path, 3)
    summary = subscription_dry_run(
        vendor=PROVIDER_ANTHROPIC_CLAUDE_CODE,
        set_dir=set_dir,
        output_dir=tmp_path / "dry",
        run_id="dry-1",
        models=[CLAUDE],
        arms=["medium"],
        repeats=1,
        evaluation_policy_file=POLICY_FILE,
        subscription_models_file=MODELS_FILE,
        policy="abstain",
        binary="/bin/harness",
    )
    assert summary["complete"] is True and summary["dry_run"]["transport_calls"] == 6
    assert summary["dry_run"]["ledger"]["spent_usd"] == "0"
    gate = json.loads(
        (tmp_path / "dry" / "ledger" / "evaluation-gate.json").read_text()
    )
    assert gate["provider"] == PROVIDER_ANTHROPIC_CLAUDE_CODE
    code = cli_main(
        [
            "--json",
            "abstention-eval",
            "--action",
            "dry-run",
            "--provider",
            PROVIDER_OPENAI_CODEX,
            "--eval-set-dir",
            str(set_dir),
            "--run-dir",
            str(tmp_path / "cli"),
            "--run-id",
            "cli-1",
            "--models",
            CODEX,
            "--arms",
            "medium",
            "--scripted-policy",
            "gold",
            "--binary-path",
            "/bin/harness",
            "--bootstrap",
            "50",
        ]
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["complete"] is True
    group = output["scores"][f"{CODEX}/medium"]
    assert group["counts"]["N1"] == 3 and group["counts"]["N5"] == 3
    assert output["total_cost_usd"] == "0"


def test_a_vanished_harness_binary_reserves_nothing_and_records_nothing(
    tmp_path: Path,
) -> None:
    """The probe runs before the row, so the trial stays pending and free.

    The Claude Code binary was reinstalled at 12:06 UTC on 2026-09-17 and the
    path was gone for a moment. Without this probe the child process fails to
    spawn, the transport reports the ``OSError`` as an exit code with no
    return code, and the trial is recorded as a failed response at N0 that the
    no-retry contract forbids ever asking again. It also stopped the arm.
    """
    missing = tmp_path / "npm-global" / "bin" / "claude"
    values = fixture(tmp_path, PROVIDER_ANTHROPIC_CLAUDE_CODE)
    provider = values["provider"]
    provider.binary = str(missing)
    provider.transport = SubprocessTransport()
    request = EvaluationRequest(
        trial=values["trials"][0],
        identity=evaluation_identity(values["items"][0]),
        run_id="sub-run-1",
    )
    with pytest.raises(HarnessUnavailableError):
        provider.answer(request)
    # Nothing reserved, nothing submitted, nothing recorded.
    ledger = json.loads(
        (values["ledger_dir"] / "subscription-ledger.json").read_text(encoding="utf-8")
    )
    assert ledger["requests"] == {}
    assert list((values["ledger_dir"] / "receipts").glob("*.json")) == []
    # The binary comes back and the same trial runs, with no receipt to work
    # around and no stop on record.
    missing.parent.mkdir(parents=True)
    missing.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    missing.chmod(0o755)
    provider.transport = values["transport"]
    response = provider.answer(request)
    assert response.state == COMPLETED
