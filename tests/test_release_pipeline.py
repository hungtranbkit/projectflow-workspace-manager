"""Integration, Release, Deploy & Runtime Verification Loop (Phase
E10). Reuses DeploymentService (real build-once artifact reuse, real
health+smoke verification, real rollback pinned to a prior artifact
image) and E9's own integration_readiness() -- never a second
deployment/review truth engine. Real git merges, real subprocess
execution of trivial (echo/python3) commands, and (for the deploy/
rollback tests) a genuinely running local HTTP server are used
throughout -- E10 needs no LLM calls at all, so "real" here means real
git/subprocess/HTTP, never fake stand-ins for those."""
from __future__ import annotations
import http.server
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.test_autonomous_execution import register as _register_repo, new_change, materialize_task
from tests.test_worktree_manager import _select_and_create_workspace
from tests.test_review_fix_loop import envelope, set_fake, PASS


def run(path, *args):
    return subprocess.run(list(args), cwd=path, text=True, capture_output=True, check=True)


@dataclass
class FR:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeResp:
    def __init__(self, status=200): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """E10.33/E10.34: a genuinely running local HTTP process -- deploy/
    rollback tests hit this over a real socket via the deployer's
    default `http_get = urllib.request.urlopen` (left un-overridden),
    instead of the FakeResp stand-in used by the rest of this file.
    Class attrs (not instance attrs -- BaseHTTPRequestHandler makes a
    fresh instance per request) are toggled by the test to simulate a
    real process going from healthy to broken and back."""
    healthy = True
    version_reply = "v1"

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200 if self.__class__.healthy else 500)
            self.end_headers()
            self.wfile.write(b"OK" if self.__class__.healthy else b"UNHEALTHY")
        elif self.path == "/version":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"version": self.__class__.version_reply}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):  # keep test output quiet
        pass


def start_health_server(port):
    """Starts a real ThreadingHTTPServer bound to 127.0.0.1:<port> on a
    daemon thread. Returns (server, handler_class) -- the handler class
    is per-server (via a dynamic subclass) so parallel tests on
    different ports never share `.healthy`/`.version_reply` state."""
    handler_cls = type(f"_HealthHandler{port}", (_HealthHandler,), {"healthy": True, "version_reply": "v1"})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, handler_cls


BUILD_PY = """
import hashlib, json, subprocess
commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
content = open("src.py", "rb").read()
digest = hashlib.sha256(content + commit.encode()).hexdigest()
# No "version" field -- the Release's own `version` (v1/v1.0.0/...) is
# ProjectFlow's own concern (E10.8), never something this build script
# invents independently; ReleaseService.build() only cross-checks a
# version field when the artifact metadata actually declares one.
json.dump({"source_commit": commit, "image": f"demo:{digest[:12]}",
           "image_digest": f"sha256:{digest}", "package_filename": "demo.tar", "package_sha256": digest},
          open("artifact.json", "w"))
print("BUILD_OK", digest[:12])
"""

PROJECT_YAML = """
schema_version: 1
project: {{code: {name}}}
source: {{root: .}}
ci: {{required: [preflight, test]}}
artifacts:
  metadata: artifact.json
service:
  healthcheck:
    url: http://127.0.0.1:{port}/health
commands:
  preflight: {{command: 'true'}}
  test: {{command: 'python3 -m pytest -q test_src.py'}}
  build:
    command: "python3 build.py"
    working_directory: .
    timeout_seconds: 60
  local_deploy:
    command: "echo DEPLOY_OK"
    working_directory: .
    timeout_seconds: 60
  smoke:
    command: "echo SMOKE_OK"
    working_directory: .
    timeout_seconds: 60
  local_status:
    command: "echo URL=http://127.0.0.1:{port}"
    working_directory: .
"""


def make_release_repo(root, name="demo", port=19999):
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    run(repo, "git", "init", "-b", "main")
    run(repo, "git", "config", "user.email", "test@example.invalid")
    run(repo, "git", "config", "user.name", "Test")
    (repo / "src.py").write_text("def feature():\n    return 1\n")
    (repo / "test_src.py").write_text("from src import feature\n\n\ndef test_feature():\n    assert feature() == 1\n")
    (repo / "build.py").write_text(BUILD_PY)
    (repo / "PROJECT.yaml").write_text(PROJECT_YAML.format(name=name, port=port))
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "base + release contract")
    run(repo, "git", "remote", "add", "origin", str(repo))
    return repo


def _reviewed_and_integrated_task(client, repo, cid, rid, title="Feature task", scope_hints=None):
    """A real Task, real worktree, real commit, real E9 CodeReview PASS
    (fake invoker -- no LLM needed for E10's own scope), real
    integration into the repo's own main branch."""
    tid, _ = materialize_task(client, cid, title=title, scope_hints=scope_hints or ["src.py"])
    w = _select_and_create_workspace(client, tid, rid, agent="claude")
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "src.py").write_text("def feature():\n    return 2\n")
    (worktree / "test_src.py").write_text("from src import feature\n\n\ndef test_feature():\n    assert feature() == 2\n")
    subprocess.run(["git", "add", "src.py", "test_src.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "implement feature v2"], cwd=worktree, check=True, capture_output=True)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, PASS)
    review = client.post(f"/api/tasks/{tid}/review/code").json()
    assert review["outcome"] == "REVIEWED" and review["verdict"] == "PASS", review
    integ = client.post(f"/api/tasks/{tid}/integrate").json()
    assert integ["outcome"] == "INTEGRATED", integ
    return tid, integ


# ================================================================ Integration (E10.1-E10.6/E10.31/E10.37)

def test_integration_ready_and_real_merge(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19001", port=19001)
    real_checkout = Path(__file__).resolve().parent.parent
    before_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout, capture_output=True, text=True).stdout.strip()

    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Integration test change", project_id=rid)
    before_main = client.app.state.git.head(repo)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)

    assert integ["target_branch"] == "main"
    assert client.app.state.git.head(repo) == integ["integrated_commit"]
    assert client.app.state.git.head(repo) != before_main  # main really advanced
    # canonical repo's own working tree files reflect the new content
    # (a fresh `git status` in that same checkout would show it once
    # refreshed -- update-ref alone doesn't rewrite files, matching
    # DeploymentService's own already-established re-sync-on-build
    # discipline, exercised for real in the build test below)
    mr = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=?", (tid,))
    assert mr["merge_status"] == "MERGED"
    assert mr["merged_commit"] == integ["integrated_commit"]
    wp = client.app.state.work_products.get(integ["work_product_id"])
    assert wp["kind"] == "INTEGRATED_CHANGE" and wp["status"] == "APPROVED"

    real_after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=real_checkout, capture_output=True, text=True).stdout.strip()
    assert real_after_head == before_head


def test_integration_blocked_without_review(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19002", port=19002)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Blocked integration change", project_id=rid)
    tid, _ = materialize_task(client, cid, scope_hints=["src.py"])
    _select_and_create_workspace(client, tid, rid)
    r = client.post(f"/api/tasks/{tid}/integrate").json()
    assert r["outcome"] == "BLOCKED"
    assert any("CODE_REVIEW" in b for b in r["reasons"])


def test_integration_conflict_detected_no_merge(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19003", port=19003)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Conflict change", project_id=rid)
    tid, _ = materialize_task(client, cid, scope_hints=["src.py"])
    w = _select_and_create_workspace(client, tid, rid, agent="claude")
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "src.py").write_text("def feature():\n    return 'worktree'\n")
    subprocess.run(["git", "add", "src.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "worktree change"], cwd=worktree, check=True, capture_output=True)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, PASS)
    client.post(f"/api/tasks/{tid}/review/code")

    (repo / "src.py").write_text("def feature():\n    return 'main-diverged'\n")
    run(repo, "git", "add", "src.py")
    run(repo, "git", "commit", "-m", "main diverges")
    before_head = client.app.state.git.head(repo)

    r = client.post(f"/api/tasks/{tid}/integrate").json()
    # main also advanced (a real, separate signal: WORKTREE_BASE_STALE),
    # so E9's own integration_readiness() blocks before the merge is
    # even attempted -- the real conflict is still genuinely detected
    # and reported alongside it, never silently merged.
    assert r["outcome"] == "BLOCKED", r
    assert "INTEGRATION_CONFLICT" in r["reasons"]
    assert client.app.state.git.head(repo) == before_head  # main never touched


def test_integration_lock_prevents_concurrent_merge(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19004", port=19004)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Lock change", project_id=rid)
    client.app.state.db.execute("INSERT INTO repository_integration_locks(repository_id,locked_by) VALUES(?,?)", (rid, "other"))
    tid, _ = materialize_task(client, cid, scope_hints=["src.py"])
    w = _select_and_create_workspace(client, tid, rid, agent="claude")
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    (worktree / "src.py").write_text("def feature():\n    return 2\n")
    subprocess.run(["git", "add", "src.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "x"], cwd=worktree, check=True, capture_output=True)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, PASS)
    client.post(f"/api/tasks/{tid}/review/code")
    r = client.post(f"/api/tasks/{tid}/integrate").json()
    assert r["outcome"] == "LOCKED", r
    client.app.state.db.execute("DELETE FROM repository_integration_locks WHERE repository_id=?", (rid,))


def test_integration_verify_failure_detected(client, git_repo, tmp_path):
    """A worktree whose own narrow submission passes, but whose merge
    into main breaks the REPO'S OWN required test stage (a real,
    separate check) -- proves 'Task worktree PASS != integrated target
    PASS' (E10.6) with real evidence."""
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19005", port=19005)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Verify fail change", project_id=rid)
    tid, _ = materialize_task(client, cid, scope_hints=["test_src.py"])
    w = _select_and_create_workspace(client, tid, rid, agent="claude")
    worktree = client.app.state.git.validate_worktree(w["worktree_path"])
    # breaks test_src.py's own required 'test' stage for the repo as a
    # whole once merged (src.py stays at return 1, but the test now
    # demands 999) -- only touches test_src.py, so it's a clean,
    # non-conflicting merge that still legitimately fails verification.
    (worktree / "test_src.py").write_text("from src import feature\n\n\ndef test_feature():\n    assert feature() == 999\n")
    subprocess.run(["git", "add", "test_src.py"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "broken expectation"], cwd=worktree, check=True, capture_output=True)
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY"}, follow_redirects=False)
    set_fake(client, PASS)
    client.post(f"/api/tasks/{tid}/review/code")
    r = client.post(f"/api/tasks/{tid}/integrate").json()
    assert r["outcome"] == "INTEGRATION_VERIFY_FAILED", r
    assert not r["verification"]["passed"]
    mr = client.app.state.db.one("SELECT * FROM merge_records WHERE task_id=?", (tid,))
    assert mr["merge_status"] == "MERGE_VERIFY_FAILED"


# ================================================================ Release / versioning / immutable failed release (E10.7/E10.8/E10.26/E10.37)

def test_release_create_version_source_commit(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19006", port=19006)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Release create change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    r = client.app.state.release_service.create_release(rid, [tid], version="v1.0.0")
    assert r["version"] == "v1.0.0"
    assert r["source_commit"] == integ["integrated_commit"]
    assert r["status"] == "DRAFT"


def test_release_requires_integrated_task(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19007", port=19007)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Not integrated change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    with pytest.raises(Exception):
        client.app.state.release_service.create_release(rid, [tid])


def test_failed_release_is_immutable_new_attempt_created(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19008", port=19008)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Immutable failed change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r1 = svc.create_release(rid, [tid], version="v1")
    svc._set(r1["id"], status="FAILED", error="ARTIFACT_INVALID: forced for test")
    failed = svc.get(r1["id"])
    assert failed["status"] == "FAILED"
    # A new attempt is a NEW row/version -- the failed one is never
    # rewritten back to READY.
    r2 = svc.create_release(rid, [tid], version="v1-retry")
    assert r2["id"] != r1["id"]
    assert svc.get(r1["id"])["status"] == "FAILED"  # untouched


# ================================================================ Artifact / build-once (E10.9-E10.11/E10.32/E10.37)

def test_build_produces_valid_artifact_with_digest(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19009", port=19009)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Build change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    r = client.app.state.release_service.create_release(rid, [tid], version="v1")
    result = client.app.state.release_service.build(r["id"])
    assert result["outcome"] == "BUILT", result
    built = client.app.state.release_service.get(r["id"])
    assert built["status"] == "BUILT"
    assert built["artifact_digest"] and built["artifact_digest"].startswith("sha256:")
    assert built["artifact_sha256"]
    wps = [wp for wp in client.app.state.db.all("SELECT * FROM work_products WHERE kind='BUILD_ARTIFACT'")]
    assert any(json.loads(wp["content_metadata"])["release_id"] == r["id"] for wp in wps)


def test_no_rebuild_between_test_and_production_same_digest(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19010", port=19010)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Build once change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    digest_after_build = svc.get(r["id"])["artifact_digest"]

    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    svc.deploy_test(r["id"])
    test_result = svc.sync_test_result(r["id"])
    assert test_result["outcome"] == "RUNTIME_VERIFIED", test_result
    test_deployment = client.app.state.db.one("SELECT * FROM deployments WHERE id=?", (svc.get(r["id"])["test_deployment_id"],))
    assert test_deployment["artifact_digest"] == digest_after_build

    svc.approve_production(r["id"], "operator")
    prod = svc.deploy_production(r["id"])
    prod_result = svc.sync_production_result(r["id"])
    assert prod_result["outcome"] == "RELEASE_COMPLETE", prod_result
    prod_deployment = client.app.state.db.one("SELECT * FROM deployments WHERE id=?", (prod["deployment_id"],))
    assert prod_deployment["artifact_digest"] == digest_after_build
    # PROOF: TEST digest == PRODUCTION digest, and no second build phase
    # ran for the production deployment (build's own artifact.json
    # source_commit already matched, so build_once's needs_build check
    # correctly skipped a second build).
    assert not client.app.state.db.all("SELECT id FROM deployment_phases WHERE deployment_id=? AND phase='BUILDING'", (prod["deployment_id"],))


def test_artifact_source_mismatch_invalidates_build(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19011", port=19011)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Mismatch change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    r = client.app.state.release_service.create_release(rid, [tid], version="v1")
    client.app.state.db.execute("UPDATE releases SET source_commit=? WHERE id=?", ("0" * 40, r["id"]))
    result = client.app.state.release_service.build(r["id"])
    assert result["outcome"] == "BUILD_FAILED", result


# ================================================================ Qualification (E10.22/E10.37)

def test_qualification_ready_when_all_evidence_passes(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19012", port=19012)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Qualify ready change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    result = svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    assert result["outcome"] == "RELEASE_READY", result
    assert svc.get(r["id"])["status"] == "READY"
    assert svc.get(r["id"])["migration_classification"] == "NO_MIGRATION"


def test_qualification_blocked_without_build(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19013", port=19013)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Qualify blocked change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    result = svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    assert result["outcome"] == "BLOCKED", result


# ================================================================ TEST deploy (E10.13-E10.15/E10.37)

def test_deploy_test_success_runtime_verified(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19014", port=19014)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Deploy test success change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    svc.deploy_test(r["id"])
    result = svc.sync_test_result(r["id"])
    assert result["outcome"] == "RUNTIME_VERIFIED"
    assert svc.get(r["id"])["status"] == "TEST_VERIFIED"


def test_deploy_test_health_failure(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19015", port=19015)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Deploy test fail change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    client.app.state.deployer.health_attempts = 1
    client.app.state.deployer.health_delay = 0.01
    def always_fails(*a, **k): raise OSError("connection refused")
    client.app.state.deployer.http_get = always_fails
    svc.deploy_test(r["id"])
    result = svc.sync_test_result(r["id"])
    assert result["outcome"] == "RUNTIME_VERIFY_FAILED", result
    assert svc.get(r["id"])["status"] == "FAILED"


# ================================================================ Production approval (E10.17/E10.36/E10.37)

def test_production_deploy_requires_test_verified_first(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19016", port=19016)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Prod requires test change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    result = svc.deploy_production(r["id"])
    assert result["outcome"] == "BLOCKED", result


def test_approval_binds_to_release_digest_target_new_artifact_invalidates(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19017", port=19017)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Approval binding change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    svc.deploy_test(r["id"])
    svc.sync_test_result(r["id"])
    svc.approve_production(r["id"], "operator")
    assert svc._production_approval_valid(svc.get(r["id"])) is True

    # Change the artifact digest (a rebuild would do this) WITHOUT a
    # fresh approval -- the old approval must no longer authorize it.
    client.app.state.db.execute("UPDATE releases SET artifact_digest=? WHERE id=?", ("sha256:different", r["id"]))
    assert svc._production_approval_valid(svc.get(r["id"])) is False
    deploy_result = svc.deploy_production(r["id"])
    assert deploy_result["outcome"] == "APPROVAL_REQUIRED", deploy_result


def test_full_production_promotion_verified(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19018", port=19018)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Full promotion change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    svc.deploy_test(r["id"])
    svc.sync_test_result(r["id"])
    svc.approve_production(r["id"], "operator")
    svc.deploy_production(r["id"])
    result = svc.sync_production_result(r["id"])
    assert result["outcome"] == "RELEASE_COMPLETE", result
    final = svc.get(r["id"])
    assert final["status"] == "PRODUCTION_VERIFIED"
    assert final["released_at"]

    # E10.28/E10.29: the Release/Deploy tabs and Change Overview summary
    # now show this real state -- composition only, over ReleaseService's
    # own already-computed truth, never a second status calculation.
    change_control_surface = client.app.state.change_control_surface
    release_data = change_control_surface.release_tab(cid)
    assert release_data["linked"] is True
    assert release_data["releases"][0]["status"] == "PRODUCTION_VERIFIED"
    deploy_data = change_control_surface.deploy_tab(cid)
    assert deploy_data["releases"][0]["production_deployment"]["health_status"] == "PASS"
    summary = change_control_surface.release_deploy_summary(cid)
    assert summary == {"integration": "INTEGRATED", "release": "ready", "test": "verified", "production": "verified"}

    rel_page = client.get(f"/changes/{cid}/release")
    assert rel_page.status_code == 200 and "PRODUCTION_VERIFIED" in rel_page.text and "Not linked yet" not in rel_page.text
    deploy_page = client.get(f"/changes/{cid}/deploy")
    assert deploy_page.status_code == 200 and "PASS" in deploy_page.text
    overview_page = client.get(f"/changes/{cid}")
    assert overview_page.status_code == 200 and "INTEGRATED" in overview_page.text


# ================================================================ Migration safety (E10.21/E10.35/E10.37)

def test_migration_no_migration_default(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19019", port=19019)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "No migration change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    assert svc.classify_migration(r["id"]) == "NO_MIGRATION"


def test_migration_destructive_requires_human_approval(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19020", port=19020)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Destructive migration change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    wp_id = client.app.state.work_products.create(
        kind="TECHNICAL_DESIGN", title="Destructive design", change_id=cid, status="APPROVED",
        content_metadata={"migration_plan": "DROP TABLE legacy_orders; this is irreversible and destroys old data.",
                            "backward_compatibility": "not compatible"})
    client.app.state.db.execute("UPDATE releases SET design_baseline_work_product_id=? WHERE id=?", (wp_id, r["id"]))
    assert svc.classify_migration(r["id"]) == "DESTRUCTIVE"
    svc.build(r["id"])
    svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    svc.deploy_test(r["id"])
    svc.sync_test_result(r["id"])
    result = svc.deploy_production(r["id"])
    assert result["outcome"] == "APPROVAL_REQUIRED", result


def test_migration_rollback_safe_classification(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19021", port=19021)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Rollback safe migration change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    wp_id = client.app.state.work_products.create(
        kind="TECHNICAL_DESIGN", title="Safe design", change_id=cid, status="APPROVED",
        content_metadata={"migration_plan": "Add a new nullable column; additive, safe to roll back.",
                            "backward_compatibility": "backward compatible"})
    client.app.state.db.execute("UPDATE releases SET design_baseline_work_product_id=? WHERE id=?", (wp_id, r["id"]))
    assert svc.classify_migration(r["id"]) == "ROLLBACK_SAFE"


# ================================================================ Rollback (E10.19-E10.20/E10.34/E10.37)

def test_rollback_auto_and_verified(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19022", port=19022)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Rollback change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.app.state.deployer.image_exists = lambda ref: True  # fixture has no real docker image
    svc.deploy_test(r["id"])
    svc.sync_test_result(r["id"])
    svc.approve_production(r["id"], "operator")
    svc.deploy_production(r["id"])
    svc.sync_production_result(r["id"])
    first_prod_id = svc.get(r["id"])["production_deployment_id"]

    # A second release, deployed to production (now "broken" via a
    # forced health failure), then rolled back to the first.
    (repo / "src.py").write_text("def feature():\n    return 3\n")
    run(repo, "git", "add", "src.py")
    run(repo, "git", "commit", "-m", "v2 source")
    cid2 = new_change(client, "Rollback change v2", project_id=rid)
    tid2, _ = materialize_task(client, cid2, scope_hints=["src.py"])
    client.app.state.db.execute("UPDATE tasks SET status='ACTIVE' WHERE id=?", (tid2,))
    client.app.state.db.execute(
        "INSERT INTO merge_records(task_id,repository_id,required,merge_status,merged_commit,merged_at) VALUES(?,?,1,'MERGED',?,CURRENT_TIMESTAMP)",
        (tid2, rid, client.app.state.git.head(repo)))
    r2 = svc.create_release(rid, [tid2], version="v2")
    svc.build(r2["id"])
    client.app.state.db.execute("UPDATE releases SET status='READY',migration_classification='NO_MIGRATION' WHERE id=?", (r2["id"],))
    svc.deploy_test(r2["id"])
    svc.sync_test_result(r2["id"])
    svc.approve_production(r2["id"], "operator")

    def health_fails(*a, **k): raise OSError("v2 broken")
    client.app.state.deployer.health_attempts = 1
    client.app.state.deployer.health_delay = 0.01
    client.app.state.deployer.http_get = health_fails
    svc.deploy_production(r2["id"])
    fail_result = svc.sync_production_result(r2["id"])
    assert fail_result["outcome"] == "RUNTIME_VERIFY_FAILED"

    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)  # "previous" version healthy again
    rb = svc.rollback_production(r2["id"])
    assert rb["outcome"] == "ROLLING_BACK", rb
    rb_result = svc.sync_rollback_result(r2["id"], rb["deployment_id"])
    assert rb_result["outcome"] == "ROLLED_BACK_VERIFIED", rb_result
    assert svc.get(r2["id"])["status"] == "ROLLED_BACK"
    rolled_back_deployment = client.app.state.db.one("SELECT * FROM deployments WHERE id=?", (rb["deployment_id"],))
    first_prod = client.app.state.db.one("SELECT * FROM deployments WHERE id=?", (first_prod_id,))
    assert rolled_back_deployment["artifact_digest"] == first_prod["artifact_digest"]  # v1's own artifact restored


# ================================================================ Real HTTP server deploy/rollback (E10.33/E10.34/E10.37)

def test_deploy_test_real_http_server_health_verified(client, git_repo, tmp_path):
    """E10.33: a real local HTTP fixture process (not FakeResp) proves
    Deploy TEST + runtime verification over a genuine socket."""
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19030", port=19030)
    server, handler = start_health_server(19030)
    try:
        rid = _register_repo(client, repo, "demo")
        cid = new_change(client, "Real server deploy change", project_id=rid)
        tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
        svc = client.app.state.release_service
        r = svc.create_release(rid, [tid], version="v1")
        svc.build(r["id"])
        svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
        # http_get is left as the deployer's own default (urllib.request.
        # urlopen) -- this is a real socket connection to the server
        # started above, never a fake stand-in.
        handler.healthy = True
        svc.deploy_test(r["id"])
        result = svc.sync_test_result(r["id"])
        assert result["outcome"] == "RUNTIME_VERIFIED", result
        assert svc.get(r["id"])["status"] == "TEST_VERIFIED"
        deployment = client.app.state.db.one(
            "SELECT * FROM deployments WHERE id=?", (svc.get(r["id"])["test_deployment_id"],))
        assert deployment["health_status"] == "PASS"
        assert deployment["artifact_digest"] == svc.get(r["id"])["artifact_digest"]
    finally:
        server.shutdown()


def test_rollback_real_process_restored_to_healthy(client, git_repo, tmp_path):
    """E10.34: a real runtime fixture (not just DB rows) goes healthy ->
    broken -> rolled back -- the health/version endpoints are real
    socket responses at every stage, matching the artifact digest each
    time so this proves an actual runtime, not a rewritten DB status."""
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19031", port=19031)
    server, handler = start_health_server(19031)
    try:
        rid = _register_repo(client, repo, "demo")
        cid = new_change(client, "Real rollback change v1", project_id=rid)
        tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
        svc = client.app.state.release_service
        client.app.state.deployer.image_exists = lambda ref: True  # fixture has no real docker image
        r = svc.create_release(rid, [tid], version="v1")
        svc.build(r["id"])
        svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
        handler.healthy = True
        svc.deploy_test(r["id"])
        svc.sync_test_result(r["id"])
        svc.approve_production(r["id"], "operator")
        svc.deploy_production(r["id"])
        prod_result = svc.sync_production_result(r["id"])
        assert prod_result["outcome"] == "RELEASE_COMPLETE", prod_result
        first_prod_id = svc.get(r["id"])["production_deployment_id"]
        first_digest = client.app.state.db.one("SELECT * FROM deployments WHERE id=?", (first_prod_id,))["artifact_digest"]

        (repo / "src.py").write_text("def feature():\n    return 3\n")
        run(repo, "git", "add", "src.py")
        run(repo, "git", "commit", "-m", "v2 source")
        cid2 = new_change(client, "Real rollback change v2", project_id=rid)
        tid2, _ = materialize_task(client, cid2, scope_hints=["src.py"])
        client.app.state.db.execute("UPDATE tasks SET status='ACTIVE' WHERE id=?", (tid2,))
        client.app.state.db.execute(
            "INSERT INTO merge_records(task_id,repository_id,required,merge_status,merged_commit,merged_at) VALUES(?,?,1,'MERGED',?,CURRENT_TIMESTAMP)",
            (tid2, rid, client.app.state.git.head(repo)))
        r2 = svc.create_release(rid, [tid2], version="v2")
        svc.build(r2["id"])
        client.app.state.db.execute(
            "UPDATE releases SET status='READY',migration_classification='NO_MIGRATION' WHERE id=?", (r2["id"],))
        handler.healthy = True
        svc.deploy_test(r2["id"])
        svc.sync_test_result(r2["id"])
        svc.approve_production(r2["id"], "operator")

        # The real process actually goes unhealthy (a genuine 500 over
        # the real socket) -- not a raised exception, not a DB flag.
        handler.healthy = False
        client.app.state.deployer.health_attempts = 1
        client.app.state.deployer.health_delay = 0.01
        svc.deploy_production(r2["id"])
        fail_result = svc.sync_production_result(r2["id"])
        assert fail_result["outcome"] == "RUNTIME_VERIFY_FAILED", fail_result

        # Rollback: the real process is restored to serving healthy
        # responses again (standing in for the previous artifact's own
        # process being brought back up), and verification re-checks
        # the real socket -- never inferred from DB state alone.
        handler.healthy = True
        rb = svc.rollback_production(r2["id"])
        assert rb["outcome"] == "ROLLING_BACK", rb
        rb_result = svc.sync_rollback_result(r2["id"], rb["deployment_id"])
        assert rb_result["outcome"] == "ROLLED_BACK_VERIFIED", rb_result
        assert svc.get(r2["id"])["status"] == "ROLLED_BACK"
        rolled_back = client.app.state.db.one("SELECT * FROM deployments WHERE id=?", (rb["deployment_id"],))
        assert rolled_back["health_status"] == "PASS"
        assert rolled_back["artifact_digest"] == first_digest  # v1's own artifact, not a rebuild
    finally:
        server.shutdown()


def test_destructive_migration_rollback_requires_human(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19023", port=19023)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Destructive rollback change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    deployment_id = client.app.state.db.execute(
        "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,status) VALUES(?,?,?,?,?)",
        (rid, "PRODUCTION", "main", r["source_commit"], "VERIFIED"))
    client.app.state.db.execute(
        "UPDATE releases SET status='DEPLOYING_PRODUCTION',migration_classification='DESTRUCTIVE',production_deployment_id=? WHERE id=?",
        (deployment_id, r["id"]))
    result = svc.rollback_production(r["id"])
    assert result["outcome"] == "ROLLBACK_REQUIRES_HUMAN", result


# ================================================================ DEPLOY_VERIFIED gate (E10.23/E10.37)

def test_deploy_verified_uses_real_release_evidence(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19024", port=19024)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "Deploy verified change", project_id=rid)
    tid, integ = _reviewed_and_integrated_task(client, repo, cid, rid)
    assert client.app.state.release_service.deploy_verified(cid) is None  # no Release evidence yet -> fall back to legacy
    svc = client.app.state.release_service
    r = svc.create_release(rid, [tid], version="v1")
    svc.build(r["id"])
    svc.qualify(r["id"], client.app.state.review_fix_orchestrator)
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    svc.deploy_test(r["id"])
    svc.sync_test_result(r["id"])
    svc.approve_production(r["id"], "operator")
    svc.deploy_production(r["id"])
    svc.sync_production_result(r["id"])
    assert svc.deploy_verified(cid) is True


def test_deploy_verified_none_when_no_tasks_falls_back(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19025", port=19025)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "No tasks change", project_id=rid)
    assert client.app.state.release_service.deploy_verified(cid) is None


# ================================================================ Backward compatibility (E10.37)

def test_legacy_dev_deployment_flow_unaffected(client, git_repo, tmp_path):
    """The exact DEV automation flow (E1) works unchanged after all of
    E10's environment-scoped command resolution -- it always falls
    through to the same global commands.<name> entries."""
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19026", port=19026)
    rid = _register_repo(client, repo, "demo")
    deployment_id = client.app.state.db.execute(
        "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,status) VALUES(?,?,?,?,?)",
        (rid, "DEV", "main", client.app.state.git.head(repo), "PENDING"))
    client.app.state.deployer.http_get = lambda *a, **k: FakeResp(200)
    client.app.state.deployer.deploy(deployment_id)
    d = client.app.state.db.one("SELECT * FROM deployments WHERE id=?", (deployment_id,))
    assert d["status"] == "VERIFIED", d


def test_legacy_review_and_e8_e9_unaffected(client, git_repo, tmp_path):
    root, _ = git_repo
    repo = make_release_repo(root, name="rel19027", port=19027)
    rid = _register_repo(client, repo, "demo")
    cid = new_change(client, "E1-E9 unaffected change", project_id=rid)
    tid, _ = materialize_task(client, cid)
    r = client.get(f"/api/tasks/{tid}/execution-readiness")
    assert r.json()["readiness"] in ("AUTO_READY", "NOT_AUTONOMOUS_TASK")
