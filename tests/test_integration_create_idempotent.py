"""Real incident regression: a duplicate Create Integration click must
never orphan a task_integrations row. Before this fix, create_task_
integration() inserted the parent task_integrations row BEFORE attempting
git.create_integration() with a deterministic branch name -- a second
call while the first call's branch already existed on disk left an
orphaned, childless task_integrations row that decision.task_integration()
(ORDER BY id DESC) then silently shadowed the real, working integration
with -- exactly what happened to Task #6 in production ("Fix giao diện
qa-"): the second click errored "Branch already exists" but had already
committed the broken row, so the next page load showed a Task stuck with
no real Integration data even though one existed underneath."""
from __future__ import annotations
import subprocess


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def decision(client, tid):
    return client.get(f"/api/tasks/{tid}/decision").json()


def new_task(client, title, risk="NORMAL"):
    r = client.post("/api/tasks", data={"title": title, "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303
    return [t["id"] for t in client.get("/api/tasks").json() if t["title"] == title][0]


def add_workspace(client, tid, rid, agent="codex", role="Backend"):
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": agent, "role": role, "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return [w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["agent"] == agent and w["role"] == role][-1]


def submit_for_review(client, w):
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})
    assert r.status_code in (200, 303)


def review(client, w, result="PASS", reviewer="claude"):
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": reviewer})
    r = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": result}, follow_redirects=False)
    assert r.status_code == 303


def test_duplicate_create_integration_click_is_a_no_op_not_a_broken_orphan(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Duplicate click", risk="NORMAL")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")

    r1 = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r1.status_code == 303
    d1 = decision(client, tid)
    first_ti_id = d1["task_integration"]["id"]
    assert d1["task_integration"] is not None
    assert d1["integration_repos"]  # real child row exists

    # The duplicate click: must NOT raise (no "branch already exists"),
    # must NOT create a second task_integrations row, and the Task must
    # still show the SAME, real, intact integration afterward.
    r2 = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r2.status_code == 303

    d2 = decision(client, tid)
    assert d2["task_integration"]["id"] == first_ti_id
    assert d2["integration_repos"]
    assert d2["integration_repos"][0]["status"] in ("TESTING", "CONFLICT")

    all_tis = client.app.state.db.all("SELECT id FROM task_integrations WHERE task_id=?", (tid,))
    assert len(all_tis) == 1, "duplicate click must never create a second task_integrations row"
