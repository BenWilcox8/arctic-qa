from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from arctic_qa.abstention_plan import (
    PLAN_MANIFEST_FILENAME,
    build_vendor_runs,
    PLAN_SUMMARY_FILENAME,
    VendorRun,
    dry_run_plan,
    effective_concurrency,
    load_pause,
    load_plan,
    merge_pause,
    parse_concurrency,
    parse_pause_models,
    paused_models,
    run_plan,
    score_plan,
    write_plan_gates,
)
from arctic_qa.abstention_providers import (
    PROVIDER_GOOGLE_GEMINI,
    ScriptedEvaluationProvider,
)
from arctic_qa.abstention_render import N0
from arctic_qa.abstention_run import RESPONSES_FILENAME
from arctic_qa.abstention_set import load_eval_set
from arctic_qa.abstention_subscription import (
    PROVIDER_ANTHROPIC_CLAUDE_CODE,
    PROVIDER_OPENAI_CODEX,
    SubscriptionLedger,
    build_subscription_provider,
    load_subscription_models,
    subscription_decoding_record,
    subscription_gate_record,
    ScriptedSubscriptionTransport,
    scripted_subscription_answers,
)
from arctic_qa.abstention_run import plan_trials
from arctic_qa.cli import main as cli_main
from arctic_qa.model_broker import SharedGeminiBroker
from arctic_qa.util import atomic_json
from arctic_qa import ledger_store  # noqa: E402
from test_abstention_broker import LetterTransport, bind, evaluation_fixture, execute
from test_abstention_run import frozen_set
from test_model_broker import payload as construction_payload, write_json


ROOT = Path(__file__).parents[1]
PLAN_FILE = ROOT / "config" / "benchmark-evaluation-plan-high-v1.json"
PAUSE_FILE = ROOT / "config" / "benchmark-evaluation-model-pause-v1.json"
POLICY_V2 = ROOT / "config" / "benchmark-evaluation-policy-v2.json"
PRICES = ROOT / "config" / "benchmark-evaluation-prices-v1.json"
MODELS_FILE = ROOT / "config" / "benchmark-evaluation-subscription-models-v1.json"
CONSTRUCTION = {
    "construction_policy_file": ROOT
    / "config"
    / "streaming-dataset-budget-policy-v1.json",
    "construction_price_config_file": ROOT / "config" / "gemini-eligibility-v1.json",
    "construction_gate_file": ROOT / "config" / "streaming-live-execution-gate-v1.json",
}


def test_plan_file_validates_and_counts_48_trials_per_item() -> None:
    plan = load_plan(PLAN_FILE)
    assert plan["trials_per_item"] == 48
    assert plan["arms"] == ["high"] and plan["repeats"] == 3
    assert sum(len(v["models"]) for v in plan["vendors"].values()) == 8
    policy = json.loads(POLICY_V2.read_text())
    assert effective_concurrency(plan, policy, PROVIDER_GOOGLE_GEMINI) == 4
    assert effective_concurrency(plan, policy, PROVIDER_ANTHROPIC_CLAUDE_CODE) == 3
    assert effective_concurrency(plan, policy, PROVIDER_OPENAI_CODEX, requested=2) == 2
    v1 = json.loads(
        (ROOT / "config" / "benchmark-evaluation-policy-v1.json").read_text()
    )
    assert effective_concurrency(plan, v1, PROVIDER_GOOGLE_GEMINI) == 1
    assert parse_concurrency("google_gemini=2, openai_codex=1") == {
        "google_gemini": 2,
        "openai_codex": 1,
    }
    with pytest.raises(ValueError, match="invalid concurrency"):
        parse_concurrency("bogus=3")
    with pytest.raises(ValueError, match="unknown vendor"):
        load_plan(_plan_variant(vendors={"other": {"models": ["x"]}}))
    with pytest.raises(ValueError, match="repeats or misnames"):
        load_plan(
            _plan_variant(
                vendors={
                    "google_gemini": {"models": ["gemini-3.8-flash"]},
                    "openai_codex": {"models": ["gemini-3.8-flash"]},
                }
            )
        )


def _plan_variant(**changes: object) -> Path:
    import tempfile

    plan = json.loads(PLAN_FILE.read_text())
    plan.update(changes)
    plan.pop("trials_per_item", None)
    path = Path(tempfile.mkdtemp()) / "plan.json"
    path.write_text(json.dumps(plan))
    return path


def test_dry_run_plan_runs_all_vendors_and_concurrency_beats_serial(
    tmp_path: Path,
) -> None:
    plan = load_plan(PLAN_FILE)
    set_dir = frozen_set(tmp_path, count=1)
    latency = 0.05
    concurrent = dry_run_plan(
        plan=plan,
        set_dir=set_dir,
        output_dir=tmp_path / "concurrent",
        run_id="plan-dry",
        evaluation_policy_file=POLICY_V2,
        evaluation_price_config_file=PRICES,
        subscription_models_file=MODELS_FILE,
        policy="gold",
        latency_seconds=latency,
        code_commit="test-commit",
        **CONSTRUCTION,
    )
    assert concurrent["complete"] is True
    assert concurrent["planned_trials"] == 48 and concurrent["recorded_trials"] == 48
    assert concurrent["invalid_count"] == 0
    assert set(concurrent["vendors"]) == {
        PROVIDER_GOOGLE_GEMINI,
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        PROVIDER_OPENAI_CODEX,
    }
    assert concurrent["vendors"][PROVIDER_GOOGLE_GEMINI]["concurrency"] == 4
    assert concurrent["vendors"][PROVIDER_ANTHROPIC_CLAUDE_CODE]["concurrency"] == 3
    assert Decimal(concurrent["gemini_usd"]) > 0
    assert set(concurrent["outcomes_by_model"]) == {
        m for v in plan["vendors"].values() for m in v["models"]
    }
    for counts in concurrent["outcomes_by_model"].values():
        assert counts["N1"] == 3 and counts["N5"] == 3
    ledgers = concurrent["dry_run"]["ledgers"]
    assert ledgers[PROVIDER_GOOGLE_GEMINI]["submissions"] == 12
    assert ledgers[PROVIDER_OPENAI_CODEX]["submissions"] == 18
    assert ledgers[PROVIDER_OPENAI_CODEX]["inflight"] == 0
    # A second invocation resumes every trial and makes no new call.
    again = dry_run_plan(
        plan=plan,
        set_dir=set_dir,
        output_dir=tmp_path / "concurrent",
        run_id="plan-dry",
        evaluation_policy_file=POLICY_V2,
        evaluation_price_config_file=PRICES,
        subscription_models_file=MODELS_FILE,
        policy="gold",
        code_commit="test-commit",
        **CONSTRUCTION,
    )
    assert again["recorded_trials"] == 48
    assert all(v["calls_this_invocation"] == 0 for v in again["vendors"].values())
    serial = dry_run_plan(
        plan=plan,
        set_dir=set_dir,
        output_dir=tmp_path / "serial",
        run_id="plan-dry",
        evaluation_policy_file=POLICY_V2,
        evaluation_price_config_file=PRICES,
        subscription_models_file=MODELS_FILE,
        policy="gold",
        latency_seconds=latency,
        serial=True,
        code_commit="test-commit",
        **CONSTRUCTION,
    )
    assert serial["complete"] is True and serial["serial"] is True
    # Serial: 48 calls x latency in a row. Concurrent: three vendors at once,
    # 12/4, 18/3 and 18/3 rounds. The measured ratio must show the effect.
    assert serial["wall_seconds"] >= 48 * latency
    assert concurrent["wall_seconds"] < serial["wall_seconds"] / 2
    scores = score_plan(tmp_path / "concurrent", resamples=20, seed=1)
    assert len(scores["groups"]) == 8
    assert (tmp_path / "concurrent" / "scores" / "main-table.csv").is_file()
    manifest = json.loads(
        (tmp_path / "concurrent" / PLAN_MANIFEST_FILENAME).read_text()
    )
    assert manifest["trials_per_item"] == 48
    with pytest.raises(ValueError, match="different plan manifest"):
        dry_run_plan(
            plan={**plan, "repeats": 1},
            set_dir=set_dir,
            output_dir=tmp_path / "concurrent",
            run_id="plan-dry",
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            subscription_models_file=MODELS_FILE,
            code_commit="test-commit",
            **CONSTRUCTION,
        )


def test_vendor_stops_on_first_error_while_the_other_vendors_finish(
    tmp_path: Path,
) -> None:
    plan = load_plan(PLAN_FILE)
    set_dir = frozen_set(tmp_path, count=1)
    manifest, items = load_eval_set(set_dir)
    codex_trials = plan_trials(
        manifest,
        items,
        models=plan["vendors"][PROVIDER_OPENAI_CODEX]["models"],
        arms=["high"],
        repeats=3,
    )
    broken = codex_trials[0]["trial_id"]
    summary = dry_run_plan(
        plan=plan,
        set_dir=set_dir,
        output_dir=tmp_path / "stop",
        run_id="plan-stop",
        evaluation_policy_file=POLICY_V2,
        evaluation_price_config_file=PRICES,
        subscription_models_file=MODELS_FILE,
        policy="gold",
        overrides={
            broken: {"raise": "exit", "returncode": 3, "stderr": "login expired"}
        },
        concurrency={PROVIDER_OPENAI_CODEX: 1},
        code_commit="test-commit",
        **CONSTRUCTION,
    )
    assert summary["complete"] is False
    codex = summary["vendors"][PROVIDER_OPENAI_CODEX]
    assert codex["complete"] is False and codex["recorded_trials"] == 1
    assert codex["stopped_on"]["state"] == "failed"
    assert summary["vendors"][PROVIDER_GOOGLE_GEMINI]["complete"] is True
    assert summary["vendors"][PROVIDER_ANTHROPIC_CLAUDE_CODE]["complete"] is True
    assert summary["vendors"][PROVIDER_ANTHROPIC_CLAUDE_CODE]["recorded_trials"] == 18
    rows = [
        json.loads(line)
        for line in (tmp_path / "stop" / PROVIDER_OPENAI_CODEX / RESPONSES_FILENAME)
        .read_text()
        .splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["outcome"] == N0 and rows[0]["invalid_reason"] == "failed"
    on_disk = json.loads((tmp_path / "stop" / PLAN_SUMMARY_FILENAME).read_text())
    assert on_disk["complete"] is False


def test_plan_gates_bind_every_vendor_and_the_reviewer_verdict(tmp_path: Path) -> None:
    plan = load_plan(PLAN_FILE)
    set_dir = frozen_set(tmp_path, count=1)
    review = tmp_path / "review.md"
    review.write_text("review\n")
    paths = write_plan_gates(
        plan=plan,
        set_dir=set_dir,
        run_id="gated",
        output_dir=tmp_path / "gates",
        review_record=review,
        review_verdict="pending",
        evaluation_policy_file=POLICY_V2,
        evaluation_price_config_file=PRICES,
        subscription_models_file=MODELS_FILE,
        integrated_code_commit="abc1234",
        binary_versions={
            PROVIDER_ANTHROPIC_CLAUDE_CODE: "9.9.9",
            PROVIDER_OPENAI_CODEX: "codex-cli 9.9.9",
        },
    )
    assert set(paths) == {
        PROVIDER_GOOGLE_GEMINI,
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        PROVIDER_OPENAI_CODEX,
    }
    gemini = json.loads(paths[PROVIDER_GOOGLE_GEMINI].read_text())
    assert gemini["evaluation_enabled"] is False and gemini["repeats_maximum"] == 3
    assert gemini["models"] == ["gemini-3.7-flash", "gemini-3.8-flash"]
    assert gemini["decoding"]["max_output_tokens_by_arm"] == {"high": 8192}
    assert gemini["decoding"]["temperature_by_model"]["gemini-3.7-flash"] == "2.0"
    claude = json.loads(paths[PROVIDER_ANTHROPIC_CLAUDE_CODE].read_text())
    assert claude["provider"] == PROVIDER_ANTHROPIC_CLAUDE_CODE
    assert claude["decoding"]["harness_version"] == "9.9.9"
    assert claude["decoding"]["presets_by_model"]["claude-fable-5-1"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert (
        claude["arms"] == ["high"] and claude["independent_review_verdict"] == "pending"
    )
    codex = json.loads(paths[PROVIDER_OPENAI_CODEX].read_text())
    assert codex["models"] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra"]


class SlowLetterTransport(LetterTransport):
    """A Gemini transport that sleeps and counts the calls in flight."""

    def __init__(self, delay: float) -> None:
        super().__init__("B")
        self.delay = delay
        self.inflight = 0
        self.peak = 0
        self._lock = threading.Lock()

    def post(self, model: str, method: str, body: dict) -> dict:
        if method != "generateContent":
            return super().post(model, method, body)
        with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(self.delay)
            return super().post(model, method, body)
        finally:
            with self._lock:
                self.inflight -= 1


def test_ledger_accepts_concurrent_evaluation_calls_and_keeps_construction_pacing(
    tmp_path: Path,
) -> None:
    transport = SlowLetterTransport(0.15)
    values = evaluation_fixture(
        tmp_path,
        transport=transport,
        repeats=8,
        policy_changes={
            "maximum_concurrent_requests": 4,
            "maximum_requests_per_minute": 100,
        },
    )
    bind(values)
    broker = values["broker"]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(
            pool.map(lambda n: execute(values, trial_id=f"t{n}", repeat=n), range(1, 9))
        )
    wall = time.monotonic() - started
    assert [r["state"] for r in receipts] == ["completed"] * 8
    assert len({r["request_key"] for r in receipts}) == 8
    assert 2 <= transport.peak <= 4
    assert wall < 8 * 0.15
    status = broker.status()
    assert status["evaluation"]["submissions"] == 8
    assert status["evaluation"]["inflight"] == 0
    assert status["halted"] is False
    ledger = json.loads(values["ledger"].read_text())
    assert ledger["inflight"] == 0
    # The evaluation calls left the construction window untouched.
    assert ledger["recent_submission_times_utc"] == []
    assert status["usage"]["generation_requests_in_current_minute"] == 0
    assert status["usage"]["concurrent_generation_requests"] == 0
    assert not list((tmp_path / "receipts" / ".inflight").glob("*.lock"))
    # The eight calls above are the proof that the in-flight lock protects a
    # live sibling: every execute() runs orphan recovery first, and every
    # sibling call is a submitted request of the same run. Without the lock
    # the recovery would settle those siblings as an ambiguous charge and
    # halt the ledger. The run completed and the ledger is not halted.
    key = receipts[0]["request_key"]
    held = broker._hold_inflight(key)
    try:
        assert broker._inflight_held(key) is True
        probe: dict[str, bool] = {}
        watcher = threading.Thread(
            target=lambda: probe.__setitem__("held", broker._inflight_held(key))
        )
        watcher.start()
        watcher.join()
        assert probe["held"] is True
    finally:
        import fcntl

        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
    # A lock file left behind by a crashed holder is not held any more.
    assert broker._inflight_lock_path(key).exists()
    assert broker._inflight_held(key) is False
    broker._inflight_lock_path(key).unlink()
    assert broker._inflight_held(key) is False


def test_evaluation_slots_never_take_a_construction_slot(tmp_path: Path) -> None:
    transport = SlowLetterTransport(0.3)
    values = evaluation_fixture(
        tmp_path,
        transport=transport,
        repeats=4,
        policy_changes={
            "maximum_concurrent_requests": 4,
            "maximum_requests_per_minute": 100,
        },
    )
    bind(values)
    broker = values["broker"]
    # Enable the construction gate for a live_test call on the same ledger.
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
    results: dict = {}

    def evaluate(n: int) -> None:
        results[n] = execute(values, trial_id=f"c{n}", repeat=n)

    threads = [threading.Thread(target=evaluate, args=(n,)) for n in range(1, 4)]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    # Three evaluation calls are in flight; the construction limit is 2.
    from arctic_qa.model_broker import broker_request_key

    body = construction_payload()
    key = broker_request_key(
        model="gemini-3.8-flash",
        run_id="construction-run",
        phase="live_test",
        stage="question_generation",
        paper_id="paper",
        family_id="family",
        source_version_id="source",
        payload=body,
    )
    receipt = broker.execute(
        phase="live_test",
        run_id="construction-run",
        stage="question_generation",
        paper_id="paper",
        family_id="family",
        source_version_id="source",
        request_key=key,
        payload=body,
    )
    for thread in threads:
        thread.join()
    assert receipt["state"] == "completed"
    assert all(r["state"] == "completed" for r in results.values())
    ledger = json.loads(values["ledger"].read_text())
    assert len(ledger["recent_submission_times_utc"]) == 1
    status = broker.status()
    assert status["evaluation"]["submissions"] == 3
    assert Decimal(status["usage"]["dataset_construction_usd"]) > 0
    assert status["usage"]["generation_requests_in_current_minute"] == 1


def test_subscription_ledger_runs_n_calls_in_flight_and_settles_stale_rows(
    tmp_path: Path,
) -> None:
    plan = load_plan(PLAN_FILE)
    set_dir = frozen_set(tmp_path, count=1)
    manifest, items = load_eval_set(set_dir)
    models = plan["vendors"][PROVIDER_OPENAI_CODEX]["models"]
    trials = plan_trials(manifest, items, models=models, arms=["high"], repeats=3)
    config = load_subscription_models(MODELS_FILE)
    decoding = subscription_decoding_record(
        config,
        PROVIDER_OPENAI_CODEX,
        models,
        ["high"],
        binary="/bin/false",
        binary_version="t",
    )
    review = tmp_path / "review.md"
    review.write_text("r\n")
    gate = tmp_path / "gate.json"
    atomic_json(
        gate,
        subscription_gate_record(
            vendor=PROVIDER_OPENAI_CODEX,
            set_dir=set_dir,
            run_id="sub-conc",
            models=models,
            arms=["high"],
            repeats_maximum=3,
            decoding=decoding,
            evaluation_policy_file=POLICY_V2,
            subscription_models_file=MODELS_FILE,
            integrated_code_commit="t",
            review_record=review,
            review_verdict="pass",
        ),
    )
    transport = ScriptedSubscriptionTransport(
        scripted_subscription_answers(
            PROVIDER_OPENAI_CODEX, trials, policy="gold", seed="s"
        ),
        version="t",
        latency_seconds=0.1,
    )
    provider, _ = build_subscription_provider(
        vendor=PROVIDER_OPENAI_CODEX,
        set_dir=set_dir,
        run_id="sub-conc",
        models=models,
        arms=["high"],
        repeats=3,
        ledger_dir=tmp_path / "ledger",
        evaluation_policy_file=POLICY_V2,
        evaluation_gate_file=gate,
        subscription_models_file=MODELS_FILE,
        transport=transport,
        binary="/bin/false",
    )
    from arctic_qa.abstention_providers import EvaluationRequest
    from arctic_qa.abstention_set import evaluation_identity

    peak = {"now": 0, "max": 0}
    guard = threading.Lock()
    original = transport.run

    def counting_run(invocation: dict) -> dict:
        with guard:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        try:
            return original(invocation)
        finally:
            with guard:
                peak["now"] -= 1

    transport.run = counting_run  # type: ignore[method-assign]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(
            pool.map(
                lambda trial: provider.answer(
                    EvaluationRequest(
                        trial=trial,
                        identity=evaluation_identity(items[0]),
                        run_id="sub-conc",
                    )
                ),
                trials[:9],
            )
        )
    wall = time.monotonic() - started
    assert all(r.state == "completed" for r in responses)
    assert 2 <= peak["max"] <= 3
    assert wall < 9 * 0.1
    status = provider.ledger.status()
    assert status["submissions"] == 9 and status["inflight"] == 0
    assert status["maximum_concurrent_requests"] == 3
    assert status["maximum_requests_per_minute"] == 12
    # A row left submitted by a crashed process is settled as interrupted on
    # the next admission, and the ledger refuses another policy on reopen.
    ledger_file = provider.ledger.ledger_file
    ledger = json.loads(ledger_file.read_text())
    ledger["requests"]["deadbeef"] = {
        "trial_id": "gone",
        "item_key": "x/y/z/w",
        "model": models[0],
        "arm": "high",
        "state": "submitted",
        "submitted_at_utc": "2026-01-01T00:00:00Z",
        "submitted_at_epoch": 0.0,
        "gate_sha256": "0" * 64,
        "cost_usd": "0",
    }
    ledger_file.write_text(json.dumps(ledger))
    tenth = provider.answer(
        EvaluationRequest(
            trial=trials[9], identity=evaluation_identity(items[0]), run_id="sub-conc"
        )
    )
    assert tenth.state == "completed"
    after = json.loads(ledger_file.read_text())
    assert after["requests"]["deadbeef"]["state"] == "interrupted"
    assert (tmp_path / "ledger" / "receipts" / "deadbeef.json").is_file()
    with pytest.raises(ValueError, match="another evaluation policy"):
        SubscriptionLedger(
            ledger_dir=tmp_path / "ledger",
            vendor=PROVIDER_OPENAI_CODEX,
            policy_file=ROOT / "config" / "benchmark-evaluation-policy-v1.json",
            gate_file=gate,
            models_file=MODELS_FILE,
        )


def test_cli_dry_run_plan_and_score_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_dir = frozen_set(tmp_path, count=1)
    common = [
        "--json",
        "--test-mode",
        "--data-root",
        str(tmp_path / "data"),
        "abstention-eval",
        "--streaming-budget-policy-file",
        str(CONSTRUCTION["construction_policy_file"]),
        "--price-config-file",
        str(CONSTRUCTION["construction_price_config_file"]),
        "--execution-gate-file",
        str(CONSTRUCTION["construction_gate_file"]),
        "--evaluation-policy-file",
        str(POLICY_V2),
        "--evaluation-price-config-file",
        str(PRICES),
        "--subscription-models-file",
        str(MODELS_FILE),
        "--plan-file",
        str(PLAN_FILE),
    ]
    assert (
        cli_main(
            [
                *common,
                "--action",
                "dry-run-plan",
                "--eval-set-dir",
                str(set_dir),
                "--run-dir",
                str(tmp_path / "run"),
                "--run-id",
                "cli-plan",
                "--scripted-policy",
                "abstain",
                "--code-commit",
                "c",
                "--no-score",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["complete"] is True and summary["recorded_trials"] == 48
    assert (
        cli_main(
            [
                *common,
                "--action",
                "score-plan",
                "--run-dir",
                str(tmp_path / "run"),
                "--bootstrap",
                "10",
            ]
        )
        == 0
    )
    scored = json.loads(capsys.readouterr().out)
    assert len(scored["summary"]) == 8
    assert all(
        group["metrics"]["abstention_rate"] == 1.0
        for group in scored["summary"].values()
    )
    review = tmp_path / "review.md"
    review.write_text("r\n")
    assert (
        cli_main(
            [
                *common,
                "--action",
                "plan-gates",
                "--eval-set-dir",
                str(set_dir),
                "--run-id",
                "cli-plan",
                "--review-record",
                str(review),
                "--output-dir",
                str(tmp_path / "gates"),
                "--code-commit",
                "c",
                "--binary-version",
                "anthropic_claude_code=1.0",
                "--binary-version",
                "openai_codex=2.0",
            ]
        )
        == 0
    )
    gates = json.loads(capsys.readouterr().out)
    assert set(gates["gates"]) == {
        PROVIDER_GOOGLE_GEMINI,
        PROVIDER_ANTHROPIC_CLAUDE_CODE,
        PROVIDER_OPENAI_CODEX,
    }


def test_run_plan_with_scripted_providers_resumes_per_vendor(tmp_path: Path) -> None:
    plan = load_plan(PLAN_FILE)
    set_dir = frozen_set(tmp_path, count=1)
    runs = {
        vendor: VendorRun(
            vendor=vendor,
            provider=ScriptedEvaluationProvider(policy="gold"),
            decoding={"scripted": True},
            models=plan["vendors"][vendor]["models"],
            concurrency=2,
        )
        for vendor in plan["vendors"]
    }
    first = run_plan(
        set_dir=set_dir,
        output_dir=tmp_path / "run",
        run_id="r",
        plan=plan,
        vendor_runs=runs,
        max_calls=2,
    )
    assert first["recorded_trials"] == 6 and first["complete"] is False
    second = run_plan(
        set_dir=set_dir,
        output_dir=tmp_path / "run",
        run_id="r",
        plan=plan,
        vendor_runs=runs,
    )
    assert second["recorded_trials"] == 48 and second["complete"] is True
    assert sum(v["calls_this_invocation"] for v in second["vendors"].values()) == 42


def test_evaluation_admission_never_settles_another_live_run(tmp_path: Path) -> None:
    """A construction call in flight elsewhere must survive an evaluation call.

    A concurrent evaluation request does not hold the exclusive operation
    lock, so another run can be inside a live call with its response already
    durable and its settlement pending. That request must stay submitted.
    """
    values = evaluation_fixture(tmp_path, transport=LetterTransport("A"), repeats=4)
    bind(values)
    broker = values["broker"]
    assert execute(values, trial_id="t1")["state"] == "completed"
    ledger = json.loads(values["ledger"].read_text())
    # Forge one submitted construction request of another run with a durable
    # response, the exact state of a live producer between the two writes.
    key = "f" * 64
    submitted = {
        "request_key": key,
        "request_sha256": "a" * 64,
        "run_id": "another-live-run",
        "phase": "away_production",
        "stage": "question_generation",
        "paper_id": "paper-x",
        "family_id": "family-x",
        "source_version_id": "source-x",
        "model": "gemini-3.8-flash",
        "state": "submitted",
        "input_tokens": 100,
        "reserved_usd": "0.010000",
        "submitted_at_utc": "2026-09-16T00:00:00Z",
        "gate_sha256": "b" * 64,
        "price_config_sha256": ledger["price_config_sha256"],
        "policy_sha256": ledger["policy_sha256"],
        "config_transition_sha256": None,
        "timeout_seconds": 300,
    }
    ledger["requests"][key] = submitted
    ledger["inflight"] = int(ledger["inflight"]) + 1
    ledger["generation_submissions"] = int(ledger["generation_submissions"]) + 1
    ledger["count_requests"] = int(ledger["count_requests"]) + 1
    ledger["reserved_usd"] = str(
        Decimal(ledger["reserved_usd"]) + Decimal(submitted["reserved_usd"])
    )
    ledger["papers"]["family-x"] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "reserved_usd": submitted["reserved_usd"],
        "spent_usd": "0",
        "ambiguous_usd": "0",
        "paper_id": "paper-x",
        "source_version_id": "source-x",
    }
    ledger["stages"]["question_generation"] = {
        "submissions": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "reserved_usd": submitted["reserved_usd"],
        "spent_usd": "0",
        "ambiguous_usd": "0",
    }
    ledger["family_bindings"]["family-x"] = {
        "paper_id": "paper-x",
        "source_version_id": "source-x",
    }
    ledger["paper_bindings"]["paper-x"] = {
        "family_id": "family-x",
        "source_version_id": "source-x",
    }
    ledger_store.write_snapshot(
        values["ledger"], ledger, ledger_store.snapshot_applied_seq(values["ledger"])
    )
    receipts = tmp_path / "receipts"
    write_json(
        receipts / f"{key}.received.json",
        {
            **submitted,
            "state": "response_received",
            "response": {
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}
                ],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 10,
                    "thoughtsTokenCount": 5,
                    "totalTokenCount": 115,
                },
            },
            "live_call_made": True,
            "received_at_utc": "2026-09-16T00:00:01Z",
        },
    )
    assert execute(values, trial_id="t2", repeat=2)["state"] == "completed"
    after = json.loads(values["ledger"].read_text())
    assert after["requests"][key]["state"] == "submitted"
    assert not (receipts / f"{key}.json").is_file()
    # A construction call, which holds the exclusive operation lock, still
    # recovers it.
    broker._recover_orphans(active_run_id=None)
    settled = json.loads(values["ledger"].read_text())
    assert settled["requests"][key]["state"] == "completed"


def test_run_plan_mixes_a_live_gemini_broker_with_a_subscription_vendor(
    tmp_path: Path,
) -> None:
    """The live wiring of both vendor kinds in one plan run.

    `build_vendor_runs` binds the Gemini vendor to the shared broker and the
    subscription vendor to its own ledger, from one gate directory. This is
    the path that `--action run-plan` uses.
    """
    plan = load_plan(PLAN_FILE)
    vendors = [PROVIDER_GOOGLE_GEMINI, PROVIDER_OPENAI_CODEX]
    set_dir = frozen_set(tmp_path, count=1)
    review = tmp_path / "review.md"
    review.write_text("review\n")
    gate_dir = tmp_path / "gates"
    write_plan_gates(
        plan=plan,
        set_dir=set_dir,
        run_id="mixed",
        output_dir=gate_dir,
        review_record=review,
        review_verdict="pass",
        evaluation_policy_file=POLICY_V2,
        evaluation_price_config_file=PRICES,
        subscription_models_file=MODELS_FILE,
        integrated_code_commit="fixture-commit",
        binary_versions={PROVIDER_OPENAI_CODEX: "codex-cli 0.154.0"},
        vendors=vendors,
    )
    manifest, items = load_eval_set(set_dir)
    codex_trials = plan_trials(
        manifest,
        items,
        models=plan["vendors"][PROVIDER_OPENAI_CODEX]["models"],
        arms=["high"],
        repeats=3,
    )
    codex_transport = ScriptedSubscriptionTransport(
        scripted_subscription_answers(
            PROVIDER_OPENAI_CODEX, codex_trials, policy="abstain", seed="mixed"
        ),
        version="codex-cli 0.154.0",
    )
    credential = tmp_path / "private" / "gemini.key"
    credential.parent.mkdir(mode=0o700, exist_ok=True)
    credential.write_text("unused-test-key", encoding="utf-8")
    credential.chmod(0o600)
    construction_gate = tmp_path / "construction-gate.json"
    write_json(
        construction_gate,
        json.loads(CONSTRUCTION["construction_gate_file"].read_text()),
    )
    gemini_transport = LetterTransport("A")

    def broker_factory(gate: Path):
        return SharedGeminiBroker(
            policy_file=CONSTRUCTION["construction_policy_file"],
            price_config_file=CONSTRUCTION["construction_price_config_file"],
            execution_gate_file=construction_gate,
            ledger_file=tmp_path / "shared-ledger.json",
            receipts_dir=tmp_path / "receipts",
            credential_file=credential,
            prior_construction_spend_usd=Decimal("0"),
            transport=gemini_transport,
            evaluation_policy_file=POLICY_V2,
            evaluation_price_config_file=PRICES,
            evaluation_gate_file=gate,
        )

    runs = build_vendor_runs(
        plan=plan,
        set_dir=set_dir,
        run_id="mixed",
        gate_dir=gate_dir,
        evaluation_policy_file=POLICY_V2,
        subscription_models_file=MODELS_FILE,
        broker_factory=broker_factory,
        subscription_ledger_root=tmp_path / "subscription",
        vendors=vendors,
    )
    assert runs[PROVIDER_GOOGLE_GEMINI].concurrency == 4
    assert runs[PROVIDER_OPENAI_CODEX].concurrency == 3
    runs[PROVIDER_OPENAI_CODEX].provider.transport = codex_transport
    summary = run_plan(
        set_dir=set_dir,
        output_dir=tmp_path / "run",
        run_id="mixed",
        plan={**plan, "vendors": {v: plan["vendors"][v] for v in vendors}},
        vendor_runs=runs,
        code_commit="fixture-commit",
        gate_dir=gate_dir,
    )
    assert summary["complete"] is True
    assert summary["recorded_trials"] == 30
    assert Decimal(summary["gemini_usd"]) > 0
    gemini = runs[PROVIDER_GOOGLE_GEMINI].provider.broker.status()
    assert gemini["evaluation"]["submissions"] == 12
    assert gemini["evaluation"]["inflight"] == 0
    assert gemini["halted"] is False
    assert Decimal(gemini["usage"]["dataset_construction_usd"]) == 0
    ledger = json.loads((tmp_path / "shared-ledger.json").read_text())
    assert ledger["recent_submission_times_utc"] == []
    codex_ledger = runs[PROVIDER_OPENAI_CODEX].provider.ledger.status()
    assert codex_ledger["submissions"] == 18 and codex_ledger["spent_usd"] == "0"


def future_resume_utc(days: int = 1) -> str:
    """A resume time still ahead of the real clock.

    The run path reads ``resume_at_utc`` against the wall clock, so a test
    that needs a model held must place its resume in the future. The
    captain's standing pause of 2026-09-16 resumed at 23:00 UTC; five tests
    that had copied that literal expired with it on the same evening.
    """
    when = datetime.now(UTC) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def fable_pause() -> dict:
    """The captain's standing pause of 2026-09-16, as a record of its own.

    A test must not read the committed file for this, because that file is
    operational: it carries whatever an operator or the cost guard paused at
    the time. The committed entry for this model is asserted once, in
    :func:`test_the_pause_record_merges_the_file_and_the_command_line`.
    """
    return {
        "schema": "benchmark-evaluation-model-pause-v1",
        "paused_models": {
            "claude-fable-5-1": {
                "reason": "the captain's daily Claude Fable quota is about 80 percent used",
                "resume_at_utc": "2026-09-16T23:00:00Z",
            }
        },
    }


def held_fable_pause() -> dict:
    """The same record with its resume still ahead of the real clock.

    A test of what a held model does needs the hold to be live when the test
    runs; the record's own resume time passed on the evening it was written.
    """
    record = fable_pause()
    entry = dict(record["paused_models"]["claude-fable-5-1"])
    entry["resume_at_utc"] = future_resume_utc()
    return {**record, "paused_models": {"claude-fable-5-1": entry}}


def test_the_pause_record_merges_the_file_and_the_command_line(tmp_path: Path) -> None:
    """The paused-model interface: the file, the options, and the resume time.

    Captain order 2026-09-16: "pause the fable evaluation because I only have
    ~80% fable usage left today; I will run the fable benchmarking after the
    reset at 6:00pm today".
    """
    # The committed file is operational: an operator or the cost guard adds
    # and removes entries while the run goes on. So this asserts the captain's
    # standing entry only, and the rest of the test owns its own record.
    committed = load_pause(PAUSE_FILE)
    assert (
        committed["paused_models"]["claude-fable-5-1"]["resume_at_utc"]
        == "2026-09-16T23:00:00Z"
    )
    shipped = fable_pause()
    before = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    after = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    assert paused_models(shipped, now=before) == frozenset({"claude-fable-5-1"})
    # The resume time frees the model with no edit and no restart.
    assert paused_models(shipped, now=after) == frozenset()
    # An absent file pauses nothing.
    assert load_pause(tmp_path / "none.json")["paused_models"] == {}
    # The command line wins over the file for the same model.
    options = parse_pause_models(
        ["claude-fable-5-1", "gpt-5.6-sol=2026-09-16T23:00:00Z"]
    )
    merged = merge_pause(shipped, options)
    assert paused_models(merged, now=after) == frozenset({"claude-fable-5-1"})
    assert paused_models(merged, now=before) == frozenset(
        {"claude-fable-5-1", "gpt-5.6-sol"}
    )
    with pytest.raises(ValueError, match="invalid paused model"):
        parse_pause_models(["=2026-09-16T23:00:00Z"])
    with pytest.raises(ValueError, match="invalid UTC timestamp"):
        parse_pause_models(["claude-fable-5-1=not-a-time"])
    bad = tmp_path / "bad.json"
    atomic_json(bad, {"schema": "benchmark-evaluation-model-pause-v1"})
    with pytest.raises(ValueError, match="no paused_models block"):
        load_pause(bad)


def test_a_paused_model_holds_its_trials_and_runs_after_the_resume_time(
    tmp_path: Path,
) -> None:
    """A paused model's trials stay pending: not recorded, not invalid.

    The other seven models of the plan run in the same invocation. After the
    resume time a later invocation runs the six held trials and calls no other
    model again.
    """
    plan = load_plan(PLAN_FILE)
    set_dir = frozen_set(tmp_path, count=1)
    run = tmp_path / "paused"
    common = dict(
        plan=plan,
        set_dir=set_dir,
        output_dir=run,
        run_id="plan-pause",
        evaluation_policy_file=POLICY_V2,
        evaluation_price_config_file=PRICES,
        subscription_models_file=MODELS_FILE,
        policy="gold",
        code_commit="test-commit",
        **CONSTRUCTION,
    )
    held = dry_run_plan(**common, pause=held_fable_pause())
    # Six of the 48 trials belong to the paused model: 2 conditions x 3 repeats.
    assert held["recorded_trials"] == 42 and held["planned_trials"] == 48
    assert held["complete"] is False
    assert held["pending_trials"] == 6
    assert held["paused_models"] == ["claude-fable-5-1"]
    assert "claude-fable-5-1" not in held["outcomes_by_model"]
    assert held["invalid_count"] == 0
    claude = held["vendors"][PROVIDER_ANTHROPIC_CLAUDE_CODE]
    assert claude["paused_models"] == ["claude-fable-5-1"]
    assert claude["recorded_trials"] == 12 and claude["complete"] is False
    # The other two vendors are complete.
    assert held["vendors"][PROVIDER_GOOGLE_GEMINI]["recorded_trials"] == 12
    assert held["vendors"][PROVIDER_OPENAI_CODEX]["recorded_trials"] == 18
    rows = [
        json.loads(line)
        for line in (run / PROVIDER_ANTHROPIC_CLAUDE_CODE / RESPONSES_FILENAME)
        .read_text()
        .splitlines()
        if line.strip()
    ]
    assert all(row["model"] != "claude-fable-5-1" for row in rows)
    # After the resume time the held trials run, and nothing else is re-called.
    resumed = dry_run_plan(
        **common,
        pause={
            "schema": "benchmark-evaluation-model-pause-v1",
            "paused_models": {
                "claude-fable-5-1": {"resume_at_utc": "2026-09-16T00:00:00Z"}
            },
        },
    )
    assert resumed["recorded_trials"] == 48 and resumed["complete"] is True
    assert resumed["paused_models"] == []
    assert (
        resumed["vendors"][PROVIDER_ANTHROPIC_CLAUDE_CODE]["calls_this_invocation"] == 6
    )
    assert resumed["vendors"][PROVIDER_GOOGLE_GEMINI]["calls_this_invocation"] == 0
    assert resumed["outcomes_by_model"]["claude-fable-5-1"]["N1"] == 3


def _manifest(**changes) -> dict:
    base = {
        "schema": "abstention-eval-plan-run-v1",
        "plan_id": "arctic-abstention-plan-8-models-high-3-repeats-v1",
        "run_id": "abstention-stream-r11-aqa-one",
        "eval_set_id": "abstention-eval-set-one",
        "eval_set_dir": "/sets/abstention-eval-set-one",
        "item_count": 1,
        "k": 4,
        "arms": ["high"],
        "repeats": 3,
        "gate_dir": "/gates/aqa-one",
        "vendors": {
            "google_gemini": {"models": ["gemini-3.7-flash", "gemini-3.8-flash"]},
            "openai_codex": {"models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra"]},
        },
        "trials_per_item": 30,
        "code_commit": "a65348d",
        "code_commits": ["a65348d"],
    }
    return {**base, **changes}


def test_a_later_pass_adds_its_vendor_and_its_commit_to_the_plan_manifest() -> None:
    """An item must be finishable by a later pass, on a later snapshot.

    The Claude Code arm was paused for two items on 2026-09-17, so their plan
    manifests bound two vendors and 30 trials. The manifest was immutable
    field by field, so the arm could never come back to them: a three-vendor
    pass stopped the run with "the run directory holds a different plan
    manifest".
    """
    from arctic_qa.abstention_plan import extend_plan_manifest

    existing = _manifest()
    claude = {"models": ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"]}
    later = _manifest(
        vendors={**existing["vendors"], "anthropic_claude_code": claude},
        trials_per_item=48,
        code_commit="ea00336",
        code_commits=["ea00336"],
    )
    merged = extend_plan_manifest(existing, later)

    assert sorted(merged["vendors"]) == [
        "anthropic_claude_code",
        "google_gemini",
        "openai_codex",
    ]
    assert merged["trials_per_item"] == 48
    # The commit that opened the item stays, and the later one is appended.
    assert merged["code_commit"] == "a65348d"
    assert merged["code_commits"] == ["a65348d", "ea00336"]


def test_a_pass_with_fewer_vendors_keeps_the_whole_plan_manifest() -> None:
    """A vendor paused again must not shrink what the item already ran."""
    from arctic_qa.abstention_plan import extend_plan_manifest

    claude = {"models": ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"]}
    whole = _manifest(
        vendors={
            **_manifest()["vendors"],
            "anthropic_claude_code": claude,
        },
        trials_per_item=48,
    )
    merged = extend_plan_manifest(whole, _manifest())
    assert sorted(merged["vendors"]) == [
        "anthropic_claude_code",
        "google_gemini",
        "openai_codex",
    ]
    assert merged["trials_per_item"] == 48


def test_the_plan_manifest_still_refuses_another_item_or_another_model_list() -> None:
    """Only the vendor set and the commit history grow; the identity cannot."""
    from arctic_qa.abstention_plan import extend_plan_manifest

    with pytest.raises(ValueError, match="different plan manifest"):
        extend_plan_manifest(_manifest(), _manifest(run_id="abstention-stream-r11-x"))
    with pytest.raises(ValueError, match="different plan manifest"):
        extend_plan_manifest(_manifest(), _manifest(repeats=1))
    with pytest.raises(ValueError, match="another model list"):
        extend_plan_manifest(
            _manifest(),
            _manifest(
                vendors={
                    **_manifest()["vendors"],
                    "google_gemini": {"models": ["gemini-3.8-flash"]},
                }
            ),
        )


def test_a_later_pass_on_a_later_commit_finishes_the_item(tmp_path: Path) -> None:
    """The whole path: two vendors, then three, on another commit.

    An item evaluated while a vendor was paused kept a plan manifest that
    bound the reduced vendor set, and a run directory was one plan for ever.
    So the paused arm could never come back to that item.
    """
    plan = load_plan(PLAN_FILE)
    set_dir = frozen_set(tmp_path, count=1)

    def runs(vendors: list[str]) -> dict:
        return {
            vendor: VendorRun(
                vendor=vendor,
                provider=ScriptedEvaluationProvider(policy="gold"),
                decoding={"scripted": True},
                models=plan["vendors"][vendor]["models"],
                concurrency=2,
            )
            for vendor in vendors
        }

    without_claude = [
        vendor for vendor in plan["vendors"] if vendor != PROVIDER_ANTHROPIC_CLAUDE_CODE
    ]
    first = run_plan(
        set_dir=set_dir,
        output_dir=tmp_path / "run",
        run_id="r",
        plan=plan,
        vendor_runs=runs(without_claude),
        code_commit="a65348d",
    )
    assert first["recorded_trials"] == 30
    manifest = json.loads(
        (tmp_path / "run" / PLAN_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert manifest["trials_per_item"] == 30
    assert PROVIDER_ANTHROPIC_CLAUDE_CODE not in manifest["vendors"]

    second = run_plan(
        set_dir=set_dir,
        output_dir=tmp_path / "run",
        run_id="r",
        plan=plan,
        vendor_runs=runs(list(plan["vendors"])),
        code_commit="ea00336",
    )
    # Only the eighteen missing trials ran, and the item is whole.
    assert second["recorded_trials"] == 48 and second["complete"] is True
    assert (
        second["vendors"][PROVIDER_ANTHROPIC_CLAUDE_CODE]["calls_this_invocation"] == 18
    )
    for vendor in without_claude:
        assert second["vendors"][vendor]["calls_this_invocation"] == 0

    manifest = json.loads(
        (tmp_path / "run" / PLAN_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert manifest["trials_per_item"] == 48
    assert manifest["code_commit"] == "a65348d"
    assert manifest["code_commits"] == ["a65348d", "ea00336"]

    # Every recorded trial says which commit ran it, and the first pass's own
    # vendor manifest is untouched beside the later pass's record.
    vendor_dir = tmp_path / "run" / PROVIDER_OPENAI_CODEX
    rows = [
        json.loads(line)
        for line in (vendor_dir / RESPONSES_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {row["code_commit"] for row in rows} == {"a65348d"}
    assert (
        json.loads((vendor_dir / "run-manifest.json").read_text(encoding="utf-8"))[
            "code_commit"
        ]
        == "a65348d"
    )
    later = vendor_dir / "run-manifest-ea00336.json"
    assert later.is_file()
    assert json.loads(later.read_text(encoding="utf-8"))["code_commit"] == "ea00336"
    claude_rows = [
        json.loads(line)
        for line in (
            tmp_path / "run" / PROVIDER_ANTHROPIC_CLAUDE_CODE / RESPONSES_FILENAME
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {row["code_commit"] for row in claude_rows} == {"ea00336"}
