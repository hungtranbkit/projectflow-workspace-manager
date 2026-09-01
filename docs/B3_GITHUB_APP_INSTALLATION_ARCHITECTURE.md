# B3 — GitHub App Installation Architecture (ADR-001's initial cut)

**Status: PARTIALLY IMPLEMENTED — the locally-completable subset only.**
The phase after B2 (Release Concurrency & Residual Security, PASS). No
`B3` spec existed in the repo before this document. Source of truth for
the *design* is `docs/B0_HOSTED_PLATFORM_SECURITY_FOUNDATION.md`'s own
**ADR-001** (Status: DECIDED, design only — not implemented, written at
B0 time) — this document does not re-derive that design, it implements
the slice of it that doesn't need a real, externally-registered GitHub
App to build and verify.

## How this scope was chosen

Fresh re-audit of `docs/TECHNICAL_DEBT.md` post-B2: the BLOCKER tier is
empty, IMPORTANT has two items (workspace-identity permanence — an
architectural constraint with a documented workaround, not a bounded
bug; the real-provider retry's bounded failure surface — already
explicitly accepted as correct-by-design), NICE_TO_HAVE is cosmetic or
already-resolved-locally (the `pytest` advisory needs a major-version
product decision, correctly not made unilaterally in B2). None of these
is a coherent, substantial B3 program on its own.

The user's own instructions named the GitHub App/JWT architecture as
the first candidate to re-evaluate. Re-reading ADR-001 in full (it was
written and approved at B0 time but never implemented — B0.7 explicitly
built only "a deliberately simplified, explicitly scoped interim
consumer" instead, flagging the full design as a residual gap every
phase since has restated) confirms it: this is the single most
substantial, most-repeatedly-flagged (B0, B1, and B2's own residual-risk
lists all restate it), already-designed-and-approved gap left. Unlike
workspace-identity or the pytest bump, it has a real, bounded,
locally-verifiable implementation path — ADR-001 itself already
distinguishes the app-wide-key/JWT/token-minting mechanics (fully
buildable and testable with a self-generated RSA keypair) from the
one genuinely external dependency (a real registered GitHub App).

A second, smaller finding from this phase's own fresh audit is folded
in as B3.2: this repository has **no dedicated health/readiness
endpoint at all** — `scripts/start.sh`'s own liveness check scrapes the
dashboard's `<title>` text, workable for today's single-process
self-hosted default but not what a real orchestrator (systemd
`ExecStartPost`, a load balancer, a container platform) expects. Small,
real, previously untouched by B0-B2, genuinely a Track-B operability
gap — included because it doesn't dilute B3.1's focus (one route, no
new service, no new trust boundary).

## Scope

**B3.1 — GitHub App installation mechanics (ADR-001's own
distinction between "buildable now" and "needs a real App").**

Buildable and fully tested locally, with real cryptography, no fakes
standing in for the actual algorithm:
- `organizations.github_installation_id` (migration 35, additive,
  nullable — an org may have none, exactly as ADR-001 specifies).
- App-wide credentials via env vars (`WORKSPACE_MANAGER_GITHUB_APP_ID`,
  `WORKSPACE_MANAGER_GITHUB_APP_PRIVATE_KEY`, `WORKSPACE_MANAGER_
  GITHUB_WEBHOOK_SECRET`) — the same precedent `session_secret`/
  `secret_encryption_keys` already established (app-wide, never a
  per-tenant secret, never stored in the database — ADR-001's own
  "Credential/token types actually stored" section requires exactly
  this).
- `app/services/github_app_service.py`: a real RS256 JWT builder
  (`cryptography`'s own RSA primitives — no new dependency, the same
  dependency-minimalism precedent as CSRF/rate-limiting being
  hand-rolled rather than reaching for PyJWT) matching GitHub's own
  App-authentication spec (`iss`=App ID, `iat` backdated 60s for clock
  drift, `exp`≤600s), and `GitHubAppService.mint_installation_token()`
  — the real `POST /app/installations/{id}/access_tokens` exchange,
  HTTP transport injectable (same DI pattern as `DeploymentService.
  http_get`), for testing without a live network call.
- `make_installation_token_runner()` (`app/services/
  github_merge_service.py`, alongside the existing `make_hosted_runner`
  PAT-based one): resolves `repositories.organization_id ->
  organizations.github_installation_id`, mints a token JIT, injects it
  via the SAME `token_runner()` env-var mechanism B0.7 already built
  (never argv, never disk) — reusing, not duplicating, that injection
  code. `app/main.py` prefers this runner when an App is configured,
  falls back to B0.7's PAT-based runner when it isn't — ADR-001's own
  "paste your own token" escape hatch, now realized as "the fallback,"
  not a separate design.
- Admin-only callback (`POST /orgs/{org_id}/github-installation`,
  B0.3-guarded ADMIN-only, CSRF-protected, rate-limited): stores a
  GitHub-supplied `installation_id` on the matching org — ADR-001's own
  "Install" flow's app-side half (the GitHub-redirect half needs a real
  registered App, see Non-goals).
- `POST /webhooks/github`: HMAC-SHA256-verified (`X-Hub-Signature-256`
  against the raw body, `hmac.compare_digest`, fail closed on any
  mismatch or missing header) before any parsing — untrusted network
  input, never trusted by default (ADR-001's own trust-boundary #4).
  Handles `installation` events with `action: "deleted"`: clears the
  matching org's `github_installation_id` — ADR-001's own "Revoke,
  org-initiated" offboarding flow. Every other event type/action is
  accepted (200, HMAC already verified) and ignored — replacing polling
  with webhook-driven PR status is ADR-001's own explicit non-goal for
  this initial cut, not silently expanded into here.

**B3.2 — health/readiness endpoint.** `GET /health`: no auth, no
CSRF, no tenant data, cheap (one trivial `SELECT 1`, not a full
dashboard render); real fail-closed behavior (returns 503 if the DB
query itself fails, never a blind 200).

## Non-goals (explicitly deferred, and why)

- **Actually registering a GitHub App on github.com.** Needs a human
  with a GitHub account, using GitHub's own App-creation UI, to obtain
  a real App ID and download a real private key — this session has no
  way to obtain or safely fabricate that as genuine evidence (the exact
  same reasoning B0.7 already gave, restated because it's still true).
  **This is the one genuinely BLOCKED piece of ADR-001** — everything
  else in its "Install/revoke/rotate" and "Trust boundaries" sections
  that does NOT require that real App is built in B3.1 above.
- **The GitHub-redirect half of the Install flow** (an org admin
  clicking "Install ProjectFlow" on GitHub's own site, GitHub
  redirecting back with a real `installation_id`) — needs the real App
  from the item above to have a redirect URL to register in the first
  place. The callback route this session CAN build (accepting an
  `installation_id` an admin supplies) is built; the redirect trigger
  side is not, for the same reason.
- **Live, end-to-end verification against a real installation** (a real
  token mint against a real repo) — same blocker.
- **Webhook-driven PR/check-run status replacing today's polling** —
  ADR-001's own explicit non-goal for this initial cut.
- **Multiple GitHub installations per organization, GitHub Enterprise
  Server support** — ADR-001's own explicit non-goals.
- **The `pytest` major-version bump** (B2's own residual) — a product
  decision, not made unilaterally here either.
- **Workspace-identity permanence** — architectural, has a documented
  workaround, not a bounded bug.

## Design principles (carried over, unchanged)

`AUTH_MODE=none` untouched — `GitHubAppService`/the new routes are
never constructed or reached in that mode, exactly like B0.7's own PAT
consumer. Additive-only migration (35). Reuse `token_runner()`'s
existing env-var-injection mechanism rather than a second one. Fail
closed: an unconfigured App, a failed mint, or a bad webhook signature
all produce an explicit rejection, never a silent bypass to an
unauthenticated fallback.

## Acceptance criteria

1. A real RS256 JWT is built and its signature verifies against the
   SAME RSA public key (self-generated test keypair, not a live App) —
   proves the algorithm is actually correct per GitHub's spec, not just
   "doesn't crash."
2. `GitHubAppService.mint_installation_token()` is proven with both a
   successful and a failing injected HTTP transport — the failure path
   raises the same error taxonomy `GitHubMergeService`'s existing
   callers already handle (`GitHubIntegrationError`), no structural
   change needed at any of its ~15 existing call sites.
3. `make_installation_token_runner()` resolves the correct org/
   installation from a repo path and injects the minted token the same
   way `token_runner()` already does (env vars only) — proven with a
   real fake GitHub API transport, not a live call.
4. The admin-only callback route: an ADMIN/OWNER can set an
   installation id; a MEMBER/VIEWER cannot (403); a non-member gets 404
   (existence-hiding, B0.2's own precedent); CSRF-guarded.
5. The webhook route: a validly-HMAC-signed `installation.deleted`
   payload clears the org's installation id; an invalid/missing
   signature is rejected (401/403, never parsed); a differently-typed
   event is accepted and ignored, not mistaken for a deletion.
6. `AUTH_MODE=none` behavior is byte-for-byte unchanged (existing full
   regression suite, self-hosted mode).
7. `GET /health` returns 200 with a real DB check under normal
   operation; the full existing GET-route completeness sweep
   (`tests/test_b1_read_isolation.py`) accounts for it explicitly.
8. Full existing regression suite (fast non-real subset) stays green.

## Stop condition

Same as B0/B1/B2: do not begin B4 automatically.
