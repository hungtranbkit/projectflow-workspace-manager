from __future__ import annotations
from pathlib import Path
import yaml


class SandboxContractError(ValueError): pass


VALID_PROFILES = {"NONE", "SHARED_DEV", "BACKEND", "FULL", "HARDWARE", "CUSTOM", "AUTO"}


def load_sandbox_contract(repo: Path) -> dict | None:
    """Read PROJECT.yaml's optional `sandbox:` block. Returns None if the
    project declares no sandbox contract at all (a project without one is
    never treated as an error -- sandboxing is opt-in, per
    docs section 10)."""
    path = repo / "PROJECT.yaml"
    if not path.is_file(): return None
    data = yaml.safe_load(path.read_text()) or {}
    block = data.get("sandbox")
    if not block: return None
    if not isinstance(block, dict): raise SandboxContractError("sandbox: must be a mapping")
    if "compose_file" not in block: raise SandboxContractError("sandbox.compose_file is required")
    profiles = block.get("profiles") or {}
    if not isinstance(profiles, dict): raise SandboxContractError("sandbox.profiles must be a mapping")
    return block


def resolve_profile(contract: dict, explicit: str | None, task_default: str | None) -> str:
    """Priority: explicit selection > task-level default > project default > NONE.
    AUTO resolves deterministically to the project's declared default_profile
    (or NONE if none declared) -- V1 never guesses via AI."""
    for candidate in (explicit, task_default, contract.get("default_profile") if contract else None):
        if candidate:
            profile = candidate.upper() if candidate.upper() in VALID_PROFILES else candidate
            if profile == "AUTO":
                return (contract or {}).get("default_profile", "NONE") or "NONE"
            return profile
    return "NONE"


def profile_services(contract: dict, profile: str) -> list[str]:
    """Services for a named profile, following `extends:` one level (V1 does
    not support multi-level extends chains -- keep profile definitions flat)."""
    profiles = contract.get("profiles") or {}
    spec = profiles.get(profile.lower()) or profiles.get(profile)
    if not spec: raise SandboxContractError(f"sandbox profile not declared: {profile}")
    services = list(spec.get("services") or [])
    extends = spec.get("extends")
    if extends:
        base = profiles.get(extends.lower()) or profiles.get(extends)
        if not base: raise SandboxContractError(f"sandbox profile extends unknown profile: {extends}")
        services = list(dict.fromkeys([*base.get("services", []), *services]))
    return services


def profile_expose_lan(contract: dict, profile: str) -> list[str]:
    profiles = contract.get("profiles") or {}
    spec = profiles.get(profile.lower()) or profiles.get(profile) or {}
    return list(spec.get("expose_lan") or [])


def port_specs(contract: dict) -> dict[str, dict]:
    """{service: {"container": int, "range": (lo, hi)}}"""
    result = {}
    for service, spec in (contract.get("ports") or {}).items():
        if "container" not in spec: raise SandboxContractError(f"sandbox.ports.{service}.container is required")
        rng = spec.get("range")
        if rng:
            lo, hi = str(rng).split("-")
            lo, hi = int(lo), int(hi)
        else:
            lo, hi = 20000, 29999
        result[service] = {"container": int(spec["container"]), "range": (lo, hi)}
    return result


def health_spec(contract: dict, service: str) -> dict | None:
    return (contract.get("health") or {}).get(service)


def seed_default(contract: dict) -> str:
    return (contract.get("seed") or {}).get("default", "EMPTY")


def output_specs(contract: dict) -> dict[str, dict]:
    return contract.get("outputs") or {}


def hardware_build_command(contract: dict) -> str | None:
    return ((contract.get("hardware") or {}).get("build_command"))
