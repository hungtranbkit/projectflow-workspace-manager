"""B6 -- Trusted Reverse-Proxy Support
(docs/B6_TRUSTED_PROXY_SUPPORT.md, closing ADR-003's own flagged
residual).

B6.1: uvicorn's real ProxyHeadersMiddleware, wired in only when
`trusted_proxy_ips` is configured -- proven both at the raw ASGI level
(direct, isolated proof of the trust decision itself) and end-to-end
through the two real mechanisms it fixes (the session cookie's Secure
flag, slowapi's real-client-IP rate-limit keying).

B6.2: a real HTTP request against /webhooks/github with an oversized
body is rejected (413) before being read in full."""
from __future__ import annotations
import asyncio

from tests.conftest import build_client
from app.config import Settings
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

TEST_SECRET_ENCRYPTION_KEY = "M2RXNV3dhIR-lc1WoE8DGxt-kowfK-34xGTIcF1t8m4="


def _proxy_client(root, tmp_path, trusted_proxy_ips=(), **overrides):
    settings = Settings(root, "127.0.0.1", 8765, tmp_path / "test.db", 30, configured_state_dir=tmp_path / "state",
                         auth_mode="required", session_secret="test-only-secret-never-a-default",
                         secret_encryption_keys=(TEST_SECRET_ENCRYPTION_KEY,),
                         trusted_proxy_ips=trusted_proxy_ips, **overrides)
    return build_client(settings)


# ================================================================ B6.1: raw ASGI-level proof (isolated, no FastAPI app)
def _run_through_middleware(trusted_hosts, peer_ip, headers):
    """Directly exercises uvicorn's own ProxyHeadersMiddleware against a
    minimal inline ASGI app that just records the scope it receives --
    the most precise, unmocked proof of the trust decision itself,
    independent of the rest of this application."""
    seen = {}

    async def inner_app(scope, receive, send):
        seen["client"] = scope.get("client")
        seen["scheme"] = scope.get("scheme")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = ProxyHeadersMiddleware(inner_app, trusted_hosts=trusted_hosts)
    scope = {
        "type": "http", "client": (peer_ip, 12345), "scheme": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }

    async def receive():
        return {"type": "http.disconnect"}

    sent = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive, send))
    return seen


def test_trusted_peer_headers_are_honored():
    seen = _run_through_middleware(
        trusted_hosts=["10.0.0.1"], peer_ip="10.0.0.1",
        headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"})
    assert seen["client"][0] == "203.0.113.7"
    assert seen["scheme"] == "https"


def test_untrusted_peer_headers_are_ignored_even_when_identical():
    """The real, literal risk this whole mechanism exists to close: the
    SAME spoofed headers from a peer NOT in the trusted list must be
    completely ignored."""
    seen = _run_through_middleware(
        trusted_hosts=["10.0.0.1"], peer_ip="6.6.6.6",
        headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"})
    assert seen["client"][0] == "6.6.6.6"
    assert seen["scheme"] == "http"


# ================================================================ B6.1: end-to-end -- session cookie Secure flag
def test_https_only_disabled_by_default(git_repo, tmp_path):
    """No trusted_proxy_ips configured -- AUTH_MODE=required's own
    existing behavior, byte-for-byte unchanged."""
    root, _ = git_repo
    client = _proxy_client(root, tmp_path)
    r = client.get("/auth/login")
    assert r.status_code == 200, r.text
    cookies = r.headers.get_list("set-cookie")
    assert cookies, "expected a session cookie to be set"
    assert "secure" not in cookies[0].lower()


def test_https_only_enabled_once_trusted_proxy_configured(git_repo, tmp_path):
    root, _ = git_repo
    client = _proxy_client(root, tmp_path, trusted_proxy_ips=("testclient",))
    # TestClient's own default ASGI peer is ("testclient", 50000) --
    # matching that exact host here proves the real request path (not a
    # hand-built scope) goes through ProxyHeadersMiddleware correctly.
    r = client.get("/auth/login", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200, r.text
    cookies = r.headers.get_list("set-cookie")
    assert cookies, "expected a session cookie to be set"
    assert "secure" in cookies[0].lower()


def test_spoofed_forwarded_for_ignored_when_configured_for_a_different_peer(git_repo, tmp_path):
    """trusted_proxy_ips configured for a DIFFERENT peer than the one
    actually connecting -- TestClient's own real peer ("testclient")
    isn't in the trusted list, so an X-Forwarded-For header must still
    be ignored end-to-end: two "different" spoofed client IPs from the
    same untrusted real connection still share ONE rate-limit budget
    (proving the real, unspoofed peer is what's actually keyed on),
    rather than https_only (a deployment-wide config decision, not a
    per-request/per-peer one -- see the two tests above for its own
    correct behavior)."""
    root, _ = git_repo
    client = _proxy_client(root, tmp_path, trusted_proxy_ips=("10.0.0.1",))

    def hit(spoofed_ip):
        return client.post("/auth/login", data={"email": "x@example.com"},
                            headers={"X-Forwarded-For": spoofed_ip})

    for _ in range(5):
        assert hit("203.0.113.10").status_code == 200
    # A "different" spoofed IP does NOT get its own budget -- the
    # header was never honored in the first place (untrusted peer), so
    # every one of these requests was really keyed on the same real
    # ("testclient") address the whole time.
    assert hit("203.0.113.20").status_code == 429


# ================================================================ B6.1: end-to-end -- rate limiting keys on the real client IP
def test_rate_limit_keys_on_real_client_ip_behind_trusted_proxy(git_repo, tmp_path):
    root, _ = git_repo
    client = _proxy_client(root, tmp_path, trusted_proxy_ips=("testclient",))

    def hit(real_ip):
        return client.post("/auth/login", data={"email": "x@example.com"},
                            headers={"X-Forwarded-For": real_ip})

    # Client A exhausts its own 5/minute budget.
    for _ in range(5):
        r = hit("203.0.113.10")
        assert r.status_code == 200, r.text
    sixth = hit("203.0.113.10")
    assert sixth.status_code == 429, sixth.text

    # Client B, a DIFFERENT real IP behind the SAME trusted proxy peer,
    # must have its own independent budget -- proving the real client
    # IP is what's actually being keyed on, not the shared proxy IP.
    r = hit("203.0.113.20")
    assert r.status_code == 200, r.text


# ================================================================ B6.2: webhook body-size cap
def test_webhook_oversized_body_rejected_by_content_length(git_repo, tmp_path):
    client = _proxy_client(git_repo[0], tmp_path, github_webhook_secret="whsec_b6")
    huge = b"0" * (2 * 1024 * 1024)  # 2 MiB, over the 1 MiB cap
    r = client.post("/webhooks/github", content=huge,
                     headers={"X-Hub-Signature-256": "sha256=" + "0" * 64, "X-GitHub-Event": "status",
                               "Content-Type": "application/octet-stream"})
    assert r.status_code == 413, r.text


def test_webhook_normal_sized_body_unaffected(git_repo, tmp_path):
    import hashlib
    import hmac
    import json
    client = _proxy_client(git_repo[0], tmp_path, github_webhook_secret="whsec_b6")
    body = json.dumps({"action": "created", "installation": {"id": 1}}).encode("utf-8")
    sig = "sha256=" + hmac.new(b"whsec_b6", body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/github", content=body,
                     headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "installation",
                               "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
