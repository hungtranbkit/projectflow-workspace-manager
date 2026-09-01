# B5 — Tenant Isolation Completeness (credential routing + aggregate counts)

**Status: IMPLEMENTED.** The phase after B4 (GitHub Webhook Status
Ingestion, PASS). No `B5` spec existed in the repo before this
document.

## How this scope was chosen

Fresh re-audit of `docs/TECHNICAL_DEBT.md` post-B4 plus the four
candidates the user's own instructions named explicitly:

1. **Wiring webhook status into merge/gate decisions** (B4's own
   named residual). **Not selected — genuinely BLOCKED for the
   "authoritative" version, and the "advisory" version is already
   built.** B4 already ships webhook data as a read-only, clearly-
   labeled advisory signal (`task_detail.html`'s own "GitHub webhook
   last reported..." line) precisely because this session has never
   observed a real GitHub webhook delivery (ordering, retries, at-
   least-once semantics, a delayed/missed delivery) — there is no
   local way to prove stale/missing/conflicting webhook data can't
   incorrectly authorize a merge, because there is no real delivery
   evidence to reason from at all. Every live merge/gate-eligibility
   call site still requires a fresh `pr_status()` poll before
   `merge_pr()` runs (`app/main.py`'s own existing call sites,
   unchanged since before B4) — that stays true after B5 too. Building
   a "stronger" read-through-cache layer on top would not remove this
   gap, only relocate it; per the user's own explicit instruction
   ("classify BLOCKED and choose another coherent scope"), this
   candidate is left exactly where B4 left it, not touched in B5.
2. **GitHub credential routing (`available()`)** — **selected.** A
   real, already-identified (B4's own audit), narrowly-scoped,
   locally-provable bug, upgraded from "latent" to "confirmed live"
   during this phase's own investigation (see Findings below) — a
   purely local `git remote get-url origin` check currently returns a
   wrong answer (not a crash) for any org with `AUTH_MODE=required`
   and no PAT/App configured yet, exactly the state a newly-onboarded
   hosted org is in before connecting GitHub at all.
3. **Dashboard/`/sandboxes` aggregate tenant filtering** — **selected.**
   B1's own noted residual, plus a SECOND, previously-undocumented
   instance found in this phase's own audit (`/sandboxes` page's own
   `running` badge, same `SandboxManager.running_count()` call, same
   unfiltered-COUNT shape). Both are real, small, and the user's own
   instructions give the exact right fix shape ("filter before
   aggregation").
4. **Workspace-identity permanence** — **re-examined, not selected.**
   Per the user's own explicit instruction ("first prove the current
   workaround is insufficient... before implementing"): re-read
   `ARCHITECTURE.md`'s own documented workaround (create a new Task
   rather than reuse the old one) against current code — nothing has
   changed since B1/B2/B3 each independently declined this same item
   for the same reason. No new evidence of insufficiency was found.
   Still declined, now for the fourth time running, with the same
   reasoning restated rather than silently dropped.

## Findings (grounding, gathered before any code changed)

- **`available()` is a confirmed LIVE bug, not merely latent.**
  `available()`'s own `try/except Exception: return False` (added
  before B0 existed) means `GitHubIntegrationError` (a `RuntimeError`
  subclass) raised by `self.runner`'s credential-resolving wrapper
  (B0.7/B3.1) is silently swallowed — `available()` returns `False`,
  not a crash. Four real call sites (`app/main.py:2588,2725,3267,4933`)
  use this to decide whether to show GitHub-integration UI at all. The
  practical effect: under `AUTH_MODE=required`, any organization that
  hasn't yet configured a PAT or installed a GitHub App sees EVERY one
  of its GitHub-backed repositories reported as "no GitHub remote" —
  wrong, and hides real functionality (Create PR) exactly when an org
  is newly onboarding, the single most common real-world state for a
  brand-new hosted tenant.
- **Two unfiltered aggregate-count call sites**, not one:
  `app/main.py`'s dashboard (`/`, already flagged by B1) AND
  `/sandboxes`' own `running=sandboxes.running_count()` badge (found
  fresh in this phase's own audit — B1's original sweep covered every
  GET route's own row-level data but this particular capacity-style
  scalar was not caught by that pass). Both call the SAME
  `SandboxManager.running_count()`, unfiltered.
- `SandboxManager.capacity_available()` also calls `running_count()`,
  but for a genuinely different, correct reason: `max_running_sandboxes`
  is a real, whole-process infrastructure ceiling (this app is one
  process, one Docker daemon budget), not a per-tenant quota — that
  call site must keep using the TRUE global count, never a filtered
  one. This is the one place B5 explicitly does NOT filter, on purpose.

## Scope

**B5.1 — `available()` credential-routing fix.** **Correction made
during this phase's own implementation, not assumed correct up front:**
the first draft copied B4.1's own `github_owner_repo()` pattern
verbatim — call `_default_runner` directly, bypass `self.runner`
entirely. That broke the full regression suite (60 failures across
`test_deployment.py`/`test_merge_reconciliation.py`/`test_deployment_
hardening.py`/`test_real_merge.py`/`test_integration_push.py`/
`test_action_feedback.py`/`test_state_invariants.py`): `self.runner`
IS the established dependency-injection seam this whole class is built
around (its own class docstring says so), and every one of those tests
injects a fake runner (`client.app.state.github_merge.runner = fake`)
to simulate "GitHub is configured" without a real `git`/`gh` call —
bypassing the seam means `available()` never sees that fake, always
runs a REAL `git remote get-url origin` against the test's own local
git fixture (which has no real GitHub remote), and reports `False`
when the test expects `True`. **This means B4.1's `github_owner_repo()`
already had this exact same latent bug** (never caught, because no
pre-existing test both injects a fake runner AND exercises the one new
B4-only code path that calls it) — fixed identically here, not left in
place now that it's understood.

The actual, correct fix: both methods keep calling `self.runner`
unchanged (preserving the DI seam intact). `make_hosted_runner()`/
`make_installation_token_runner()` (`app/services/github_merge_
service.py`) — the credential-resolving wrapper functions that
`self.runner` actually points to under `AUTH_MODE=required` — gain a
new `_needs_no_credential(argv)` check: an EXACT match (never a
prefix/substring test, to avoid ever accidentally exempting a real
network call) against the one local, credential-free command shape
(`("git", "remote", "get-url", "origin")`), short-circuiting straight
to `_default_runner` for it. Everything else keeps requiring a
resolved credential exactly as before. `available()` continues to
answer "does this repo have a real GitHub remote," never conflated
with "do I have a resolved GitHub credential for it" — the two are
genuinely different questions, and only the first is what every one of
its 4 call sites actually needs answered before deciding whether to
show Create-PR UI at all.

**B5.2 — Tenant-scoped aggregate counts.** `SandboxManager.
running_count()` gains optional `repo_ids`/`task_ids` parameters
(both `None` = unrestricted, the exact same `AUTH_MODE=none`/
unauthenticated-caller precedent every B1-B4 filter already uses) that
restrict the `COUNT` query itself — filtered BEFORE aggregation, never
computed globally then hidden after — to sandboxes resolvable to a
visible repository (direct `repository_id` FK) or a visible task (the
indirect-ownership case, e.g. `REPOSITORY_TEST`-owned sandboxes with no
direct repo FK). Reuses `AuthzService.visible_repository_ids()`/
`visible_task_ids()` (both already built, B1), never a third resolution
implementation. `capacity_available()`'s own call site is explicitly
left calling `running_count()` with no filter (see Findings). The
dashboard's `cleanup_pending` count gets the same repo/task-scoped
`WHERE` treatment, inline (it was a one-off query, not a shared
method).

## Non-goals (explicit)

- Wiring webhook status into any merge/gate decision (see item 1
  above — BLOCKED on real delivery evidence this session cannot
  obtain).
- Workspace-identity permanence (re-examined, not proven insufficient).
- Any other cosmetic `TECHNICAL_DEBT.md` NICE_TO_HAVE item
  (`review_runs` filter, `SECURITY_PASS` docstring, UI complexity) —
  none is security/tenant-isolation relevant, out of this phase's own
  theme.

## Design principles (carried over, unchanged)

`AUTH_MODE=none` untouched — every new filter parameter defaults to
`None` (unrestricted), the exact behavior every existing caller
(`capacity_available()`, any direct test construction) already gets
today. No schema change. No new trust boundary. Reuses `AuthzService`'s
existing resolvers, never a parallel one.

## Acceptance criteria

1. `available()` returns the CORRECT answer (based on the real git
   remote, not credential availability) under `AUTH_MODE=required`
   with no PAT/App configured for the org — proven with a real cross-
   org test, not just a unit call.
2. A member of Org A never sees Org B's running-sandbox/cleanup-
   pending counts reflected in either the dashboard's or `/sandboxes`'
   own numbers — proven by creating real sandboxes in two different
   orgs and asserting the count difference directly (not just "the
   route returns 200").
3. `capacity_available()`'s own behavior (and `max_running_sandboxes`
   enforcement) is provably unaffected — a real test creating sandboxes
   across multiple orgs still hits the TRUE global ceiling, not a
   per-tenant one.
4. `AUTH_MODE=none` behavior is byte-for-byte unchanged (existing full
   regression suite, self-hosted mode).
5. Full existing regression suite (fast non-real subset) stays green --
   including, explicitly, every pre-existing test that injects a fake
   `GitHubMergeService.runner` (the real thing B5.1's own first draft
   broke and this phase's own full-suite run is what caught it).

## Stop condition

Same as B0-B4: do not begin B6 automatically.
