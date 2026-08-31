"""B0.1 -- AuthN foundation (docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md,
ADR-002's resolved design). Real, end-to-end evidence: real SQLite,
real hashed tokens, real session cookies (Starlette's own
SessionMiddleware), real slowapi rate limiting -- only the SMTP
transport itself is faked (EmailSenderService's own log-fallback path,
exercised here via log capture, is real code, not a mock of this
module's own logic).

AUTH_MODE=none is the default and MUST stay completely unaffected --
every test in this file that constructs a `none`-mode client is
proving exactly that, not merely assuming it."""
from __future__ import annotations
import hashlib
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


def _bootstrap(client, logs, email="admin@example.com"):
    token = _bootstrap_token(logs)
    r = client.post("/auth/bootstrap", data={"token": token, "email": email})
    assert r.status_code == 200 or r.status_code == 303
    return token


# ================================================================ AUTH_MODE=none: zero new surface, zero regression
def test_auth_mode_none_new_routes_all_404(client):
    """`client` (conftest's own default fixture) is AUTH_MODE=none --
    every B0.1 route must not exist at all, not merely reject."""
    for path in ("/auth/login", "/auth/verify?token=x", "/auth/bootstrap", "/account"):
        assert client.get(path).status_code == 404, path


def test_auth_mode_none_no_session_middleware_no_bootstrap_token(client):
    assert client.app.state.bootstrap_token_hash is None
    # Direct, unambiguous evidence: no middleware of the Session kind was
    # ever added to the app's own middleware stack.
    assert not any("SessionMiddleware" in repr(m.cls) for m in client.app.user_middleware)


def test_auth_mode_none_whoami_honest_unauthenticated(client):
    r = client.get("/api/whoami")
    assert r.status_code == 200
    assert r.json() == {"auth_mode": "none", "authenticated": False, "email": None}


def test_auth_mode_none_existing_pages_unaffected(client, git_repo):
    """Real existing routes (Track A1's own /changes, plus dashboard/
    tasks) render exactly as before -- B0.1 adds no gate to any of them."""
    for path in ("/", "/changes", "/tasks"):
        assert client.get(path).status_code == 200, path


# ================================================================ AUTH_MODE=required: startup safety
def test_required_mode_refuses_to_start_without_session_secret(git_repo, tmp_path):
    root, _ = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state",
                         auth_mode="required")
    with pytest.raises(RuntimeError, match="REFUSED"):
        create_app(settings)


def test_unrecognized_auth_mode_env_value_fails_closed_to_none(monkeypatch):
    """app/config.py's own load_settings(): anything other than the
    literal string "required" stays "none" -- never fails open."""
    monkeypatch.setenv("WORKSPACE_MANAGER_AUTH_MODE", "yes-please")
    from app.config import load_settings
    assert load_settings().auth_mode == "none"


# ================================================================ First-user bootstrap
def test_bootstrap_creates_user_and_session(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    token = _bootstrap_token(captured_logs)
    r = c.post("/auth/bootstrap", data={"token": token, "email": "Admin@Example.com"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert c.get("/api/whoami").json() == {"auth_mode": "required", "authenticated": True, "email": "admin@example.com"}


def test_bootstrap_rejects_wrong_token(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap_token(captured_logs)  # consumed just to confirm one exists
    r = c.post("/auth/bootstrap", data={"token": "totally-wrong", "email": "a@example.com"})
    assert "Invalid setup token" in r.text
    assert c.get("/api/whoami").json()["authenticated"] is False


def test_bootstrap_closes_after_first_user(git_repo, tmp_path, captured_logs):
    """Defense in depth, tested at both layers: the route itself 404s
    once used (test_bootstrap_route_404_once_no_bootstrap_token_pending,
    below) AND AuthService.bootstrap() independently refuses once a
    user exists, even if called with the exact original token/hash --
    never relying on the route-level guard alone."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    token = _bootstrap_token(captured_logs)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    c.post("/auth/bootstrap", data={"token": token, "email": "first@example.com"})
    auth_service = c.app.state.auth_service
    assert auth_service.user_count() == 1
    from app.services.auth_service import AuthError
    with pytest.raises(AuthError) as exc_info:
        auth_service.bootstrap(token, token_hash, "second@example.com")
    assert exc_info.value.code == "BOOTSTRAP_CLOSED"
    assert auth_service.user_count() == 1


def test_bootstrap_route_404_once_no_bootstrap_token_pending(git_repo, tmp_path, captured_logs):
    """After the first user exists, /auth/bootstrap itself 404s (the
    guard checks app.state.bootstrap_token_hash, set only while zero
    users exist) -- not just the POST action, the page too."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    token = _bootstrap_token(captured_logs)
    c.post("/auth/bootstrap", data={"token": token, "email": "first@example.com"})
    assert c.get("/auth/bootstrap").status_code == 404


# ================================================================ Magic-link login flow
def test_login_link_request_verify_flow(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app)  # no session cookie -- a different "browser"
    captured_logs.clear()
    r = fresh.post("/auth/login", data={"email": "owner@example.com"})
    assert r.status_code == 200 and "Check your email" in r.text
    email_log = next(m for m in captured_logs if "would send to" in m)
    token = re.search(r"token=(\S+)", email_log).group(1)

    # GET peeks (shows the real email, never auto-authenticates)
    peek = fresh.get(f"/auth/verify?token={token}")
    assert peek.status_code == 200 and "owner@example.com" in peek.text
    assert fresh.get("/api/whoami").json()["authenticated"] is False  # GET alone never establishes a session
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', peek.text).group(1)

    confirm = fresh.post("/auth/verify", data={"token": token, "csrf_token": csrf}, follow_redirects=False)
    assert confirm.status_code == 303 and confirm.headers["location"] == "/account"
    assert fresh.get("/api/whoami").json() == {"auth_mode": "required", "authenticated": True, "email": "owner@example.com"}


def test_login_token_is_single_use(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app)
    captured_logs.clear()
    fresh.post("/auth/login", data={"email": "owner@example.com"})
    token = re.search(r"token=(\S+)", next(m for m in captured_logs if "would send to" in m)).group(1)
    first = TestClient(c.app); second = TestClient(c.app)
    first_csrf = re.search(r'name="csrf_token" value="([^"]+)"', first.get(f"/auth/verify?token={token}").text).group(1)
    r1 = first.post("/auth/verify", data={"token": token, "csrf_token": first_csrf}, follow_redirects=False)
    assert r1.status_code == 303
    # The token is already consumed at this point, so peek_login_token()
    # correctly returns invalid=True and no longer renders a csrf_token
    # field at all -- mint second's own session token from any other
    # page instead (CSRF is session-bound, not token-bound, so this is
    # still a real, valid token for second's own session).
    second_csrf = re.search(r'_TOKEN = "([^"]+)"', second.get("/auth/login").text).group(1)
    r2 = second.post("/auth/verify", data={"token": token, "csrf_token": second_csrf})
    assert "invalid or expired" in r2.text.lower() and r2.status_code == 200
    assert second.get("/api/whoami").json()["authenticated"] is False


def test_login_token_hashed_at_rest_never_raw(git_repo, tmp_path, captured_logs):
    """ADR-001's own 'never store the raw secret' discipline, applied
    here: the raw token that goes into the email is never findable
    verbatim in the login_tokens table -- only its SHA-256 hash is."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app)
    captured_logs.clear()
    fresh.post("/auth/login", data={"email": "owner@example.com"})
    token = re.search(r"token=(\S+)", next(m for m in captured_logs if "would send to" in m)).group(1)
    rows = c.app.state.db.all("SELECT * FROM login_tokens")
    assert rows and all(token not in str(v) for row in rows for v in row.values())


def test_login_request_enumeration_mitigation_identical_response(git_repo, tmp_path, captured_logs):
    """ADR-002's own explicit requirement: identical response whether or
    not the email is registered, and no email attempted for an unknown
    address."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh1, fresh2 = TestClient(c.app), TestClient(c.app)
    r_known = fresh1.post("/auth/login", data={"email": "owner@example.com"})
    captured_logs.clear()
    r_unknown = fresh2.post("/auth/login", data={"email": "nobody@example.com"})
    assert r_known.status_code == r_unknown.status_code == 200
    # B0.4: base.html now embeds a real, random, per-SESSION CSRF token
    # on every page (via its own Jinja global) -- two separate sessions
    # legitimately get two different random values regardless of which
    # email was submitted, so byte-for-byte equality must normalize that
    # one dynamic value out first; it carries no signal correlated with
    # email validity (unlike the requirement this test actually
    # verifies), so normalizing it doesn't weaken the assertion.
    normalize = lambda text: re.sub(r'_TOKEN = "[^"]+"', '_TOKEN = "X"', text)
    assert normalize(r_known.text) == normalize(r_unknown.text)
    assert not any("nobody@example.com" in m for m in captured_logs)


def test_login_request_per_email_cooldown(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app)
    fresh.post("/auth/login", data={"email": "owner@example.com"})
    fresh.post("/auth/login", data={"email": "owner@example.com"})  # within cooldown -- must not create a 2nd token
    rows = c.app.state.db.all("SELECT * FROM login_tokens WHERE email='owner@example.com'")
    assert len(rows) == 1


def test_login_request_rate_limited_per_ip(git_repo, tmp_path, captured_logs):
    """ADR-002/ADR-003's own launch-blocking requirement: this cannot
    wait for B0.5's general middleware."""
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    fresh = TestClient(c.app)
    statuses = [fresh.post("/auth/login", data={"email": f"x{i}@example.com"}).status_code for i in range(8)]
    assert 429 in statuses, statuses


def test_verify_garbage_token_handled_gracefully(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs)
    r = TestClient(c.app).get("/auth/verify?token=not-a-real-token")
    assert r.status_code == 200 and "invalid or expired" in r.text.lower()


# ================================================================ API tokens (service/automation accounts)
def test_api_token_create_use_revoke(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    page = c.get("/account")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    created = c.post("/account/api-tokens", data={"name": "CI", "csrf_token": csrf})
    raw_token = re.search(r"<code>(pf_[^<]+)</code>", created.text).group(1)

    bearer_client = TestClient(c.app)
    r = bearer_client.get("/api/whoami", headers={"Authorization": f"Bearer {raw_token}"})
    assert r.json() == {"auth_mode": "required", "authenticated": True, "email": "owner@example.com"}

    page2 = c.get("/account")
    csrf2 = re.search(r'name="csrf_token" value="([^"]+)"', page2.text).group(1)
    token_id = re.search(r"api-tokens/(\d+)/revoke", page2.text).group(1)
    c.post(f"/account/api-tokens/{token_id}/revoke", data={"csrf_token": csrf2})

    r2 = TestClient(c.app).get("/api/whoami", headers={"Authorization": f"Bearer {raw_token}"})
    assert r2.json()["authenticated"] is False


def test_api_token_hashed_at_rest_never_raw(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    page = c.get("/account")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    created = c.post("/account/api-tokens", data={"name": "CI", "csrf_token": csrf})
    raw_token = re.search(r"<code>(pf_[^<]+)</code>", created.text).group(1)
    rows = c.app.state.db.all("SELECT * FROM api_tokens")
    assert rows and all(raw_token not in str(v) for row in rows for v in row.values())


# ================================================================ CSRF on B0.1's own new mutating routes
def test_account_mutation_requires_csrf_token(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    r = c.post("/account/api-tokens", data={"name": "no csrf"})
    assert r.status_code == 403


def test_logout_requires_csrf_token(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    _bootstrap(c, captured_logs, "owner@example.com")
    r = c.post("/auth/logout")
    assert r.status_code == 403
    assert c.get("/api/whoami").json()["authenticated"] is True  # never logged out despite the attempt


# ================================================================ Login gate on B0.1's own new routes
def test_account_redirects_to_login_when_not_authenticated(git_repo, tmp_path):
    root, _ = git_repo
    c = auth_client(root, tmp_path)
    r = c.get("/account", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"


# ================================================================ SMTP transport (real code, fake transport)
def test_email_sender_uses_configured_smtp_when_present():
    from app.services.email_sender import EmailSenderService
    sent = {}
    class FakeSMTP:
        def __init__(self, host, port, timeout=10): sent["connect"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg): sent["message"] = msg
    svc = EmailSenderService(host="smtp.example.invalid", port=587, user="u", password="p",
                              from_addr="noreply@example.invalid", smtp_client_factory=FakeSMTP)
    assert svc.configured is True
    ok = svc.send("to@example.invalid", "Subject", "Body")
    assert ok is True
    assert sent["connect"] == ("smtp.example.invalid", 587)
    assert sent["tls"] is True and sent["login"] == ("u", "p")
    assert sent["message"]["To"] == "to@example.invalid"


def test_email_sender_falls_back_to_log_when_unconfigured(caplog):
    from app.services.email_sender import EmailSenderService
    svc = EmailSenderService(host=None, port=587, user=None, password=None, from_addr="noreply@localhost")
    assert svc.configured is False
    with caplog.at_level("WARNING", logger="projectflow.email"):
        ok = svc.send("to@example.invalid", "Subject", "Body text")
    assert ok is False
    assert "SMTP_NOT_CONFIGURED" in caplog.text
