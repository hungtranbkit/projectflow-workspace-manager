"""Synthetic-only regression tests for Claude trust-state isolation."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from app.launchers import AgentLauncher
from app.services.agent_session_manager import AgentSessionManager
from tests.claude_test_config import (
    prepare_claude_test_config, register_project_trust, require_isolated_claude_auth,
)


def _fixture_root(tmp_path):
    root = tmp_path / "pytest-owned"
    root.mkdir()
    return root


def test_restore_removes_only_owned_key_and_preserves_sibling_pytest_entries(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path)
    config_dir = root / ".projectflow-claude-test-config"
    config_dir.mkdir()
    config = config_dir / ".claude.json"
    own_key = str((root / "repo" / "worktree").resolve())
    sibling_key = "/tmp/pytest-of-dell/another-pytest/worktree"
    unrelated_key = "/home/dell/projects/another-session"
    config.write_text(json.dumps({"projects": {
        own_key: {"previous": "owned-value"},
        sibling_key: {"hasTrustDialogAccepted": True},
        unrelated_key: {"hasTrustDialogAccepted": True},
    }}))
    finalizers = []

    trust = register_project_trust(
        root / "repo" / "worktree",
        allowed_root=root,
        monkeypatch=monkeypatch,
        add_finalizer=finalizers.append,
    )
    during = json.loads(config.read_text())["projects"]
    assert during[own_key]["hasTrustDialogAccepted"] is True
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(config_dir)
    assert len(finalizers) == 1

    finalizers[0]()
    after = json.loads(config.read_text())["projects"]
    assert after[own_key] == {"previous": "owned-value"}
    assert after[sibling_key] == {"hasTrustDialogAccepted": True}
    assert after[unrelated_key] == {"hasTrustDialogAccepted": True}
    trust.restore()  # finalizer and explicit cleanup are safely idempotent


def test_finalizer_restores_exact_key_after_launch_setup_failure(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path)
    config_dir = root / ".projectflow-claude-test-config"
    config_dir.mkdir()
    config = config_dir / ".claude.json"
    sibling = "/tmp/pytest-of-dell/sibling/worktree"
    config.write_text(json.dumps({"projects": {sibling: {"keep": True}}}))
    finalizers = []
    trust = register_project_trust(
        root / "repo" / "worktree",
        allowed_root=root,
        monkeypatch=monkeypatch,
        add_finalizer=finalizers.append,
    )
    assert trust.project_key in json.loads(config.read_text())["projects"]

    # A failure after registration but before provider launch is the exact
    # ordering that previously escaped the test's try/finally block.
    with pytest.raises(RuntimeError, match="synthetic launch failure"):
        raise RuntimeError("synthetic launch failure")
    finalizers[0]()
    projects = json.loads(config.read_text())["projects"]
    assert trust.project_key not in projects
    assert projects[sibling] == {"keep": True}


def test_real_agent_session_child_inherits_isolated_config_directory(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path)
    config_dir = root / ".projectflow-claude-test-config"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text('{"projects": {}}\n')
    finalizers = []
    register_project_trust(
        root / "repo" / "worktree",
        allowed_root=root,
        monkeypatch=monkeypatch,
        add_finalizer=finalizers.append,
    )

    class SessionDb:
        def __init__(self):
            self.transcript_tail = ""

        def execute(self, _sql, _args=()):
            if "transcript_tail=?" in _sql:
                self.transcript_tail = _args[0]
            return 1

        def one(self, _sql, _args=()):
            return {"transcript_tail": self.transcript_tail}

    script = "import os; print(os.environ['CLAUDE_CONFIG_DIR'], flush=True)"
    manager = AgentSessionManager(
        SessionDb(),
        launchers={"probe": AgentLauncher("env probe", sys.executable, ("-c", script))},
        which=lambda _name: sys.executable,
    )
    worktree = root / "repo" / "worktree"
    worktree.mkdir(parents=True)
    sid = manager.start(task_id=None, workspace_id=1, agent="probe", worktree_path=worktree)
    session = manager.get(sid)
    deadline = time.monotonic() + 5
    try:
        while not session.closed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert session.closed, "PTY child did not exit within the bounded test wait"
        assert str(config_dir) in (manager.live_tail(sid) or "")
    finally:
        manager.stop(sid)
        finalizers[0]()


def test_rejects_home_escape_symlink_and_nonfixture_project_path(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path)
    finalizers = []
    kwargs = {
        "allowed_root": root,
        "monkeypatch": monkeypatch,
        "add_finalizer": finalizers.append,
    }
    with pytest.raises(ValueError, match="disposable root"):
        register_project_trust(tmp_path / "outside" / "repo", **kwargs)

    link = root / "linked-config"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        register_project_trust(link / "repo", allowed_root=root, monkeypatch=monkeypatch,
                               add_finalizer=finalizers.append)

    with pytest.raises(ValueError, match="user home"):
        from tests.claude_test_config import _validate_fixture_root
        _validate_fixture_root(Path.home())


def test_rejects_symlinked_config_directory_and_config_file(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path)
    finalizers = []
    target = root / "target-config"
    target.mkdir()
    config_link = root / ".projectflow-claude-test-config"
    config_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        register_project_trust(root / "repo" / "worktree", allowed_root=root,
                               monkeypatch=monkeypatch, add_finalizer=finalizers.append)
    assert list(target.iterdir()) == []

    config_link.unlink()
    config_link.mkdir()
    (config_link / ".projectflow-test-owned").write_text(
        "projectflow pytest-owned Claude config v1\n")
    outside_config = root / "outside.json"
    outside_config.write_text('{"preserve": true}\n')
    (config_link / ".claude.json").symlink_to(outside_config)
    with pytest.raises(ValueError, match="must not be a symlink"):
        register_project_trust(root / "repo" / "worktree", allowed_root=root,
                               monkeypatch=monkeypatch, add_finalizer=finalizers.append)
    assert outside_config.read_text() == '{"preserve": true}\n'


def test_credential_variable_does_not_bypass_isolated_auth_probe(tmp_path, monkeypatch):
    root = _fixture_root(tmp_path)
    config_dir = prepare_claude_test_config(root, monkeypatch=monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-noncredential-marker")
    monkeypatch.setattr("tests.claude_test_config.shutil.which", lambda _name: None)
    with pytest.raises(RuntimeError, match="no provider was invoked"):
        require_isolated_claude_auth(config_dir)
