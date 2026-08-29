"""[Push Integration Branch]: a real, non-force `git push origin
<branch>:<branch>` for the registered Integration record, reusing
GitHubMergeService.push_branch()/pr_status() -- the exact same
primitives Create PR/Merge already use, never a second ad-hoc
subprocess path. GitHubMergeService.runner is injectable (same DI
pattern as test_real_merge.py's FakeGh); real git (real repos, real
branches, real commits) backs everything TaskDecisionService itself
reads."""
from __future__ import annotations
import json
import subprocess
import time
from dataclasses import dataclass

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
        self.pushed_branches: list[str] = []
        self.push_should_fail: str | None = None  # None | "generic" | "non_fast_forward"

    def real_head(self, repo_path, branch):
        r = subprocess.run(["git", "rev-parse", branch], cwd=str(repo_path), text=True, capture_output=True)
        return r.stdout.strip()

    def register_pr(self, number, head_branch, base_branch, repo_path, ci_checks=None):
        self.prs[number] = {
            "number": number, "url": f"https://github.com/test/test/pull/{number}", "state": "OPEN",
            "headRefName": head_branch, "baseRefName": base_branch,
            "headRefOid": self.real_head(repo_path, head_branch), "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN", "statusCheckRollup": ci_checks or [], "mergeCommit": None, "title": "t",
        }
        self.next_number = max(self.next_number, number + 1)

    def __call__(self, argv, cwd, timeout=30):
        if argv[:3] == ["git", "remote", "get-url"]:
            return FR(0, "git@github.com:test/test.git\n")
        if argv[:2] == ["git", "push"]:
            if self.push_should_fail == "non_fast_forward":
                return FR(1, "", "! [rejected]  x -> x (non-fast-forward)\nerror: failed to push some refs")
            if self.push_should_fail == "generic":
                return FR(1, "", "fatal: could not read from remote repository")
            self.pushed_branches.append(argv[3])
            branch = argv[3].split(":", 1)[0]
            for pr in self.prs.values():
                if pr["headRefName"] == branch:
                    pr["headRefOid"] = self.real_head(cwd, branch)
            return FR(0, "")
        if argv[:3] == ["gh", "pr", "list"]:
            head = argv[argv.index("--head") + 1]
            base = argv[argv.index("--base") + 1]
            matches = [p for p in self.prs.values() if p["headRefName"] == head and p["baseRefName"] == base]
            return FR(0, json.dumps([{"number": p["number"], "url": p["url"], "state": p["state"]} for p in matches[:1]]))
        if argv[:3] == ["gh", "pr", "create"]:
            raise AssertionError("gh pr create must never be called by Push Integration Branch (section 10)")
        if argv[:3] == ["gh", "pr", "view"]:
            num = int(argv[3])
            pr = self.prs[num]
            data = {
                "number": pr["number"], "url": pr["url"], "state": pr["state"], "mergeable": pr["mergeable"],
                "mergeStateStatus": pr["mergeStateStatus"], "headRefOid": pr["headRefOid"],
                "baseRefName": pr["baseRefName"], "statusCheckRollup": pr["statusCheckRollup"],
                "mergedAt": pr.get("mergedAt"),
                "mergeCommit": {"oid": pr["mergeCommit"]} if pr.get("mergeCommit") else None, "title": pr["title"],
            }
            return FR(0, json.dumps(data))
        return FR(1, "", f"unhandled fake gh/git call: {argv}")


def ready_normal_risk_task_with_integration(client, git_repo, title):
    """Task -> Builder READY -> Review PASS -> Integration created (NORMAL
    risk requires it). Returns (tid, rid, integration_id)."""
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks/create", data={"title": title, "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "NORMAL"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    r2 = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r2.status_code == 303
    iid = client.get("/api/integrations").json()[0]["id"]
    return tid, rid, iid


def test_push_sends_exact_integration_branch_non_force(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Push exact branch")
    i = client.get(f"/api/integrations").json()[0]
    branch = i["branch"]

    r = client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    assert r.status_code == 303
    assert fake.pushed_branches == [f"{branch}:{branch}"]  # exact branch, exact refspec, never --force anywhere


def test_push_blocked_when_worktree_dirty(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Dirty worktree")
    i = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    with open(i["worktree_path"] + "/dirty.txt", "w") as f:
        f.write("uncommitted")

    r = client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    assert r.status_code == 409
    assert "INTEGRATION_WORKTREE_DIRTY" in r.text
    assert fake.pushed_branches == []


def test_push_blocked_when_remote_not_configured(client, git_repo):
    class NoRemote(FakeGh):
        def __call__(self, argv, cwd, timeout=30):
            if argv[:3] == ["git", "remote", "get-url"]:
                return FR(1, "", "fatal: No such remote 'origin'")
            return super().__call__(argv, cwd, timeout)
    fake = NoRemote()
    client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "No remote")

    r = client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    assert r.status_code == 409
    assert "REMOTE_NOT_CONFIGURED" in r.text
    assert fake.pushed_branches == []


def test_successful_push_records_exact_head_and_audit(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Records head")
    i = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    head_before = client.app.state.git.head(i["worktree_path"])

    r = client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    assert r.status_code == 303
    row = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    assert row["last_pushed_head"] == head_before
    assert row["push_status"] == "PUSHED"
    assert row["pushed_at"]

    actions = [e["action"] for e in client.app.state.db.all("SELECT action FROM workspace_events WHERE entity_type='integration' AND entity_id=? ORDER BY id", (iid,))]
    assert "INTEGRATION_PUSH_STARTED" in actions
    assert "INTEGRATION_PUSH_SUCCEEDED" in actions


def test_push_failure_is_surfaced_and_recorded(client, git_repo):
    fake = FakeGh()
    fake.push_should_fail = "generic"
    client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Push fails")

    r = client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    assert r.status_code == 409
    assert "PUSH_FAILED" in r.text
    row = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    assert row["push_status"] == "PUSH_FAILED"
    assert row["push_error"]
    actions = [e["action"] for e in client.app.state.db.all("SELECT action FROM workspace_events WHERE entity_type='integration' AND entity_id=? ORDER BY id", (iid,))]
    assert "INTEGRATION_PUSH_FAILED" in actions


def test_non_fast_forward_refused_never_force_overwrites(client, git_repo):
    fake = FakeGh()
    fake.push_should_fail = "non_fast_forward"
    client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Diverged remote")

    r = client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    assert r.status_code == 409
    assert "PUSH_BLOCKED_REMOTE_CHANGED" in r.text
    row = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    assert row["push_status"] == "PUSH_BLOCKED_REMOTE_CHANGED"
    assert row["last_pushed_head"] is None  # never recorded as pushed -- nothing was force-overwritten


def test_push_never_creates_a_pr_only_refreshes_existing_one(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Refresh not create")
    i = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    fake.register_pr(6, i["branch"], "main", i["worktree_path"], ci_checks=[{"status": "IN_PROGRESS", "conclusion": None, "name": "ci"}])
    client.app.state.db.execute(
        "UPDATE merge_records SET pr_number=6,pr_url=?,pr_state='OPEN' WHERE task_id=? AND repository_id=?",
        (fake.prs[6]["url"], tid, rid))

    # make a new local commit so HEAD actually changes before pushing
    with open(i["worktree_path"] + "/change.txt", "w") as f:
        f.write("new change")
    run(i["worktree_path"], "git", "add", ".")
    run(i["worktree_path"], "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "integration change")
    new_head = client.app.state.git.head(i["worktree_path"])

    r = client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    assert r.status_code == 303
    assert 6 in fake.prs  # still the same PR object, never a second one created
    assert len(fake.prs) == 1

    mr = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert mr["pr_number"] == 6  # same PR number
    assert mr["head_sha"] == new_head  # head refreshed to the just-pushed commit
    assert mr["ci_status"] == "PENDING"  # CI status refreshed too


def test_task_decision_selects_push_integration_when_tests_pass_but_not_pushed(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Selects push action")
    client.post(f"/api/integrations/{iid}/test")
    for _ in range(100):
        st = client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"]
        if st != "TESTING":
            break
        time.sleep(0.05)

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["next_action"]["action"] == "PUSH_INTEGRATION"


def test_task_decision_selects_wait_for_ci_after_push_with_pending_ci(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Selects wait for ci")
    i = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    fake.register_pr(9, i["branch"], "main", i["worktree_path"], ci_checks=[{"status": "IN_PROGRESS", "conclusion": None, "name": "ci"}])
    client.app.state.db.execute(
        "UPDATE merge_records SET pr_number=9,pr_url=?,pr_state='OPEN' WHERE task_id=? AND repository_id=?",
        (fake.prs[9]["url"], tid, rid))

    client.post(f"/api/integrations/{iid}/test")
    for _ in range(100):
        st = client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"]
        if st != "TESTING":
            break
        time.sleep(0.05)
    client.post(f"/api/integrations/{iid}/push")

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["next_action"]["action"] == "WAIT_FOR_CI"


def test_multi_repo_integration_push_tracked_per_repo(client, git_repo):
    root, repo = git_repo
    other = root / "second"
    other.mkdir()
    run(other, "git", "init", "-b", "main")
    run(other, "git", "config", "user.email", "t@t")
    run(other, "git", "config", "user.name", "t")
    (other / "PROJECT.yaml").write_text("schema_version: 1\nproject: {code: second}\nsource: {root: .}\ncommands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n")
    (other / "README.md").write_text("x\n")
    run(other, "git", "add", ".")
    run(other, "git", "commit", "-m", "base")
    register(client, repo, "demo")
    register(client, other, "second")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}

    r = client.post("/api/tasks/create", data={
        "title": "Multi-repo integration push", "risk_profile": "NORMAL",
        "repository_id": repos["demo"], "agent": "claude", "sandbox_profile": "NONE",
        "ws_repository_id": [str(repos["second"])], "ws_agent": ["codex"], "ws_role": ["Firmware"],
        "ws_base_branch": ["main"], "ws_sandbox_profile": ["NONE"],
    }, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    ws = client.get(f"/api/tasks/{tid}").json()["workspaces"]
    for w in ws:
        client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
        client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
        client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    integrations = client.get("/api/integrations").json()
    assert len(integrations) == 2

    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    first = integrations[0]
    r2 = client.post(f"/api/integrations/{first['id']}/push", follow_redirects=False)
    assert r2.status_code == 303

    row_first = client.app.state.db.one("SELECT push_status FROM integration_workspaces WHERE id=?", (first["id"],))
    row_second = client.app.state.db.one("SELECT push_status FROM integration_workspaces WHERE id=?", (integrations[1]["id"],))
    assert row_first["push_status"] == "PUSHED"
    assert row_second["push_status"] == "NOT_PUSHED"  # untouched -- push is tracked per repo/integration
