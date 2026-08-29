"""Post-Merge DEV Deployment: a Deployment is pinned to the exact
MergeRecord.merged_commit (never an integration/agent branch), driven
entirely through the repo's own PROJECT.yaml commands (build/
local_deploy/smoke/local_status) -- never a shell string constructed by
a route/template -- and is a lifecycle genuinely separate from Task
(a FAILED deployment never moves Task off DONE). Reuses
test_merge_reconciliation.py's FakeGh pattern (duplicated, not
imported, per this repo's convention); build/local_deploy/smoke run as
REAL trivial shell commands (echo/true) against a REAL disposable git
repo -- only the HTTP health check and (where a test needs a specific
failure) the shell command's own exit code are faked/configured,
matching this repo's 'real where cheap and safe' testing convention."""
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
            pr["state"] = "MERGED"; pr["mergeCommit"] = self.real_head(cwd, "main"); pr["mergedAt"] = "2026-01-01T00:00:00Z"
            return FR(0, "")
        return FR(1, "", f"unhandled fake gh/git call: {argv}")


PROJECT_YAML_TEMPLATE = """
schema_version: 1
project: {{code: {name}}}
source: {{root: .}}
ci: {{required: [preflight, test]}}

service:
  healthcheck:
    type: http
    url: http://127.0.0.1:19999/health

commands:
  preflight: {{command: 'true'}}
  test: {{command: 'true'}}
  build:
    command: "echo BUILD_OK"
    working_directory: .
    timeout_seconds: 60
  local_deploy:
    command: "echo DEPLOY_OK"
    working_directory: .
    timeout_seconds: 60
  smoke:
    command: "{smoke_cmd}"
    working_directory: .
    timeout_seconds: 60
  local_status:
    command: "echo URL=http://127.0.0.1:19999"
    working_directory: .

deployment:
  local:
    enabled: true
    approval_required: false
    target: demo-dev
"""


def make_deployable_repo(root, name="demo", smoke_cmd="echo SMOKE_OK"):
    """A disposable repo whose PROJECT.yaml declares a real (if trivial)
    build/local_deploy/smoke/local_status contract -- the exact same
    commands.<name> shape ProjectFlow already reads for preflight/test,
    just naming deploy-lifecycle stages instead. One single `commands:`
    mapping (a real bug caught writing this fixture: YAML silently lets
    a LATER top-level `commands:` key clobber an earlier one instead of
    merging, so preflight/test would otherwise vanish)."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    run(repo, "git", "init", "-b", "main")
    run(repo, "git", "config", "user.email", "test@example.invalid")
    run(repo, "git", "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    (repo / "PROJECT.yaml").write_text(PROJECT_YAML_TEMPLATE.format(name=name, smoke_cmd=smoke_cmd))
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "base + deployment contract")
    # DeploymentService._prepare_source does a REAL `git fetch origin
    # main` (real production repos always have one; github_merge's own
    # tests fake this at the gh/git-runner layer, but this specific step
    # calls git directly since it's pinning the SHARED checkout, not
    # talking to GitHub) -- a self-pointing origin lets that real fetch
    # succeed without needing an actual GitHub remote.
    run(repo, "git", "remote", "add", "origin", str(repo))
    return repo


def create_task(client, title, rid, agent="claude", risk="LOW"):
    r = client.post("/api/tasks/create", data={"title": title, "repository_id": rid, "agent": agent, "sandbox_profile": "NONE", "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].split("/")[-1])


def submit_and_review(client, w, result="PASS"):
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    r = client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": result}, follow_redirects=False)
    assert r.status_code == 303


class FakeResp:
    def __init__(self, status=200): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def done_task(client, root, repo_name="demo", smoke_cmd="echo SMOKE_OK"):
    """Task -> DONE via the real create-pr/merge flow (fake gh), repo's
    PROJECT.yaml declares a real deploy contract."""
    repo = make_deployable_repo(root, repo_name, smoke_cmd)
    register(client, repo, repo_name)
    rid = [r for r in client.get("/api/repositories").json() if r["repo_name"] == repo_name][0]["id"]
    tid = create_task(client, f"Deploy {repo_name}", rid, risk="LOW")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    submit_and_review(client, w, "PASS")
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"
    mr = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=? AND repository_id=?", (tid, rid))
    return tid, rid, mr, repo


def latest_deployment_of(client, tid, rid):
    return client.app.state.db.one("SELECT * FROM deployments WHERE task_id=? AND repository_id=? ORDER BY id DESC LIMIT 1", (tid, rid))


# ------------------------------------------------------------- eligibility
def test_done_task_exposes_deploy_to_dev(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-a")
    html = client.get(f"/tasks/{tid}").text
    assert "Deploy to DEV" in html
    assert "NOT DEPLOYED" in html


def test_non_done_task_cannot_deploy(client, git_repo):
    root, _ = git_repo
    repo = make_deployable_repo(root, "svc-b")
    register(client, repo, "svc-b")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "Not done", rid, risk="LOW")
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 409
    assert "not DONE" in r.text


def test_production_is_never_auto_deployed(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-c")
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "PRODUCTION"}, follow_redirects=False)
    assert r.status_code == 409
    assert "Unknown environment" in r.text or "PRODUCTION" in r.text


def test_target_not_configured_is_reported_honestly(client, git_repo):
    root, repo = git_repo  # the plain conftest fixture repo has NO deployment.local block
    register(client, repo, "demo")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "No target", rid, risk="LOW")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    submit_and_review(client, w, "PASS")
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    html = client.get(f"/tasks/{tid}").text
    assert "DEV target not configured" in html
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 409


# --------------------------------------------------------------- source --
def test_deployment_source_is_exact_merge_commit_never_integration_branch(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-d")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303
    dep = latest_deployment_of(client, tid, rid)
    assert dep["source_commit"] == mr["merged_commit"]
    assert dep["source_branch"] == (mr["base_branch"] or "main")
    assert "integration/" not in dep["source_branch"]
    assert "agent/" not in dep["source_branch"]


# --------------------------------------------------------- idempotency ---
def test_duplicate_deploy_click_is_blocked_and_reuses_state(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-e")
    client.app.state.db.execute(
        "INSERT INTO deployments(task_id,repository_id,environment,source_branch,source_commit,status) VALUES(?,?,?,?,?,'DEPLOYING')",
        (tid, rid, "DEV", "main", mr["merged_commit"]))
    before = client.app.state.db.all("SELECT id FROM deployments WHERE task_id=? AND repository_id=?", (tid, rid))
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303
    after = client.app.state.db.all("SELECT id FROM deployments WHERE task_id=? AND repository_id=?", (tid, rid))
    assert len(after) == len(before)  # no second real deployment started


def test_verified_source_does_not_still_show_deploy_to_dev_as_primary(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-f")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    dep = latest_deployment_of(client, tid, rid)
    assert dep["status"] == "VERIFIED"
    html = client.get(f"/tasks/{tid}").text
    assert "DEV VERIFIED" in html
    assert ">Deploy to DEV<" not in html


# ------------------------------------------------------------ health/smoke
def test_health_failure_prevents_verified(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-g")
    def always_fails(*a, **k): raise OSError("connection refused")
    client.app.state.deployer.http_get = always_fails
    client.app.state.deployer.health_attempts = 1
    client.app.state.deployer.health_delay = 0.01
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303
    dep = latest_deployment_of(client, tid, rid)
    assert dep["status"] == "FAILED"
    assert dep["health_status"] == "FAIL"
    assert "HEALTH_FAILED" in dep["error"]
    # Task must stay DONE regardless of deployment failure (section 1/18).
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"


def test_smoke_failure_prevents_verified(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-h", smoke_cmd="exit 1")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303
    dep = latest_deployment_of(client, tid, rid)
    assert dep["status"] == "FAILED"
    assert dep["health_status"] == "PASS"
    assert dep["smoke_status"] == "FAIL"
    d = client.get(f"/api/tasks/{tid}/decision").json()
    assert d["status"] == "DONE"


def test_successful_deployment_becomes_verified_with_url(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-i")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    r = client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    assert r.status_code == 303
    dep = latest_deployment_of(client, tid, rid)
    assert dep["status"] == "VERIFIED"
    assert dep["health_status"] == "PASS" and dep["smoke_status"] == "PASS"
    assert dep["deployed_url"] == "http://127.0.0.1:19999"
    html = client.get(f"/tasks/{tid}").text
    assert "Open DEV" in html


def test_open_dev_only_rendered_when_url_is_trusted(client, git_repo):
    """No local_status command declared -> no deployed_url -> no Open
    DEV button rendered, never a guessed/browser-derived URL."""
    root, _ = git_repo
    repo = make_deployable_repo(root, "svc-j")
    txt = (repo / "PROJECT.yaml").read_text().replace(
        "  local_status:\n    command: \"echo URL=http://127.0.0.1:19999\"\n    working_directory: .\n", "")
    (repo / "PROJECT.yaml").write_text(txt)
    run(repo, "git", "commit", "-am", "remove local_status")
    register(client, repo, "svc-j")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "No status cmd", rid, risk="LOW")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    submit_and_review(client, w, "PASS")
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    dep = latest_deployment_of(client, tid, rid)
    assert dep["status"] == "VERIFIED" and not dep["deployed_url"]
    html = client.get(f"/tasks/{tid}").text
    assert "Open DEV" not in html


# --------------------------------------------------------------- redeploy
def test_redeploy_uses_same_source_commit_not_current_head(client, git_repo):
    root, _ = git_repo
    tid, rid, mr, repo = done_task(client, root, "svc-k")
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    dep1 = latest_deployment_of(client, tid, rid)
    assert dep1["status"] == "VERIFIED"

    r = client.post(f"/api/deployments/{dep1['id']}/redeploy", follow_redirects=False)
    assert r.status_code == 303
    dep2 = latest_deployment_of(client, tid, rid)
    assert dep2["id"] != dep1["id"]
    assert dep2["source_commit"] == dep1["source_commit"] == mr["merged_commit"]
    assert dep2["status"] == "VERIFIED"
    # historical evidence never overwritten (section 18)
    assert client.app.state.db.one("SELECT status FROM deployments WHERE id=?", (dep1["id"],))["status"] == "VERIFIED"


# ------------------------------------------------------------- multi-repo
def test_multi_repo_deployments_are_repo_scoped(client, git_repo):
    root, _ = git_repo
    repo_a = make_deployable_repo(root, "svc-m1")
    repo_b = make_deployable_repo(root, "svc-m2")
    register(client, repo_a, "svc-m1"); register(client, repo_b, "svc-m2")
    rid_a = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-m1"][0]["id"]
    rid_b = [r for r in client.get("/api/repositories").json() if r["repo_name"] == "svc-m2"][0]["id"]
    r = client.post("/api/tasks/create", data={"title": "Multi repo deploy", "repository_id": rid_a, "agent": "claude", "sandbox_profile": "NONE", "risk_profile": "LOW"}, follow_redirects=False)
    tid = int(r.headers["location"].split("/")[-1])
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid_b, "agent": "codex", "role": "b", "base_branch": "main", "sandbox_profile": "NONE"}, follow_redirects=False)
    for w in client.get(f"/api/tasks/{tid}").json()["workspaces"]:
        submit_and_review(client, w, "PASS")
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    client.post(f"/api/tasks/{tid}/merges/{rid_a}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid_a}/merge", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid_b}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid_b}/merge", follow_redirects=False)
    assert client.get(f"/api/tasks/{tid}/decision").json()["status"] == "DONE"

    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid_a, "environment": "DEV"}, follow_redirects=False)
    dep_a = latest_deployment_of(client, tid, rid_a)
    dep_b = latest_deployment_of(client, tid, rid_b)
    assert dep_a and dep_a["status"] == "VERIFIED"
    assert dep_b is None  # repo B never deployed just because repo A was


# ------------------------------------------------------------------ secrets
def test_secrets_are_sanitized_from_deploy_logs(client, git_repo):
    from app.services.deployment_service import sanitize
    raw = "MESFLOW_ADMIN_PASSWORD=Admin@123456\nAPI_TOKEN: abc123secret\nnormal output line"
    cleaned = sanitize(raw)
    assert "Admin@123456" not in cleaned
    assert "abc123secret" not in cleaned
    assert "normal output line" in cleaned


def test_deploy_log_page_has_no_leaked_secret(client, git_repo):
    root, _ = git_repo
    repo = make_deployable_repo(root, "svc-n", smoke_cmd="echo API_TOKEN=leaked-secret-value && exit 1")
    register(client, repo, "svc-n")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = create_task(client, "Secret in log", rid, risk="LOW")
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    submit_and_review(client, w, "PASS")
    fake = FakeGh(); client.app.state.github_merge.runner = fake
    client.post(f"/api/tasks/{tid}/merges/{rid}/create-pr", follow_redirects=False)
    client.post(f"/api/tasks/{tid}/merges/{rid}/merge", follow_redirects=False)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.post(f"/api/tasks/{tid}/deployments", data={"repository_id": rid, "environment": "DEV"}, follow_redirects=False)
    dep = latest_deployment_of(client, tid, rid)
    html = client.get(f"/deployments/{dep['id']}").text
    assert "leaked-secret-value" not in html
    assert "REDACTED" in html
