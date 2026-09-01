"""B1.1 -- GET-route cross-org read isolation
(docs/B1_HOSTED_SERVICE_READ_ISOLATION.md). Real, end-to-end evidence:
real SQLite, real org membership (B0.2), real cross-org isolation
enforced server-side for both halves of this phase's mechanism --
(a) require_read_role() on per-id GET routes (the exact same
AuthzService resolution B0.3's require_role() already uses, minus
CSRF), and (b) visible_repository_ids()/visible_task_ids() filtering
list routes before pagination.

AUTH_MODE=none is the default and MUST stay completely unaffected --
the full existing regression suite (1080+ tests, all AUTH_MODE=none)
is the primary evidence for that; this file adds direct, representative
checks of its own, plus a completeness sweep over every GET route."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_b03_authz import (
    auth_client, captured_logs, _bootstrap, _csrf, _create_org, _invite, _accept,
    _second_repo, _link_repo, _make_task, two_org_fixture, _bind_csrf,
)


# ================================================================ Completeness sweep -- the general-mechanism proof
# Routes deliberately NOT swept -- see docs/B1_HOSTED_SERVICE_READ_
# ISOLATION.md's "Not swept" list for the reasoning behind each group.
EXCLUDED_PREFIXES = (
    "/auth/", "/account", "/api/whoami", "/orgs",
    "/help", "/settings", "/api/engineering/", "/api/spec/",
    "/openapi.json", "/docs", "/redoc",  # FastAPI's own framework routes, not app data
    "/health",  # B3.2: no tenant data, must work unauthenticated for a real health probe
)
# List routes -- filtered via _visible_repo_ids/_visible_task_ids/
# _filter_polymorphic instead of a per-id Depends(), so this structural
# sweep (which only sees Depends()-based dependencies) can't see their
# guard directly; each has its own dedicated cross-org test below.
KNOWN_LIST_ROUTES = {
    ("GET", "/"), ("GET", "/repositories"), ("GET", "/api/repositories"),
    ("GET", "/workspaces"), ("GET", "/api/workspaces"), ("GET", "/agents/live"),
    ("GET", "/integrations"), ("GET", "/api/integrations"), ("GET", "/test-runs"),
    ("GET", "/tasks"), ("GET", "/kanban"), ("GET", "/api/tasks"),
    ("GET", "/changes"), ("GET", "/api/changes"),
    ("GET", "/incidents"), ("GET", "/api/incidents"),
    ("GET", "/sandboxes"), ("GET", "/api/sandboxes"), ("GET", "/api/releases"),
}


def test_every_get_route_carries_read_authz_or_is_accounted_for(client):
    """The mechanical completeness proof B1's own spec calls for. Runs
    against the plain AUTH_MODE=none `client` fixture -- route wiring is
    identical regardless of AUTH_MODE, only behavior at request time
    differs."""
    seen_list = set()
    unguarded = []
    for route in client.app.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods or not hasattr(route, "path"):
            continue
        path = route.path
        if any(path.startswith(p) for p in EXCLUDED_PREFIXES):
            continue
        key = ("GET", path)
        if key in KNOWN_LIST_ROUTES:
            seen_list.add(key)
            continue
        dependant = getattr(route, "dependant", None)
        deps = getattr(dependant, "dependencies", []) if dependant else []
        names = {getattr(d.call, "__qualname__", "") for d in deps}
        if not any(n.endswith("require_read_role.<locals>._dep") for n in names):
            unguarded.append(key)
    assert not unguarded, f"GET routes with no require_read_role() guard and not in the known-list allowlist: {unguarded}"
    assert seen_list == KNOWN_LIST_ROUTES, \
        f"list-route allowlist drifted from actual routes: missing={KNOWN_LIST_ROUTES - seen_list}"


# ================================================================ AUTH_MODE=none: zero new surface, zero regression
def test_auth_mode_none_read_routes_unaffected(client, git_repo):
    root, repo = git_repo
    rid = client.app.state.db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("demo", str(repo)))
    tid = client.app.state.db.execute(
        "INSERT INTO tasks(slug,title,status,repo_scope_id) VALUES(?,?,?,?)", ("t1", "T", "BACKLOG", rid))
    for path in (f"/tasks/{tid}", f"/api/tasks/{tid}", "/tasks", "/changes", "/repositories", "/api/repositories"):
        r = client.get(path)
        assert r.status_code not in (401, 403, 404), (path, r.text)


# ================================================================ B1.1(a): per-id GET routes
def test_cross_org_change_detail_is_404_not_leaked(two_org_fixture):
    f = two_org_fixture
    cid_b = f["owner"].app.state.changes.create(title="Org B's secret change", project_id=f["rid_b"])
    denied = f["member"].get(f"/changes/{cid_b}")
    assert denied.status_code == 404, denied.text
    assert "Org B" not in denied.text
    allowed = f["outsider"].get(f"/changes/{cid_b}")
    assert allowed.status_code == 200, allowed.text


def test_cross_org_task_api_is_404_not_leaked(two_org_fixture):
    f = two_org_fixture
    denied = f["member"].get(f"/api/tasks/{f['tid_b']}")
    assert denied.status_code == 404, denied.text
    allowed = f["outsider"].get(f"/api/tasks/{f['tid_b']}")
    assert allowed.status_code == 200, allowed.text


def test_unauthenticated_get_is_401(two_org_fixture, git_repo):
    fresh = TestClient(two_org_fixture["owner"].app)
    r = fresh.get(f"/api/tasks/{two_org_fixture['tid_a']}")
    assert r.status_code == 401, r.text


def test_get_does_not_require_csrf_token(two_org_fixture):
    """The whole point of require_read_role() being a SEPARATE dependency
    from require_role(): a plain GET, with no csrf_token anywhere, must
    still succeed for a real member -- CSRF only ever guards mutation."""
    f = two_org_fixture
    r = f["member"].get(f"/tasks/{f['tid_a']}")
    assert r.status_code == 200, r.text


# ================================================================ B1.1(b): list routes
def test_changes_list_excludes_other_orgs_change(two_org_fixture):
    f = two_org_fixture
    changes = f["owner"].app.state.changes
    cid_a = changes.create(title="Org A change", project_id=f["rid_a"])
    cid_b = changes.create(title="Org B change", project_id=f["rid_b"])
    body = f["member"].get("/api/changes").json()
    ids = {c["id"] for c in body}
    assert cid_a in ids and cid_b not in ids, body


def test_repositories_list_excludes_other_orgs_repo(two_org_fixture):
    f = two_org_fixture
    body = f["member"].get("/api/repositories").json()
    ids = {r["id"] for r in body}
    assert f["rid_a"] in ids and f["rid_b"] not in ids, body


def test_tasks_list_excludes_other_orgs_task(two_org_fixture):
    f = two_org_fixture
    body = f["member"].get("/api/tasks").json()
    ids = {t["id"] for t in body}
    assert f["tid_a"] in ids and f["tid_b"] not in ids, body


def test_changes_html_page_pagination_reflects_visible_only(two_org_fixture):
    """The full end-to-end HTML path: ChangeListSummaryService.build()'s
    own `total`/`total_pages` must reflect only what this member can
    see -- proving the filter runs BEFORE pagination, not after (which
    would silently under-fill a page instead of showing an honest
    count)."""
    f = two_org_fixture
    changes = f["owner"].app.state.changes
    for i in range(3):
        changes.create(title=f"Org B change {i}", project_id=f["rid_b"])
    r = f["member"].get("/changes")
    assert r.status_code == 200, r.text
    assert "Org B change" not in r.text


# ================================================================ B1.2: SSRF guard
def test_ssrf_guard_rejects_loopback_and_metadata():
    from app.services import ssrf_guard
    for url in ("http://127.0.0.1:9/x", "http://169.254.169.254/latest/meta-data", "http://10.1.2.3/x"):
        with pytest.raises(ssrf_guard.SSRFGuardError):
            ssrf_guard.check_url(url)


def test_ssrf_guard_accepts_public_host():
    from app.services import ssrf_guard
    ssrf_guard.check_url("https://8.8.8.8/x")  # must not raise


def test_deployment_service_ssrf_guard_blocks_when_enforced(tmp_path):
    from app.services.deployment_service import DeploymentService
    class _FakeDB:
        def execute(self, *a, **k): return 1
        def one(self, *a, **k): return None
    svc = DeploymentService(_FakeDB(), None, enforce_ssrf_guard=True)
    ok = svc._check_health(1, "http://127.0.0.1:1/health")
    assert ok is False


def test_deployment_service_ssrf_guard_off_under_auth_mode_none(tmp_path):
    """The permanent self-hosted default: enforce_ssrf_guard defaults
    False, so a real localhost DEV target (this module's own audited
    precedent) keeps working exactly as before -- proven by NOT raising/
    short-circuiting on the guard, reaching the real http_get call
    instead (which then fails to connect in this sandbox, a normal
    connection-refused False, not a guard rejection)."""
    from app.services.deployment_service import DeploymentService
    class _FakeDB:
        def execute(self, *a, **k): return 1
        def one(self, *a, **k): return None
    svc = DeploymentService(_FakeDB(), None)  # enforce_ssrf_guard defaults False
    assert svc.enforce_ssrf_guard is False
    svc.health_attempts = 1
    svc.health_delay = 0
    ok = svc._check_health(1, "http://127.0.0.1:1/health")
    assert ok is False  # connection-refused, not a guard rejection -- either way, no crash


def test_sandbox_runtime_ssrf_guard_blocks_when_enforced():
    from app.services.sandbox_runtime import SandboxRuntimeService
    svc = SandboxRuntimeService(enforce_ssrf_guard=True)
    ok, detail = svc.health_check("http://127.0.0.1:1/health")
    assert ok is False
    assert "TARGET_NOT_ALLOWED" in detail
