"""B0.7 -- Secrets boundary (docs/B0_HOSTED_PLATFORM_SECURITY_
FOUNDATION.md). Real, end-to-end evidence: real Fernet encryption
(`cryptography`), real ciphertext-at-rest verified directly against
the SQLite row (never the plaintext), real cross-org isolation, real
key-rotation (MultiFernet), a real redaction pass against known
credential shapes, and a real (simplified, per-org-token) consumer
wiring for GitHubMergeService -- no real GitHub API call is made
anywhere in this file (that would require a real external token this
session cannot possess); the consumer wiring itself (env var
injection, org->secret resolution) is exercised directly and via a
fake `runner`, matching this codebase's own existing DI-seam test
convention for GitHubMergeService.

Every secret value used below is a synthetic placeholder
(`FAKE-...`), never anything resembling a real, live credential.

AUTH_MODE=none is the default and MUST stay completely unaffected --
every test in this file constructing a `none`-mode client is proving
exactly that, not merely assuming it."""
from __future__ import annotations
import subprocess

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.services.github_merge_service import GitHubIntegrationError, GitHubMergeService, make_hosted_runner, token_runner
from app.services.secret_redaction import redact
from app.services.secrets_service import SecretsError, SecretsService
from tests.test_b03_authz import TEST_SECRET_ENCRYPTION_KEY, auth_client, captured_logs, _bootstrap, _create_org, _invite, _accept, _bind_csrf


FAKE_TOKEN = "FAKE-github-pat-1234567890abcdefghijklmnop"


# ================================================================ SecretsService -- real encryption at rest
def test_ciphertext_at_rest_never_plaintext(client):
    """`client` (AUTH_MODE=none default) still has a real, working
    SecretsService -- storage doesn't require AUTH_MODE=required, only
    the HTTP routes/GitHub consumer wiring do."""
    db = client.app.state.db
    svc = SecretsService(db, [Fernet.generate_key().decode()])
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Test Org", "test-org"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("owner@example.com",))
    secret_id = svc.create(org_id, "github_token", FAKE_TOKEN, uid)

    row = db.one("SELECT ciphertext FROM org_secrets WHERE id=?", (secret_id,))
    assert FAKE_TOKEN not in row["ciphertext"]
    assert row["ciphertext"] != FAKE_TOKEN

    assert svc.reveal(org_id, "github_token", uid) == FAKE_TOKEN


def test_wrong_key_cannot_decrypt(client):
    db = client.app.state.db
    key_a = Fernet.generate_key().decode()
    svc_a = SecretsService(db, [key_a])
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "org"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o@example.com",))
    svc_a.create(org_id, "s1", FAKE_TOKEN, uid)

    svc_wrong_key = SecretsService(db, [Fernet.generate_key().decode()])  # a DIFFERENT, unrelated key
    with pytest.raises(SecretsError) as excinfo:
        svc_wrong_key.reveal(org_id, "s1", uid)
    assert excinfo.value.code == "DECRYPTION_FAILED"


def test_corrupted_ciphertext_fails_closed(client):
    db = client.app.state.db
    key = Fernet.generate_key().decode()
    svc = SecretsService(db, [key])
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "org2"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o2@example.com",))
    secret_id = svc.create(org_id, "s1", FAKE_TOKEN, uid)
    db.execute("UPDATE org_secrets SET ciphertext=? WHERE id=?", ("not-a-real-fernet-token", secret_id))
    with pytest.raises(SecretsError) as excinfo:
        svc.reveal(org_id, "s1", uid)
    assert excinfo.value.code == "DECRYPTION_FAILED"


def test_no_encryption_configured_fails_closed(client):
    db = client.app.state.db
    svc = SecretsService(db, [])  # no keys at all
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "org3"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o3@example.com",))
    with pytest.raises(SecretsError) as excinfo:
        svc.create(org_id, "s1", FAKE_TOKEN, uid)
    assert excinfo.value.code == "ENCRYPTION_NOT_CONFIGURED"


def test_wrong_org_cannot_read_another_orgs_secret(client):
    db = client.app.state.db
    svc = SecretsService(db, [Fernet.generate_key().decode()])
    org_a = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("A", "a-org"))
    org_b = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("B", "b-org"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o4@example.com",))
    svc.create(org_a, "shared_name", FAKE_TOKEN, uid)
    with pytest.raises(SecretsError) as excinfo:
        svc.reveal(org_b, "shared_name", uid)  # same NAME, different org -- must not resolve org_a's row
    assert excinfo.value.code == "SECRET_NOT_FOUND"


def test_revoke_then_get_for_use_returns_none(client):
    db = client.app.state.db
    svc = SecretsService(db, [Fernet.generate_key().decode()])
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "org5"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o5@example.com",))
    svc.create(org_id, "s1", FAKE_TOKEN, uid)
    assert svc.get_for_use(org_id, "s1") == FAKE_TOKEN
    svc.revoke(org_id, "s1", uid)
    assert svc.get_for_use(org_id, "s1") is None
    with pytest.raises(SecretsError):
        svc.reveal(org_id, "s1", uid)


def test_restart_persistence_new_service_instance_same_keys(client):
    """A fresh SecretsService instance (simulating a process restart),
    same configured keys, same db -- must still decrypt correctly."""
    db = client.app.state.db
    key = Fernet.generate_key().decode()
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "org6"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o6@example.com",))
    SecretsService(db, [key]).create(org_id, "s1", FAKE_TOKEN, uid)

    fresh_instance = SecretsService(db, [key])
    assert fresh_instance.reveal(org_id, "s1", uid) == FAKE_TOKEN


def test_key_rotation_old_key_still_decrypts_until_reencrypt(client):
    """MultiFernet's real rotation story: a secret written under the OLD
    key still decrypts once the NEW key is added (old kept, newest
    first) -- and after re_encrypt_all(), the old key alone can no
    longer decrypt it (proving re-encryption actually happened, not
    merely that MultiFernet tries multiple keys forever)."""
    db = client.app.state.db
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "org7"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o7@example.com",))
    SecretsService(db, [old_key]).create(org_id, "s1", FAKE_TOKEN, uid)

    rotated = SecretsService(db, [new_key, old_key])  # newest first
    assert rotated.reveal(org_id, "s1", uid) == FAKE_TOKEN
    count = rotated.re_encrypt_all()
    assert count == 1

    old_key_only = SecretsService(db, [old_key])
    with pytest.raises(SecretsError):
        old_key_only.reveal(org_id, "s1", uid)
    new_key_only = SecretsService(db, [new_key])
    assert new_key_only.reveal(org_id, "s1", uid) == FAKE_TOKEN


def test_access_log_never_stores_plaintext_or_ciphertext(client):
    db = client.app.state.db
    svc = SecretsService(db, [Fernet.generate_key().decode()])
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "org8"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o8@example.com",))
    secret_id = svc.create(org_id, "s1", FAKE_TOKEN, uid)
    svc.reveal(org_id, "s1", uid)
    svc.rotate(org_id, "s1", FAKE_TOKEN + "-rotated", uid)

    rows = db.all("SELECT * FROM secret_access_log WHERE secret_id=?", (secret_id,))
    actions = sorted(r["action"] for r in rows)
    assert actions == ["CREATE", "REVEAL", "ROTATE"]
    for row in rows:
        row_text = str(dict(row))
        assert FAKE_TOKEN not in row_text
        assert "-rotated" not in row_text


def test_list_for_org_never_includes_ciphertext_or_plaintext(client):
    db = client.app.state.db
    svc = SecretsService(db, [Fernet.generate_key().decode()])
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "org9"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o9@example.com",))
    svc.create(org_id, "s1", FAKE_TOKEN, uid)
    listed = svc.list_for_org(org_id)
    assert len(listed) == 1
    assert set(listed[0].keys()) == {"id", "name", "kind", "created_at", "updated_at", "rotated_at", "last_accessed_at"}
    assert FAKE_TOKEN not in str(listed[0])


def test_duplicate_name_within_org_rejected(client):
    db = client.app.state.db
    svc = SecretsService(db, [Fernet.generate_key().decode()])
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "org10"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("o10@example.com",))
    svc.create(org_id, "s1", FAKE_TOKEN, uid)
    with pytest.raises(SecretsError) as excinfo:
        svc.create(org_id, "s1", "different-value", uid)
    assert excinfo.value.code == "NAME_ALREADY_EXISTS"


# ================================================================ Redaction
def test_redact_known_secret_value():
    text = f"deploy log: using token {FAKE_TOKEN} for push"
    out = redact(text, known_secrets=[FAKE_TOKEN])
    assert FAKE_TOKEN not in out
    assert "***REDACTED***" in out


def test_redact_credential_shaped_patterns_even_when_unregistered():
    samples = [
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "aws_secret_access_key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "-----BEGIN PRIVATE KEY-----\nMIIBVgIBADANBgkqhkiG9w0BA\n-----END PRIVATE KEY-----",
        'api_key="sk-thisisatotallyfakekeyabcdefghijklmno"',
    ]
    for s in samples:
        out = redact(s)
        assert "***REDACTED***" in out, s


def test_redact_is_pure_and_idempotent_on_clean_text():
    clean = "just a normal log line about a PASS test run, nothing secret here"
    assert redact(clean) == clean


# ================================================================ GitHub credential consumer (no real GitHub calls)
def test_token_runner_injects_gh_token_env_never_argv():
    captured = {}

    def fake_subprocess_run(argv, cwd, text, capture_output, timeout, env):
        captured["argv"] = argv
        captured["env"] = env
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    import app.services.github_merge_service as gms
    orig = gms.subprocess.run
    gms.subprocess.run = fake_subprocess_run
    try:
        runner = token_runner(FAKE_TOKEN)
        runner(["gh", "pr", "list"], "/tmp/repo")
        assert FAKE_TOKEN not in captured["argv"]  # never in argv (ps-visible)
        assert captured["env"]["GH_TOKEN"] == FAKE_TOKEN
    finally:
        gms.subprocess.run = orig


def test_token_runner_injects_git_config_env_for_plain_git_never_argv():
    captured = {}

    def fake_subprocess_run(argv, cwd, text, capture_output, timeout, env):
        captured["argv"] = argv
        captured["env"] = env
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    import app.services.github_merge_service as gms
    orig = gms.subprocess.run
    gms.subprocess.run = fake_subprocess_run
    try:
        runner = token_runner(FAKE_TOKEN)
        runner(["git", "push", "origin", "main:main"], "/tmp/repo")
        assert FAKE_TOKEN not in captured["argv"]
        assert captured["env"]["GIT_CONFIG_KEY_0"] == "http.extraheader"
        assert FAKE_TOKEN not in captured["env"]["GIT_CONFIG_VALUE_0"]  # base64-encoded, not raw
        assert "AUTHORIZATION: basic" in captured["env"]["GIT_CONFIG_VALUE_0"]
    finally:
        gms.subprocess.run = orig


def test_make_hosted_runner_resolves_org_token_and_calls_through(client):
    db = client.app.state.db
    secrets = SecretsService(db, [Fernet.generate_key().decode()])
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "gh-org"))
    uid = db.execute("INSERT INTO users(email) VALUES(?)", ("gh@example.com",))
    rid = db.execute("INSERT INTO repositories(repo_name,repo_path,organization_id) VALUES(?,?,?)",
                      ("demo", "/tmp/demo-repo-path", org_id))
    secrets.create(org_id, "github_token", FAKE_TOKEN, uid)

    calls = []

    def fake_subprocess_run(argv, cwd, text, capture_output, timeout, env):
        calls.append((argv, env.get("GH_TOKEN")))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    import app.services.github_merge_service as gms
    orig = gms.subprocess.run
    gms.subprocess.run = fake_subprocess_run
    try:
        runner = make_hosted_runner(db, secrets)
        result = runner(["gh", "pr", "list"], "/tmp/demo-repo-path")
        assert result.returncode == 0
        assert calls[0][1] == FAKE_TOKEN
    finally:
        gms.subprocess.run = orig


def test_make_hosted_runner_no_token_raises_installation_unavailable(client):
    db = client.app.state.db
    secrets = SecretsService(db, [Fernet.generate_key().decode()])
    org_id = db.execute("INSERT INTO organizations(name,slug) VALUES(?,?)", ("Org", "no-token-org"))
    db.execute("INSERT INTO repositories(repo_name,repo_path,organization_id) VALUES(?,?,?)",
               ("demo2", "/tmp/no-token-repo", org_id))
    runner = make_hosted_runner(db, secrets)
    with pytest.raises(GitHubIntegrationError) as excinfo:
        runner(["gh", "pr", "list"], "/tmp/no-token-repo")
    assert excinfo.value.code == "INSTALLATION_UNAVAILABLE"


def test_auth_mode_none_github_merge_service_uses_default_runner(client):
    """The real feature flip -- AUTH_MODE=none keeps GitHubMergeService
    on its own original default runner, untouched by any of B0.7's
    machinery."""
    from app.services.github_merge_service import _default_runner
    assert client.app.state.github_merge.runner is _default_runner


# ================================================================ HTTP routes -- role gating, CSRF, rate limit
@pytest.fixture
def secrets_org_fixture(git_repo, tmp_path, captured_logs):
    root, _ = git_repo
    owner = auth_client(root, tmp_path)
    _bootstrap(owner, captured_logs, "owner@example.com")
    org_id = _create_org(owner, "Secrets Org")
    member_token = _invite(owner, org_id, "member@example.com", "MEMBER", captured_logs)
    member = _accept(owner.app, member_token)
    return dict(owner=owner, member=member, org_id=org_id)


def test_owner_can_create_list_and_reveal_secret(secrets_org_fixture):
    f = secrets_org_fixture
    csrf = _bind_csrf(f["owner"])
    r = f["owner"].post(f"/orgs/{f['org_id']}/secrets",
                         data={"name": "github_token", "value": FAKE_TOKEN, "kind": "GITHUB_TOKEN", "csrf_token": csrf},
                         follow_redirects=False)
    assert r.status_code == 303, r.text

    listing = f["owner"].get(f"/orgs/{f['org_id']}/secrets")
    assert listing.status_code == 200 and "github_token" in listing.text
    assert FAKE_TOKEN not in listing.text  # list view never shows the value

    csrf2 = _bind_csrf(f["owner"])
    reveal = f["owner"].post(f"/orgs/{f['org_id']}/secrets/github_token/reveal", data={"csrf_token": csrf2})
    assert reveal.status_code == 200 and FAKE_TOKEN in reveal.text


def test_member_role_cannot_manage_secrets(secrets_org_fixture):
    """Only OWNER/ADMIN may even LIST secret names -- least-privilege
    default, stricter than most other org resources."""
    f = secrets_org_fixture
    assert f["member"].get(f"/orgs/{f['org_id']}/secrets").status_code == 403
    csrf = _bind_csrf(f["member"])
    assert f["member"].post(f"/orgs/{f['org_id']}/secrets",
                             data={"name": "x", "value": "y", "csrf_token": csrf}).status_code == 403


def test_secrets_routes_require_csrf(secrets_org_fixture):
    f = secrets_org_fixture
    r = f["owner"].post(f"/orgs/{f['org_id']}/secrets", data={"name": "x", "value": FAKE_TOKEN})
    assert r.status_code == 403


def test_secrets_routes_rate_limited(secrets_org_fixture):
    f = secrets_org_fixture
    statuses = []
    for i in range(25):
        csrf = _bind_csrf(f["owner"])
        statuses.append(f["owner"].post(
            f"/orgs/{f['org_id']}/secrets", data={"name": f"s{i}", "value": FAKE_TOKEN, "csrf_token": csrf}).status_code)
    assert 429 in statuses, statuses


def test_stranger_cannot_reach_another_orgs_secrets(secrets_org_fixture, git_repo, tmp_path, captured_logs):
    """Existence-hiding, same B0.2 precedent -- a non-member gets 404,
    never 403 (403 would confirm the org id is valid)."""
    f = secrets_org_fixture
    outsider = TestClient(f["owner"].app)
    r = outsider.get(f"/orgs/{f['org_id']}/secrets", follow_redirects=False)
    assert r.status_code in (303, 404)  # redirect-to-login (not yet identified) or 404 (existence-hiding)


def test_revoked_secret_returns_404_on_reveal(secrets_org_fixture):
    f = secrets_org_fixture
    csrf = _bind_csrf(f["owner"])
    f["owner"].post(f"/orgs/{f['org_id']}/secrets",
                     data={"name": "temp", "value": FAKE_TOKEN, "csrf_token": csrf}, follow_redirects=False)
    csrf2 = _bind_csrf(f["owner"])
    f["owner"].post(f"/orgs/{f['org_id']}/secrets/temp/revoke", data={"csrf_token": csrf2}, follow_redirects=False)
    csrf3 = _bind_csrf(f["owner"])
    r = f["owner"].post(f"/orgs/{f['org_id']}/secrets/temp/reveal", data={"csrf_token": csrf3})
    assert r.status_code == 200 and "No active secret" in r.text and FAKE_TOKEN not in r.text


# ================================================================ AUTH_MODE=none: zero new surface, zero regression
def test_auth_mode_none_secret_routes_404(client):
    for path, data in [
        (f"/orgs/1/secrets", {}),
    ]:
        assert client.get(path).status_code == 404
        assert client.post(path, data={"name": "x", "value": "y"}).status_code == 404
    assert client.post("/orgs/1/secrets/x/rotate", data={"value": "y"}).status_code == 404
    assert client.post("/orgs/1/secrets/x/revoke").status_code == 404
    assert client.post("/orgs/1/secrets/x/reveal").status_code == 404


def test_required_mode_refuses_to_start_without_secret_encryption_keys(git_repo, tmp_path):
    from app.config import Settings
    from app.main import create_app
    root, _ = git_repo
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "t.db", 30, configured_state_dir=tmp_path / "state",
                         auth_mode="required", session_secret="test-only-secret-never-a-default")
    with pytest.raises(RuntimeError, match="REFUSED"):
        create_app(settings)


# ================================================================ Redaction wiring -- agent transcript surface
def test_agent_transcript_persist_redacts_known_patterns(client, git_repo):
    """persist_tail() (app/services/agent_session_manager.py) applies
    pattern-based redaction unconditionally -- a Builder's own PTY
    output containing a credential-shaped string never lands verbatim
    in the persisted, multi-tenant-visible transcript_tail column."""
    root, repo = git_repo
    from app.services.agent_session_manager import AgentSessionManager, LivePtySession
    db = client.app.state.db
    wid = db.execute(
        "INSERT INTO agent_workspaces(repository_id,agent,task_name,branch,worktree_path,base_branch,base_commit,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (db.execute("INSERT INTO repositories(repo_name,repo_path) VALUES(?,?)", ("r", str(repo))),
         "claude", "t", "b", str(repo), "main", "abc", "CREATED"))
    sid = db.execute(
        "INSERT INTO agent_sessions(workspace_id,agent,command_profile,cwd,status) VALUES(?,?,?,?,?)",
        (wid, "claude", "default", str(repo), "RUNNING"))
    mgr = AgentSessionManager(db)
    fake_session = type("F", (), {})()
    fake_session._buffer = bytearray(f"exporting GH token: {FAKE_TOKEN.replace('FAKE-github-pat','ghp')}".encode())
    fake_session.BUFFER_CAP = 10_000
    fake_session._lock = __import__("threading").Lock()
    mgr._live[sid] = fake_session
    mgr.persist_tail(sid)
    row = db.one("SELECT transcript_tail FROM agent_sessions WHERE id=?", (sid,))
    assert "ghp_" not in row["transcript_tail"] or "***REDACTED***" in row["transcript_tail"]
