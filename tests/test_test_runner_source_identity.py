"""Test runs must not be PASS evidence for a different or dirty source tree."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from app.db import Database
from app.services.git_workspace import GitWorkspaceService
from app.services.task_decision_service import TaskDecisionService
from app.services.test_runner import TestRunner as ProjectFlowTestRunner


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, text=True, capture_output=True).stdout.strip()


def make_repo(tmp_path: Path):
    root = tmp_path / "workspace"
    repo = root / "demo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Source identity test")
    (repo / "source.txt").write_text("A\n", encoding="utf-8")
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: source-identity}\nsource: {root: .}\n"
        "commands:\n  test: {command: 'cat source.txt', working_directory: ., timeout_seconds: 20}\n"
        "ci: {required: [test]}\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "source A")
    return root, repo, git(repo, "rev-parse", "HEAD")


class RealSynchronizedShellRunner:
    """Wait at the process boundary, then execute the configured command for real."""
    def __init__(self, *, block=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = block
        self.calls = []

    def run(self, command, worktree_root, working_dir, timeout):
        self.calls.append((command, worktree_root, working_dir, timeout))
        self.entered.set()
        if self.block:
            assert self.release.wait(5), "test did not release the shell process"
        return subprocess.run(
            command,
            cwd=(worktree_root / (working_dir or ".")).resolve(),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )


class DeferredThread:
    """Queue TestRunner work without starting it until the test advances Git."""
    latest = None

    def __init__(self, *, target, args, daemon):
        self.target, self.args, self.daemon = target, args, daemon
        type(self).latest = self

    def start(self):
        return None

    def run(self):
        return self.target(*self.args)


def commit_source(repo: Path, text: str) -> str:
    (repo / "source.txt").write_text(text, encoding="utf-8")
    git(repo, "add", "source.txt")
    git(repo, "commit", "-m", f"source {text.strip()}")
    return git(repo, "rev-parse", "HEAD")


def runner_for(tmp_path: Path, repo: Path, shell_runner):
    db = Database(tmp_path / "projectflow.sqlite")
    db.init()
    git_service = GitWorkspaceService(tmp_path / "workspace", worktree_root=tmp_path / "workspace" / ".worktrees")
    (tmp_path / "workspace" / ".worktrees").mkdir(exist_ok=True)
    return db, git_service, ProjectFlowTestRunner(db, git_service, runner=shell_runner)


def wait_terminal(db: Database, run_id: int):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = db.one("SELECT * FROM test_runs WHERE id=?", (run_id,))
        if row["status"] not in {"QUEUED", "RUNNING"}:
            return row
        time.sleep(0.01)
    raise AssertionError("test run did not finish")


def decision(db, git_service, repo, workspace_id=7):
    return TaskDecisionService(db, git_service).builder_tests_status({
        "id": workspace_id,
        "head": git_service.head(repo),
        "worktree_path": str(repo),
    })


def test_commit_change_after_queue_is_rejected_before_shell_command(tmp_path, monkeypatch):
    from app.services import test_runner as test_runner_module

    root, repo, head_a = make_repo(tmp_path)
    db, git_service, service = runner_for(tmp_path, repo, RealSynchronizedShellRunner())
    monkeypatch.setattr(test_runner_module.threading, "Thread", DeferredThread)

    [run_id] = service.start("agent", 7, repo)
    head_b = commit_source(repo, "B\n")
    assert head_b != head_a
    DeferredThread.latest.run()

    row = db.one("SELECT * FROM test_runs WHERE id=?", (run_id,))
    assert row["tested_commit"] == head_a
    assert row["status"] == "FAIL"
    assert "SOURCE_CHANGED_BEFORE_TEST" in row["stderr_tail"]
    assert service.runner.calls == []
    assert decision(db, git_service, repo) == "NOT_RUN"


def test_commit_change_during_real_shell_command_cannot_record_pass_for_queued_sha(tmp_path):
    root, repo, head_a = make_repo(tmp_path)
    shell = RealSynchronizedShellRunner(block=True)
    db, git_service, service = runner_for(tmp_path, repo, shell)

    [run_id] = service.start("agent", 7, repo)
    assert shell.entered.wait(3), "runner did not reach the real shell boundary"
    head_b = commit_source(repo, "B\n")
    shell.release.set()

    row = wait_terminal(db, run_id)
    assert row["tested_commit"] == head_a
    assert row["status"] == "FAIL"
    assert row["exit_code"] == 0
    assert row["stdout_tail"].strip() == "B"
    assert "SOURCE_CHANGED_DURING_TEST" in row["stderr_tail"]
    assert decision(db, git_service, repo) == "NOT_RUN"
    assert head_b == git_service.head(repo)


def test_dirty_tracked_source_can_run_but_is_not_commit_bound(tmp_path):
    root, repo, head = make_repo(tmp_path)
    (repo / "source.txt").write_text("dirty\n", encoding="utf-8")
    shell = RealSynchronizedShellRunner()
    db, git_service, service = runner_for(tmp_path, repo, shell)

    [run_id] = service.start("agent", 7, repo)
    row = wait_terminal(db, run_id)

    assert row["status"] == "PASS"
    assert row["tested_commit"] is None
    assert row["stdout_tail"].strip() == "dirty"
    assert "SOURCE_NOT_COMMIT_BOUND" in row["stderr_tail"]
    assert git_service.head(repo) == head
    assert decision(db, git_service, repo) == "NOT_RUN"


def test_tracked_source_drift_during_dirty_run_fails_closed(tmp_path):
    root, repo, head = make_repo(tmp_path)
    (repo / "source.txt").write_text("dirty-A\n", encoding="utf-8")
    shell = RealSynchronizedShellRunner(block=True)
    db, git_service, service = runner_for(tmp_path, repo, shell)

    [run_id] = service.start("agent", 7, repo)
    assert shell.entered.wait(3)
    (repo / "source.txt").write_text("dirty-B\n", encoding="utf-8")
    shell.release.set()

    row = wait_terminal(db, run_id)
    assert row["tested_commit"] is None
    assert row["status"] == "FAIL"
    assert row["exit_code"] == 0
    assert row["stdout_tail"].strip() == "dirty-B"
    assert "SOURCE_CHANGED_DURING_TEST" in row["stderr_tail"]
    assert decision(db, git_service, repo) == "NOT_RUN"


def test_new_untracked_source_during_clean_run_cannot_inherit_commit_sha(tmp_path):
    root, repo, head = make_repo(tmp_path)
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: source-identity}\nsource: {root: .}\n"
        "commands:\n  test: {command: 'cat future-source.txt', working_directory: ., timeout_seconds: 20}\n"
        "ci: {required: [test]}\n",
        encoding="utf-8",
    )
    git(repo, "add", "PROJECT.yaml")
    git(repo, "commit", "-m", "read future source")
    head = git(repo, "rev-parse", "HEAD")
    shell = RealSynchronizedShellRunner(block=True)
    db, git_service, service = runner_for(tmp_path, repo, shell)

    [run_id] = service.start("agent", 7, repo)
    assert shell.entered.wait(3)
    (repo / "future-source.txt").write_text("new untracked source\n", encoding="utf-8")
    shell.release.set()

    row = wait_terminal(db, run_id)
    assert row["tested_commit"] == head
    assert row["status"] == "FAIL"
    assert row["exit_code"] == 0
    assert row["stdout_tail"].strip() == "new untracked source"
    assert "SOURCE_CHANGED_DURING_TEST" in row["stderr_tail"]
    assert decision(db, git_service, repo) == "NOT_RUN"


def test_clean_pass_becomes_not_run_after_head_advances(tmp_path):
    root, repo, head_a = make_repo(tmp_path)
    shell = RealSynchronizedShellRunner()
    db, git_service, service = runner_for(tmp_path, repo, shell)

    [run_id] = service.start("agent", 7, repo)
    row = wait_terminal(db, run_id)
    assert row["status"] == "PASS"
    assert row["tested_commit"] == head_a
    assert decision(db, git_service, repo) == "PASS"

    head_b = commit_source(repo, "B\n")
    assert head_b != head_a
    assert decision(db, git_service, repo) == "NOT_RUN"


def test_clean_pass_becomes_not_run_when_same_head_becomes_dirty(tmp_path):
    root, repo, head = make_repo(tmp_path)
    shell = RealSynchronizedShellRunner()
    db, git_service, service = runner_for(tmp_path, repo, shell)

    [run_id] = service.start("agent", 7, repo)
    row = wait_terminal(db, run_id)
    assert row["status"] == "PASS"
    assert row["tested_commit"] == head
    assert decision(db, git_service, repo) == "PASS"

    (repo / "source.txt").write_text("modified after test\n", encoding="utf-8")
    assert git_service.head(repo) == head
    assert decision(db, git_service, repo) == "NOT_RUN"


def test_integration_pass_becomes_not_run_when_source_drifts(tmp_path):
    root, repo, head = make_repo(tmp_path)
    db, git_service, _ = runner_for(tmp_path, repo, RealSynchronizedShellRunner())

    class GateDB:
        def one(self, _sql, _params):
            return {"implementation_prompt": "backend integration"}

        def all(self, _sql, _params):
            return [{"stage": "test", "status": "PASS", "tested_commit": head, "stdout_tail": ""}]

    task_decision = TaskDecisionService(GateDB(), git_service)
    integration = {"id": 4, "worktree_path": str(repo)}
    assert task_decision.integration_gate_status(integration, 9)["tests_status"] == "PASS"

    (repo / "source.txt").write_text("dirty integration source\n", encoding="utf-8")
    assert git_service.head(repo) == head
    stale = task_decision.integration_gate_status(integration, 9)
    assert stale["tests_status"] == "NOT_RUN"
    assert not stale["tests_pass"]


def test_failed_stage_still_skips_later_stages(tmp_path):
    root, repo, _ = make_repo(tmp_path)
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: source-identity}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'exit 7', working_directory: ., timeout_seconds: 20}\n"
        "  test: {command: 'printf should-not-run', working_directory: ., timeout_seconds: 20}\n"
        "ci: {required: [preflight, test]}\n",
        encoding="utf-8",
    )
    git(repo, "add", "PROJECT.yaml")
    git(repo, "commit", "-m", "two test stages")
    shell = RealSynchronizedShellRunner()
    db, _git_service, service = runner_for(tmp_path, repo, shell)

    [first_id, second_id] = service.start("agent", 7, repo)
    first = wait_terminal(db, first_id)
    second = wait_terminal(db, second_id)

    assert first["status"] == "FAIL"
    assert first["exit_code"] == 7
    assert second["status"] == "SKIPPED"
    assert [call[0] for call in shell.calls] == ["exit 7"]
