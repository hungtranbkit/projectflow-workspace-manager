"""Task-centric UX: Task is the primary object, Agent Workspace is a
child. Real git + real docker (nginx:alpine, no network pull) throughout,
same fixtures as test_task_sandbox_views.py / test_verification_ux.py."""
from __future__ import annotations
import shutil
import subprocess
import time
from urllib.parse import urlencode

import pytest

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not on PATH")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


if not _docker_available():
    pytestmark = pytest.mark.skip(reason="docker daemon not reachable in this environment")


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def post_multi(client, url, pairs, **kw):
    """POST a form body with repeated field names (New Task's ws_* arrays)
    -- httpx's TestClient does not reliably urlencode a list-of-tuples via
    `data=`, so build the body explicitly."""
    body = urlencode(pairs)
    return client.post(url, content=body, headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=kw.get("follow_redirects", True))


def wait_agent_tests(client, wid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = client.app.state.db.all("SELECT status FROM test_runs WHERE workspace_type='agent' AND workspace_id=?", (wid,))
        if runs and all(x["status"] not in ("QUEUED", "RUNNING") for x in runs): return
        time.sleep(.05)
    raise AssertionError("agent test did not finish")


@pytest.fixture
def cleanup_sandboxes(client):
    created = []
    yield created
    for sid in created:
        client.post(f"/api/sandboxes/{sid}/cleanup")


def test_new_task_creates_at_least_one_agent_workspace_with_real_branch_and_worktree(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "Fix kiosk session"), ("description", "d"),
        ("ws_repository_id", str(rid)), ("ws_agent", "claude"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", ""),
    ], follow_redirects=False)
    assert r.status_code == 303
    tid = client.get("/api/tasks").json()[0]["id"]
    ws = client.get("/api/workspaces").json()
    assert len(ws) == 1
    w = ws[0]
    assert w["task_id"] == tid
    from pathlib import Path
    assert Path(w["worktree_path"]).is_dir()
    assert run(w["worktree_path"], "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == w["branch"]


def test_task_can_have_multiple_workspaces_across_multiple_repos(client, git_repo):
    root, _ = git_repo
    r1 = root / "backend"; r1.mkdir()
    run(r1, "git", "init", "-b", "main"); run(r1, "git", "config", "user.email", "t@t"); run(r1, "git", "config", "user.name", "t")
    (r1 / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: backend}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (r1 / "README.md").write_text("x\n"); run(r1, "git", "add", "."); run(r1, "git", "commit", "-m", "base")
    r2 = root / "firmware"; r2.mkdir()
    run(r2, "git", "init", "-b", "main"); run(r2, "git", "config", "user.email", "t@t"); run(r2, "git", "config", "user.name", "t")
    (r2 / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: firmware}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (r2 / "README.md").write_text("x\n"); run(r2, "git", "add", "."); run(r2, "git", "commit", "-m", "base")
    register(client, r1, "backend"); register(client, r2, "firmware")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}

    r = post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "Cross-repo task"), ("description", ""),
        ("ws_repository_id", str(repos["backend"])), ("ws_agent", "claude"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", ""),
        ("ws_repository_id", str(repos["firmware"])), ("ws_agent", "codex"), ("ws_role", "Firmware"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "NONE"),
    ], follow_redirects=False)
    assert r.status_code == 303
    tid = client.get("/api/tasks").json()[0]["id"]
    ws = client.get(f"/api/tasks/{tid}").json()["workspaces"]
    assert len(ws) == 2
    assert {w["agent"] for w in ws} == {"claude", "codex"}
    assert {w["repository_id"] for w in ws} == set(repos.values())
    page = client.get(f"/tasks/{tid}").text
    assert "backend" in page and "firmware" in page


def test_failed_workspace_does_not_roll_back_task_or_successful_workspace(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "Partial failure task"), ("description", ""),
        ("ws_repository_id", str(rid)), ("ws_agent", "claude"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", ""),
        ("ws_repository_id", "999999"), ("ws_agent", "codex"), ("ws_role", "Bad"), ("ws_base_branch", "main"), ("ws_sandbox_profile", ""),
    ], follow_redirects=False)
    assert r.status_code == 303
    tid = client.get("/api/tasks").json()[0]["id"]
    assert client.get(f"/api/tasks/{tid}").json() is not None  # Task itself remains
    ws = client.get(f"/api/tasks/{tid}").json()["workspaces"]
    assert len(ws) == 1 and ws[0]["agent"] == "claude"  # successful workspace remains
    events = client.app.state.db.all("SELECT * FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='WORKSPACE_CREATE_FAILED'", (tid,))
    assert events  # failure clearly recorded, not hidden


def test_legacy_workspace_can_attach_to_existing_task_without_new_worktree(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/workspaces", data={"repository_id": rid, "agent": "codex", "task_name": "legacy-work", "base_branch": "main"})
    w = client.get("/api/workspaces").json()[0]
    assert w["task_id"] is None
    worktree_before = w["worktree_path"]

    page = client.get("/workspaces").text
    assert "UNASSIGNED WORKSPACE" in page
    assert "Create Task from Workspace" in page

    client.post("/api/tasks", data={"title": "Existing task"}, follow_redirects=False)
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Existing task"][0]
    r = client.post(f"/api/workspaces/{w['id']}/attach-task", data={"task_id": tid}, follow_redirects=False)
    assert r.status_code == 303
    w2 = client.get(f"/api/workspaces/{w['id']}").json()
    assert w2["task_id"] == tid
    assert w2["worktree_path"] == worktree_before  # no duplicate worktree created
    assert client.get("/api/workspaces").json().__len__() == 1  # still exactly one workspace row


def test_create_task_from_workspace_prefills_title_and_attaches_without_new_worktree(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/workspaces", data={"repository_id": rid, "agent": "claude", "task_name": "update-default", "base_branch": "main"})
    w = client.get("/api/workspaces").json()[0]
    worktree_before = w["worktree_path"]

    r = client.post(f"/api/workspaces/{w['id']}/create-task", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/tasks/")
    tid = int(r.headers["location"].split("/")[-1])
    t = client.get(f"/api/tasks/{tid}").json()
    assert "update default" in t["title"].lower()  # task_name slug read as a human title
    w2 = client.get(f"/api/workspaces/{w['id']}").json()
    assert w2["task_id"] == tid and w2["worktree_path"] == worktree_before


def test_kanban_maps_task_states_to_correct_columns(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "kanban-repo", port_range=(21900, 21919))
    register(client, repo, "kanban-repo")
    rid = client.get("/api/repositories").json()[0]["id"]

    # BACKLOG: draft task, no workspace
    client.post("/api/tasks", data={"title": "Draft task"}, follow_redirects=False)
    tid_backlog = client.get("/api/tasks").json()[0]["id"]
    assert client.get(f"/api/tasks/{tid_backlog}/decision").json()["status"] == "BACKLOG"

    # DEVELOPMENT: task with a CODING (not-yet-Ready) workspace
    post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "Dev task"), ("description", ""),
        ("ws_repository_id", str(rid)), ("ws_agent", "codex"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "NONE"),
    ])
    tid_dev = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "Dev task"][0]
    assert client.get(f"/api/tasks/{tid_dev}/decision").json()["stage"] == "DEVELOPMENT"

    kanban = client.get("/kanban").text
    board_positions = {col: kanban.find(f">{col} <") for col in ["Backlog", "Development", "Review / QA", "Integration", "Ready for Main", "Done", "Blocked"]}
    draft_pos = kanban.find("Draft task")
    dev_pos = kanban.find("Dev task")
    assert board_positions["Backlog"] != -1 and board_positions["Backlog"] < draft_pos < board_positions["Development"]
    assert board_positions["Development"] < dev_pos < board_positions["Review / QA"]

    # All required repos merged (LOW risk -> no QA/Integration needed) -> DONE, computed live
    client.app.state.db.execute("UPDATE tasks SET status='CANCELLED' WHERE id=?", (tid_backlog,))  # keep BACKLOG task out of the way
    w = client.get(f"/api/tasks/{tid_dev}").json()["workspaces"][0]
    client.post(f"/api/tasks/{tid_dev}/brief", data={"goal": "g", "acceptance_criteria": "a", "risk_profile": "LOW"})
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    client.post(f"/api/tasks/{tid_dev}/mark-merged")
    assert client.get(f"/api/tasks/{tid_dev}/decision").json()["status"] == "DONE"
    kanban2 = client.get("/kanban").text
    done_pos = kanban2.find(">Done <")
    dev_pos2 = kanban2.find("Dev task")
    assert done_pos < dev_pos2


def test_task_detail_aggregates_child_state_and_never_duplicates_it(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "agg-repo", port_range=(21920, 21939))
    register(client, repo, "agg-repo")
    rid = client.get("/api/repositories").json()[0]["id"]
    post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "Aggregate task"), ("description", ""),
        ("ws_repository_id", str(rid)), ("ws_agent", "claude"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "backend"),
    ])
    tid = client.get("/api/tasks").json()[0]["id"]
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])

    page = client.get(f"/tasks/{tid}").text
    outputs = client.app.state.sandboxes.outputs(sb["id"])
    assert outputs["backend_url"] in page  # same Sandbox record, no duplicated URL state
    assert "0 / 1" in page  # agents ready summary derived, not stored


def test_test_readiness_no_partial_yes(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    repoA = sandboxable_repo_factory(root, "readiness-a", port_range=(21940, 21959))
    repoB = root / "readiness-b"; repoB.mkdir()
    run(repoB, "git", "init", "-b", "main"); run(repoB, "git", "config", "user.email", "t@t"); run(repoB, "git", "config", "user.name", "t")
    (repoB / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: readiness-b}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (repoB / "README.md").write_text("x\n"); run(repoB, "git", "add", "."); run(repoB, "git", "commit", "-m", "base")
    register(client, repoA, "readiness-a"); register(client, repoB, "readiness-b")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}

    r = post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "Readiness task"), ("description", ""),
        ("ws_repository_id", str(repos["readiness-a"])), ("ws_agent", "claude"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "backend"),
        ("ws_repository_id", str(repos["readiness-b"])), ("ws_agent", "codex"), ("ws_role", "Firmware"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "NONE"),
    ], follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])
    ws = client.get(f"/api/tasks/{tid}").json()["workspaces"]

    # neither workspace submitted for review yet -> NO
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["test_readiness"] == "NO"
    page = client.get(f"/tasks/{tid}").text
    assert "Test Readiness</small><code>NO</code>" in page

    # mark only ONE workspace ready (submitted for review) -> PARTIAL
    ready_ws = next(w for w in ws if w["agent"] == "codex")
    client.post(f"/api/workspaces/{ready_ws['id']}/ready")
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["test_readiness"] == "PARTIAL"

    # mark the other workspace ready too -> YES, every Builder submitted
    backend_ws = next(w for w in ws if w["agent"] == "claude")
    client.post(f"/api/workspaces/{backend_ws['id']}/ready")
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["test_readiness"] == "YES"


def test_blocking_workspace_shown_and_next_action_progresses(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    """The Task-level next_action (TaskDecisionService) progresses through
    START_BUILDER -> SUBMIT_FOR_REVIEW -> START_REVIEW -> CREATE_INTEGRATION
    as the one Builder Workspace moves through the gate model. (The
    older per-sandbox "Run Tests"/"Open App and Verify" steps still exist,
    but now live only on the Workspace's own Runtime section, a separate
    concern from this Task-level gate engine -- section 39.)"""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "block-repo", port_range=(21960, 21979))
    register(client, repo, "block-repo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "Block task"), ("description", ""),
        ("ws_repository_id", str(rid)), ("ws_agent", "codex"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "backend"),
    ], follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])

    page = client.get(f"/tasks/{tid}").text
    assert "codex" in page
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["next_action"]["action"] == "START_BUILDER"

    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["next_action"]["action"] == "SUBMIT_FOR_REVIEW"  # builder READY, no review started

    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["next_action"]["action"] == "START_REVIEW"  # review in progress

    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["next_action"]["action"] == "START_QA"  # NORMAL risk, review PASS -- Runtime Verification next (sandbox already auto-created)
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["next_action"]["action"] == "CREATE_INTEGRATION"
    page = client.get(f"/tasks/{tid}").text
    assert "Create Integration" in page


def test_ready_workspace_never_makes_task_ready_for_main(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "gate-repo", port_range=(21980, 21999))
    register(client, repo, "gate-repo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "Gate task"), ("description", ""),
        ("ws_repository_id", str(rid)), ("ws_agent", "claude"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "backend"),
    ], follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])
    client.post(f"/api/workspaces/{w['id']}/ready")

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["ready_for_main"] is False  # workspace READY (agent thinks it's done) is not a review PASS, let alone Integration
    assert d["status"] != "READY_FOR_MAIN"


def test_create_integration_gated_with_reason_when_not_all_ready(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    root, _ = git_repo
    repoA = sandboxable_repo_factory(root, "gatereason-a", port_range=(22000, 22019))
    repoB = root / "gatereason-b"; repoB.mkdir()
    run(repoB, "git", "init", "-b", "main"); run(repoB, "git", "config", "user.email", "t@t"); run(repoB, "git", "config", "user.name", "t")
    (repoB / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: b}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (repoB / "README.md").write_text("x\n"); run(repoB, "git", "add", "."); run(repoB, "git", "commit", "-m", "base")
    register(client, repoA, "gatereason-a"); register(client, repoB, "gatereason-b")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}
    r = post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "Gate reason task"), ("description", ""),
        ("ws_repository_id", str(repos["gatereason-a"])), ("ws_agent", "claude"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "backend"),
        ("ws_repository_id", str(repos["gatereason-b"])), ("ws_agent", "codex"), ("ws_role", "ESP"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "NONE"),
    ], follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])

    page = client.get(f"/tasks/{tid}").text
    assert "Create Integration disabled" in page
    assert "Waiting for" in page and "codex" in page

    # READY alone (agent thinks it's done) is not enough -- eligibility
    # requires every Builder's review to be PASS (section 22).
    ws = client.get(f"/api/tasks/{tid}").json()["workspaces"]
    for w in ws:
        client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    page = client.get(f"/tasks/{tid}").text
    assert "Create Integration disabled" in page  # still disabled -- no reviews yet

    for w in ws:
        client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
        client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    page = client.get(f"/tasks/{tid}").text
    assert "Create Integration disabled" not in page
    assert 'action="/api/tasks/%s/integrations"' % tid in page


def test_ready_for_main_task_enters_ready_for_main_column(client, git_repo, sandboxable_repo_factory, cleanup_sandboxes):
    """Regression: task_integrations.ready_for_main is never written by any
    route -- Kanban/List must compute READY_FOR_MAIN live (the same way
    Task Detail does), not read that always-zero column."""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "rfm-repo", port_range=(22040, 22059))
    register(client, repo, "rfm-repo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = post_multi(client, "/api/tasks/new-with-workspace", [
        ("title", "RFM column task"), ("description", ""),
        ("ws_repository_id", str(rid)), ("ws_agent", "claude"), ("ws_role", "Backend"), ("ws_base_branch", "main"), ("ws_sandbox_profile", "backend"),
    ], follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})  # NORMAL now also requires Runtime Verification
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    client.post(f"/api/tasks/{tid}/integrations")
    for sb in client.get("/api/sandboxes").json(): cleanup_sandboxes.append(sb["id"])
    iid = client.get("/api/integrations").json()[0]["id"]
    client.post(f"/api/integrations/{iid}/test")
    for _ in range(100):
        if client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"] != "TESTING": break
        time.sleep(.05)
    client.post(f"/api/integrations/{iid}/ready-for-main")

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "READY_FOR_MAIN" and d["ready_for_main"] is True

    kanban = client.get("/kanban").text
    rfm_pos = kanban.find(">Ready for Main <")
    done_pos = kanban.find(">Done <")
    title_pos = kanban.find("RFM column task")
    assert rfm_pos != -1 and rfm_pos < title_pos < done_pos


def test_merged_task_enters_done_column(client, git_repo):
    """DONE is computed purely from MergeRecord state (section 25/41) --
    never from a raw tasks.status write. LOW risk here so Review PASS is
    the only required gate before every repo can be marked merged."""
    root, repo = git_repo
    register(client, repo, "demo")
    client.post("/api/tasks", data={"title": "To be merged", "risk_profile": "LOW"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "base_branch": "main"})
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    client.post(f"/api/tasks/{tid}/mark-merged")
    assert client.get(f"/api/tasks/{tid}/decision").json()["status"] == "DONE"

    kanban = client.get("/kanban").text
    done_idx = kanban.find(">Done <")
    title_idx = kanban.find("To be merged")
    assert 0 <= done_idx < title_idx
