# B6 — Trusted Reverse-Proxy Support (closing ADR-003's own flagged residual)

**Status: IMPLEMENTED.** The phase after B5 (Tenant Isolation
Completeness, PASS). No `B6` spec existed in the repo before this
document.

## How this scope was chosen

Fresh re-audit, explicitly re-evaluating every named candidate before
picking anything new:

1. **Webhook status into merge/gate decisions** — **still BLOCKED,
   unchanged.** No real GitHub webhook delivery has been observed by
   this session (ordering, retries, at-least-once semantics); there is
   still no local way to prove stale/conflicting data can't wrongly
   authorize a merge. Per the user's own explicit instruction, a
   different independently-valuable scope is chosen instead of
   weakening that safety requirement.
2. **Workspace-identity permanence** — **still not selected.**
   Re-checked `ARCHITECTURE.md`'s own documented workaround against
   current code; nothing has changed since B1-B5 each independently
   declined this for the same reason. No new evidence of insufficiency.
3. **Remaining tenant-isolation/security gaps** — a fresh sweep (not
   re-reading old findings) surfaced a real, previously-undiscussed one
   (see Findings below), **selected as B6's primary scope.**
4. **Remaining `TECHNICAL_DEBT.md` items** — `review_runs` filter,
   `SECURITY_PASS` docstring, UI complexity: all cosmetic, none
   security/tenant-relevant, not selected.

## Findings (grounding, gathered before any code changed)

`app/main.py`'s own `SessionMiddleware` construction carries a comment
that is itself an open admission: `https_only=False` because "this app
has never assumed TLS termination itself... a future hosted
deployment's own reverse proxy is the TLS boundary, per ADR-003's own
flagged-but-unresolved proxy-topology residual risk." A second,
related admission sits in ADR-003 itself (`docs/B0_HOSTED_PLATFORM_
SECURITY_FOUNDATION.md`'s own B0.5 section): rate limiting is keyed by
`get_remote_address` (the raw ASGI peer address), "never a client-
spoofable `X-Forwarded-For`, since this deployment has no reverse
proxy in front of it yet, ADR-003's own flagged residual."

Both are the SAME underlying gap, restated twice: this app has no
concept of "which reverse proxy, if any, do I trust," so it can safely
assume neither TLS termination nor the real client IP once one exists.
Track B (hosted) is exactly the deployment shape B0-B5 have been
building toward — a real hosted deployment WILL sit behind a
TLS-terminating reverse proxy, and both gaps become live the moment one
does:

- **Session-cookie theft over plaintext**: `https_only=False` means the
  session cookie is sent even if a client somehow reaches the app over
  plain HTTP (e.g. a misconfigured proxy hop, or a client bypassing the
  proxy's TLS entirely) — a classic session-hijacking vector.
- **Rate-limit bypass / cross-tenant IP misattribution**: behind a
  reverse proxy, EVERY request's ASGI peer address is the proxy's own
  IP, not the real client's — `/auth/login`'s 5/minute limit (and
  every other slowapi-guarded route) becomes a shared budget across
  every real user behind that proxy, trivially exhausted by one abusive
  client to lock out everyone else.

A second, smaller, independently-real finding from this same fresh
sweep: `POST /webhooks/github` (`app/main.py`) reads the full request
body (`await request.body()`) with no size cap, BEFORE the HMAC check
even runs — an unauthenticated caller can POST an arbitrarily large
body and consume memory proportional to it. Included as B6.2.

## Scope

**B6.1 — Trusted reverse-proxy support.** `settings.trusted_proxy_ips`
(new, `WORKSPACE_MANAGER_TRUSTED_PROXY_IPS`, comma-separated IPs/CIDRs,
empty by default — the exact same "REFUSED/off unless explicitly
configured" precedent `session_secret`/`secret_encryption_keys`/
`github_app_id` already established). When non-empty, `create_app()`
installs uvicorn's own `ProxyHeadersMiddleware` (already a direct
dependency, zero new package — the standard, security-reviewed
mechanism for exactly this problem, not hand-rolled header parsing):
only rewrites `scope["client"]`/`scope["scheme"]` from `X-Forwarded-
For`/`X-Forwarded-Proto` when the DIRECT connecting peer is itself in
the trusted list — a request from anywhere else has those headers
ignored entirely, so an untrusted caller can never spoof its way past
this. Once installed, this ONE mechanism transparently fixes BOTH
residuals with no further code changes: `slowapi`'s `get_remote_
address` reads `scope["client"]`, so rate limiting automatically keys
on the real client IP; `SessionMiddleware`'s own `https_only` flag now
correctly reflects `scope["scheme"]`, so the cookie only needs `Secure`
when the ORIGINAL client connection really was HTTPS — set to `True`
only when `trusted_proxy_ips` is configured (self-hosted's own default
empty config keeps `https_only=False`, today's exact behavior, byte-
for-byte unchanged — plain-HTTP loopback is a real, correct topology
there, not a gap).

**B6.2 — Webhook request body-size cap.** `POST /webhooks/github`
rejects (413) any request whose `Content-Length` exceeds a bounded
limit (1 MiB — GitHub's own real webhook payloads for the three event
types this app ingests are a few KB; 1 MiB is generous headroom, never
unbounded) BEFORE calling `request.body()` — checked from the header
first (cheap, no body read at all for an honest oversized declaration);
a request that lies about `Content-Length` or omits it is still bounded
by reading in capped chunks rather than one unbounded `await request.
body()`.

## Non-goals (explicit)

- Actually deploying a reverse proxy, or auto-detecting one — this is
  configuration surface only; a real hosted operator supplies their own
  proxy's IP(s) via the new env var, exactly like every other B0-B5
  "REFUSED unless explicitly configured" secret/credential.
- Webhook status into merge/gate decisions (still BLOCKED, see above).
- Workspace-identity permanence (re-examined, not selected).
- Trusting `X-Forwarded-Host` or rewriting `request.url` beyond
  `scheme`/`client` — `ProxyHeadersMiddleware` itself doesn't touch
  those, and this phase doesn't extend it to.

## Design principles (carried over, unchanged)

`AUTH_MODE=none` untouched — `trusted_proxy_ips` defaults empty
regardless of `auth_mode`, so no new middleware is installed and no
behavior changes for the permanent self-hosted default (ADR-004).
Fail closed: an unconfigured or misconfigured trusted-proxy list means
NO header trust at all (uvicorn's own `ProxyHeadersMiddleware`
semantics), never a silent fallback to trusting everyone. Reuses an
already-direct dependency (uvicorn ships this middleware already);
no new package.

## Acceptance criteria

1. A request from a TRUSTED peer IP carrying `X-Forwarded-For`/
   `X-Forwarded-Proto` has its real client IP/scheme correctly applied
   — proven with a real ASGI-level ScopeMiddleware test, not a mock.
2. A request from an UNTRUSTED peer carrying the SAME spoofed headers
   has them completely ignored — the real, literal risk this mechanism
   must close, proven adversarially. **Correction made during this
   phase's own test-writing, not assumed correct up front:** the first
   draft tried to prove this via the session cookie's `Secure` flag
   (an untrusted peer's spoofed `X-Forwarded-Proto` shouldn't flip it)
   — wrong premise: `https_only` is a deployment-wide startup-time
   config decision applied to every cookie, not a per-request check of
   the connecting peer, so it can never meaningfully vary by peer
   trust. Re-proven correctly via rate limiting instead (a mechanism
   that genuinely IS per-request): two "different" spoofed client IPs
   from the same untrusted real connection still share one rate-limit
   budget, proving the spoofed header was never honored.
3. With `trusted_proxy_ips` configured, the session cookie's `Secure`
   flag is set; with it left at the default (empty), `https_only`
   stays `False` — `AUTH_MODE=required` self-hosted-behind-nothing
   behavior is unaffected.
4. Rate limiting (a real slowapi-guarded route) keys on the real
   client IP once a trusted proxy is configured, not the proxy's own
   IP — proven with two different simulated real-client IPs behind the
   same trusted proxy peer, hitting independent limits.
5. `POST /webhooks/github` rejects an oversized body (413) before
   reading it in full; a normal-sized, validly-signed payload is
   unaffected — full B3/B4 webhook test suites still pass unchanged.
6. `AUTH_MODE=none` behavior is byte-for-byte unchanged (existing full
   regression suite, self-hosted mode).
7. Full existing regression suite (fast non-real subset) stays green.

## Stop condition

Same as B0-B5: do not begin B7 automatically.
