# B0 — Hosted Platform Security Foundation (spec, 2026-08)

**Status: SPEC ONLY. Not started. Do not begin implementation
automatically** — this document exists so a future, explicit "begin B0"
instruction has a grounded, implementation-ready plan to start from, per
Track A1's own closing instruction ("Do NOT begin B0 automatically") and
this document's own request ("define B0 ... as an implementation-ready
spec/plan; do not start broad B0 implementation").

Track A1 (Performance Foundation & Simple Mode) is complete and verified
in production — see `docs/TRACK_A1_PERFORMANCE_AND_SIMPLE_MODE.md` and
that track's own live-verification report. E1–E13 is the existing core
engineering lifecycle. This document does not touch either.

## Why now

Every future hosted/multi-user/App-Builder direction P0 and A1 both
gestured at is blocked on this program: ProjectFlow today has zero
AuthN, zero AuthZ, zero multi-tenant data isolation, zero CSRF
protection, zero rate limiting, opt-in (not mandatory) sandboxing, and
no secrets-storage layer — all by design, for a local, single-user tool
(see the grounded audit below). None of that is a defect *today*; it
becomes a defect the moment this app is exposed to more than one person
or beyond `127.0.0.1`.

**Real production usage already exists** — A1's own live-verification
pass found 7 real Tasks, 5 real repositories, and real agent-session
history in the single production database, none of it wrapped in a
Change yet. Whatever B0 ships **must not disrupt that existing
single-user, local deployment** — it must be an additive mode, not a
flag day. See Design Principles below.

## Grounded current-state audit (evidence, not assumptions)

Gathered directly from the codebase on 2026-08-31, the same evidence-
first discipline P0 and A1 both used. Every line number is current as
of commit `8e460e7`.

| Area | Finding | Citation |
|---|---|---|
| AuthN | Zero. No login/session/user_id concept anywhere in app code. Only cookie is `pf_mode` (A1's UI-mode preference, not auth) | `app/main.py:336`, comment at `app/main.py:319-320` |
| AuthZ | Zero. No `Depends()`-based guard, `current_user`, or `require_*` anywhere in the 4,319-line `app/main.py` | grep across `app/main.py` |
| Multi-tenancy | None. `repositories` is the existing "project" boundary (`project_id` FKs to it everywhere, E1.6's own convention) but has no owner/tenant column. No `users`/`organizations`/`tenants` table exists in any of the 51 tables. `sandboxes.owner_type/owner_id` means internal resource ownership (which AgentWorkspace/TaskIntegration/RepositoryTest owns this sandbox), not user identity | `app/db.py:17` (repositories), `app/db.py:589` (project_id convention comment), `app/db.py:50-51` (sandboxes owner columns) |
| CSRF | Zero. No token in any template, no CSRF middleware, **no middleware of any kind** registered on the FastAPI app | grep across `app/templates/`, `app/main.py` |
| Rate limiting | Zero. No throttling, no middleware | grep across `app/main.py` |
| Secrets | No secrets table/column anywhere. GitHub integration deliberately never stores a token — shells out to the host's own already-authenticated `gh` CLI | `app/services/github_merge_service.py:6-13`'s own docstring: "never a hand-rolled OAuth flow or a raw access token handled by this app" |
| Sandbox isolation | `SandboxManager`/`SandboxRuntimeService` exist and work, but are opt-in — Builder git worktrees run on the host filesystem by default; PROJECT.yaml commands execute via `shell=True` in several places with no sandboxing forced | `app/services/gate_waiver_service.py:60`, `app/services/test_runner.py:24`, `app/main.py:4313` |
| Network boundary | `127.0.0.1` enforced at three independent layers: hard refusal of any other host/port, a matching default, and the systemd unit's own reasoning about staying local-only. No reverse proxy config anywhere in the repo | `scripts/start.sh:13-20`, `app/config.py:34`, `systemd/workspace-manager.service.in:12-19` |
| Dependencies | fastapi, uvicorn, jinja2, python-multipart, PyYAML, websockets, ruamel.yaml. **Absent**: passlib/bcrypt, PyJWT/authlib, slowapi/fastapi-limiter, any session library. Starlette's own `SessionMiddleware` (itsdangerous) ships transitively via FastAPI but is unused — available at near-zero new-dependency cost | `pyproject.toml:11` |

P0's own exact classification (`docs/PRODUCTIZATION_AUDIT.md:244-258`,
gathered in that audit, not re-derived here): AuthN/AuthZ, multi-tenant
isolation, CSRF, sandboxing, and SSRF-via-deployment-health-checks are
all `MUST_FIX_BEFORE_PUBLIC_BETA`; rate limiting is `CAN_WAIT for track
A; MUST_FIX_BEFORE_PUBLIC_BETA for B/C`; secrets-in-logs, dependency
audit, and path traversal are `CAN_WAIT (flagged, not scored)`.

## Scope

Matching this program's own named focus areas exactly:

- **B0.1 AuthN** — real user accounts, login/logout, sessions.
- **B0.2 AuthZ** — coarse role-based route guards.
- **B0.3 Organizations/Tenants** — a real tenant boundary and per-tenant
  data isolation.
- **B0.4 Sandbox boundary** — mandatory (not opt-in) sandboxing once
  tenant-supplied code executes.
- **B0.5 CSRF** — token issuance/validation on every mutating route.
- **B0.6 Rate limiting** — per-identity/per-IP throttling.
- **B0.7 Secrets boundary** — encrypted, tenant-scoped secret storage +
  log/transcript redaction.

## Non-goals / explicit deferrals

- Billing/payments — a distinct future program.
- Fine-grained, per-resource RBAC — B0 ships coarse roles only
  (OWNER / ADMIN / MEMBER / VIEWER per organization).
- SSO/SAML/OIDC enterprise auth — B0 ships local email+auth plus, at
  most, a single OAuth provider stub; enterprise SSO is a later program.
- Chat App Builder — A1.24 already scoped its own remaining gap
  separately; B0 does not implement it.
- Infra/deployment topology (multi-region, load balancing, etc.) — B0
  is app-layer only.

## Design principles

1. **Additive-only migrations**, continuing the existing
   `schema_migrations` sequence from version 31 forward — never rewrite
   a shipped migration (the whole codebase's own standing discipline).
2. **Self-hosted single-user mode must keep working**, unmodified, via
   an explicit mode toggle — proposed `WORKSPACE_MANAGER_AUTH_MODE=
   none|required`, defaulting to `none` (today's real, exact behavior:
   the same zero-AuthN experience the current production instance
   already runs). This is not a flag day; both modes are real,
   supported deployment targets from B0.1 onward.
3. **Reuse existing infrastructure over inventing new mechanisms**:
   - `repositories` is already the one place every other table's
     `project_id` FKs through (E1.6's own convention) — adding a single
     `repositories.organization_id` column is the one scoping lever
     that transitively tenant-scopes the entire existing schema,
     without touching 50 other tables' own FKs.
   - `SandboxManager`/`SandboxRuntimeService`'s actual sandboxing
     mechanism is reused as-is; B0.4 only flips the opt-in policy check
     to mandatory when `AUTH_MODE=required`.
   - Prefer a small number of shared, reusable primitives (in the same
     spirit as A1's own `RequestMemo`) over hand-editing every route —
     e.g. one `require_role()` FastAPI dependency, one scoping helper
     for tenant-filtered queries, applied uniformly.
4. **No security theater** — every gate must be real and covered by a
   real, disposable-fixture test (the same "golden fixture" discipline
   P0/A1 already established), not asserted by inspection alone.

## Proposed architecture per sub-area

### B0.1 AuthN
New tables `users(id, email, password_hash, created_at, ...)` and
`sessions(id, user_id, token_hash, created_at, expires_at)`, **or**
Starlette's built-in `SessionMiddleware` (itsdangerous, already
transitively available, zero new dependency) for a signed-cookie
session — recommended default for the MVP given the project's
demonstrated dependency-minimalism, revisited once real multi-device/
revocation needs are known. Login/logout routes; a `current_user`
FastAPI dependency injected wherever `AUTH_MODE=required`; a no-op
fallback (today's exact behavior) wherever it's `none`.

### B0.2 AuthZ
Coarse per-organization roles (OWNER/ADMIN/MEMBER/VIEWER). One
`require_role(min_role)` dependency wraps every mutating route (there
are roughly 100+ POST/PUT/DELETE routes in `app/main.py` today — this
needs a systematic sweep, not a handful of spot fixes, tracked as its
own sub-phase).

### B0.3 Organizations/Tenants
New tables `organizations(id, name, slug, created_at)` and
`organization_members(org_id, user_id, role)`; `repositories.
organization_id` FK (see Design Principles #3 for why this is the
single leverage point). Every list/read query needs an
`organization_id` filter added at the query layer — via one reusable
scoping helper, not 50 hand-edited call sites.

### B0.4 Sandbox boundary
Flip `SandboxManager`'s opt-in policy to mandatory when `AUTH_MODE=
required`: every Builder workspace's command execution (today's
`shell=True` call sites, see the audit table) must run inside a
container once tenant-supplied code is involved. The sandboxing
mechanism itself (`sandbox_manager.py`/`sandbox_runtime.py`/
`sandbox_contract.py`) is reused unmodified.

### B0.5 CSRF
No existing middleware to extend (this app registers none today) —
add one lightweight double-submit-cookie CSRF token, validated by a
shared dependency applied to every mutating route, alongside B0.2's
own route sweep.

### B0.6 Rate limiting
An in-house, minimal per-IP/per-user token-bucket middleware —
recommended over adopting `slowapi`/`fastapi-limiter` given this app
has zero existing middleware infrastructure to extend and a
demonstrated preference for a small dependency footprint; revisit if
the in-house version proves insufficient under real load.

### B0.7 Secrets boundary
The current GitHub-integration design (delegate to the host's own
`gh` CLI, never store a token) **does not survive multi-org hosting** —
a hosted service can't rely on one shared host's authenticated CLI
session across tenants. This is the single largest unresolved
architectural question this audit surfaced (see Open Decisions below);
B0.7 is scoped to build the *general* encrypted, org-scoped secret-
storage primitive, with the GitHub-specific redesign as its first real
consumer once B0.3 exists. A redaction layer for agent transcripts/
logs (flagged `CAN_WAIT` in P0, still open) is scoped here too, since
transcripts become multi-tenant-visible surface once B0.3 ships.
GitHub-integration architecture itself is **resolved** — see ADR-001
below.

## ADR-001: GitHub authentication/authorization architecture for hosted multi-tenant mode

**Status: DECIDED (design only — not implemented).** Resolves Open
Decision #3 below. Scope: how `GitHubMergeService` authenticates once
`AUTH_MODE=required` and B0.3's organizations exist; `AUTH_MODE=none`
is unaffected (see its own section below) and needs no ADR.

### Grounding: what GitHubMergeService actually does today

Read in full (`app/services/github_merge_service.py`, 160 lines) to
ground this decision in the real, current operation set rather than a
generic "GitHub integration" assumption:

- `git push` (push a branch to origin), `gh pr list`/`gh pr create`/
  `gh pr view`/`gh pr merge`, `git fetch`/`git rev-parse`/`git
  merge-base` — every operation needs: **push access to repo contents,
  read/write on pull requests, and read on commit statuses/check
  runs**. Nothing broader (no admin, no secrets, no Actions triggers).
- `_parse()` reads `statusCheckRollup` via **polling** (`gh pr view
  --json ...`) — there is no webhook integration today at all.
- **Every single call site is keyed by an already-registered
  `repositories.repo_path` row** (`app/main.py:1591,1728,1734,1757,
  1761,2256,3900,3912-3920,3950,3967,3982,4010-4011`,
  `app/services/integration_service.py`) — never by a client-supplied
  owner/repo string. The module's own docstring states this as a
  deliberate invariant: "there is no code path that accepts a repo
  owner/name or PR number from the browser." **This invariant must
  survive B0 unchanged** — the design below extends it to "and never
  accepts an installation id from the browser either."
- `available()` only checks the git remote looks like a GitHub URL
  locally — it never confirms `gh` is actually authenticated; that's
  discovered lazily by the real API calls failing. This changes under
  the hosted design (see Failure behavior below).

### Options compared

| | GitHub App per org | Per-user OAuth token | Tenant-provided/BYOC | 
|---|---|---|---|
| Tenant isolation | Native — installation scoped to granted repos | Weak — token scoped to whatever the user can access, spans orgs outside ProjectFlow's own boundary unless policed in-app | Perfect by construction (tenant's own credential never leaves their control, in the self-hosted variant) |
| Least privilege | Fine-grained permissions declared once (`contents`, `pull_requests`, `checks`) | Coarse (`repo` scope = all repos, public+private); fine-grained PATs help but are user-managed, not app-provisioned | Entirely the tenant's own discipline; ProjectFlow can't enforce it |
| Repo/org scoping | Native (installation = "all repos" or "selected repos") | None native — must be enforced in ProjectFlow's own policy layer | Whatever the tenant's own credential is scoped to |
| Token lifetime/rotation | Installation tokens auto-expire ≤1hr, minted on demand; only the App's own long-lived private key is stored, app-wide | Classic tokens don't expire by default; a real long-lived bearer token must be stored per user | Tenant's own responsibility; ProjectFlow can't rotate it |
| Webhook model | One App-wide endpoint, `installation.id` cleanly dispatches per-tenant | No native installation scoping; per-repo-per-user registration, needs the connecting user to have repo admin rights | N/A for self-hosted; same as OAuth if a pasted PAT is used instead |
| Installation lifecycle | Real install/uninstall webhook events — clean onboarding/offboarding | No install/uninstall concept; needs custom connect/disconnect UI | N/A (self-hosted) / manual paste-replace (pasted-PAT variant) |
| Auditability | GitHub log shows "App on behalf of installation X" — org-level, not per-ProjectFlow-user unless logged separately | GitHub log shows "user via OAuth App" — better individual attribution | Whatever the underlying credential shows |
| Secret storage burden | LOW — one app-wide private key + webhook secret; **zero per-tenant secrets** (tokens minted, never persisted) | HIGH — one real bearer token stored per user, forever until revoked | ZERO for true self-hosted BYOC (nothing to store); HIGH for hosted-pasted-PAT (same as OAuth, arguably worse — PATs are commonly over-scoped) |
| UX/onboarding | One-click "Install ProjectFlow" GitHub flow, org-admin-approved once | "Connect your GitHub account" per user, or a shared bot account (bus-factor risk) | Zero onboarding for self-hosted (today's exact experience); manual token generation/paste for hosted-pasted-PAT (known support/security-mistake source elsewhere) |
| Service-account/automation compatibility | Excellent — Apps ARE the service-account model | Requires manually provisioning + OAuth-connecting a dedicated bot account | Good for self-hosted (a bot PAT works, unchanged from today) |
| Self-hosted compatibility | Orthogonal — self-hosted keeps using `gh` CLI, untouched | Orthogonal | **Is** self-hosted compatibility — the natural formalization of `AUTH_MODE=none`'s existing design |
| Blast radius | "Every installed org," bounded by minimal declared permissions + 1hr token TTL | Every connected user's live, long-lived, broad-scoped token leaked on backend/DB compromise | Contained inside tenant's own infra for self-hosted BYOC; same as OAuth's per-org concentration for hosted-pasted-PAT |
| Revocation | Instant, org-admin self-service (uninstall) or ProjectFlow-initiated | Per-user, not per-org — an offboarded org leaves N individual tokens to clean up | Tenant's own action (self-hosted) / ProjectFlow secret-store deletion (pasted-PAT) |
| GitHub API limits | Own 5,000-15,000/hr budget **per installation** — scales with tenant count | Shared 5,000/hr **per user**, contends with that user's own personal GitHub usage | Whatever the tenant's own credential's budget is |
| Operational complexity | Moderate upfront (App registration, JWT signing, installation-token minting, webhook lifecycle); well-trodden pattern elsewhere | Lower upfront, higher ongoing (token storage/rotation, no clean lifecycle, per-user reconnect flows) | Lowest for self-hosted (already built); moderate for hosted-pasted-PAT (needs B0.7's secret store, no App machinery) |
| Migration from today's `gh` CLI delegation | Clean — `GitHubMergeService`'s method surface unchanged, only its credential-resolution layer gains an `AUTH_MODE`-gated branch | Clean in principle, but requires B0.7's secret store built first, and reintroduces the "raw access token handled by this app" the current design explicitly avoids | Self-hosted: zero migration (already the current design). Hosted-pasted-PAT: same secret-store dependency as OAuth |

A fourth combined "hybrid" was also considered and **is** the actual
recommendation below — not a fourth independent design.

### Recommendation

**GitHub App per organization as the primary hosted-mode mechanism.
Self-hosted BYOC (today's `gh` CLI delegation under `AUTH_MODE=none`)
remains a fully first-class, permanently supported mode — not a
fallback.** Per-user OAuth is rejected outright as the primary
mechanism. A narrow "paste your own token" escape hatch for hosted
tenants whose org policy forbids installing third-party GitHub Apps
*may* be offered later as an explicitly documented exception, never
the default — its own threat model is not designed here (see Residual
risks).

**Rationale**: the App model is the only option that satisfies this
codebase's own already-stated principle — "never a hand-rolled OAuth
flow or a raw access token handled by this app"
(`app/services/github_merge_service.py:6-13`) — while also working for
genuinely hosted, multi-org, multi-user scenarios that self-hosted BYOC
structurally cannot serve (a hosted tenant expects to click "install,"
not run `gh auth login` on infrastructure they don't control). It
natively matches B0.3's organization boundary, mints tokens on demand
rather than storing per-tenant secrets (minimizing B0.7's burden to
exactly one app-wide key), has a real install/uninstall lifecycle for
clean offboarding, and scales API budget per-installation instead of
sharing one global limit across every tenant.

### Trust boundaries + threat model

1. **ProjectFlow backend <-> GitHub.** The backend holds ONE app-wide
   RSA private key (never a per-tenant secret), used to sign short-
   lived JWTs (≤10 minutes, GitHub's own limit) exchanged for
   installation access tokens. Compromise of this one key is the
   single highest-severity risk in this design (an attacker could mint
   tokens for any installed org) — it warrants its own protection tier
   (dedicated secret-manager entry, scheduled rotation, access
   logging), distinct from B0.7's general per-tenant secret store.
2. **Organization <-> ProjectFlow.** An org admin explicitly grants
   installation scope (all repos or selected repos) — GitHub's own
   enforced boundary, not ProjectFlow's to get wrong.
3. **Request handling <-> repository resolution.** Every token mint is
   keyed `repositories.id -> repositories.organization_id ->
   organizations.github_installation_id`, resolved server-side —
   **never from a client-supplied owner/repo or installation id**,
   extending today's existing invariant (see Grounding above) rather
   than replacing it.
4. **Webhook ingress <-> ProjectFlow.** Every inbound payload is
   HMAC-verified (`X-Hub-Signature-256`, the App's webhook secret)
   before any parsing — untrusted network input, never trusted by
   default.

### Credential/token types actually stored (or not)

- **Stored, app-wide, long-lived:** one App private key (RSA) + one
  webhook secret. Not per-tenant.
- **Stored, per-org, non-secret:** `organizations.github_installation_id`
  — an opaque integer, safe in a plain column (not a credential).
- **Never stored, anywhere:** any per-org or per-user bearer/access
  token. Installation access tokens are minted on demand (`POST
  /app/installations/{id}/access_tokens`), used for exactly one
  `git`/`gh` subprocess call, and left to expire naturally — the
  direct continuation of today's "never a raw access token handled by
  this app" principle, just replacing "delegate entirely to the host's
  CLI" with "mint one, use it once, discard it."
- **Encryption requirement:** the App private key and webhook secret
  need B0.7's general encrypted-secret-store primitive (envelope
  encryption via a KMS, or at minimum a locally-held encryption key
  never committed to the DB in plaintext) as a **prerequisite** — this
  ADR does not bypass B0.7, it is B0.7's first real consumer, exactly
  as the Proposed Architecture section above already scoped.

### Organization/repository mapping model

`organizations.github_installation_id` (nullable — an org may exist
without GitHub connected, or use self-hosted BYOC instead).
`repositories.organization_id` (already B0.3's own column) resolves a
repo to its org, and the org to its installation — one more hop on the
existing `project_id -> repositories.id` chain (E1.6's own
convention). A separate `github_installations` join table (for one
ProjectFlow org spanning multiple GitHub installations) is explicitly
deferred — start with the simpler one-column model, add the join table
only if real usage demands it.

### Webhook identity/verification

One shared endpoint, `POST /webhooks/github`, registered once at App
creation. Every payload is (1) HMAC-verified against the App's webhook
secret before parsing, (2) dispatched by its `installation.id` field
to the matching `organizations` row, (3) further scoped by
`repository.full_name`/`repository.id` to the already-registered
`repositories` row. An `installation` event with action `deleted`
triggers the offboarding cleanup below automatically. Using
`pull_request`/`check_run`/`status` webhooks to **replace** today's
polling (`gh pr view`) is a real, worthwhile enhancement — explicitly
deferred to a phase-2 pass (see Non-goals), not part of this decision's
initial cut.

### Permissions/scopes principle

Least-privilege, declared once at App-registration time (every org
sees exactly what they're granting): `contents: write`, `pull_requests:
write`, `checks: read`, `statuses: read`. No `administration`, no
`secrets`, no `actions: write`, no org-admin permissions — a direct,
minimal mapping of the operations `GitHubMergeService` already performs
today (see Grounding), nothing broader requested "just in case."

### Install/revoke/rotate flows

- **Install:** org admin uses GitHub's own App-install URL, selects
  org + repos, GitHub redirects back with the new `installation_id`;
  a B0.2-guarded (admin-only) callback route stores it on the matching
  `organizations` row.
- **Revoke, org-initiated:** admin uninstalls from GitHub's side ->
  `installation.deleted` webhook -> ProjectFlow clears
  `organizations.github_installation_id` and marks every `repositories`
  row under that org unavailable (same spirit as today's `available()`
  check, now driven by real installation state).
- **Revoke, ProjectFlow-initiated** (tenant offboarding/non-payment):
  call GitHub's installation-deletion API directly; same cleanup.
- **Rotate:** the App private key rotates on a defined schedule (or on
  suspected compromise) via GitHub's own App-settings UI, which
  supports multiple concurrent keys during rotation — no per-tenant
  action needed, since no per-tenant secret exists to rotate.

### Failure and tenant-offboarding behavior

A failed token mint (installation suspended, or deleted but its
webhook not yet processed) surfaces as the existing
`GitHubIntegrationError` shape (a new code, e.g.
`INSTALLATION_UNAVAILABLE`, alongside today's `GH_CLI_ERROR`/
`PR_CREATE_FAILED`/etc.) — every existing caller in
`integration_service.py`/`main.py` needs no structural change, only a
new distinguishable error code to handle. Tenant offboarding is a
single, complete, GitHub-side action (uninstall) — no per-user token
cleanup, unlike the OAuth option.

### How `AUTH_MODE=none` stays isolated from hosted GitHub auth

`GitHubMergeService`'s credential-resolution layer becomes mode-aware
at construction, not scattered through call sites. Under `AUTH_MODE=
none` (today's default, unchanged): delegates entirely to the host's
already-authenticated `gh` CLI, exactly as today — the App/installation
code path is never reached at all. Under `AUTH_MODE=required`: resolves
an installation token per repo as described above. This mirrors B0.1's
own `AUTH_MODE` toggle discipline exactly — self-hosted operators never
need a GitHub App, a private key, or any B0.7 dependency.

### Migration path — no flag-day breakage

`GitHubMergeService`'s public method surface (`available`,
`push_branch`, `find_existing_pr`, `create_pr`, `pr_status`,
`merge_pr`, `target_head`, `is_ancestor`) is **unchanged**. Only its
internal `runner` construction changes, gated by `AUTH_MODE`:
`AUTH_MODE=none` keeps `runner=_default_runner` exactly as today;
`AUTH_MODE=required` uses a new runner wrapper that resolves an
installation token just-in-time and injects it for that one subprocess
call. Every existing caller (already keyed by `repositories.id`, per
Grounding above) needs no change at all — the existing `runner`
dependency-injection seam (already used for tests) is precisely the
seam this migration needs.

### Non-goals (of this decision)

- Webhook-driven status updates replacing today's polling — deferred
  enhancement, not part of the initial cut.
- Per-ProjectFlow-user attribution inside GitHub's own audit log (App
  actions are attributable to "the App on installation X," not to
  which ProjectFlow user triggered it, unless ProjectFlow logs that
  correlation itself — a separate audit-logging feature).
- Multiple GitHub installations per ProjectFlow organization — deferred
  until real usage demands it.
- Designing the hosted-with-pasted-PAT escape hatch's UI/flow.
- Any change to `AUTH_MODE=none` behavior.

### Residual risks / open questions

- App private key custody (dedicated secrets manager vs. B0.7's general
  encrypted store) is not decided here — B0.7's implementation must
  choose, this ADR only requires the capability exist.
- GitHub Enterprise Server (self-hosted GitHub) support is unresolved —
  the App model works there too but registration/URLs differ; not
  investigated in this pass.
- JWT-exchange rate-limit behavior under very high tenant counts
  sharing ProjectFlow's own outbound IP is not load-tested (distinct
  from the per-installation 5,000/hr budget, which scales fine) —
  flagged for empirical verification during B0.7 implementation,
  matching this codebase's own profile-before-optimizing discipline.
- The hosted-with-pasted-PAT escape hatch, if ever built, reintroduces
  per-tenant secret-storage burden for that subset of tenants and needs
  its own dedicated threat-model pass.

## Phasing

Each phase independently shippable and testable, mirroring E1–E13's
own phased discipline — no phase silently depends on a later one's
completion beyond what's noted:

1. **B0.1** AuthN foundation (users/sessions, login/logout, the
   `AUTH_MODE` toggle preserving today's default behavior exactly).
2. **B0.2** Organizations/Tenants (org/membership tables,
   `repositories.organization_id`, the scoping helper).
3. **B0.3** AuthZ (roles, `require_role()`, the full route sweep).
4. **B0.4** CSRF (token issuance/validation, same route sweep as B0.3).
5. **B0.5** Rate limiting (middleware).
6. **B0.6** Mandatory sandboxing in hosted mode.
7. **B0.7** Secrets boundary (encrypted org-scoped secrets +
   redaction; GitHub-integration redesign as its first consumer).

## Open decisions requiring explicit human sign-off before implementation

These are real, unresolved architectural choices — not silently
decided by this document:

1. **Password vs passwordless (magic-link) authentication** for B0.1.
   A passwordless flow avoids needing a password-hashing dependency
   and its own attack surface entirely; a password flow is more
   familiar/portable. No default is assumed here.
2. **In-house vs adopt a library** for CSRF/rate-limiting (this
   document recommends in-house for both, given zero existing
   middleware and the project's dependency-minimalism, but that's a
   recommendation, not a decision made on the user's behalf).
3. ~~**GitHub auth architecture** once multi-org hosting means
   "delegate to the host's own `gh` CLI" no longer holds.~~
   **RESOLVED — see ADR-001** (above, in the B0.7 section): GitHub App
   per organization, self-hosted `gh` CLI delegation preserved unchanged
   under `AUTH_MODE=none`. Still requires a human sign-off on ADR-001's
   own recommendation before B0.7 implementation starts — "resolved"
   here means "a concrete, evidence-based recommendation now exists,"
   not "silently approved."
4. **Whether self-hosted single-user mode (`AUTH_MODE=none`) is a
   permanent, supported deployment target**, or an eventually-
   deprecated transitional one. Affects how much long-term test/
   maintenance weight the dual-mode design in Design Principle #2 is
   worth carrying.

## Acceptance criteria template (per sub-program)

Mirroring A1's own finish-conditions discipline:

- A real, disposable, multi-organization fixture (two orgs, two users
  each, cross-org access attempts) proves actual data isolation — not
  merely asserted by code inspection.
- Full existing regression suite (`pytest tests/ -k "not real_"`, 891
  tests as of Track A1) still passes unmodified with `AUTH_MODE=none`
  — the existing single-user experience must never regress.
- Every new mutating route added or touched carries both an AuthZ
  guard (B0.2+) and a CSRF check (B0.4+) — swept systematically, with
  a test asserting the sweep is complete (e.g. enumerating registered
  routes and checking each mutating one carries the dependency), not
  spot-checked.
- Before/after evidence for anything performance-sensitive (session
  lookup overhead, tenant-scoping query cost), following A1's own
  profiling-before-optimizing discipline — never guessed.

## Closing instruction

This document is a spec for review only. **Do not begin B0
implementation automatically** — wait for an explicit instruction to
start, and resolve the Open Decisions above before any code is
written.
