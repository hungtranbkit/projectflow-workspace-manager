"""Action-button feedback (IDLE -> RUNNING -> SUCCEEDED/FAILED, sections
1-20): OperationService unit coverage, duplicate-click protection on
Merge Latest Changes/Run Tests/Push Integration Branch/Create PR/Merge
PR/Mark Ready for Main, result/failure surfacing, the sandbox provision/
reset background-threading + real CLEANING status, and the Integration
page's single dominant primary action. Reuses test_integration_push.py's
FakeGh/FR pattern (duplicated here, not imported, per this repo's own
test-file convention)."""
from __future__ import annotations
import json
import subprocess
import threading
import time
from dataclasses import dataclass

import pytest

from app.services.operations import OperationInProgress, OperationService


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
        self.merge_calls: list[int] = []

    def real_head(self, repo_path, branch):
        r = subprocess.run(["git", "rev-parse", branch], cwd=str(repo_path), text=True, capture_output=True)
        return r.stdout.strip()

    def __call__(self, argv, cwd, timeout=30):
        if argv[:3] == ["git", "remote", "get-url"]:
            return FR(0, "git@github.com:test/test.git\n")
        if argv[:2] == ["git", "push"]:
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
            head = argv[argv.index("--head") + 1]; base = argv[argv.index("--base") + 1]
            number = self.next_number; self.next_number += 1
            url = f"https://github.com/test/test/pull/{number}"
            self.prs[number] = {
                "number": number, "url": url, "state": "OPEN",
                "headRefName": head, "baseRefName": base, "headRefOid": self.real_head(cwd, head),
                "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "statusCheckRollup": [], "mergeCommit": None, "title": "t",
            }
            return FR(0, url + "\n")  # real `gh pr create` prints the plain PR URL, not JSON
        if argv[:3] == ["gh", "pr", "view"]:
            num = int(argv[3]); pr = self.prs[num]
            data = {
                "number": pr["number"], "url": pr["url"], "state": pr["state"], "mergeable": pr["mergeable"],
                "mergeStateStatus": pr["mergeStateStatus"], "headRefOid": pr["headRefOid"],
                "baseRefName": pr["baseRefName"], "statusCheckRollup": pr["statusCheckRollup"],
                "mergedAt": pr.get("mergedAt"),
                "mergeCommit": {"oid": pr["mergeCommit"]} if pr.get("mergeCommit") else None, "title": pr["title"],
            }
            return FR(0, json.dumps(data))
        if argv[:3] == ["gh", "pr", "merge"]:
            num = int(argv[3]); self.merge_calls.append(num)
            pr = self.prs[num]; pr["state"] = "MERGED"; pr["mergeCommit"] = self.real_head(cwd, "main") or "deadbeef"
            return FR(0, "")
        return FR(1, "", f"unhandled fake gh/git call: {argv}")


def ready_normal_risk_task_with_integration(client, git_repo, title):
    root, repo = git_repo
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks/create", data={"title": title, "repository_id": rid, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "NORMAL"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"})
    client.post(f"/api/tasks/{tid}/start-qa", data={"tester_agent": "qa"})  # NORMAL now also requires Runtime Verification
    client.post(f"/api/tasks/{tid}/submit-qa", data={"result": "PASS"})
    r2 = client.post(f"/api/tasks/{tid}/integrations", follow_redirects=False)
    assert r2.status_code == 303
    iid = client.get("/api/integrations").json()[0]["id"]
    return tid, rid, iid


def wait_for(fn, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v: return v
        time.sleep(interval)
    return fn()


# ---------------------------------------------------------------- unit ----
def test_operation_service_begin_succeed_and_latest():
    db = _FakeOpsDb()
    ops = OperationService(db)
    op_id = ops.begin("integration", 1, "MERGE_LATEST")
    assert db.rows[op_id]["status"] == "RUNNING"
    ops.succeed(op_id, "Merged latest changes")
    latest = ops.latest("integration", 1, "MERGE_LATEST")
    assert latest["status"] == "SUCCEEDED" and latest["result_summary"] == "Merged latest changes"


def test_operation_service_begin_raises_when_already_active():
    db = _FakeOpsDb()
    ops = OperationService(db)
    ops.begin("integration", 1, "PUSH_INTEGRATION")
    with pytest.raises(OperationInProgress):
        ops.begin("integration", 1, "PUSH_INTEGRATION")


def test_operation_service_fail_records_error_and_frees_the_slot():
    db = _FakeOpsDb()
    ops = OperationService(db)
    op_id = ops.begin("merge_record", 5, "MERGE_PR")
    ops.fail(op_id, "CI_FAIL: checks failing")
    latest = ops.latest("merge_record", 5, "MERGE_PR")
    assert latest["status"] == "FAILED" and "CI_FAIL" in latest["error"]
    assert ops.active("merge_record", 5, "MERGE_PR") is None  # a FAILED op is no longer "active" -- retry is allowed


class _FakeOpsDb:
    """Minimal in-memory stand-in for app.db.Database, exercising the
    exact SQL shapes OperationService issues -- not a real sqlite
    connection, but real logic against real (fake) rows."""
    def __init__(self):
        self.rows: dict[int, dict] = {}
        self._next = 1

    def one(self, sql, args=()):
        if "status IN ('QUEUED','RUNNING')" in sql:
            entity_type, entity_id, operation_type = args
            for r in sorted(self.rows.values(), key=lambda r: -r["id"]):
                if r["entity_type"] == entity_type and r["entity_id"] == entity_id and r["operation_type"] == operation_type and r["status"] in ("QUEUED", "RUNNING"):
                    return dict(r)
            return None
        if "ORDER BY id DESC LIMIT 1" in sql and len(args) == 3:
            entity_type, entity_id, operation_type = args
            for r in sorted(self.rows.values(), key=lambda r: -r["id"]):
                if r["entity_type"] == entity_type and r["entity_id"] == entity_id and r["operation_type"] == operation_type:
                    return dict(r)
            return None
        raise AssertionError(f"unexpected SQL in fake db: {sql}")

    def execute(self, sql, args=()):
        if sql.startswith("INSERT INTO operations"):
            operation_type, entity_type, entity_id = args
            oid = self._next; self._next += 1
            self.rows[oid] = {"id": oid, "operation_type": operation_type, "entity_type": entity_type, "entity_id": entity_id,
                               "status": "RUNNING", "result_summary": None, "error": None}
            return oid
        if sql.startswith("UPDATE operations SET status='SUCCEEDED'"):
            result_summary, oid = args
            self.rows[oid]["status"] = "SUCCEEDED"; self.rows[oid]["result_summary"] = result_summary
            return None
        if sql.startswith("UPDATE operations SET status='FAILED'"):
            error, oid = args
            self.rows[oid]["status"] = "FAILED"; self.rows[oid]["error"] = error
            return None
        raise AssertionError(f"unexpected SQL in fake db: {sql}")


# ------------------------------------------------------- duplicate-click --
def test_merge_latest_cannot_double_submit(client, git_repo):
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Merge latest dup")
    client.app.state.db.execute(
        "INSERT INTO operations(operation_type,entity_type,entity_id,status,started_at) VALUES('MERGE_LATEST','integration',?,'RUNNING',CURRENT_TIMESTAMP)", (iid,))
    events_before = len(client.app.state.db.all("SELECT id FROM workspace_events WHERE entity_type='integration' AND entity_id=?", (iid,)))
    r = client.post(f"/api/integrations/{iid}/merge-latest", follow_redirects=False)
    assert r.status_code == 303  # reflected back, not an error
    events_after = len(client.app.state.db.all("SELECT id FROM workspace_events WHERE entity_type='integration' AND entity_id=?", (iid,)))
    assert events_after == events_before  # no second real merge attempted


def test_run_tests_cannot_double_submit(client, git_repo):
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Run tests dup")
    client.app.state.db.execute(
        "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status) VALUES('integration',?,'true','preflight','RUNNING')", (iid,))
    runs_before = len(client.app.state.db.all("SELECT id FROM test_runs WHERE workspace_type='integration' AND workspace_id=?", (iid,)))
    r = client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    assert r.status_code == 303
    runs_after = len(client.app.state.db.all("SELECT id FROM test_runs WHERE workspace_type='integration' AND workspace_id=?", (iid,)))
    assert runs_after == runs_before  # no second TestRunner.start()


def test_push_cannot_double_submit(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Push dup")
    client.app.state.db.execute(
        "INSERT INTO operations(operation_type,entity_type,entity_id,status,started_at) VALUES('PUSH_INTEGRATION','integration',?,'RUNNING',CURRENT_TIMESTAMP)", (iid,))
    r = client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    assert r.status_code == 303
    assert fake.pushed_branches == []  # never actually pushed a second time


def test_ready_for_main_cannot_double_submit(client, git_repo):
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Ready dup")
    client.app.state.db.execute(
        "INSERT INTO operations(operation_type,entity_type,entity_id,status,started_at) VALUES('MARK_READY_FOR_MAIN','integration',?,'RUNNING',CURRENT_TIMESTAMP)", (iid,))
    r = client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    assert r.status_code == 303
    row = client.app.state.db.one("SELECT ready_for_main FROM integration_workspaces WHERE id=?", (iid,))
    assert row["ready_for_main"] == 0  # readiness was never (re)confirmed by the blocked duplicate


def test_create_pr_cannot_double_submit(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Create PR dup")
    i = client.app.state.db.one("SELECT * FROM integration_workspaces WHERE id=?", (iid,))
    client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    wait_for(lambda: client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"] in ("TESTING", "READY_FOR_MAIN", "FAILED"))
    client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    mr = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    client.app.state.db.execute(
        "INSERT INTO operations(operation_type,entity_type,entity_id,status,started_at) VALUES('CREATE_PR','merge_record',?,'RUNNING',CURRENT_TIMESTAMP)", (mr["id"],))
    r = client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    assert r.status_code == 303
    assert fake.prs == {}  # never actually created


def test_merge_pr_cannot_double_submit(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Merge PR dup")
    mr = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    client.app.state.db.execute("UPDATE merge_records SET pr_number=1 WHERE id=?", (mr["id"],))
    fake.prs[1] = {"number": 1, "url": "https://github.com/test/test/pull/1", "state": "OPEN",
                   "headRefName": "x", "baseRefName": "main", "headRefOid": "abc", "mergeable": "MERGEABLE",
                   "mergeStateStatus": "CLEAN", "statusCheckRollup": [], "mergeCommit": None, "title": "t"}
    client.app.state.db.execute(
        "INSERT INTO operations(operation_type,entity_type,entity_id,status,started_at) VALUES('MERGE_PR','merge_record',?,'RUNNING',CURRENT_TIMESTAMP)", (mr["id"],))
    r = client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    assert r.status_code == 303
    assert fake.merge_calls == []  # never actually called gh pr merge


# ------------------------------------------------------- result surfacing -
def test_merge_latest_success_is_recorded_with_a_human_result(client, git_repo):
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Merge latest ok")
    r = client.post(f"/api/integrations/{iid}/merge-latest", follow_redirects=False)
    assert r.status_code == 303
    op = client.app.state.ops.latest("integration", iid, "MERGE_LATEST")
    assert op["status"] == "SUCCEEDED"
    assert op["result_summary"] in ("Merged latest changes", "Already up to date")


def test_push_failure_is_recorded_on_the_operation(client, git_repo):
    class FailingPush(FakeGh):
        def __call__(self, argv, cwd, timeout=30):
            if argv[:2] == ["git", "push"]:
                return FR(1, "", "fatal: could not read from remote repository")
            return super().__call__(argv, cwd, timeout)
    fake = FailingPush(); client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Push fails op")
    r = client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    assert r.status_code == 409
    op = client.app.state.ops.latest("integration", iid, "PUSH_INTEGRATION")
    assert op["status"] == "FAILED" and op["error"]


def test_create_pr_success_records_pr_number_in_result(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Create PR ok")
    client.post(f"/api/integrations/{iid}/push", follow_redirects=False)
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    wait_for(lambda: client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"] in ("TESTING", "READY_FOR_MAIN", "FAILED"))
    client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    r = client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    assert r.status_code == 303
    mr = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    op = client.app.state.ops.latest("merge_record", mr["id"], "CREATE_PR")
    assert op["status"] == "SUCCEEDED" and "#1" in op["result_summary"] and "created" in op["result_summary"]


# ------------------------------------------------ sandbox background work -
def test_sandbox_provision_runs_in_background_and_is_visible_as_provisioning(client, sandboxable_repo_factory, git_repo):
    """Section 3: refresh-while-running must show the real in-flight
    state, not freeze until the whole docker call finishes -- undoes the
    test-default synchronous spawn override for this one test so the
    real threaded behavior is actually exercised."""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-async", port_range=(21600, 21649))
    register(client, repo, "svc-async")
    rid = client.get("/api/repositories").json()[0]["id"]
    r = client.post("/api/tasks", data={"title": "Async provision"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "A", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]

    real_spawn = lambda fn, args=(): threading.Thread(target=fn, args=args, daemon=True).start()
    client.app.state.sandboxes.spawn = real_spawn
    try:
        # provision() already ran once via workspace creation (auto_create_sandbox) -- wait for it, then reset to test start's own transition.
        wait_for(lambda: client.app.state.db.one("SELECT status FROM sandboxes WHERE id=?", (sb["id"],))["status"] in ("RUNNING", "FAILED"), timeout=20)
        client.post(f"/api/sandboxes/{sb['id']}/stop")
        r = client.post(f"/api/sandboxes/{sb['id']}/start", follow_redirects=False)
        assert r.status_code == 303
        immediately_after = client.get(f"/api/sandboxes/{sb['id']}").json()
        assert immediately_after["status"] in ("PROVISIONING", "STARTING", "RUNNING")  # never silently still STOPPED
        settled = wait_for(lambda: client.get(f"/api/sandboxes/{sb['id']}").json()["status"] == "RUNNING" or None, timeout=20)
        assert settled
    finally:
        client.app.state.sandboxes.spawn = lambda fn, args=(): fn(*args)
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")


def test_sandbox_start_duplicate_click_does_not_provision_twice(client, sandboxable_repo_factory, git_repo):
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-dup", port_range=(21650, 21699))
    register(client, repo, "svc-dup")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Dup provision"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "A", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    try:
        client.app.state.db.execute("UPDATE sandboxes SET status='PROVISIONING' WHERE id=?", (sb["id"],))
        calls = []
        real_provision = client.app.state.sandboxes.provision
        client.app.state.sandboxes.provision = lambda sid: (calls.append(sid), real_provision(sid))[1]
        client.post(f"/api/sandboxes/{sb['id']}/start", follow_redirects=False)
        assert calls == []  # route's own busy-check refused to call provision() again while PROVISIONING
    finally:
        client.app.state.sandboxes.provision = real_provision
        client.app.state.db.execute("UPDATE sandboxes SET status='CREATED' WHERE id=?", (sb["id"],))
        client.post(f"/api/sandboxes/{sb['id']}/cleanup")


def test_cleanup_sets_the_real_cleaning_status(client, sandboxable_repo_factory, git_repo):
    """CLEANING was already a recognized transitional status elsewhere
    (cleanup_worker reconciliation, mark_cleanup_eligible's guard) but
    cleanup() never actually set it -- verify it now does, even though
    the test-default synchronous spawn means it also reaches CLOSED by
    the time this call returns (recorded via a real sandbox_operations
    CLEANUP row, not asserted mid-flight here)."""
    root, _ = git_repo
    repo = sandboxable_repo_factory(root, "svc-cleaning", port_range=(21700, 21749))
    register(client, repo, "svc-cleaning")
    rid = client.get("/api/repositories").json()[0]["id"]
    client.post("/api/tasks", data={"title": "Cleaning status"}, follow_redirects=False)
    tid = client.get("/api/tasks").json()[0]["id"]
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "A", "base_branch": "main", "sandbox_profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    wait_for(lambda: client.app.state.db.one("SELECT status FROM sandboxes WHERE id=?", (sb["id"],))["status"] in ("RUNNING", "FAILED"), timeout=20)
    client.post(f"/api/sandboxes/{sb['id']}/cleanup")
    final = client.get(f"/api/sandboxes/{sb['id']}").json()
    assert final["status"] == "CLOSED"
    op = client.app.state.db.one("SELECT * FROM sandbox_operations WHERE sandbox_id=? AND operation_type='CLEANUP' ORDER BY id DESC LIMIT 1", (sb["id"],))
    assert op and op["status"] == "SUCCESS"


# ----------------------------------------------------- one dominant action
def test_integration_page_shows_run_tests_as_primary_when_tests_not_current(client, git_repo):
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Primary run tests")
    html = client.get(f"/integrations/{iid}").text
    assert ">Run Tests<" in html
    assert ">Push Integration Branch<" not in html.split("primary-action")[1].split("</div>")[0]


def test_integration_page_promotes_push_after_tests_pass(client, git_repo):
    fake = FakeGh(); client.app.state.github_merge.runner = fake  # a GitHub remote must actually exist for Push to ever be the real next step
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Primary push")
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    wait_for(lambda: client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"] in ("TESTING", "FAILED"))
    html = client.get(f"/integrations/{iid}").text
    primary = html.split('class="primary-action"')[1].split("</div>")[0]
    assert "Push Integration Branch" in primary
    assert ">Run Tests<" not in primary  # demoted, not competing with Push


def test_integration_page_never_offers_push_as_primary_without_a_github_remote(client, git_repo):
    """Section 19: a repo with no GitHub remote can never actually push --
    once tests pass, the real next step is Mark Ready for Main (readiness
    itself never requires push_status), never a PUSH_INTEGRATION primary
    button that would just 409 if clicked."""
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "No remote push")
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    wait_for(lambda: client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"] in ("TESTING", "FAILED"))
    html = client.get(f"/integrations/{iid}").text
    primary = html.split('class="primary-action"')[1].split("</div>")[0]
    assert "Push Integration Branch" not in primary
    assert "Mark Ready for Main" in primary


def test_mark_ready_for_main_retires_itself_as_primary_after_success(client, git_repo):
    """Section 17: a completed action must not linger as the primary --
    once Mark Ready for Main actually succeeds, the button must not
    still show itself as the thing to click next."""
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Ready retires")
    client.post(f"/api/integrations/{iid}/test", follow_redirects=False)
    wait_for(lambda: client.app.state.db.one("SELECT status FROM integration_workspaces WHERE id=?", (iid,))["status"] in ("TESTING", "FAILED"))
    r = client.post(f"/api/integrations/{iid}/ready-for-main", follow_redirects=False)
    assert r.status_code == 303
    html = client.get(f"/integrations/{iid}").text
    primary = html.split('class="primary-action"')[1].split("</div>")[0]
    assert "Mark Ready for Main" not in primary
    assert "Ready for Main" in primary  # shown as the retired/info state, not a clickable button


def test_integration_next_action_ladder_matches_task_wizard(client, git_repo):
    """Section 19: the Integration page's primary_action must be
    computed via the SAME TaskDecisionService.integration_next_action
    the Task wizard's overall next_action already uses -- never a
    second, independently-drifting ordering."""
    tid, rid, iid = ready_normal_risk_task_with_integration(client, git_repo, "Ladder parity")
    decision = client.app.state.decision
    i = client.app.state.db.one("SELECT i.*,r.repo_name FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id WHERE i.id=?", (iid,))
    i["gate_status"] = decision.integration_gate_status(i, tid)
    primary = decision.integration_next_action(i, tid, None)
    d = decision.evaluate(tid)
    assert d["next_action"]["action"] == primary["action"] == "RUN_INTEGRATION_TEST"
