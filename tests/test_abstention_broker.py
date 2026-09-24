from __future__ import annotations

import json
import urllib.error
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.abstention_providers import (
    ScriptedTransport,
    decoding_record,
    evaluation_payload,
    scripted_key,
)
from arctic_qa.abstention_render import PROMPT_VERSION, prompt_sha256
from arctic_qa.abstention_run import write_evaluation_gate
from arctic_qa.abstention_set import write_eval_set
from arctic_qa.model_broker import (
    EVALUATION_CEILING_CHANGES,
    EVALUATION_CEILING_REASON,
    EVALUATION_PHASE,
    EVALUATION_POLICY_TRANSITION_SCHEMA,
    PER_REQUEST_CAP_REASON,
    SharedGeminiBroker,
    broker_request_key,
    evaluation_stage,
    is_evaluation_stage,
    stage_supported,
)
from arctic_qa.cli import main as cli_main
from arctic_qa.util import canonical_json, sha256_bytes, sha256_file
from arctic_qa import ledger_store, model_broker  # noqa: E402
from test_abstention_render import item
from test_model_broker import Transport, payload, write_json


ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def no_automatic_continuation(monkeypatch):
    """Drive the halt and the reviewed release, not the automatic continuation.

    An ambiguous charge of a bounded case releases itself while the hourly
    bound of its phase has room, so the halt these tests read is the one that
    stands once that bound is used up. The test of the automatic rule raises
    the bound itself.
    """
    monkeypatch.setattr(model_broker, "AUTOMATIC_CONTINUATION_LIMIT_PER_HOUR", 0)


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
    defer_transient_reservations: bool = False,
) -> dict:
    """A broker with construction files plus the evaluation policy, prices, gate."""
    models = models or [PRO]
    arms = arms or ["medium"]
    construction_gate = tmp_path / "construction-gate.json"
    write_json(
        construction_gate,
        json.loads(
            (ROOT / "config" / "streaming-live-execution-gate-v1.json").read_text()
        ),
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
    policy_file = _write_policy(
        tmp_path / "evaluation-policy.json", **(policy_changes or {})
    )
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
        defer_transient_reservations=defer_transient_reservations,
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
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "abstention_option_text": "I abstain from answering",
        "decoding": values["decoding"],
    }
    binding.update(changes)
    values["broker"].bind_evaluation(**binding)


def trial_payload(
    values: dict, *, model: str = PRO, arm: str = "medium", text: str = "Q"
) -> dict:
    return evaluation_payload(
        system_text="Reply with one letter.",
        user_text=text,
        letters="ABCDE",
        temperature=values["decoding"]["temperature_by_model"][model],
        max_output_tokens=values["decoding"]["max_output_tokens_by_arm"][arm],
        arm=arm,
    )


def execute(
    values: dict,
    *,
    trial_id: str,
    condition: str = "gold_present",
    repeat: int = 1,
    model: str = PRO,
    arm: str = "medium",
    text: str = "Q",
    item_id: str = "aqa-one",
) -> dict:
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
    assert stage_supported("question_generation") and stage_supported(
        evaluation_stage(PRO)
    )
    assert not stage_supported("evaluation_answer")
    with pytest.raises(ValueError, match="cannot form a stage"):
        evaluation_stage("Gemini Pro")
    common = dict(
        model=PRO,
        run_id="r",
        phase=EVALUATION_PHASE,
        stage=evaluation_stage(PRO),
        paper_id="p",
        family_id="f",
        source_version_id="s",
        payload=payload(),
    )
    first = broker_request_key(trial_id="t1", **common)
    second = broker_request_key(trial_id="t2", **common)
    assert first != second
    assert broker_request_key(trial_id="t1", **common) == first
    assert broker_request_key(trial_id="t1", **{**common, "run_id": "other"}) != first
    with pytest.raises(ValueError, match="requires a trial id"):
        broker_request_key(**common)


def test_evaluation_call_meters_under_its_own_phase_and_never_touches_construction(
    tmp_path: Path,
) -> None:
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
    assert status["remaining"]["benchmark_evaluation_usd"] == str(
        Decimal("500.00") - Decimal("0.006512")
    )
    assert status["remaining"]["project_lifetime_usd"] == str(
        Decimal("1000.00") - Decimal("0.006512")
    )
    assert Decimal(status["remaining"]["away_session_usd"]) == Decimal("25.00")
    assert status["evaluation"]["phase"] == EVALUATION_PHASE
    assert status["evaluation"]["spent_usd"] == "0.006512"
    assert status["evaluation"]["remaining_usd"] == str(
        Decimal("5.00") - Decimal("0.006512")
    )
    assert status["evaluation"]["thinking_tokens"] == 500
    assert set(status["stages"]) == {evaluation_stage(PRO)}
    ledger = json.loads(values["ledger"].read_text())
    request = next(iter(ledger["requests"].values()))
    assert request["phase"] == EVALUATION_PHASE and request[
        "stage"
    ] == evaluation_stage(PRO)
    assert ledger["family_bindings"]["evaluation-item:aqa-one"]["paper_id"] == "aqa-one"
    assert ledger["live_test_papers"] == {}
    # The same trial is never replayed; a second repeat is a new request.
    with pytest.raises(ValueError, match="already exists"):
        execute(values, trial_id="t1")
    second = execute(values, trial_id="t2", repeat=2)
    assert (
        second["state"] == "completed"
        and second["request_key"] != receipt["request_key"]
    )
    assert values["broker"].status()["evaluation"]["submissions"] == 2


def test_evaluation_ceiling_reserve_and_repeat_limit_are_enforced(
    tmp_path: Path,
) -> None:
    values = evaluation_fixture(
        tmp_path,
        transport=LetterTransport("A"),
        policy_changes={
            "evaluation_ceiling_usd": "0.05",
            "maximum_calls_per_item_condition_model_arm": 1,
        },
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
    assert (
        execute(generous, trial_id="t3", condition="gold_absent")["state"]
        == "completed"
    )
    # The per-request cap of the evaluation policy still applies.
    capped = evaluation_fixture(
        tmp_path / "capped",
        transport=LetterTransport("A"),
        policy_changes={"maximum_request_reserved_cost_usd": "0.01"},
    )
    bind(capped)
    assert execute(capped, trial_id="t1")["reason"] == PER_REQUEST_CAP_REASON


def test_evaluation_gate_binding_stops_a_changed_set_prompt_model_or_run(
    tmp_path: Path,
) -> None:
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
        bind(
            values,
            decoding={**values["decoding"], "temperature_by_model": {PRO: "1.0"}},
        )
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


def test_evaluation_payload_rules_pin_temperature_preset_and_enum(
    tmp_path: Path,
) -> None:
    values = evaluation_fixture(tmp_path, transport=LetterTransport("A"))
    bind(values)
    broker = values["broker"]
    identity = {
        "paper_id": "aqa-one",
        "family_id": "evaluation-item:aqa-one",
        "source_version_id": "s",
    }

    def attempt(
        request_payload: dict,
        *,
        stage: str = evaluation_stage(PRO),
        phase: str = EVALUATION_PHASE,
        trial=None,
        key_trial_id: str | None = None,
    ):
        key = broker_request_key(
            model=PRO,
            run_id=values["run_id"],
            phase=phase,
            stage=stage,
            payload=request_payload,
            trial_id=key_trial_id or (trial or {}).get("trial_id"),
            **identity,
        )
        return broker.execute(
            phase=phase,
            run_id=values["run_id"],
            stage=stage,
            request_key=key,
            payload=request_payload,
            trial=trial,
            **identity,
        )

    trial = {
        "trial_id": "t",
        "eval_set_id": "x",
        "item_id": "aqa-one",
        "condition": "gold_present",
        "arm": "medium",
        "repeat": 1,
    }
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


def test_construction_broker_without_evaluation_files_rejects_evaluation_work(
    tmp_path: Path,
) -> None:
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
        evaluation_fixture(
            tmp_path / "a", policy_changes={"evaluation_ceiling_usd": "600"}
        )
    with pytest.raises(ValueError, match="permits retries"):
        evaluation_fixture(
            tmp_path / "b", policy_changes={"automatic_transport_retries": 1}
        )
    with pytest.raises(ValueError, match="re_ask_on_invalid_response"):
        evaluation_fixture(
            tmp_path / "c", policy_changes={"re_ask_on_invalid_response": True}
        )
    with pytest.raises(ValueError, match="exceeds the construction cap"):
        evaluation_fixture(
            tmp_path / "d", policy_changes={"maximum_request_reserved_cost_usd": "1.00"}
        )
    with pytest.raises(ValueError, match="not an official preset"):
        evaluation_fixture(tmp_path / "e", arms=["ultra"])
    with pytest.raises(ValueError, match="no price entry"):
        evaluation_fixture(tmp_path / "f", models=["gemini-9-pro"])


def test_ambiguous_evaluation_charge_halts_only_the_evaluation_phase(
    tmp_path: Path,
) -> None:
    """An evaluation ambiguity must never stop the construction pipeline.

    Firstmate instruction, 2026-09-16: a `benchmark_evaluation` request that
    ends ambiguous pauses the evaluation phase only, with its own halt flag and
    reason in the evaluation block, while construction continues under its own
    ceiling.
    """
    values = evaluation_fixture(tmp_path, transport=Transport(failure="generate"))
    bind(values)
    receipt = execute(values, trial_id="t1")
    assert receipt["state"] == "ambiguous_charge"
    status = values["broker"].status()
    # The ledger itself is not halted; the evaluation phase is.
    assert status["halted"] is False and status["halt_reason"] is None
    assert status["evaluation"]["halted"] is True
    assert status["evaluation"]["phase_halted"] is True
    assert status["evaluation"]["halt_reason"] == "ambiguous_generation_charge"
    assert Decimal(status["evaluation"]["ambiguous_usd"]) > 0
    assert Decimal(status["usage"]["dataset_construction_usd"]) == 0
    ledger = json.loads(values["ledger"].read_text())
    assert ledger["halted"] is False
    assert ledger["evaluation_halted"] is True
    # A second evaluation call is refused.
    with pytest.raises(ValueError, match="halted"):
        execute(values, trial_id="t2", repeat=2)
    # A construction call on the same ledger still goes out.
    write_json(
        values["construction_gate"],
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": True,
            "allowed_phase": "live_test",
            "integrated_code_commit": "fixture-commit",
            "independent_review_verdict": "pass",
            "review_record": "fixture-review",
        },
    )
    # A healthy transport for the construction call; the evaluation transport
    # failed on purpose above.
    values["broker"].transport = Transport()
    construction_payload = payload()
    construction = values["broker"].execute(
        phase="live_test",
        run_id="construction-run",
        stage="question_generation",
        paper_id="paper",
        family_id="family",
        source_version_id="source",
        request_key=broker_request_key(
            model="gemini-3.8-flash",
            run_id="construction-run",
            phase="live_test",
            stage="question_generation",
            paper_id="paper",
            family_id="family",
            source_version_id="source",
            payload=construction_payload,
        ),
        payload=construction_payload,
    )
    assert construction["state"] == "completed"
    after = values["broker"].status()
    assert Decimal(after["usage"]["dataset_construction_usd"]) > 0
    assert after["evaluation"]["halted"] is True
    body = trial_payload(values)
    transport = ScriptedTransport({scripted_key(PRO, body): {"text": "C"}})
    assert transport.post(PRO, "generateContent", body)["candidates"][0]["content"][
        "parts"
    ] == [{"text": "C"}]
    with pytest.raises(ValueError, match="no answer for this payload"):
        transport.post("gemini-3.8-flash", "generateContent", body)


RECORDED_37_FLASH_USAGE = {
    "promptTokenCount": 174,
    "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 174}],
    "serviceTier": "standard",
    "thoughtsTokenCount": 1421,
    "totalTokenCount": 1595,
}
"""The usage record gemini-3.7-flash returned on 2026-09-16 at 10:01:08Z.

`candidatesTokenCount` is absent although the answer text was the letter `C`.
The total equals the prompt count plus the thinking count, so the omitted
value is zero. Receipt:
`.../streaming-dataset-r1/model-receipts/684400ee....received.json`.
"""


def test_omitted_answer_token_count_normalizes_from_the_recorded_response() -> None:
    from arctic_qa.model_broker import _normalized_usage, _omitted_zero_usage_field

    usage = _normalized_usage({"usageMetadata": RECORDED_37_FLASH_USAGE})
    assert usage["candidatesTokenCount"] == 0
    assert usage["thoughtsTokenCount"] == 1421
    assert usage["promptTokenCount"] == 174 and usage["totalTokenCount"] == 1595
    assert _omitted_zero_usage_field(RECORDED_37_FLASH_USAGE) == "candidatesTokenCount"
    # The older shape still normalizes.
    omitted_thoughts = {
        "promptTokenCount": 100,
        "candidatesTokenCount": 10,
        "totalTokenCount": 110,
    }
    assert _omitted_zero_usage_field(omitted_thoughts) == "thoughtsTokenCount"
    assert (
        _normalized_usage({"usageMetadata": omitted_thoughts})["thoughtsTokenCount"]
        == 0
    )
    # A total that does not prove the omitted zero stays an error.
    for usage_record, message in (
        (
            {
                "promptTokenCount": 174,
                "thoughtsTokenCount": 1421,
                "totalTokenCount": 1596,
            },
            "zero answer tokens",
        ),
        (
            {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "totalTokenCount": 120,
            },
            "zero thinking tokens",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            _normalized_usage({"usageMetadata": usage_record})
    # Two absent counts are never proved by one total, whatever the total
    # says. The message names the thinking count, because the reviewed
    # chapter 2 continuation of the `answer_agreement` MAX_TOKENS incident
    # binds that exact string and its receipt carries neither count
    # (tests/test_ambiguous_continuation.py).
    for total in (174, 999):
        with pytest.raises(ValueError, match="cannot prove zero thinking tokens"):
            _normalized_usage(
                {"usageMetadata": {"promptTokenCount": 174, "totalTokenCount": total}}
            )


class OmittedCandidatesTransport(Transport):
    """Answer one letter with the recorded gemini-3.7-flash usage shape."""

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 174}
        return {
            "responseId": "omitted-candidates",
            "modelVersion": model,
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": "C"}]}}
            ],
            "usageMetadata": dict(RECORDED_37_FLASH_USAGE),
        }


def test_an_omitted_answer_count_now_settles_without_an_ambiguous_charge(
    tmp_path: Path,
) -> None:
    """The live stop of 2026-09-16 must not repeat."""
    values = evaluation_fixture(tmp_path, transport=OmittedCandidatesTransport())
    bind(values)
    receipt = execute(values, trial_id="t1")
    assert receipt["state"] == "completed"
    assert receipt["usage"]["candidatesTokenCount"] == 0
    assert receipt["usage"]["thoughtsTokenCount"] == 1421
    # gemini-3.1-pro-preview: 174 x 2e-6 + 1421 x 12e-6 = 0.017400.
    assert Decimal(receipt["actual_cost_usd"]) == Decimal("0.017400")
    status = values["broker"].status()
    assert status["halted"] is False
    assert status["evaluation"]["halted"] is False
    assert status["evaluation"]["spent_usd"] == "0.017400"


def test_reconciliation_settles_a_recorded_omitted_candidates_charge(
    tmp_path: Path,
) -> None:
    """The settlement path for the request that halted the shared ledger.

    The broker had no reviewed path for an omitted answer-token count, so the
    reconciliation now accepts both omitted-zero shapes and reads the
    evaluation gate for an evaluation request.
    """
    values = evaluation_fixture(tmp_path, transport=OmittedCandidatesTransport())
    bind(values)
    broker = values["broker"]
    # Reproduce the old behaviour: the response is saved, the usage rule of the
    # day refused it, and the request is an ambiguous charge.
    import arctic_qa.model_broker as module

    original = module._normalized_usage

    def refusing(response):
        usage = (response or {}).get("usageMetadata")
        if isinstance(usage, dict) and "candidatesTokenCount" not in usage:
            raise ValueError("provider usage is inconsistent")
        return original(response)

    module._normalized_usage = refusing
    try:
        receipt = execute(values, trial_id="t1")
    finally:
        module._normalized_usage = original
    assert receipt["state"] == "ambiguous_charge"
    assert receipt["error"] == "ValueError: provider usage is inconsistent"
    request_key = receipt["request_key"]
    status = broker.status()
    assert status["halted"] is False and status["evaluation"]["halted"] is True
    ambiguous_before = Decimal(status["evaluation"]["ambiguous_usd"])
    assert ambiguous_before > 0

    result = broker.reconcile_omitted_thought_usage(request_key)
    assert result["applied"] is True
    assert Decimal(result["actual_cost_usd"]) == Decimal("0.017400")
    event = json.loads(Path(result["reconciliation_receipt"]).read_text())
    assert event["omitted_zero_usage_field"] == "candidatesTokenCount"
    assert event["phase"] == EVALUATION_PHASE
    assert event["normalized_usage"]["candidatesTokenCount"] == 0
    assert event["gate_sha256"] == sha256_file(values["gate"])
    after = broker.status()
    assert after["halted"] is False
    assert after["evaluation"]["halted"] is False
    assert after["evaluation"]["ambiguous_usd"] == "0"
    assert after["evaluation"]["spent_usd"] == "0.017400"
    ledger = json.loads(values["ledger"].read_text())
    assert ledger["requests"][request_key]["state"] == "completed"
    assert ledger["evaluation_halted"] is False
    # The original receipts stay untouched and a repeat changes nothing.
    again = broker.reconcile_omitted_thought_usage(request_key)
    assert again["applied"] is False
    assert again["actual_cost_usd"] == result["actual_cost_usd"]
    # A repeat also leaves no halt standing that has no blocking cause.
    stale = json.loads(values["ledger"].read_text())
    stale["halted"] = True
    stale["halt_reason"] = "ambiguous_generation_charge"
    stale["evaluation_halted"] = True
    stale["evaluation_halt_reason"] = "ambiguous_generation_charge"
    ledger_store.write_snapshot(
        values["ledger"], stale, ledger_store.snapshot_applied_seq(values["ledger"])
    )
    repeat = broker.reconcile_omitted_thought_usage(request_key)
    assert repeat["applied"] is False
    lifted = json.loads(values["ledger"].read_text())
    assert lifted["halted"] is False and lifted["evaluation_halted"] is False
    final = json.loads((tmp_path / "receipts" / f"{request_key}.json").read_text())
    assert final["state"] == "ambiguous_charge"
    # The effective receipt reads as a completed call.
    effective = broker.effective_receipt(request_key)
    assert effective["state"] == "completed"
    assert effective["usage"]["candidatesTokenCount"] == 0


def _transition_file(
    tmp_path: Path,
    values: dict,
    *,
    source: Path,
    target_policy: Path,
    gate: Path,
    review: Path,
    predecessor: str | None = None,
    **changes: object,
) -> Path:
    authorization = {
        "schema": EVALUATION_POLICY_TRANSITION_SCHEMA,
        "ledger_file": str(values["ledger"]),
        "from_evaluation_transition_sha256": predecessor,
        "from_policy_file": str(source),
        "from_policy_sha256": sha256_file(source),
        "to_policy_sha256": sha256_file(target_policy),
        "changed_policy_fields": {
            "evaluation_ceiling_usd": {"from": "5.00", "to": "200.00"}
        },
        "expected_ledger_sha256": sha256_file(values["ledger"]),
        "evaluation_gate_sha256": sha256_file(gate),
        "integrated_code_commit": "fixture-commit",
        "review_record": str(review),
        "review_record_sha256": sha256_file(review),
        "reason": (
            "Captain allocation 2026-09-16: USD 200 for benchmarking the Gemini "
            "models on the abstention benchmark."
        ),
        "authorized_at_utc": "2026-09-16T11:00:00Z",
    }
    authorization.update(changes)
    path = tmp_path / f"evaluation-transition-{len(list(tmp_path.glob('*.json')))}.json"
    write_json(path, authorization)
    return path


def _raised_ceiling(
    tmp_path: Path, values: dict, **changes: object
) -> tuple[Path, Path, Path, Path]:
    """Write the USD 200 policy, its gate and the transition file."""
    source = values["policy"]
    raised = tmp_path / "evaluation-policy-v3.json"
    policy = json.loads(source.read_text())
    policy["policy_id"] = "fixture-v3"
    policy["evaluation_ceiling_usd"] = "200.00"
    write_json(raised, policy)
    gate = tmp_path / "evaluation-gate-v3.json"
    review = tmp_path / "review.md"
    write_evaluation_gate(
        gate,
        set_dir=values["set_dir"],
        run_id=values["run_id"],
        models=values["models"],
        arms=values["arms"],
        repeats_maximum=values["repeats"],
        decoding=values["decoding"],
        evaluation_policy_file=raised,
        evaluation_price_config_file=values["prices"],
        integrated_code_commit="fixture-commit",
        review_record=review,
    )
    transition = _transition_file(
        tmp_path,
        values,
        source=source,
        target_policy=raised,
        gate=gate,
        review=review,
        **changes,
    )
    return raised, gate, review, transition


def _raised_broker(
    tmp_path: Path, values: dict, *, raised: Path, gate: Path, transition: Path | None
) -> SharedGeminiBroker:
    return SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["construction_gate"],
        ledger_file=values["ledger"],
        receipts_dir=values["broker"].receipts_dir,
        credential_file=values["broker"].credential_file,
        prior_construction_spend_usd=Decimal("0"),
        transport=LetterTransport(),
        evaluation_policy_file=raised,
        evaluation_price_config_file=values["prices"],
        evaluation_gate_file=gate,
        evaluation_policy_transition_file=transition,
    )


def test_the_shipped_policy_v3_raises_the_ceiling_to_the_captains_allocation() -> None:
    """The USD 200 policy differs from v2 only in the ceiling and its names.

    Captain allocation 2026-09-16: USD 200 for benchmarking the Gemini models.
    The registered change set of the broker names exactly that step, so no
    other ceiling can be applied without a code change and a review.
    """
    two = json.loads(
        (ROOT / "config" / "benchmark-evaluation-policy-v2.json").read_text()
    )
    three = json.loads(
        (ROOT / "config" / "benchmark-evaluation-policy-v3.json").read_text()
    )
    assert two["evaluation_ceiling_usd"] == "5.00"
    assert three["evaluation_ceiling_usd"] == "200.00"
    assert sorted(k for k in set(two) | set(three) if two.get(k) != three.get(k)) == [
        "evaluation_ceiling_usd",
        "policy_id",
        "purpose",
    ]
    assert EVALUATION_CEILING_CHANGES == (
        {"evaluation_ceiling_usd": {"from": "5.00", "to": "200.00"}},
    )
    # The reserve of the construction policy still covers the new ceiling.
    construction = json.loads(
        (ROOT / "config" / "streaming-dataset-budget-policy-v1.json").read_text()
    )
    assert Decimal(construction["reserved_for_benchmark_evaluation_usd"]) >= Decimal(
        three["evaluation_ceiling_usd"]
    )


def test_a_larger_evaluation_ceiling_needs_a_reviewed_chained_transition(
    tmp_path: Path,
) -> None:
    """The evaluation ceiling moves only through a reviewed chained transition.

    The baseline is USD 5.00. A policy with a larger ceiling is refused until
    a transition file, whose change set the broker registers, is applied. The
    event is immutable, it names its predecessor, and every later start reads
    the event instead of the file.
    """
    values = evaluation_fixture(tmp_path, transport=LetterTransport())
    bind(values)
    assert values["broker"].authorized_evaluation_ceiling_usd() == Decimal("5.00")
    raised, gate, _, transition = _raised_ceiling(tmp_path, values)
    # Without the transition the larger ceiling is refused.
    with pytest.raises(ValueError, match="requires a reviewed transition"):
        _raised_broker(tmp_path, values, raised=raised, gate=gate, transition=None)
    broker = _raised_broker(
        tmp_path, values, raised=raised, gate=gate, transition=transition
    )
    assert broker.authorized_evaluation_ceiling_usd() == Decimal("200.00")
    status = broker.status()
    assert Decimal(status["evaluation"]["ceiling_usd"]) == Decimal("200.00")
    events = sorted(values["broker"].receipts_dir.glob("evaluation-policy-*.json"))
    assert len(events) == 1
    event = json.loads(events[0].read_text())
    assert event["authorization"]["from_evaluation_transition_sha256"] is None
    assert event["authorization"]["changed_policy_fields"] == {
        "evaluation_ceiling_usd": {"from": "5.00", "to": "200.00"}
    }
    assert events[0].name == (
        f"evaluation-policy-transition-{event['transition_authorization_sha256']}.json"
    )
    # A later start needs no transition file: the event authorizes the ceiling.
    again = _raised_broker(tmp_path, values, raised=raised, gate=gate, transition=None)
    assert again.authorized_evaluation_ceiling_usd() == Decimal("200.00")
    assert (
        len(list(values["broker"].receipts_dir.glob("evaluation-policy-*.json"))) == 1
    )
    # A paid call under the raised ceiling binds the transition event.
    values["broker"] = again
    values["gate"] = gate
    bind(values)
    receipt = execute(values, trial_id="raised-1")
    assert receipt["state"] == "completed"
    request = json.loads(values["ledger"].read_text())["requests"][
        receipt["request_key"]
    ]
    assert (
        request["evaluation_policy_transition_sha256"]
        == event["transition_authorization_sha256"]
    )
    # A second event on the same predecessor forks the chain and is refused.
    fork = _transition_file(
        tmp_path,
        values,
        source=values["policy"],
        target_policy=raised,
        gate=gate,
        review=tmp_path / "review.md",
        reason="a forked authorization",
    )
    forked = json.loads(fork.read_text())
    digest = sha256_bytes(canonical_json(forked).encode())
    write_json(
        values["broker"].receipts_dir / f"evaluation-policy-transition-{digest}.json",
        {
            "schema": "benchmark-evaluation-policy-transition-event-v1",
            "authorization": forked,
            "transition_authorization_sha256": digest,
            "applied_at_utc": "2026-09-16T11:30:00Z",
        },
    )
    with pytest.raises(ValueError, match="share one predecessor"):
        _raised_broker(tmp_path, values, raised=raised, gate=gate, transition=None)


def test_an_unauthorized_evaluation_ceiling_transition_is_refused(
    tmp_path: Path,
) -> None:
    """Every binding of the transition is validated before it is applied."""
    values = evaluation_fixture(tmp_path, transport=LetterTransport())
    bind(values)
    raised, gate, review, _ = _raised_ceiling(tmp_path, values)

    def refuse(match: str, **changes: object) -> None:
        path = _transition_file(
            tmp_path,
            values,
            source=values["policy"],
            target_policy=raised,
            gate=gate,
            review=review,
            **changes,
        )
        with pytest.raises(ValueError, match=match):
            _raised_broker(tmp_path, values, raised=raised, gate=gate, transition=path)

    refuse(
        "change set is not authorized",
        changed_policy_fields={
            "evaluation_ceiling_usd": {"from": "5.00", "to": "500.00"}
        },
    )
    refuse("names another ledger", ledger_file=str(tmp_path / "other-ledger.json"))
    refuse("ledger snapshot changed", expected_ledger_sha256="0" * 64)
    refuse("binds another gate", evaluation_gate_sha256="0" * 64)
    refuse("source changed", from_policy_sha256="0" * 64)
    refuse("target changed", to_policy_sha256="0" * 64)
    refuse("source is absent", from_policy_file=str(tmp_path / "absent.json"))
    refuse("names another predecessor", from_evaluation_transition_sha256="0" * 64)
    refuse("lacks its code commit", integrated_code_commit="  ")
    refuse("lacks its reason", reason="")
    refuse("authorization time is invalid", authorized_at_utc="not-a-time")
    refuse("review record is absent", review_record=str(tmp_path / "absent.md"))
    refuse("review record changed", review_record_sha256="0" * 64)
    refuse("transition file is invalid", schema="another-schema")
    # A policy that also changes a control field is refused.
    tampered = tmp_path / "tampered-policy.json"
    policy = json.loads(raised.read_text())
    policy["maximum_concurrent_requests"] = 8
    write_json(tampered, policy)
    tampered_gate = tmp_path / "tampered-gate.json"
    write_evaluation_gate(
        tampered_gate,
        set_dir=values["set_dir"],
        run_id=values["run_id"],
        models=values["models"],
        arms=values["arms"],
        repeats_maximum=values["repeats"],
        decoding=values["decoding"],
        evaluation_policy_file=tampered,
        evaluation_price_config_file=values["prices"],
        integrated_code_commit="fixture-commit",
        review_record=review,
    )
    path = _transition_file(
        tmp_path,
        values,
        source=values["policy"],
        target_policy=tampered,
        gate=tampered_gate,
        review=review,
    )
    with pytest.raises(ValueError, match="changes another field"):
        _raised_broker(
            tmp_path, values, raised=tampered, gate=tampered_gate, transition=path
        )
    # No event was written by any refused attempt.
    assert list(values["broker"].receipts_dir.glob("evaluation-policy-*.json")) == []


def test_cli_apply_evaluation_ceiling_writes_the_event_and_calls_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first start under a larger ceiling is its own action.

    The streaming evaluator derives one gate per item, so it cannot be that
    first start: the transition binds one gate hash. This action applies the
    transition with a gate of its own and makes no paid call. Every later
    start reads the immutable event.
    """
    values = evaluation_fixture(tmp_path, transport=LetterTransport())
    bind(values)
    raised, gate, _, transition = _raised_ceiling(tmp_path, values)
    argv = [
        "--json",
        "--test-mode",
        "--data-root",
        str(tmp_path / "data"),
        "abstention-eval",
        "--action",
        "apply-evaluation-ceiling",
        "--streaming-budget-policy-file",
        str(ROOT / "config" / "streaming-dataset-budget-policy-v1.json"),
        "--price-config-file",
        str(ROOT / "config" / "gemini-eligibility-v1.json"),
        "--execution-gate-file",
        str(values["construction_gate"]),
        "--evaluation-policy-file",
        str(raised),
        "--evaluation-price-config-file",
        str(values["prices"]),
        "--evaluation-gate-file",
        str(gate),
        "--evaluation-policy-transition-file",
        str(transition),
        "--shared-ledger-file",
        str(values["ledger"]),
        "--model-receipts-dir",
        str(values["broker"].receipts_dir),
        "--credential-file",
        str(values["broker"].credential_file),
        "--prior-construction-spend-usd",
        "0",
    ]
    assert cli_main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["authorized_ceiling_usd"] == "200.00"
    assert Decimal(result["evaluation"]["ceiling_usd"]) == Decimal("200.00")
    assert result["ledger"]["integrity_valid"] is True
    assert result["ledger"]["halted"] is False
    assert Decimal(result["evaluation"]["used_usd"]) == 0
    events = list(values["broker"].receipts_dir.glob("evaluation-policy-*.json"))
    assert len(events) == 1
    assert result["transition_event_sha256"] in events[0].name
    # A second run of the same action changes nothing.
    assert cli_main(argv) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["transition_event_sha256"] == result["transition_event_sha256"]
    assert (
        len(list(values["broker"].receipts_dir.glob("evaluation-policy-*.json"))) == 1
    )
    # Once the event exists the action reads the ceiling back with no
    # transition file, which is how an operator proves it is in force.
    flag = "--evaluation-policy-transition-file"
    read_back = [
        item
        for index, item in enumerate(argv)
        if item != flag and (index == 0 or argv[index - 1] != flag)
    ]
    assert cli_main(read_back) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["authorized_ceiling_usd"] == "200.00"
    assert shown["transition_event_sha256"] == result["transition_event_sha256"]


class Http503ThenLetter(LetterTransport):
    """One HTTP 503 on the first generation, then the ordinary answer.

    This is the shape of the live 503 of 2026-09-16 at 17:53 UTC: the provider
    reported a server error, no usage came back, and the charge is unknown.
    """

    def __init__(self, letter: str = "B") -> None:
        super().__init__(letter=letter)
        self.generation_calls = 0

    def post(self, model: str, method: str, body: dict) -> dict:
        if method != "countTokens":
            self.generation_calls += 1
            if self.generation_calls == 1:
                self.methods.append(method)
                raise urllib.error.HTTPError(
                    "https://fake.invalid", 503, "upstream failure", {}, None
                )
        return super().post(model, method, body)


def test_an_evaluation_ambiguity_is_released_under_its_own_evaluation_gate(
    tmp_path: Path,
) -> None:
    """The release reads the evaluation gate and lifts the evaluation halt.

    Two checks of `authorize_ambiguous_continuation` knew only the construction
    phase, so the 503 of 2026-09-16 at 17:53 UTC could not be released at all:
    the phase-scoped halt never sets `halted`, and an evaluation request binds
    a `benchmark-evaluation-execution-gate-v1` gate whose authorized run is in
    `authorized_run_id`, not `authorized_new_run_id`.
    """
    values = evaluation_fixture(tmp_path, transport=Http503ThenLetter())
    bind(values)
    receipt = execute(values, trial_id="t1")
    assert receipt["state"] == "ambiguous_charge"
    assert receipt["http_status"] == 503
    before = json.loads(values["ledger"].read_text())
    assert before["halted"] is False
    assert before["evaluation_halted"] is True
    assert before["evaluation_halt_reason"] == "ambiguous_generation_charge"
    review = tmp_path / "continuation-review.md"
    review.write_text("pass\n", encoding="utf-8")
    evidence = tmp_path / "continuation-evidence.json"
    write_json(
        evidence,
        {
            "schema": "shared-paid-call-ambiguous-continuation-evidence-v1",
            "request_key": receipt["request_key"],
            "error_class": "known_http_response_unknown_charge",
            "http_status": 503,
            "live_call_made": True,
            "received_receipt_absent": True,
            "actual_cost_known": False,
            "replay_prohibited": True,
            "affected_family_id": receipt["family_id"],
            "authorized_run_id": receipt["run_id"],
        },
    )
    result = values["broker"].authorize_ambiguous_continuation(
        request_key=receipt["request_key"],
        expected_ledger_sha256=sha256_file(values["ledger"]),
        review_file=review,
        evidence_file=evidence,
        authorized_run_id=values["run_id"],
        operator_id="test-operator",
    )
    assert result["applied"] is True
    assert result["reserved_usd_retained"] == receipt["reserved_usd"]
    event = json.loads(Path(result["continuation_receipt"]).read_text())
    # The event binds the evaluation gate, not the construction gate.
    assert event["gate_sha256"] == sha256_file(values["gate"])
    assert event["gate_sha256"] != sha256_file(values["construction_gate"])
    assert event["skip_reason_code"] == "operational_ambiguous_charge_http_500"
    after = json.loads(values["ledger"].read_text())
    # Only the evaluation halt lifts, and the reservation stays reserved.
    assert after["evaluation_halted"] is False
    assert after["evaluation_halt_reason"] is None
    assert after["halted"] is False and after["halt_reason"] is None
    assert after["ambiguous_reserved_usd"] == receipt["reserved_usd"]
    status = values["broker"].status()
    assert status["evaluation"]["phase_halted"] is False
    assert status["evaluation"]["ambiguous_usd"] == receipt["reserved_usd"]
    # Unrelated trials run again; the affected trial is never replayed.
    assert execute(values, trial_id="t2", repeat=2)["state"] == "completed"
    with pytest.raises(ValueError, match="request key already exists"):
        execute(values, trial_id="t1")


def test_an_evaluation_release_needs_the_evaluation_files(tmp_path: Path) -> None:
    """A broker with no evaluation gate cannot release an evaluation ambiguity."""
    values = evaluation_fixture(tmp_path, transport=Http503ThenLetter())
    bind(values)
    receipt = execute(values, trial_id="t1")
    assert receipt["state"] == "ambiguous_charge"
    construction_only = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["construction_gate"],
        ledger_file=values["ledger"],
        receipts_dir=values["ledger"].parent / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
    )
    review = tmp_path / "continuation-review.md"
    review.write_text("pass\n", encoding="utf-8")
    evidence = tmp_path / "continuation-evidence.json"
    write_json(evidence, {"schema": "unused"})
    with pytest.raises(ValueError, match="no benchmark evaluation policy"):
        construction_only.authorize_ambiguous_continuation(
            request_key=receipt["request_key"],
            expected_ledger_sha256=sha256_file(values["ledger"]),
            review_file=review,
            evidence_file=evidence,
            authorized_run_id=values["run_id"],
            operator_id="test-operator",
        )
    assert json.loads(values["ledger"].read_text())["evaluation_halted"] is True


def test_an_unknown_evaluation_charge_continues_the_arm_inside_its_own_bound(
    tmp_path: Path, monkeypatch
) -> None:
    """The evaluation phase gets its own hourly bound of automatic releases.

    Firstmate order 2026-09-17 10:26 UTC. A construction charge never spends
    the evaluation phase's bound, and the reservation is retained in full, so
    the evaluation ceiling still counts every unknown charge.
    """
    monkeypatch.setattr(model_broker, "AUTOMATIC_CONTINUATION_LIMIT_PER_HOUR", 5)
    values = evaluation_fixture(tmp_path, transport=Http503ThenLetter())
    bind(values)
    receipt = execute(values, trial_id="t1")
    assert receipt["state"] == "ambiguous_charge"
    ledger = json.loads(values["ledger"].read_text())
    # Neither halt stands: the arm keeps scoring.
    assert ledger.get("evaluation_halted", False) is False
    assert ledger.get("evaluation_halt_reason") is None
    assert ledger["halted"] is False
    # The reservation is retained and nothing is spent on it.
    assert ledger["ambiguous_reserved_usd"] == receipt["reserved_usd"]
    event = json.loads(
        (
            values["ledger"].parent
            / "receipts"
            / f"ambiguous-continuation-{receipt['request_key']}.json"
        ).read_text()
    )
    assert event["operator_id"] == "automatic-ambiguous-continuation"
    assert event["authorized_run_id"] == values["run_id"]
    # Unrelated trials run, and the affected trial is never replayed.
    assert execute(values, trial_id="t2", repeat=2)["state"] == "completed"
    with pytest.raises(ValueError, match="request key already exists"):
        execute(values, trial_id="t1")
    assert values["broker"].status()["integrity_valid"] is True
