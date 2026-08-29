"""Post-merge reconciliation and Task completion UX: GitHub's own
merged state is authoritative -- once a PR reports MERGED (through a
Refresh, through Create-PR reusing an existing PR, or through
Workspace Manager's own real Merge action), the MergeRecord is
reconciled immediately (exact merge commit + GitHub's own merged_at
persisted), pre-merge blockers (PR_OPEN/CI_PENDING/UNKNOWN_MERGEABILITY/
SOURCE_STALE/...) stop being evaluated for that repo, and Task
completion (DONE) is recomputed in the same request -- no second
button press, no manual "Mark Merged". Reuses test_real_merge.py's
FakeGh pattern (duplicated here, not imported, per this repo's test
convention)."""
from __future__ import annotations
import json
import subprocess
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

    def real_head(self, repo_path, branch):
        r = subprocess.run(["git", "rev-parse", branch], cwd=str(repo_path), text=True, capture_output=True)
        return r.stdout.strip()

    def register_pr(self, number, head_branch, base_branch, repo_path, state="OPEN", merged_at=None, merge_commit=None, mergeable="MERGEABLE"):
        self.prs[number] = {
            "number": number, "url": f"https://github.com/test/test/pull/{number}", "state": state,
            "headRefName": head_branch, "baseRefName": base_branch, "headRefOid": self.real_head(repo_path, head_branch),
            "mergeable": mergeable, "mergeStateStatus": "CLEAN" if mergeable == "MERGEABLE" else "DIRTY",
            "statusCheckRollup": [], "mergeCommit": merge_commit, "mergedAt": merged_at, "title": "t",
        }
        self.next_number = max(self.next_number, number + 1)

    def __call__(self, argv, cwd, timeout=30):
        if argv[:3] == ["git", "remote", "get-url"]:
            return FR(0, "git@github.com:test/test.git\n")
        if argv[:2] == ["git", "push"]:
            self.pushed_branches.append(argv[3])
            return FR(0, "")
        if argv[:3] == ["gh", "pr", "list"]:
            head = argv[argv.index("--head") + 1]
            base = argv[argv.index("--base") + 1]
            matches = [p for p in self.prs.values() if p["headRefName"] == head and p["baseRefName"] == base and p["state"] != "CLOSED"]
            return FR(0, json.dumps([{"number": p["number"], "url": p["url"], "state": p["state"]} for p in matches[:1]]))
        if argv[:3] == ["gh", "pr", "create"]:
            head = argv[argv.index("--head") + 1]; base = argv[argv.index("--base") + 1]
            num = self.next_number; self.next_number += 1
            url = f"https://github.com/test/test/pull/{num}"
            self.prs[num] = {"number": num, "url": url, "state": "OPEN", "headRefName": head, "baseRefName": base,
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


def merge_record_of(client, tid, rid):
    return client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))


# --------------------------------------------------------- refresh states
def test_refresh_on_open_pr_stays_open(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Stays open")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    r = client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    assert r.status_code == 303
    row = merge_record_of(client, tid, rid)
    assert row["merge_status"] == "PR_OPEN"
    assert row["pr_state"] == "OPEN"


def test_refresh_on_closed_not_merged_pr_does_not_become_merged(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Closed not merged")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    row = merge_record_of(client, tid, rid)
    fake.prs[row["pr_number"]]["state"] = "CLOSED"  # closed WITHOUT merging -- gh reports state=CLOSED, no mergedAt
    r = client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    assert r.status_code == 303
    row = merge_record_of(client, tid, rid)
    assert row["merge_status"] != "MERGED"
    assert row["merged_commit"] is None
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] != "DONE"


def test_refresh_detects_merged_pr_and_persists_exact_sha(client, git_repo):
    """Sections 1/5/7: the real bug -- a PR merged externally on GitHub
    must be reconciled by a single Refresh, exact merge commit + GitHub's
    own merged_at persisted, never left at stale PR_OPEN."""
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "External merge")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    row = merge_record_of(client, tid, rid)
    fake.register_pr(row["pr_number"], w["branch"], "main", client.app.state.db.one("SELECT repo_path FROM repositories WHERE id=?", (rid,))["repo_path"],
                      state="MERGED", merged_at="2026-03-14T12:00:00Z", merge_commit="deadbeef" * 5)

    r = client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    assert r.status_code == 303
    row = merge_record_of(client, tid, rid)
    assert row["merge_status"] == "MERGED"
    assert row["merged_commit"] == "deadbeef" * 5
    assert row["merged_at"] == "2026-03-14T12:00:00Z"
    assert row["pr_state"] == "MERGED"


# --------------------------------------------------- stale blocker cleanup
def test_merged_repo_clears_blockers_and_stops_evaluating_mergeability(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "No more blockers")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    row = merge_record_of(client, tid, rid)
    repo_path = client.app.state.db.one("SELECT repo_path FROM repositories WHERE id=?", (rid,))["repo_path"]
    fake.register_pr(row["pr_number"], w["branch"], "main", repo_path, state="MERGED",
                      merged_at="2026-03-14T12:00:00Z", merge_commit="c" * 40, mergeable="UNKNOWN")
    client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)

    d = client.app.state.decision.evaluate(tid)
    row = merge_record_of(client, tid, rid)
    gate = client.app.state.decision.merge_gate_status(d, rid, row)
    assert gate["blockers"] == []
    assert gate["eligible"] is False
    assert gate.get("merged") is True


# ----------------------------------------------------------- task DONE ---
def test_one_repo_task_becomes_done_on_refresh_detecting_merge(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "One repo done")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    row = merge_record_of(client, tid, rid)
    repo_path = client.app.state.db.one("SELECT repo_path FROM repositories WHERE id=?", (rid,))["repo_path"]
    fake.register_pr(row["pr_number"], w["branch"], "main", repo_path, state="MERGED", merged_at="2026-03-14T12:00:00Z", merge_commit="d" * 40)

    d_before = client.get(f"/api/tasks/{tid}/decision").json()
    assert d_before["status"] != "DONE"
    r = client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    assert r.status_code == 303
    d_after = client.get(f"/api/tasks/{tid}/decision").json()
    assert d_after["status"] == "DONE"
    assert d_after["next_action"]["action"] in ("CLOSE_TASK", "NONE")


def test_multi_repo_partial_merge_stays_active_not_done(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    root, repo_a = git_repo
    register(client, repo_a, "repo-a")
    from tests.conftest import make_repo
    repo_b = make_repo(root, "repo-b")
    register(client, repo_b, "repo-b")
    rid_a = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "repo-a"][0]["id"]
    rid_b = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "repo-b"][0]["id"]
    r = client.post("/api/tasks/create", data={"title": "Two repos", "repository_id": rid_a, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid_b, "agent": "codex", "role": "b", "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    for w in client.get(f"/api/tasks/{tid}").json()["workspaces"]:
        submit_and_review(client, w, "PASS")

    wa = next(w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["repository_id"] == rid_a)
    client.post(f"/api/tasks/{tid}/merges/{rid_a}/create-pr", follow_redirects=False)
    row_a = merge_record_of(client, tid, rid_a)
    repo_path_a = client.app.state.db.one("SELECT repo_path FROM repositories WHERE id=?", (rid_a,))["repo_path"]
    fake.register_pr(row_a["pr_number"], wa["branch"], "main", repo_path_a, state="MERGED", merged_at="2026-03-14T12:00:00Z", merge_commit="a" * 40)
    client.post(f"/api/tasks/{tid}/merges/{rid_a}/refresh", follow_redirects=False)

    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] != "DONE"  # repo B not merged yet
    row_a = merge_record_of(client, tid, rid_a)
    row_b = merge_record_of(client, tid, rid_b)
    assert row_a["merge_status"] == "MERGED"
    assert row_b["merge_status"] != "MERGED"

    # now merge repo B too -> Task DONE
    wb = next(w for w in client.get(f"/api/tasks/{tid}").json()["workspaces"] if w["repository_id"] == rid_b)
    client.post(f"/api/tasks/{tid}/merges/{rid_b}/create-pr", follow_redirects=False)
    row_b = merge_record_of(client, tid, rid_b)
    repo_path_b = client.app.state.db.one("SELECT repo_path FROM repositories WHERE id=?", (rid_b,))["repo_path"]
    fake.register_pr(row_b["pr_number"], wb["branch"], "main", repo_path_b, state="MERGED", merged_at="2026-03-14T12:00:00Z", merge_commit="b" * 40)
    client.post(f"/api/tasks/{tid}/merges/{rid_b}/refresh", follow_redirects=False)
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"


def test_workspace_manager_merge_immediately_makes_task_done(client, git_repo):
    """Section 6: no later Refresh needed -- the real /merge action
    itself reconciles + recomputes completion in the same request."""
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "WM merge done")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    r = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert r.status_code == 303
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"
    row = merge_record_of(client, tid, rid)
    assert row["merge_status"] == "MERGED"
    assert row["merged_commit"]
    assert row["merged_at"]


# ------------------------------------------------------------ idempotency
def test_repeated_refresh_after_merge_is_idempotent(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Idempotent refresh")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    row = merge_record_of(client, tid, rid)
    repo_path = client.app.state.db.one("SELECT repo_path FROM repositories WHERE id=?", (rid,))["repo_path"]
    fake.register_pr(row["pr_number"], w["branch"], "main", repo_path, state="MERGED", merged_at="2026-03-14T12:00:00Z", merge_commit="e" * 40)
    client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    row1 = merge_record_of(client, tid, rid)

    client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    row2 = merge_record_of(client, tid, rid)
    assert row2["merge_status"] == "MERGED"
    assert row2["merged_commit"] == row1["merged_commit"]
    assert row2["merged_at"] == row1["merged_at"]
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"

    events = client.app.state.db.all("SELECT action FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='TASK_COMPLETED'", (tid,))
    assert len(events) == 1  # no duplicate TASK_COMPLETED across 3 refreshes
    merge_events = client.app.state.db.all("SELECT action FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='PR_MERGED_DETECTED'", (tid,))
    assert len(merge_events) == 1  # only the transition PR_OPEN->MERGED gets one, not every refresh after


def test_task_completed_audit_has_repo_and_merge_details(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Audit details")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    ev = client.app.state.db.one("SELECT * FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='TASK_COMPLETED'", (tid,))
    assert ev and "pr=" in ev["details"] and "merge_sha=" in ev["details"]
    refreshed = client.app.state.db.all("SELECT * FROM workspace_events WHERE entity_type='task' AND entity_id=? AND action='PR_REFRESHED'", (tid,))
    assert refreshed and "source=WORKSPACE_MANAGER_MERGE" in refreshed[-1]["details"]


# --------------------------------------------------------------- DONE UI -
def test_done_ui_hides_merge_create_pr_and_shows_view_pr(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Done UI")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)

    html = client.get(f"/tasks/{tid}").text
    assert ">DONE<" in html
    assert "View PR #" in html
    # No lingering pre-merge controls in the primary wizard view (Advanced
    # details section further down may still show historical facts, but
    # never an actionable Merge/Create PR button for an already-merged repo).
    wizard_html = html.split('id="advanced-details"')[0]
    assert ">Merge</button>" not in wizard_html
    assert ">Create PR</button>" not in wizard_html
    assert "Mark Ready for Main" not in wizard_html


def test_done_ui_shows_verification_sandbox_action_when_supported(client, sandboxable_repo_factory, git_repo):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-verify", port_range=(21800, 21849))
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    register(client, repo, "svc-verify")
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-verify"][0]["id"]
    r = client.post("/api/tasks/create", data={"title": "Verify sandbox", "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "NORMAL"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    submit_and_review(client, w, "PASS")
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})  # NORMAL now also requires Runtime Verification
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    client.post(f"/api/integrations/1/test", follow_redirects=False)
    import time
    for _ in range(50):
        st = client.app.state.db.one("SELECT status FROM integration_workspaces WHERE task_integration_id=1")["status"]
        if st in ("TESTING", "FAILED"): break
        time.sleep(0.1)
    client.post(f"/api/integrations/1/ready-for-main", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)

    html = client.get(f"/tasks/{tid}").text
    assert ">DONE<" in html
    assert "Verified source" in html
    # Real bug found via screenshot: Task DONE -> _schedule_cleanup_if_done
    # immediately flips a healthy sandbox's status from RUNNING to
    # CLEANUP_ELIGIBLE (retention countdown, container still genuinely
    # running/healthy for the whole grace window) -- View Running App
    # must still be the primary action, never demoted back to "Create
    # Verification Sandbox" just because a completed Task started its
    # sandbox's cleanup countdown.
    sb = client.get("/api/sandboxes").json()[0]
    assert sb["status"] == "CLEANUP_ELIGIBLE" and sb["health_status"] == "HEALTHY"
    assert "View Running App" in html
    assert "Create Verification Sandbox" not in html


# --------------------------------------------------- consistency invariant
def assert_no_contradictory_view_model(client, tid, rid):
    """Section 7: it must be impossible to produce a view model where
    (a) a MergeRecord is MERGED but its own pr_state still reads OPEN,
    (b) every required repo is MERGED but Task.status still reads
    READY_FOR_MAIN, or (c) a MergeRecord classified merge_complete
    (merge_gate_status merged=True) still carries non-empty blockers.
    Called after every state-changing action in this file that could
    plausibly produce a merged repo, so a regression fails loudly in
    tests rather than silently shipping to production again."""
    d = client.get(f"/api/tasks/{tid}/decision").json()
    row = merge_record_of(client, tid, rid)
    if row["merge_status"] == "MERGED":
        assert row["pr_state"] != "OPEN", "MERGED MergeRecord must never keep pr_state=OPEN"
    required = [m for m in d["merge_records"] if m["required"]]
    all_merged = bool(required) and all(m["merge_status"] == "MERGED" for m in required)
    if all_merged:
        assert d["status"] not in ("READY_FOR_MAIN", "ACTIVE", "BLOCKED"), \
            f"all required repos merged but Task.status is still {d['status']!r}, not DONE"
        assert d["status"] == "DONE"
    gate = client.app.state.decision.merge_gate_status(d, rid, row)
    if gate.get("merged"):
        assert gate["blockers"] == [], "a merge_complete repo must never carry non-empty blockers"


def test_no_contradictory_state_across_the_full_lifecycle(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Invariant check")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    assert_no_contradictory_view_model(client, tid, rid)  # OPEN state: no contradiction yet
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert_no_contradictory_view_model(client, tid, rid)  # MERGED state: DONE, no blockers, pr_state != OPEN
    client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    assert_no_contradictory_view_model(client, tid, rid)  # repeated refresh: still consistent


# --------------------------------------------------- Task #5 real regression
def test_task5_equivalent_state_renders_done_with_no_stale_text(client, git_repo):
    """Section 9/11: the exact real-world shape Task #5 was stuck in --
    single required repo, PR already MERGED on GitHub with CI PASS,
    Integration READY_FOR_MAIN -- must render DONE with none of the
    stale pre-merge text once reconciled, and the Advanced Details view
    must carry no bare/empty heading for the Integration Sources list."""
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, w = ready_low_risk_task(client, git_repo, "Task 5 shape", repo_name="mesflow-app")
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    row = merge_record_of(client, tid, rid)
    repo_path = client.app.state.db.one("SELECT repo_path FROM repositories WHERE id=?", (rid,))["repo_path"]
    fake.prs[row["pr_number"]]["statusCheckRollup"] = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    fake.register_pr(row["pr_number"], w["branch"], "main", repo_path, state="MERGED",
                      merged_at="2026-08-29T01:46:18Z", merge_commit="92e4162ecb0624f8aec4cc9d95e35682a2ab1850")
    fake.prs[row["pr_number"]]["statusCheckRollup"] = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]

    r = client.post(f"/api/tasks/{tid}/merges/{rid}/refresh", follow_redirects=False)
    assert r.status_code == 303
    html = client.get(f"/tasks/{tid}").text
    assert ">DONE<" in html
    assert "MERGED" in html
    assert "All required repos merged to main" in html
    assert "✓ All required repos merged to main" in html
    for stale in ("PR OPEN", "UNKNOWN_MERGEABILITY", "SOURCE_STALE"):
        assert stale not in html, f"stale pre-merge text {stale!r} must not survive reconciliation"
    # Advanced Details: no bare "Integration Sources" label with nothing under it.
    advanced = html.split('id="advanced-details"')[1] if 'id="advanced-details"' in html else ""
    assert "<p><small>Integration Sources</small></p>\n</section>" not in advanced
    assert_no_contradictory_view_model(client, tid, rid)
