"""Deterministic executable discovery for managed node launches."""
from __future__ import annotations
import os
import shutil
from pathlib import Path
from typing import Callable, Mapping

def resolve_executable(name: str, *, which: Callable[..., str | None] = shutil.which,
                       environ: Mapping[str, str] | None = None) -> str | None:
    value = str(name or "").strip()
    if not value:
        return None
    if os.path.sep in value:
        path = Path(value).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    env = environ if environ is not None else os.environ
    for pinned_key in ("PROJECTFLOW_RUNTIME_PRIMARY", "PROJECTFLOW_RUNTIME_FALLBACK"):
        pinned = env.get(pinned_key, "").strip()
        if pinned:
            path = Path(pinned).expanduser()
            if path.name == value and path.is_file() and os.access(path, os.X_OK):
                return str(path)
    explicit = env.get("PROJECTFLOW_RUNTIME_PATH", "")
    if explicit:
        path = shutil.which(value, path=explicit)
        if path:
            return path
    try:
        return which(value, path=env.get("PATH"))
    except TypeError:  # injectable test/node resolver with the old one-arg contract
        return which(value)

def runtime_path(environ: Mapping[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    return os.pathsep.join(p for p in (env.get("PROJECTFLOW_RUNTIME_PATH", ""), env.get("PATH", "")) if p)
