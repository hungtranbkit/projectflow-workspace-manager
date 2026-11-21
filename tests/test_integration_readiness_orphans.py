"""Regression for live Dell orphan worktree integration-readiness 500/409 errors.

All paths are disposable pytest fixtures, never production source or DB.
The requested GET must be fail-closed and read-only, not an uncaught
FileNotFoundError or GitCommandError after a cleanup.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from tests.test_autonomous_execution import register, new_change
from tests.test_review_fix_loop import _committed_task, set_fake, PASS


def setup_worktree(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Stale workspace integration decision", project_id=rid)
    tid, workspace = _committed_task(client, cid)
    return root, repo, rid, tid, workspace


def assert_blocked_readiness(client, tid, expected: str):
    for endpoint in ("integration-readiness", "integration"):
        response = client.get(f"/api/tasks/{tid}/{endpoint}")
        assert response.status_code == 200, (endpoint, response.status_code, response.text)
        body = response.json()
        assert body["ready"] is False, body
        assert body["outcome"] == "BLOCKED", body
        assert expected in body["blockers"], body


def test_worktree_deleted_after_registration_returns_typed_blocker(client, git_repo):
    _, repo, _, tid, ws = setup_worktree(client, git_repo)
    shutil.rmtree(ws["worktree_path"])  # disposable fixture ONLY; leave DB reference stale
    assert_blocked_readiness(client, tid, "WORKTREE_MISSING")


def test_registered_repository_deleted_returns_typed_blocker(client, git_repo):
    root, _, rid, tid, _ = setup_worktree(client, git_repo)
    client.app.state.db.execute(
        "UPDATE repositories SET repo_path=? WHERE id=?",
        (str(root / "nonexistent-canonical"), rid))
    assert_blocked_readiness(client, tid, "REPOSITORY_MISSING")


def test_worktree_directory_without_git_metadata_returns_typed_blocker(client, git_repo):
    _, _, _, tid, ws = setup_worktree(client, git_repo)
    gitfile = Path(ws["worktree_path"]) / ".git"
    assert gitfile.is_file()
    gitfile.rename(gitfile.with_name(".git.disconnected"))  # fixture ONLY
    assert_blocked_readiness(client, tid, "WORKTREE_INVALID")


def test_cancelled_task_does_not_reopen_or_touch_deleted_worktree(client, git_repo):
    _, _, _, tid, ws = setup_worktree(client, git_repo)
    client.app.state.db.execute("UPDATE tasks SET status='CANCELLED' WHERE id=?", (tid,))
    shutil.rmtree(ws["worktree_path"])  # fixture ONLY
    assert_blocked_readiness(client, tid, "TASK_CANCELLED")


def test_merged_task_never_rechecks_missing_review_worktree(client, git_repo):
    _, _, rid, tid, ws = setup_worktree(client, git_repo)
    client.app.state.db.execute(
        "INSERT INTO merge_records(task_id,repository_id,required,merge_status) "
        "VALUES(?,?,1,'MERGED') "
        "ON CONFLICT(task_id,repository_id) DO UPDATE SET required=1,merge_status='MERGED'",
        (tid, rid))
    shutil.rmtree(ws["worktree_path"])  # fixture ONLY
    assert_blocked_readiness(client, tid, "ALREADY_MERGED")


def test_real_valid_review_and_worktree_still_ready(client, git_repo):
    _, _, _, tid, _ = setup_worktree(client, git_repo)
    set_fake(client, PASS)
    review = client.post(f"/api/tasks/{tid}/review/code")
    assert review.status_code == 200, review.text
    response = client.get(f"/api/tasks/{tid}/integration-readiness")
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "INTEGRATION_READY", response.json()
