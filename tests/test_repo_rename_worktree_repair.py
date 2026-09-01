"""P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a repository directory
rename/move is now a real, supported operation (B7.1's own rebind
policy) -- but a real git limitation means every EXISTING worktree
(an in-flight Builder/Integration session's real working directory)
silently breaks at the git-internals level when its parent repo moves,
even though nothing about the worktree's own files or commits changed.
Reproduced and fixed with real git worktrees, a real filesystem
rename, and a real subprocess `git status` call in the worktree --
never mocked."""
from __future__ import annotations
import subprocess

from tests.test_autonomous_execution import register


def test_renamed_repo_breaks_worktree_without_repair(git_repo):
    """First, prove the underlying git limitation is real (not assumed)
    -- this is the failure register()'s new repair call exists to fix."""
    root, repo = git_repo
    subprocess.run(["git", "worktree", "add", "-q", str(root / "wt1"), "-b", "agent/claude/demo", "main"],
                    cwd=repo, check=True)
    moved = root / "demo-moved-raw"
    repo.rename(moved)
    r = subprocess.run(["git", "status", "--porcelain"], cwd=root / "wt1", capture_output=True, text=True)
    assert r.returncode != 0
    assert "not a git repository" in (r.stderr or "")
    moved.rename(repo)  # restore for any fixture teardown expecting the original path


def test_rebind_repairs_existing_worktree_of_the_rebound_repo(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    r = client.post("/api/workspaces", data={"repository_id": rid, "agent": "claude", "task_name": "demo-task", "base_branch": "main"},
                     follow_redirects=False)
    assert r.status_code == 303, r.text
    wt_path = client.app.state.db.one("SELECT worktree_path FROM agent_workspaces WHERE repository_id=?", (rid,))["worktree_path"]

    # Sanity: the worktree is real and usable before the rename.
    pre = subprocess.run(["git", "status", "--porcelain"], cwd=wt_path, capture_output=True, text=True)
    assert pre.returncode == 0

    moved = root / "demo-moved"
    repo.rename(moved)
    broken = subprocess.run(["git", "status", "--porcelain"], cwd=wt_path, capture_output=True, text=True)
    assert broken.returncode != 0, "the worktree must actually be broken by the rename (real git limitation)"

    r = client.post("/api/repositories", data={"repo_path": str(moved), "repo_name": "demo", "default_branch": "main"},
                     follow_redirects=False)
    assert r.status_code == 303, r.text

    rows = client.app.state.db.all("SELECT * FROM repositories")
    assert len(rows) == 1, "must have rebound the existing row, not duplicated it"
    assert rows[0]["id"] == rid
    assert rows[0]["repo_path"] == str(moved)

    fixed = subprocess.run(["git", "status", "--porcelain"], cwd=wt_path, capture_output=True, text=True)
    assert fixed.returncode == 0, f"worktree must be repaired after the rebind: {fixed.stderr}"

    # The worktree's own commit history must be intact, not just "some
    # command exits 0" -- proves the repair is real, not cosmetic.
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=wt_path, capture_output=True, text=True)
    assert log.returncode == 0 and log.stdout.strip()


def test_rebind_repair_is_best_effort_when_worktree_already_gone(client, git_repo):
    """A worktree whose directory was already removed (e.g. cleaned up
    by CleanupWorker before the rebind ran) must never make the rebind
    itself fail -- repair is best-effort per path."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    r = client.post("/api/workspaces", data={"repository_id": rid, "agent": "claude", "task_name": "gone-task", "base_branch": "main"},
                     follow_redirects=False)
    assert r.status_code == 303, r.text
    wt_path = client.app.state.db.one("SELECT worktree_path FROM agent_workspaces WHERE repository_id=?", (rid,))["worktree_path"]

    import shutil
    shutil.rmtree(wt_path)  # simulate an already-cleaned-up worktree

    moved = root / "demo-moved"
    repo.rename(moved)
    r = client.post("/api/repositories", data={"repo_path": str(moved), "repo_name": "demo", "default_branch": "main"},
                     follow_redirects=False)
    assert r.status_code == 303, r.text
    rows = client.app.state.db.all("SELECT * FROM repositories")
    assert len(rows) == 1 and rows[0]["repo_path"] == str(moved)
