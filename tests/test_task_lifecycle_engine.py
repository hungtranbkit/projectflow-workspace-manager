"""Task Lifecycle & Gate Model Refactor: TaskDecisionService is the single
source for Task status/stage/next-action/gate-eligibility (spec sections
30-34). These tests exercise the real HTTP API + real git worktrees but
deliberately use sandbox_profile=NONE everywhere so they need no Docker at
all -- the state engine itself has nothing to do with sandboxes/docker,
and this file must run in every CI environment, not just docker-enabled
ones (unlike test_control_plane.py / test_task_centric_ux.py, which are
docker-gated because they also exercise real sandboxes).

Sections covered: 50 (state-engine cases), 51 (policy/gate tests), 52
(exact-commit staleness), 53 (brief-version staleness)."""
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


# ================================================================== 50
# State-engine cases: one Task walked through the golden flow, asserting
# TaskDecisionService's computed status/stage/next_action at each step.

def test_01_backlog_no_workspace(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Case 1")
    d = decision(client, tid)
    assert d["status"] == "BACKLOG" and d["stage"] == "PLANNING"
    assert d["next_action"]["action"] == "SELECT_FOR_DEVELOPMENT"
    assert d["builders"] == [] and d["ready_for_main"] is False


def test_02_selected_no_brief_no_workspace(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Case 2")
    client.post(f"/api/tasks/{tid}/select")
    d = decision(client, tid)
    assert d["status"] == "ACTIVE" and d["stage"] == "PLANNING"
    assert d["next_action"]["action"] == "COMPLETE_BRIEF"


def test_03_brief_complete_no_workspace(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Case 3")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/brief", data={"goal": "g", "acceptance_criteria": "a"})
    d = decision(client, tid)
    assert d["next_action"]["action"] == "CREATE_BUILDER_WORKSPACE"


def test_04_workspace_created_not_submitted(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Case 4")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    d = decision(client, tid)
    assert d["stage"] == "DEVELOPMENT"
    assert d["next_action"]["action"] == "OPEN_BUILDER"
    assert d["builders"][0]["ready"] is False


def test_05_submitted_no_review(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Case 5")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    d = decision(client, tid)
    assert d["stage"] == "REVIEW"
    assert d["next_action"]["action"] == "SUBMIT_FOR_REVIEW"
    assert d["builders"][0]["ready"] is True and d["builders"][0]["review_status"] == "NONE"


def test_06_review_running(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Case 6")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    d = decision(client, tid)
    assert d["next_action"]["action"] == "START_REVIEW"
    assert d["builders"][0]["review_status"] == "RUNNING"


def test_07_low_risk_review_pass_is_ready_for_main(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Case 7", risk="LOW")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    d = decision(client, tid)
    assert d["status"] == "READY_FOR_MAIN" and d["stage"] == "MERGING"
    assert not d["qa"] and not d["task_integration"]


def test_08_normal_risk_review_pass_needs_integration(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Case 8", risk="NORMAL")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    d = decision(client, tid)
    assert d["stage"] == "INTEGRATION"
    assert d["next_action"]["action"] == "CREATE_INTEGRATION"
    assert d["status"] == "ACTIVE"  # not READY_FOR_MAIN -- Integration still required and missing


def test_09_fix_required_blocks_task(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Case 9")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "FIX_REQUIRED")
    d = decision(client, tid)
    assert d["status"] == "BLOCKED"
    assert d["next_action"]["action"] == "RETURN_TO_BUILDER"
    assert d["blocking_reasons"]


def test_10_high_risk_requires_qa_before_integration(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Case 10", risk="HIGH")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    d = decision(client, tid)
    assert d["stage"] == "QA"
    assert d["next_action"]["action"] == "START_QA"


def test_11_high_risk_qa_pass_moves_to_integration(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Case 11", risk="HIGH")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "codex"})
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    d = decision(client, tid)
    assert d["qa"]["status"] == "PASS"
    assert d["stage"] == "INTEGRATION"
    assert d["next_action"]["action"] == "CREATE_INTEGRATION"


def test_12_cross_repo_partial_merge_stays_active(client, git_repo):
    """Section 25's explicit example: backend merged, second repo not ->
    Task must NOT be DONE."""
    root, repo = git_repo
    other = root / "second"; other.mkdir()
    run(other, "git", "init", "-b", "main"); run(other, "git", "config", "user.email", "t@t"); run(other, "git", "config", "user.name", "t")
    (other / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: second}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (other / "README.md").write_text("x\n"); run(other, "git", "add", "."); run(other, "git", "commit", "-m", "base")
    register(client, repo, "demo"); register(client, other, "second")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}
    tid = new_task(client, "Case 12", risk="LOW")
    client.post(f"/api/tasks/{tid}/select")
    w1 = add_workspace(client, tid, repos["demo"], agent="claude", role="Backend")
    w2 = add_workspace(client, tid, repos["second"], agent="codex", role="ESP")
    for w in (w1, w2):
        submit_for_review(client, w)
        review(client, w, "PASS")
    d = decision(client, tid)
    assert d["status"] == "READY_FOR_MAIN"
    client.post(f"/api/tasks/{tid}/merges/{repos['demo']}/mark-merged")
    d2 = decision(client, tid)
    assert d2["status"] != "DONE"
    assert d2["status"] in ("READY_FOR_MAIN", "ACTIVE")
    merged = {m["repository_id"]: m["merge_status"] for m in d2["merge_records"]}
    assert merged[repos["demo"]] == "MERGED" and merged[repos["second"]] != "MERGED"


def test_13_all_required_merges_done_reaches_done(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Case 13", risk="LOW")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    client.post(f"/api/tasks/{tid}/mark-merged")
    d = decision(client, tid)
    assert d["status"] == "DONE" and d["stage"] == "COMPLETE"
    assert d["next_action"]["action"] == "CLOSE_TASK"
    assert client.get(f"/api/tasks/{tid}").json()["status"] == "ACTIVE"  # never persisted as DONE


def test_14_cancelled_task(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "Case 14")
    client.post(f"/api/tasks/{tid}/cancel")
    d = decision(client, tid)
    assert d["status"] == "CANCELLED" and d["stage"] == "COMPLETE"
    assert d["next_action"]["action"] == "NONE"


# ================================================================== 51
# Risk/gate policy: LOW/NORMAL/HIGH, and NOT_REQUIRED gates never read as
# a false PASS.

@pytest.mark.parametrize("risk,expects_qa,expects_integration", [("LOW", False, False), ("NORMAL", False, True), ("HIGH", True, True)])
def test_risk_policy_gate_requirements(client, git_repo, risk, expects_qa, expects_integration):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, f"Policy {risk}", risk=risk)
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    d = decision(client, tid)
    if not expects_qa and not expects_integration:
        assert d["status"] == "READY_FOR_MAIN"
    elif expects_qa:
        assert d["stage"] == "QA"
    else:
        assert d["stage"] == "INTEGRATION"


def test_project_default_risk_profile_is_normal_when_unspecified(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    r = client.post("/api/tasks", data={"title": "No explicit risk"}, follow_redirects=False)
    assert r.status_code == 303
    tid = [t["id"] for t in client.get("/api/tasks").json() if t["title"] == "No explicit risk"][0]
    assert client.get(f"/api/tasks/{tid}").json()["risk_profile"] == "NORMAL"


def test_task_detail_gate_checklist_marks_skipped_gates_not_required_not_pass(client, git_repo):
    """A LOW-risk Task's QA/Integration gates must render as explicitly
    NOT_REQUIRED, never silently checked off as if they PASSed."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Gate checklist", risk="LOW")
    client.post(f"/api/tasks/{tid}/select")
    add_workspace(client, tid, rid)
    page = client.get(f"/tasks/{tid}").text
    assert "NOT_REQUIRED for this risk profile" in page


# ================================================================== 52
# Exact-commit staleness: Review/QA PASS must not survive a moved commit.

def test_review_pass_stale_after_new_commit_on_branch(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Stale review")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    assert decision(client, tid)["builders"][0]["review_status"] == "PASS"

    (client.app.state.git.validate_worktree(w["worktree_path"]) / "more.txt").write_text("x\n")
    run(w["worktree_path"], "git", "add", "."); run(w["worktree_path"], "git", "commit", "-m", "more")
    d = decision(client, tid)
    assert d["builders"][0]["review_status"] == "STALE"
    assert d["status"] != "READY_FOR_MAIN"
    assert d["next_action"]["action"] == "START_REVIEW"  # re-review, not silently reused


def test_qa_pass_stale_after_brief_version_bump(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Stale QA", risk="HIGH")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/brief", data={"goal": "v1", "acceptance_criteria": "a"})
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "codex"})
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    d = decision(client, tid)
    assert d["qa"]["status"] == "PASS" and d["stage"] == "INTEGRATION"

    client.post(f"/api/tasks/{tid}/brief", data={"goal": "v2 -- scope changed", "acceptance_criteria": "a"})
    d2 = decision(client, tid)
    # a Brief change invalidates the Review it was reviewed against too --
    # QA built on that review is stale by extension, and the cascade sends
    # the Task all the way back to REVIEW, not just to QA.
    assert d2["stage"] == "REVIEW"
    assert d2["builders"][0]["review_status"] == "STALE"
    assert d2["next_action"]["action"] == "START_REVIEW"


def test_integration_not_current_blocks_ready_for_main(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Integration currency")
    client.post(f"/api/tasks/{tid}/select")
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    r = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r.status_code == 303
    d = decision(client, tid)
    # freshly created Integration is MERGING/TESTING, not yet verified --
    # never treated as an automatic READY_FOR_MAIN.
    assert d["status"] != "READY_FOR_MAIN"
    assert d["task_integration"] is not None


# ================================================================== 53
# Brief-version staleness on the Builder side (section 9): a Brief bump
# after Submit for Review makes the pinned prompt/report stale, never
# silently treated as still matching the new Brief.

def test_brief_bump_after_submit_does_not_retroactively_validate_old_review(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Brief bump")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/brief", data={"goal": "v1", "acceptance_criteria": "a"})
    w = add_workspace(client, tid, rid)
    submit_for_review(client, w)
    review(client, w, "PASS")
    v1 = client.get(f"/api/tasks/{tid}").json()["brief_version"]

    client.post(f"/api/tasks/{tid}/brief", data={"goal": "v2 -- different requirement", "acceptance_criteria": "a"})
    v2 = client.get(f"/api/tasks/{tid}").json()["brief_version"]
    assert v2 == v1 + 1

    d = decision(client, tid)
    assert d["builders"][0]["review_status"] == "STALE"


def test_brief_save_without_content_change_does_not_bump_version(client, git_repo):
    root, repo = git_repo
    register(client, repo, "demo")
    tid = new_task(client, "No-op brief save")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/brief", data={"goal": "same", "acceptance_criteria": "a"})
    v1 = client.get(f"/api/tasks/{tid}").json()["brief_version"]
    client.post(f"/api/tasks/{tid}/brief", data={"goal": "same", "acceptance_criteria": "a"})
    v2 = client.get(f"/api/tasks/{tid}").json()["brief_version"]
    assert v1 == v2


# ================================================================== 54
# REAL_UI_SCENARIO -- the mandatory "Fix Kiosk Session" walk: HIGH risk,
# cross-repo (backend + firmware), BACKLOG -> Select -> Brief -> two real
# Builder Workspaces -> DEVELOPMENT -> both submit -> REVIEW (exact
# commits) -> QA (required for HIGH) -> INTEGRATION (real cross-repo git
# merge, real `preflight`/`test` run per PROJECT.yaml, no Docker needed
# since neither fixture repo declares a sandbox: contract) ->
# READY_FOR_MAIN -> mark ONLY backend merged -> still not DONE -> mark
# firmware merged -> DONE.

def _wait_integration_tested(client, iid, timeout=20):
    """integration_workspaces.status stays 'TESTING' until an explicit
    ready-for-main call -- the actual completion signal is the test_runs
    rows themselves finishing (same pattern as wait_agent_tests)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = client.app.state.db.all("SELECT status FROM test_runs WHERE workspace_type='integration' AND workspace_id=?", (iid,))
        if runs and all(x["status"] not in ("QUEUED", "RUNNING") for x in runs): return
        time.sleep(.05)
    raise AssertionError("integration test did not finish")


def test_fix_kiosk_session_high_risk_cross_repo_golden_flow(client, git_repo):
    root, backend_repo = git_repo  # git_repo's repo doubles as "backend"
    firmware_repo = root / "firmware"; firmware_repo.mkdir()
    run(firmware_repo, "git", "init", "-b", "main")
    run(firmware_repo, "git", "config", "user.email", "t@t"); run(firmware_repo, "git", "config", "user.name", "t")
    (firmware_repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: firmware}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n"
    )
    (firmware_repo / "README.md").write_text("firmware base\n")
    run(firmware_repo, "git", "add", "."); run(firmware_repo, "git", "commit", "-m", "base")
    register(client, backend_repo, "kiosk-backend"); register(client, firmware_repo, "kiosk-firmware")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}

    # BACKLOG: no workspace at all
    tid = new_task(client, "Fix Kiosk Session", risk="HIGH")
    assert decision(client, tid)["status"] == "BACKLOG"

    # Select for Development
    client.post(f"/api/tasks/{tid}/select")
    d = decision(client, tid)
    assert d["status"] == "ACTIVE" and d["stage"] == "PLANNING"

    # Task Brief as source of truth
    client.post(f"/api/tasks/{tid}/brief", data={
        "goal": "Fix kiosk session token expiring mid-scan.",
        "context": "Kiosk backend issues a session token; firmware holds it for the scan duration.",
        "acceptance_criteria": "Session survives a full scan cycle; expired sessions are rejected cleanly.",
        "risk_profile": "HIGH",
    })
    assert decision(client, tid)["next_action"]["action"] == "CREATE_BUILDER_WORKSPACE"

    # Two real Builder Workspaces: Claude on backend, Codex on firmware
    backend_ws = add_workspace(client, tid, repos["kiosk-backend"], agent="claude", role="Backend")
    firmware_ws = add_workspace(client, tid, repos["kiosk-firmware"], agent="codex", role="Firmware")
    d = decision(client, tid)
    assert d["stage"] == "DEVELOPMENT" and len(d["builders"]) == 2

    # Builders do real, distinct work on their own branch/commit
    for ws, fname, content in ((backend_ws, "session.py", "extend token ttl\n"), (firmware_ws, "scan.c", "// hold session across scan\n")):
        (client.app.state.git.validate_worktree(ws["worktree_path"]) / fname).write_text(content)
        run(ws["worktree_path"], "git", "add", "."); run(ws["worktree_path"], "git", "commit", "-m", f"fix: {fname}")

    # Both submit for review -> stage REVIEW
    submit_for_review(client, backend_ws); submit_for_review(client, firmware_ws)
    d = decision(client, tid)
    assert d["stage"] == "REVIEW"
    assert d["next_action"]["action"] == "SUBMIT_FOR_REVIEW"

    # Review each at its exact commit
    review(client, backend_ws, "PASS"); review(client, firmware_ws, "PASS")
    d = decision(client, tid)
    assert all(b["review_status"] == "PASS" for b in d["builders"])
    reviewed_commits = {b["repository_id"]: b["review"]["reviewed_commit"] for b in d["builders"]}
    for b in d["builders"]:
        assert reviewed_commits[b["repository_id"]] == b["head"]  # pinned to the exact commit, not "latest"

    # HIGH risk -> QA required before Integration
    assert d["stage"] == "QA"
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS", "manual_result": "PASS"})
    d = decision(client, tid)
    assert d["qa"]["status"] == "PASS"
    assert d["stage"] == "INTEGRATION"
    assert d["next_action"]["action"] == "CREATE_INTEGRATION"

    # Cross-repo Integration: one Integration Workspace PER repo, real git merges
    r = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r.status_code == 303
    ti = client.app.state.db.one("SELECT * FROM task_integrations WHERE task_id=?", (tid,))
    iws = client.app.state.db.all("SELECT * FROM integration_workspaces WHERE task_integration_id=?", (ti["id"],))
    assert len(iws) == 2  # one per repo, never one worktree spanning repos
    assert {i["repository_id"] for i in iws} == {repos["kiosk-backend"], repos["kiosk-firmware"]}
    assert all(i["status"] == "TESTING" for i in iws), "two independent single-source branches should merge cleanly"

    for iw in iws:
        client.post(f"/api/integrations/{iw['id']}/test")
        _wait_integration_tested(client, iw["id"])
        client.post(f"/api/integrations/{iw['id']}/ready-for-main")

    d = decision(client, tid)
    assert d["status"] == "READY_FOR_MAIN" and d["ready_for_main"] is True
    assert "Cross-repo rollout may temporarily produce mixed versions." in client.get(f"/tasks/{tid}").text

    # Mark ONLY backend's repo merged -- Task must NOT become DONE yet
    client.post(f"/api/tasks/{tid}/merges/{repos['kiosk-backend']}/mark-merged")
    d = decision(client, tid)
    assert d["status"] != "DONE"
    assert d["stage"] == "MERGING"
    merged = {m["repository_id"]: m["merge_status"] for m in d["merge_records"]}
    assert merged[repos["kiosk-backend"]] == "MERGED"
    assert merged[repos["kiosk-firmware"]] != "MERGED"

    # Mark firmware merged too -- NOW the Task reaches DONE
    client.post(f"/api/tasks/{tid}/merges/{repos['kiosk-firmware']}/mark-merged")
    d = decision(client, tid)
    assert d["status"] == "DONE"
    assert all(m["merge_status"] == "MERGED" for m in d["merge_records"] if m["required"])
