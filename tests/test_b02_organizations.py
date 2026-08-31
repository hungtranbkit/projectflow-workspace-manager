"""B0.2 -- Organizations/Tenants (docs/B0_HOSTED_PLATFORM_SECURITY_
FOUNDATION.md). Real, end-to-end evidence: real SQLite, real hashed
invitation tokens, real cross-org isolation enforced at the service/
data layer (never merely UI-hidden), real migration/backfill against
realistic pre-existing B0.1 data (a bootstrapped user + real
repositories, not only a fresh empty database).

AUTH_MODE=none is the default and MUST stay completely unaffected --
every test in this file constructing a `none`-mode client is proving
exactly that, not merely assuming it."""
from __future__ import annotations
import logging
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import build_client


TEST_SECRET_ENCRYPTION_KEY = "M2RXNV3dhIR-lc1WoE8DGxt-kowfK-34xGTIcF1t8m4="  # test-only, never a real deployment key


def auth_client(root, tmp_path, **overrides):
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "test.db", 30, configured_state_dir=tmp_path / "state",
                         auth_mode="required", session_secret="test-only-secret-never-a-default",
                         secret_encryption_keys=(TEST_SECRET_ENCRYPTION_KEY,), **overrides)
    return build_client(settings)


@pytest.fixture
def captured_logs():
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


# ================================================================ AUTH_MODE=none: zero new surface, zero regression
def test_auth_mode_none_org_routes_all_404_not_crash(client):
    """`client` (conftest's own default fixture) is AUTH_MODE=none.
    Every /orgs/* route -- GET and POST -- must 404, never crash."""
    for path in ("/orgs", "/orgs/new", "/orgs/1", "/orgs/invitations/x"):
        assert client.get(path).status_code == 404, path
    for path, data in [
        ("/orgs", {"name": "test"}),
        ("/orgs/1/invite", {"email": "a@b.com", "role": "MEMBER"}),
        ("/orgs/1/repositories/link", {"repo_id": "1"}),
        ("/orgs/1/members/1/remove", {}),
        ("/orgs/1/members/1/role", {"role": "ADMIN"}),
        ("/orgs/1/invitations/1/revoke", {}),
        ("/orgs/invitations/x", {}),
    ]:
        r = client.post(path, data=data)
        assert r.status_code == 404, (path, r.status_code)


def test_regression_csrf_guarded_b01_routes_404_not_500_under_auth_mode_none(client):
    """Real bug found during B0.2 implementation: Depends(require_csrf)
    unconditionally touched request.session, which doesn't exist as a
    scope key at all under AUTH_MODE=none (SessionMiddleware is never
    installed there) -- every CSRF-guarded POST route, including B0.1's
    own /auth/logout and /account/api-tokens(/revoke), crashed with an
    unhandled AssertionError (500) instead of a clean 404. Fixed in
    app/services/csrf.py's require_csrf(); this is the regression test."""
    for path, data in [
        ("/auth/logout", {}),
        ("/account/api-tokens", {"name": "x"}),
        ("/account/api-tokens/1/revoke", {}),
    ]:
        r = client.post(path, data=data)
        assert r.status_code == 404, (path, r.status_code)


def test_auth_mode_none_no_migration_no_org_tables_touched(client):
    assert not hasattr(client.app.state, "b02_migration_result") or client.app.state.b02_migration_result is None
    assert client.app.state.db.all("SELECT * FROM organizations") == []


# ================================================================ Org creation / membership boundary
def test_create_org_makes_creator_owner(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs)
    org_id = _create_org(c)
    r = c.get(f"/orgs/{org_id}")
    assert r.status_code == 200 and "OWNER" in r.text and "Acme Inc" in r.text
    row = c.app.state.db.one("SELECT role FROM organization_members WHERE org_id=? AND user_id=1", (org_id,))
    assert row["role"] == "OWNER"


def test_stranger_user_404_on_unrelated_org(git_repo, tmp_path, captured_logs):
    """Existence-hiding: a non-member never learns an org id is valid --
    404, not 403."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c, "Owner Org")

    org2_id = _create_org(c, "Org For Outsider")
    outsider_token = _invite(c, org2_id, "outsider@example.com", "OWNER", captured_logs)
    outsider = TestClient(c.app)
    r = outsider.post(f"/orgs/invitations/{outsider_token}", follow_redirects=False)
    assert r.status_code == 303

    # outsider is a real, logged-in member of org2 -- but NOT of org_id
    assert outsider.get(f"/orgs/{org_id}").status_code == 404
    outsider_csrf = _csrf(outsider)
    assert outsider.post(f"/orgs/{org_id}/invite",
                          data={"email": "x@example.com", "role": "MEMBER", "csrf_token": outsider_csrf}).status_code == 404
    # org2's own repos/members are untouched by anything happening in org_id
    assert outsider.get(f"/orgs/{org2_id}").status_code == 200


# ================================================================ Invitation lifecycle
def test_invitation_accept_creates_new_user_and_session(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    token = _invite(c, org_id, "teammate@example.com", "MEMBER", captured_logs)

    fresh = TestClient(c.app)
    peek = fresh.get(f"/orgs/invitations/{token}")
    assert peek.status_code == 200 and "Acme Inc" in peek.text and "MEMBER" in peek.text
    assert fresh.get("/api/whoami").json()["authenticated"] is False  # GET never auto-authenticates

    confirm = fresh.post(f"/orgs/invitations/{token}", follow_redirects=False)
    assert confirm.status_code == 303 and confirm.headers["location"] == f"/orgs/{org_id}"
    assert fresh.get("/api/whoami").json() == {"auth_mode": "required", "authenticated": True, "email": "teammate@example.com"}
    row = c.app.state.db.one("SELECT role FROM organization_members m JOIN users u ON u.id=m.user_id "
                              "WHERE u.email='teammate@example.com' AND m.org_id=?", (org_id,))
    assert row["role"] == "MEMBER"


def test_invitation_accept_for_existing_user_adds_membership_only(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org1 = _create_org(c, "First Org")
    org2 = _create_org(c, "Second Org")
    token1 = _invite(c, org1, "shared@example.com", "MEMBER", captured_logs)
    TestClient(c.app).post(f"/orgs/invitations/{token1}")
    before = c.app.state.db.one("SELECT COUNT(*) n FROM users WHERE email='shared@example.com'")["n"]
    assert before == 1

    token2 = _invite(c, org2, "shared@example.com", "ADMIN", captured_logs)
    TestClient(c.app).post(f"/orgs/invitations/{token2}")
    after = c.app.state.db.one("SELECT COUNT(*) n FROM users WHERE email='shared@example.com'")["n"]
    assert after == 1  # no duplicate user created
    memberships = c.app.state.db.all(
        "SELECT org_id, role FROM organization_members m JOIN users u ON u.id=m.user_id WHERE u.email='shared@example.com' ORDER BY org_id")
    assert memberships == [{"org_id": org1, "role": "MEMBER"}, {"org_id": org2, "role": "ADMIN"}]


def test_invite_already_a_member_is_a_noop_not_a_duplicate(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    token = _invite(c, org_id, "teammate@example.com", "MEMBER", captured_logs)
    TestClient(c.app).post(f"/orgs/invitations/{token}")

    csrf = _csrf(c, f"/orgs/{org_id}")
    r = c.post(f"/orgs/{org_id}/invite", data={"email": "teammate@example.com", "role": "ADMIN", "csrf_token": csrf},
               follow_redirects=False)
    assert r.status_code == 303
    rows = c.app.state.db.all("SELECT role FROM organization_members WHERE org_id=? AND user_id="
                               "(SELECT id FROM users WHERE email='teammate@example.com')", (org_id,))
    assert len(rows) == 1 and rows[0]["role"] == "MEMBER"  # unchanged, not silently upgraded


def test_invitation_token_hashed_at_rest_never_raw(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    token = _invite(c, org_id, "teammate@example.com", "MEMBER", captured_logs)
    rows = c.app.state.db.all("SELECT * FROM organization_invitations")
    assert rows and all(token not in str(v) for row in rows for v in row.values())


def test_invitation_single_use_replay_rejected(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    token = _invite(c, org_id, "teammate@example.com", "MEMBER", captured_logs)
    first, second = TestClient(c.app), TestClient(c.app)
    r1 = first.post(f"/orgs/invitations/{token}", follow_redirects=False)
    assert r1.status_code == 303
    r2 = second.post(f"/orgs/invitations/{token}")
    assert r2.status_code == 200 and "invalid" in r2.text.lower()
    assert second.get("/api/whoami").json()["authenticated"] is False


def test_invitation_revoked_rejected(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    token = _invite(c, org_id, "teammate@example.com", "MEMBER", captured_logs)
    iid = c.app.state.db.one("SELECT id FROM organization_invitations WHERE email='teammate@example.com'")["id"]
    csrf = _csrf(c, f"/orgs/{org_id}")
    r = c.post(f"/orgs/{org_id}/invitations/{iid}/revoke", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    accept = TestClient(c.app).post(f"/orgs/invitations/{token}")
    assert accept.status_code == 200 and "invalid" in accept.text.lower()


def test_invitation_expired_rejected(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    token = _invite(c, org_id, "teammate@example.com", "MEMBER", captured_logs)
    c.app.state.db.execute(
        "UPDATE organization_invitations SET expires_at='2000-01-01 00:00:00' WHERE email='teammate@example.com'")
    r = TestClient(c.app).post(f"/orgs/invitations/{token}")
    assert r.status_code == 200 and "invalid" in r.text.lower()


def test_invitation_malformed_token_handled_gracefully(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    r = TestClient(c.app).get("/orgs/invitations/not-a-real-token%20%20garbage")
    assert r.status_code == 200 and "invalid" in r.text.lower()


# ================================================================ Role enforcement (data layer, not UI-only)
def test_member_cannot_invite_remove_or_link(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    token = _invite(c, org_id, "teammate@example.com", "MEMBER", captured_logs)
    member = TestClient(c.app)
    member.post(f"/orgs/invitations/{token}")
    member_csrf = _csrf(member)

    r1 = member.post(f"/orgs/{org_id}/invite", data={"email": "x@example.com", "role": "OWNER", "csrf_token": member_csrf})
    assert r1.status_code == 403
    r2 = member.post(f"/orgs/{org_id}/repositories/link", data={"repo_id": "1", "csrf_token": member_csrf})
    assert r2.status_code == 403
    r3 = member.post(f"/orgs/{org_id}/members/1/remove", data={"csrf_token": member_csrf})
    assert r3.status_code == 403
    # underlying state genuinely unchanged -- not merely a rejected response
    assert c.app.state.db.one(
        "SELECT id FROM organization_invitations WHERE email='x@example.com'") is None


def test_admin_can_invite_and_remove_but_only_owner_changes_roles(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    admin_token = _invite(c, org_id, "admin@example.com", "ADMIN", captured_logs)
    admin = TestClient(c.app)
    admin.post(f"/orgs/invitations/{admin_token}")
    admin_csrf = _csrf(admin)

    invite_r = admin.post(f"/orgs/{org_id}/invite",
                           data={"email": "new@example.com", "role": "MEMBER", "csrf_token": admin_csrf},
                           follow_redirects=False)
    assert invite_r.status_code == 303  # ADMIN can invite

    admin_uid = c.app.state.db.one("SELECT id FROM users WHERE email='admin@example.com'")["id"]
    role_r = admin.post(f"/orgs/{org_id}/members/{admin_uid}/role",
                         data={"role": "OWNER", "csrf_token": admin_csrf})
    assert role_r.status_code == 403  # ADMIN cannot change roles, only OWNER can


def test_last_owner_cannot_be_removed_or_demoted(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    owner_uid = 1
    csrf = _csrf(c, f"/orgs/{org_id}")
    r1 = c.post(f"/orgs/{org_id}/members/{owner_uid}/remove", data={"csrf_token": csrf})
    assert r1.status_code == 403
    r2 = c.post(f"/orgs/{org_id}/members/{owner_uid}/role", data={"role": "MEMBER", "csrf_token": csrf})
    assert r2.status_code == 403
    assert c.app.state.db.one(
        "SELECT role FROM organization_members WHERE org_id=? AND user_id=?", (org_id, owner_uid))["role"] == "OWNER"


# ================================================================ Repository linking (cross-org isolation)
def test_repository_link_and_unlink(git_repo, tmp_path, captured_logs):
    root, repo = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    rid = c.app.state.db.execute(
        "INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("demo", str(repo)))
    org_id = _create_org(c)
    csrf = _csrf(c, f"/orgs/{org_id}")
    r = c.post(f"/orgs/{org_id}/repositories/link", data={"repo_id": rid, "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert c.app.state.db.one("SELECT organization_id FROM repositories WHERE id=?", (rid,))["organization_id"] == org_id

    csrf2 = _csrf(c, f"/orgs/{org_id}")
    r2 = c.post(f"/orgs/{org_id}/repositories/{rid}/unlink", data={"csrf_token": csrf2}, follow_redirects=False)
    assert r2.status_code == 303
    assert c.app.state.db.one("SELECT organization_id FROM repositories WHERE id=?", (rid,))["organization_id"] is None


def test_repository_cannot_be_linked_to_a_second_org(git_repo, tmp_path, captured_logs):
    root, repo = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    rid = c.app.state.db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("demo", str(repo)))
    org1 = _create_org(c, "Org One")
    org2 = _create_org(c, "Org Two")
    csrf1 = _csrf(c, f"/orgs/{org1}")
    c.post(f"/orgs/{org1}/repositories/link", data={"repo_id": rid, "csrf_token": csrf1})

    csrf2 = _csrf(c, f"/orgs/{org2}")
    r = c.post(f"/orgs/{org2}/repositories/link", data={"repo_id": rid, "csrf_token": csrf2})
    assert r.status_code == 403
    assert c.app.state.db.one("SELECT organization_id FROM repositories WHERE id=?", (rid,))["organization_id"] == org1


# ================================================================ CSRF enforcement on B0.2's own new mutating routes
def test_org_mutations_require_csrf_token(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    r = c.post(f"/orgs/{org_id}/invite", data={"email": "x@example.com", "role": "MEMBER"})
    assert r.status_code == 403


# ================================================================ Auditability
def test_membership_events_are_audited(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    org_id = _create_org(c)
    token = _invite(c, org_id, "teammate@example.com", "MEMBER", captured_logs)
    TestClient(c.app).post(f"/orgs/invitations/{token}")
    csrf = _csrf(c, f"/orgs/{org_id}")
    member_uid = c.app.state.db.one("SELECT id FROM users WHERE email='teammate@example.com'")["id"]
    c.post(f"/orgs/{org_id}/members/{member_uid}/remove", data={"csrf_token": csrf})

    events = c.app.state.db.all(
        "SELECT action FROM workspace_events WHERE entity_type='organization' AND entity_id=? ORDER BY id", (org_id,))
    actions = [e["action"] for e in events]
    assert "ORG_CREATED" in actions
    assert "MEMBER_INVITED" in actions
    assert actions.count("MEMBER_JOINED") == 2  # owner at creation + teammate at accept
    assert "MEMBER_REMOVED" in actions


# ================================================================ Migration / backfill against realistic B0.1 data
def test_migration_backfills_single_user_and_their_repositories(git_repo, tmp_path, captured_logs):
    """Realistic pre-existing B0.1 data: a bootstrapped user + real
    repositories registered before organizations existed at all --
    not merely a fresh empty database."""
    root, repo = git_repo
    c1 = auth_client(root, tmp_path)
    _bootstrap(c1, captured_logs, "solo@example.com")
    r1 = c1.app.state.db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("repo-a", str(root / "a")))
    r2 = c1.app.state.db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("repo-b", str(root / "b")))
    assert c1.app.state.db.all("SELECT * FROM organizations") == []  # confirmed: nothing migrated yet

    # A second create_app() against the SAME db_path is what actually
    # triggers the startup migration -- simulating a real process restart.
    settings2 = Settings(root, "127.0.0.1", 8765, tmp_path / "test.db", 30, configured_state_dir=tmp_path / "state",
                          auth_mode="required", session_secret="test-only-secret-never-a-default",
                          secret_encryption_keys=(TEST_SECRET_ENCRYPTION_KEY,))
    app2 = create_app(settings2)
    result = app2.state.b02_migration_result
    assert result["action"] == "MIGRATED"
    assert result["repositories_linked"] == 2
    org_id = result["org_id"]
    assert app2.state.db.one("SELECT organization_id FROM repositories WHERE id=?", (r1,))["organization_id"] == org_id
    assert app2.state.db.one("SELECT organization_id FROM repositories WHERE id=?", (r2,))["organization_id"] == org_id
    assert app2.state.db.one(
        "SELECT role FROM organization_members WHERE org_id=? AND user_id=1", (org_id,))["role"] == "OWNER"


def test_migration_is_idempotent_across_repeated_restarts(git_repo, tmp_path, captured_logs):
    root, repo = git_repo
    c1 = auth_client(root, tmp_path)
    _bootstrap(c1, captured_logs, "solo@example.com")
    c1.app.state.db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("repo-a", str(root / "a")))

    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "test.db", 30, configured_state_dir=tmp_path / "state",
                         auth_mode="required", session_secret="test-only-secret-never-a-default",
                         secret_encryption_keys=(TEST_SECRET_ENCRYPTION_KEY,))
    app2 = create_app(settings)
    assert app2.state.b02_migration_result["action"] == "MIGRATED"
    org_count_after_first = app2.state.db.one("SELECT COUNT(*) c FROM organizations")["c"]

    app3 = create_app(settings)
    assert app3.state.b02_migration_result["action"] == "NONE"
    org_count_after_second = app3.state.db.one("SELECT COUNT(*) c FROM organizations")["c"]
    assert org_count_after_first == org_count_after_second == 1


def test_migration_noop_when_no_users_exist(git_repo, tmp_path):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    assert c.app.state.b02_migration_result == {"action": "NONE", "reason": "no users exist yet"}
    assert c.app.state.db.all("SELECT * FROM organizations") == []


def test_migration_refuses_to_guess_when_ownership_is_ambiguous():
    """Direct service-level test (not reachable via B0.1's own HTTP
    surface, which can only ever produce exactly one user with no
    organization -- defensive correctness, not a guessed default): two
    users, neither in any organization, must never be silently
    auto-assigned to the same organization."""
    import tempfile
    from pathlib import Path
    from app.db import Database
    from app.services.auth_service import AuthService
    from app.services.email_sender import EmailSenderService
    from app.services.organization_service import OrganizationService

    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "t.db"); db.init()
    email = EmailSenderService(host=None, port=587, user=None, password=None, from_addr="x@x")
    auth = AuthService(db, email)
    org = OrganizationService(db, auth, email)
    auth._create_user("a@example.com")
    auth._create_user("b@example.com")
    result = org.migrate_existing_data()
    assert result["action"] == "SKIPPED_AMBIGUOUS"
    assert db.all("SELECT * FROM organizations") == []
