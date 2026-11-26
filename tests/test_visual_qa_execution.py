"""Exercise the real TestRunner stage dispatch and TaskDecisionService gate.

Only the command executor and DB boundary are in-memory; TestRunner,
project-contract loading, marker parsing and TaskDecisionService are production
code. This keeps the test provider-free while proving the actual stage flow.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from app.services.task_decision_service import TaskDecisionService
from app.services.git_workspace import GitWorkspaceService
from app.services.test_runner import TestRunner as _ProjectFlowTestRunner
from tests.test_visual_qa_evidence import png


def _project(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "docs/reference.png").write_bytes(png(430, 932))
    (root / "PROJECT.yaml").write_text(
        "commands:\n"
        "  test: {command: 'fixture-test'}\n"
        "  visual_qa: {command: 'fixture-visual-qa'}\n"
        "visual_qa:\n"
        "  enabled: true\n"
        "  reference: docs/reference.png\n"
        "  route: http://127.0.0.1:4173/fixture\n"
        "  viewports: [{width: 430, height: 932}]\n"
        "  threshold: 0.08\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Visual QA Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "visual-qa-test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "add", "PROJECT.yaml", "docs/reference.png"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "commit visual QA fixture"], cwd=root, check=True, capture_output=True)


class _DB:
    def __init__(self, prompt: str):
        self.task = {"implementation_prompt": prompt}
        self.rows: list[dict] = []
        self.events: list[tuple] = []
        self.finished = threading.Event()

    def one(self, sql, _params):
        if "agent_workspaces" in sql or "FROM tasks" in sql:
            return self.task
        return None

    def all(self, _sql, _params):
        return list(self.rows)

    def execute(self, sql, params):
        if sql.startswith("INSERT INTO test_runs"):
            run_id = len(self.rows) + 1
            self.rows.append({
                "id": run_id,
                "stage": params[3],
                "status": params[4],
                "tested_commit": params[5],
            })
            return run_id
        if sql.startswith("UPDATE test_runs SET status=?,finished_at=?,exit_code=?"):
            status, _finished, _exit, _stdout, _stderr, run_id = params
            self.rows[run_id - 1]["status"] = status
            self.rows[run_id - 1]["exit_code"] = _exit
            if all(row["status"] in {"PASS", "FAIL", "TIMEOUT", "SKIPPED"} for row in self.rows):
                self.finished.set()
            return 1
        if sql.startswith("UPDATE test_runs SET status='RUNNING'"):
            _started, run_id = params
            self.rows[run_id - 1]["status"] = "RUNNING"
            return 1
        return 1

    def event(self, kind, entity_id, name, detail=None):
        self.events.append((kind, entity_id, name, detail))


class _Git(GitWorkspaceService):
    def __init__(self, root: Path):
        super().__init__(root)


class _Runner:
    def __init__(self, fail_stage: str | None = None):
        self.commands: list[str] = []
        self.fail_stage = fail_stage

    def run(self, command, _path, _working, _timeout, env=None):
        self.commands.append(command)
        failed = command == self.fail_stage
        return subprocess.CompletedProcess(command, 1 if failed else 0, "fixture stdout", "fixture stderr" if failed else "")


def _run_and_wait(db, runner, root: Path):
    test_runner = _ProjectFlowTestRunner(db, _Git(root), runner=runner)
    run_ids = test_runner.start("agent", 11, root)
    assert db.finished.wait(3), "TestRunner background stage dispatch did not finish"
    return run_ids


def _decision(db, root: Path):
    git = _Git(root)
    service = TaskDecisionService(db, git)
    return service.builder_tests_status({
        "id": 11,
        "task_id": 7,
        "head": git.head(root),
        "worktree_path": str(root),
    })


def test_opted_in_ui_task_exit_zero_without_coordinator_evidence_fails_closed(tmp_path):
    _project(tmp_path)
    db = _DB("Implement this UI screen\nVISUAL_QA_REQUIRED: true")
    runner = _Runner()

    run_ids = _run_and_wait(db, runner, tmp_path)

    assert run_ids == [1, 2]
    assert runner.commands == ["fixture-test", "fixture-visual-qa"]
    assert [(row["stage"], row["status"]) for row in db.rows] == [
        ("test", "PASS"), ("visual_qa", "FAIL")
    ]
    assert _decision(db, tmp_path) == "FAIL"


def test_visual_failure_fails_opted_in_task_decision(tmp_path):
    _project(tmp_path)
    db = _DB("UI repair\nVISUAL_QA_REQUIRED: true")
    runner = _Runner(fail_stage="fixture-visual-qa")

    _run_and_wait(db, runner, tmp_path)

    assert [(row["stage"], row["status"], row["exit_code"]) for row in db.rows] == [
        ("test", "PASS", 0), ("visual_qa", "FAIL", 1)
    ]
    assert _decision(db, tmp_path) == "FAIL"


def test_backend_task_without_marker_dispatches_only_existing_test_stage(tmp_path):
    _project(tmp_path)
    db = _DB("Backend API migration; no UI")
    runner = _Runner()

    run_ids = _run_and_wait(db, runner, tmp_path)

    assert run_ids == [1]
    assert runner.commands == ["fixture-test"]
    assert [(row["stage"], row["status"]) for row in db.rows] == [("test", "PASS")]
    assert _decision(db, tmp_path) == "PASS"
