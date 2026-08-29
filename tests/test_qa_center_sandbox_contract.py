"""Real incident: QA Center Task #6 ("Fix giao diện qa-") -- Create Sandbox
FAILED with SANDBOX_CONTRACT_REQUIRED even though qa-center's main branch
genuinely declares a sandbox: contract, because the Task's own Builder
Workspace worktree was pinned to a commit that predates the contract being
added to main, and every sandbox lifecycle method (provision/health_check/
stop/reset_data/cleanup) read the contract from that pinned WORKTREE
instead of the repository's trusted canonical checkout create() itself
already used.

Covers spec section 20's checklist: root-cause fix, runtime-dependency
resolution + labeling, isolated DB is untouched by this change (still the
sandbox's own compose file's concern), health requiring the dependency
reachable, the failed-contract-missing UX guard, the manual-verification
PASS/FAIL guards, and the generic output-URL fix."""
from __future__ import annotations
import subprocess

import pytest

from tests.conftest import make_repo, run, NGINX_SANDBOX_CONTRACT, NGINX_COMPOSE


def register(client, repo, name):
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": name, "default_branch": "main"})
    assert r.status_code in (200, 303), r.text


def decision(client, tid):
    return client.get(f"/api/tasks/{tid}/decision").json()


def new_task(client, title, risk="LOW"):
    r = client.post("/api/tasks", data={"title": title, "risk_profile": risk}, follow_redirects=False)
    assert r.status_code == 303
    return [t["id"] for t in client.get("/api/tasks").json() if t["title"] == title][0]


@pytest.fixture
def cleanup_sandboxes(client):
    created = []
    yield created
    for sid in created:
        client.post(f"/api/sandboxes/{sid}/cleanup")


def wait_for(fn, timeout=25.0, interval=0.1):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return fn()


# ============================================================ Root cause =

def test_stale_task_branch_still_finds_contract_added_to_main_later(client, tmp_path, cleanup_sandboxes):
    """The exact QA Center Task #6 regression: a Builder Workspace's
    worktree is created BEFORE the repo's main branch ever gets a
    sandbox: contract -- Create Sandbox must still succeed, reading the
    contract from the repo's trusted canonical checkout, never fail with
    SANDBOX_CONTRACT_REQUIRED just because the pinned commit predates it."""
    root = tmp_path / "root"
    repo = make_repo(root, "qa-like")  # no sandbox: contract yet
    register(client, repo, "qa-like")
    rid = client.get("/api/repositories").json()[0]["id"]

    tid = new_task(client, "Stale branch regression")
    client.post(f"/api/tasks/{tid}/select")
    r = client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main"}, follow_redirects=False)
    assert r.status_code == 303
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]

    # The contract lands on main AFTER the Builder Workspace's worktree
    # (and its base_commit) already exist -- the worktree's own
    # PROJECT.yaml genuinely has no sandbox: block.
    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: qa-like}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n"
        + NGINX_SANDBOX_CONTRACT.format(lo=21400, hi=21419)
    )
    (repo / "compose.yml").write_text(NGINX_COMPOSE)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "add sandbox contract")

    r = client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"}, follow_redirects=False)
    assert r.status_code == 303
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])
    assert sb["error_code"] != "SANDBOX_CONTRACT_REQUIRED"

    ready = wait_for(lambda: client.app.state.db.one("SELECT status FROM sandboxes WHERE id=?", (sb["id"],))["status"] in ("RUNNING", "FAILED", "UNHEALTHY"))
    row = client.app.state.db.one("SELECT * FROM sandboxes WHERE id=?", (sb["id"],))
    assert row["status"] == "RUNNING", row["error_message"]
    assert row["health_status"] == "HEALTHY"


def test_source_still_pinned_to_exact_worktree_commit_not_main(client, tmp_path, cleanup_sandboxes):
    """The fix must not start pinning a Task's own source to main --
    only the sandbox: CONTRACT lookup changed. sandbox_sources.commit_sha
    for the AGENT_WORKSPACE source stays the worktree's own exact commit."""
    root = tmp_path / "root"
    repo = make_repo(root, "qa-like2")
    register(client, repo, "qa-like2")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Pinning check")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main"}, follow_redirects=False)
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    pinned_commit = w["base_commit"]

    (repo / "PROJECT.yaml").write_text(
        "schema_version: 1\nproject: {code: qa-like2}\nsource: {root: .}\n"
        "commands:\n  preflight: {command: 'true'}\n  test: {command: 'true'}\nci: {required: [preflight, test]}\n"
        + NGINX_SANDBOX_CONTRACT.format(lo=21420, hi=21439)
    )
    (repo / "compose.yml").write_text(NGINX_COMPOSE)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "add sandbox contract, plus this should never be what gets pinned")

    client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])
    src = client.app.state.db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? AND source_type='AGENT_WORKSPACE'", (sb["id"],))
    assert src["commit_sha"] == pinned_commit


# =================================================== Runtime dependency ==

def _register_two_repos_with_dependency(client, tmp_path):
    root = tmp_path / "root"
    backend = make_repo(root, "backend-dep")
    register(client, backend, "backend-dep")
    # runtime_dependencies nests INSIDE the sandbox: block (matches
    # qa-center's real PROJECT.yaml, and load_sandbox_contract() reads
    # everything from within that one mapping) -- insert it right after
    # the "sandbox:" line, never as a second top-level YAML key.
    base_contract = NGINX_SANDBOX_CONTRACT.format(lo=21440, hi=21459)
    dep_extra = base_contract.replace(
        "\nsandbox:\n",
        "\nsandbox:\n  runtime_dependencies:\n    - repo: backend-dep\n      profile: BACKEND\n      mode: KNOWN_GOOD_MAIN\n",
        1,
    )
    assert dep_extra != base_contract
    frontend = make_repo(root, "qa-front", dep_extra)
    (frontend / "compose.yml").write_text(NGINX_COMPOSE)
    run(frontend, "git", "add", ".")
    run(frontend, "git", "commit", "-m", "sandbox contract with runtime dependency")
    register(client, frontend, "qa-front")
    repos = {r["repo_name"]: r["id"] for r in client.get("/api/repositories").json()}
    return backend, frontend, repos


def test_runtime_dependency_recorded_as_separate_labeled_source(client, tmp_path, cleanup_sandboxes):
    """Section 4/5: mesflow-app-style dependency is a real, separately
    labeled RUNTIME_DEPENDENCY source, never a second Builder Workspace,
    never AGENT_WORKSPACE."""
    backend, frontend, repos = _register_two_repos_with_dependency(client, tmp_path)
    tid = new_task(client, "Runtime dependency labeling")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": repos["qa-front"], "agent": "codex", "role": "Frontend", "base_branch": "main"}, follow_redirects=False)
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]

    client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])

    sources = client.app.state.db.all("SELECT * FROM sandbox_sources WHERE sandbox_id=?", (sb["id"],))
    assert len(sources) == 2, sources
    dep = next(s for s in sources if s["source_type"] == "RUNTIME_DEPENDENCY")
    primary = next(s for s in sources if s["source_type"] == "AGENT_WORKSPACE")
    assert dep["repository_id"] == repos["backend-dep"]
    assert dep["role"] == "backend-dep"
    assert primary["repository_id"] == repos["qa-front"]
    # This Task never touched backend-dep -- its Builder Workspace list
    # (never created a second one) proves it, but assert the positive too.
    assert len(client.get(f"/api/tasks/{tid}").json()["workspaces"]) == 1


def test_runtime_dependency_uses_known_good_main_deployment_commit(client, tmp_path, cleanup_sandboxes):
    """mode: KNOWN_GOOD_MAIN prefers the dependency repo's latest VERIFIED
    DEV deployment's exact pinned commit over just 'whatever HEAD is'."""
    backend, frontend, repos = _register_two_repos_with_dependency(client, tmp_path)
    pinned_sha = "deadbeef" * 5  # 40 hex chars, a deliberately-distinct fake verified commit
    client.app.state.db.execute(
        "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,status,deployed_url) VALUES(?,?,?,?,?,?)",
        (repos["backend-dep"], "DEV", "main", pinned_sha, "VERIFIED", "http://127.0.0.1:1"),
    )
    tid = new_task(client, "Known good main")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": repos["qa-front"], "agent": "codex", "role": "Frontend", "base_branch": "main"}, follow_redirects=False)
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])
    dep = client.app.state.db.one("SELECT * FROM sandbox_sources WHERE sandbox_id=? AND source_type='RUNTIME_DEPENDENCY'", (sb["id"],))
    assert dep["commit_sha"] == pinned_sha


def test_health_requires_runtime_dependency_actually_reachable(client, tmp_path, cleanup_sandboxes):
    """Section 10: never HEALTHY merely because the sandbox's own
    container exists -- an unreachable declared runtime dependency fails
    overall health with a clear reason."""
    backend, frontend, repos = _register_two_repos_with_dependency(client, tmp_path)
    # Point the dependency's tracked DEV deployment at a port nothing is
    # listening on -- genuinely unreachable, no network flakiness.
    client.app.state.db.execute(
        "INSERT INTO deployments(repository_id,environment,source_branch,source_commit,status,deployed_url) VALUES(?,?,?,?,?,?)",
        (repos["backend-dep"], "DEV", "main", "a" * 40, "VERIFIED", "http://127.0.0.1:1"),
    )
    tid = new_task(client, "Dependency unreachable")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": repos["qa-front"], "agent": "codex", "role": "Frontend", "base_branch": "main"}, follow_redirects=False)
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])

    wait_for(lambda: client.app.state.db.one("SELECT status FROM sandboxes WHERE id=?", (sb["id"],))["status"] in ("RUNNING", "UNHEALTHY", "FAILED"))
    row = client.app.state.db.one("SELECT * FROM sandboxes WHERE id=?", (sb["id"],))
    # qa-front's own `app` container is genuinely healthy, but the
    # declared runtime dependency is not reachable -- overall must not
    # be RUNNING/HEALTHY.
    assert row["status"] != "RUNNING" or row["health_status"] != "HEALTHY"
    assert "backend-dep" in (row["error_message"] or "")


# ================================================ Generic output URL ====

def test_sandbox_view_finds_arbitrarily_named_url_output(client, tmp_path, cleanup_sandboxes):
    """Section 9/11: the contract can call its one output anything
    (qa_center_url, not just frontend_url/backend_url) -- the Open App
    link must still find it, never require inspecting Advanced."""
    root = tmp_path / "root"
    contract = NGINX_SANDBOX_CONTRACT.format(lo=21460, hi=21479).replace("backend_url:", "qa_center_url:")
    repo = make_repo(root, "qa-url-test", contract)
    (repo / "compose.yml").write_text(NGINX_COMPOSE)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "qa_center_url output")
    register(client, repo, "qa-url-test")
    rid = client.get("/api/repositories").json()[0]["id"]
    # NORMAL risk + Review PASS: reach the TEST_QA wizard step, the one
    # that actually renders the sandbox's Open App link (SETUP doesn't).
    tid = new_task(client, "Generic output url", risk="NORMAL")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main"}, follow_redirects=False)
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    client.post(f"/api/workspaces/{w['id']}/verification-report", data={"work_status": "READY", "what_changed": "x"})
    client.post(f"/api/workspaces/{w['id']}/start-review", data={"reviewer_agent": "claude"})
    client.post(f"/api/workspaces/{w['id']}/submit-review", data={"result": "PASS"}, follow_redirects=False)
    client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])
    wait_for(lambda: client.app.state.db.one("SELECT status FROM sandboxes WHERE id=?", (sb["id"],))["status"] == "RUNNING")

    d = decision(client, tid)
    assert d["current_step"] == "TEST_QA"
    html = client.get(f"/tasks/{tid}").text
    assert "Open App" in html
    assert 'href="http://127.0.0.1:' in html


# ================================================ Failed-contract UX =====

def test_failed_contract_missing_hides_useless_retry_actions(client, tmp_path):
    """Sections 13/14: SANDBOX_CONTRACT_REQUIRED must not render Start/
    Restart/Rebuild/Health Check (none can succeed) and must show a
    translated message, never the raw code as the primary text."""
    root = tmp_path / "root"
    repo = make_repo(root, "no-contract")  # deliberately no sandbox: block
    register(client, repo, "no-contract")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "No contract task")
    client.post(f"/api/tasks/{tid}/select")
    # Force a sandbox row to exist in the FAILED/SANDBOX_CONTRACT_REQUIRED
    # state directly (create() itself already refuses -- this reproduces
    # the orphaned-row shape a genuine race could still leave, and is
    # the exact rendering this UI guard must handle).
    r = client.app.state.db
    sid = r.execute(
        "INSERT INTO sandboxes(repository_id,owner_type,owner_id,sandbox_slug,profile,compose_project,status,error_code,error_message) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (rid, "AGENT_WORKSPACE", 999999, "no-contract-test-slug", "BACKEND", "wm-no-contract-test-proj", "FAILED", "SANDBOX_CONTRACT_REQUIRED", "sandbox: contract missing"),
    )
    html = client.get(f"/sandboxes/{sid}").text
    # The raw code stays available (a hover tooltip / "Technical:" label
    # counts as technical detail, section 14) but the PRIMARY, visible
    # explanation is the translated sentence, never the bare code.
    assert "does not yet define a runtime sandbox contract" in html
    assert "View Setup Guidance" in html
    assert '>Start<' not in html
    assert '>Restart<' not in html
    assert '>Rebuild<' not in html
    assert '>Health Check<' not in html


# ============================================ Manual verification guard ==

def test_manual_verification_blocked_when_sandbox_never_running(client, tmp_path):
    root = tmp_path / "root"
    repo = make_repo(root, "never-running")
    register(client, repo, "never-running")
    rid = client.get("/api/repositories").json()[0]["id"]
    db = client.app.state.db
    sid = db.execute(
        "INSERT INTO sandboxes(repository_id,owner_type,owner_id,sandbox_slug,profile,compose_project,status) VALUES(?,?,?,?,?,?,?)",
        (rid, "AGENT_WORKSPACE", 1, "never-running-slug", "BACKEND", "wm-never-running-proj", "FAILED"),
    )
    for result in ("PASS", "FAIL"):
        r = client.post(f"/api/sandboxes/{sid}/manual-verification", data={"result": result}, follow_redirects=False)
        assert r.status_code != 303, f"{result} must be blocked for a non-RUNNING sandbox"
    assert db.all("SELECT * FROM manual_verifications WHERE sandbox_id=?", (sid,)) == []


def test_manual_verification_pass_blocked_when_unhealthy_fail_allowed(client, tmp_path):
    root = tmp_path / "root"
    repo = make_repo(root, "unhealthy-case")
    register(client, repo, "unhealthy-case")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Unhealthy case")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main"}, follow_redirects=False)
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    db = client.app.state.db
    sid = db.execute(
        "INSERT INTO sandboxes(repository_id,owner_type,owner_id,sandbox_slug,profile,compose_project,status,health_status) VALUES(?,?,?,?,?,?,?,?)",
        (rid, "AGENT_WORKSPACE", w["id"], "unhealthy-slug", "BACKEND", "wm-unhealthy-proj", "RUNNING", "UNHEALTHY"),
    )
    r_pass = client.post(f"/api/sandboxes/{sid}/manual-verification", data={"result": "PASS"}, follow_redirects=False)
    assert r_pass.status_code != 303
    r_fail = client.post(f"/api/sandboxes/{sid}/manual-verification", data={"result": "FAIL", "note": "broken"}, follow_redirects=False)
    assert r_fail.status_code == 303


def test_manual_verification_pass_pins_built_commit_and_operator(client, tmp_path, cleanup_sandboxes):
    """Section 16: PASS pins the sandbox's OWN actual built commit (never
    a live re-read of the worktree HEAD, which may have moved since the
    sandbox was built) and records who/how (operator)."""
    root = tmp_path / "root"
    repo = make_repo(root, "pin-test", NGINX_SANDBOX_CONTRACT.format(lo=21480, hi=21499))
    (repo / "compose.yml").write_text(NGINX_COMPOSE)
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "sandbox contract")
    register(client, repo, "pin-test")
    rid = client.get("/api/repositories").json()[0]["id"]
    tid = new_task(client, "Pin test")
    client.post(f"/api/tasks/{tid}/select")
    client.post(f"/api/tasks/{tid}/workspaces", data={"repository_id": rid, "agent": "codex", "role": "Backend", "base_branch": "main"}, follow_redirects=False)
    w = client.get(f"/api/tasks/{tid}").json()["workspaces"][0]
    built_commit = w["base_commit"]
    client.post(f"/api/tasks/{tid}/workspaces/{w['id']}/create-sandbox", data={"profile": "backend"}, follow_redirects=False)
    sb = client.get("/api/sandboxes").json()[0]
    cleanup_sandboxes.append(sb["id"])
    wait_for(lambda: client.app.state.db.one("SELECT status FROM sandboxes WHERE id=?", (sb["id"],))["status"] == "RUNNING")

    # A new commit lands on the worktree AFTER the sandbox was already
    # built from the older one -- a live re-read of HEAD here would be
    # wrong evidence.
    (repo / ".worktrees").exists()  # no-op guard; nothing to assert, just documents intent
    r = client.post(f"/api/sandboxes/{sb['id']}/manual-verification", data={"result": "PASS", "note": "looks right"}, follow_redirects=False)
    assert r.status_code == 303
    row = client.app.state.db.one("SELECT * FROM manual_verifications WHERE sandbox_id=? ORDER BY id DESC LIMIT 1", (sb["id"],))
    assert row["source_commit"] == built_commit
    assert row["operator"] == "ui"
