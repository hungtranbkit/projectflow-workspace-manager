from pathlib import Path
import os
import subprocess

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

def test_pinned_primary_then_fallback_is_deterministic(tmp_path):
    fallback = tmp_path / "codex"
    fallback.write_text("#!/bin/sh\nexit 0\n")
    fallback.chmod(0o755)
    env = {"PROJECTFLOW_RUNTIME_PRIMARY": str(tmp_path / "missing-codex"),
           "PROJECTFLOW_RUNTIME_FALLBACK": str(fallback), "PATH": "/missing"}
    assert resolve_executable("codex", environ=env) == str(fallback)


def test_script_accepts_missing_primary_when_pinned_fallback_is_valid(tmp_path):
    fallback = tmp_path / "codex"
    fallback.write_text("#!/bin/sh\nexit 0\n")
    fallback.chmod(0o755)
    env = os.environ.copy()
    env.update({"PROJECTFLOW_RUNTIME_PRIMARY": str(tmp_path / "missing"),
                "PROJECTFLOW_RUNTIME_FALLBACK": str(fallback)})
    result = subprocess.run(["bash", "scripts/preflight.sh"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_script_rejects_when_both_pinned_runtimes_are_unavailable(tmp_path):
    env = os.environ.copy()
    env.update({"PROJECTFLOW_RUNTIME_PRIMARY": str(tmp_path / "missing-a"),
                "PROJECTFLOW_RUNTIME_FALLBACK": str(tmp_path / "missing-b")})
    result = subprocess.run(["bash", "scripts/preflight.sh"], env=env,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "neither pinned runtime executable" in result.stderr


def test_script_rejects_directory_as_runtime_pin(tmp_path):
    env = os.environ.copy()
    env.update(PROJECTFLOW_RUNTIME_PRIMARY=str(tmp_path), PROJECTFLOW_RUNTIME_FALLBACK="")
    result = subprocess.run(["bash", "scripts/preflight.sh"], env=env,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "runtime executable pin" in result.stderr
