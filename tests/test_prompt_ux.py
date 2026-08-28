"""Simplify Task preparation UX: ONE Implementation Prompt replaces the
structured Brief form as the primary way to describe a Task. These tests
exercise the real HTTP API + real git worktrees, deliberately using
sandbox_profile=NONE everywhere so they need no Docker at all (same
no-docker discipline as test_task_lifecycle_engine.py).

Covers: create-from-one-prompt, structured old tasks still load, risk/
workflow defaults Normal, advanced fields optional, prompt edit increments
version, review becomes stale after prompt version changes."""
from __future__ import annotations
import subprocess

import pytest


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def decision(client, tid):
    return client.get(f"/api/tasks/{tid}/decision").json()


def task(client, tid):
    return client.get(f"/api/tasks/{tid}").json()


SAMPLE_PROMPT = (
    "Update the MESFlow default password.\n"
    "The seeded admin should use the new password.\n"
    "First login must still require password change.\n"
    "Do not affect existing users.\n"
    "Verify in a fresh sandbox that the new password works and old password does not.\n"
    "Run tests, commit, and report how to verify."
)


def test_task_created_from_one_prompt_lands_in_backlog(client, git_repo):
    """No Repository/Agent given -- just a title + prompt -- must land in
    BACKLOG with no branch/worktree/sandbox allocated, same BACKLOG
    contract as the old /api/tasks route."""
    r = client.post("/api/tasks/create", data={"title": "Fix default password", "implementation_prompt": SAMPLE_PROMPT}, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[-1])
    t = task(client, tid)
    assert t["status"] == "BACKLOG"
    assert t["implementation_prompt"] == SAMPLE_PROMPT
    assert t["workspaces"] == []
    d = decision(client, tid)
    assert d["next_action"]["action"] == "SELECT_FOR_DEVELOPMENT"


def test_create_and_start_with_repository_and_agent(client, git_repo):
    """Repository + Agent given -> Task + first Builder Workspace created
    together ("Create & Start"), same one-shot semantics the old
    /new-with-workspace Advanced form had, now as the primary flow."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks/create", data={
        "title": "Fix default password", "implementation_prompt": SAMPLE_PROMPT,
        "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE",
    }, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[-1])
    t = task(client, tid)
    assert t["status"] == "ACTIVE"
    assert len(t["workspaces"]) == 1
    w = t["workspaces"][0]
    assert w["agent"] == "claude" and w["repository_id"] == rid
    d = decision(client, tid)
    assert d["stage"] == "DEVELOPMENT"
    assert d["next_action"]["action"] == "OPEN_BUILDER"


def test_advanced_fields_optional_with_correct_defaults(client, git_repo):
    """Base branch defaults to main and Workflow (risk_profile) defaults
    to NORMAL when Advanced is never opened -- omitting ws_base_branch/
    ws_role/risk_profile entirely must not error or silently pick a
    different default."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks/create", data={
        "title": "No advanced fields", "implementation_prompt": SAMPLE_PROMPT,
        "repository_id": rid, "agent": "codex", "sandbox_profile": "NONE",
    }, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[-1])
    t = task(client, tid)
    assert t["risk_profile"] == "NORMAL"
    w = t["workspaces"][0]
    assert w["base_branch"] == "main"
    assert run(w["worktree_path"], "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == w["branch"]


def test_workflow_defaults_normal_when_omitted_on_plain_create(client, git_repo):
    r = client.post("/api/tasks/create", data={"title": "Plain create, no workflow field"}, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[-1])
    assert task(client, tid)["risk_profile"] == "NORMAL"


def test_advanced_workflow_and_additional_repository_honored(client, git_repo):
    """Advanced's Workflow select and the additional-repositories array
    (ws_repository_id/ws_agent/ws_role/ws_base_branch/ws_sandbox_profile)
    both apply on top of the primary repository/agent."""
    root, repo = git_repo
    other = root / "second"; other.mkdir()
    run(other, "git", "init", "-b", "main"); run(other, "git", "config", "user.email", "t@t"); run(other, "git", "config", "user.name", "t")
    (other / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: second}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (other / "README.md").write_text("x\n"); run(other, "git", "add", "."); run(other, "git", "commit", "-m", "base")
    register(client, repo, "demo"); register(client, other, "second")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}
    r = client.post("/api/tasks/create", data={
        "title": "Cross-repo via advanced", "implementation_prompt": SAMPLE_PROMPT,
        "repository_id": repos["demo"], "agent": "claude", "sandbox_profile": "NONE",
        "risk_profile": "high",
        "ws_repository_id": [str(repos["second"])], "ws_agent": ["codex"], "ws_role": ["Firmware"],
        "ws_base_branch": ["main"], "ws_sandbox_profile": ["NONE"],
    }, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[-1])
    t = task(client, tid)
    assert t["risk_profile"] == "HIGH"
    assert len(t["workspaces"]) == 2
    assert {w["repository_id"] for w in t["workspaces"]} == {repos["demo"], repos["second"]}


def test_agent_prompt_wraps_users_prompt_verbatim_with_repo_context(client, git_repo):
    """'Do not rewrite the user's prompt silently': the composed
    agent_prompt must contain the Implementation Prompt text completely
    unchanged, plus real repo context (repo name, AGENTS.md if present)."""
    root, repo = git_repo
    (repo / "AGENTS.md").write_text("RULE: never touch main directly.\n")
    run(repo, "git", "add", "."); run(repo, "git", "commit", "-m", "add AGENTS.md")
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks/create", data={
        "title": "Prompt fidelity check", "implementation_prompt": SAMPLE_PROMPT,
        "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE",
    }, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    t = task(client, tid)
    assert SAMPLE_PROMPT in t["agent_prompt"]  # verbatim, not reworded
    assert "demo" in t["agent_prompt"]
    assert "never touch main directly" in t["agent_prompt"]


def test_prompt_edit_increments_version_no_op_save_does_not(client, git_repo):
    r = client.post("/api/tasks/create", data={"title": "Version bump check", "implementation_prompt": "v1 of the prompt"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    v1 = task(client, tid)["brief_version"]

    client.post(f"/api/tasks/{tid}/prompt", data={"implementation_prompt": "v1 of the prompt"})
    assert task(client, tid)["brief_version"] == v1  # identical content -> no bump

    client.post(f"/api/tasks/{tid}/prompt", data={"implementation_prompt": "v2 -- a materially different prompt"})
    v2 = task(client, tid)["brief_version"]
    assert v2 == v1 + 1
    assert task(client, tid)["implementation_prompt"] == "v2 -- a materially different prompt"


def test_review_becomes_stale_after_prompt_version_changes(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks/create", data={
        "title": "Stale on prompt edit", "implementation_prompt": "initial scope",
        "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE",
    }, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = task(client, tid)["workspaces"][0]
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    r2 = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"}, follow_redirects=False)
    assert r2.status_code == 303
    assert decision(client, tid)["builders"][0]["review_status"] == "PASS"

    client.post(f"/api/tasks/{tid}/prompt", data={"implementation_prompt": "expanded scope -- also handle X"})
    d = decision(client, tid)
    assert d["builders"][0]["review_status"] == "STALE"
    assert d["next_action"]["action"] == "START_REVIEW"


def test_structured_old_task_still_loads(client, git_repo):
    """A Task created before the prompt-first UX existed (structured
    brief_* fields, no implementation_prompt) must still render its Task
    Detail page and be considered to have a complete brief -- 'structured
    old tasks still load'."""
    r = client.post("/api/tasks", data={"title": "Legacy structured task"}, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[-1])
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/brief", data={"goal": "old-style goal", "acceptance_criteria": "old-style AC"})
    t = task(client, tid)
    assert t["implementation_prompt"] == ""
    assert t["brief_goal"] == "old-style goal"

    page = client.get(f"/tasks/{tid}").text
    assert page  # renders without error
    assert "old-style goal" in page
    assert "Implementation Brief" in page  # legacy form still shown, not the new prompt box

    d = decision(client, tid)
    assert d["next_action"]["action"] == "CREATE_BUILDER_WORKSPACE"  # brief_complete() is True via the legacy path
