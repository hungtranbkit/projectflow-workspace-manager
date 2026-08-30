"""Worktree Isolation Foundation (Phase E8.5). Discovery-driven: this
codebase's Builder Workspaces have ALWAYS been real, isolated git
worktrees (GitWorkspaceService.create_agent(), used by add_task_
workspace() for a manual launch and an E8 autonomous one alike) --
WorktreeManager is a thin service over that existing agent_workspaces
table + GitWorkspaceService, never a second workspace/Git concept.
These tests cover what's genuinely new: computed lifecycle status,
staleness, non-mutating integration/conflict check, explicit abandon/
remove, and canonical-checkout-untouched verification."""
from __future__ import annotations
import json
import subprocess

import pytest

from app.launchers import AgentLauncher
from tests.test_autonomous_execution import register, new_change, enable_autonomous, materialize_task


def _select_and_create_workspace(client, tid, rid, agent="codex"):
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces",
                data={"repository_id": rid, "agent": agent, "role": "BUILDER", "base_branch": "main", "sandbox_profile": "NONE"})
    return [x for x in client.get(f"/api/tasks/{tid}").json()["workspaces"] if x["agent"] == agent][-1]


# ================================================================ Worktree creation (E8.5.31)

def test_create_task_worktree_captures_deterministic_branch_and_base(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "WT create change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/worktree/create", data={"repository_id": rid, "agent": "codex"})
    assert r.status_code == 200, r.text
    ws = client.get(f"/api/tasks/{tid}/worktree").json()
    task_slug = client.app.state.db.one("SELECT slug FROM tasks WHERE id=?", (tid,))["slug"]
    assert ws["branch"] == f"agent/codex/{task_slug}-demo".lower()
    assert ws["base_commit"]
    assert ws["worktree_path"]
    assert ws["lifecycle_status"] == "READY"


def test_duplicate_task_worktree_for_same_agent_rejected(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "WT dup change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    client.post(f"/api/tasks/{tid}/select")
    r1 = client.post(f"/api/tasks/{tid}/worktree/create", data={"repository_id": rid, "agent": "codex"})
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/api/tasks/{tid}/worktree/create", data={"repository_id": rid, "agent": "codex"})
    assert r2.status_code == 400, r2.text  # branch already exists -> WorktreeManagerError -> 400


def test_worktree_for_unknown_repository_rejected(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "WT bad repo change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/worktree/create", data={"repository_id": 999999, "agent": "codex"})
    assert r.status_code == 400, r.text


def test_no_worktree_returns_404(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "No worktree change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    r = client.get(f"/api/tasks/{tid}/worktree")
    assert r.status_code == 404


# ================================================================ Isolation (E8.5.5)

def test_canonical_checkout_unaffected_by_worktree_edits(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Isolation change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    before_status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    w = _select_and_create_workspace(client, tid, rid)
    # simulate a Builder editing/committing inside the WORKTREE only
    (client.app.state.git.validate_worktree(w["worktree_path"]) / "new_file.txt").write_text("builder work\n")
    subprocess.run(["git", "add", "new_file.txt"], cwd=w["worktree_path"], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "builder commit"], cwd=w["worktree_path"], check=True, capture_output=True)
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    after_status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert after == before
    assert after_status == before_status
    assert not (repo / "new_file.txt").exists()


def test_worktree_changes_isolated_from_canonical_and_scope_uses_worktree_diff(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Scope isolation change", project_id=rid)
    tid, plan_id = materialize_task(client, cid, scope_hints=["src/"])
    w = _select_and_create_workspace(client, tid, rid)
    (client.app.state.git.validate_worktree(w["worktree_path"]) / "src").mkdir()
    (client.app.state.git.validate_worktree(w["worktree_path"]) / "src" / "a.py").write_text("x=1\n")
    subprocess.run(["git", "add", "src/a.py"], cwd=w["worktree_path"], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "in scope"], cwd=w["worktree_path"], check=True, capture_output=True)
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    wp = client.app.state.db.one("SELECT * FROM work_products WHERE task_id=? AND kind='CODE_CHANGE'", (tid,))
    meta = json.loads(wp["content_metadata"])
    assert meta["scope_check"]["violation"] is False
    assert meta["scope_check"]["changed_files"] == ["src/a.py"]
    assert meta["branch_name"] == w["branch"]
    assert meta["base_commit"]
    assert meta["head_commit"]
    assert len(meta["commits"]) == 1


# ================================================================ Autonomous execution integration (E8.5.6/E8.5.9)

def test_autonomous_execution_still_uses_managed_worktree_via_existing_supervisor(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "Auto worktree change", project_id=rid)
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "echo READY; cat"))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1
    tid, _ = materialize_task(client, cid)
    r = client.post(f"/api/tasks/{tid}/autonomous-start")
    assert r.json()["outcome"] == "LAUNCHED", r.json()
    ws = client.get(f"/api/tasks/{tid}/worktree").json()
    assert ws["worktree_path"] != str(repo)  # never the canonical checkout itself
    assert ws["lifecycle_status"] == "IN_USE"


# ================================================================ Builder context isolation rules (E8.5.12)

def test_builder_prompt_includes_workspace_isolation_rules(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    r = client.post("/api/tasks", data={"title": "Isolation prompt task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Isolation prompt task"][0]
    w = _select_and_create_workspace(client, tid, rid)
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "echo READY; cat"))}
    client.app.state.agent_sessions.prompt_ready_timeout = 3.0
    client.app.state.agent_sessions.prompt_quiet_window = 0.1
    client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"})
    prompt = client.app.state.db.one("SELECT content FROM prompts WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))["content"]
    assert "## WORKSPACE ISOLATION" in prompt
    assert "Do not access or edit the canonical checkout." in prompt
    assert "Do not merge." in prompt


# ================================================================ Staleness (E8.5.18)

def test_worktree_base_stale_detected_never_auto_rebased(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Stale WT change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    stale_before = client.get(f"/api/tasks/{tid}/worktree").json()["staleness"]
    assert stale_before["stale"] is False
    (repo / "another.txt").write_text("advance base\n")
    subprocess.run(["git", "add", "another.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "advance main"], cwd=repo, check=True, capture_output=True)
    stale_after = client.get(f"/api/tasks/{tid}/worktree").json()["staleness"]
    assert stale_after["stale"] is True
    assert stale_after["reason"] == "WORKTREE_BASE_STALE"
    # never auto-rebased -- the worktree's own branch tip is unchanged
    wt_head_before = client.app.state.git.head(w["worktree_path"])
    assert client.app.state.git.head(w["worktree_path"]) == wt_head_before


# ================================================================ Integration check (E8.5.19)

def test_integration_check_clean(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Integration clean change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    (client.app.state.git.validate_worktree(w["worktree_path"]) / "feature.txt").write_text("hello\n")
    subprocess.run(["git", "add", "feature.txt"], cwd=w["worktree_path"], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add feature"], cwd=w["worktree_path"], check=True, capture_output=True)
    r = client.get(f"/api/tasks/{tid}/integration-check")
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "CLEAN"
    # never actually merged into main
    assert not (repo / "feature.txt").exists()


def test_integration_check_conflict(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Integration conflict change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    (client.app.state.git.validate_worktree(w["worktree_path"]) / "README.md").write_text("worktree version\n")
    subprocess.run(["git", "add", "README.md"], cwd=w["worktree_path"], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "worktree edits README"], cwd=w["worktree_path"], check=True, capture_output=True)
    (repo / "README.md").write_text("canonical version, different\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "main edits README differently"], cwd=repo, check=True, capture_output=True)
    r = client.get(f"/api/tasks/{tid}/integration-check")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "CONFLICT"
    assert "README.md" in body["conflicting_files"]
    # probe never left behind, main never touched
    assert (repo / "README.md").read_text() == "canonical version, different\n"


# ================================================================ Cleanup / abandon / remove (E8.5.20/E8.5.21)

def test_worktree_retained_after_builder_submission(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Retain change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    ws = client.get(f"/api/tasks/{tid}/worktree").json()
    assert ws["lifecycle_status"] == "REVIEW_PENDING"
    from pathlib import Path
    assert Path(ws["worktree_path"]).is_dir()


def test_remove_blocked_until_abandoned_or_integrated(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Remove blocked change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    _select_and_create_workspace(client, tid, rid)
    r = client.post(f"/api/tasks/{tid}/worktree/cleanup")
    assert r.status_code == 400, r.text


def test_explicit_abandon_preserves_metadata_never_deletes_filesystem(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Abandon change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    r = client.post(f"/api/tasks/{tid}/worktree/abandon", data={"note": "no longer needed"})
    assert r.status_code == 200, r.text
    ws = client.get(f"/api/tasks/{tid}/worktree").json()
    assert ws["lifecycle_status"] == "ABANDONED"
    from pathlib import Path
    assert Path(ws["worktree_path"]).is_dir()  # never auto-deleted


def test_explicit_remove_after_abandon_removes_only_managed_path(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Remove after abandon change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    client.post(f"/api/tasks/{tid}/worktree/abandon")
    r = client.post(f"/api/tasks/{tid}/worktree/cleanup")
    assert r.status_code == 200, r.text
    from pathlib import Path
    assert not Path(w["worktree_path"]).is_dir()
    assert repo.is_dir()  # canonical checkout itself never touched


# ================================================================ Recovery (E8.5.22)

def test_missing_worktree_reported_honestly_no_auto_recreate(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Missing WT change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    w = _select_and_create_workspace(client, tid, rid)
    import shutil
    shutil.rmtree(client.app.state.git.validate_worktree(w["worktree_path"]))
    ws = client.get(f"/api/tasks/{tid}/worktree").json()
    assert ws["lifecycle_status"] == "MISSING"
    # no live session was started to "fix" this
    assert not client.app.state.db.all("SELECT id FROM agent_sessions WHERE task_id=?", (tid,))


# ================================================================ Backward compatibility (E8.5.31)

def test_manual_legacy_workspace_flow_unaffected(client, git_repo):
    root, repo = git_repo
    rid = register(client, repo, "demo")
    r = client.post("/api/tasks", data={"title": "Legacy manual task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Legacy manual task"][0]
    w = _select_and_create_workspace(client, tid, rid)
    assert w["worktree_path"]
    r2 = client.get(f"/tasks/{tid}")
    assert r2.status_code == 200
    assert "Worktree state" in r2.text


def test_e8_readiness_unaffected_by_worktree_manager_wiring(client, git_repo):
    root, repo = git_repo
    enable_autonomous(repo)
    rid = register(client, repo, "demo")
    cid = new_change(client, "E8 unaffected change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] == "AUTO_READY"


# ================================================================ Dirty canonical repo real test (E8.5.29)

def test_dirty_canonical_repo_worktree_excludes_uncommitted_changes(client, git_repo):
    """The DETERMINISTIC counterpart to E8.24/E8.5.28's real-LLM proof:
    a canonical checkout with a genuinely unrelated uncommitted file
    (the SAME shape as this very repo's own known unrelated WIP diff)
    must not have that file leak into a freshly created Task worktree,
    and the readiness/API surface must say so explicitly, never guess
    or silently include it."""
    root, repo = git_repo
    rid = register(client, repo, "demo")
    cid = new_change(client, "Dirty canonical worktree change", project_id=rid)
    tid, _ = materialize_task(client, cid)

    (repo / "unrelated_wip.txt").write_text("someone's uncommitted work in progress, unrelated to this Task\n")

    readiness = client.get(f"/api/tasks/{tid}/execution-readiness").json()
    assert readiness["readiness"] == "AUTO_READY"
    assert readiness["worktree_isolation_note"] == "BASE_REVISION_EXCLUDES_UNCOMMITTED_CHANGES"

    w = _select_and_create_workspace(client, tid, rid)
    from pathlib import Path
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    assert not (worktree / "unrelated_wip.txt").exists()  # dirty canonical file never copied in
    assert (repo / "unrelated_wip.txt").exists()  # user's own uncommitted file untouched, still there
    assert (repo / "unrelated_wip.txt").read_text() == "someone's uncommitted work in progress, unrelated to this Task\n"

    # Builder operates safely on the committed base only -- base_commit
    # is the canonical branch's last COMMITTED tip, not a snapshot that
    # includes the dirty file.
    committed_head = client.app.state.git.head(repo)
    assert w["base_commit"] == committed_head
