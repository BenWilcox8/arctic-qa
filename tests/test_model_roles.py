from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arctic_qa import streaming as streaming_module
from arctic_qa.model_roles import (
    JUDGE_ROLES,
    MODEL_ROLES_CONTRACT_VERSION,
    STRONGEST_JUDGE_ROLES,
    WRITER_ROLE,
    load_role_contract,
    resolve_roles,
    role_separation_error,
)


REPO = Path(__file__).parents[1]
ROLES_FILE = REPO / "config" / "roles.v1.json"


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _provider(
    name: str, model: str, *, role_models: dict | None = None, phase="offline"
):
    namespace = SimpleNamespace(name=name, model=model, phase=phase)
    if role_models is not None:
        namespace.model_for_role = lambda role: role_models.get(role, model)
    return namespace


def test_shipped_role_contract_separates_the_writer_from_every_judge() -> None:
    contract = load_role_contract(ROLES_FILE)

    assert contract["contract_version"] == MODEL_ROLES_CONTRACT_VERSION
    for profile in contract["profiles"]:
        roles = resolve_roles(contract, profile)
        writer = roles[WRITER_ROLE]
        strength = contract["model_strength_rank"]
        single_provider = len({row["provider"] for row in roles.values()}) == 1
        for role in JUDGE_ROLES:
            assert roles[role]["model"] != writer["model"]
            # A single-provider profile separates by model; a mixed one by family.
            assert single_provider or roles[role]["provider"] != writer["provider"]
        best = max(strength[roles[role]["model"]] for role in JUDGE_ROLES)
        for role in STRONGEST_JUDGE_ROLES:
            assert strength[roles[role]["model"]] == best


def test_role_contract_refuses_a_judge_on_the_writer_model(tmp_path: Path) -> None:
    contract = json.loads(ROLES_FILE.read_text(encoding="utf-8"))
    profile = contract["profiles"]["strongest"]
    profile["standalone_verifier"] = dict(profile[WRITER_ROLE])
    path = _write(tmp_path / "roles.json", contract)

    with pytest.raises(ValueError, match="judges with the writer model"):
        load_role_contract(path)


def test_role_contract_requires_the_strongest_model_on_the_source_blind_gates(
    tmp_path: Path,
) -> None:
    contract = json.loads(ROLES_FILE.read_text(encoding="utf-8"))
    profile = contract["profiles"]["strongest"]
    profile["standalone_verifier"] = {
        "provider": "gemini",
        "model": "gemini-3.8-flash",
    }
    path = _write(tmp_path / "roles.json", contract)

    with pytest.raises(ValueError, match="strongest judge model"):
        load_role_contract(path)


def test_role_contract_rejects_a_missing_role(tmp_path: Path) -> None:
    contract = json.loads(ROLES_FILE.read_text(encoding="utf-8"))
    del contract["profiles"]["cost_aware"]["option_verifier"]
    path = _write(tmp_path / "roles.json", contract)

    with pytest.raises(ValueError, match="missing roles"):
        load_role_contract(path)


def test_role_separation_error_names_every_shared_judge() -> None:
    shared = {WRITER_ROLE: "one-model", **{role: "one-model" for role in JUDGE_ROLES}}

    error = role_separation_error(shared)

    assert error is not None
    for role in JUDGE_ROLES:
        assert role in error
    assert (
        role_separation_error({WRITER_ROLE: "writer", "answer_verifier": "judge"})
        is None
    )


def test_a_bound_profile_fails_when_one_model_holds_every_role() -> None:
    provider = _provider("gemini", "gemini-3.8-flash")

    with pytest.raises(ValueError, match="do not serve the configured model roles"):
        streaming_module._resolve_model_roles(
            author=provider,
            verifier=provider,
            roles_file=ROLES_FILE,
            role_profile="strongest",
        )


def test_a_bound_profile_accepts_providers_that_serve_the_configured_roles() -> None:
    contract = load_role_contract(ROLES_FILE)
    roles = resolve_roles(contract, "strongest")
    author = _provider("claude", roles[WRITER_ROLE]["model"])
    verifier = _provider(
        "gemini",
        roles["answer_verifier"]["model"],
        role_models={role: roles[role]["model"] for role in JUDGE_ROLES},
    )

    resolved = streaming_module._resolve_model_roles(
        author=author,
        verifier=verifier,
        roles_file=ROLES_FILE,
        role_profile="strongest",
    )

    assert resolved["enforced"] is True
    assert resolved["profile"] == "strongest"
    assert resolved["same_model_roles"] is False
    assert resolved["effective_role_models"][WRITER_ROLE] == roles[WRITER_ROLE]["model"]


def test_an_unbound_offline_run_discloses_that_one_model_holds_every_role() -> None:
    provider = _provider("fake", "fake-gemini-3.8-flash")

    resolved = streaming_module._resolve_model_roles(
        author=provider,
        verifier=provider,
        roles_file=ROLES_FILE,
        role_profile=None,
    )

    assert resolved["enforced"] is False
    assert resolved["same_model_roles"] is True
    assert resolved["resolved_roles"] is None


def test_a_production_run_must_name_a_role_profile() -> None:
    provider = _provider("gemini", "gemini-3.8-flash", phase="away_production")

    with pytest.raises(ValueError, match="must name a model role profile"):
        streaming_module._resolve_model_roles(
            author=provider,
            verifier=provider,
            roles_file=ROLES_FILE,
            role_profile=None,
        )
