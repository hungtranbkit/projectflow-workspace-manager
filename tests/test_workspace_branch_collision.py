"""agent_workspaces.branch is UNIQUE across the WHOLE table, not scoped
per repository_id -- a multi-repo Task using the same agent for a second
repo must not compute the identical `agent/<agent>/<task-slug>` branch
string for both repos, or the second workspace's INSERT fails with a raw
'UNIQUE constraint failed: agent_workspaces.branch' surfaced verbatim to
the browser as 'Action blocked' (reported bug: this happened from the
Task's own 'build config' / Cấu hình tab when adding a second repo's
Builder Workspace with the same agent already used on the first repo)."""
from __future__ import annotations

from tests.conftest import make_repo


def create_task_with_repo(client, rid, title="Multi repo feature"):
    r = client.post("/api/tasks/create", data={"title": title, "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("/")[-1])


def test_same_agent_on_two_repos_in_one_task_does_not_collide(client, git_repo):
    """The exact reported repro: one Task, two repos, the SAME agent used
    for both -- must not hit the DB's global branch UNIQUE constraint."""
    root, _ = git_repo
    repo_a = make_repo(root, "repo-a")
    repo_b = make_repo(root, "repo-b")
    client.post("/api/repositories", data={"repo_path": str(repo_a), "repo_name": "repo-a", "default_branch": "main"})
    client.post("/api/repositories", data={"repo_path": str(repo_b), "repo_name": "repo-b", "default_branch": "main"})
    rid_a = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "repo-a"][0]["id"]
    rid_b = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "repo-b"][0]["id"]

    tid = create_task_with_repo(client, rid_a)
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid_b, "agent": "claude", "role": "b", "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    assert r.status_code == 303, r.text  # must succeed, not 409 "Action blocked"

    workspaces = client.get(f"/api/tasks/{tid}").json()["workspaces"]
    assert len(workspaces) == 2
    branches = {w["branch"] for w in workspaces}
    assert len(branches) == 2  # distinct branches, no collision
    repos = {w["repository_id"] for w in workspaces}
    assert repos == {rid_a, rid_b}


def test_true_duplicate_workspace_gets_a_readable_error_not_raw_sql(client, git_repo):
    """Adding the SAME agent to the SAME repo twice for one Task is a
    genuine duplicate -- it must still be rejected, but never with a raw
    'UNIQUE constraint failed: ...' string leaking to the browser."""
    root, repo_path = git_repo
    client.post("/api/repositories", data={"repo_path": str(repo_path), "repo_name": "demo", "default_branch": "main"})
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task_with_repo(client, rid)
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "claude", "role": "dup", "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    assert r.status_code == 409
    assert "UNIQUE constraint failed" not in r.text
    assert "Action blocked" in r.text
