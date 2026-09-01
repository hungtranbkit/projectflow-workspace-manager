from __future__ import annotations
import base64
import json
import time
import urllib.request
import urllib.error

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

"""B3.1 (docs/B3_GITHUB_APP_INSTALLATION_ARCHITECTURE.md, ADR-001's
own "initial cut" -- see that ADR in docs/B0_HOSTED_PLATFORM_SECURITY_
FOUNDATION.md for the full design this implements a slice of).

Two real, protocol-correct pieces, both fully verifiable with a
self-generated test RSA keypair -- no live GitHub App needed to prove
either is correct:

1. `build_app_jwt()`: GitHub's own App-authentication JWT, RS256 per
   RFC 7518, claims per GitHub's documented spec (`iss`=App ID, `iat`
   backdated for clock-drift tolerance, `exp`<=600s -- GitHub's own
   hard limit). Hand-rolled with `cryptography` (already a B0.7
   dependency for Fernet) rather than adding PyJWT as a new dependency
   -- this codebase's own established pattern (CSRF, rate limiting are
   both hand-rolled protocol primitives too, not reached-for libraries)
   and a JWT is a small enough, well-specified enough format that
   hand-rolling it here is the SAME judgment call, not a new one.

2. `GitHubAppService.mint_installation_token()`: the real `POST
   /app/installations/{id}/access_tokens` exchange -- injectable HTTP
   transport (same DI seam as DeploymentService.http_get), so tests
   prove this against a real, hand-computed HTTP response shape
   without ever making a live network call to api.github.com.

Never stores or caches a minted token -- ADR-001's own "Stored,
app-wide, long-lived: one App private key... Never stored, anywhere:
any per-org or per-user bearer/access token" is the whole point;
every call mints fresh and lets the token expire naturally."""


class GitHubAppError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None, ttl_seconds: int = 540) -> str:
    """RS256 JWT: header.payload.signature, each `.`-joined base64url
    segment. `now` is injectable for deterministic tests; production
    callers never pass it. `iat` is backdated 60s -- GitHub's own
    documented guidance to tolerate clock drift between this host and
    GitHub's servers; `ttl_seconds` defaults to 540 (9 minutes),
    leaving margin under GitHub's hard 600s (10 minute) maximum."""
    now = int(time.time()) if now is None else now
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + ttl_seconds, "iss": str(app_id)}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "." + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")))
    try:
        key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except Exception as exc:
        raise GitHubAppError("APP_KEY_INVALID", f"Configured GitHub App private key is not a valid PEM key: {exc}") from exc
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


def _default_http_post(url: str, jwt: str) -> dict:
    req = urllib.request.Request(url, method="POST", headers={
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise GitHubAppError("INSTALLATION_TOKEN_MINT_FAILED", f"GitHub API returned {exc.code}: {body}") from exc
    except Exception as exc:
        raise GitHubAppError("INSTALLATION_TOKEN_MINT_FAILED", str(exc)) from exc


class GitHubAppService:
    """`http_post` is injectable: (url, jwt) -> dict, same DI pattern as
    DeploymentService.http_get / GitHubMergeService.runner -- tests
    substitute a fake that returns a real GitHub-shaped response dict
    without a live network call; production uses the real urllib
    wrapper above."""

    def __init__(self, app_id: str | None, private_key_pem: str | None, http_post=_default_http_post):
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.http_post = http_post

    def configured(self) -> bool:
        return bool(self.app_id and self.private_key_pem)

    def mint_installation_token(self, installation_id: int) -> tuple[str, str | None]:
        """Returns (token, expires_at). Never caches -- ADR-001's own
        "mint on demand, use once, let it expire" discipline; the
        caller (make_installation_token_runner) uses the token for
        exactly one subprocess call."""
        if not self.configured():
            raise GitHubAppError("APP_NOT_CONFIGURED", "No GitHub App id/private key configured on this deployment.")
        jwt = build_app_jwt(self.app_id, self.private_key_pem)
        data = self.http_post(f"https://api.github.com/app/installations/{installation_id}/access_tokens", jwt)
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise GitHubAppError("INSTALLATION_TOKEN_MINT_FAILED", f"GitHub API response missing a token: {data!r}")
        return token, (data.get("expires_at") if isinstance(data, dict) else None)
