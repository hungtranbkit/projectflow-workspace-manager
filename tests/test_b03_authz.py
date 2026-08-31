"""B0.3 -- AuthZ (docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md). Real,
end-to-end evidence: real SQLite, real org membership (B0.2), real
cross-org isolation enforced at the service/data layer for the general
`require_role()` sweep across the pre-existing E1-E13 route surface --
never merely a hidden UI affordance, proven here by hitting routes
directly (including via a bare Bearer API token, no browser session at
all) rather than only through whatever the UI happens to link to.

AUTH_MODE=none is the default and MUST stay completely unaffected --
every test in this file constructing a `none`-mode client is proving
exactly that, not merely assuming it. The full existing regression
suite (943+ tests, all AUTH_MODE=none) is the primary evidence for
that; this file adds a few direct, representative checks of its own."""
from __future__ import annotations
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.authz_service import ROLE_LEVEL
from tests.conftest import build_client, run


TEST_SECRET_ENCRYPTION_KEY = "M2RXNV3dhIR-lc1WoE8DGxt-kowfK-34xGTIcF1t8m4="  # test-only, never a real deployment key


def auth_client(root, tmp_path, **overrides):
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "test.db", 30, configured_state_dir=tmp_path / "state",
                         auth_mode="required", session_secret="test-only-secret-never-a-default",
                         secret_encryption_keys=(TEST_SECRET_ENCRYPTION_KEY,), **overrides)
    return build_client(settings)


@pytest.fixture
def captured_logs():
    import logging
    logs = []
    class _Capture(logging.Handler):
        def emit(self, record): logs.append(record.getMessage())
    handler = _Capture()
    auth_logger, email_logger = logging.getLogger("projectflow.auth"), logging.getLogger("projectflow.email")
    auth_logger.addHandler(handler); auth_logger.setLevel(logging.INFO)
    email_logger.addHandler(handler); email_logger.setLevel(logging.INFO)
    yield logs
    auth_logger.removeHandler(handler); email_logger.removeHandler(handler)


def _bootstrap_token(logs):
    msg = next(m for m in logs if "FIRST_USER_SETUP" in m)
    return re.search(r"token=(\S+)", msg).group(1)


def _bootstrap(client, logs, email="owner@example.com"):
    token = _bootstrap_token(logs)
    client.post("/auth/bootstrap", data={"token": token, "email": email})


def _csrf(client, path="/account"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def _create_org(client, name="Acme Inc"):
    csrf = _csrf(client, "/orgs/new")
    r = client.post("/orgs", data={"name": name, "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[-1])


def _invite(client, org_id, email, role, logs):
    logs.clear()
    csrf = _csrf(client, f"/orgs/{org_id}")
    r = client.post(f"/orgs/{org_id}/invite", data={"email": email, "role": role, "csrf_token": csrf},
                     follow_redirects=False)
    assert r.status_code == 303, r.text
    email_log = next(m for m in logs if "would send to" in m)
    return re.search(r"invitations/(\S+)", email_log).group(1)


def _accept(app, token) -> TestClient:
    """A fresh cookie jar sharing the same app/db -- a distinct logged-in
    'browser' for the invited user, exactly test_b02's own pattern."""
    fresh = TestClient(app)
    r = fresh.post(f"/orgs/invitations/{token}", follow_redirects=False)
    assert r.status_code == 303, r.text
    return fresh


def _bind_csrf(client) -> str:
    """B0.4 folded CSRF into the same require_role()/manual-route sweep
    this file exercises -- every "allowed" request below now needs a
    real, session-bound token too, minted the same way any real page
    view mints one (a GET request through base.html's own Jinja
    global). See tests/test_b04_csrf.py for CSRF's own dedicated
    coverage; this helper only exists so B0.3's own tests keep
    verifying AuthZ specifically, not incidentally failing on CSRF."""
    return re.search(r'name="csrf_token"\s+value="([^"]+)"', client.get("/account").text).group(1)


def _second_repo(root, name="second"):
    repo = root / name
    repo.mkdir(parents=True)
    run(repo, "git", "init", "-b", "main")
    run(repo, "git", "config", "user.email", "test@example.invalid")
    run(repo, "git", "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "base")
    return repo


def _link_repo(client, org_id, repo_path, name="demo"):
    rid = client.app.state.db.execute(
        "INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", (name, str(repo_path)))
    csrf = _csrf(client, f"/orgs/{org_id}")
    r = client.post(f"/orgs/{org_id}/repositories/link", data={"repo_id": rid, "csrf_token": csrf},
                     follow_redirects=False)
    assert r.status_code == 303, r.text
    return rid


def _make_task(db, repo_id, change_id=None, status="BACKLOG"):
    import secrets
    slug = f"t{repo_id}-{status}-{secrets.token_hex(4)}"
    return db.execute(
        "INSERT INTO tasks(slug,title,status,repo_scope_id,change_id) VALUES(?,?,?,?,?)",
        (slug, "Test Task", status, repo_id, change_id))


@pytest.fixture
def two_org_fixture(git_repo, tmp_path, captured_logs):
    """Org A: owner (OWNER), a MEMBER, a VIEWER, one repo, one BACKLOG
    task scoped to that repo. Org B: a separate OWNER, one repo -- used
    to prove cross-org isolation. Returns a dict of everything a test
    might need."""
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

    return dict(owner=owner, member=member, viewer=viewer, outsider=outsider,
                org_a=org_a, org_b=org_b, rid_a=rid_a, rid_b=rid_b, tid_a=tid_a, tid_b=tid_b, db=db)


# ================================================================ Completeness sweep -- the general-mechanism proof
# The 12 body-based `create` routes below have no existing-resource id in
# the path to build a Depends() dependency against; each is instead
# guarded by an inline _require_org_role_for_*/_require_login_only call
# at the top of its own handler body (see app/main.py) -- verified by
# their own allowed/denied tests further down, not by this structural
# sweep (which can only see Depends()-based dependencies).
KNOWN_MANUAL_CREATE_ROUTES = {
    ("POST", "/api/repositories"), ("POST", "/api/tasks"), ("POST", "/api/tasks/create"),
    ("POST", "/api/tasks/new-with-workspace"), ("POST", "/api/workspaces"), ("POST", "/api/incidents"),
    ("POST", "/api/integrations"), ("POST", "/api/releases"), ("POST", "/api/work-products"),
    ("POST", "/api/changes"), ("POST", "/changes"), ("POST", "/api/engineering/validate-assignment"),
}
# AuthN identity/pre-org routes (B0.1) and the /orgs/* family (B0.2) --
# already carry their own membership guard (_org_context) or are
# structurally pre-identity (login/bootstrap/logout) and are explicitly
# out of B0.3's "existing E1-E13 route surface" scope.
EXCLUDED_PREFIXES = ("/auth/", "/account", "/orgs")


def test_every_mutating_route_carries_authz_or_is_accounted_for(client):
    """The mechanical completeness proof the B0 spec itself calls for
    (docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md's own "a test
    asserting every mutating route carries the dependency, not spot-
    checked"). Runs against the plain AUTH_MODE=none `client` fixture --
    route wiring is identical regardless of AUTH_MODE, only behavior at
    request time differs."""
    seen_manual = set()
    unguarded = []
    for route in client.app.routes:
        methods = getattr(route, "methods", None) or set()
        mutating = methods & {"POST", "PUT", "DELETE", "PATCH"}
        if not mutating or not hasattr(route, "path"):
            continue
        path = route.path
        if any(path.startswith(p) for p in EXCLUDED_PREFIXES):
            continue
        for method in mutating:
            key = (method, path)
            if key in KNOWN_MANUAL_CREATE_ROUTES:
                seen_manual.add(key)
                continue
            dependant = getattr(route, "dependant", None)
            deps = getattr(dependant, "dependencies", []) if dependant else []
            names = {getattr(d.call, "__qualname__", "") for d in deps}
            if not any(n.endswith("require_role.<locals>._dep") for n in names):
                unguarded.append(key)
    assert not unguarded, f"mutating routes with no require_role() guard and not in the manual allowlist: {unguarded}"
    assert seen_manual == KNOWN_MANUAL_CREATE_ROUTES, \
        f"manual-route allowlist drifted from actual routes: missing={KNOWN_MANUAL_CREATE_ROUTES - seen_manual}"


# ================================================================ AUTH_MODE=none: zero new surface, zero regression
def test_auth_mode_none_sample_routes_unaffected(client, git_repo):
    """A representative sample across several different B0.3 resource
    kinds -- proving require_role()'s AUTH_MODE=none no-op holds for the
    general mechanism, not just the routes B0.1/B0.2 already covered.
    The full suite (943+ tests, this file's own two_org_fixture aside)
    is the exhaustive version of this same claim."""
    root, repo = git_repo
    rid = client.app.state.db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("demo", str(repo)))
    tid = client.app.state.db.execute(
        "INSERT INTO tasks(slug,title,status,repo_scope_id) VALUES(?,?,?,?)", ("t1", "T", "BACKLOG", rid))
    r = client.post(f"/api/tasks/{tid}/select")
    assert r.status_code not in (401, 403, 404), r.text
    r2 = client.post("/api/repositories", data={"repo_path": str(repo), "repo_name": "demo"})
    assert r2.status_code not in (401, 403, 404), r2.text


# ================================================================ Allowed / denied per role
def test_member_can_act_owner_can_act(two_org_fixture):
    f = two_org_fixture
    token = _bind_csrf(f["member"])
    r = f["member"].post(f"/api/tasks/{f['tid_a']}/select", headers={"X-CSRF-Token": token})
    assert r.status_code not in (401, 403, 404), r.text


def test_viewer_gets_403_insufficient_role_not_404(two_org_fixture):
    """Membership IS confirmed (VIEWER is a real member of Org A) -- so
    this must be 403, the B0.2-established distinction from 404's
    existence-hiding for a non-member."""
    f = two_org_fixture
    r = f["viewer"].post(f"/api/tasks/{f['tid_a']}/select")
    assert r.status_code == 403, r.text


def test_cross_org_member_gets_404_not_403(two_org_fixture):
    """Existence-hiding: outsider is a real, logged-in OWNER -- just not
    of Org A. A cross-org reach for Org A's task must be 404, never 403
    (403 would confirm the task id is valid, leaking its existence)."""
    f = two_org_fixture
    r = f["outsider"].post(f"/api/tasks/{f['tid_a']}/select")
    assert r.status_code == 404, r.text
    # And the reverse holds too -- Org A's own member can't reach Org B's
    # task (the fixture's `owner` client created both orgs and is a
    # legitimate member of each, so it isn't a valid "outsider" probe here).
    r2 = f["member"].post(f"/api/tasks/{f['tid_b']}/select")
    assert r2.status_code == 404, r2.text


def test_unauthenticated_request_is_401(two_org_fixture):
    f = two_org_fixture
    fresh = TestClient(f["owner"].app)
    r = fresh.post(f"/api/tasks/{f['tid_a']}/select")
    assert r.status_code == 401, r.text


def test_nonexistent_and_malformed_ids_fail_closed(two_org_fixture):
    f = two_org_fixture
    assert f["owner"].post("/api/tasks/999999/select").status_code == 404
    assert f["owner"].post("/api/tasks/not-a-number/select").status_code in (404, 422)


def test_removed_membership_immediately_loses_access(two_org_fixture):
    """Stale-permission proof: member could act a moment ago; once
    removed from Org A, the exact same request is refused."""
    f = two_org_fixture
    token = _bind_csrf(f["member"])
    r1 = f["member"].post(f"/api/tasks/{f['tid_a']}/close", headers={"X-CSRF-Token": token})
    assert r1.status_code not in (401, 403, 404), r1.text

    member_uid = f["db"].one("SELECT id FROM users WHERE email='member@example.com'")["id"]
    csrf = _csrf(f["owner"], f"/orgs/{f['org_a']}")
    rm = f["owner"].post(f"/orgs/{f['org_a']}/members/{member_uid}/remove",
                          data={"csrf_token": csrf}, follow_redirects=False)
    assert rm.status_code == 303, rm.text

    r2 = f["member"].post(f"/api/tasks/{f['tid_a']}/select")
    assert r2.status_code == 404, r2.text


def test_direct_api_bypass_via_bearer_token_still_enforced(two_org_fixture):
    """Proves the guard is a real server-side boundary, not merely
    something the UI happens to hide -- a bare Bearer API token, zero
    browser session/cookies at all, hitting the route directly."""
    f = two_org_fixture
    viewer_uid = f["db"].one("SELECT id FROM users WHERE email='viewer@example.com'")["id"]
    raw_token = f["owner"].app.state.auth_service.create_api_token(viewer_uid, "ci-token")[1]

    bare = TestClient(f["owner"].app)
    # No cookies at all -- identity comes only from the Authorization header.
    r_allowed_membership_but_low_role = bare.post(
        f"/api/tasks/{f['tid_a']}/select", headers={"Authorization": f"Bearer {raw_token}"})
    assert r_allowed_membership_but_low_role.status_code == 403  # VIEWER, same as the cookie-based case

    r_cross_org = bare.post(f"/api/tasks/{f['tid_b']}/select", headers={"Authorization": f"Bearer {raw_token}"})
    assert r_cross_org.status_code == 404

    r_no_token = bare.post(f"/api/tasks/{f['tid_a']}/select")
    assert r_no_token.status_code == 401


# ================================================================ Cross-resource-kind spot checks (not just Task)
def test_workspace_kind_cross_org_isolation(two_org_fixture, git_repo):
    f = two_org_fixture
    wid = f["db"].execute(
        "INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,status) "
        "VALUES(?,?,?,?,?,?,?,?)", (f["rid_a"], "claude", "demo", "agent/claude/demo", "/tmp/nonexistent-wt",
                                     "main", "deadbeef", "CREATED"))
    assert f["outsider"].post(f"/api/workspaces/{wid}/close").status_code == 404
    token = _bind_csrf(f["member"])
    assert f["member"].post(f"/api/workspaces/{wid}/close",
                             headers={"X-CSRF-Token": token}).status_code not in (401, 403, 404)


def test_change_kind_cross_org_isolation(two_org_fixture):
    f = two_org_fixture
    cid = f["db"].execute(
        "INSERT INTO changes(project_id,title,description) VALUES(?,?,?)", (f["rid_a"], "Change A", ""))
    assert f["outsider"].post(f"/api/changes/{cid}/lifecycle", data={"state": "NEW"}).status_code == 404
    # member has the role, but may still get a business-logic 4xx from
    # the underlying lifecycle transition itself -- only proving the
    # AuthZ layer itself let the request through (not a 401/403/404).
    token = _bind_csrf(f["member"])
    assert f["member"].post(f"/api/changes/{cid}/lifecycle",
                             data={"state": "NEW", "csrf_token": token}).status_code not in (401, 403, 404)


# ================================================================ Multi-org / body-based create routes
# The 12 body-based `create` routes carry CSRF as a plain Depends()
# (there's no path-id to fold it into a require_role() call for -- see
# app/main.py's own _mutating_csrf), and FastAPI always resolves a
# route's Depends() before its body runs -- so for these 12 routes
# specifically, CSRF is necessarily checked BEFORE the inline
# _require_org_role_for_*/_require_login_only call in the body (the
# reverse of the path-param routes above, where require_role's own
# _dep checks CSRF last, deliberately, by calling it itself at the end
# of its own body). Every request below -- allowed or denied -- needs
# its own valid CSRF token first, or it never reaches the AuthZ check
# this file is actually testing.
def test_create_task_requires_role_in_named_repo_org(two_org_fixture):
    f = two_org_fixture
    outsider_token = _bind_csrf(f["outsider"])
    denied = f["outsider"].post("/api/tasks", data={"title": "x", "repo_scope_id": str(f["rid_a"]),
                                                      "csrf_token": outsider_token})
    assert denied.status_code == 404, denied.text
    member_token = _bind_csrf(f["member"])
    allowed = f["member"].post("/api/tasks", data={"title": "x", "repo_scope_id": str(f["rid_a"]),
                                                     "csrf_token": member_token})
    assert allowed.status_code not in (401, 403, 404), allowed.text


def test_create_task_blank_repo_scope_allowed_for_any_authenticated_user(two_org_fixture):
    """A BACKLOG task with no repository yet is legitimately orgless --
    any identified user may create pure intent, matching the existing
    E1-E13 BACKLOG contract (nothing tenant-scoped is touched)."""
    f = two_org_fixture
    token = _bind_csrf(f["outsider"])
    # follow_redirects=False (B1.1's own established precedent, see this
    # file's other follow_redirects=False call sites): TestClient's
    # default auto-follow would otherwise chase the 303 into GET
    # /tasks/{tid}, which B1.1's require_read_role() correctly 404s for
    # an orgless task (same fail-closed "zero resolved orgs" precedent
    # B0.3's require_role() already applies to mutations of this exact
    # same task) -- masking the actual thing under test here, the
    # CREATE response itself.
    r = f["outsider"].post("/api/tasks", data={"title": "just an idea", "csrf_token": token}, follow_redirects=False)
    assert r.status_code not in (401, 403, 404), r.text


def test_create_repository_requires_login_only_no_org_yet(two_org_fixture, git_repo):
    root, _ = git_repo
    extra = _second_repo(root, "extra-repo")
    outsider_token = _bind_csrf(two_org_fixture["outsider"])
    r = two_org_fixture["outsider"].post(
        "/api/repositories", data={"repo_path": str(extra), "repo_name": "extra", "csrf_token": outsider_token})
    assert r.status_code not in (401, 403, 404), r.text

    # A totally fresh, cookie-less client hits CSRF (checked first for
    # this route family, see the module comment above) before AuthZ's
    # own 401 -- both are real rejections either way, just a different
    # one surfaces first for this route shape.
    fresh = TestClient(two_org_fixture["owner"].app)
    r2 = fresh.post("/api/repositories", data={"repo_path": str(extra), "repo_name": "extra"})
    assert r2.status_code == 403, r2.text
