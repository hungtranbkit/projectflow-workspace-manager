from __future__ import annotations

from app.services.node_capacity import CapacityDecision


def test_real_gate_http_rejects_measured_filesystem(client, git_repo, monkeypatch):
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
    from dataclasses import replace
    calls = []
    decisions = []
    monkeypatch.setattr(gate, "policy", replace(gate.policy, block_free_bytes=1<<60, clear_free_bytes=(1<<60)+1))
    real_evaluate = gate.evaluate_paths
    def observed(paths, **kwargs):
        calls.append(tuple(map(str, paths)))
        decision = real_evaluate(paths, **kwargs)
        decisions.append(decision)
        return decision
    monkeypatch.setattr(gate, "evaluate_paths", observed)
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


    assert decisions and decisions[0].code == 'BLOCKED_CAPACITY'
    print('REAL_GATE_DECISION', decisions[0])


def test_real_gate_http_allows_measured_filesystem(client, git_repo, monkeypatch):
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
    from dataclasses import replace
    calls = []
    decisions = []
    real_evaluate = gate.evaluate_paths
    def observed(paths, **kwargs):
        calls.append(tuple(map(str, paths)))
        decision = real_evaluate(paths, **kwargs)
        decisions.append(decision)
        return decision
    monkeypatch.setattr(gate, "evaluate_paths", observed)
    before = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_workspaces")["n"]
    response = client.post(f"/api/tasks/{task_id}/workspaces", data={
        "repository_id": str(repository_id), "agent": "codex", "role": "Backend",
        "base_branch": "main", "sandbox_profile": "NONE",
    }, follow_redirects=False)
    after = client.app.state.db.one("SELECT COUNT(*) AS n FROM agent_workspaces")["n"]
    assert response.status_code < 400
    assert calls and str(client.app.state.settings.worktree_root) in calls[0]
    assert after == before + 1


    assert decisions and decisions[0].code in {'ALLOW','WARN_CAPACITY'}
    print('REAL_GATE_DECISION', decisions[0])
