"""Real GitHub-backed merge execution: PR creation, live CI/mergeability-
aware merge eligibility, the real merge call, conflict/staleness
blocking, multi-repo partial-merge semantics, and the manual external-
merge fallback's real ancestry check. GitHubMergeService.runner is
injectable (same DI pattern as AgentSessionManager.which/launchers) --
these tests substitute a deterministic FakeGh that never shells out to
the real `gh` CLI, while still exercising real git (real repos, real
branches, real commits) for everything TaskDecisionService itself
reads."""
from __future__ import annotations
import json
import subprocess
import time
from dataclasses import dataclass, field

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
    """A deterministic stand-in for the real `gh`/`git` calls
    GitHubMergeService issues. Real head SHAs are read via a genuine
    local `git rev-parse` against the caller's own repo (so they stay
    consistent with what TaskDecisionService's own real git reads see)
    -- only the GitHub-facing (gh pr ...) and remote-facing
    (fetch/rev-parse origin/X/merge-base) calls are faked."""

    def __init__(self):
        self.prs: dict[int, dict] = {}
        self.next_number = 1
        self.target_heads: dict[str, str] = {}  # base_branch -> fake remote HEAD sha
        self.ancestors: set[str] = set()  # commits considered ancestors of the target for external-merge tests

    def real_head(self, repo_path, branch):
        r = subprocess.run(["git", "rev-parse", branch], cwd=str(repo_path), text=True, capture_output=True)
        return r.stdout.strip()

    def __call__(self, argv, cwd, timeout=30):
        if argv[:3] == ["git", "remote", "get-url"]:
            return FR(0, "git@github.com:test/test.git\n")
        if argv[:3] == ["gh", "pr", "list"]:
            head = argv[argv.index("--head") + 1]
            base = argv[argv.index("--base") + 1]
            matches = [p for p in self.prs.values() if p["headRefName"] == head and p["baseRefName"] == base]
            return FR(0, json.dumps([{"number": p["number"], "url": p["url"], "state": p["state"]} for p in matches[:1]]))
        if argv[:3] == ["gh", "pr", "create"]:
            head = argv[argv.index("--head") + 1]
            base = argv[argv.index("--base") + 1]
            title = argv[argv.index("--title") + 1]
            num = self.next_number
            self.next_number += 1
            url = f"https://github.com/test/test/pull/{num}"
            self.prs[num] = {
                "number": num, "url": url, "state": "OPEN", "headRefName": head, "baseRefName": base,
                "headRefOid": self.real_head(cwd, head), "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                "statusCheckRollup": [], "mergeCommit": None, "title": title,
            }
            return FR(0, url + "\n")
        if argv[:3] == ["gh", "pr", "view"]:
            num = int(argv[3])
            pr = self.prs[num]
            data = {
                "number": pr["number"], "url": pr["url"], "state": pr["state"], "mergeable": pr["mergeable"],
                "mergeStateStatus": pr["mergeStateStatus"], "headRefOid": pr["headRefOid"],
                "baseRefName": pr["baseRefName"], "statusCheckRollup": pr["statusCheckRollup"],
                "mergedAt": pr.get("mergedAt"),
                "mergeCommit": {"oid": pr["mergeCommit"]} if pr.get("mergeCommit") else None,
                "title": pr["title"],
            }
            return FR(0, json.dumps(data))
        if argv[:3] == ["gh", "pr", "merge"]:
            num = int(argv[3])
            pr = self.prs[num]
            if pr["mergeable"] != "MERGEABLE":
                return FR(1, "", "GraphQL: Pull request is not mergeable")
            if any((c.get("conclusion") or "").upper() == "FAILURE" for c in pr["statusCheckRollup"]):
                return FR(1, "", "required status check failed")
            if any((c.get("status") or "").upper() != "COMPLETED" for c in pr["statusCheckRollup"]):
                return FR(1, "", "not all checks have completed yet")
            pr["state"] = "MERGED"
            pr["mergeCommit"] = f"merged{num:040d}"[:40]
            pr["mergedAt"] = "2026-01-01T00:00:00Z"
            return FR(0, "")
        if argv[:2] == ["git", "fetch"]:
            return FR(0, "")
        if argv[:2] == ["git", "rev-parse"] and str(argv[2]).startswith("origin/"):
            base = argv[2].split("/", 1)[1]
            return FR(0, self.target_heads.get(base, "0" * 40) + "\n")
        if argv[:2] == ["git", "merge-base"]:
            commit = argv[3]
            return FR(0 if commit in self.ancestors else 1, "")
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


def ready_low_risk_task(client, git_repo, title):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, title, rid, risk="LOW")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    submit_and_review(client, w, "PASS")
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["ready_for_main"] is True
    return tid, rid, w


def test_create_pr_persists_number_url_and_verified_commit(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Create PR task")

    r = client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    assert r.status_code == 303
    row = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert row["pr_number"] == 1
    assert row["pr_state"] == "OPEN"
    assert row["merge_status"] == "PR_OPEN"
    assert row["verified_commit"] == w["head"] if "head" in w else row["verified_commit"]
    assert row["source_branch"] == w["branch"]


def test_create_pr_is_idempotent_no_duplicate(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Dup PR task")

    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr")
    assert len(fake.prs) == 1  # gh pr create only ever called once; second call reused find_existing_pr


def test_ci_pending_blocks_merge(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "CI pending task")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr")
    fake.prs[1]["statusCheckRollup"] = [{"status": "IN_PROGRESS", "conclusion": None, "name": "ci"}]

    r = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert r.status_code == 409
    row = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert row["merge_status"] != "MERGED"
    assert row["ci_status"] == "PENDING"


def test_ci_fail_blocks_merge(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "CI fail task")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr")
    fake.prs[1]["statusCheckRollup"] = [{"status": "COMPLETED", "conclusion": "FAILURE", "name": "ci"}]

    r = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert r.status_code == 409
    row = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert row["merge_status"] != "MERGED"
    assert row["ci_status"] == "FAIL"


def test_conflict_blocks_merge_and_sets_conflict_status(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Conflict task")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr")
    fake.prs[1]["mergeable"] = "CONFLICTING"

    r = client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    assert r.status_code == 303
    row = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert row["merge_status"] == "CONFLICT"
    assert row["mergeability"] == "CONFLICTING"

    r2 = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert r2.status_code == 409


def test_stale_verified_sha_blocks_merge(client, git_repo):
    """After PR creation, a NEW commit lands on the same source branch --
    the previously-verified commit is no longer what the branch (or the
    PR's own head) actually points at, and Merge must refuse."""
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Stale SHA task")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr")

    # simulate the PR's remote head having moved past what was verified
    fake.prs[1]["headRefOid"] = "f" * 40

    r = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert r.status_code == 409
    row = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert row["merge_status"] != "MERGED"


def test_clean_pr_merges_and_stores_exact_merge_sha(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Clean merge task")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr")
    fake.prs[1]["statusCheckRollup"] = [{"status": "COMPLETED", "conclusion": "SUCCESS", "name": "ci"}]

    r = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert r.status_code == 303
    row = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert row["merge_status"] == "MERGED"
    assert row["merged_commit"] == fake.prs[1]["mergeCommit"]
    assert row["merged_commit"] is not None

    events = client.app.state.db.all("SELECT action FROM workspace_events WHERE entity_type='task' AND entity_id=? ORDER BY id", (tid,))
    actions = [e["action"] for e in events]
    assert "MERGE_REQUESTED" in actions
    assert "MERGE_SUCCEEDED" in actions

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"  # single-repo Task, only required repo merged


def test_no_state_only_fake_merge_real_gh_call_required(client, git_repo):
    """The merge route must actually call through the injected runner --
    breaking the runner (always erroring) must make Merge fail, proving
    it isn't a state-only DB write standing in for the real thing."""
    class AlwaysFail:
        def __call__(self, argv, cwd, timeout=30):
            if argv[:3] == ["git", "remote", "get-url"]:
                return FR(0, "git@github.com:test/test.git\n")
            return FR(1, "", "simulated gh CLI outage")
    client.app.state.github_merge.runner = AlwaysFail()
    tid, rid, w = ready_low_risk_task(client, git_repo, "No fake merge task")

    r = client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    assert r.status_code == 409
    row = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert row["pr_number"] is None
    assert row["merge_status"] == "NOT_STARTED"


def test_multi_repo_partial_merge_keeps_task_active(client, git_repo):
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
        "title": "Multi-repo merge task", "risk_profile": "LOW",
        "repository_id": repos["demo"], "agent": "claude", "sandbox_profile": "NONE",
        "ws_repository_id": [str(repos["second"])], "ws_agent": ["codex"], "ws_role": ["Firmware"],
        "ws_base_branch": ["main"], "ws_sandbox_profile": ["NONE"],
    }, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    ws = client.get(f"/api/tasks/{tid}").json()["workspaces"]
    submit_and_review(client, ws[0], "PASS")
    submit_and_review(client, ws[1], "PASS")
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["ready_for_main"] is True

    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    client.post(f"/api/tasks/{tid}/merges/{repos['demo']}/create-pr")
    client.post(f"/api/tasks/{tid}/merges/{repos['demo']}/merge")

    d2 = client.get(f"/api/tasks/{tid}/decision").json()
    # Partial merge: status stays READY_FOR_MAIN (the existing invariant
    # -- it only flips once EVERY required repo is merged), but stage
    # reflects MERGING and only 1 of 2 required repos is actually MERGED.
    assert d2["status"] == "READY_FOR_MAIN"
    assert d2["stage"] == "MERGING"
    merged = [m for m in d2["merge_records"] if m["merge_status"] == "MERGED"]
    assert len(merged) == 1

    client.post(f"/api/tasks/{tid}/merges/{repos['second']}/create-pr")
    client.post(f"/api/tasks/{tid}/merges/{repos['second']}/merge")
    d3 = client.get(f"/api/tasks/{tid}/decision").json()
    assert d3["status"] == "DONE"  # all required repos merged now


def test_external_merge_verifies_ancestry_before_confirming(client, git_repo):
    fake = FakeGh()
    client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "External merge task")

    # ancestry check fails -- verified commit is NOT (per the fake) an
    # ancestor of the target branch -- must be refused, never trusted
    r = client.post(f"/api/tasks/{tid}/merges/{rid}/confirm-external-merge", data={"reason": "merged manually"}, follow_redirects=False)
    assert r.status_code == 409
    row = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert row["merge_status"] != "MERGED"

    # now make the fake report the verified commit as a real ancestor
    d = client.get(f"/api/tasks/{tid}/decision").json()
    branch, commit = w["branch"], d["builders"][0]["head"]
    fake.ancestors.add(commit)
    fake.target_heads["main"] = "abc123" * 6 + "abcd"

    r2 = client.post(f"/api/tasks/{tid}/merges/{rid}/confirm-external-merge", data={"reason": "merged via GitHub UI, verified manually"}, follow_redirects=False)
    assert r2.status_code == 303
    row2 = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    assert row2["merge_status"] == "MERGED"
    assert row2["merged_commit"] == fake.target_heads["main"]
    assert row2["external_merge_reason"] == "merged via GitHub UI, verified manually"

    events = client.app.state.db.all("SELECT action FROM workspace_events WHERE entity_type='task' AND entity_id=? ORDER BY id", (tid,))
    assert "EXTERNAL_MERGE_CONFIRMED" in [e["action"] for e in events]


def test_sandbox_cleanup_scheduled_only_after_done(client, git_repo, sandboxable_repo_factory):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "cleanup-repo", port_range=(22400, 22419))
    register(client, repo, "cleanup-repo")
    rid = client.get("/api/repositories").json()[0]["id"]
    # Built with the plain BACKLOG->select->one-workspace path (like
    # test_full_lifecycle_backlog_to_closed) rather than create_task()'s
    # /api/tasks/create, which would otherwise create ITS OWN workspace
    # too -- this task must have exactly one Builder Workspace, with a
    # real sandbox, for the DONE transition to actually happen here.
    client.post("/api/tasks", data={"title": "Cleanup gating task", "risk_profile": "LOW"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    assert r.status_code == 303
    sb = client.get("/api/sandboxes").json()[0]
    try:
        w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
        submit_and_review(client, w, "PASS")
        row_before = client.app.state.db.one("SELECT status,cleanup_eligible_at FROM sandboxes WHERE id=?", (sb["id"],))
        assert row_before["cleanup_eligible_at"] is None  # not DONE yet -- never scheduled early

        fake = FakeGh()
        client.app.state.github_merge.runner = fake
        client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr")
        client.post(f"/api/tasks/{tid}/merges/{rid}/merge")

        d = client.get(f"/api/tasks/{tid}/decision").json()
        assert d["status"] == "DONE"
        row_after = client.app.state.db.one("SELECT status,cleanup_eligible_at FROM sandboxes WHERE id=?", (sb["id"],))
        assert row_after["cleanup_eligible_at"] is not None  # DONE -> cleanup scheduled
        assert row_after["status"] != "CLOSED"  # never immediately deleted (section 14)
    finally:
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")
