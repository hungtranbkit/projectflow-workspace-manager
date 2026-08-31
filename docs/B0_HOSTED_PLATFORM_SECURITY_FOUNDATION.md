# B0 — Hosted Platform Security Foundation (spec, 2026-08)

**Status: ADR-001 through ADR-004 approved as current product/
engineering decisions. B0.1 through B0.7 are all IMPLEMENTED** — email
magic-link login, self-hosted first-user console-token bootstrap, API
tokens; real organizations/membership/coarse roles/invitations,
`repositories.organization_id` as the one tenant-scoping lever (Design
Principle #3); a general `require_role()` AuthZ guard swept across all
157 mutating routes; in-house CSRF folded into that same sweep plus
the standalone login-CSRF fix on `/auth/verify`; slowapi rate limiting
on the named abuse-sensitive auth/org/token routes; mandatory
ephemeral-container sandboxing for tenant-supplied PROJECT.yaml command
execution; and a general encrypted org-scoped secret store with a
simplified GitHub-token consumer and transcript/log redaction. See
each sub-phase's own section below and its implementation report for
full acceptance-criteria coverage, test evidence, and residual risks
(most notably: B0.3's GET-route cross-org read gap, and B0.7's
simplified-PAT-vs-full-GitHub-App gap — both explicitly flagged in
their own sections, not silently left implicit). A FINAL B0
QUALIFICATION pass runs after this document's own edits settle, per
the same explicit user authorization to continue autonomously through
B0.7 without per-phase approval as long as each phase genuinely
PASSes.

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
| Dependencies | fastapi, uvicorn, jinja2, python-multipart, PyYAML, websockets, ruamel.yaml. **Absent**: passlib/bcrypt, PyJWT/authlib, slowapi/fastapi-limiter, any session library. **Correction, made during B0.1 implementation** (superseding this row's own original claim below): `itsdangerous` — required by Starlette's `SessionMiddleware` — was verified NOT actually installed in this environment (`import itsdangerous` failed) despite this audit's original "ships transitively via FastAPI" claim; declared as an explicit direct dependency instead of relied upon implicitly. `slowapi`/`itsdangerous` are now both real, declared dependencies as of B0.1 (`pyproject.toml`) | `pyproject.toml:11` (original); corrected by direct verification during B0.1 implementation, see B0.1's own report |

P0's own exact classification (`docs/PRODUCTIZATION_AUDIT.md:244-258`,
gathered in that audit, not re-derived here): AuthN/AuthZ, multi-tenant
isolation, CSRF, sandboxing, and SSRF-via-deployment-health-checks are
all `MUST_FIX_BEFORE_PUBLIC_BETA`; rate limiting is `CAN_WAIT for track
A; MUST_FIX_BEFORE_PUBLIC_BETA for B/C`; secrets-in-logs, dependency
audit, and path traversal are `CAN_WAIT (flagged, not scored)`.

## Scope

Matching this program's own named focus areas exactly:

- **B0.1 AuthN** — real user accounts, login/logout, sessions.
- **B0.2 Organizations/Tenants** — a real tenant boundary and per-tenant
  data isolation.
- **B0.3 AuthZ** — coarse role-based route guards.
- **B0.4 CSRF** — token issuance/validation on every mutating route.
- **B0.5 Rate limiting** — per-identity/per-IP throttling.
- **B0.6 Sandbox boundary** — mandatory (not opt-in) sandboxing once
  tenant-supplied code executes.
- **B0.7 Secrets boundary** — encrypted, tenant-scoped secret storage +
  log/transcript redaction.

*(Numbering corrected during B0.2 implementation — see the note at the
top of "Proposed architecture per sub-area" below for what was wrong
and why.)*

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
   **permanently** supported deployment targets, not merely "from
   B0.1 onward" — see ADR-004 (below) for the resolved decision and
   its exact scope.
3. **Reuse existing infrastructure over inventing new mechanisms**:
   - `repositories` is already the one place every other table's
     `project_id` FKs through (E1.6's own convention) — adding a single
     `repositories.organization_id` column is the one scoping lever
     that transitively tenant-scopes the entire existing schema,
     without touching 50 other tables' own FKs.
   - `SandboxManager`/`SandboxRuntimeService`'s actual sandboxing
     mechanism is reused as-is; B0.6 only flips the opt-in policy check
     to mandatory when `AUTH_MODE=required`.
   - Prefer a small number of shared, reusable primitives (in the same
     spirit as A1's own `RequestMemo`) over hand-editing every route —
     e.g. one `require_role()` FastAPI dependency, one scoping helper
     for tenant-filtered queries, applied uniformly.
4. **No security theater** — every gate must be real and covered by a
   real, disposable-fixture test (the same "golden fixture" discipline
   P0/A1 already established), not asserted by inspection alone.

## Proposed architecture per sub-area

**Numbering correction, made during B0.2 implementation (real evidence,
not a style change)**: this section's own sub-headers originally read
B0.1 AuthN / B0.2 AuthZ / B0.3 Organizations / B0.4 Sandbox / B0.5 CSRF
/ B0.6 Rate limiting / B0.7 Secrets — inconsistent with the canonical
`## Phasing` list below (B0.2 Organizations / B0.3 AuthZ / B0.4 CSRF /
B0.5 Rate limiting / B0.6 Sandbox), which several ADR passages already
correctly followed (e.g. ADR-003's own "B0.3 AuthZ then B0.4 CSRF")
while others incorrectly followed this section's original, wrong
numbering — a real, internal inconsistency spanning both this section
and scattered ADR cross-references, not merely a cosmetic mismatch.
Corrected here (this section reordered/relabeled to match Phasing) and
at every affected cross-reference throughout the document; no
technical content changed, only which number each sub-area is called.

### B0.1 AuthN -- IMPLEMENTED
Login mechanism **resolved — see ADR-002** below: email magic-link at
launch, `password_hash` kept nullable on `users` for a purely-additive
password option later, API tokens for service/automation accounts, and
a self-hosted first-admin console-token bootstrap so `AUTH_MODE=
required` never hard-requires SMTP just to get started. Implemented in
`app/services/auth_service.py`, `app/services/email_sender.py`,
`app/services/csrf.py`, migration 32 (`app/db.py`), and the `/auth/*`,
`/account`, `/api/whoami` routes in `app/main.py` — see the B0.1
implementation report for the full file/test map. New tables
`users(id, email, password_hash, created_at, ...)` and `login_tokens
(id, user_id, token_hash, created_at, expires_at, used_at)`; session
issuance via Starlette's built-in `SessionMiddleware` (itsdangerous --
**correction**: verified NOT actually transitively installed, added as
an explicit direct dependency instead, see the corrected audit table
row above) for the signed session cookie — separate from the
short-lived login token itself. Login/verify/logout routes; a
`current_user` FastAPI dependency injected wherever `AUTH_MODE=
required`; a no-op fallback (today's exact
behavior) wherever it's `none`.

### B0.2 Organizations/Tenants -- IMPLEMENTED
New tables (migration 33, `app/db.py`): `organizations(id, name, slug,
created_at, created_by_user_id)`, `organization_members(org_id,
user_id, role)` (role is a real, enforced `CHECK` constraint --
OWNER/ADMIN/MEMBER/VIEWER), `organization_invitations` (hashed,
single-use, 7-day TTL, mirroring `login_tokens`' own discipline
exactly); `repositories.organization_id` FK, nullable (see Design
Principles #3 for why this is the single leverage point -- no other
table gained an `organization_id` column). Implemented in `app/
services/organization_service.py` (`OrganizationService`: org
creation, membership, invite/accept/revoke, role changes with a
last-owner-can-never-be-removed invariant, and repository link/unlink
with cross-org-conflict rejection) and the `/orgs/*` routes in
`app/main.py`. `OrganizationService.member_role()` is the one
data-layer boundary primitive every `/orgs/{id}/*` route's own guard
is built on -- a non-member gets 404 (existence-hiding), never a
403 that would confirm the org id is even valid. `_require_manage_role`
(OWNER/ADMIN only) gates invite/remove/link actions at the service
layer itself, never only hidden in the template. An idempotent
`migrate_existing_data()` backfills a pre-existing B0.1 single user
(and their repositories) into a personal organization on first
`AUTH_MODE=required` startup after this migration ships -- and
explicitly refuses to guess (leaves data untouched, logs a warning)
if ownership is ever ambiguous. See the B0.2 implementation report for
the full file/test map and the exact B0.2/B0.3 boundary this session
drew (no retrofit of the 143 pre-existing engineering-lifecycle
routes -- that general per-route AuthZ sweep stays B0.3's own scope).

### B0.3 AuthZ -- IMPLEMENTED
Coarse per-organization roles (OWNER/ADMIN/MEMBER/VIEWER), enforced by
one general `require_role(kind, param, min_role)` FastAPI dependency
factory in `app/main.py`, resolving against `app/services/
authz_service.py`'s `AuthzService` -- one small resolver per distinct
mutating-route entity kind (task, change, workspace, integration,
sandbox, agent_session, deployment, release, incident, finding, plan,
spec_proposal, product_acceptance, test_case_spec, work_product,
execution_wave, human_decision, repository), each grounded directly in
the real FK chain read out of `app/db.py`'s own CREATE TABLE
statements, walking back to one or more `repositories.id` and then to
`organization_id` -- the same single tenant-scoping lever B0.2
established, never a second copy of tenant identity anywhere else.

All 157 mutating routes in `app/main.py` are covered: 131 sub-resource
routes (an id already in the URL path) via a mechanical `Depends(
require_role(...))` sweep; 12 body-based `create` routes (no id exists
yet to build a path dependency against -- `/api/repositories`,
`/api/tasks`, `/api/tasks/create`, `/api/tasks/new-with-workspace`,
`/api/workspaces`, `/api/incidents`, `/api/integrations`,
`/api/releases`, `/api/work-products`, `/api/changes`, `/changes`,
`/api/engineering/validate-assignment`) via equivalent inline
`_require_org_role_for_repo`/`_require_org_role_for_repos`/
`_require_org_role_for_entity`/`_require_login_only` calls at the top
of each handler; the remaining 14 (`/auth/*`, `/account*`, `/orgs/*`)
already carry their own B0.1/B0.2 guard and are unchanged.
`tests/test_b03_authz.py::test_every_mutating_route_carries_authz_or_
is_accounted_for` is the mechanical completeness proof -- it walks
`app.routes` itself and fails if any future mutating route is added
with no guard and no explicit allowlist entry, so this sweep cannot
silently regress.

A resource resolving to more than one organization (a cross-repository
Task -- see AGENTS.md's own "a Task may span many repositories")
requires `min_role` in **every** resolved organization, not just one.
A resource resolving to zero organizations (an unlinked repository, an
orgless BACKLOG Task) fails closed (404) rather than being treated as
open to anyone -- the same existence-hiding precedent B0.2 established
for `/orgs/{id}/*` (non-member/non-existent -> 404; member but
below `min_role` -> 403). No identified user at all is 401 on these
JSON/API routes (never a redirect, unlike `/orgs/*`'s own HTML-page
`_org_context`). No-op under `AUTH_MODE=none`, the same precedent
`current_user()`/`require_csrf` already established -- proven by both
the existing 943-test suite (all `AUTH_MODE=none`, unmodified) and this
file's own direct `AUTH_MODE=none` checks.

**Known, deliberately deferred residual risk**: this sweep covers
*mutating* routes only, matching this section's own original "wraps
every mutating route" scope and the B0.3 authorization's own repeated
"mutating HTTP surface" framing. Read (`GET`) routes are NOT
cross-org-scoped by this phase -- a member of one organization who
guesses/enumerates another organization's numeric resource id via a
`GET` route can still read that resource's data today. This is a real,
known gap, not an oversight; closing it (read-path tenant scoping)
is unscoped work for a future phase, called out explicitly here rather
than silently left implicit.

### B0.4 CSRF -- IMPLEMENTED
In-house double-submit-cookie CSRF (`app/services/csrf.py`, unchanged
primitive from B0.1) now validated on all 143 pre-existing mutating
routes, folded into the exact same B0.3 sweep rather than a second
pass: the 131 path-param routes get it as the last step inside
`require_role()`'s own dependency (checked only once identity+role are
already confirmed, so an unauthenticated/wrong-org caller still gets
the 401/404/403 that actually describes their situation); the 12
body-based `create` routes get a small `_mutating_csrf` wrapper as
their own `Depends()` (FastAPI always resolves a route's declared
dependencies before its body runs, so for these 12 specifically CSRF
is necessarily checked *before* the inline AuthZ call in the body --
documented at both call sites, not a silent inconsistency). A new
`require_csrf_unless_bearer` (`app/services/csrf.py`) skips the check
entirely for a Bearer/API-token request (ADR-003's own structural-
immunity reasoning: no ambient browser credential, nothing to forge).

Client-side: rather than hand-editing the ~150 pre-existing
`<form method=post>` tags and 5 `fetch()` call sites across ~20
templates, one capture-phase `submit` listener plus a wrapped
`window.fetch` in `base.html` (every page extends it) inject the
current session's token into any current *or future* mutating
form/fetch automatically -- a new template needs no CSRF-specific code
at all. The token itself comes from a new Jinja global
(`templates.env.globals["issue_csrf_token"]`, gated on
`AUTH_MODE=="required"`, `""` otherwise) so no route handler needed
editing either.

**Login CSRF gap, closed**: `/auth/verify`'s POST (the step that
actually creates a session) had no CSRF guard at all before this --
letting an attacker force a victim's browser to submit the
*attacker's own* real magic-link token, silently logging the victim in
as the attacker (a session-fixation-via-forced-login class of bug).
Fixed with the same double-submit primitive one step earlier than
usual: the GET peek page now mints (or reuses) this anonymous
session's own CSRF token via `issue_csrf_token()` -- SessionMiddleware
is installed for every request under `AUTH_MODE=required` regardless
of login state, so a real, unguessable-cross-origin session already
exists the moment the page is first viewed -- and the POST now carries
a plain `Depends(require_csrf)` (never Bearer-eligible, so no
`_unless_bearer` needed). Real regression test:
`tests/test_b04_csrf.py::test_login_csrf_forged_post_without_token_is_
rejected`.

No-op under `AUTH_MODE=none` -- both `require_role`'s own auth_mode
gate and `_mutating_csrf`'s matching one keep the 12 pre-existing
`create` routes (e.g. `/api/repositories`, `/api/tasks`) fully working
exactly as before; a **real bug found and fixed in this same session**
was that a first attempt used the bare `require_csrf_unless_bearer` as
those 12 routes' own `Depends()`, which -- unlike `require_role`'s own
internal gate -- only prevents crashing under `AUTH_MODE=none` (a
clean 404, matching `/auth/logout`'s own precedent) rather than truly
passing through, silently 404-ing every one of those 12 real,
heavily-used production routes under the default self-hosted mode.
Caught by this file's own `test_b03_authz.py::test_auth_mode_none_
sample_routes_unaffected` before it ever reached the full regression
suite; fixed by giving these 12 routes their own `_mutating_csrf`
wrapper with the same auth_mode-first gate `require_role` already
established.

### B0.5 Rate limiting -- IMPLEMENTED
`slowapi` (ADR-003's own resolved choice), in-memory backend, keyed by
the real ASGI peer address (`get_remote_address` -- never a client-
spoofable `X-Forwarded-For`, since this deployment has no reverse
proxy in front of it yet, ADR-003's own flagged residual). Scope, per
this phase's own explicit authorization: the named abuse-sensitive
auth/magic-link/bootstrap/invite/org/token routes -- `/auth/login`
(5/min) and `/auth/bootstrap` (5/min) shipped with B0.1;
`/auth/verify` (10/min), `/orgs` create (10/min), `/orgs/{id}/invite`
(20/min), `/orgs/invitations/{token}` accept (10/min), and
`/account/api-tokens` create (20/min) land here. **Not** a blanket
sweep across all 143 mutating routes -- that general middleware
rollout this section's own text originally described remains
unscoped, explicitly deferred future work, not silently done.

### B0.6 Sandbox boundary -- IMPLEMENTED
`SandboxManager`/`SandboxRuntimeService` (the long-running, docker-
compose-based runtime sandbox feature) turned out to be the WRONG
mechanism to reuse here -- it's an opt-in, health-checked persistent
service, not a "run one PROJECT.yaml command and get its exit code"
primitive. Built instead: `SandboxRuntimeService.run_ephemeral()` (a
real, disposable `docker run --rm` per command -- `--memory`/`--cpus`/
`--pids-limit` cgroup caps, `--network none` by default, `--cap-drop
ALL --security-opt no-new-privileges`, one bind mount of the exact
worktree/probe directory, real timeout-then-kill-then-remove cleanup)
and `SandboxedCommandRunner` (`app/services/sandboxed_exec.py`), the
one shared chokepoint `TestRunner`'s preflight/test stages,
`GateWaiverService`'s baseline-probe re-run, and the hardware-firmware-
build route all now go through, replacing each one's own
`subprocess.run(..., shell=True)`. Direct-host under `AUTH_MODE=none`
(unchanged); mandatory ephemeral-container isolation under
`AUTH_MODE=required` (never a silent unsandboxed fallback). A repo
declares its own execution image/network/resource caps via an
additive `exec_sandbox:` PROJECT.yaml block (`project_contract.
load_exec_sandbox`); one without it still gets a real, safe default
profile (`python:3.12-slim`, `network: none`), never "sandboxing not
required for this repo." **Real bug found and fixed in this same
session**: the timeout-cleanup path's `docker rm -f` can transiently
race a container mid-transition into Docker's own "Dead" state and
fail silently (`SandboxRuntimeService._run` never inspected
returncode) -- fixed with a short bounded retry; caught by this
phase's own adversarial cleanup test before it ever reached production.

### B0.7 Secrets boundary -- IMPLEMENTED
The general primitive: `org_secrets`/`secret_access_log` (migration
34), `SecretsService` (`app/services/secrets_service.py`) -- real
envelope encryption via `cryptography`'s `Fernet`/`MultiFernet` (a new,
justified dependency-minimalism exception, same precedent as ADR-003's
own slowapi/itsdangerous — Python's stdlib has no AEAD primitive at
all, and hand-rolling one is exactly the class of mistake this
codebase's own GitHub-integration docstring already refuses to make
elsewhere), app-wide master key(s) in `Settings.secret_encryption_keys`
(`WORKSPACE_MANAGER_SECRET_ENCRYPTION_KEYS`, never in the database,
same "REFUSED, never guessed" startup discipline as `session_secret`),
real key rotation (`MultiFernet` + `re_encrypt_all()`). No plaintext
ever touches a log/audit row -- `secret_access_log` is metadata-only
(actor/action/timestamp). `/orgs/{id}/secrets` routes (list/create/
rotate/revoke/reveal) are OWNER/ADMIN-only, CSRF-guarded, rate-limited,
reveal-once (matching B0.1's own API-token precedent).

A pattern-based redaction layer (`app/services/secret_redaction.py`)
is wired into the agent-session transcript persist path
(`AgentSessionManager.persist_tail`) and every `SandboxedCommandRunner`
result (test output, gate-baseline re-runs, firmware builds) --
unconditional, regardless of `AUTH_MODE` (a strict improvement over
today's behavior, no new gate).

**GitHub-integration architecture is resolved — see ADR-001** below,
but this phase implements a deliberately **simplified, explicitly
scoped interim consumer**, not the full design: a single stored
Personal-Access-Token per organization (`SecretsService`, name
`github_token`), injected just-in-time per subprocess call
(`github_merge_service.py`'s `token_runner`/`make_hosted_runner` --
`GH_TOKEN` env var for `gh` CLI calls, the `GIT_CONFIG_COUNT`/
`_KEY_n`/`_VALUE_n` environment-variable mechanism for plain `git`
calls, never argv-embedded, never written to disk), never the full
GitHub-App-per-org architecture (App registration, JWT signing,
short-lived installation-token minting, webhook lifecycle) ADR-001
actually designs. That full design needs a real, externally-registered
GitHub App's private key this implementation session has no way to
obtain or safely fabricate as genuine evidence -- explicitly flagged
as a residual gap for a real hosted deployment to close before
onboarding real multi-org GitHub traffic, not silently substituted.
Same public method surface, same `runner` DI seam ADR-001's own
"Migration path" already describes, so swapping in a real App-based
runner later needs no call-site change.

## ADR-001: GitHub authentication/authorization architecture for hosted multi-tenant mode

**Status: DECIDED (design only — not implemented).** Resolves Open
Decision #3 below. Scope: how `GitHubMergeService` authenticates once
`AUTH_MODE=required` and B0.2's organizations exist; `AUTH_MODE=none`
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
natively matches B0.2's organization boundary, mints tokens on demand
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
`repositories.organization_id` (already B0.2's own column) resolves a
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
  a B0.3-guarded (admin-only) callback route stores it on the matching
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

## ADR-002: Password vs passwordless authentication for B0.1

**Status: DECIDED (design only — not implemented).** Resolves Open
Decision #1 below. Scope: how a human establishes their first
authenticated session under `AUTH_MODE=required`. `AUTH_MODE=none` is
unaffected — no login of any kind exists in that mode, unchanged.

### Grounding

Same dependency-minimalism evidence B0's own audit and ADR-001 both
already established: `pyproject.toml`'s 7 core dependencies include no
password-hashing library (no `passlib`/`bcrypt`/`argon2-cffi`) and no
OAuth client library, but Starlette's `SessionMiddleware` (itsdangerous,
signed cookies) ships transitively via FastAPI, unused today — usable
for session issuance at zero new-dependency cost regardless of which
login mechanism is chosen. ADR-001 already recommended in-house
implementations over new libraries twice for the same
dependency-minimalism reason; that pattern carries into this decision.

### Options compared

| | Password + hashing + reset | Email magic-link (passwordless) | External OIDC/social (e.g. "Sign in with GitHub") |
|---|---|---|---|
| Threat model | Attacker needs the password itself: breach, brute force, or credential stuffing | No static secret exists to steal, breach, or reuse | Delegates credential custody entirely to the IdP; ProjectFlow never touches a secret |
| Account takeover risk | Structurally the highest — inherits whatever password-reuse habits the human has, the dominant real-world breach vector industry-wide | Structurally the lowest of the three login-only options — nothing to reuse across sites | Low, inherited from the IdP's own account-security posture |
| Phishing/replay | Phishable and, once phished, reusable indefinitely until changed | Phishable in principle, but single-use + short TTL bounds the damage to one session; **real failure mode**: corporate email-security scanners auto-"click" links to pre-scan them, silently consuming a single-use link before the real user does — must be mitigated by an explicit confirm-click landing page, not auto-auth on raw GET | Mature OAuth phishing surface (fake consent screens); well-tooled mitigations exist (state, PKCE) |
| Email dependency | Only for account recovery — login itself keeps working if outbound email is down | **Hard dependency for every login**, not just recovery | None for login itself |
| Credential storage | New: a real password-hashing library (argon2id/bcrypt) + `password_hash` column | None: single-use tokens, hashed at rest, short TTL — no persistent secret | None: only OAuth state/session data |
| Recovery | Needs a full separate "forgot password" flow — which is itself basically a magic-link mechanism bolted on as a second path | Login **is** the recovery flow, always — no separate mechanism to build | Recovery is the IdP's own responsibility |
| MFA compatibility | Most standard/familiar layering (password + TOTP/WebAuthn) | Fully compatible but a less common UX pattern (challenge after link-click); less urgent since there's no static secret to protect | Inherited from the IdP for free — the strongest MFA story of the three, at zero implementation cost to ProjectFlow |
| Tenant admin onboarding | Needs an initial-password or temp-password-plus-forced-reset step | "Enter your email" only — lowest friction | "Click sign in with GitHub" — comparably low friction, plus likely overlap with users who already have a GitHub account (this product's own domain) |
| Self-hosted usability | **Works with zero external dependency** — no SMTP needed at all | Needs a self-hoster to configure outbound SMTP/a transactional-email service even to log in the first admin — a real, concrete setup burden this project's self-hosted-first identity should weigh heavily | Needs an OAuth App registration with the provider — comparable setup shape to ADR-001's own GitHub App step, so not a new kind of burden for an operator already doing that |
| Operational burden | Hashing library + brute-force rate limiting on failed attempts + a reset-email sender | Email sender + rate limiting on link *requests* (anti-email-bombing) — no hashing, no separate reset flow | OAuth flow correctness (redirect/state/token exchange) + no email sender needed |
| Auditability | Equivalent — a login event either way | Equivalent | Equivalent, plus a free correlation to the user's real GitHub identity |
| Session lifecycle | Identical once established — not a differentiator between options | Identical once established | Identical once established |
| Rate-limiting implications | Must rate-limit failed-login attempts per account/IP (brute force) | Must rate-limit link *requests* per email/IP (inbox-bombing, timing-based enumeration) — needs to exist even before B0.5's general middleware ships, or launch has an open email-bombing vector | Mostly upstream (the IdP's own login throttling); ProjectFlow still needs to rate-limit its own callback endpoint |
| Account enumeration | Solved by the standard "invalid email or password" (never reveal which) pattern — familiar, well-understood | Needs equal care: the "check your email" response must be **identical** regardless of whether the account exists, or the request-link endpoint itself becomes an enumeration oracle | Not applicable in the same way — the IdP handles its own account existence privately |
| Service/non-human accounts | Not naturally suited (a shared service password is an anti-pattern) | Not naturally suited (CI can't click an email) | Not naturally suited |
| Enterprise readiness | Neutral-to-negative — many enterprise security policies now actively discourage bare passwords given credential-stuffing prevalence | Neutral-to-positive as a bridge; true enterprise readiness is SSO/SAML federation (already a B0 non-goal) | A reasonable bridge given this product's GitHub-centric domain, but couples account identity to a third-party provider outages/renames can disrupt, and doesn't satisfy "the org's own IdP" the way real SSO would |
| Migration path | Additive either way — a nullable `password_hash` column doesn't block adding magic-link later, or vice versa | Additive either way | Additive; can be layered on once B0.1's session-issuance plumbing exists regardless of which login method shipped first |
| Dependency footprint | New: a real hashing library not currently in `pyproject.toml` | None beyond stdlib `secrets` + the already-available `itsdangerous`/`SessionMiddleware` | A small OAuth client (a modest library, or in-house state/PKCE handling — comparable to magic-link's footprint, more moving parts than pure magic-link) |

A fourth, genuinely justified hybrid is the actual recommendation below
— not merely "offer all three."

### Recommendation

**Email magic-link as B0.1's only human-login mechanism at launch.**
Password authentication is explicitly **deferred**, not rejected
forever (see "What B0.1 defers" below). External OIDC/social login
("Sign in with GitHub") is explicitly **deferred** despite being a
strong domain fit, in favor of shipping the simpler mechanism first.

Two service-non-human-account gaps neither option solves are called
out explicitly and included in B0.1's scope regardless of the human-
login decision:

- **API tokens for service/automation use** (CI pipelines, scripted
  API access) — a third, separate mechanism (`Authorization: Bearer
  <token>`, checked independently of the human-session cookie), issued
  per-user, revocable, never a shared credential. In scope for B0.1
  from the start, since it's needed no matter which human-login option
  is chosen.
- **Self-hosted first-admin bootstrap without requiring SMTP** — a
  one-time setup token printed to the process's own stdout/log on
  first boot when `AUTH_MODE=required` and no users exist yet (the
  same pattern several other well-known self-hosted tools use for
  their own first-admin setup) — so a self-hoster choosing hosted-style
  auth for their own instance is never forced to stand up email
  delivery just to create the first account.

### Rationale

Weighing the comparison above against this specific project's own
already-demonstrated constraints (not a generic best-practice
default):

1. **Dependency-minimalism, established twice already** (the audit's
   own findings, then ADR-001's own recommendation): magic-link adds
   zero new dependencies (stdlib `secrets` + the already-available
   `SessionMiddleware`); password auth requires a real hashing library
   this project has never needed before.
2. **Removes the dominant real-world account-takeover vector
   structurally**: credential stuffing/reuse is the single most common
   breach pattern industry-wide, and magic-link has no static secret
   for it to act on at all — a stronger default security posture than
   password + hashing, without needing to get hashing-parameter/reset-
   flow details right.
3. **Recovery is free**: a password system needs a whole second
   secret-delivery mechanism (forgot-password) that is itself
   basically a magic-link bolted on — building magic-link as the
   primary path means never building that second mechanism at all.
4. **The self-hosted-SMTP cost is real and is explicitly paid down**,
   not ignored: the first-admin console-token bootstrap (above) means
   a self-hoster never *needs* SMTP just to start using
   `AUTH_MODE=required` — they can configure email later, once real
   multi-user usage justifies it, or never if `AUTH_MODE=none` remains
   their permanent choice (the default, unaffected either way).
5. **Social login is a good idea, not yet a necessary one**: "Sign in
   with GitHub" is a strong fit for this product's domain and is worth
   real consideration in a later B0.1.x pass, but it adds OAuth-flow
   complexity (redirect handling, state/PKCE, third-party outage
   coupling) that isn't justified when magic-link already satisfies
   B0.1's actual requirement with less moving parts.

### What B0.1 should include now vs. defer

**Include now:**
- Magic-link request/verify flow, with the phishing mitigation above
  (explicit confirm-click landing page, never auto-authenticate on a
  raw GET request) and the enumeration mitigation above (identical
  response whether or not the email is registered).
- Login tokens stored **hashed**, single-use, short TTL (minutes, not
  hours) — the same "never store the raw secret" discipline ADR-001
  already applied to GitHub installation tokens.
- Session cookie via Starlette's `SessionMiddleware` once
  authenticated (separate concern from the login token itself — a
  normal-lifetime, e.g. 30-day, refreshed-on-activity session, not
  re-derived from the short-lived login token).
- A minimal, launch-blocking rate limit on the link-*request* endpoint
  specifically (per-email and per-IP) — this cannot wait for B0.5's
  general middleware, since an unthrottled link-request endpoint is
  both an inbox-bombing vector and a timing-based enumeration oracle
  from day one.
- API tokens for service/automation accounts (see Recommendation
  above) — needed regardless of the human-login mechanism.
- The self-hosted first-admin console-token bootstrap (see
  Recommendation above).

**Defer:**
- Password authentication as an *additional*, optional login method —
  worth adding later if real users specifically request it (e.g. for
  password-manager/enterprise-policy compatibility), at the cost of
  the hashing dependency and a real reset flow. The `users` table
  should keep `password_hash` nullable from the start so this remains
  purely additive whenever it happens, not a schema migration fight.
- External OIDC/social login ("Sign in with GitHub" or others) — a
  strong later candidate, plausibly bundled with, or immediately
  following, true enterprise SSO/SAML work (already a stated B0
  non-goal).
- MFA (TOTP/WebAuthn) as a second factor on top of magic-link — real
  and worth doing, but less urgent than for a password system (no
  static secret to protect against reuse), deferred to a B0.1.x pass.

## ADR-003: In-house vs library for CSRF and rate limiting

**Status: DECIDED (design only — not implemented).** Resolves Open
Decision #2 below.

### Grounding (evidence, not assumptions)

- **Zero middleware registered today**: `grep -n "middleware\|add_middleware"
  app/main.py` returns nothing — whatever ships here is the *first*
  middleware this app has ever carried.
- **Zero `Depends()` usage today**: `app/main.py` (4,319 lines) uses no
  FastAPI dependency-injection guards anywhere — CSRF verification (and
  B0.3's own AuthZ guard) would be the first introduction of that
  pattern into this codebase. Both naturally land in the **same
  mechanical sweep**, already sequenced adjacently in Phasing
  (B0.3 AuthZ then B0.4 CSRF, "same route sweep").
- **Real CSRF migration footprint, counted, not estimated**: 143
  POST/PUT/DELETE routes in `app/main.py`; 129 `<form method="post">`
  occurrences across 14 templates (`task_detail.html` alone: 69;
  `sandbox_detail.html`: 12; `workspace_detail.html`: 21); **no shared
  form macro exists** in `app/templates/_macros.html` to centralize a
  hidden CSRF field through — each form needs the field added
  individually, though the consistent `<form method="post"` pattern
  makes this a scriptable, mechanical sweep, not 129 bespoke edits.
  Separately, `base.html`'s own shared JS (`runAutonomousTick`,
  `runReleaseAction`, `launchWorkspace`, `runAutonomousStart`) issues
  `fetch(..., {method:'POST'})` calls with no CSRF header today — a
  *smaller* migration surface than the forms (4 shared functions, not
  129 sites), but a real one.
- **Single-process deployment today**: `scripts/start.sh:47` runs
  `uvicorn app.main:app --host 127.0.0.1 --port 8765` with no
  `--workers` flag — one process, one instance, in-memory state is
  trivially correct today. This is the load-bearing fact for the
  "when must this evolve" question below.
- **No reverse proxy exists anywhere in the repo** (confirmed in the
  original audit) — hosted mode's eventual proxy/load-balancer
  topology, and therefore its `X-Forwarded-For` trust boundary, is
  **undesigned**, not merely unimplemented. Flagged as a residual risk
  below, not invented here.
- **ADR-002 already created a launch-blocking, endpoint-specific rate
  limit requirement** (the magic-link request endpoint) that "cannot
  wait for B0.5's general middleware." Whatever B0.4/B0.5 design is
  chosen here must supply that primitive in time for B0.1's own
  launch, not just as a general B0.4/B0.5 deliverable — a real
  cross-ADR dependency, not a hypothetical one.
- **Bearer-token (API/service-account, from ADR-002) and webhook
  (`POST /webhooks/github`, from ADR-001) requests are structurally
  immune to CSRF** — CSRF exploits a browser automatically attaching
  *cookies* to a cross-site request; a request authenticated via an
  `Authorization: Bearer <token>` header or an HMAC signature carries
  no ambient credential a malicious page could ride on. CSRF
  verification must apply **only** to session-cookie-authenticated
  (browser) requests — applying it to Bearer/webhook routes would be
  both incorrect and would break legitimate API/CI/webhook traffic.
- **Dependency list remains minimal** (`pyproject.toml`): fastapi,
  uvicorn, jinja2, python-multipart, PyYAML, websockets, ruamel.yaml —
  same 7 core dependencies referenced in the original audit and both
  prior ADRs.

### Options compared

**CSRF:**

| | In-house (double-submit token) | Library (e.g. a Starlette/FastAPI CSRF middleware from the ecosystem) |
|---|---|---|
| Security correctness | Correct if implemented carefully (constant-time compare, SameSite cookie, verified on every state-changing method) — but the burden of getting every detail right (and keeping it right as routes are added) falls entirely on this project | A maintained library has already had these details reviewed/exercised by a wider user base — lower first-implementation risk, but only as good as its own upkeep |
| Bypass risk | Real if a future route is added without remembering the guard — same class of risk B0.3's AuthZ sweep already carries, mitigated the same way (a test asserting every mutating route carries the dependency, not spot-checked) | Same bypass risk shape (a forgotten route is a forgotten route either way) — the library doesn't remove the need for a completeness test, it only reduces bugs *within* the check itself |
| Same-site/cookie implications | Full control — can set `SameSite=Lax` (or `Strict`) directly alongside the session cookie from B0.1, tuned to this app's own login/redirect flows (magic-link verify is itself a cross-site-ish redirect from an email client, worth testing explicitly) | Depends on the library's own defaults and how configurable they are; still needs the same explicit testing against the magic-link redirect flow either way |
| Testability | Trivial with this project's own established `TestClient`-based test style (`tests/conftest.py`'s `client` fixture) — a plain function, easy to unit-test in isolation and to assert against in integration tests | Same `TestClient` compatibility (any real Starlette middleware/dependency is testable that way) — not a differentiator given this project's existing test infra already fits either |
| Dependency maintenance/CVEs | None — nothing to track | A small, less-widely-used CSRF library for FastAPI adds a dependency whose maintenance cadence and CVE history this project has not audited live in this pass (see Residual risks) — a real, if modest, ongoing-maintenance and supply-chain surface P0's own audit already flagged generally (`docs/PRODUCTIZATION_AUDIT.md`'s "Dependency/supply-chain audit: CAN_WAIT (flagged, not scored)") |
| Operational complexity | Low — one dependency function + one template-global, matching the project's own existing `templates.env.globals["pf_t"]` pattern from Track A1 | Low-to-moderate — one more `pip install`, one more version to track through future Python/FastAPI upgrades |
| Migration cost | Same either way — the 129-form + 4-JS-function sweep above is identical regardless of who wrote the verification logic | Same |

**Rate limiting:**

| | In-house (token bucket) | Library (e.g. `slowapi` or similar ecosystem middleware) |
|---|---|---|
| Security correctness | Straightforward algorithm (token bucket / sliding window), well-understood, easy to get right for the simple per-key cases this app needs | A maintained library likely covers more edge cases (burst handling, multiple simultaneous limit tiers) out of the box |
| Persistence/backend needs | In-memory dict is correct **only** as long as deployment stays single-process (true today, see Grounding) | The common libraries in this ecosystem (e.g. `slowapi`, built on the `limits` package) support pluggable backends including in-memory *and* Redis — meaning adopting one now costs nothing extra today but removes a later migration step if horizontal scale arrives |
| Distributed/multi-instance behavior | **Breaks silently** the moment a second process or machine is added — each instance enforces its own independent limit, effectively multiplying the real limit by instance count, with no error, no crash, just a quietly-wrong security control | A library with a Redis (or similar shared-store) backend is correct across instances *if* that backend is actually configured — still requires standing up Redis (or equivalent) for hosted mode, which is new infrastructure either way |
| Trusted proxy/IP handling | Must be built explicitly — deciding which header (if any) to trust for the real client IP behind a future reverse proxy is unresolved regardless of in-house vs library (see Grounding: no proxy topology exists yet) | Same unresolved dependency — a library doesn't invent a trust boundary this project hasn't decided on; most libraries expose a configurable "IP extraction" hook but someone still has to decide what to trust |
| Tenant/user/IP/key dimensions | Straightforward to key on whatever's needed (IP for anonymous/pre-auth requests like magic-link requests, user id or org id post-auth, API token id for service accounts) — this app's own multi-dimension needs (anonymous vs authenticated vs API-token traffic) are simple enough not to need a general-purpose framework | Equally capable, usually with a cleaner declarative syntax for "N requests per M seconds per key-function" |
| Endpoint-specific limits | Needs its own small per-route configuration mechanism, built once | Typically a first-class, well-tested feature (per-route decorators/limits) |
| Failure behavior | Whatever this project chooses to build — must be decided explicitly (see Recommendation) | Typically returns HTTP 429 with a `Retry-After` header by convention — a sensible default this project can simply adopt regardless of which implementation is chosen |
| Operational complexity | Low today (in-memory, no new infra); **hidden complexity deferred to whenever horizontal scale arrives**, at which point it becomes a forced rewrite under pressure rather than a planned migration | Slightly higher upfront (one more dependency + eventually Redis for hosted mode) but the multi-instance-correct path is already paved when that day comes |
| Dependency maintenance/CVEs | None | Same caveat as CSRF above — not live-audited in this pass |

### Recommendation

**In-house for CSRF. Library (specifically the `limits`-package-backed
family, e.g. `slowapi`) for rate limiting, with the in-memory backend
at B0 launch.** This is the justified hybrid from the options list —
not a default toward "in-house is simpler" across the board, and not a
default toward "always adopt a library" either; the two concerns have
genuinely different risk shapes once grounded in this project's actual
deployment reality.

### Rationale

1. **CSRF's correctness surface is small and fully owned by this
   project's own routing/templating conventions** (Jinja templates via
   the same `pf_t`-style global pattern Track A1 already established;
   FastAPI dependencies for the JSON/fetch-based mutation routes) — a
   double-submit-cookie check is a few dozen lines with no external
   state, no backend, and a bypass risk that a completeness test (not
   library choice) is what actually closes. Adding a dependency here
   buys little beyond what careful, tested in-house code already
   provides, at the cost of one more supply-chain surface for a
   security-critical, easy-to-get-right primitive.
2. **Rate limiting's correctness surface is NOT small once multi-
   instance deployment is real** — an in-house in-memory limiter is
   correct today (single process, confirmed in Grounding) but becomes
   **silently, dangerously wrong** the moment hosted mode scales
   horizontally, with no error to signal the break. A library already
   built around a pluggable backend (memory now, Redis later) converts
   a future forced-rewrite-under-pressure into a planned backend swap
   — the asymmetry in downside risk (CSRF: a caught bug vs. rate
   limiting: a silent security-control failure at exactly the moment
   real scale makes it matter most) is what tips this one toward
   adopting a maintained implementation despite the project's
   otherwise-consistent dependency-minimalism (already the stated
   reasoning in ADR-001 and ADR-002, not abandoned here — see Residual
   risks for the honest tension this creates).
3. **The in-memory backend is the correct default at launch either
   way** — today's single-process reality (Grounding) means adopting a
   library does not require standing up Redis on day one; it only
   means the *option* to swap the backend exists without a rewrite
   when it's actually needed (see "When this must evolve" below).

### Exact boundary: what is in-house vs. adopted dependency

- **In-house**: CSRF token generation/verification (double-submit
  cookie + a FastAPI dependency + the `pf_t`-pattern template global
  for the hidden field), the CSRF-exemption logic for Bearer-token and
  webhook-signature-authenticated routes (see Grounding), and the
  route-completeness test asserting every mutating route carries the
  guard.
- **Adopted dependency**: one rate-limiting library from the
  `limits`-backed ecosystem family (e.g. `slowapi`), configured with
  its in-memory backend at launch. The exact package is a Phasing-time
  choice (not pinned by this ADR) — the requirement this ADR states is
  "pluggable backend supporting both in-memory and a shared store,"
  not a specific package name.
- **In-house, built early, shared by both**: the *narrow* rate limiter
  needed for the magic-link request endpoint by B0.1's own launch
  (ADR-002's own requirement) should use the **same library**, applied
  to that one route ahead of B0.5's general middleware rollout — not a
  second, separate implementation. This is the one point where B0.1
  and B0.5 must ship in close coordination (see Phasing note below).

### Configuration defaults

- CSRF: `SameSite=Lax` on the session cookie (from B0.1) and the CSRF
  cookie itself; token verified on every state-changing method (POST/
  PUT/PATCH/DELETE) for session-cookie-authenticated requests only;
  GET/HEAD/OPTIONS never require it (never state-changing by this
  app's own routing convention).
- Rate limiting: per-IP limits for unauthenticated endpoints (the
  magic-link request endpoint chief among them), per-user (or
  per-organization, once B0.2 exists) limits for authenticated
  endpoints, per-API-token limits for service/automation traffic
  (ADR-002) — three distinct key dimensions, not one global limit.
  Specific numeric thresholds are deliberately **not** invented here;
  they belong in implementation-time tuning informed by real traffic,
  not asserted as a made-up default in a design document.

### Failure behavior

- CSRF failure: HTTP 403 with a machine-readable reason (matching this
  app's own existing `GitHubIntegrationError`-style typed-error
  convention — see ADR-001), never a silent pass-through.
- Rate-limit exceeded: HTTP 429 with a `Retry-After` header (the
  library's own conventional default, adopted rather than reinvented).
- Both failure paths must be exercised by a real test, not asserted by
  inspection — consistent with this program's own "no security
  theater" design principle.

### When the design must evolve for horizontal scale

**The exact trigger, stated precisely, not left vague**: the moment
hosted-mode deployment adds a second `uvicorn` worker/process or a
second machine — i.e., the moment `scripts/start.sh`'s own current
single-process invocation (Grounding) is no longer literally true for
a given deployment — the rate-limiting backend **must** move from
in-memory to a shared store (Redis or equivalent) before that
deployment change ships, not after. This ADR's library choice exists
specifically so that transition is a configuration change (swap the
backend) rather than a rewrite. In-house CSRF does not have an
equivalent multi-instance failure mode (it depends only on the
request's own cookie/header, never on cross-request shared state), so
it carries no analogous trigger.

### Self-hosted compatibility

Both are `AUTH_MODE`-scoped the same way as every other B0 sub-area:
`AUTH_MODE=none` (today's default, unaffected) never constructs a
session cookie in the first place, so CSRF verification (which only
applies to session-cookie-authenticated requests, see Grounding) has
nothing to guard — the dependency is simply never invoked. Rate
limiting is more permissive to apply even under `AUTH_MODE=none` for
its own sake (protecting a self-hosted instance from accidental
runaway automation is a reasonable default regardless of auth mode),
but is **not required** to, and default thresholds — if enabled at all
in that mode — must be generous enough never to interfere with today's
real, already-verified single-user production usage (Track A1's own
live-verification pass).

### Observability/auditability

Both should log a structured event on rejection (route, key dimension,
identity where available) — reusing this app's own existing
`workspace_events`-style append-only audit convention (`app/db.py:22`)
is a natural fit for CSRF/rate-limit rejections specifically, though
whether they belong in that exact table or a dedicated one is an
implementation-time decision, not resolved here.

### Non-goals (of this decision)

- Specific numeric rate-limit thresholds — implementation-time tuning.
- The exact rate-limiting package name — a Phasing-time choice within
  the stated "pluggable backend" requirement.
- Designing the reverse-proxy/`X-Forwarded-For` trust boundary itself
  — flagged as a residual risk below, not resolved here.
- A general-purpose "any number of limit tiers" framework — this
  app's own three key dimensions (IP / user-or-org / API-token) are
  sufficient; nothing more elaborate is justified by evidence gathered
  so far.

### Residual risks / open questions

- **Dependency CVE/maintenance history for the recommended rate-
  limiting library family was not live-audited in this pass** — stated
  as a factor, not verified against a current advisory database;
  implementation-time must do that check before pinning a version,
  consistent with P0's own flagged-but-not-scored "dependency/supply-
  chain audit" item.
- **Reverse-proxy topology and `X-Forwarded-For` trust are genuinely
  undesigned** (Grounding) — both CSRF's `SameSite` reasoning and rate
  limiting's per-IP keying assume a direct connection or a *trusted*
  proxy; if hosted mode ends up behind an untrusted or misconfigured
  proxy, IP-based keying is spoofable. This needs its own design pass
  before hosted-mode rate limiting can be trusted in production, not
  assumed solved by this ADR.
- **This ADR chose a library for rate limiting specifically because of
  the distributed-correctness asymmetry**, a narrower justification
  than "libraries are generally better" — worth re-examining if this
  project's own dependency-minimalism preference (ADR-001, ADR-002)
  turns out to weigh more heavily in practice than this ADR assumes;
  the in-house alternative remains viable if that preference wins out,
  provided the multi-instance trigger above is treated as a hard
  blocker on horizontal scale, not a known gap left unaddressed.
- **The 129-form CSRF sweep's actual mechanical cost was counted, not
  time-estimated** — a real implementation-planning input for
  whichever engineer picks up B0.4, not resolved further here.

## ADR-004: Is self-hosted single-user mode (`AUTH_MODE=none`) permanent or transitional?

**Status: DECIDED (design only — not implemented).** Resolves Open
Decision #4 below. Scope: whether `AUTH_MODE=none` is a permanently
supported product mode or an eventually-deprecated bootstrap/onboarding
convenience. This decision governs how much long-term weight every
other B0 sub-area's own dual-mode design (Design Principle #2,
restated in every prior ADR's own "self-hosted compatibility" section)
is actually worth carrying — it is the one decision that, if answered
differently, would require revisiting language already committed
across ADR-001, ADR-002, and ADR-003.

### Grounding (evidence, not product opinion)

- **Real, active production usage already exists under `AUTH_MODE=
  none`'s exact real-world equivalent** (today, before B0 exists at
  all): Track A1's own live-verification pass found 7 real Tasks with
  genuine Vietnamese-language titles ("Update password default cho
  mesflow la Admin@123456", "Fix giao diện qa-"), 5 real repositories,
  and real agent-session history in the single production database —
  see `docs/TRACK_A1_PERFORMANCE_AND_SIMPLE_MODE.md`'s own live
  verification report. This is not a hypothetical "someone might
  self-host" scenario; it is confirmed, active, real usage happening
  in this exact deployment right now.
- **The project's own packaging metadata states "local-only" as its
  primary identity, not a caveat**: `pyproject.toml:9`, `description =
  "Local-only agent worktree and integration readiness manager"`. This
  is the project's own self-description, not an external observation.
- **Every prior ADR has already repeatedly promised permanence**, not
  merely "initial" support: ADR-001's "How `AUTH_MODE=none` stays
  isolated" section states self-hosted operators "never need a GitHub
  App... or any B0.7 dependency" (unqualified by time); ADR-002 built
  a first-admin console-token bootstrap *specifically* so self-hosted
  operators "never need SMTP" (a permanent design accommodation, not a
  transitional one); ADR-003's "Self-hosted compatibility" section
  states rate-limit defaults "must be generous enough never to
  interfere with today's real, already-verified single-user production
  usage." Answering this decision "transitional" now would require
  walking back committed language in three already-pushed documents —
  a real consistency cost this ADR weighs directly, not incidentally.
- **Network-boundary enforcement for `AUTH_MODE=none`'s equivalent
  today is already triple-layered and simple** (original audit,
  unchanged by B0): `scripts/start.sh:13-20` hard-refuses any host but
  `127.0.0.1`, `app/config.py:34` defaults to it, the systemd unit's
  own comment reasons about staying local-only. This is a mature,
  already-proven security boundary, not new scaffolding B0 must
  invent.

### Options compared

| | Permanent, first-class mode | Transitional/bootstrap-only mode |
|---|---|---|
| Product | Two real, supported product shapes forever (free/simple local tool + hosted multi-tenant SaaS) — a proven dual-mode pattern used by comparable self-hosted/cloud products in this space | Single-user mode exists only for onboarding/evaluation/dev use, with an explicit sunset; long-run product is hosted-only |
| Architecture | Every future auth-gated feature permanently needs a working, tested no-op/bypass branch for `AUTH_MODE=none` — an ongoing tax on all future work, not just B0 | Eventually simplifies to one code path — but only *after* a real deprecation event; near-term architecture cost during the transitional window is identical to the permanent option, not smaller |
| Security boundary | Stays exactly as simple as it is today (network-only, triple-enforced, already proven) — nothing new accumulates on this path since no auth code ever runs in this mode | Same boundary during the transitional window; disappears only after full sunset, at which point it's replaced by the hosted AuthN/AuthZ boundary entirely |
| Auth/RBAC implications | RBAC code must always tolerate "no `current_user` at all" as a legitimate, permanent state — a discipline every future contributor must maintain indefinitely | RBAC eventually becomes mandatory everywhere once single-user mode is retired, removing the permanent no-op-branch requirement — but only realized after a successful migration of every existing install |
| Upgrade/migration path | No self-hoster is ever forced to adopt hosted-style auth to get a version upgrade — directly continues the promise already made in ADR-001/002/003 | Requires building **both** a real `AUTH_MODE=none -> required` data-migration tool (converting a single-user install into, effectively, a personal organization of one) **and** a deprecation communication/timeline plan — real, non-trivial scope beyond B0 itself, not free |
| Operational burden (on maintainers) | Ongoing, indefinite: both modes tested and supported forever, across every future feature | Lower *eventually* (one path only) but adds a *new*, one-time-or-multi-year burden the permanent option never has: managing the deprecation process itself, including support during the transition and the real risk of a self-hosted install base that simply refuses to migrate, fragmenting on the last supported version (a known failure mode when open-source tools attempt forced cloud-only pivots) |
| Backward compatibility | The whole point of the option — real production users (the one already confirmed above) never have to change how they use the tool, ever | Broken by design, eventually — directly contradicts the unqualified "unaffected"/"never" language already committed in ADR-001/002/003 unless those are revised to add an expiration caveat, weakening this document's own prior commitments |
| Hosted-platform convergence | Never fully converges — the codebase permanently carries two deployment shapes, requiring an ongoing (not one-time) product decision about which features are hosted-only vs. universal | Full convergence eventually (one codebase, one mode) — the architecturally cleanest long-term shape, *conditional on* the migration succeeding without alienating the self-hosted base |
| Testing | Permanently two states to cover (`AUTH_MODE=none` / `required`) for every relevant test, indefinitely | Same two-state burden during the transitional window; only reduces to one state after a successful, completed sunset |
| Documentation | A stable, permanent "self-hosted mode" doc section, written once and kept accurate | Needs an explicit, actively-maintained deprecation notice and timeline — extra, ongoing documentation burden the permanent option never carries |
| Long-term maintenance | Higher in steady state (two paths forever) but bounded and predictable — no migration event ever required | Lower after a successful sunset, but the sunset itself is a real, risky, one-time (or multi-year) project with its own failure modes — not a free simplification |

### Recommendation

**Self-hosted single-user mode (`AUTH_MODE=none`) is a PERMANENT,
first-class, indefinitely supported product mode — not a transitional
bootstrap, not deprecated on any timeline.** This is a confirmation and
formalization of what Design Principle #2 and every prior ADR's own
"self-hosted compatibility" section already assumed, not a new
direction — see Grounding above for why treating it as anything else
now would require walking back already-committed, already-pushed
language.

### Rationale

1. **Real users already depend on it, today, not hypothetically.** A1's
   own live-verification pass is direct, first-party evidence — not a
   projected future user, an actual one, in this exact deployment.
   Choosing "transitional" would mean this specific, already-confirmed
   real user is on a product roadmap toward eventual forced migration
   or feature freeze, which nothing in this program's own instructions
   has ever asked for.
2. **It's the project's own stated identity, not an accommodation.**
   `pyproject.toml`'s own description leads with "local-only" — this
   isn't a hosted product that happens to also support self-hosting as
   a courtesy; local-only is foundational to what this project already
   is.
3. **The transitional option's "eventual simplification" is not free —
   it's a deferred, larger, riskier cost** (a real data-migration tool
   plus a deprecation program plus the real risk of install-base
   fragmentation), while its near-term cost during the transitional
   window is *identical* to the permanent option's steady-state cost.
   There is no scenario in which choosing "transitional" saves
   engineering effort sooner than choosing "permanent" — only a
   scenario where it defers a cost and adds new ones.
4. **Internal consistency**: three already-committed ADRs (001, 002,
   003) already promise unqualified, indefinite self-hosted
   compatibility. Confirming permanence here is what keeps this
   document truthful to itself, per this decision's own explicit
   instruction to remain internally consistent — the alternative would
   require going back and re-qualifying language already pushed to
   `origin/main`.

### What "permanent" means precisely (scope boundary, not a blank check)

Permanent support does **not** mean permanent feature parity with
hosted mode — some B0-and-beyond features are legitimately hosted-only
by their own nature and are never expected to make sense under
`AUTH_MODE=none`:

- **Hosted-only, never backported**: GitHub App installation flow
  (ADR-001 — a single-user, single-`gh`-auth deployment has no
  "installation" concept to speak of), organization/tenant management
  UI (B0.2 — there is exactly one implicit "tenant" in single-user
  mode), any future billing/multi-seat feature.
- **Universal, must always work in both modes**: the core engineering
  lifecycle (E1–E13, entirely unaffected by B0 regardless of this
  decision), Track A1's Simple Mode and performance work, and anything
  B0 itself adds that isn't inherently about multi-party coordination
  (e.g., CSRF/rate-limiting infrastructure exists in the codebase
  either way, simply unexercised when no session cookie is ever
  issued — see ADR-003's own "self-hosted compatibility" section).
- **What "permanent" commits to**: `AUTH_MODE=none` keeps working,
  unmodified in its own observable behavior, for as long as the E1–E13
  engineering lifecycle itself is maintained — not a fixed version
  number or calendar date. It does **not** commit to every future
  hosted-platform feature being made available in single-user mode.

### Consistency updates this decision requires elsewhere in this document

To keep the document internally consistent (this decision's own
explicit requirement), Design Principle #2's phrasing ("both modes are
real, supported deployment targets from B0.1 onward") is tightened
below to remove the ambiguity between "supported starting at B0.1" and
"supported starting at B0.1 and never revisited" — this ADR resolves
that ambiguity toward the latter, explicitly.

### Residual risks / open questions

- **This is a product commitment, not merely a technical one** — it
  constrains future product/business decisions (e.g., a future
  monetization strategy cannot assume every user eventually converts
  to hosted mode). Worth surfacing explicitly to whoever owns
  ProjectFlow's product direction, not just its engineering, before
  B0.1 implementation starts.
- **"Permanent" was scoped above to mean "as long as E1–E13 itself is
  maintained,"** not literally forever in an absolute sense — a
  deliberately honest hedge rather than an unfalsifiable promise; if
  the whole project is ever sunset, this mode is sunset with it, which
  is not a meaningful exception.
- **The hosted-only feature list above (GitHub App flow, org
  management UI) is illustrative, not exhaustive** — each future B0+
  feature still needs its own explicit "does this apply to
  `AUTH_MODE=none`?" judgment call at implementation time; this ADR
  establishes the principle, not a complete enumeration.
- **No opposing product/business argument for a transitional posture
  was raised during this pass** (e.g., a hypothetical case that
  maintaining two modes forever meaningfully slows hosted-platform
  velocity) — if such a case exists, it was not evaluated here and
  would warrant revisiting this ADR specifically, not silently
  overriding it in a later implementation decision.

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

1. ~~**Password vs passwordless (magic-link) authentication** for
   B0.1.~~ **RESOLVED — see ADR-002** (above): email magic-link at
   launch; password kept as a purely-additive future option
   (`password_hash` nullable from the start); social/OIDC login and
   MFA explicitly deferred. As with ADR-001, "resolved" means "a
   concrete, evidence-based recommendation now exists," not "silently
   approved" — still needs explicit human sign-off before B0.1
   implementation starts.
2. ~~**In-house vs adopt a library** for CSRF/rate-limiting.~~
   **RESOLVED — see ADR-003** (above): in-house for CSRF (small,
   fully-owned correctness surface); a maintained pluggable-backend
   library for rate limiting specifically because an in-house
   in-memory limiter breaks silently under horizontal scale — a
   deliberate hybrid, not a uniform default either direction. As with
   ADR-001/002, still needs explicit human sign-off before B0.4/B0.5
   implementation starts.
3. ~~**GitHub auth architecture** once multi-org hosting means
   "delegate to the host's own `gh` CLI" no longer holds.~~
   **RESOLVED — see ADR-001** (above, in the B0.7 section): GitHub App
   per organization, self-hosted `gh` CLI delegation preserved unchanged
   under `AUTH_MODE=none`. Still requires a human sign-off on ADR-001's
   own recommendation before B0.7 implementation starts — "resolved"
   here means "a concrete, evidence-based recommendation now exists,"
   not "silently approved."
4. ~~**Whether self-hosted single-user mode (`AUTH_MODE=none`) is a
   permanent, supported deployment target**, or an eventually-
   deprecated transitional one.~~ **RESOLVED — see ADR-004** (above):
   permanent, for as long as the E1–E13 engineering lifecycle itself is
   maintained — not a fixed date, not a transitional bootstrap. Hosted-
   only features (e.g. the GitHub App install flow, org-management UI)
   are explicitly never expected to be backported, but `AUTH_MODE=none`
   itself is never sunset. As with ADR-001/002/003, still a
   recommendation requiring explicit human sign-off — this decision
   also carries a genuine product/business commitment beyond the
   purely technical ones in ADR-001–003 (see ADR-004's own residual
   risks), worth a deliberate look from whoever owns ProjectFlow's
   product direction before it's treated as final.

## Acceptance criteria template (per sub-program)

Mirroring A1's own finish-conditions discipline:

- A real, disposable, multi-organization fixture (two orgs, two users
  each, cross-org access attempts) proves actual data isolation — not
  merely asserted by code inspection.
- Full existing regression suite (`pytest tests/ -k "not real_"`, 891
  tests as of Track A1, 915 as of B0.1) still passes unmodified with `AUTH_MODE=none`
  — the existing single-user experience must never regress.
- Every new mutating route added or touched carries both an AuthZ
  guard (B0.3+) and a CSRF check (B0.4+) — swept systematically, with
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
