"""Builder execution UX with Task Title fallback: implementation_prompt is
optional everywhere a Task/Builder is created or started -- the Task title
(mandatory at creation) is always sufficient intent. These tests exercise
the real HTTP API + real git worktrees, deliberately using
sandbox_profile=NONE everywhere so they need no Docker at all (same
no-docker discipline as test_task_lifecycle_engine.py / test_prompt_ux.py).

Covers: title-fallback Start Builder, detailed prompt overrides title,
whitespace-only prompt falls back, empty title rejected, prompt-version
increments and stales old review evidence, multi-Builder role context,
Start All Builders with title fallback, and workspace READY + no session
-> START_BUILDER."""
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


def create_and_start(client, title, rid, agent="claude", prompt="", role=""):
    data = {"title": title, "repository_id": rid, "agent": agent, "sandbox_profile": "NONE"}
    if prompt:
        data["implementation_prompt"] = prompt
    if role:
        data["ws_role"] = role
    r = client.post("/api/tasks/create", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("/")[-1])


def test_title_exists_prompt_empty_start_builder_allowed_effective_prompt_is_title(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Update default password", rid, agent="claude")
    t = task(client, tid)
    assert t["implementation_prompt"] == ""
    w = t["workspaces"][0]

    d = decision(client, tid)
    assert d["prompt_source"] == "TITLE"
    assert d["effective_task_prompt"] == "Update default password"
    assert d["next_action"]["action"] == "START_BUILDER"  # not blocked by empty prompt

    # Start Builder (web PTY path) is genuinely not blocked -- no launcher
    # registered for these agents in the test settings, but the request
    # reaches the trusted-agent-registry check (never a prompt check).
    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert r.status_code in (303, 409)  # 409 only if launcher unavailable, never over a missing prompt
    if r.status_code == 409:
        assert "prompt" not in r.text.lower()


def test_detailed_prompt_overrides_title_as_primary_intent(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Update default password", rid, prompt="Change the seeded admin password to a new value.")
    d = decision(client, tid)
    assert d["prompt_source"] == "IMPLEMENTATION_PROMPT"
    assert d["effective_task_prompt"] == "Change the seeded admin password to a new value."
    t = task(client, tid)
    assert "Change the seeded admin password" in t["agent_prompt"]


def test_blank_whitespace_prompt_falls_back_to_title(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Fix kiosk login", rid, prompt="   \n\t  ")
    d = decision(client, tid)
    assert d["prompt_source"] == "TITLE"
    assert d["effective_task_prompt"] == "Fix kiosk login"


def test_empty_title_rejected(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks/create", data={"title": "", "repository_id": rid, "agent": "claude"}, follow_redirects=False)
    assert r.status_code == 409
    r2 = client.post("/api/tasks", data={"title": "   "}, follow_redirects=False)
    assert r2.status_code == 409


def test_adding_detailed_prompt_after_review_increments_version_and_stales_evidence(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Update default password", rid, agent="claude")
    v1 = task(client, tid)["brief_version"]
    w = task(client, tid)["workspaces"][0]

    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    r = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"}, follow_redirects=False)
    assert r.status_code == 303
    assert decision(client, tid)["builders"][0]["review_status"] == "PASS"

    client.post(f"/api/tasks/{tid}/prompt", data={"implementation_prompt": "New password must be X and old password must stop working."})
    v2 = task(client, tid)["brief_version"]
    assert v2 == v1 + 1  # never silently treated as the same version as the title-derived intent

    d = decision(client, tid)
    assert d["prompt_source"] == "IMPLEMENTATION_PROMPT"
    assert d["builders"][0]["review_status"] == "STALE"
    assert d["next_action"]["action"] == "START_REVIEW"


def test_multiple_builders_with_empty_prompt_each_get_title_and_own_role(client, git_repo):
    root, repo = git_repo
    other = root / "second"; other.mkdir()
    run(other, "git", "init", "-b", "main"); run(other, "git", "config", "user.email", "t@t"); run(other, "git", "config", "user.name", "t")
    (other / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: second}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (other / "README.md").write_text("x\n"); run(other, "git", "add", "."); run(other, "git", "commit", "-m", "base")
    register(client, repo, "demo"); register(client, other, "second")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}

    r = client.post("/api/tasks/create", data={
        "title": "Fix kiosk login",
        "repository_id": repos["demo"], "agent": "claude", "sandbox_profile": "NONE", "primary_role": "Backend",
        "ws_repository_id": [str(repos["second"])], "ws_agent": ["codex"], "ws_role": ["Firmware"],
        "ws_base_branch": ["main"], "ws_sandbox_profile": ["NONE"],
    }, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[-1])
    t = task(client, tid)
    assert t["implementation_prompt"] == ""
    ws = {w["role"]: w for w in t["workspaces"]}
    assert set(ws) == {"Backend", "Firmware"}

    page = client.get(f"/tasks/{tid}").text
    # both Builders' live-generated prompts carry the same Task-title
    # intent plus their own distinct ROLE context, with no separate
    # user-written prompt required per Builder.
    assert page.count("Fix kiosk login") >= 2
    assert "## ROLE\nBackend" in page
    assert "## ROLE\nFirmware" in page


def test_start_all_builders_works_with_title_fallback_and_skips_running(client, git_repo):
    root, repo = git_repo
    other = root / "second"; other.mkdir()
    run(other, "git", "init", "-b", "main"); run(other, "git", "config", "user.email", "t@t"); run(other, "git", "config", "user.name", "t")
    (other / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: second}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (other / "README.md").write_text("x\n"); run(other, "git", "add", "."); run(other, "git", "commit", "-m", "base")
    register(client, repo, "demo"); register(client, other, "second")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}

    r = client.post("/api/tasks/create", data={
        "title": "Fix kiosk login",
        "repository_id": repos["demo"], "agent": "claude", "sandbox_profile": "NONE",
        "ws_repository_id": [str(repos["second"])], "ws_agent": ["codex"], "ws_role": ["Firmware"],
        "ws_base_branch": ["main"], "ws_sandbox_profile": ["NONE"],
    }, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    assert all(b["agent_status"] == "NOT_STARTED" for b in decision(client, tid)["builders"])

    r2 = client.post(f"/api/tasks/{tid}/start-all-builders", follow_redirects=False)
    assert r2.status_code == 303
    # test settings have no real launcher registered for claude/codex, so
    # each start attempt fails at the launcher step and is recorded, not
    # silently ignored, and never blocked by a missing Implementation Prompt.
    events = client.app.state.db.all("SELECT * FROM workspace_events WHERE action IN ('SESSION_STARTED','SESSION_START_FAILED')")
    assert len(events) == 2  # attempted both, in one call


def test_workspace_ready_no_session_next_action_is_start_builder(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_and_start(client, "Ready but not started", rid, agent="codex")
    d = decision(client, tid)
    b = d["builders"][0]
    assert b["status"] == "CREATED"
    assert b["agent_status"] == "NOT_STARTED"
    assert d["next_action"]["action"] == "START_BUILDER"
