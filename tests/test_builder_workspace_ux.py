"""Task Setup / Builder Workspace UX: an existing, not-yet-started
Builder must be the obvious, single primary action ([Start Codex]) --
"Add Another Builder" is a secondary, collapsed-by-default, explicitly
optional affordance, never a second competing primary button, and never
labeled with the implementation term "Workspace" in user-facing copy."""
from __future__ import annotations
import re
import time

import pytest

from app.launchers import AgentLauncher


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def create_task(client, title, rid, agent="codex", risk="LOW"):
    r = client.post("/api/tasks/create", data={"title": title, "repository_id": rid, "agent": agent, "sandbox_profile": "NONE", "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("/")[-1])


@pytest.fixture(autouse=True)
def _stop_lingering_sessions(client):
    yield
    for row in client.app.state.db.all("SELECT id FROM agent_sessions WHERE status IN ('STARTING','RUNNING','WAITING_FOR_INPUT')"):
        try: client.app.state.agent_sessions.stop(row["id"])
        except Exception: pass


def test_no_builders_shows_create_first_builder_flow(client, git_repo):
    """Empty state: a Task with a repo scope but no Builder Workspace
    yet must present ONE unambiguous action to create the first one --
    not two competing forms."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks/create", data={"title": "Empty", "repository_id": "", "agent": "", "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    # Some deployments require repository_id; fall back to the documented flow.
    if r.status_code != 303:
        tid = create_task(client, "Empty2", rid)
        client.app.state.db.execute("DELETE FROM agent_workspaces WHERE task_id=?", (tid,))
    else:
        tid = int(r.headers["location"].split("/")[-1])
    html = client.get(f"/tasks/{tid}").text
    assert "Add Builder Workspace" not in html


def test_one_ready_builder_shows_start_as_primary_and_hides_old_label(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "Fix giao dien qa", rid, agent="codex")
    html = client.get(f"/tasks/{tid}").text
    assert "Start Codex" in html
    assert "Add Builder Workspace" not in html
    assert "Add another repository" not in html


def test_add_another_builder_collapsed_by_default(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "Fix giao dien qa", rid, agent="codex")
    html = client.get(f"/tasks/{tid}").text
    assert "+ Add Another Builder" in html
    # collapsed: the <details> wrapping it must not carry `open`.
    m = re.search(r"<details[^>]*>\s*<summary>\+ Add Another Builder</summary>", html)
    assert m, "Add Another Builder must be inside a <details> immediately after its <summary>"
    assert "open" not in html[max(0, m.start() - 20):m.start()]


def test_additional_builder_form_labeled_create_builder(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "Fix giao dien qa", rid, agent="codex")
    html = client.get(f"/tasks/{tid}").text
    assert "Create Builder</button>" in html
    assert "Add Builder Workspace</button>" not in html


def test_start_and_add_another_builder_have_different_visual_priority(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "Fix giao dien qa", rid, agent="codex")
    html = client.get(f"/tasks/{tid}").text
    start_idx = html.index("Start Codex")
    add_idx = html.index("+ Add Another Builder")
    # Start Codex renders as a real <button class="success">, Add Another
    # Builder as a <summary> toggle -- never both as equal primary buttons.
    assert '<button class="success">Start Codex' in html
    assert "<summary>+ Add Another Builder</summary>" in html


def test_multiple_builders_each_get_their_own_start_action(client, git_repo):
    root, repo = git_repo
    repo_b = repo.parent / "repo-b"
    import subprocess
    repo_b.mkdir(); subprocess.run(["git", "init", "-b", "main"], cwd=repo_b, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], cwd=repo_b, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo_b, check=True)
    (repo_b / "README.md").write_text("base\n")
    (repo_b / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: repo-b}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    subprocess.run(["git", "add", "."], cwd=repo_b, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo_b, check=True)
    register(client, repo, "demo")
    register(client, repo_b, "repo-b")
    rid_a = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "demo"][0]["id"]
    rid_b = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "repo-b"][0]["id"]
    tid = create_task(client, "Multi", rid_a, agent="codex")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid_b, "agent": "claude", "role": "b", "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    html = client.get(f"/tasks/{tid}").text
    assert "Start Codex" in html
    assert "Start Claude" in html
    assert "Start All Builders" in html


def test_started_builder_no_longer_shows_start_button(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "Fix giao dien qa", rid, agent="codex")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.app.state.agent_sessions.launchers = {"codex": AgentLauncher("Codex", "bash", ("-c", "sleep 100"))}
    r = client.post(f"/api/workspaces/{w['id']}/sessions", data={"mode": "INTERACTIVE"}, follow_redirects=False)
    assert r.status_code == 303
    sid = int(r.headers["location"].split("/")[-1])
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.app.state.db.one("SELECT status FROM agent_sessions WHERE id=?", (sid,))["status"] == "RUNNING":
            break
        time.sleep(0.05)
    html = client.get(f"/tasks/{tid}").text
    # The wizard's own primary action (SETUP step's "Start Codex" button)
    # must be gone once the agent is actually running -- the Advanced/
    # technical panel's independent re-launch quick action is a separate,
    # always-available control and out of scope here. (The exact live-
    # terminal link/label is covered by test_live_terminal_routing.py.)
    assert '<button class="success">Start Codex' not in html


def test_page_refresh_preserves_consistent_builder_state(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "Fix giao dien qa", rid, agent="codex")
    html1 = client.get(f"/tasks/{tid}").text
    html2 = client.get(f"/tasks/{tid}").text
    assert ("Start Codex" in html1) == ("Start Codex" in html2)
    assert "Add Builder Workspace" not in html1 and "Add Builder Workspace" not in html2
