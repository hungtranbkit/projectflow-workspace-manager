"""Real Task #6 regression: "Start Runtime Verification" turned out to be
a dead anchor (`#qa`, an id that only exists inside the Advanced/legacy
"Review & QA" panel) instead of a real action, that same panel still said
"QA: NOT_REQUIRED for this risk profile" even though NORMAL now requires
Runtime Verification, the entire Integration ladder's hero target was
unconditionally overwritten to another dead anchor (`#integration`), and
Create PR was exposed in Advanced Merge Tracking with no readiness check
(the backend route already refused it, but the button dangled anyway).
Also covers a related latent bug found auditing the same code: a CLOSED/
STOPPED sandbox from a previous execution was misclassified as
"PROVISIONING" forever instead of offering Restart/Rebuild."""
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


def new_task(client, title, risk="NORMAL"):
    r = client.post("/api/tasks", data={"title": title, "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303
    return [t["id"] for t in client.get("/api/tasks").json() if t["title"] == title][0]


def add_workspace(client, tid, rid, agent="codex", role="Backend", sandbox_profile="NONE"):
    data = {"repository_id": rid, "agent": agent, "role": role, "base_branch": "main"}
    if sandbox_profile is not None:
        data["sandbox_profile"] = sandbox_profile
    r = client.post(f"/api/tasks/{tid}/workspaces", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return [w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["agent"] == agent and w["role"] == role][-1]


def submit_for_review(client, w):
    r = client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})
    assert r.status_code in (200, 303)


def review(client, w, result="PASS", reviewer="claude"):
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": reviewer})
    r = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": result}, follow_redirects=False)
    assert r.status_code == 303


def golden_to_qa(client, git_repo, risk="NORMAL", sandbox_profile="NONE"):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, f"Case {risk} {sandbox_profile}", risk=risk)
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, sandbox_profile=sandbox_profile)
    submit_for_review(client, w)
    review(client, w, "PASS")
    return tid, w


def ready_integration(client, git_repo, title="Integration ready"):
    tid, w = golden_to_qa(client, git_repo, risk="NORMAL")
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    r = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r.status_code == 303
    iid = client.get("/api/integrations").json()[0]["id"]
    return tid, w, iid


# =================================================== Start Runtime Verification

def test_start_runtime_verification_is_a_real_post_not_a_dead_anchor(client, git_repo):
    tid, w = golden_to_qa(client, git_repo, risk="NORMAL")
    d = decision(client, tid)
    assert d["current_step"] == "TEST_QA"
    na = d["next_action"]
    assert na["action"] == "START_QA"
    assert na["method"] == "POST"
    assert "#" not in na["target"]
    assert na["target"] == f"/api/tasks/{tid}/start-qa"
    # And it must actually work when clicked.
    r = client.post(na["target"], data={}, follow_redirects=False)
    assert r.status_code == 303
    d2 = decision(client, tid)
    assert d2["qa"] is not None and d2["qa"]["status"] == "RUNNING"


def test_qa_in_progress_has_no_redundant_dead_hero_button(client, git_repo):
    tid, w = golden_to_qa(client, git_repo, risk="NORMAL")
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})
    d = decision(client, tid)
    na = d["next_action"]
    assert na["action"] == "START_QA"
    # The real PASS/FAIL actions live in the already-visible wizard panel
    # -- the hero must not offer a second, dead-anchor button.
    assert na["target"] is None


def test_qa_review_panel_text_matches_normal_policy(client, git_repo):
    """The Advanced 'Review & QA' panel used to unconditionally say
    'QA: NOT_REQUIRED for this risk profile' for anything but HIGH --
    stale since NORMAL now requires Runtime Verification too."""
    tid, w = golden_to_qa(client, git_repo, risk="NORMAL")
    html = client.get(f"/tasks/{tid}").text
    assert "NOT_REQUIRED for this risk profile" not in html
    assert "QA skipped" not in html


def test_qa_review_panel_text_shows_not_required_for_low_risk(client, git_repo):
    tid, w = golden_to_qa(client, git_repo, risk="LOW")
    html = client.get(f"/tasks/{tid}").text
    assert "Runtime Verification: NOT_REQUIRED for this risk profile." in html


# ======================================================= Integration stale

def test_integration_source_stale_produces_refresh_integration_action(client, git_repo):
    tid, w, iid = ready_integration(client, git_repo)
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    d = decision(client, tid)
    assert d["status"] == "READY_FOR_MAIN"

    # A new, never-tested commit lands directly on the Integration
    # worktree -- out of band, no "Merge Latest Changes" click.
    irow = client.app.state.db.one("SELECT worktree_path FROM integration_workspaces WHERE id=?", (iid,))
    from pathlib import Path
    (Path(irow["worktree_path"]) / "late.txt").write_text("late change\n")
    run(irow["worktree_path"], "git", "add", ".")
    run(irow["worktree_path"], "git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "late change")

    d2 = decision(client, tid)
    assert d2["status"] != "READY_FOR_MAIN", "a stale READY_FOR_MAIN must not survive a new unmerged/untested commit"
    checklist = {c["key"]: c for c in d2["checklist"]}
    assert checklist["INTEGRATION"]["state"] != "done", "stale Integration must never render as completed"
    na = d2["next_action"]
    assert na["action"] == "RUN_INTEGRATION_TEST"
    assert na["label"] == "Refresh Integration"
    assert na["method"] == "POST"
    assert na["target"] == f"/api/integrations/{iid}/test"
    assert "#" not in na["target"]


def test_integration_refresh_invalidates_old_evidence_and_reruns(client, git_repo):
    tid, w, iid = ready_integration(client, git_repo)
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    before = client.app.state.db.one("SELECT verified_commit FROM integration_workspaces WHERE id=?", (iid,))

    irow = client.app.state.db.one("SELECT worktree_path FROM integration_workspaces WHERE id=?", (iid,))
    from pathlib import Path
    (Path(irow["worktree_path"]) / "late.txt").write_text("late change\n")
    run(irow["worktree_path"], "git", "add", ".")
    run(irow["worktree_path"], "git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "late change")

    d = decision(client, tid)
    r = client.post(d["next_action"]["target"], follow_redirects=False)
    assert r.status_code == 303
    # invalidate() ran as part of /test -- ready_for_main is cleared
    # until re-confirmed.
    row = client.app.state.db.one("SELECT ready_for_main,verified_commit FROM integration_workspaces WHERE id=?", (iid,))
    assert row["ready_for_main"] == 0

    client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    after = client.app.state.db.one("SELECT verified_commit FROM integration_workspaces WHERE id=?", (iid,))
    assert after["verified_commit"] != before["verified_commit"]
    d2 = decision(client, tid)
    assert d2["status"] == "READY_FOR_MAIN"
    checklist = {c["key"]: c for c in d2["checklist"]}
    assert checklist["INTEGRATION"]["state"] == "done"


def test_verification_pass_advances_to_integration(client, git_repo):
    tid, w = golden_to_qa(client, git_repo, risk="NORMAL")
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})
    r = client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"}, follow_redirects=False)
    assert r.status_code == 303
    d = decision(client, tid)
    assert d["current_step"] == "INTEGRATION"
    assert d["next_action"]["action"] == "CREATE_INTEGRATION"
    checklist = {c["key"]: c for c in d["checklist"]}
    assert checklist["RUNTIME_VERIFICATION"]["state"] == "done"
    assert checklist["INTEGRATION"]["state"] == "current"


def test_integration_pass_advances_to_prepare_pr(client, git_repo):
    tid, w, iid = ready_integration(client, git_repo)
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    d = decision(client, tid)
    assert d["status"] == "READY_FOR_MAIN"
    assert d["next_action"]["action"] == "PREPARE_PR"
    assert d["next_action"]["method"] == "POST"
    assert "#" not in d["next_action"]["target"]


# ================================================== Create PR guard =====

def test_create_pr_hidden_in_advanced_when_not_ready_for_main(client, git_repo):
    tid, w, iid = ready_integration(client, git_repo)
    # Integration exists but is not yet tested/ready -- Task is nowhere
    # near ready_for_main.
    html = client.get(f"/tasks/{tid}").text
    assert "Not ready for main yet" in html
    assert ">Create PR<" not in html


def test_create_pr_backend_blocked_while_not_ready(client, git_repo):
    tid, w, iid = ready_integration(client, git_repo)
    rid = client.app.state.db.one("SELECT repository_id FROM integration_workspaces WHERE id=?", (iid,))["repository_id"]
    r = client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    assert r.status_code != 303


# =============================================== Closed/stopped sandbox ==

def test_closed_sandbox_restarts_instead_of_duplicating(client, tmp_path):
    from tests.conftest import make_repo, NGINX_SANDBOX_CONTRACT, NGINX_COMPOSE
    root = tmp_path / "root"
    repo = make_repo(root, "closed-test", NGINX_SANDBOX_CONTRACT.format(lo=21500, hi=21519))
    (repo / "compose.yml").write_text(NGINX_COMPOSE)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "sandbox contract")
    register(client, repo, "closed-test")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Closed sandbox case", risk="NORMAL")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, sandbox_profile=None)
    submit_for_review(client, w)
    review(client, w, "PASS")
    client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    sid = sb["id"]

    # Simulate a previous execution's sandbox now cleaned up.
    client.app.state.db.execute("UPDATE sandboxes SET status='CLOSED' WHERE id=?", (sid,))

    d = decision(client, tid)
    na = d["next_action"]
    assert na["action"] == "RESTART_SANDBOX"
    assert na["method"] == "POST"
    assert na["target"] == f"/api/sandboxes/{sid}/start"

    r = client.post(na["target"], follow_redirects=False)
    assert r.status_code == 303
    # Same row restarted -- never a second sandbox created.
    all_sandboxes = client.app.state.db.all("SELECT id FROM sandboxes WHERE owner_type='AGENT_WORKSPACE' AND owner_id=?", (w["id"],))
    assert len(all_sandboxes) == 1
    row = client.app.state.db.one("SELECT status FROM sandboxes WHERE id=?", (sid,))
    assert row["status"] in ("RUNNING", "PROVISIONING", "STARTING")


def test_closed_stale_sandbox_needs_rebuild_not_plain_restart(client, tmp_path):
    from tests.conftest import make_repo, NGINX_SANDBOX_CONTRACT, NGINX_COMPOSE
    root = tmp_path / "root"
    repo = make_repo(root, "closed-stale-test", NGINX_SANDBOX_CONTRACT.format(lo=21520, hi=21539))
    (repo / "compose.yml").write_text(NGINX_COMPOSE)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "sandbox contract")
    register(client, repo, "closed-stale-test")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Closed stale sandbox case", risk="NORMAL")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid, sandbox_profile=None)
    submit_for_review(client, w)
    review(client, w, "PASS")
    client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    sid = sb["id"]
    client.app.state.db.execute("UPDATE sandboxes SET status='CLOSED' WHERE id=?", (sid,))

    # This sandbox's pinned source commit is now stale relative to the
    # Builder Workspace's real current HEAD -- set directly (not via a
    # new commit on the worktree, which would also stale the REVIEW gate
    # first and never reach the sandbox check at all; this isolates the
    # sandbox-staleness check specifically, same as the real-world case
    # of a sandbox surviving across a legitimate re-review/re-submit).
    client.app.state.db.execute("UPDATE sandbox_sources SET commit_sha=? WHERE sandbox_id=?", ("f" * 40, sid))

    d = decision(client, tid)
    na = d["next_action"]
    assert na["action"] == "REBUILD_SANDBOX"
    assert na["method"] == "POST"
    assert na["target"] == f"/api/sandboxes/{sid}/rebuild"


# ============================================ Exactly one primary action =

def test_exactly_one_primary_hero_action_rendered(client, git_repo):
    tid, w = golden_to_qa(client, git_repo, risk="NORMAL")
    html = client.get(f"/tasks/{tid}").text
    assert html.count("hero-primary") == 1
