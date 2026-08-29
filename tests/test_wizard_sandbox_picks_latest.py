"""Real incident found verifying the Task #6 regression live: after a
workspace's sandbox was recreated (its old CLOSED row still on file, a
newer RUNNING one now owns the workspace), the Task Detail wizard's
Runtime Verification panel showed the OLD sandbox's dead port ("Sandbox
CLOSED", stale Open App link) while TaskDecisionService's own next_action
correctly pointed at the new, RUNNING one -- two places computing "this
workspace's sandbox" disagreed. Root cause: task_detail()'s inline
`next(s for s in sbxs if ...)` walked sbxs in ORDER BY id ASC (its oldest
match), unlike sandbox_for_workspace() (used everywhere else) which
already correctly orders DESC."""
from __future__ import annotations
import subprocess


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def test_wizard_shows_the_latest_sandbox_not_an_old_closed_one(client, tmp_path):
    from tests.conftest import make_repo, NGINX_SANDBOX_CONTRACT, NGINX_COMPOSE
    root = tmp_path / "root"
    repo = make_repo(root, "wizard-sandbox-test", NGINX_SANDBOX_CONTRACT.format(lo=21540, hi=21559))
    (repo / "compose.yml").write_text(NGINX_COMPOSE)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "sandbox contract")
    register(client, repo, "wizard-sandbox-test")
    rid = client.get("/api/repositories").json()[0]["id"]

    r = client.post("/api/tasks", data={"title": "Wizard sandbox pick", "risk_profile": "NORMAL"}, follow_redirects=False)
    assert r.status_code == 303
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Wizard sandbox pick"][0]
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main"}, follow_redirects=False)
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"}, follow_redirects=False)

    # First sandbox, now closed (a previous execution).
    client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={}, follow_redirects=False)
    old_sb = client.get("/api/sandboxes").json()[0]
    client.app.state.db.execute("UPDATE sandboxes SET status='CLOSED' WHERE id=?", (old_sb["id"],))

    # A genuinely new sandbox for the same workspace (simulating Restart-
    # after-CLOSED producing a fresh row, or a rebuild flow) -- higher id,
    # RUNNING, a different port.
    db = client.app.state.db
    new_id = db.execute(
        "INSERT INTO sandboxes(repository_id,owner_type,owner_id,sandbox_slug,profile,compose_project,status,health_status,source_manifest_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (rid, "AGENT_WORKSPACE", w["id"], "wizard-sandbox-test-new-slug", "BACKEND", "wm-wizard-sandbox-test-new-proj", "RUNNING", "HEALTHY",
         '{"outputs": {"backend_url": "http://127.0.0.1:29999"}}'),
    )
    assert new_id > old_sb["id"]

    html = client.get(f"/tasks/{tid}").text
    assert "http://127.0.0.1:29999" in html, "wizard must show the LATEST sandbox's URL, not the closed one's"
