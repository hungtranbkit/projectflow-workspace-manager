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
3. **GitHub auth architecture** once multi-org hosting means "delegate
   to the host's own `gh` CLI" no longer holds — the single biggest
   open question this audit found. Options include a GitHub App
   installed per-organization, per-user OAuth tokens stored via B0.7's
   own secret store, or continuing to require each tenant to bring
   their own already-authenticated environment (limiting hosted
   viability). Needs a dedicated design pass before B0.7 implementation
   starts.
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
