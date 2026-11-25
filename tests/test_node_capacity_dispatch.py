from __future__ import annotations

from app.services.node_capacity import CapacityDecision


def test_capacity_block_refuses_workspace_and_worktree_allocation_before_side_effects(client, git_repo, monkeypatch):
    root, repo = git_repo
    registered = client.post("/api/repositories", data={
        "repo_path": str(repo), "repo_name": "capacity-demo", "default_branch": "main"
    }, follow_redirects=False)
    assert registered.status_code in (200, 303)
    repository_id = client.get("/api/repositories").json()[0]["id"]
    created = client.post("/api/tasks", data={"title": "Capacity gate canary"}, follow_redirects=False)
    task_id = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{task_id}/select", follow_redirects=False)

    gate = client.app.state.node_capacity_gate
    calls = []
    def block(paths, **kwargs):
        calls.append(tuple(map(str, paths)))
        return CapacityDecision("BLOCKED_CAPACITY", "dell-linux", "dev-test",
                                "CAPACITY_THRESHOLD_EXCEEDED", None, 1, 99.0, 10.0)
    monkeypatch.setattr(gate, "evaluate_paths", block)
    before = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_workspaces")["n"]
    response = client.post(f"/api/tasks/{task_id}/workspaces", data={
        "repository_id": str(repository_id), "agent": "codex", "role": "Backend",
        "base_branch": "main", "sandbox_profile": "NONE",
    }, follow_redirects=False)
    after = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_workspaces")["n"]
    assert response.status_code == 409
    assert calls and str(client.app.state.settings.worktree_root) in calls[0]
    assert after == before
    assert not (root / ".worktrees").exists()


def test_capacity_block_refuses_agent_process_before_session_creation(client, git_repo, monkeypatch):
    _, repo = git_repo
    client.post("/api/repositories", data={
        "repo_path": str(repo), "repo_name": "capacity-session", "default_branch": "main"
    }, follow_redirects=False)
    repository_id = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Capacity session gate"}, follow_redirects=False)
    task_id = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{task_id}/select", follow_redirects=False)
    # This first allocation creates a test worktree on pytest's disposable
    # /tmp tmpfs, which intentionally has less than the production 20 GiB
    # floor. Permit fixture setup, then exercise the actual blocked boundary.
    gate = client.app.state.node_capacity_gate
    monkeypatch.setattr(gate, "evaluate_paths", lambda paths, **kwargs: CapacityDecision(
        "ALLOW", "dell-linux", "dev-test", "fixture setup", None,
        100 * 1024**3, 20.0, 10.0))
    created = client.post(f"/api/tasks/{task_id}/workspaces", data={
        "repository_id": str(repository_id), "agent": "codex", "role": "Backend",
        "base_branch": "main", "sandbox_profile": "NONE",
    }, follow_redirects=False)
    assert created.status_code == 303
    workspace = client.get(f"/api/tasks/{task_id}").json()["workspaces"][0]
    # The task helper created its Builder Workspace; refuse at the real
    # session boundary and prove no AgentSession/process record is added.
    gate = client.app.state.node_capacity_gate
    monkeypatch.setattr(gate, "evaluate_paths", lambda paths, **kwargs: CapacityDecision(
        "BLOCKED_CAPACITY", "dell-linux", "dev-test", "CAPACITY_THRESHOLD_EXCEEDED",
        None, 1, 99.0, 10.0))
    before = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_sessions")["n"]
    response = client.post(f"/api/workspaces/{workspace['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    after = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_sessions")["n"]
    assert response.status_code == 409
    assert after == before
