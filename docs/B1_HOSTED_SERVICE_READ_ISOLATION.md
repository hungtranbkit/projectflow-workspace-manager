# B1 — Hosted-Service Read-Path Hardening

**Status: IMPLEMENTED.** Track B (hosted ProjectFlow service), the phase
after B0 (Hosted Platform Security Foundation, `docs/B0_HOSTED_PLATFORM_
SECURITY_FOUNDATION.md`, PASS). No `B1` scope existed anywhere in the
repo before this document — this file was written from a fresh,
evidence-based investigation (see below) before any code changed, per
this program's own standing rule of grounding specs in real code, not
assumption.

## Why this scope, not the one first floated

B0's own closing docs explicitly named one gap it did **not** cover:
"B0.3's AuthZ sweep covers mutating routes only — GET-route cross-org
read isolation is unscoped future work." That gap is real, current,
and — once verified against the live code below — is the single most
severe open issue for Track B: under `AUTH_MODE=required`, a member of
Organization A can read Organization B's Changes, Tasks, Workspaces,
Sandboxes, Releases, Incidents, etc. by URL or by just browsing the
list pages, with zero server-side check. This document scopes and
closes that gap.

Two other candidate B1 items were considered and are **explicitly
NOT** in this phase's scope, with evidence:

- **`/changes` list performance** (`docs/PRODUCTIZATION_AUDIT.md`
  P0.18, `docs/TECHNICAL_DEBT.md`'s BLOCKER list) — verified via
  `scripts/benchmark_changes_list.py 100` at the start of this phase:
  **1.9s**, not the recorded 16s. Track A1 already fixed this
  (`RequestMemo`, `app/services/request_memo.py`, wired into
  `TaskDecisionService`/`WorkProductService`/`WorkflowService.
  evaluate_workflow()` and `ChangeListSummaryService`) after the P0
  audit was written but before this phase started. TECHNICAL_DEBT.md
  and PRODUCTIZATION_AUDIT.md are corrected in this same commit to stop
  citing a number that is no longer true (see "Stale-doc correction"
  below) — no B1 code was written for this, since there is nothing
  left to fix at today's evidence.
- **Observability, onboarding friction, general UI complexity** — real
  P0.13 gaps, but not BLOCKER/MUST_FIX severity, and not touched here
  (`docs/TECHNICAL_DEBT.md`'s own severity tiers). Left for B2+.

## Scope

**B1.1 — GET-route cross-org read isolation (primary).** Extend the
exact same B0.3 mechanism (`AuthzService`, `ROLE_LEVEL`, `org_service.
member_role`) to every GET route that can return another organization's
data, in two parts:

- **(a) Per-id routes** — a route reading one entity by path param
  (`/changes/{cid}`, `/tasks/{tid}`, ...). New dependency
  `require_read_role(kind, param, min_role="VIEWER")`
  (`app/main.py`) — the exact same AuthzService resolution and
  401/404/403 semantics as B0.3's `require_role()`, deliberately
  **without** the CSRF check folded in (GET is safe/idempotent; CSRF
  guards state-changing requests only — `app/services/csrf.py`'s own
  docstring). Swept across every GET route whose path param names an
  entity already in `AuthzService.RESOLVERS`, using the exact
  kind↔param mapping B0.3's own mutating-route sweep already
  established (grepped from the 157 existing `require_role(...)`
  call sites, not re-guessed).
- **(b) List routes** — a route returning many rows with no id in the
  path (`/changes`, `/tasks`, `/repositories`, ...). New
  `AuthzService.visible_repository_ids(user_id)` (one JOIN query
  through `organization_members` → `repositories.organization_id`,
  the same tenant-scoping anchor B0.2/B0.3 already established) plus
  `AuthzService.visible_task_ids(user_id)` (Task has no direct
  repository column — unions the same three sources
  `_repo_ids_for_task` already resolves: `repo_scope_id`,
  `changes.project_id`, `agent_workspaces.repository_id` — in one
  batched query, not per-row). Each list route filters its existing,
  unchanged query result against this set before returning/rendering;
  no route's query shape or pagination logic changes, only which rows
  survive the filter.

Not swept (with reasons, so a completeness test can assert the
boundary rather than silently trusting it):
- Auth/account/org self-service routes — already identity-scoped, no
  entity to check (`/auth/*`, `/account`, `/api/whoami`, `/orgs`,
  `/orgs/new`) or already carry their own dedicated guard
  (`/orgs/{org_id}` → `_org_context`, `/orgs/invitations/{token}` →
  token-scoped, `/orgs/{org_id}/secrets` → `_secrets_ctx`).
- Operator-level, no-tenant-concept routes — `/help`, `/settings`
  (launcher status), every `/api/engineering/*` route (a static,
  code-shipped catalog, not DB data), `/api/spec/*` (the filesystem
  `specs/` tree — one tree per operator today, same trust boundary as
  `PROJECT.yaml`; no per-org spec registry exists in this codebase,
  and building one is out of this phase's scope).

**B1.2 — SSRF allowlist for tenant-controlled health-check URLs.**
`DeploymentService._check_health`/`http_get` and `SandboxRuntimeService.
health_check` both issue a real `urllib.request.urlopen(url)` where
`url` comes from a tenant's own `PROJECT.yaml` (`service.healthcheck.
url`) — confirmed real, unvalidated, in both call sites
(`app/services/deployment_service.py`, `app/services/sandbox_runtime.
py:207`). `docs/PRODUCTIZATION_AUDIT.md` P0.17 rates this
MUST_FIX_BEFORE_PUBLIC_BETA for tracks B/C. Fix: a new
`app/services/ssrf_guard.py` — resolves the URL's host, rejects
loopback/private/link-local/multicast/reserved ranges and the cloud
metadata address (`169.254.169.254`) by default, **gated on
`AUTH_MODE=required` only** — under `AUTH_MODE=none` (the permanent
self-hosted default, ADR-004) behavior is byte-for-byte unchanged,
since a self-hosted operator's own DEV target legitimately IS
`127.0.0.1`/an internal LAN address (this module's own docstring
audit trail). No allowlist escape hatch is added in this phase (no
real hosted operator exists yet to need one) — a fail-closed default
with zero configuration surface, matching this track's own
global rule ("fail closed").

## Non-goals (unchanged from B0's own list, reaffirmed here)

No billing, no fine-grained per-resource RBAC beyond the existing
OWNER/ADMIN/MEMBER/VIEWER four roles, no SSO/SAML/OIDC, no App Builder
work, no infra/deployment topology changes. Also, explicitly, no B2+
item is pulled forward: `releases._next_version()`'s SELECT-then-INSERT
race (`docs/TECHNICAL_DEBT.md` IMPORTANT tier) and general observability
work are real, but not touched here — B1 does not depend on them for
correctness.

## Design principles (carried over from B0, unchanged)

Additive-only migrations (none needed this phase — no new tables, only
new service methods and route dependencies); `AUTH_MODE=none` stays a
zero-new-surface no-op throughout (every new check function's first
line is the same `if settings.auth_mode != "required": return`
short-circuit B0.3 established); reuse `AuthzService`/`org_service`
exactly as they exist today, no second resolver, no parallel
tenant-scoping mechanism.

## Acceptance criteria

1. Every GET route that can return another organization's row data is
   either (a) guarded by `require_read_role`, (b) filtered via
   `visible_repository_ids`/`visible_task_ids`, or (c) explicitly
   listed above as intentionally unscoped, with a completeness test
   (`tests/test_b1_read_isolation.py`) enumerating `app.routes` to
   catch any future unguarded addition.
2. A real cross-org adversarial test exists for at least one route in
   each category (per-id HTML, per-id API, list HTML, list API) proving
   a Member of Org A gets 404/an empty-filtered list for Org B's data,
   never Org B's real content.
3. `AUTH_MODE=none` behavior is byte-for-byte unchanged — the existing
   full regression suite (self-hosted mode) passes with zero new
   failures.
4. SSRF guard rejects loopback/private/link-local/metadata-address
   targets under `AUTH_MODE=required`, with real tests (no mocking the
   guard itself) proving both a rejected and an accepted case; under
   `AUTH_MODE=none`, existing DEV-deploy-to-localhost behavior is
   unchanged (a real regression test, not just an assertion of intent).
5. Stale performance claims in `TECHNICAL_DEBT.md`/
   `PRODUCTIZATION_AUDIT.md` are corrected with fresh, real benchmark
   evidence from this phase, not silently left contradicting the code.

## Stop condition

Same as B0: do not begin B2 automatically. This document, once its own
acceptance criteria are met and reported, is the full extent of B1.
