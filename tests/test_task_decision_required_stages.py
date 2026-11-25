"""TaskDecisionService must require the exact stages in PROJECT.yaml."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.db import Database
from app.services.git_workspace import GitWorkspaceService
from app.services.task_decision_service import TaskDecisionService, fingerprint_of


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()


def make_fixture(tmp_path: Path, required=("test", "build"), *, visual=False, contract=True):
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "pf-03c@example.invalid")
    git(repo, "config", "user.name", "PF-03c test")
    if contract:
        commands = "".join(f"  {stage}: {{command: 'true'}}\n" for stage in ("test", "build", "visual_qa", "extra"))
        (repo / "PROJECT.yaml").write_text(
            "schema_version: 1\nproject: {code: pf-03c}\nsource: {root: .}\ncommands:\n"
            + commands
            + "ci:\n  required: [" + ", ".join(required) + "]\n"
            + ("visual_qa:\n  enabled: true\n  reference: reference.png\n  route: http://127.0.0.1:4173/orders\n  viewports: [{width: 430, height: 932}]\n  threshold: 0.08\n" if visual else ""),
            encoding="utf-8",
        )
        if visual:
            (repo / "reference.png").write_bytes(b"test reference")
    (repo / "source.txt").write_text("committed source\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture source")
    head = git(repo, "rev-parse", "HEAD")

    (workspace / ".worktrees").mkdir()
    db = Database(tmp_path / "projectflow.sqlite")
    db.init()
    db.execute(
        "INSERT INTO tasks(id,slug,title,implementation_prompt) VALUES(1,'pf-03c','PF-03c',?)",
        ("UI task\nVISUAL_QA_REQUIRED: true" if visual else "Backend task",),
    )
    db.execute("INSERT INTO repositories(id,repo_name,repo_path) VALUES(1,'fixture',?)", (str(repo),))
    db.execute(
        "INSERT INTO integration_workspaces(id,repository_id,name,branch,worktree_path,base_branch,base_commit) VALUES(1,1,'integration','integration/pf-03c',?,'main',?)",
        (str(repo), head),
    )
    service = TaskDecisionService(db, GitWorkspaceService(workspace, worktree_root=workspace / ".worktrees"))
    return db, service, repo, head


def add_run(db: Database, *, workspace_type="integration", workspace_id=1, stage, status="PASS", sha, stdout="", stderr=""):
    return db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit,stdout_tail,stderr_tail) VALUES(?,?,?,?,?,?,?,?)",
        (workspace_type, workspace_id, "true", stage, status, sha, stdout, stderr),
    )


def integration(service, repo):
    return service.integration_gate_status(
        {"id": 1, "repository_id": 1, "base_commit": "base-sha", "worktree_path": str(repo)},
        1,
    )


def test_integration_requires_each_contract_stage_not_same_number_of_unrelated_rows(tmp_path):
    db, service, _repo, head = make_fixture(tmp_path, required=("test", "build"))
    add_run(db, stage="test", sha=head)
    add_run(db, stage="unrelated", sha=head)

    result = integration(service, _repo)

    assert result["tests_status"] == "NOT_RUN"
    assert result["tests_required"] == 2
    assert result["tests_passed"] == 1


def test_builder_requires_each_contract_stage_and_ignores_unrelated_pass(tmp_path):
    db, service, repo, head = make_fixture(tmp_path, required=("test", "build"))
    add_run(db, workspace_type="agent", workspace_id=9, stage="test", sha=head)
    add_run(db, workspace_type="agent", workspace_id=9, stage="unrelated", sha=head)

    assert service.builder_tests_status({"id": 9, "task_id": 1, "head": head, "worktree_path": str(repo)}) == "NOT_RUN"


def test_stale_sha_rows_cannot_supply_missing_required_stage(tmp_path):
    db, service, _repo, head = make_fixture(tmp_path, required=("test", "build"))
    add_run(db, stage="test", sha=head)
    add_run(db, stage="build", sha="stale-" + head)
    add_run(db, stage="extra", sha=head)

    assert integration(service, _repo)["tests_status"] == "NOT_RUN"


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "SKIPPED"])
def test_latest_pending_or_skipped_required_stage_is_not_run(tmp_path, status):
    db, service, _repo, head = make_fixture(tmp_path, required=("test", "build"))
    add_run(db, stage="test", sha=head)
    add_run(db, stage="build", status="PASS", sha=head)
    add_run(db, stage="build", status=status, sha=head)

    assert integration(service, _repo)["tests_status"] == "NOT_RUN"


def test_latest_required_pass_supersedes_older_failure_and_valid_waiver_keeps_named_failure_semantics(tmp_path):
    db, service, _repo, head = make_fixture(tmp_path, required=("test",))
    add_run(db, stage="test", status="FAIL", sha=head)
    add_run(db, stage="test", status="PASS", sha=head)
    add_run(db, stage="unrelated", status="FAIL", sha=head)
    assert integration(service, _repo)["tests_status"] == "PASS"

    row_id = add_run(db, stage="test", status="FAIL", sha=head)
    fp = fingerprint_of("test", "FAIL")
    db.execute(
        "INSERT INTO gate_waivers(task_id,integration_id,gate,test_identifier,failure_fingerprint,baseline_commit,baseline_run_id,reason,approved_by) VALUES(1,1,'test','test',?,'base-sha',?,'existing approved baseline','reviewer')",
        (fp, row_id),
    )
    result = integration(service, _repo)
    assert result["tests_status"] == "PASS_WITH_APPROVED_BASELINE_WAIVER"
    assert result["failures"][0]["classification"] == "WAIVED"


def test_timeout_or_mismatched_waiver_fingerprint_does_not_pass_required_gate(tmp_path):
    db, service, _repo, head = make_fixture(tmp_path, required=("test",))
    row_id = add_run(db, stage="test", status="TIMEOUT", sha=head)
    db.execute(
        "INSERT INTO gate_waivers(task_id,integration_id,gate,test_identifier,failure_fingerprint,baseline_commit,baseline_run_id,reason,approved_by) VALUES(1,1,'test','test','different-fingerprint','base-sha',?,'stale waiver','reviewer')",
        (row_id,),
    )

    result = integration(service, _repo)
    assert result["tests_status"] == "FAIL"
    assert result["failures"][0]["classification"] != "WAIVED"


def test_visual_opt_in_requires_visual_stage_and_visual_timeout_cannot_be_waived(tmp_path):
    db, service, _repo, head = make_fixture(tmp_path, required=("test",), visual=True)
    add_run(db, stage="test", sha=head)
    result = integration(service, _repo)
    assert result["tests_status"] == "NOT_RUN"
    assert result["tests_required"] == 2

    row_id = add_run(db, stage="visual_qa", status="TIMEOUT", sha=head)
    fp = fingerprint_of("visual_qa", "TIMEOUT")
    db.execute(
        "INSERT INTO gate_waivers(task_id,integration_id,gate,test_identifier,failure_fingerprint,baseline_commit,baseline_run_id,reason,approved_by) VALUES(1,1,'visual_qa','visual_qa',?,'base-sha',?,'must not waive visual gate','reviewer')",
        (fp, row_id),
    )
    result = integration(service, _repo)
    assert result["tests_status"] == "FAIL"
    assert result["failures"][0]["stage"] == "visual_qa"
    assert result["failures"][0]["classification"] != "WAIVED"


def test_backend_builder_does_not_require_optional_visual_stage(tmp_path):
    db, service, repo, head = make_fixture(tmp_path, required=("test", "build"), visual=True)
    db.execute("UPDATE tasks SET implementation_prompt='Backend task' WHERE id=1")
    add_run(db, workspace_type="agent", workspace_id=9, stage="test", sha=head)
    add_run(db, workspace_type="agent", workspace_id=9, stage="build", sha=head)
    add_run(db, workspace_type="agent", workspace_id=9, stage="visual_qa", status="FAIL", sha=head)

    assert service.builder_tests_status({"id": 9, "task_id": 1, "head": head, "worktree_path": str(repo)}) == "PASS"


def test_exit_zero_visual_stage_without_coordinator_evidence_fails_integration_gate(tmp_path):
    db, service, repo, head = make_fixture(tmp_path, required=("test",), visual=True)
    add_run(db, stage="test", sha=head)
    add_run(db, stage="visual_qa", status="PASS", sha=head, stdout="command returned zero")

    result = integration(service, repo)

    assert result["tests_status"] == "FAIL"
    assert result["failures"][0]["stage"] == "visual_qa"
    assert result["failures"][0]["classification"] != "WAIVED"


def test_integration_without_contract_and_zero_stage_legacy_behavior_is_preserved(tmp_path):
    db, service, repo, head = make_fixture(tmp_path, contract=False)
    add_run(db, workspace_type="agent", workspace_id=9, stage="legacy", sha=head)
    assert integration(service, repo)["tests_status"] == "PASS"
    assert service.builder_tests_status({"id": 9, "task_id": 1, "head": head, "worktree_path": str(repo)}) == "PASS"

    # A syntactically valid but zero-stage contract has the same historical
    # integration policy; it must not be confused with a malformed contract.
    (repo / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: empty}\ncommands: {}\n")
    git(repo, "add", "PROJECT.yaml")
    git(repo, "commit", "-m", "zero-stage legacy contract")
    new_head = git(repo, "rev-parse", "HEAD")
    assert integration(service, repo)["tests_status"] == "PASS"
    assert integration(service, repo)["tests_required"] == 0
    assert service.builder_tests_status({"id": 9, "task_id": 1, "head": new_head, "worktree_path": str(repo)}) == "NOT_RUN"


def test_visual_opt_in_with_invalid_contract_fails_closed_for_both_gates(tmp_path):
    db, service, repo, head = make_fixture(tmp_path, required=("test",), visual=True)
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: invalid}\ncommands:\n  test: {command: 'true'}\n"
        "ci: {required: [test]}\nvisual_qa: []\n",
        encoding="utf-8",
    )
    git(repo, "add", "PROJECT.yaml")
    git(repo, "commit", "-m", "invalid visual contract")
    invalid_head = git(repo, "rev-parse", "HEAD")
    add_run(db, stage="test", sha=invalid_head)
    add_run(db, workspace_type="agent", workspace_id=9, stage="test", sha=invalid_head)

    assert integration(service, repo)["tests_status"] == "UNKNOWN"
    assert service.builder_tests_status({"id": 9, "task_id": 1, "head": invalid_head, "worktree_path": str(repo)}) == "FAIL"


def test_invalid_present_nonvisual_contract_is_not_treated_as_missing_legacy_contract(tmp_path):
    db, service, repo, head = make_fixture(tmp_path, required=("test",))
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: invalid}\ncommands: []\nci: {required: [test]}\n",
        encoding="utf-8",
    )
    git(repo, "add", "PROJECT.yaml")
    git(repo, "commit", "-m", "invalid project contract")
    invalid_head = git(repo, "rev-parse", "HEAD")
    add_run(db, stage="test", sha=invalid_head)
    add_run(db, workspace_type="agent", workspace_id=9, stage="test", sha=invalid_head)

    result = integration(service, repo)
    assert result["tests_status"] == "UNKNOWN"
    assert not result["tests_pass"]
    assert service.builder_tests_status({"id": 9, "task_id": 1, "head": invalid_head, "worktree_path": str(repo)}) == "FAIL"
