from __future__ import annotations
from pathlib import Path
import yaml

class ContractError(ValueError): pass

def _read(repo: Path) -> dict:
    path = repo / "PROJECT.yaml"
    if not path.is_file(): raise ContractError("Managed repository has no PROJECT.yaml")
    return yaml.safe_load(path.read_text()) or {}

def load_contract(repo: Path):
    data = _read(repo)
    commands = data.get("commands") or {}; required = (data.get("ci") or {}).get("required") or [x for x in ("preflight", "test") if x in commands]
    if not required: raise ContractError("PROJECT.yaml declares no required CI stages")
    result = []
    for stage in required:
        spec = commands.get(stage)
        if not isinstance(spec, dict) or not spec.get("command"): raise ContractError(f"Required stage missing command: {stage}")
        result.append((stage, str(spec["command"]), str(spec.get("working_directory", ".")), int(spec.get("timeout_seconds", 1800))))
    return result

def load_command(repo: Path, name: str):
    """One named command.<name> entry (build/local_deploy/smoke/
    local_status/...) -- same shape load_contract() already reads for the
    CI-required stages, just not restricted to that list. Returns None
    (never raises) when the command simply isn't declared, so a caller
    can render 'not configured' instead of a hard error -- deployment
    section 8's explicit requirement. Never a route/template constructing
    a shell command itself; PROJECT.yaml is always the one source."""
    try: data = _read(repo)
    except ContractError: return None
    spec = (data.get("commands") or {}).get(name)
    if not isinstance(spec, dict) or not spec.get("command"): return None
    return (str(spec["command"]), str(spec.get("working_directory", ".")), int(spec.get("timeout_seconds", 1800)))

# ProjectFlow's user-facing environment name -> the PROJECT.yaml
# `deployment.<key>` block it actually reads. DEV deliberately maps to
# the existing `local` block/`local_deploy` command family, not a
# `dev` block that doesn't exist anywhere in this workspace's real
# contracts -- `deployment.local` is already the established, audited,
# isolated-sandbox convention (see deployment_service.py's module
# docstring for why). TEST/PRODUCTION are named so they can reuse the
# ALSO-already-existing `deployment.test`/`deployment.production`
# blocks later without inventing new PROJECT.yaml vocabulary.
ENVIRONMENT_KEYS = {"DEV": "local", "TEST": "test", "PRODUCTION": "production"}

def deployment_config(repo: Path, environment: str) -> dict | None:
    """PROJECT.yaml's deployment.<key> block for one ProjectFlow
    environment name (see ENVIRONMENT_KEYS) -- the trusted target/
    environment registry sections 8/11 require. Returns None when the
    environment has no declared block at all, or isn't a known
    ProjectFlow environment (never guessed/defaulted)."""
    key = ENVIRONMENT_KEYS.get(environment.upper())
    if not key: return None
    try: data = _read(repo)
    except ContractError: return None
    block = (data.get("deployment") or {}).get(key)
    return block if isinstance(block, dict) else None
