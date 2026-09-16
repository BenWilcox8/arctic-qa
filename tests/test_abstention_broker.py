from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.abstention_providers import (
    ScriptedTransport,
    decoding_record,
    evaluation_payload,
    scripted_key,
)
from arctic_qa.abstention_render import prompt_sha256
from arctic_qa.abstention_run import write_evaluation_gate
from arctic_qa.abstention_set import write_eval_set
from arctic_qa.model_broker import (
    EVALUATION_CEILING_REASON,
    EVALUATION_PHASE,
    PER_REQUEST_CAP_REASON,
    SharedGeminiBroker,
    broker_request_key,
    evaluation_stage,
    is_evaluation_stage,
    stage_supported,
)
from arctic_qa.util import sha256_file
from test_abstention_render import item
from test_model_broker import Transport, payload, write_json


ROOT = Path(__file__).parents[1]
PRO = "gemini-3.1-pro-preview"


def _write_policy(path: Path, **changes: object) -> Path:
    policy = json.loads(
        (ROOT / "config" / "benchmark-evaluation-policy-v1.json").read_text()
    )
    policy.update(changes)
    write_json(path, policy)
    return path


def evaluation_fixture(
    tmp_path: Path,
    *,
    transport=None,
    models: list[str] | None = None,
    arms: list[str] | None = None,
    policy_changes: dict | None = None,
    repeats: int = 2,
    run_id: str = "eval-run-1",
) -> dict:
    """A broker with construction files plus the evaluation policy, prices, gate."""
    models = models or [PRO]
    arms = arms or ["medium"]
    construction_gate = tmp_path / "construction-gate.json"
    write_json(
        construction_gate,
        json.loads((ROOT / "config" / "streaming-live-execution-gate-v1.json").read_text()),
    )
    credential = tmp_path / "private" / "gemini.key"
    credential.parent.mkdir(mode=0o700, exist_ok=True)
    credential.write_text("unused-test-key", encoding="utf-8")
    credential.chmod(0o600)
    set_manifest = write_eval_set(
        [item(item_id="aqa-one"), item(item_id="aqa-two")],
        excluded=[],
        output_dir=tmp_path / "sets",
        population_record={"population": "list"},
        k=4,
        state_db=None,
    )
    set_dir = tmp_path / "sets" / set_manifest["eval_set_id"]
    policy_file = _write_policy(tmp_path / "evaluation-policy.json", **(policy_changes or {}))
    prices_file = ROOT / "config" / "benchmark-evaluation-prices-v1.json"
    price_config = json.loads(prices_file.read_text())
    decoding = decoding_record(price_config, models, arms)
    review = tmp_path / "review.md"
    review.write_text("pass\n", encoding="utf-8")
    gate = tmp_path / "evaluation-gate.json"
    write_evaluation_gate(
        gate,
        set_dir=set_dir,
        run_id=run_id,
        models=models,
        arms=arms,
        repeats_maximum=repeats,
        decoding=decoding,
        evaluation_policy_file=policy_file,
        evaluation_price_config_file=prices_file,
        integrated_code_commit="fixture-commit",
        review_record=review,
    )
    broker = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=construction_gate,
        ledger_file=tmp_path / "shared-ledger.json",
        receipts_dir=tmp_path / "receipts",
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        evaluation_policy_file=policy_file,
        evaluation_price_config_file=prices_file,
        evaluation_gate_file=gate,
    )
    return {
        "broker": broker,
        "set_dir": set_dir,
        "set_manifest": set_manifest,
        "gate": gate,
        "policy": policy_file,
        "prices": prices_file,
        "decoding": decoding,
        "models": models,
        "arms": arms,
        "repeats": repeats,
        "run_id": run_id,
        "ledger": tmp_path / "shared-ledger.json",
        "construction_gate": construction_gate,
    }


def bind(values: dict, **changes: object) -> None:
    binding = {
        "eval_set_manifest_file": values["set_dir"] / "manifest.json",
        "eval_set_id": values["set_manifest"]["eval_set_id"],
        "run_id": values["run_id"],
        "models": values["models"],
        "arms": values["arms"],
        "repeats": values["repeats"],
        "prompt_version": "abstention-eval-prompt-v1",
        "prompt_sha256": prompt_sha256(),
        "abstention_option_text": "I abstain from answering",
        "decoding": values["decoding"],
    }
    binding.update(changes)
    values["broker"].bind_evaluation(**binding)


def trial_payload(values: dict, *, model: str = PRO, arm: str = "medium", text: str = "Q") -> dict:
    return evaluation_payload(
        system_text="Reply with one letter.",
        user_text=text,
        letters="ABCDE",
        temperature=values["decoding"]["temperature_by_model"][model],
        max_output_tokens=values["decoding"]["max_output_tokens_by_arm"][arm],
        arm=arm,
    )


def execute(values: dict, *, trial_id: str, condition: str = "gold_present", repeat: int = 1,
            model: str = PRO, arm: str = "medium", text: str = "Q", item_id: str = "aqa-one") -> dict:
    request_payload = trial_payload(values, model=model, arm=arm, text=text)
    identity = {
        "paper_id": item_id,
        "family_id": f"evaluation-item:{item_id}",
        "source_version_id": f"evaluation-item:{item_id}:hash",
    }
    key = broker_request_key(
        model=model,
        run_id=values["run_id"],
        phase=EVALUATION_PHASE,
        stage=evaluation_stage(model),
        payload=request_payload,
        trial_id=trial_id,
        **identity,
    )
    return values["broker"].execute(
        phase=EVALUATION_PHASE,
        run_id=values["run_id"],
        stage=evaluation_stage(model),
        request_key=key,
        payload=request_payload,
        trial={
            "trial_id": trial_id,
            "eval_set_id": values["set_manifest"]["eval_set_id"],
            "item_id": item_id,
            "condition": condition,
            "arm": arm,
            "repeat": repeat,
        },
        **identity,
    )


class LetterTransport(Transport):
    def __init__(self, letter: str = "B", thinking: int = 500) -> None:
        super().__init__()
        self.letter = letter
        self.thinking = thinking

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 250}
        return {
            "responseId": "letter-response",
            "modelVersion": model,
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": self.letter}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": 250,
                "candidatesTokenCount": 1,
                "thoughtsTokenCount": self.thinking,
                "totalTokenCount": 251 + self.thinking,
            },
        }


def test_stage_family_and_request_key_bind_trials() -> None:
    assert is_evaluation_stage("evaluation_answer:gemini-3.1-pro-preview")
    assert not is_evaluation_stage("evaluation_answer:Bad Model")
    assert stage_supported("question_generation") and stage_supported(evaluation_stage(PRO))
    assert not stage_supported("evaluation_answer")
    with pytest.raises(ValueError, match="cannot form a stage"):
        evaluation_stage("Gemini Pro")
    common = dict(model=PRO, run_id="r", phase=EVALUATION_PHASE, stage=evaluation_stage(PRO),
                  paper_id="p", family_id="f", source_version_id="s", payload=payload())
    first = broker_request_key(trial_id="t1", **common)
    second = broker_request_key(trial_id="t2", **common)
    assert first != second
    assert broker_request_key(trial_id="t1", **common) == first
    assert broker_request_key(trial_id="t1", **{**common, "run_id": "other"}) != first
    with pytest.raises(ValueError, match="requires a trial id"):
        broker_request_key(**common)


def test_evaluation_call_meters_under_its_own_phase_and_never_touches_construction(tmp_path: Path) -> None:
    transport = LetterTransport("B")
    values = evaluation_fixture(tmp_path, transport=transport)
    with pytest.raises(ValueError, match="not bound"):
        execute(values, trial_id="t-unbound")
    bind(values)
    receipt = execute(values, trial_id="t1")
    assert receipt["state"] == "completed"
    assert receipt["evaluation_trial"]["trial_id"] == "t1"
    assert receipt["evaluation_policy_sha256"] == sha256_file(values["policy"])
    assert receipt["evaluation_price_config_sha256"] == sha256_file(values["prices"])
    assert receipt["gate_sha256"] == sha256_file(values["gate"])
    # gemini-3.1-pro-preview: 250 x 2e-6 + 501 x 12e-6 = 0.006512
    assert Decimal(receipt["actual_cost_usd"]) == Decimal("0.006512")
    assert transport.methods == ["countTokens", "generateContent"]
    status = values["broker"].status()
    assert status["usage"]["benchmark_evaluation_usd"] == "0.006512"
    assert Decimal(status["usage"]["dataset_construction_usd"]) == 0
    assert Decimal(status["usage"]["away_session_usd"]) == 0
    assert status["usage"]["project_lifetime_usd"] == "0.006512"
    assert status["usage"]["away_generation_submissions"] == 0
    assert status["remaining"]["benchmark_evaluation_usd"] == str(Decimal("500.00") - Decimal("0.006512"))
    assert status["remaining"]["project_lifetime_usd"] == str(Decimal("1000.00") - Decimal("0.006512"))
    assert Decimal(status["remaining"]["away_session_usd"]) == Decimal("25.00")
    assert status["evaluation"]["phase"] == EVALUATION_PHASE
    assert status["evaluation"]["spent_usd"] == "0.006512"
    assert status["evaluation"]["remaining_usd"] == str(Decimal("5.00") - Decimal("0.006512"))
    assert status["evaluation"]["thinking_tokens"] == 500
    assert set(status["stages"]) == {evaluation_stage(PRO)}
    ledger = json.loads(values["ledger"].read_text())
    request = next(iter(ledger["requests"].values()))
    assert request["phase"] == EVALUATION_PHASE and request["stage"] == evaluation_stage(PRO)
    assert ledger["family_bindings"]["evaluation-item:aqa-one"]["paper_id"] == "aqa-one"
    assert ledger["live_test_papers"] == {}
    # The same trial is never replayed; a second repeat is a new request.
    with pytest.raises(ValueError, match="already exists"):
        execute(values, trial_id="t1")
    second = execute(values, trial_id="t2", repeat=2)
    assert second["state"] == "completed" and second["request_key"] != receipt["request_key"]
    assert values["broker"].status()["evaluation"]["submissions"] == 2


def test_evaluation_ceiling_reserve_and_repeat_limit_are_enforced(tmp_path: Path) -> None:
    values = evaluation_fixture(
        tmp_path,
        transport=LetterTransport("A"),
        policy_changes={"evaluation_ceiling_usd": "0.05", "maximum_calls_per_item_condition_model_arm": 1},
    )
    bind(values)
    # Reservation at the 4096-token medium cap: 250 x 2e-6 + 4096 x 12e-6 = 0.049652.
    first = execute(values, trial_id="t1")
    assert first["state"] == "completed"
    blocked = execute(values, trial_id="t2", repeat=2)
    assert blocked["state"] == "not_submitted"
    assert blocked["reason"] == EVALUATION_CEILING_REASON
    assert blocked["live_call_made"] is False
    other_item = execute(values, trial_id="t3", item_id="aqa-two")
    assert other_item["state"] == "not_submitted"  # the ceiling, not the repeat limit
    generous = evaluation_fixture(
        tmp_path / "generous",
        transport=LetterTransport("A"),
        policy_changes={"maximum_calls_per_item_condition_model_arm": 1},
    )
    bind(generous)
    assert execute(generous, trial_id="t1")["state"] == "completed"
    repeat = execute(generous, trial_id="t2", repeat=2)
    assert repeat["state"] == "not_submitted"
    assert "repeat limit" in repeat["reason"]
    assert execute(generous, trial_id="t3", condition="gold_absent")["state"] == "completed"
    # The per-request cap of the evaluation policy still applies.
    capped = evaluation_fixture(
        tmp_path / "capped",
        transport=LetterTransport("A"),
        policy_changes={"maximum_request_reserved_cost_usd": "0.01"},
    )
    bind(capped)
    assert execute(capped, trial_id="t1")["reason"] == PER_REQUEST_CAP_REASON


def test_evaluation_gate_binding_stops_a_changed_set_prompt_model_or_run(tmp_path: Path) -> None:
    values = evaluation_fixture(tmp_path, transport=LetterTransport("A"))
    with pytest.raises(ValueError, match="prompt changed"):
        bind(values, prompt_sha256="0" * 64)
    with pytest.raises(ValueError, match="model list changed"):
        bind(values, models=["gemini-3.8-flash"])
    with pytest.raises(ValueError, match="thinking arms changed"):
        bind(values, arms=["high"])
    with pytest.raises(ValueError, match="run identity changed"):
        bind(values, run_id="another-run")
    with pytest.raises(ValueError, match="repeat count changed"):
        bind(values, repeats=3)
    with pytest.raises(ValueError, match="decoding settings changed"):
        bind(values, decoding={**values["decoding"], "temperature_by_model": {PRO: "1.0"}})
    bind(values)
    # A manifest edit after binding stops before countTokens.
    manifest_path = values["set_dir"] / "manifest.json"
    original = manifest_path.read_bytes()
    manifest_path.write_bytes(original.replace(b'"k":4', b'"k":5'))
    transport = values["broker"].transport
    with pytest.raises(ValueError, match="manifest changed"):
        execute(values, trial_id="t1")
    assert transport.methods == []
    manifest_path.write_bytes(original)
    # A disabled gate stops the broker too.
    gate = json.loads(values["gate"].read_text())
    write_json(values["gate"], {**gate, "evaluation_enabled": False})
    with pytest.raises(ValueError, match="evaluation is disabled"):
        execute(values, trial_id="t1")
    write_json(values["gate"], gate)
    assert execute(values, trial_id="t1")["state"] == "completed"


def test_evaluation_payload_rules_pin_temperature_preset_and_enum(tmp_path: Path) -> None:
    values = evaluation_fixture(tmp_path, transport=LetterTransport("A"))
    bind(values)
    broker = values["broker"]
    identity = {"paper_id": "aqa-one", "family_id": "evaluation-item:aqa-one", "source_version_id": "s"}

    def attempt(request_payload: dict, *, stage: str = evaluation_stage(PRO), phase: str = EVALUATION_PHASE, trial=None, key_trial_id: str | None = None):
        key = broker_request_key(
            model=PRO, run_id=values["run_id"], phase=phase, stage=stage, payload=request_payload,
            trial_id=key_trial_id or (trial or {}).get("trial_id"), **identity,
        )
        return broker.execute(
            phase=phase, run_id=values["run_id"], stage=stage, request_key=key,
            payload=request_payload, trial=trial, **identity,
        )

    trial = {"trial_id": "t", "eval_set_id": "x", "item_id": "aqa-one", "condition": "gold_present", "arm": "medium", "repeat": 1}
    good = trial_payload(values)
    cold = json.loads(json.dumps(good))
    cold["generationConfig"]["temperature"] = 1.0
    with pytest.raises(ValueError, match="pinned maximum"):
        attempt(cold, trial=trial)
    wrong_arm = json.loads(json.dumps(good))
    wrong_arm["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "ultra"}
    with pytest.raises(ValueError, match="official preset"):
        attempt(wrong_arm, trial=trial)
    budget = json.loads(json.dumps(good))
    budget["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 1024}
    with pytest.raises(ValueError, match="official preset"):
        attempt(budget, trial=trial)
    with pytest.raises(ValueError, match="needs its trial record"):
        attempt(good, key_trial_id="t")
    with pytest.raises(ValueError, match="requires a trial id"):
        attempt(good)
    with pytest.raises(ValueError, match="does not match its stage family"):
        attempt(good, stage="question_generation", trial=trial)
    with pytest.raises(ValueError, match="cannot carry a trial record"):
        attempt(payload(), stage="question_generation", phase="live_test", trial=trial)
    assert broker.transport.methods == []


def test_construction_broker_without_evaluation_files_rejects_evaluation_work(tmp_path: Path) -> None:
    values = evaluation_fixture(tmp_path, transport=LetterTransport("A"))
    bind(values)
    assert execute(values, trial_id="t1")["state"] == "completed"
    plain = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["construction_gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=LetterTransport("A"),
    )
    # The shared ledger with an evaluation request still validates, and the
    # construction view excludes the evaluation spend.
    status = plain.status()
    assert Decimal(status["usage"]["benchmark_evaluation_usd"]) > 0
    assert Decimal(status["usage"]["dataset_construction_usd"]) == 0
    assert status["evaluation"]["policy_id"] is None
    assert not plain.evaluation_enabled()
    with pytest.raises(ValueError, match="no benchmark evaluation"):
        execute({**values, "broker": plain}, trial_id="t9")
    with pytest.raises(ValueError, match="together"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["construction_gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            evaluation_policy_file=values["policy"],
        )


def test_evaluation_policy_and_price_config_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exceeds the evaluation reserve"):
        evaluation_fixture(tmp_path / "a", policy_changes={"evaluation_ceiling_usd": "600"})
    with pytest.raises(ValueError, match="permits retries"):
        evaluation_fixture(tmp_path / "b", policy_changes={"automatic_transport_retries": 1})
    with pytest.raises(ValueError, match="re_ask_on_invalid_response"):
        evaluation_fixture(tmp_path / "c", policy_changes={"re_ask_on_invalid_response": True})
    with pytest.raises(ValueError, match="exceeds the construction cap"):
        evaluation_fixture(tmp_path / "d", policy_changes={"maximum_request_reserved_cost_usd": "1.00"})
    with pytest.raises(ValueError, match="not an official preset"):
        evaluation_fixture(tmp_path / "e", arms=["ultra"])
    with pytest.raises(ValueError, match="no price entry"):
        evaluation_fixture(tmp_path / "f", models=["gemini-9-pro"])


def test_ambiguous_evaluation_charge_halts_and_scripted_transport_keys_by_model(tmp_path: Path) -> None:
    values = evaluation_fixture(tmp_path, transport=Transport(failure="generate"))
    bind(values)
    receipt = execute(values, trial_id="t1")
    assert receipt["state"] == "ambiguous_charge"
    status = values["broker"].status()
    assert status["halted"] is True
    assert Decimal(status["evaluation"]["ambiguous_usd"]) > 0
    assert Decimal(status["usage"]["dataset_construction_usd"]) == 0
    with pytest.raises(ValueError, match="halted"):
        execute(values, trial_id="t2", repeat=2)
    body = trial_payload(values)
    transport = ScriptedTransport({scripted_key(PRO, body): {"text": "C"}})
    assert transport.post(PRO, "generateContent", body)["candidates"][0]["content"]["parts"] == [{"text": "C"}]
    with pytest.raises(ValueError, match="no answer for this payload"):
        transport.post("gemini-3.8-flash", "generateContent", body)
