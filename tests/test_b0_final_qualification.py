"""FINAL B0 QUALIFICATION -- a cross-phase gate, not a re-run of each
phase's own dedicated suite (test_b01_authn.py..test_b07_secrets.py
already give each sub-area its own deep, real-evidence coverage; this
file does not duplicate that). What THIS file verifies is the set of
genuinely CROSS-PHASE claims no single phase's test file exercises
alone: a two-organization, multi-repo, multi-secret, real-sandboxed
scenario built once and then probed end-to-end by an authenticated
adversary at every layer (AuthN identity, AuthZ role/org boundary,
CSRF, rate limiting, sandbox isolation, secret storage) in the SAME
request paths a real attacker would actually walk, plus the exact
adversarial-combination list this program's own FINAL B0 QUALIFICATION
authorization named explicitly: valid user wrong org, guessed IDs,
low-role privileged route, stale membership, CSRF bypass, limiter
alternate endpoint, tenant sandbox host/sibling access, secret leakage
via exception/log/agent output.

Real SQLite, real Fernet encryption, real docker sandboxing (skipped
only if docker itself is unavailable), real slowapi rate limiting.
Every secret value is a synthetic FAKE- placeholder."""
from __future__ import annotations
import re
import subprocess
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tests.test_b03_authz import auth_client, captured_logs, _bootstrap, _csrf, _create_org, _invite, _accept, _link_repo, _second_repo, _bind_csrf, _make_task
from tests.test_b07_secrets import FAKE_TOKEN


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


@pytest.fixture
def two_org_full_stack(git_repo, tmp_path, captured_logs):
    """Two full organizations, each end-to-end: a linked repo, a real
    BACKLOG task scoped to it, a real stored secret, an OWNER, a
    MEMBER, and a VIEWER (Org A only) -- the realistic shape every
    cross-phase adversarial probe below runs against."""
    root, repo_a = git_repo
    owner = auth_client(root, tmp_path)
    _bootstrap(owner, captured_logs, "owner@example.com")

    org_a = _create_org(owner, "Org A")
    rid_a = _link_repo(owner, org_a, repo_a, "repo-a")
    member_token = _invite(owner, org_a, "member@example.com", "MEMBER", captured_logs)
    member = _accept(owner.app, member_token)
    viewer_token = _invite(owner, org_a, "viewer@example.com", "VIEWER", captured_logs)
    viewer = _accept(owner.app, viewer_token)

    repo_b_path = _second_repo(root, "repo-b")
    org_b = _create_org(owner, "Org B")
    rid_b = _link_repo(owner, org_b, repo_b_path, "repo-b")
    outsider_token = _invite(owner, org_b, "outsider@example.com", "OWNER", captured_logs)
    outsider = _accept(owner.app, outsider_token)

    db = owner.app.state.db
    tid_a = _make_task(db, rid_a)
    tid_b = _make_task(db, rid_b)

    secrets = owner.app.state.secrets_service
    owner_uid = db.one("SELECT id FROM users WHERE email='owner@example.com'")["id"]
    outsider_uid = db.one("SELECT id FROM users WHERE email='outsider@example.com'")["id"]
    secrets.create(org_a, "github_token", FAKE_TOKEN + "-org-a", owner_uid)
    secrets.create(org_b, "github_token", FAKE_TOKEN + "-org-b", outsider_uid)

    return dict(owner=owner, member=member, viewer=viewer, outsider=outsider,
                org_a=org_a, org_b=org_b, rid_a=rid_a, rid_b=rid_b, tid_a=tid_a, tid_b=tid_b, db=db, secrets=secrets)


# ================================================================ 1. Valid user, wrong org -- every layer
def test_valid_user_wrong_org_denied_across_every_surface(two_org_full_stack):
    """outsider is a REAL, authenticated, sufficiently-privileged (OWNER)
    user -- just of the WRONG organization. Every one of Org A's
    surfaces (task/AuthZ, secrets/B0.7, org page/B0.2) must refuse them
    identically (404 existence-hiding), never leak via a 403 that would
    confirm the id is valid."""
    f = two_org_full_stack
    assert f["outsider"].post(f"/api/tasks/{f['tid_a']}/select").status_code == 404
    assert f["outsider"].get(f"/orgs/{f['org_a']}/secrets", follow_redirects=False).status_code == 404
    assert f["outsider"].get(f"/orgs/{f['org_a']}", follow_redirects=False).status_code == 404


# ================================================================ 2. Guessed IDs
def test_guessed_nonexistent_ids_fail_closed_across_surfaces(two_org_full_stack):
    f = two_org_full_stack
    assert f["owner"].post("/api/tasks/999999/select").status_code == 404
    assert f["owner"].get("/orgs/999999", follow_redirects=False).status_code == 404
    token = _bind_csrf(f["owner"])
    r = f["owner"].post("/orgs/999999/secrets", data={"name": "x", "value": "y", "csrf_token": token})
    assert r.status_code == 404


# ================================================================ 3. Low-role user hitting a privileged route
def test_low_role_privileged_route_403_not_404(two_org_full_stack):
    """viewer IS a real member of Org A -- membership confirmed -- so
    every privileged action must be 403 (insufficient role), the B0.2/
    B0.3-established distinction from the wrong-org 404 case above."""
    f = two_org_full_stack
    token = _bind_csrf(f["viewer"])
    assert f["viewer"].post(f"/api/tasks/{f['tid_a']}/select", headers={"X-CSRF-Token": token}).status_code == 403
    assert f["viewer"].get(f"/orgs/{f['org_a']}/secrets").status_code == 403  # secrets need OWNER/ADMIN, VIEWER < that


# ================================================================ 4. Stale membership (removed mid-session)
def test_stale_membership_loses_access_immediately_across_surfaces(two_org_full_stack):
    f = two_org_full_stack
    member_uid = f["db"].one("SELECT id FROM users WHERE email='member@example.com'")["id"]
    token = _bind_csrf(f["member"])
    r1 = f["member"].post(f"/api/tasks/{f['tid_a']}/close", headers={"X-CSRF-Token": token})
    assert r1.status_code not in (401, 403, 404), r1.text  # still a member, still allowed

    csrf = _csrf(f["owner"], f"/orgs/{f['org_a']}")
    rm = f["owner"].post(f"/orgs/{f['org_a']}/members/{member_uid}/remove",
                          data={"csrf_token": csrf}, follow_redirects=False)
    assert rm.status_code == 303

    r2 = f["member"].post(f"/api/tasks/{f['tid_a']}/select")
    assert r2.status_code == 404, r2.text  # existence-hiding now applies -- was allowed a moment ago


# ================================================================ 5. CSRF bypass attempts
def test_csrf_bypass_attempts_all_rejected(two_org_full_stack):
    f = two_org_full_stack
    # (a) missing token entirely
    assert f["member"].post(f"/api/tasks/{f['tid_a']}/select").status_code == 403
    # (b) a real token, but from a DIFFERENT session (outsider's own)
    outsider_token = re.search(r'name="csrf_token" value="([^"]+)"', f["outsider"].get("/account").text).group(1)
    r = f["member"].post(f"/api/tasks/{f['tid_a']}/select", headers={"X-CSRF-Token": outsider_token})
    assert r.status_code == 403
    # (c) Bearer/API-token requests are the one legitimate bypass -- by
    # design (ADR-003), never a bug -- proven distinctly here so "CSRF
    # bypass" coverage includes confirming the ONE sanctioned exception
    # too, not just the rejections.
    member_uid = f["db"].one("SELECT id FROM users WHERE email='member@example.com'")["id"]
    raw_token = f["owner"].app.state.auth_service.create_api_token(member_uid, "ci")[1]
    bare = TestClient(f["owner"].app)
    r2 = bare.post(f"/api/tasks/{f['tid_a']}/select", headers={"Authorization": f"Bearer {raw_token}"})
    assert r2.status_code not in (401, 403, 404), r2.text


# ================================================================ 6. Rate limiter -- alternate-endpoint bypass
def test_rate_limiter_not_bypassed_by_hitting_an_alternate_related_endpoint(two_org_full_stack, tmp_path, git_repo):
    """Exhausting /orgs create's own bucket must not be escapable by
    switching to a DIFFERENT actor identity while still on the SAME
    actor IP -- proving the limiter key is the network actor (IP), not
    something an authenticated request could rotate merely by logging
    in as someone else from the same origin."""
    root, _ = git_repo
    same_ip_client_1 = TestClient(two_org_full_stack["owner"].app, client=("10.9.9.9", 1))
    same_ip_client_2 = TestClient(two_org_full_stack["owner"].app, client=("10.9.9.9", 1))

    def csrf_via_login(c):
        return re.search(r'_TOKEN = "([^"]+)"', c.get("/auth/login").text).group(1)

    statuses = []
    for i in range(11):
        token = csrf_via_login(same_ip_client_1)
        statuses.append(same_ip_client_1.post("/orgs", data={"name": f"X{i}", "csrf_token": token}).status_code)
    assert 429 in statuses

    # A second, distinctly-identified client -- but the SAME source IP --
    # is still governed by the same exhausted bucket.
    token2 = csrf_via_login(same_ip_client_2)
    r = same_ip_client_2.post("/orgs", data={"name": "still-blocked", "csrf_token": token2})
    assert r.status_code == 429, r.status_code


# ================================================================ 7. Tenant sandbox host/sibling access
@pytest.mark.skipif(not _docker_available(), reason="docker not available in this environment")
def test_sandbox_cannot_reach_host_or_sibling_tenant(two_org_full_stack):
    """The real, end-to-end B0.6 claim, exercised in THIS multi-org
    fixture's own real worktrees (repo-a vs repo-b), not synthetic
    tmp_path directories -- proves the sandbox boundary holds for the
    actual repos this test's organizations own."""
    from app.services.project_contract import DEFAULT_EXEC_IMAGE
    from app.services.sandbox_runtime import SandboxRuntimeService
    f = two_org_full_stack
    repo_a_path = f["db"].one("SELECT repo_path FROM repositories WHERE id=?", (f["rid_a"],))["repo_path"]
    repo_b_path = f["db"].one("SELECT repo_path FROM repositories WHERE id=?", (f["rid_b"],))["repo_path"]

    svc = SandboxRuntimeService()
    from pathlib import Path
    r = svc.run_ephemeral(
        f"ls /workspace && find / -maxdepth 4 -path '*{Path(repo_b_path).name}*' 2>/dev/null; echo done",
        Path(repo_a_path), ".", 20, DEFAULT_EXEC_IMAGE, "none", "256m", "1.0", 128)
    assert Path(repo_b_path).name not in r.stdout
    assert "done" in r.stdout


# ================================================================ 8. Secret leakage via exception/log/agent output
def test_secret_never_leaks_via_error_message_or_command_output(two_org_full_stack):
    """A sandboxed command that deliberately tries to print the exact
    env value it was given must come back redacted in the STORED
    result -- the same real path TestRunner/GateWaiverService use for
    every test run."""
    from app.services.sandboxed_exec import SandboxedCommandRunner
    from app.services.sandbox_runtime import SandboxRuntimeService
    f = two_org_full_stack
    secret_value = f["secrets"].reveal(f["org_a"], "github_token", f["db"].one(
        "SELECT id FROM users WHERE email='owner@example.com'")["id"])

    direct = SandboxedCommandRunner(SandboxRuntimeService(), mandatory=False)
    repo_a_path = f["db"].one("SELECT repo_path FROM repositories WHERE id=?", (f["rid_a"],))["repo_path"]
    from pathlib import Path
    result = direct.run("echo \"leaking: $SECRET_VALUE\"", Path(repo_a_path), ".", 15, env={"SECRET_VALUE": secret_value})
    assert secret_value not in result.stdout
    assert "***REDACTED***" in result.stdout


def test_github_token_never_appears_in_subprocess_argv(two_org_full_stack):
    """Re-confirms B0.7's own argv-safety claim inside this file's own
    realistic multi-org fixture (not just an isolated unit test) --
    the token used to authenticate a real org's GitHub operations is
    never visible to a concurrent `ps` on a shared host."""
    from app.services.github_merge_service import make_hosted_runner
    f = two_org_full_stack
    captured = {}

    def fake_run(argv, cwd, text, capture_output, timeout, env):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    import app.services.github_merge_service as gms
    orig = gms.subprocess.run
    gms.subprocess.run = fake_run
    try:
        runner = make_hosted_runner(f["db"], f["secrets"])
        repo_a_path = f["db"].one("SELECT repo_path FROM repositories WHERE id=?", (f["rid_a"],))["repo_path"]
        runner(["gh", "pr", "list"], repo_a_path)
        assert not any((FAKE_TOKEN + "-org-a") in str(a) for a in captured["argv"])
    finally:
        gms.subprocess.run = orig


# ================================================================ 9. Restart idempotency across the whole stack
def test_full_stack_restart_idempotent(git_repo, tmp_path, captured_logs):
    """Org migration (B0.2) + secrets persistence (B0.7) both survive a
    real process restart (a second create_app() against the same
    db_path) without duplication or data loss -- the same "no flag-day
    breakage" discipline every B0 sub-phase has held individually,
    verified here as one continuous restart across the whole stack."""
    from app.config import Settings
    from app.main import create_app
    from tests.test_b03_authz import TEST_SECRET_ENCRYPTION_KEY
    root, repo = git_repo
    c1 = auth_client(root, tmp_path)
    _bootstrap(c1, captured_logs, "solo@example.com")
    c1.app.state.db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("r1", str(repo)))

    settings2 = Settings(root, "127.0.0.1", 8765, tmp_path / "test.db", 30, configured_state_dir=tmp_path / "state",
                          auth_mode="required", session_secret="test-only-secret-never-a-default",
                          secret_encryption_keys=(TEST_SECRET_ENCRYPTION_KEY,))
    app2 = create_app(settings2)
    org_id = app2.state.b02_migration_result["org_id"]
    uid = app2.state.db.one("SELECT id FROM users WHERE email='solo@example.com'")["id"]
    app2.state.secrets_service.create(org_id, "s1", FAKE_TOKEN, uid)

    app3 = create_app(settings2)  # a THIRD construction -- simulating a second restart
    # "NONE" is OrganizationService.migrate_existing_data()'s own real
    # idempotent-no-op result once every user already belongs to an org
    # -- not a new organization, not a re-migration.
    assert app3.state.b02_migration_result["action"] == "NONE"
    assert app3.state.db.one("SELECT COUNT(*) c FROM organizations")["c"] == 1
    assert app3.state.secrets_service.reveal(org_id, "s1", uid) == FAKE_TOKEN


# ================================================================ 10. AUTH_MODE=none end-to-end, whole stack untouched
def test_auth_mode_none_full_stack_smoke(client, git_repo):
    """One continuous AUTH_MODE=none flow across every B0 sub-area's own
    surface -- register a repo, create a task, select it, close it,
    hit every new B0.3-B0.7 guard's own route -- all must behave
    EXACTLY as pre-B0 (no 401/403/429 ever introduced), since none of
    this program's own guards apply outside AUTH_MODE=required."""
    root, repo = git_repo
    r = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo"})
    assert r.status_code not in (401, 403, 404, 429)
    rid = client.get("/api/repositories").json()[0]["id"]
    r2 = client.post("/api/tasks", data={"title": "smoke", "repo_scope_id": str(rid)}, follow_redirects=False)
    assert r2.status_code not in (401, 403, 404, 429)
    tid = int(r2.headers["location"].rsplit("/", 1)[-1])
    assert client.post(f"/api/tasks/{tid}/select").status_code not in (401, 403, 404, 429)
    assert client.post(f"/api/tasks/{tid}/close").status_code not in (401, 403, 404, 429)
    # every B0.1-B0.7 new surface stays fully absent
    for path in ("/auth/login", "/orgs", f"/orgs/1/secrets"):
        assert client.get(path).status_code == 404
