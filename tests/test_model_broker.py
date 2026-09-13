from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.cli import main as cli_main
from arctic_qa.model_broker import SharedGeminiBroker, broker_request_key
from arctic_qa.providers import GeminiProvider, ProviderError, make_provider
from arctic_qa.util import canonical_json, sha256_bytes, sha256_file


ROOT = Path(__file__).parents[1]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def payload() -> dict:
    return {
        "systemInstruction": {"parts": [{"text": "Return JSON."}]},
        "contents": [{"role": "user", "parts": [{"text": "Paper text."}]}],
        "generationConfig": {
            "candidateCount": 1,
            "responseMimeType": "application/json",
            "responseJsonSchema": {"type": "object"},
            "maxOutputTokens": 1000,
            "thinkingConfig": {"thinkingLevel": "medium"},
        },
        "store": False,
    }


def test_legacy_gemini_provider_cannot_bypass_shared_broker() -> None:
    with pytest.raises(ValueError, match="shared streaming broker"):
        make_provider("gemini", "gemini-3.8-flash", None)
    with pytest.raises(ProviderError, match="shared streaming broker"):
        GeminiProvider("gemini-3.8-flash").invoke("verifier", "system", "prompt", {}, 1)


class Transport:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.methods: list[str] = []

    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            if self.failure == "count":
                raise TimeoutError("count unavailable")
            return {"totalTokens": 100}
        if self.failure == "generate":
            raise TimeoutError("outcome unknown")
        missing_thoughts = self.failure in {
            "missing_thoughts",
            "missing_thoughts_inconsistent",
        }
        return {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                **({} if missing_thoughts else {"thoughtsTokenCount": 5}),
                "totalTokenCount": (
                    111
                    if self.failure == "missing_thoughts_inconsistent"
                    else 110
                    if missing_thoughts
                    else 115
                ),
            },
        }


class HighCostTransport(Transport):
    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 290_000}
        return {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "usageMetadata": {
                "promptTokenCount": 290_000,
                "candidatesTokenCount": 8_192,
                "thoughtsTokenCount": 0,
                "totalTokenCount": 298_192,
            },
        }


class CapturedZeroThoughtUsageTransport(Transport):
    def post(self, model: str, method: str, body: dict) -> dict:
        self.methods.append(method)
        if method == "countTokens":
            return {"totalTokens": 14059}
        return {
            "candidates": [
                {
                    "content": {"parts": [{"text": "{}"}]},
                    "finishReason": "STOP",
                }
            ],
            "modelVersion": "gemini-3.8-flash",
            "responseId": "captured-response-id",
            "usageMetadata": {
                "promptTokenCount": 14059,
                "candidatesTokenCount": 1355,
                "totalTokenCount": 15414,
            },
        }


def fixture(tmp_path: Path, *, enabled: bool = True, transport=None) -> dict:
    gate = tmp_path / "gate.json"
    write_json(
        gate,
        {
            "schema": "streaming-live-execution-gate-v1",
            "live_generation_enabled": enabled,
            "allowed_phase": "live_test" if enabled else None,
            "integrated_code_commit": "fixture-commit" if enabled else None,
            "independent_review_verdict": "pass" if enabled else "pending",
            "review_record": "fixture-review" if enabled else None,
        },
    )
    credential = tmp_path / "private" / "gemini.key"
    credential.parent.mkdir(mode=0o700, exist_ok=True)
    credential.write_text("unused-test-key", encoding="utf-8")
    credential.chmod(0o600)
    broker = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=gate,
        ledger_file=tmp_path / "shared-ledger.json",
        receipts_dir=tmp_path / "receipts",
        credential_file=credential,
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
    )
    return {"broker": broker, "gate": gate, "ledger": tmp_path / "shared-ledger.json"}


def reviewed_transition(
    tmp_path: Path,
    values: dict,
    active_config: Path,
    **changes: object,
) -> Path:
    identity = tmp_path / ".shared-ledger.json.identity.json"
    review = tmp_path / "review.md"
    review.write_text("Exact integrated revision passed.\n", encoding="utf-8")
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate["review_record"] = str(review)
    gate["review_record_sha256"] = sha256_file(review)
    write_json(values["gate"], gate)
    authorization = {
        "schema": "shared-paid-call-config-transition-v1",
        "ledger_file": str(values["ledger"].resolve()),
        "from_price_config_sha256": json.loads(identity.read_text(encoding="utf-8"))[
            "price_config_sha256"
        ],
        "to_price_config_sha256": sha256_file(active_config),
        "expected_ledger_sha256": sha256_file(values["ledger"]),
        "expected_identity_sha256": sha256_file(identity),
        "execution_gate_sha256": sha256_file(values["gate"]),
        "integrated_code_commit": "fixture-commit",
        "review_record": str(review),
        "review_record_sha256": sha256_file(review),
        "reason": "Use the reviewed low-thinking request configuration.",
        "authorized_at_utc": "2026-09-12T09:00:00Z",
        **changes,
    }
    transition = tmp_path / "private" / "config-transition.json"
    write_json(transition, authorization)
    return transition


def policy_with_paper_limit(tmp_path: Path, limit: int = 40) -> Path:
    return policy_with_limits(tmp_path, paper_limit=limit)


def policy_with_limits(
    tmp_path: Path,
    *,
    paper_limit: int | None,
    submission_limit: int | None = 100,
    source_policy: Path | None = None,
) -> Path:
    source_policy = source_policy or (
        ROOT / "config" / "streaming-dataset-budget-policy-v1.json"
    )
    policy = json.loads(source_policy.read_text(encoding="utf-8"))
    policy["live_test_maximum_papers"] = paper_limit
    policy["live_test_maximum_generation_submissions"] = submission_limit
    path = (
        tmp_path
        / f"streaming-dataset-budget-policy-{paper_limit}-{submission_limit}.json"
    )
    write_json(path, policy)
    return path


def reviewed_policy_transition(
    tmp_path: Path,
    values: dict,
    active_policy: Path,
    active_config: Path | None = None,
    from_config_transition_sha256: str | None = None,
    from_policy: Path | None = None,
    **changes: object,
) -> Path:
    identity = tmp_path / ".shared-ledger.json.identity.json"
    review = tmp_path / "policy-review.md"
    review.write_text("Exact policy transition passed.\n", encoding="utf-8")
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate["review_record"] = str(review)
    gate["review_record_sha256"] = sha256_file(review)
    write_json(values["gate"], gate)
    active_config = active_config or ROOT / "config" / "gemini-eligibility-v1.json"
    from_policy = from_policy or (
        ROOT / "config" / "streaming-dataset-budget-policy-v1.json"
    )
    authorization = {
        "schema": "shared-paid-call-config-transition-v2",
        "ledger_file": str(values["ledger"].resolve()),
        "from_price_config_sha256": sha256_file(active_config),
        "to_price_config_sha256": sha256_file(active_config),
        "from_config_transition_sha256": from_config_transition_sha256,
        "from_policy_file": str(from_policy.resolve()),
        "from_policy_sha256": sha256_file(from_policy),
        "to_policy_sha256": sha256_file(active_policy),
        "changed_policy_fields": {"live_test_maximum_papers": {"from": 20, "to": 40}},
        "maximum_authorized_cumulative_tranche_usd": "5.00",
        "expected_ledger_sha256": sha256_file(values["ledger"]),
        "expected_identity_sha256": sha256_file(identity),
        "execution_gate_sha256": sha256_file(values["gate"]),
        "integrated_code_commit": "fixture-commit",
        "review_record": str(review),
        "review_record_sha256": sha256_file(review),
        "reason": "Increase only the reviewed live-test family limit.",
        "authorized_at_utc": "2026-09-13T01:20:00Z",
        **changes,
    }
    transition = tmp_path / "private" / "policy-transition.json"
    write_json(transition, authorization)
    return transition


def prepared_family_and_submission_transition(tmp_path: Path) -> dict:
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    current_policy = policy_with_limits(tmp_path, paper_limit=40)
    current_transition = reviewed_policy_transition(tmp_path, values, current_policy)
    current_broker = SharedGeminiBroker(
        policy_file=current_policy,
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=current_transition,
    )
    predecessor = current_broker.status()["config_transition_sha256"]
    assert predecessor
    proposed_policy = policy_with_limits(
        tmp_path,
        paper_limit=41,
        submission_limit=101,
        source_policy=current_policy,
    )
    proposed_transition = reviewed_policy_transition(
        tmp_path,
        values,
        proposed_policy,
        from_config_transition_sha256=predecessor,
        from_policy=current_policy,
        changed_policy_fields={
            "live_test_maximum_papers": {"from": 40, "to": 41},
            "live_test_maximum_generation_submissions": {"from": 100, "to": 101},
        },
    )
    return {
        **values,
        "current_policy": current_policy,
        "predecessor": predecessor,
        "proposed_policy": proposed_policy,
        "proposed_transition": proposed_transition,
    }


def prepared_budget_bounded_transition(tmp_path: Path) -> dict:
    values = prepared_family_and_submission_transition(tmp_path)
    active_broker = SharedGeminiBroker(
        policy_file=values["proposed_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=values["proposed_transition"],
    )
    predecessor = active_broker.status()["config_transition_sha256"]
    budget_policy = policy_with_limits(
        tmp_path,
        paper_limit=None,
        submission_limit=None,
        source_policy=values["proposed_policy"],
    )
    access = tmp_path / "reviewed-stream-input"
    manifest = access / "run-manifest.json"
    receipt = access / "run-receipt.json"
    write_json(
        manifest,
        {
            "schema": "article-access-manifest-v1",
            "run_id": "reviewed-stream-input",
            "target_total": 1,
            "selection": [{"candidate_key": "p2", "position": 1}],
            "frozen_manifest_sha256": "test-only-frozen-manifest",
            "remaining_order_sha256": "test-only-frozen-order",
        },
    )
    write_json(
        receipt,
        {
            "schema": "article-access-run-receipt-v1",
            "state": "completed",
            "run_id": "reviewed-stream-input",
            "run_manifest_sha256": sha256_file(manifest),
            "frozen_manifest_sha256": "test-only-frozen-manifest",
            "remaining_order_sha256": "test-only-frozen-order",
            "counts": {"target": 1},
        },
    )
    eligibility_policy = tmp_path / "eligibility-policy.json"
    write_json(eligibility_policy, {"protocol_id": "test-only"})
    stream_binding = {
        "access_run_dir": access,
        "phase": "live_test",
        "run_id": "run-1",
        "campaign_id": "campaign-1",
        "eligibility_prompt_file": ROOT / "config" / "gemini-eligibility-prompt-v4.txt",
        "eligibility_schema_file": ROOT
        / "schemas"
        / "gemini-eligibility.v2.schema.json",
        "eligibility_policy_file": eligibility_policy,
    }
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate.update(
        {
            "continuation_input_binding_version": "stream-input-binding-v1",
            "continuation_artifact": str(access.resolve()),
            "continuation_access_run_id": "reviewed-stream-input",
            "continuation_run_manifest_sha256": sha256_file(manifest),
            "continuation_run_receipt_sha256": sha256_file(receipt),
            "continuation_frozen_manifest_sha256": "test-only-frozen-manifest",
            "continuation_order_sha256": "test-only-frozen-order",
            "continuation_family_count": 1,
            "authorized_new_run_id": stream_binding["run_id"],
            "authorized_campaign_id": stream_binding["campaign_id"],
            "eligibility_prompt_sha256": sha256_file(
                stream_binding["eligibility_prompt_file"]
            ),
            "eligibility_schema_sha256": sha256_file(
                stream_binding["eligibility_schema_file"]
            ),
            "eligibility_policy_sha256": sha256_file(eligibility_policy),
        }
    )
    write_json(values["gate"], gate)
    budget_transition = reviewed_policy_transition(
        tmp_path,
        values,
        budget_policy,
        from_config_transition_sha256=predecessor,
        from_policy=values["proposed_policy"],
        changed_policy_fields={
            "live_test_maximum_papers": {"from": 41, "to": None},
            "live_test_maximum_generation_submissions": {
                "from": 101,
                "to": None,
            },
        },
    )
    return {
        **values,
        "budget_policy": budget_policy,
        "budget_predecessor": predecessor,
        "budget_transition": budget_transition,
        "stream_binding": stream_binding,
    }


def execute(
    broker: SharedGeminiBroker,
    *,
    stage: str = "eligibility",
    paper="p1",
    family=None,
    source=None,
    phase="live_test",
    run_id="run-1",
    body=None,
):
    body = body or payload()
    family = family or f"family-{paper}"
    source = source or f"source-{paper}"
    key = broker_request_key(
        model="gemini-3.8-flash",
        run_id=run_id,
        stage=stage,
        paper_id=paper,
        family_id=family,
        source_version_id=source,
        payload=body,
    )
    return broker.execute(
        phase=phase,
        run_id=run_id,
        stage=stage,
        paper_id=paper,
        family_id=family,
        source_version_id=source,
        request_key=key,
        payload=body,
    )


def bind_budget_stream_input(broker: SharedGeminiBroker, values: dict) -> None:
    broker.bind_stream_input(**values["stream_binding"])


def test_disabled_gate_prevents_any_transport_call(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, enabled=False, transport=transport)["broker"]
    with pytest.raises(ValueError, match="disabled"):
        execute(broker)
    assert transport.methods == []


def test_missing_credential_does_not_create_a_request_state(tmp_path: Path):
    values = fixture(tmp_path, transport=None)
    (tmp_path / "private" / "gemini.key").unlink()
    with pytest.raises(ValueError, match="credential"):
        execute(values["broker"])
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["requests"] == {}
    assert ledger["count_requests"] == 0


def test_omitted_zero_thought_usage_settles_from_exact_total(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport("missing_thoughts"))

    receipt = execute(values["broker"])

    assert receipt["state"] == "completed"
    assert receipt["usage"] == {
        "promptTokenCount": 100,
        "candidatesTokenCount": 10,
        "thoughtsTokenCount": 0,
        "totalTokenCount": 110,
    }
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["halted"] is False
    assert ledger["ambiguous_reserved_usd"] == "0"


def test_captured_omitted_zero_thought_usage_settles_without_replay(tmp_path: Path):
    transport = CapturedZeroThoughtUsageTransport()
    values = fixture(tmp_path, transport=transport)
    body = payload()
    body["generationConfig"]["maxOutputTokens"] = 8192

    receipt = execute(values["broker"], body=body)

    assert receipt["state"] == "completed"
    assert receipt["actual_cost_usd"] == "0.015626"
    assert receipt["usage"]["thoughtsTokenCount"] == 0
    assert transport.methods == ["countTokens", "generateContent"]


def test_omitted_thought_usage_without_exact_total_remains_ambiguous(
    tmp_path: Path,
):
    transport = Transport("missing_thoughts_inconsistent")
    values = fixture(tmp_path, transport=transport)

    receipt = execute(values["broker"])

    assert receipt["state"] == "ambiguous_charge"
    assert receipt["error"] == (
        "ValueError: provider usage cannot prove zero thinking tokens"
    )
    assert values["broker"].status()["ambiguous_reserved_usd"] == "0.003825"
    with pytest.raises(ValueError, match="halted"):
        execute(values["broker"], paper="p2")
    assert transport.methods == ["countTokens", "generateContent"]


def test_reconciliation_preserves_ambiguous_receipt_and_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    transport = CapturedZeroThoughtUsageTransport()
    values = fixture(tmp_path, transport=transport)

    def legacy_classification(submitted: dict, response: dict):
        return (
            {
                **submitted,
                "state": "ambiguous_charge",
                "error": "KeyError: 'thoughtsTokenCount'",
                "response": response,
                "live_call_made": True,
                "completed_at_utc": "2026-09-12T17:24:41Z",
            },
            None,
            None,
        )

    monkeypatch.setattr(values["broker"], "_completed_receipt", legacy_classification)
    body = payload()
    body["generationConfig"]["maxOutputTokens"] = 8192
    receipt = execute(values["broker"], body=body)
    request_key = receipt["request_key"]
    final_path = tmp_path / "receipts" / f"{request_key}.json"
    received_path = tmp_path / "receipts" / f"{request_key}.received.json"
    original_final = final_path.read_bytes()
    original_received = received_path.read_bytes()

    review = tmp_path / "usage-review.md"
    review.write_text("The usage repair passed independent review.\n", encoding="utf-8")
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate.update(
        {
            "integrated_code_commit": "usage-repair-commit",
            "review_record": str(review),
            "review_record_sha256": sha256_file(review),
        }
    )
    write_json(values["gate"], gate)

    result = values["broker"].reconcile_omitted_thought_usage(request_key)

    assert result["applied"] is True
    assert result["actual_cost_usd"] == "0.015626"
    assert final_path.read_bytes() == original_final
    assert received_path.read_bytes() == original_received
    reconciliation_path = (
        tmp_path / "receipts" / f"{request_key}.usage-reconciliation.json"
    )
    assert reconciliation_path.is_file()
    reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    assert reconciliation["ambiguous_receipt_sha256"] == sha256_file(final_path)
    assert reconciliation["received_receipt_sha256"] == sha256_file(received_path)
    assert reconciliation["normalized_usage"]["thoughtsTokenCount"] == 0

    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["spent_usd"] == "0.015626"
    assert ledger["ambiguous_reserved_usd"] == "0.000000"
    assert ledger["halted"] is False
    assert ledger["requests"][request_key]["state"] == "completed"
    assert transport.methods == ["countTokens", "generateContent"]

    resumed = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
    )
    repeated = resumed.reconcile_omitted_thought_usage(request_key)
    assert repeated["applied"] is False
    assert (
        reconciliation_path.read_bytes()
        == canonical_json(reconciliation).encode() + b"\n"
    )
    assert transport.methods == ["countTokens", "generateContent"]

    assert (
        cli_main(
            [
                "--json",
                "reconcile-usage",
                "--request-key",
                request_key,
                "--execution-gate-file",
                str(values["gate"]),
                "--shared-ledger-file",
                str(values["ledger"]),
                "--model-receipts-dir",
                str(tmp_path / "receipts"),
                "--credential-file",
                str(tmp_path / "private" / "gemini.key"),
                "--prior-construction-spend-usd",
                "0",
            ]
        )
        == 0
    )
    cli_result = json.loads(capsys.readouterr().out)
    assert cli_result["applied"] is False

    reconciliation["actual_cost_usd"] = "0.000001"
    write_json(reconciliation_path, reconciliation)
    with pytest.raises(ValueError, match="usage reconciliation event changed"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=transport,
        )
    assert (tmp_path / ".shared-ledger.json.integrity-halt.json").is_file()
    assert transport.methods == ["countTokens", "generateContent"]


def test_reconciliation_resumes_after_event_write_before_ledger_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    transport = Transport("missing_thoughts")
    values = fixture(tmp_path, transport=transport)

    def legacy_classification(submitted: dict, response: dict):
        return (
            {
                **submitted,
                "state": "ambiguous_charge",
                "error": "KeyError: 'thoughtsTokenCount'",
                "response": response,
                "live_call_made": True,
                "completed_at_utc": "2026-09-12T17:24:41Z",
            },
            None,
            None,
        )

    monkeypatch.setattr(values["broker"], "_completed_receipt", legacy_classification)
    receipt = execute(values["broker"])
    request_key = receipt["request_key"]
    review = tmp_path / "usage-review.md"
    review.write_text("The usage repair passed independent review.\n", encoding="utf-8")
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate.update(
        {
            "integrated_code_commit": "usage-repair-commit",
            "review_record": str(review),
            "review_record_sha256": sha256_file(review),
        }
    )
    write_json(values["gate"], gate)
    original_commit = values["broker"]._commit_ledger

    def crash_commit(ledger: dict):
        raise OSError("simulated reconciliation ledger crash")

    monkeypatch.setattr(values["broker"], "_commit_ledger", crash_commit)
    with pytest.raises(OSError, match="simulated reconciliation ledger crash"):
        values["broker"].reconcile_omitted_thought_usage(request_key)

    reconciliation_path = (
        tmp_path / "receipts" / f"{request_key}.usage-reconciliation.json"
    )
    assert reconciliation_path.is_file()
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["requests"][request_key]["state"] == "ambiguous_charge"

    monkeypatch.setattr(values["broker"], "_commit_ledger", original_commit)
    result = values["broker"].reconcile_omitted_thought_usage(request_key)
    assert result["applied"] is True
    assert values["broker"].status()["halted"] is False
    assert transport.methods == ["countTokens", "generateContent"]


def test_all_stages_share_one_durable_ledger(tmp_path: Path):
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    assert execute(values["broker"])["state"] == "completed"
    assert (
        execute(values["broker"], stage="question_generation", paper="p2")["state"]
        == "completed"
    )
    resumed = fixture(tmp_path, transport=transport)["broker"]
    status = resumed.status()
    assert status["generation_submissions"] == 2
    assert status["stages"]["eligibility"]["input_tokens"] == 100
    assert status["stages"]["question_generation"]["thinking_tokens"] == 5
    assert status["papers"]["family-p2"]["output_tokens"] == 10
    assert Decimal(status["spent_usd"]) > 0


def test_inconsistent_ledger_totals_fail_closed_on_restart(tmp_path: Path):
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert execute(values["broker"])["state"] == "completed"
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    ledger["spent_usd"] = "0"
    write_json(values["ledger"], ledger)
    with pytest.raises(ValueError, match="spent_usd total is inconsistent"):
        fixture(tmp_path, transport=transport)
    assert (tmp_path / ".shared-ledger.json.integrity-halt.json").is_file()


def test_consistently_lowered_ledger_spend_conflicts_with_immutable_receipt(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    request = next(iter(ledger["requests"].values()))
    request["actual_cost_usd"] = "0"
    ledger["spent_usd"] = "0"
    ledger["stages"]["eligibility"]["spent_usd"] = "0"
    ledger["papers"]["family-p1"]["spent_usd"] = "0"
    ledger["live_test_papers"]["family-p1"]["spent_usd"] = "0"
    write_json(values["ledger"], ledger)
    with pytest.raises(ValueError, match="immutable final event changed cost"):
        fixture(tmp_path, transport=Transport())


def test_ambiguous_generation_halts_without_replay(tmp_path: Path):
    transport = Transport("generate")
    broker = fixture(tmp_path, transport=transport)["broker"]
    receipt = execute(broker)
    assert receipt["state"] == "ambiguous_charge"
    assert broker.status()["halted"] is True
    with pytest.raises(ValueError, match="halted"):
        execute(broker, paper="p2")
    assert transport.methods == ["countTokens", "generateContent"]


def test_count_error_is_durable_and_never_generates(tmp_path: Path):
    transport = Transport("count")
    values = fixture(tmp_path, transport=transport)
    receipt = execute(values["broker"])
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert receipt["state"] == "count_error"
    assert ledger["requests"][receipt["request_key"]]["state"] == "count_error"
    assert values["broker"].status()["halted"] is True
    assert transport.methods == ["countTokens"]


def test_request_key_and_payload_features_fail_closed(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    body = payload()
    with pytest.raises(ValueError, match="bind"):
        broker.execute(
            phase="live_test",
            run_id="run-1",
            stage="eligibility",
            paper_id="p1",
            family_id="family-p1",
            source_version_id="source-p1",
            request_key="0" * 64,
            payload=body,
        )
    body["tools"] = []
    with pytest.raises(ValueError, match="top-level"):
        broker.execute(
            phase="live_test",
            run_id="run-1",
            stage="eligibility",
            paper_id="p1",
            family_id="family-p1",
            source_version_id="source-p1",
            request_key="0" * 64,
            payload=body,
        )
    assert transport.methods == []


def test_one_accepted_item_per_family_survives_restart(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    values["broker"].record_accepted(family_id="family-1", item_id="item-1")
    resumed = fixture(tmp_path, transport=Transport())["broker"]
    assert resumed.status()["accepted_question_count"] == 1
    with pytest.raises(ValueError, match="already"):
        resumed.record_accepted(family_id="family-1", item_id="item-2")
    with pytest.raises(ValueError, match="another paper family"):
        resumed.record_accepted(family_id="family-2", item_id="item-1")


def test_new_run_id_cannot_replay_the_same_request(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    assert execute(broker)["state"] == "completed"
    with pytest.raises(ValueError, match="already exists"):
        execute(broker, run_id="renamed-run")
    assert transport.methods == ["countTokens", "generateContent"]


def test_paper_alias_cannot_bypass_family_binding(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    assert (
        execute(broker, paper="canonical", family="family-1", source="source-v1")[
            "state"
        ]
        == "completed"
    )
    with pytest.raises(ValueError, match="already bound"):
        execute(broker, paper="alias", family="family-1", source="source-v1")
    assert transport.methods == ["countTokens", "generateContent"]


def test_source_version_cannot_move_to_an_alias_family(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    assert (
        execute(broker, paper="canonical", family="family-1", source="source-v1")[
            "state"
        ]
        == "completed"
    )
    with pytest.raises(ValueError, match="source version is already bound"):
        execute(broker, paper="canonical", family="family-alias", source="source-v1")
    assert transport.methods == ["countTokens", "generateContent"]


def test_paper_cannot_move_to_a_new_family_and_source_version(tmp_path: Path):
    transport = Transport()
    broker = fixture(tmp_path, transport=transport)["broker"]
    for stage in (
        "eligibility",
        "finding_answer_extraction",
        "question_generation",
        "blinded_reconstruction",
    ):
        assert (
            execute(
                broker,
                stage=stage,
                paper="canonical",
                family="family-1",
                source="source-v1",
            )["state"]
            == "completed"
        )
    with pytest.raises(ValueError, match="paper ID is already bound"):
        execute(
            broker,
            stage="answer_verification",
            paper="canonical",
            family="family-2",
            source="source-v2",
        )
    assert transport.methods == ["countTokens", "generateContent"] * 4


def test_received_response_is_recovered_after_final_receipt_write_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import arctic_qa.model_broker as broker_module

    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    original = broker_module.atomic_json

    def crash_final(path: Path, value: object, *, immutable: bool = False):
        if (
            path.parent.name == "receipts"
            and path.name.endswith(".json")
            and not (
                path.name.endswith(".submitted.json")
                or path.name.endswith(".received.json")
            )
        ):
            raise OSError("simulated final receipt crash")
        return original(path, value, immutable=immutable)

    monkeypatch.setattr(broker_module, "atomic_json", crash_final)
    with pytest.raises(OSError, match="simulated final receipt crash"):
        execute(values["broker"])
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    request_key = next(iter(ledger["requests"]))
    assert ledger["requests"][request_key]["state"] == "submitted"
    assert (tmp_path / "receipts" / f"{request_key}.received.json").is_file()

    monkeypatch.setattr(broker_module, "atomic_json", original)
    resumed = fixture(tmp_path, transport=transport)["broker"]
    with pytest.raises(ValueError, match="already exists"):
        execute(resumed)
    recovered = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert recovered["requests"][request_key]["state"] == "completed"
    final = json.loads(
        (tmp_path / "receipts" / f"{request_key}.json").read_text(encoding="utf-8")
    )
    assert final["response"]["usageMetadata"]["totalTokenCount"] == 115
    assert transport.methods == ["countTokens", "generateContent"]


def test_deleted_request_cannot_orphan_immutable_spend_events(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    ledger["requests"] = {}
    ledger["family_bindings"] = {}
    ledger["paper_bindings"] = {}
    ledger["stages"] = {}
    ledger["papers"] = {}
    ledger["live_test_papers"] = {}
    ledger["spent_usd"] = "0"
    ledger["generation_submissions"] = 0
    ledger["count_requests"] = 0
    ledger["recent_submission_times_utc"] = []
    write_json(values["ledger"], ledger)
    with pytest.raises(ValueError, match="immutable paid-call event"):
        fixture(tmp_path, transport=Transport())


def test_integrity_halt_republishes_honest_status(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    broker = values["broker"]
    status_path = tmp_path / "shared-ledger.status.json"
    observed: list[dict] = []
    broker.set_status_observer(
        lambda path: observed.append(json.loads(path.read_text(encoding="utf-8")))
    )
    before = json.loads(status_path.read_text(encoding="utf-8"))
    write_json(tmp_path / "receipts" / f"{'f' * 64}.json", {})

    with pytest.raises(ValueError, match="immutable paid-call event"):
        broker.status()

    after = json.loads(status_path.read_text(encoding="utf-8"))
    assert after != before
    assert after["halted"] is True
    assert after["integrity_valid"] is False
    assert "immutable paid-call event" in after["halt_reason"]
    assert observed[-1] == after


def test_production_spend_does_not_renew_or_inflate_live_test(tmp_path: Path):
    transport = Transport()
    values = fixture(tmp_path, transport=transport)
    assert execute(values["broker"])["state"] == "completed"
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate["allowed_phase"] = "away_production"
    write_json(values["gate"], gate)
    assert (
        execute(
            values["broker"],
            phase="away_production",
            stage="question_generation",
        )["state"]
        == "completed"
    )
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["live_test_papers"]["family-p1"]["spent_usd"] == "0.000132"
    assert ledger["generation_submissions"] == 2


def test_initialized_ledger_cannot_silently_reset(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    values["ledger"].unlink()
    with pytest.raises(ValueError, match="absent after initialization"):
        fixture(tmp_path, transport=Transport())


def test_reviewed_config_transition_preserves_spend_and_immutable_custody(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    ledger = values["ledger"]
    identity = tmp_path / ".shared-ledger.json.identity.json"
    original_ledger = ledger.read_bytes()
    original_identity = identity.read_bytes()
    original_receipts = {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    }

    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)

    resumed = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=ledger,
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=transition,
    )

    status = resumed.status()
    assert status["spent_usd"] == "0.000132"
    assert status["generation_submissions"] == 1
    assert status["initial_price_config_sha256"] != status["price_config_sha256"]
    assert ledger.read_bytes() == original_ledger
    assert identity.read_bytes() == original_identity
    for name, content in original_receipts.items():
        assert (tmp_path / "receipts" / name).read_bytes() == content
    transition_events = list((tmp_path / "receipts").glob("config-transition-*.json"))
    assert len(transition_events) == 1

    restarted = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=ledger,
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
    )
    assert restarted.status()["config_transition_sha256"] == sha256_file(
        transition_events[0]
    )
    new_receipt = execute(restarted, paper="p2")
    assert new_receipt["price_config_sha256"] == sha256_file(active_config)
    assert new_receipt["config_transition_sha256"] == sha256_file(transition_events[0])

    replay_transport = Transport()
    replayed = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=ledger,
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=replay_transport,
    )
    replay_status = replayed.status()
    assert replay_status["generation_submissions"] == 2
    with pytest.raises(ValueError, match="request key already exists"):
        execute(replayed, paper="p2")
    assert replay_transport.methods == []

    changed_event = json.loads(transition_events[0].read_text(encoding="utf-8"))
    changed_event["authorization"]["unreviewed_field"] = True
    changed_event["transition_authorization_sha256"] = sha256_bytes(
        canonical_json(changed_event["authorization"]).encode()
    )
    changed_path = transition_events[0].with_name(
        f"config-transition-{changed_event['transition_authorization_sha256']}.json"
    )
    transition_events[0].rename(changed_path)
    write_json(changed_path, changed_event)
    with pytest.raises(ValueError, match="applied price configuration transition"):
        restarted.status()
    assert (tmp_path / ".shared-ledger.json.integrity-halt.json").is_file()


def test_changed_config_without_reviewed_transition_fails_without_state_change(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    ledger = values["ledger"]
    identity = tmp_path / ".shared-ledger.json.identity.json"
    original_ledger = ledger.read_bytes()
    original_identity = identity.read_bytes()
    original_receipts = {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    }
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )

    with pytest.raises(ValueError, match="requires a reviewed transition"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=ledger,
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
        )

    assert ledger.read_bytes() == original_ledger
    assert identity.read_bytes() == original_identity
    assert {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    } == original_receipts


def test_transition_with_wrong_ledger_hash_creates_no_event(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(
        tmp_path,
        values,
        active_config,
        expected_ledger_sha256="0" * 64,
    )

    with pytest.raises(ValueError, match="transition ledger hash changed"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
            config_transition_file=transition,
        )

    assert list((tmp_path / "receipts").glob("config-transition-*.json")) == []


def test_forged_transition_event_cannot_authorize_restart_or_submit(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    identity = tmp_path / ".shared-ledger.json.identity.json"
    initial_config_hash = json.loads(identity.read_text(encoding="utf-8"))[
        "price_config_sha256"
    ]
    authorization = {
        "schema": "shared-paid-call-config-transition-v1",
        "ledger_file": str(values["ledger"].resolve()),
        "from_price_config_sha256": initial_config_hash,
        "to_price_config_sha256": sha256_file(active_config),
        "expected_ledger_sha256": "0" * 64,
        "expected_identity_sha256": sha256_file(identity),
        "execution_gate_sha256": "1" * 64,
        "integrated_code_commit": "not-reviewed",
        "review_record": str(tmp_path / "review-does-not-exist.md"),
        "review_record_sha256": "2" * 64,
        "reason": "Forged direct receipt event.",
        "authorized_at_utc": "2026-09-12T09:00:00Z",
    }
    authorization_hash = sha256_bytes(canonical_json(authorization).encode())
    event = tmp_path / "receipts" / f"config-transition-{authorization_hash}.json"
    write_json(
        event,
        {
            "schema": "shared-paid-call-config-transition-event-v1",
            "authorization": authorization,
            "transition_authorization_sha256": authorization_hash,
            "applied_at_utc": "2026-09-12T09:01:00Z",
        },
    )
    forged_transport = Transport()

    transition_error = None
    try:
        resumed = SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=forged_transport,
        )
        execute(resumed, paper="p2")
    except ValueError as error:
        transition_error = error

    assert forged_transport.methods == []
    assert transition_error is not None
    assert "transition" in str(transition_error)
    ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
    assert ledger["generation_submissions"] == 1
    assert len(ledger["requests"]) == 1


def test_unreviewed_transition_event_stops_before_provider_submission(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    identity = tmp_path / ".shared-ledger.json.identity.json"
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    authorization = {
        "schema": "shared-paid-call-config-transition-v1",
        "ledger_file": str(values["ledger"].resolve()),
        "from_price_config_sha256": json.loads(identity.read_text(encoding="utf-8"))[
            "price_config_sha256"
        ],
        "to_price_config_sha256": sha256_file(active_config),
        "expected_ledger_sha256": sha256_file(values["ledger"]),
        "expected_identity_sha256": sha256_file(identity),
        "execution_gate_sha256": sha256_file(values["gate"]),
        "integrated_code_commit": gate["integrated_code_commit"],
        "review_record": gate["review_record"],
        "review_record_sha256": None,
        "reason": "Unreviewed direct receipt event.",
        "authorized_at_utc": "2026-09-12T09:00:00Z",
    }
    authorization_hash = sha256_bytes(canonical_json(authorization).encode())
    write_json(
        tmp_path / "receipts" / f"config-transition-{authorization_hash}.json",
        {
            "schema": "shared-paid-call-config-transition-event-v1",
            "authorization": authorization,
            "transition_authorization_sha256": authorization_hash,
            "applied_at_utc": "2026-09-12T09:01:00Z",
        },
    )
    unreviewed_transport = Transport()

    with pytest.raises(ValueError, match="transition review is invalid"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=unreviewed_transport,
        )

    assert unreviewed_transport.methods == []


def test_forged_transition_event_cannot_replace_request_anchor(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)
    transitioned = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=transition,
    )
    assert execute(transitioned, paper="p2")["state"] == "completed"

    original_event = next((tmp_path / "receipts").glob("config-transition-*.json"))
    forged_event = json.loads(original_event.read_text(encoding="utf-8"))
    forged_event["authorization"]["expected_ledger_sha256"] = "0" * 64
    forged_event["authorization"]["review_record"] = str(
        tmp_path / "review-does-not-exist.md"
    )
    forged_hash = sha256_bytes(canonical_json(forged_event["authorization"]).encode())
    forged_event["transition_authorization_sha256"] = forged_hash
    forged_path = original_event.with_name(f"config-transition-{forged_hash}.json")
    original_event.rename(forged_path)
    write_json(forged_path, forged_event)
    forged_transport = Transport()

    transition_error = None
    try:
        restarted = SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=forged_transport,
        )
        execute(restarted, paper="p3")
    except ValueError as error:
        transition_error = error

    assert forged_transport.methods == []
    assert transition_error is not None
    assert "transition" in str(transition_error)


def test_extra_transition_event_stops_active_broker_before_submission(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)
    active_transport = Transport()
    transitioned = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=active_transport,
        config_transition_file=transition,
    )
    original_event = next((tmp_path / "receipts").glob("config-transition-*.json"))
    extra_event = json.loads(original_event.read_text(encoding="utf-8"))
    extra_event["authorization"]["reason"] = "Crafted second transition event."
    extra_hash = sha256_bytes(canonical_json(extra_event["authorization"]).encode())
    extra_event["transition_authorization_sha256"] = extra_hash
    write_json(
        original_event.with_name(f"config-transition-{extra_hash}.json"),
        extra_event,
    )

    with pytest.raises(ValueError, match="multiple applied price configuration"):
        execute(transitioned, paper="p2")

    assert active_transport.methods == []


def test_transition_review_is_rechecked_before_first_submission(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)
    active_transport = Transport()
    transitioned = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=active_transport,
        config_transition_file=transition,
    )
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    Path(gate["review_record"]).write_text(
        "The review record changed.\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="transition review is invalid"):
        execute(transitioned, paper="p2")

    assert active_transport.methods == []


def test_transition_gate_binding_is_rechecked_after_first_submission(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_policy = policy_with_paper_limit(tmp_path)
    transition = reviewed_policy_transition(tmp_path, values, active_policy)
    active_transport = Transport()
    transitioned = SharedGeminiBroker(
        policy_file=active_policy,
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=active_transport,
        config_transition_file=transition,
    )
    assert execute(transitioned, paper="p2")["state"] == "completed"
    active_transport.methods.clear()
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate["integrated_code_commit"] = "different-commit"
    write_json(values["gate"], gate)

    with pytest.raises(ValueError, match="configuration transition review changed"):
        execute(transitioned, paper="p3")

    assert active_transport.methods == []


def test_transition_review_file_is_rechecked_after_first_submission(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_policy = policy_with_paper_limit(tmp_path)
    transition = reviewed_policy_transition(tmp_path, values, active_policy)
    active_transport = Transport()
    transitioned = SharedGeminiBroker(
        policy_file=active_policy,
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=active_transport,
        config_transition_file=transition,
    )
    assert execute(transitioned, paper="p2")["state"] == "completed"
    active_transport.methods.clear()
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    Path(gate["review_record"]).write_text(
        "The review record changed after the first request.\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="configuration transition review is invalid"):
        execute(transitioned, paper="p3")

    assert active_transport.methods == []


def test_transition_restart_with_unchanged_review_allows_downstream(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_policy = policy_with_paper_limit(tmp_path)
    transition = reviewed_policy_transition(tmp_path, values, active_policy)
    transitioned = SharedGeminiBroker(
        policy_file=active_policy,
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=transition,
    )
    assert execute(transitioned, paper="p2")["state"] == "completed"
    restarted_transport = Transport()
    restarted = SharedGeminiBroker(
        policy_file=active_policy,
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=restarted_transport,
    )

    downstream = execute(
        restarted,
        paper="p2",
        stage="question_generation",
        body={
            **payload(),
            "contents": [{"role": "user", "parts": [{"text": "Downstream payload."}]}],
        },
    )

    assert downstream["state"] == "completed"
    assert restarted_transport.methods == ["countTokens", "generateContent"]


def test_transition_does_not_apply_to_an_unsettled_ledger(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport("generate"))
    assert execute(values["broker"])["state"] == "ambiguous_charge"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    transition = reviewed_transition(tmp_path, values, active_config)

    with pytest.raises(ValueError, match="requires a settled ledger"):
        SharedGeminiBroker(
            policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
            price_config_file=active_config,
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
            config_transition_file=transition,
        )

    assert list((tmp_path / "receipts").glob("config-transition-*.json")) == []


def test_reviewed_policy_transition_preserves_spend_and_immutable_custody(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    identity = tmp_path / ".shared-ledger.json.identity.json"
    original_ledger = values["ledger"].read_bytes()
    original_identity = identity.read_bytes()
    original_receipts = {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    }
    active_policy = policy_with_paper_limit(tmp_path)
    transition = reviewed_policy_transition(tmp_path, values, active_policy)

    transitioned = SharedGeminiBroker(
        policy_file=active_policy,
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=transition,
    )

    status = transitioned.status()
    assert status["limits"]["live_test_maximum_papers"] == 40
    assert status["usage"]["live_test_papers"] == 1
    assert status["remaining"]["live_test_papers"] == 39
    assert status["initial_policy_sha256"] != status["policy_sha256"]
    assert status["limits"]["authorized_live_test_ceiling_usd"] == "5.00"
    assert Decimal(status["remaining"]["live_test_usd"]) < Decimal("5.00")
    assert status["generation_submissions"] == 1
    assert values["ledger"].read_bytes() == original_ledger
    assert identity.read_bytes() == original_identity
    for name, content in original_receipts.items():
        assert (tmp_path / "receipts" / name).read_bytes() == content

    transition_event = next((tmp_path / "receipts").glob("config-transition-*.json"))
    new_receipt = execute(transitioned, paper="p2")
    assert new_receipt["policy_sha256"] == sha256_file(active_policy)
    assert new_receipt["config_transition_sha256"] == sha256_file(transition_event)

    restarted = SharedGeminiBroker(
        policy_file=active_policy,
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
    )
    assert restarted.status()["config_transition_sha256"] == sha256_file(
        transition_event
    )


def test_reviewed_family_and_submission_extension_chains_from_active_policy(
    tmp_path: Path,
) -> None:
    values = prepared_family_and_submission_transition(tmp_path)
    identity = tmp_path / ".shared-ledger.json.identity.json"
    original_ledger = values["ledger"].read_bytes()
    original_identity = identity.read_bytes()
    original_receipts = {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    }
    transport = Transport()

    proposed_broker = SharedGeminiBroker(
        policy_file=values["proposed_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=values["proposed_transition"],
    )

    status = proposed_broker.status()
    assert status["limits"]["live_test_maximum_papers"] == 41
    assert status["limits"]["live_test_maximum_generation_submissions"] == 101
    assert status["limits"]["authorized_live_test_ceiling_usd"] == "5.00"
    assert status["limits"]["maximum_paper_cost_usd"] == "1.00"
    assert status["limits"]["maximum_request_reserved_cost_usd"] == "0.25"
    assert status["limits"]["maximum_concurrent_generation_requests"] == 2
    assert status["limits"]["maximum_generation_requests_per_minute"] == 10
    assert values["ledger"].read_bytes() == original_ledger
    assert identity.read_bytes() == original_identity
    for name, content in original_receipts.items():
        assert (tmp_path / "receipts" / name).read_bytes() == content

    active_transition = status["config_transition_sha256"]
    assert active_transition != values["predecessor"]
    receipt = execute(proposed_broker, paper="p2")
    assert receipt["state"] == "completed"
    assert receipt["policy_sha256"] == sha256_file(values["proposed_policy"])
    assert receipt["config_transition_sha256"] == active_transition

    restarted = SharedGeminiBroker(
        policy_file=values["proposed_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
    )
    assert restarted.status()["config_transition_sha256"] == active_transition


def test_budget_bounded_transition_removes_live_count_caps_and_survives_restart(
    tmp_path: Path,
) -> None:
    values = prepared_budget_bounded_transition(tmp_path)
    original_ledger = values["ledger"].read_bytes()
    transport = Transport()
    broker = SharedGeminiBroker(
        policy_file=values["budget_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=values["budget_transition"],
    )
    bind_budget_stream_input(broker, values)

    status = broker.status()
    assert status["limits"]["live_test_maximum_papers"] is None
    assert status["limits"]["live_test_maximum_generation_submissions"] is None
    assert status["remaining"]["live_test_papers"] is None
    assert status["remaining"]["live_test_generation_submissions"] is None
    assert status["limits"]["authorized_live_test_ceiling_usd"] == "5.00"
    assert values["ledger"].read_bytes() == original_ledger
    active_transition = status["config_transition_sha256"]

    receipt = execute(broker, paper="p2")
    assert receipt["state"] == "completed"
    assert receipt["config_transition_sha256"] == active_transition

    restarted = SharedGeminiBroker(
        policy_file=values["budget_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
    )
    assert restarted.status()["config_transition_sha256"] == active_transition


def test_family_and_submission_extension_stops_new_forty_second_family(
    tmp_path: Path,
) -> None:
    values = prepared_family_and_submission_transition(tmp_path)
    transport = Transport()
    broker = SharedGeminiBroker(
        policy_file=values["proposed_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=values["proposed_transition"],
    )
    for position in range(2, 42):
        assert execute(broker, paper=f"p{position}")["state"] == "completed"
        ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
        ledger["recent_submission_times_utc"] = []
        write_json(values["ledger"], ledger)

    assert broker.status()["remaining"]["live_test_papers"] == 0
    transport.methods.clear()
    blocked = execute(broker, paper="p42")

    assert blocked["state"] == "not_submitted"
    assert blocked["reason"] == "the live test reached its paper limit"
    assert blocked["live_call_made"] is False
    assert transport.methods == ["countTokens"]


def test_budget_bounded_transition_stops_at_cumulative_trial_amount(
    tmp_path: Path,
) -> None:
    values = prepared_budget_bounded_transition(tmp_path)
    transport = HighCostTransport()
    broker = SharedGeminiBroker(
        policy_file=values["budget_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=values["budget_transition"],
    )
    bind_budget_stream_input(broker, values)
    body = payload()
    body["generationConfig"]["maxOutputTokens"] = 8_192
    for position in range(2, 22):
        assert execute(broker, paper=f"p{position}", body=body)["state"] == (
            "completed"
        )
        ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
        ledger["recent_submission_times_utc"] = []
        write_json(values["ledger"], ledger)

    status = broker.status()
    assert Decimal(status["spent_usd"]) < Decimal("5.00")
    assert Decimal(status["live_test_remaining_usd"]) < Decimal("0.25")
    transport.methods.clear()
    blocked = execute(broker, paper="p22", body=body)

    assert blocked["state"] == "not_submitted"
    assert blocked["reason"] == "the paid request exceeds the authorized live-test cap"
    assert blocked["live_call_made"] is False
    assert transport.methods == ["countTokens"]
    assert Decimal(broker.status()["spent_usd"]) < Decimal("5.00")


def test_budget_bounded_transition_rechecks_gate_after_request(
    tmp_path: Path,
) -> None:
    values = prepared_budget_bounded_transition(tmp_path)
    transport = Transport()
    broker = SharedGeminiBroker(
        policy_file=values["budget_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=values["budget_transition"],
    )
    bind_budget_stream_input(broker, values)
    assert execute(broker, paper="p2")["state"] == "completed"
    transport.methods.clear()
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate["integrated_code_commit"] = "different-commit"
    write_json(values["gate"], gate)

    with pytest.raises(ValueError, match="configuration transition review changed"):
        execute(broker, paper="p3")

    assert transport.methods == []


def test_budget_bounded_transition_rejects_partial_limit_removal(
    tmp_path: Path,
) -> None:
    values = prepared_budget_bounded_transition(tmp_path)
    authorization = json.loads(values["budget_transition"].read_text(encoding="utf-8"))
    authorization["changed_policy_fields"] = {
        "live_test_maximum_papers": {"from": 41, "to": None}
    }
    write_json(values["budget_transition"], authorization)
    transport = Transport()

    with pytest.raises(ValueError, match="policy transition change set"):
        SharedGeminiBroker(
            policy_file=values["budget_policy"],
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=transport,
            config_transition_file=values["budget_transition"],
        )

    assert transport.methods == []


def test_budget_bounded_transition_requires_complete_stream_input_gate(
    tmp_path: Path,
) -> None:
    values = prepared_budget_bounded_transition(tmp_path)
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    del gate["continuation_input_binding_version"]
    write_json(values["gate"], gate)
    authorization = json.loads(values["budget_transition"].read_text(encoding="utf-8"))
    authorization["execution_gate_sha256"] = sha256_file(values["gate"])
    write_json(values["budget_transition"], authorization)

    with pytest.raises(ValueError, match="reviewed continuation input version"):
        SharedGeminiBroker(
            policy_file=values["budget_policy"],
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
            config_transition_file=values["budget_transition"],
        )


@pytest.mark.parametrize(
    "changed_policy_fields",
    [
        {"live_test_maximum_papers": {"from": 40, "to": 41}},
        {"live_test_maximum_generation_submissions": {"from": 100, "to": 101}},
        {
            "live_test_maximum_papers": {"from": 39, "to": 41},
            "live_test_maximum_generation_submissions": {"from": 100, "to": 101},
        },
        {
            "live_test_maximum_papers": {"from": 40, "to": 41},
            "live_test_maximum_generation_submissions": {"from": 100, "to": 102},
        },
    ],
)
def test_family_and_submission_extension_rejects_mismatched_change_set(
    tmp_path: Path,
    changed_policy_fields: dict,
) -> None:
    values = prepared_family_and_submission_transition(tmp_path)
    authorization = json.loads(
        values["proposed_transition"].read_text(encoding="utf-8")
    )
    authorization["changed_policy_fields"] = changed_policy_fields
    write_json(values["proposed_transition"], authorization)
    transport = Transport()

    with pytest.raises(ValueError, match="policy transition change set"):
        SharedGeminiBroker(
            policy_file=values["proposed_policy"],
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=transport,
            config_transition_file=values["proposed_transition"],
        )

    assert transport.methods == []
    assert len(list((tmp_path / "receipts").glob("config-transition-*.json"))) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("maximum_paper_cost_usd", "1.01"),
        ("maximum_request_reserved_cost_usd", "0.26"),
        ("maximum_concurrent_generation_requests", 3),
        ("maximum_generation_requests_per_minute", 11),
    ],
)
def test_family_and_submission_extension_rejects_changed_accounting_control(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    values = prepared_family_and_submission_transition(tmp_path)
    policy = json.loads(values["proposed_policy"].read_text(encoding="utf-8"))
    policy[field] = value
    write_json(values["proposed_policy"], policy)
    transport = Transport()

    with pytest.raises(ValueError, match=f"streaming budget value changed: {field}"):
        SharedGeminiBroker(
            policy_file=values["proposed_policy"],
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=transport,
            config_transition_file=values["proposed_transition"],
        )

    assert transport.methods == []
    assert len(list((tmp_path / "receipts").glob("config-transition-*.json"))) == 1


@pytest.mark.parametrize(
    ("paper_limit", "submission_limit"),
    [(41, 100), (40, 101), (42, 101), (None, 101), (41, None)],
)
def test_streaming_policy_rejects_unapproved_live_test_limit_pairs(
    tmp_path: Path,
    paper_limit: int | None,
    submission_limit: int | None,
) -> None:
    values = fixture(tmp_path, transport=Transport())
    policy = policy_with_limits(
        tmp_path,
        paper_limit=paper_limit,
        submission_limit=submission_limit,
    )
    transport = Transport()

    with pytest.raises(ValueError, match="streaming budget live-test limits changed"):
        SharedGeminiBroker(
            policy_file=policy,
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=transport,
        )

    assert transport.methods == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "maximum_authorized_cumulative_tranche_usd",
            "5.01",
            "policy transition identity",
        ),
        ("expected_ledger_sha256", "0" * 64, "transition ledger hash"),
        ("from_config_transition_sha256", "0" * 64, "transition predecessor"),
    ],
)
def test_family_and_submission_extension_rejects_changed_transition_binding(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    values = prepared_family_and_submission_transition(tmp_path)
    authorization = json.loads(
        values["proposed_transition"].read_text(encoding="utf-8")
    )
    authorization[field] = value
    write_json(values["proposed_transition"], authorization)
    transport = Transport()

    with pytest.raises(ValueError, match=message):
        SharedGeminiBroker(
            policy_file=values["proposed_policy"],
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "gemini.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=transport,
            config_transition_file=values["proposed_transition"],
        )

    assert transport.methods == []
    assert len(list((tmp_path / "receipts").glob("config-transition-*.json"))) == 1


def test_family_and_submission_extension_rechecks_gate_after_request(
    tmp_path: Path,
) -> None:
    values = prepared_family_and_submission_transition(tmp_path)
    transport = Transport()
    broker = SharedGeminiBroker(
        policy_file=values["proposed_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=values["proposed_transition"],
    )
    assert execute(broker, paper="p2")["state"] == "completed"
    transport.methods.clear()
    gate = json.loads(values["gate"].read_text(encoding="utf-8"))
    gate["integrated_code_commit"] = "different-commit"
    write_json(values["gate"], gate)

    with pytest.raises(ValueError, match="configuration transition review changed"):
        execute(broker, paper="p3")

    assert transport.methods == []


def test_family_and_submission_extension_stops_after_101_submissions(
    tmp_path: Path,
) -> None:
    values = prepared_family_and_submission_transition(tmp_path)
    transport = Transport()
    broker = SharedGeminiBroker(
        policy_file=values["proposed_policy"],
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=values["proposed_transition"],
    )
    for submission in range(2, 102):
        body = {
            **payload(),
            "contents": [
                {"role": "user", "parts": [{"text": f"Request {submission}."}]}
            ],
        }
        assert execute(broker, paper="p1", body=body)["state"] == "completed"
        ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
        ledger["recent_submission_times_utc"] = []
        write_json(values["ledger"], ledger)

    status = broker.status()
    assert status["generation_submissions"] == 101
    assert status["remaining"]["live_test_generation_submissions"] == 0
    transport.methods.clear()
    blocked = execute(
        broker,
        paper="p1",
        body={
            **payload(),
            "contents": [{"role": "user", "parts": [{"text": "Request 102."}]}],
        },
    )

    assert blocked["state"] == "not_submitted"
    assert blocked["reason"] == "the live-test submission limit is complete"
    assert blocked["live_call_made"] is False
    assert transport.methods == ["countTokens"]


def test_policy_change_without_reviewed_transition_fails_without_state_change(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_policy = policy_with_paper_limit(tmp_path)
    original_ledger = values["ledger"].read_bytes()
    original_receipts = {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    }

    with pytest.raises(ValueError, match="requires a reviewed transition"):
        SharedGeminiBroker(
            policy_file=active_policy,
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
        )

    assert values["ledger"].read_bytes() == original_ledger
    assert {
        path.name: path.read_bytes() for path in (tmp_path / "receipts").iterdir()
    } == original_receipts


def test_policy_transition_rejects_any_second_policy_change_before_transport(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    active_policy = policy_with_paper_limit(tmp_path)
    policy = json.loads(active_policy.read_text(encoding="utf-8"))
    policy["maximum_generation_requests_per_minute"] = 11
    write_json(active_policy, policy)
    transition = reviewed_policy_transition(tmp_path, values, active_policy)
    transport = Transport()

    with pytest.raises(ValueError, match="streaming budget value changed"):
        SharedGeminiBroker(
            policy_file=active_policy,
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=transport,
            config_transition_file=transition,
        )

    assert transport.methods == []
    assert list((tmp_path / "receipts").glob("config-transition-*.json")) == []


def test_policy_transition_rejects_changed_field_declaration_before_transport(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    active_policy = policy_with_paper_limit(tmp_path)
    transition = reviewed_policy_transition(
        tmp_path,
        values,
        active_policy,
        changed_policy_fields={"live_test_maximum_papers": {"from": 20, "to": 41}},
    )
    transport = Transport()

    with pytest.raises(ValueError, match="policy transition change set"):
        SharedGeminiBroker(
            policy_file=active_policy,
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=transport,
            config_transition_file=transition,
        )

    assert transport.methods == []
    assert list((tmp_path / "receipts").glob("config-transition-*.json")) == []


def test_expanded_policy_cannot_initialize_a_new_ledger(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    active_policy = policy_with_paper_limit(tmp_path)

    with pytest.raises(ValueError, match="requires an existing reviewed ledger"):
        SharedGeminiBroker(
            policy_file=active_policy,
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=tmp_path / "new-ledger.json",
            receipts_dir=tmp_path / "new-receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
        )


def test_policy_transition_layers_on_existing_reviewed_price_transition(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_config = tmp_path / "active-price-config.json"
    active_config.write_bytes(
        (ROOT / "config" / "gemini-eligibility-v1.json").read_bytes() + b"\n"
    )
    price_transition = reviewed_transition(tmp_path, values, active_config)
    price_broker = SharedGeminiBroker(
        policy_file=ROOT / "config" / "streaming-dataset-budget-policy-v1.json",
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=price_transition,
    )
    assert execute(price_broker, paper="p2")["state"] == "completed"
    price_event = next((tmp_path / "receipts").glob("config-transition-*.json"))
    active_policy = policy_with_paper_limit(tmp_path)
    policy_transition = reviewed_policy_transition(
        tmp_path,
        values,
        active_policy,
        active_config=active_config,
        from_config_transition_sha256=sha256_file(price_event),
    )

    policy_broker = SharedGeminiBroker(
        policy_file=active_policy,
        price_config_file=active_config,
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=Transport(),
        config_transition_file=policy_transition,
    )
    status = policy_broker.status()
    assert status["limits"]["live_test_maximum_papers"] == 40
    assert status["usage"]["live_test_papers"] == 2
    assert status["remaining"]["live_test_papers"] == 38
    assert execute(policy_broker, paper="p3")["state"] == "completed"
    assert len(list((tmp_path / "receipts").glob("config-transition-*.json"))) == 2


def test_policy_transition_rejects_wrong_predecessor_and_tranche(tmp_path: Path):
    values = fixture(tmp_path, transport=Transport())
    active_policy = policy_with_paper_limit(tmp_path)
    wrong_predecessor = reviewed_policy_transition(
        tmp_path,
        values,
        active_policy,
        from_config_transition_sha256="0" * 64,
    )

    with pytest.raises(ValueError, match="policy transition predecessor"):
        SharedGeminiBroker(
            policy_file=active_policy,
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
            config_transition_file=wrong_predecessor,
        )

    wrong_tranche = reviewed_policy_transition(
        tmp_path,
        values,
        active_policy,
        maximum_authorized_cumulative_tranche_usd="5.01",
    )
    with pytest.raises(ValueError, match="policy transition identity"):
        SharedGeminiBroker(
            policy_file=active_policy,
            price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
            execution_gate_file=values["gate"],
            ledger_file=values["ledger"],
            receipts_dir=tmp_path / "receipts",
            credential_file=tmp_path / "private" / "absent.key",
            prior_construction_spend_usd=Decimal("0"),
            transport=Transport(),
            config_transition_file=wrong_tranche,
        )


def test_expanded_policy_stops_new_forty_first_family_but_allows_downstream(
    tmp_path: Path,
):
    values = fixture(tmp_path, transport=Transport())
    assert execute(values["broker"])["state"] == "completed"
    active_policy = policy_with_paper_limit(tmp_path)
    transition = reviewed_policy_transition(tmp_path, values, active_policy)
    transport = Transport()
    broker = SharedGeminiBroker(
        policy_file=active_policy,
        price_config_file=ROOT / "config" / "gemini-eligibility-v1.json",
        execution_gate_file=values["gate"],
        ledger_file=values["ledger"],
        receipts_dir=tmp_path / "receipts",
        credential_file=tmp_path / "private" / "gemini.key",
        prior_construction_spend_usd=Decimal("0"),
        transport=transport,
        config_transition_file=transition,
    )
    for position in range(2, 41):
        assert execute(broker, paper=f"p{position}")["state"] == "completed"
        ledger = json.loads(values["ledger"].read_text(encoding="utf-8"))
        ledger["recent_submission_times_utc"] = []
        write_json(values["ledger"], ledger)

    status = broker.status()
    assert status["usage"]["live_test_papers"] == 40
    assert status["remaining"]["live_test_papers"] == 0
    transport.methods.clear()
    blocked = execute(broker, paper="p41")
    assert blocked["state"] == "not_submitted"
    assert blocked["reason"] == "the live test reached its paper limit"
    assert blocked["live_call_made"] is False
    assert transport.methods == ["countTokens"]

    transport.methods.clear()
    downstream = execute(
        broker,
        paper="p40",
        stage="question_generation",
        body={
            **payload(),
            "contents": [
                {"role": "user", "parts": [{"text": "Different stage payload."}]}
            ],
        },
    )
    assert downstream["state"] == "completed"
    assert transport.methods == ["countTokens", "generateContent"]
