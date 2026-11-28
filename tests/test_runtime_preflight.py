from pathlib import Path

from app.services.runtime_preflight import resolve_executable, runtime_path


def test_explicit_runtime_path_is_used_when_process_path_is_missing(tmp_path, monkeypatch):
    tool = tmp_path / "claude"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    env = {"PROJECTFLOW_RUNTIME_PATH": str(tmp_path), "PATH": "/missing"}
    assert resolve_executable("claude", environ=env) == str(tool)
    assert runtime_path(env).split(":")[0] == str(tmp_path)


def test_unsafe_or_missing_executable_fails_closed(tmp_path):
    assert resolve_executable(str(tmp_path / "missing"), environ={"PATH": "/missing"}) is None
