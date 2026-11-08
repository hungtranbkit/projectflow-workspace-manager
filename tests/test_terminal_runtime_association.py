from __future__ import annotations

import pytest

from app.services.terminal_runtime import RuntimeAssociationError, TerminalRuntimeAssociations


def test_association_is_idempotent_for_same_task_workspace_and_native_session(client, git_repo):
    root, repo = git_repo
    client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo", "default_branch": "main"})
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks/create", data={"title": "runtime", "repository_id": rid, "agent": "codex", "sandbox_profile": "NONE", "risk_profile": "LOW"})
    task = client.app.state.db.one("SELECT * FROM tasks ORDER BY id DESC LIMIT 1")
    ws = client.app.state.db.one("SELECT * FROM agent_workspaces WHERE task_id=?", (task["id"],))
    store = TerminalRuntimeAssociations(client.app.state.db)

    first = store.record(task_id=task["id"], workspace_id=ws["id"], backend="herdr-core",
                         node_id="dell-linux", native_session_id="t7", durable_task_id="tmcp-9")
    second = store.record(task_id=task["id"], workspace_id=ws["id"], backend="herdr-core",
                          node_id="dell-linux", native_session_id="t7", durable_task_id="tmcp-9")

    assert first["id"] == second["id"]
    assert first["branch"] == ws["branch"]
    assert first["worktree"] == ws["worktree_path"]
    assert client.app.state.db.one("SELECT count(*) n FROM terminal_runtime_associations")["n"] == 1


def test_association_refuses_mismatched_task_workspace_and_unknown_backend(client, git_repo):
    store = TerminalRuntimeAssociations(client.app.state.db)
    with pytest.raises(RuntimeAssociationError, match="BACKEND_REFUSED"):
        store.record(task_id=1, workspace_id=1, backend="other", node_id="hp-linux", native_session_id="x")
    with pytest.raises(RuntimeAssociationError, match="WORKSPACE_TASK_MISMATCH"):
        store.record(task_id=999, workspace_id=999, backend="tmux", node_id="hp-linux", native_session_id="x")


def test_source_backed_status_never_guesses_blocked_or_done(client, git_repo):
    root, repo = git_repo
    client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo", "default_branch": "main"})
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks/create", data={"title": "runtime", "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "LOW"})
    task = client.app.state.db.one("SELECT * FROM tasks ORDER BY id DESC LIMIT 1")
    ws = client.app.state.db.one("SELECT * FROM agent_workspaces WHERE task_id=?", (task["id"],))
    store = TerminalRuntimeAssociations(client.app.state.db)
    row = store.record(task_id=task["id"], workspace_id=ws["id"], backend="tmux",
                       node_id="hp-linux", native_session_id="claude-1")
    assert row["status"] == "unknown"
    assert row["status_source"] == "unobserved"
