"""Resolve and enforce the construction model role assignment.

The r15 holistic audit (section 4.2, section 4.8 item 1) found that one cheap
model wrote every question and then judged it. ``config/roles.v1.json``
described the intended split, and no code read the file. This module reads it,
validates the split, and gives the stream a resolved role map it can record and
assert against the providers it was handed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MODEL_ROLES_CONTRACT_VERSION = "generation-model-roles-v1"
DEFAULT_ROLES_FILE = Path(__file__).parents[2] / "config" / "roles.v1.json"

WRITER_ROLE = "question_writer"
AUTHOR_ROLES = (
    "extractor",
    "question_writer",
    "direct_joint",
    "distractor_writer",
    "correction",
    # Chapter 2 yield audit 4.6 c: one cheap retrieval call that returns the
    # verbatim sentence stating a referent slot. It is a writer-side lookup and
    # judges nothing, so it stays on the author side of the role split.
    "slot_lookup",
)
JUDGE_ROLES = (
    "standalone_verifier",
    "option_verifier",
    "reconstructor",
    "answer_verifier",
    "answer_judge",
)
STRONGEST_JUDGE_ROLES = ("standalone_verifier", "option_verifier")
REQUIRED_ROLES = AUTHOR_ROLES + JUDGE_ROLES


def _default_roles_file() -> Path:
    """Prefer the checked-in contract, then the working-directory copy."""
    if DEFAULT_ROLES_FILE.is_file():
        return DEFAULT_ROLES_FILE
    return Path("config") / "roles.v1.json"


def load_role_contract(path: Path | None = None) -> dict[str, Any]:
    """Read and validate the role contract. Raise before any paid call."""
    roles_file = path or _default_roles_file()
    try:
        value = json.loads(roles_file.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("the model role contract file is missing") from error
    except json.JSONDecodeError as error:
        raise ValueError("the model role contract is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("the model role contract is invalid")
    if value.get("contract_version") != MODEL_ROLES_CONTRACT_VERSION:
        raise ValueError("the model role contract version is not current")
    strength = value.get("model_strength_rank")
    if not isinstance(strength, dict) or not strength:
        raise ValueError("the model role contract declares no model strength rank")
    if any(
        not isinstance(model, str)
        or not model
        or not isinstance(rank, int)
        or isinstance(rank, bool)
        or rank < 1
        for model, rank in strength.items()
    ):
        raise ValueError("the model role strength rank is invalid")
    profiles = value.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("the model role contract declares no profiles")
    for name, profile in profiles.items():
        _validate_profile(str(name), profile, strength)
    return value


def profile_names(contract: dict[str, Any]) -> list[str]:
    return sorted(str(name) for name in contract["profiles"])


def resolve_roles(contract: dict[str, Any], profile: str) -> dict[str, dict[str, str]]:
    """Return the role map of one named profile."""
    profiles = contract["profiles"]
    if profile not in profiles:
        raise ValueError(f"unknown model role profile: {profile}")
    return {
        role: {
            "provider": str(assignment["provider"]),
            "model": str(assignment["model"]),
        }
        for role, assignment in profiles[profile].items()
    }


def role_separation_error(role_models: dict[str, str]) -> str | None:
    """Return why a writer and judge share one model, or None when separated."""
    writer = role_models.get(WRITER_ROLE)
    if not writer:
        return "the writer role has no resolved model"
    shared = sorted(
        role
        for role in JUDGE_ROLES
        if role in role_models and role_models[role] == writer
    )
    if shared:
        return "the writer and these judge roles share one model: " + ", ".join(shared)
    return None


def assert_role_separation(role_models: dict[str, str]) -> None:
    error = role_separation_error(role_models)
    if error is not None:
        raise ValueError(error)


def _validate_profile(name: str, profile: Any, strength: dict[str, Any]) -> None:
    if not isinstance(profile, dict):
        raise ValueError(f"model role profile is invalid: {name}")
    missing = [role for role in REQUIRED_ROLES if role not in profile]
    if missing:
        raise ValueError(
            f"model role profile {name} is missing roles: {', '.join(missing)}"
        )
    unknown = [role for role in profile if role not in REQUIRED_ROLES]
    if unknown:
        raise ValueError(
            f"model role profile {name} declares unknown roles: {', '.join(sorted(unknown))}"
        )
    for role, assignment in profile.items():
        if (
            not isinstance(assignment, dict)
            or not isinstance(assignment.get("provider"), str)
            or not assignment["provider"]
            or not isinstance(assignment.get("model"), str)
            or not assignment["model"]
        ):
            raise ValueError(f"model role assignment is invalid: {name}.{role}")
        if assignment["model"] not in strength:
            raise ValueError(
                f"model role {name}.{role} uses an unranked model: {assignment['model']}"
            )
    writer = profile[WRITER_ROLE]
    # A profile may run inside one provider only when every role does. The
    # chapter 2 launch needs that, because the shared broker meters one
    # provider. The judge must still be a different model from the writer,
    # which is the audit's point (r15 section 4.2 fix 4). A profile that mixes
    # providers must keep every judge outside the writer's family.
    single_provider = len({row["provider"] for row in profile.values()}) == 1
    for role in JUDGE_ROLES:
        judge = profile[role]
        if judge["model"] == writer["model"]:
            raise ValueError(
                f"model role profile {name} judges with the writer model: {role}"
            )
        if not single_provider and judge["provider"] == writer["provider"]:
            raise ValueError(
                f"model role profile {name} judges inside the writer family: {role}"
            )
    best = max(int(strength[profile[role]["model"]]) for role in JUDGE_ROLES)
    for role in STRONGEST_JUDGE_ROLES:
        if int(strength[profile[role]["model"]]) < best:
            raise ValueError(
                f"model role profile {name} does not put the strongest judge model on {role}"
            )
