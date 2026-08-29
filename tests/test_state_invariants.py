"""State-consistency audit: invariants that must hold across Task,
Builder Workspace, Agent Session, Review, QA, Integration, TestRun,
Sandbox, MergeRecord, and GitHub PR/CI state -- encoded as regression
tests against the real decision/view-model layer (TaskDecisionService,
SandboxManager), never a second parallel check. Motivated by the real
production bug where GitHub reported a PR MERGED while MergeRecord
still said PR_OPEN (fixed in a prior change); this file generalizes
that lesson to every entity pair the audit identified as a real or
plausible risk. Reuses test_real_merge.py's FakeGh pattern (duplicated,
not imported, per this repo's convention)."""
from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass
from app.launchers import AgentLauncher

import pytest


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


@dataclass
class FR:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeGh:
    def __init__(self):
        self.prs: dict[int, dict] = {}
        self.next_number = 1

    def real_head(self, repo_path, branch):
        r = subprocess.run(["git", "rev-parse", branch], cwd=str(repo_path), text=True, capture_output=True)
        return r.stdout.strip()

    def __call__(self, argv, cwd, timeout=30):
        if argv[:3] == ["git", "remote", "get-url"]:
            return FR(0, "git@github.com:test/test.git\n")
        if argv[:2] == ["git", "push"]:
            return FR(0, "")
        if argv[:3] == ["gh", "pr", "list"]:
            head = argv[argv.index("--head") + 1]; base = argv[argv.index("--base") + 1]
            m = [p for p in self.prs.values() if p["headRefName"] == head and p["baseRefName"] == base and p["state"] != "CLOSED"]
            return FR(0, json.dumps([{"number": p["number"], "url": p["url"], "state": p["state"]} for p in m[:1]]))
        if argv[:3] == ["gh", "pr", "create"]:
            head = argv[argv.index("--head") + 1]; base = argv[argv.index("--base") + 1]
            n = self.next_number; self.next_number += 1
            url = f"https://github.com/test/test/pull/{n}"
            self.prs[n] = {"number": n, "url": url, "state": "OPEN", "headRefName": head, "baseRefName": base,
                           "headRefOid": self.real_head(cwd, head), "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                           "statusCheckRollup": [], "mergeCommit": None, "mergedAt": None, "title": "t"}
            return FR(0, url + "\n")
        if argv[:3] == ["gh", "pr", "view"]:
            num = int(argv[3]); pr = self.prs[num]
            data = {"number": pr["number"], "url": pr["url"], "state": pr["state"], "mergeable": pr["mergeable"],
                    "mergeStateStatus": pr["mergeStateStatus"], "headRefOid": pr["headRefOid"], "baseRefName": pr["baseRefName"],
                    "statusCheckRollup": pr["statusCheckRollup"], "mergedAt": pr.get("mergedAt"),
                    "mergeCommit": {"oid": pr["mergeCommit"]} if pr.get("mergeCommit") else None, "title": pr["title"]}
            return FR(0, json.dumps(data))
        if argv[:3] == ["gh", "pr", "merge"]:
            num = int(argv[3]); pr = self.prs[num]
            pr["state"] = "MERGED"; pr["mergeCommit"] = f"merged{num:040d}"[:40]; pr["mergedAt"] = "2026-01-01T00:00:00Z"
            return FR(0, "")
        return FR(1, "", f"unhandled fake gh/git call: {argv}")


def create_task(client, title, rid, agent="claude", risk="LOW"):
    r = client.post("/api/tasks/create", data={"title": title, "repository_id": rid, "agent": agent, "sandbox_profile": "NONE", "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("/")[-1])


def submit_and_review(client, w, result="PASS"):
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    r = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": result}, follow_redirects=False)
    assert r.status_code == 303


def ready_low_risk_task(client, git_repo, title, repo_name="demo"):
    root, repo = git_repo
    register(client, repo, repo_name)
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == repo_name][0]["id"]
    tid = create_task(client, title, rid, risk="LOW")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    submit_and_review(client, w, "PASS")
    return tid, rid, w


def ready_normal_risk_task_with_integration(client, git_repo, title, repo_name="demo"):
    root, repo = git_repo
    register(client, repo, repo_name)
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == repo_name][0]["id"]
    r = client.post("/api/tasks/create", data={"title": title, "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "NORMAL"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    submit_and_review(client, w, "PASS")
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})  # NORMAL now also requires Runtime Verification
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    r2 = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r2.status_code == 303
    iid = client.get("/api/integrations").json()[0]["id"]
    return tid, rid, iid, w


def merge_record_of(client, tid, rid):
    return client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))


# ============================================================== Task/MergeRecord
def test_invariant_merged_repo_has_no_blockers_and_no_workflow_actions(client, git_repo):
    """MERGED repo -> no merge blockers, no Merge/Create PR/Push actions."""
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Merged no blockers")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)

    d = client.get(f"/api/tasks/{tid}/decision").json()
    row = merge_record_of(client, tid, rid)
    gate = client.app.state.decision.merge_gate_status(d, rid, row)
    assert gate["blockers"] == []
    assert gate["eligible"] is False
    assert row["merged_commit"] is not None  # never MERGED with no commit to point to

    html = client.get(f"/tasks/{tid}").text
    wizard_html = html.split('id="advanced-details"')[0]
    assert ">Merge</button>" not in wizard_html
    assert ">Create PR</button>" not in wizard_html


def test_invariant_task_done_implies_all_required_repos_merged(client, git_repo):
    """Task DONE -> all required repos merged, no workflow action except
    post-completion actions (CLOSE_TASK/NONE)."""
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Done implies merged")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"
    required = [m for m in d["merge_records"] if m["required"]]
    assert required and all(m["merge_status"] == "MERGED" for m in required)
    assert d["next_action"]["action"] in ("CLOSE_TASK", "NONE")


def test_invariant_never_done_while_a_required_repo_is_unmerged(client, git_repo):
    """Converse of the above: as long as any required repo is not
    MERGED, Task.status must never read DONE."""
    root, repo_a = git_repo
    register(client, repo_a, "repo-a")
    from tests.conftest import make_repo
    repo_b = make_repo(root, "repo-b")
    register(client, repo_b, "repo-b")
    rid_a = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "repo-a"][0]["id"]
    rid_b = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "repo-b"][0]["id"]
    r = client.post("/api/tasks/create", data={"title": "Never done partial", "repository_id": rid_a, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid_b, "agent": "codex", "role": "b", "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    for w in client.get(f"/api/tasks/{tid}").json()["workspaces"]:
        submit_and_review(client, w, "PASS")

    fake = FakeGh(); client.app.state.github_merge.runner = fake
    wa = next(w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["repository_id"] == rid_a)
    client.post(f"/api/tasks/{tid}/merges/{rid_a}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid_a}/merge", follow_redirects=False)

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] != "DONE"
    row_a = merge_record_of(client, tid, rid_a); row_b = merge_record_of(client, tid, rid_b)
    assert row_a["merge_status"] == "MERGED" and row_b["merge_status"] != "MERGED"


# ============================================================== Integration
def test_invariant_integration_ready_requires_head_equal_verified(client, git_repo):
    """Integration READY_FOR_MAIN -> current HEAD must equal verified
    HEAD. Real audit finding: the persisted status column previously
    only got reconciled as a side effect of loading /integrations/{iid}
    directly -- any OTHER route (here: /api/tasks/{tid}/decision, never
    visiting the Integration page at all) must ALSO see the correct,
    non-stale answer once a new commit lands on the source branch."""
    tid, rid, iid, w = ready_normal_risk_task_with_integration(client, git_repo, "Integration staleness")
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    row = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    assert row["ready_for_main"] == 1

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["stage"] != "INTEGRATION"  # confirmed ready before any new commit

    # A new, never-tested commit lands directly on the INTEGRATION
    # branch's own worktree (out of band -- no "Merge Latest Changes",
    # no visit to /integrations) -- deliberately NOT on the Builder's
    # branch, so Review staleness cannot be what re-blocks this: this
    # isolates the Integration-level self-healing check specifically.
    irow = client.app.state.db.one("SELECT worktree_path FROM integration_workspaces WHERE id=?", (iid,))
    (__import__("pathlib").Path(irow["worktree_path"]) / "late.txt").write_text("late change\n")
    run(irow["worktree_path"], "git", "add", ".")
    run(irow["worktree_path"], "git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-m", "late change")

    # Decision API only -- /integrations/{iid} is never loaded.
    d2 = client.get(f"/api/tasks/{tid}/decision").json()
    assert d2["stage"] == "INTEGRATION", "a stale READY_FOR_MAIN must not survive a new unmerged/untested commit"
    ti_repo = next(r for r in d2["integration_repos"] if r["id"] == iid)
    gs = ti_repo.get("gate_status") or {}
    if ti_repo["verified_commit"] and gs.get("head"):
        assert ti_repo["verified_commit"] != gs["head"]  # the actual divergence this invariant is about


def test_invariant_test_pass_verified_commit_equals_tested_commit(client, git_repo):
    tid, rid, iid, w = ready_normal_risk_task_with_integration(client, git_repo, "Verified equals tested")
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    row = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    assert row["ready_for_main"] == 1
    tested = client.app.state.db.all(
        "SELECT DISTINCT tested_commit FROM test_runs WHERE workspace_type='integration' AND workspace_id=? AND status='PASS'", (iid,))
    assert tested and all(t["tested_commit"] == row["verified_commit"] for t in tested)


# ============================================================== Sandbox
def test_invariant_cleanup_eligible_still_counts_toward_capacity(client, sandboxable_repo_factory, git_repo):
    """Sandbox CLEANUP_ELIGIBLE -> may still be RUNNING during
    retention; the real container is still up, so it must still count
    against max_running (a capacity-enforcement bug otherwise: more
    real containers could run than max_running allows)."""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-cap", port_range=(21850, 21899))
    register(client, repo, "svc-cap")
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-cap"][0]["id"]
    client.post("/api/tasks", data={"title": "Capacity check"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[-1]["id"]
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "A", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[-1]

    before = client.app.state.sandboxes.running_count()
    client.app.state.db.execute("UPDATE sandboxes SET status='CLEANUP_ELIGIBLE' WHERE id=?", (sb["id"],))
    after = client.app.state.sandboxes.running_count()
    assert after == before  # CLEANUP_ELIGIBLE must count exactly like RUNNING did
    try:
        pass
    finally:
        client.app.state.db.execute("UPDATE sandboxes SET status='RUNNING' WHERE id=?", (sb["id"],))
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")


def test_invariant_cleanup_eligible_sandbox_is_not_reported_not_running(client, sandboxable_repo_factory, git_repo):
    """A Builder Workspace whose sandbox is CLEANUP_ELIGIBLE (but still
    genuinely running) must never be told its sandbox 'is not running'
    when asked what to do next."""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-cap2", port_range=(21860, 21899))
    register(client, repo, "svc-cap2")
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-cap2"][0]["id"]
    client.post("/api/tasks", data={"title": "Not-running check"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[-1]["id"]
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "A", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[-1]
    try:
        client.app.state.db.execute("UPDATE sandboxes SET status='CLEANUP_ELIGIBLE' WHERE id=?", (sb["id"],))
        html = client.get(f"/tasks/{tid}").text
        assert "SANDBOX NOT RUNNING" not in html.upper().replace("_", " ")
    finally:
        client.app.state.db.execute("UPDATE sandboxes SET status='RUNNING' WHERE id=?", (sb["id"],))
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")


# ============================================================== Agent Session
def test_invariant_live_session_cwd_matches_its_workspace_worktree(client, git_repo):
    """Agent Session RUNNING -> associated workspace must still exist
    and its cwd must match that workspace's own registered
    worktree_path -- the one-agent/one-branch/one-worktree source
    isolation guarantee, never a session pinned to a different path."""
    tid, rid, w = ready_low_risk_task(client, git_repo, "Session isolation")
    client.app.state.agent_sessions.launchers = {"claude": AgentLauncher("Claude", "bash", ("-c", "sleep 5"))}
    r = client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    sid = int(r.headers["location"].split("/")[-1])
    session = client.app.state.db.one("SELECT * FROM agent_sessions WHERE id=?", (sid,))
    wrow = client.app.state.db.one("SELECT worktree_path FROM agent_workspaces WHERE id=?", (w["id"],))
    assert session["cwd"] == wrow["worktree_path"]
    assert client.app.state.db.one("SELECT id FROM agent_workspaces WHERE id=?", (session["workspace_id"],))  # workspace still exists
    client.post(f"/api/sessions/{sid}/stop")


def test_invariant_at_most_one_live_session_per_workspace(client, git_repo):
    tid, rid, w = ready_low_risk_task(client, git_repo, "One live session")
    client.app.state.agent_sessions.launchers = {"claude": AgentLauncher("Claude", "bash", ("-c", "sleep 5"))}
    client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    client.post(f"/api/workspaces/{w['id']}/sessions", follow_redirects=False)
    live = client.app.state.db.all(
        "SELECT id FROM agent_sessions WHERE workspace_id=? AND status IN ('STARTING','RUNNING','WAITING_FOR_INPUT')", (w["id"],))
    assert len(live) <= 1
    for s in live:
        client.post(f"/api/sessions/{s['id']}/stop")
