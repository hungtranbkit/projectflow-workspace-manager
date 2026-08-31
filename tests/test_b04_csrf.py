"""B0.4 -- CSRF (docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md,
ADR-003's resolved design). Real, end-to-end evidence: the general
`require_role()` sweep from B0.3 now also enforces the in-house
double-submit-cookie CSRF token (`app/services/csrf.py`) on the same
143 pre-existing mutating routes, Bearer/API-token requests stay
structurally exempt, and the "login CSRF" gap on /auth/verify (a
forged cross-site POST of the ATTACKER's own valid magic-link token,
silently logging the VICTIM's browser into the attacker's account) is
closed with the same double-submit primitive, one level earlier than a
real session exists.

AUTH_MODE=none is the default and MUST stay completely unaffected --
every test in this file constructing a `none`-mode client is proving
exactly that, not merely assuming it. The full existing regression
suite (943+ tests, all AUTH_MODE=none) is the primary evidence for
that."""
from __future__ import annotations
import re

import pytest
from fastapi.testclient import TestClient

from tests.test_b03_authz import (
    auth_client, captured_logs, _bootstrap, _csrf, _create_org, _invite, _accept,
    _second_repo, _link_repo, _make_task, two_org_fixture,
)


# ================================================================ General mutating-route sweep (folded into require_role)
def test_missing_csrf_token_is_403(two_org_fixture):
    f = two_org_fixture
    # member IS authorized (role+org both valid) -- omitting csrf_token
    # entirely must still be refused.
    r = f["member"].post(f"/api/tasks/{f['tid_a']}/select")
    assert r.status_code == 403
    assert r.json()["detail"] == "CSRF_TOKEN_MISSING"


def _member_csrf_token(f):
    """member's session-bound token, minted the moment their invitation-
    accept response (a real page render) went through base.html."""
    return re.search(r'name="csrf_token"\s+value="([^"]+)"', f["member"].get("/account").text).group(1)


def test_wrong_csrf_token_is_403(two_org_fixture):
    f = two_org_fixture
    _member_csrf_token(f)  # binds a real token to member's session first
    r = f["member"].post(f"/api/tasks/{f['tid_a']}/select", headers={"X-CSRF-Token": "not-the-real-token"})
    assert r.status_code == 403
    assert r.json()["detail"] == "CSRF_TOKEN_INVALID"


def test_correct_csrf_token_via_header_succeeds(two_org_fixture):
    f = two_org_fixture
    token = _member_csrf_token(f)
    r = f["member"].post(f"/api/tasks/{f['tid_a']}/select", headers={"X-CSRF-Token": token})
    assert r.status_code not in (401, 403, 404), r.text


def test_correct_csrf_token_via_form_field_succeeds(two_org_fixture):
    f = two_org_fixture
    token = _member_csrf_token(f)
    r = f["member"].post(f"/api/tasks/{f['tid_a']}/close", data={"csrf_token": token})
    assert r.status_code not in (401, 403, 404), r.text


def test_cross_session_csrf_token_rejected(two_org_fixture):
    """A token that's real -- just bound to a DIFFERENT session (the
    outsider's, not member's) -- must not validate against member's
    session. Proves the check is genuinely session-bound, not merely
    "is this any token this server ever issued"."""
    f = two_org_fixture
    _member_csrf_token(f)  # binds a real token to member's own session first
    outsider_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', f["outsider"].get("/account").text).group(1)
    r = f["member"].post(f"/api/tasks/{f['tid_a']}/select", headers={"X-CSRF-Token": outsider_token})
    assert r.status_code == 403
    assert r.json()["detail"] == "CSRF_TOKEN_INVALID"


def test_bearer_api_token_needs_no_csrf_at_all(two_org_fixture):
    """ADR-003's own structural-immunity reasoning: a Bearer/API-token
    request carries no ambient browser credential, so it is never
    CSRF-guarded -- this is the general-sweep counterpart to
    test_b03_authz's own Bearer-bypass-of-AuthZ proof, this time for
    CSRF specifically."""
    f = two_org_fixture
    member_uid = f["db"].one("SELECT id FROM users WHERE email='member@example.com'")["id"]
    raw_token = f["owner"].app.state.auth_service.create_api_token(member_uid, "ci-token")[1]
    bare = TestClient(f["owner"].app)
    r = bare.post(f"/api/tasks/{f['tid_a']}/select", headers={"Authorization": f"Bearer {raw_token}"})
    assert r.status_code not in (401, 403, 404), r.text  # no csrf_token supplied at all, still allowed


def test_manual_create_route_also_csrf_guarded(two_org_fixture):
    """The 12 body-based `create` routes get the CSRF check as a plain
    Depends() (not folded into require_role, since they have no path-id
    to key a require_role() dependency on) -- proven independently here
    so the general sweep claim covers all 143, not just the 131
    path-param ones."""
    f = two_org_fixture
    r_no_token = f["member"].post("/api/tasks", data={"title": "x", "repo_scope_id": str(f["rid_a"])})
    assert r_no_token.status_code == 403
    token = _member_csrf_token(f)
    r_ok = f["member"].post("/api/tasks", data={"title": "x", "repo_scope_id": str(f["rid_a"]), "csrf_token": token})
    assert r_ok.status_code not in (401, 403, 404), r_ok.text


# ================================================================ AUTH_MODE=none: zero new surface, zero regression
def test_auth_mode_none_unaffected(client, git_repo):
    root, repo = git_repo
    rid = client.app.state.db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("demo", str(repo)))
    tid = client.app.state.db.execute(
        "INSERT INTO tasks(slug,title,status,repo_scope_id) VALUES(?,?,?,?)", ("t1", "T", "BACKLOG", rid))
    # No csrf_token supplied at all -- must behave exactly as before B0.4
    # (still a real 200/303/whatever the business route itself returns,
    # never a CSRF rejection, since require_role/require_csrf both no-op
    # entirely under AUTH_MODE=none).
    r = client.post(f"/api/tasks/{tid}/select")
    assert r.status_code not in (401, 403, 404), r.text


# ================================================================ Login CSRF (the /auth/verify gap)
def test_login_csrf_forged_post_without_token_is_rejected(git_repo, tmp_path, captured_logs):
    """The attack: attacker requests a real magic link for THEIR OWN
    account, then tries to force the VICTIM's browser to POST it to
    /auth/verify (e.g. via a hidden auto-submitting cross-site form) so
    the victim ends up silently logged into the attacker's account.
    The victim's own browser/session has no way to know the CSRF token
    the attacker would need to supply -- unguessable, unreadable
    cross-origin -- so the forged POST must be rejected before the
    token is ever consumed."""
    root, _ = git_repo
    app_client = auth_client(root, tmp_path)
    _bootstrap(app_client, captured_logs, "victim.owner@example.com")
    # B0.1 has no open self-registration (AuthService.request_login's own
    # docstring) -- the attacker must already be a real registered user
    # before they can request their own real magic link, same as any
    # other org member (an invitation-accept is the only user-creation
    # path here).
    org_id = _create_org(app_client, "Attacker Org")
    attacker_invite = _invite(app_client, org_id, "attacker@example.com", "OWNER", captured_logs)
    _accept(app_client.app, attacker_invite)

    # Attacker requests their own real magic link.
    captured_logs.clear()
    app_client.post("/auth/login", data={"email": "attacker@example.com"})
    email_log = next(m for m in captured_logs if "would send to attacker@example.com" in m)
    attacker_token = re.search(r"token=(\S+)", email_log).group(1)

    # The victim's browser, forced into a cross-site POST -- a fresh
    # session that has never seen this app's csrf token at all.
    victim = TestClient(app_client.app)
    r = victim.post("/auth/verify", data={"token": attacker_token})
    assert r.status_code == 403
    # And the token itself must still be unconsumed -- a rejected forged
    # attempt must not burn the attacker's own one-time login token
    # (rendering it a livable, reusable griefing vector against the
    # attacker's own future real login, but more importantly proving the
    # dependency really did block BEFORE the route body ran).
    row = app_client.app.state.db.one(
        "SELECT used_at FROM login_tokens WHERE token_hash IS NOT NULL ORDER BY id DESC LIMIT 1")
    assert row["used_at"] is None


def test_login_csrf_with_correct_token_from_peek_page_succeeds(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    app_client = auth_client(root, tmp_path)
    _bootstrap(app_client, captured_logs, "owner@example.com")
    captured_logs.clear()
    app_client.post("/auth/login", data={"email": "owner@example.com"})
    email_log = next(m for m in captured_logs if "would send to owner@example.com" in m)
    token = re.search(r"token=(\S+)", email_log).group(1)

    fresh = TestClient(app_client.app)
    peek = fresh.get(f"/auth/verify?token={token}")
    assert peek.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', peek.text).group(1)

    confirm = fresh.post("/auth/verify", data={"token": token, "csrf_token": csrf}, follow_redirects=False)
    assert confirm.status_code == 303
    assert confirm.headers["location"] == "/account"


def test_auth_verify_missing_token_still_404_under_none(client):
    assert client.post("/auth/verify", data={"token": "x"}).status_code == 404
