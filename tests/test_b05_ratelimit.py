"""B0.5 -- Rate limiting (docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md,
ADR-003's resolved design). Real, end-to-end evidence: real slowapi
Limiter (`app.state.limiter`, constructed in B0.1), a real in-memory
`limits` storage backend -- no mocking of the limiter itself, only
`time.time()` is patched (via pytest's own `monkeypatch`) to prove
window recovery without a real 60-second sleep.

Scope, per this program's own explicit B0.5 authorization: the named
abuse-sensitive auth/magic-link/bootstrap/invite/org/token routes, not
a blanket sweep across all 143 mutating routes (that general
middleware rollout stays unscoped future work, flagged explicitly
below) -- `/auth/login` (5/min) and `/auth/bootstrap` (5/min) shipped
with B0.1 already; this phase adds `/auth/verify` (10/min), `/orgs`
create (10/min), `/orgs/{id}/invite` (20/min), `/orgs/invitations/
{token}` accept (10/min), and `/account/api-tokens` create (20/min).

These tests exercise the rate-limit mechanism itself, decoupled from
login-flow correctness (already covered by test_b01_authn.py/
test_b02_organizations.py) -- a CSRF token is bound via the always-
reachable `/auth/login` GET page (no login required to view it), and
"allowed" is `status_code != 429` (a real 303-to-login redirect for a
not-yet-authenticated caller is itself a legitimate distinguishing
non-429 response the limiter must let through) rather than a specific
success code.

AUTH_MODE=none is the default and MUST stay completely unaffected --
every test in this file constructing a `none`-mode client is proving
exactly that, not merely assuming it."""
from __future__ import annotations
import re
import time as time_module

import pytest
from fastapi.testclient import TestClient

from tests.test_b03_authz import auth_client, captured_logs, _bootstrap


def _csrf_via_login_page(client) -> str:
    """/auth/login is reachable with no session/login at all -- the one
    page every one of this file's fake-IP clients can always mint a
    real, session-bound CSRF token from before making a POST."""
    return re.search(r'_TOKEN = "([^"]+)"', client.get("/auth/login").text).group(1)


# ================================================================ Normal traffic / threshold / recovery
def test_normal_traffic_under_threshold_all_succeed(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app, client=("10.0.0.1", 1))
    for i in range(3):  # well under /orgs's own 10/minute
        token = _csrf_via_login_page(fresh)
        r = fresh.post("/orgs", data={"name": f"Org {i}", "csrf_token": token}, follow_redirects=False)
        assert r.status_code != 429, r.text


def test_threshold_exceeded_is_429(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app, client=("10.0.0.2", 1))
    statuses = []
    for i in range(12):  # /orgs is 10/minute
        token = _csrf_via_login_page(fresh)
        statuses.append(fresh.post("/orgs", data={"name": f"Org {i}", "csrf_token": token}).status_code)
    assert 429 in statuses, statuses


def test_recovery_after_window_elapses(git_repo, tmp_path, captured_logs, monkeypatch):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app, client=("10.0.0.3", 1))

    real_time = time_module.time
    fake_now = real_time()
    monkeypatch.setattr(time_module, "time", lambda: fake_now)

    for i in range(10):
        token = _csrf_via_login_page(fresh)
        assert fresh.post("/orgs", data={"name": f"Org {i}", "csrf_token": token}).status_code != 429
    token = _csrf_via_login_page(fresh)
    assert fresh.post("/orgs", data={"name": "Org over", "csrf_token": token}).status_code == 429

    # Advance the clock past the 1-minute window -- the exact same
    # actor, same route, must be allowed again (a real reset, not
    # merely "some other route still works").
    monkeypatch.setattr(time_module, "time", lambda: fake_now + 61)
    token = _csrf_via_login_page(fresh)
    r_recovered = fresh.post("/orgs", data={"name": "Org after recovery", "csrf_token": token})
    assert r_recovered.status_code != 429, r_recovered.text


# ================================================================ Actor independence
def test_actor_independence_different_ips_have_separate_limits(git_repo, tmp_path, captured_logs):
    """Proves the limiter key is genuinely per-actor (IP), not global --
    exhausting one IP's bucket must never block a different IP."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    actor_a = TestClient(c.app, client=("10.0.1.1", 1))
    actor_b = TestClient(c.app, client=("10.0.1.2", 1))

    for i in range(10):
        token = _csrf_via_login_page(actor_a)
        assert actor_a.post("/orgs", data={"name": f"A Org {i}", "csrf_token": token}).status_code != 429
    token = _csrf_via_login_page(actor_a)
    assert actor_a.post("/orgs", data={"name": "A over", "csrf_token": token}).status_code == 429

    # actor_b, a different IP, is completely unaffected by actor_a's
    # exhausted bucket.
    token_b = _csrf_via_login_page(actor_b)
    r_b = actor_b.post("/orgs", data={"name": "B Org", "csrf_token": token_b})
    assert r_b.status_code != 429, r_b.text


# ================================================================ Equivalent-endpoint bypass
def test_login_limit_not_bypassed_by_varying_submitted_email(git_repo, tmp_path, captured_logs):
    """The limiter key is the actor (IP), never a request parameter --
    varying the submitted email must not reset or bypass the per-IP
    bucket (this is /auth/login's own B0.1 limit; re-verified here as
    the "equivalent-endpoint"/parameter-variation bypass proof this
    phase's own test requirements call for)."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app, client=("10.0.2.1", 1))
    statuses = [fresh.post("/auth/login", data={"email": f"distinct{i}@example.com"}).status_code
                for i in range(8)]
    assert 429 in statuses, statuses


def test_orgs_create_and_invite_have_independent_buckets(git_repo, tmp_path, captured_logs):
    """Two DIFFERENT named routes (org create vs. org invite) are not
    accidentally sharing one combined bucket -- exhausting one must not
    silently exhaust the other (the "equivalent endpoint" a naive
    single-global-key implementation could conflate). Uses the default-
    IP `c` client throughout so a real logged-in OWNER can actually
    reach the invite action (which needs real org membership, unlike
    the anonymous-reachable create-org checks above)."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/orgs/new").text).group(1)
    r = c.post("/orgs", data={"name": "Org", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    org_id = int(r.headers["location"].rsplit("/", 1)[-1])

    statuses = []
    for i in range(11):  # /orgs create is 10/minute -- exhaust it (org creation itself, not invite)
        csrf_i = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/orgs/new").text).group(1)
        statuses.append(c.post("/orgs", data={"name": f"Extra {i}", "csrf_token": csrf_i}).status_code)
    assert 429 in statuses, statuses

    # /orgs/{id}/invite is a SEPARATE 20/minute bucket -- still open.
    csrf2 = re.search(r'name="csrf_token" value="([^"]+)"', c.get(f"/orgs/{org_id}").text).group(1)
    r2 = c.post(f"/orgs/{org_id}/invite", data={"email": "teammate@example.com", "role": "MEMBER",
                                                 "csrf_token": csrf2}, follow_redirects=False)
    assert r2.status_code == 303, r2.text


# ================================================================ AUTH_MODE=none: zero new surface, zero regression
def test_auth_mode_none_unaffected(client):
    """Every newly-limited route still 404s under AUTH_MODE=none exactly
    as before -- a single call each (not exhausting any bucket) is
    enough to prove the route's own auth_mode gate still wins."""
    assert client.post("/auth/verify", data={"token": "x"}).status_code == 404
    assert client.post("/orgs", data={"name": "x"}).status_code == 404
    assert client.post("/orgs/1/invite", data={"email": "a@b.com", "role": "MEMBER"}).status_code == 404
    assert client.post("/orgs/invitations/x").status_code == 404
    assert client.post("/account/api-tokens", data={"name": "x"}).status_code == 404


# ================================================================ Proxy-header trust
def test_limiter_key_ignores_x_forwarded_for_header(git_repo, tmp_path, captured_logs):
    """ADR-003's own flagged proxy-topology residual risk: this
    deployment has no reverse-proxy config today, so the limiter must
    key off the real ASGI peer address, never a client-supplied
    X-Forwarded-For header an attacker could freely spoof to reset
    their own bucket on every request."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app, client=("10.0.4.1", 1))
    statuses = []
    for i in range(11):
        token = _csrf_via_login_page(fresh)
        statuses.append(fresh.post(
            "/orgs", data={"name": f"Org {i}", "csrf_token": token},
            headers={"X-Forwarded-For": f"1.2.3.{i}"},  # a different spoofed IP on every request
        ).status_code)
    # If the spoofed header were trusted, every request would look like a
    # brand-new actor and never hit the real 10/minute ceiling.
    assert 429 in statuses, statuses
