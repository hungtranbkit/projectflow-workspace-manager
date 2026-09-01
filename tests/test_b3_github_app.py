"""B3.1 -- GitHub App Installation Architecture
(docs/B3_GITHUB_APP_INSTALLATION_ARCHITECTURE.md, ADR-001's own
"initial cut"). Real RSA cryptography (a self-generated test keypair,
not a live GitHub App -- see that doc's own Non-goals for why a real
App can't be part of this session's evidence), a real HMAC-verified
webhook route, real cross-org isolation on the admin-only installation
callback.

B3.2 -- health/readiness endpoint, its own small section at the end."""
from __future__ import annotations
import hashlib
import hmac
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tests.test_b03_authz import (
    auth_client, captured_logs, _bootstrap, _csrf, _create_org, _invite, _accept,
    _second_repo, _link_repo, _make_task, two_org_fixture, _bind_csrf, TEST_SECRET_ENCRYPTION_KEY,
)
from app.services.github_app_service import GitHubAppService, GitHubAppError, build_app_jwt
from app.services.github_merge_service import GitHubIntegrationError, make_installation_token_runner


@pytest.fixture(scope="module")
def rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("utf-8")
    return key, pem


# ================================================================ JWT construction (real RS256, no live App)
def test_build_app_jwt_is_verifiable_rs256_with_correct_claims(rsa_keypair):
    key, pem = rsa_keypair
    jwt = build_app_jwt("999", pem, now=1_700_000_000)
    header_b64, payload_b64, sig_b64 = jwt.split(".")

    def unb64(s):
        import base64
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    header = json.loads(unb64(header_b64))
    payload = json.loads(unb64(payload_b64))
    assert header == {"alg": "RS256", "typ": "JWT"}
    assert payload == {"iat": 1_700_000_000 - 60, "exp": 1_700_000_000 + 540, "iss": "999"}
    # Real signature verification against the matching public key --
    # proves the algorithm is actually correct, not just "didn't crash".
    key.public_key().verify(unb64(sig_b64), (header_b64 + "." + payload_b64).encode("ascii"),
                             padding.PKCS1v15(), hashes.SHA256())


def test_build_app_jwt_signature_rejects_wrong_key(rsa_keypair):
    _key, pem = rsa_keypair
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwt = build_app_jwt("999", pem)
    header_b64, payload_b64, sig_b64 = jwt.split(".")
    import base64
    sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    with pytest.raises(Exception):
        other_key.public_key().verify(sig, (header_b64 + "." + payload_b64).encode("ascii"),
                                       padding.PKCS1v15(), hashes.SHA256())


def test_build_app_jwt_rejects_invalid_pem():
    with pytest.raises(GitHubAppError) as exc:
        build_app_jwt("1", "not a real pem")
    assert exc.value.code == "APP_KEY_INVALID"


# ================================================================ GitHubAppService.mint_installation_token
def test_mint_installation_token_success_with_injected_transport(rsa_keypair):
    _key, pem = rsa_keypair
    calls = []

    def fake_http_post(url, jwt):
        calls.append((url, jwt))
        assert url == "https://api.github.com/app/installations/42/access_tokens"
        assert jwt.count(".") == 2  # a real 3-part JWT was built and passed
        return {"token": "ghs_faketoken123", "expires_at": "2099-01-01T00:00:00Z"}

    svc = GitHubAppService("1234", pem, http_post=fake_http_post)
    assert svc.configured()
    token, expires_at = svc.mint_installation_token(42)
    assert token == "ghs_faketoken123"
    assert expires_at == "2099-01-01T00:00:00Z"
    assert len(calls) == 1


def test_mint_installation_token_failure_raises_clean_error(rsa_keypair):
    _key, pem = rsa_keypair

    def failing_http_post(url, jwt):
        raise GitHubAppError("INSTALLATION_TOKEN_MINT_FAILED", "GitHub API returned 404: installation not found")

    svc = GitHubAppService("1234", pem, http_post=failing_http_post)
    with pytest.raises(GitHubAppError) as exc:
        svc.mint_installation_token(42)
    assert exc.value.code == "INSTALLATION_TOKEN_MINT_FAILED"


def test_mint_installation_token_response_missing_token_field(rsa_keypair):
    _key, pem = rsa_keypair
    svc = GitHubAppService("1234", pem, http_post=lambda url, jwt: {"message": "Bad credentials"})
    with pytest.raises(GitHubAppError) as exc:
        svc.mint_installation_token(42)
    assert exc.value.code == "INSTALLATION_TOKEN_MINT_FAILED"


def test_unconfigured_app_service_refuses_to_mint():
    svc = GitHubAppService(None, None)
    assert not svc.configured()
    with pytest.raises(GitHubAppError) as exc:
        svc.mint_installation_token(1)
    assert exc.value.code == "APP_NOT_CONFIGURED"


# ================================================================ make_installation_token_runner
class _FakeDB:
    def __init__(self, repo_org, org_installation):
        self.repo_org = repo_org  # {cwd: organization_id}
        self.org_installation = org_installation  # {org_id: installation_id}

    def one(self, sql, args=()):
        if "FROM repositories" in sql:
            org_id = self.repo_org.get(args[0])
            return {"organization_id": org_id} if org_id is not None else None
        if "FROM organizations" in sql:
            iid = self.org_installation.get(args[0])
            return {"github_installation_id": iid} if iid is not None else None
        return None


def test_installation_token_runner_injects_minted_token(rsa_keypair, tmp_path):
    _key, pem = rsa_keypair
    cwd = str(tmp_path)
    db = _FakeDB(repo_org={cwd: 7}, org_installation={7: 999})
    svc = GitHubAppService("app1", pem, http_post=lambda url, jwt: {"token": "ghs_realish", "expires_at": None})
    runner = make_installation_token_runner(db, svc)

    captured_env = {}

    class _FakeCompleted:
        returncode = 0

    def fake_subprocess_run(argv, cwd, text, capture_output, timeout, env):
        captured_env.update(env)
        return _FakeCompleted()

    import app.services.github_merge_service as gms
    original = gms.subprocess.run
    gms.subprocess.run = fake_subprocess_run
    try:
        runner(["gh", "pr", "list"], tmp_path, timeout=10)
    finally:
        gms.subprocess.run = original
    assert captured_env["GH_TOKEN"] == "ghs_realish"


def test_installation_token_runner_fails_closed_when_no_installation_configured(tmp_path):
    cwd = str(tmp_path)
    db = _FakeDB(repo_org={cwd: 7}, org_installation={})  # org 7 has no installation_id
    svc = GitHubAppService(None, None)
    runner = make_installation_token_runner(db, svc)
    with pytest.raises(GitHubIntegrationError) as exc:
        runner(["gh", "pr", "list"], tmp_path, timeout=10)
    assert exc.value.code == "INSTALLATION_UNAVAILABLE"


def test_installation_token_runner_translates_mint_failure(tmp_path):
    cwd = str(tmp_path)
    db = _FakeDB(repo_org={cwd: 7}, org_installation={7: 999})
    svc = GitHubAppService("app1", "not-a-pem")  # configured() True, but signing will fail
    runner = make_installation_token_runner(db, svc)
    with pytest.raises(GitHubIntegrationError) as exc:
        runner(["gh", "pr", "list"], tmp_path, timeout=10)
    assert exc.value.code == "INSTALLATION_UNAVAILABLE"


# ================================================================ Admin-only callback route (real HTTP, cross-org)
def test_admin_can_set_installation_member_cannot_outsider_gets_404(two_org_fixture):
    f = two_org_fixture
    csrf = _csrf(f["owner"], f"/orgs/{f['org_a']}")
    r = f["owner"].post(f"/orgs/{f['org_a']}/github-installation",
                         data={"installation_id": "555", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303, r.text
    row = f["db"].one("SELECT github_installation_id FROM organizations WHERE id=?", (f["org_a"],))
    assert row["github_installation_id"] == 555

    # _csrf() greps for a hidden `name="csrf_token"` input, which
    # org_detail.html only renders inside manage-role-only forms (the
    # invite/link/unlink forms) -- a MEMBER's own page render has none
    # of those, only base.html's script-embedded token remains (see
    # _bind_csrf()'s own docstring), so _bind_csrf() is the right helper
    # here, not _csrf().
    member_csrf = _bind_csrf(f["member"])
    denied = f["member"].post(f"/orgs/{f['org_a']}/github-installation",
                               data={"installation_id": "111", "csrf_token": member_csrf}, follow_redirects=False)
    assert denied.status_code == 403, denied.text

    outsider_csrf = _bind_csrf(f["outsider"])
    hidden = f["outsider"].post(f"/orgs/{f['org_a']}/github-installation",
                                 data={"installation_id": "111", "csrf_token": outsider_csrf}, follow_redirects=False)
    assert hidden.status_code == 404, hidden.text


def test_installation_id_must_be_a_positive_integer(two_org_fixture):
    f = two_org_fixture
    csrf = _csrf(f["owner"], f"/orgs/{f['org_a']}")
    r = f["owner"].post(f"/orgs/{f['org_a']}/github-installation",
                         data={"installation_id": "not-a-number", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 422, r.text


# ================================================================ Webhook (real HMAC, no live GitHub)
def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_webhook_valid_signature_clears_installation(git_repo, tmp_path):
    root, _repo = git_repo
    client = auth_client(root, tmp_path, github_webhook_secret="whsec_test123")
    db = client.app.state.db
    org_id = db.execute("INSERT INTO organizations(name,slug,github_installation_id) VALUES(?,?,?)",
                         ("Webhook Org", "webhook-org", 777))
    body = json.dumps({"action": "deleted", "installation": {"id": 777}}).encode("utf-8")
    r = client.post("/webhooks/github", content=body,
                     headers={"X-Hub-Signature-256": _sign("whsec_test123", body), "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    row = db.one("SELECT github_installation_id FROM organizations WHERE id=?", (org_id,))
    assert row["github_installation_id"] is None


def test_webhook_invalid_signature_rejected_never_parsed(git_repo, tmp_path):
    root, _repo = git_repo
    client = auth_client(root, tmp_path, github_webhook_secret="whsec_test123")
    db = client.app.state.db
    org_id = db.execute("INSERT INTO organizations(name,slug,github_installation_id) VALUES(?,?,?)",
                         ("Webhook Org 2", "webhook-org-2", 778))
    body = json.dumps({"action": "deleted", "installation": {"id": 778}}).encode("utf-8")
    r = client.post("/webhooks/github", content=body,
                     headers={"X-Hub-Signature-256": "sha256=" + "0" * 64, "Content-Type": "application/json"})
    assert r.status_code == 401, r.text
    row = db.one("SELECT github_installation_id FROM organizations WHERE id=?", (org_id,))
    assert row["github_installation_id"] == 778  # untouched -- the bad signature never reached parsing/handling


def test_webhook_missing_signature_rejected(git_repo, tmp_path):
    root, _repo = git_repo
    client = auth_client(root, tmp_path, github_webhook_secret="whsec_test123")
    body = json.dumps({"action": "deleted", "installation": {"id": 1}}).encode("utf-8")
    r = client.post("/webhooks/github", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 401, r.text


def test_webhook_ignores_non_deletion_events(git_repo, tmp_path):
    root, _repo = git_repo
    client = auth_client(root, tmp_path, github_webhook_secret="whsec_test123")
    db = client.app.state.db
    org_id = db.execute("INSERT INTO organizations(name,slug,github_installation_id) VALUES(?,?,?)",
                         ("Webhook Org 3", "webhook-org-3", 779))
    body = json.dumps({"action": "created", "installation": {"id": 779}}).encode("utf-8")
    r = client.post("/webhooks/github", content=body,
                     headers={"X-Hub-Signature-256": _sign("whsec_test123", body), "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    row = db.one("SELECT github_installation_id FROM organizations WHERE id=?", (org_id,))
    assert row["github_installation_id"] == 779  # a non-deletion event never clears anything


def test_webhook_unconfigured_secret_is_404(client):
    """AUTH_MODE=none / no webhook secret configured -- the plain `client`
    fixture (test_b03's own auth_mode=none convention) has no
    github_webhook_secret at all, so this route stays a clean 404,
    never crashing on a missing secret."""
    body = json.dumps({"action": "deleted", "installation": {"id": 1}}).encode("utf-8")
    r = client.post("/webhooks/github", content=body,
                     headers={"X-Hub-Signature-256": "sha256=" + "0" * 64, "Content-Type": "application/json"})
    assert r.status_code == 404


# ================================================================ AUTH_MODE=none unaffected
def test_auth_mode_none_github_app_service_never_reached(client):
    """The plain `client` fixture is AUTH_MODE=none -- GitHubMergeService
    is constructed with its default direct-`gh`-CLI runner, exactly as
    B0.7 already established; this just re-confirms B3.1's own wiring
    didn't change that."""
    assert client.app.state.github_merge.runner.__name__ in ("_default_runner",)


# ================================================================ B3.2: health endpoint
def test_health_endpoint_ok(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["auth_mode"] == "none"


def test_health_endpoint_no_auth_required_under_auth_mode_required(git_repo, tmp_path):
    root, _repo = git_repo
    client = auth_client(root, tmp_path)
    fresh = client  # no login performed at all
    r = fresh.get("/health")
    assert r.status_code == 200, r.text
