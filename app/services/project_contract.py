from __future__ import annotations
from pathlib import Path
import yaml

class ContractError(ValueError): pass

def load_contract(repo: Path):
    path = repo / "PROJECT.yaml"
    if not path.is_file(): raise ContractError("Managed repository has no PROJECT.yaml")
    data = yaml.safe_load(path.read_text()) or {}
    commands = data.get("commands") or {}; required = (data.get("ci") or {}).get("required") or [x for x in ("preflight", "test") if x in commands]
    if not required: raise ContractError("PROJECT.yaml declares no required CI stages")
    result = []
    for stage in required:
        spec = commands.get(stage)
        if not isinstance(spec, dict) or not spec.get("command"): raise ContractError(f"Required stage missing command: {stage}")
        result.append((stage, str(spec["command"]), str(spec.get("working_directory", ".")), int(spec.get("timeout_seconds", 1800))))
    return result
