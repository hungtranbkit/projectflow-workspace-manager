# B2 — Release Concurrency Correctness & Residual Security Verification

**Status: IMPLEMENTED.** The phase after B1 (Hosted-Service Read
Isolation, PASS). No `B2` scope existed anywhere in the repo before
this document — written from a fresh re-audit of `docs/TECHNICAL_DEBT.
md`'s current IMPORTANT/NICE_TO_HAVE tiers and `docs/PRODUCTIZATION_
AUDIT.md`'s P0.17 security table (both already corrected as of B1),
plus direct code inspection, before any B2 code changed.

## How this scope was chosen

`docs/TECHNICAL_DEBT.md` (post-B1) has zero open BLOCKER items — every
one from P0 is now fixed (B0/B1) or superseded. The remaining IMPORTANT
tier has three items:

1. **Workspace identity is permanent** — an architectural constraint
   with a proven, documented workaround (ARCHITECTURE.md), not a bug.
   Not actionable as a bounded fix; excluded.
2. **`releases._next_version()` SELECT-then-INSERT race** — a real,
   confirmed, still-open concurrency bug with an established fix
   pattern (the SAME bounded-retry-on-collision shape already used
   twice: `plans.(change_id,revision)` and `execution_waves.
   wave_number`, both P0.9). **Selected — highest readiness, direct
   correctness impact, matches this repo's own established pattern.**
3. **Real-provider retry's bounded-not-unlimited failure surface** —
   already explicitly accepted as correct-by-design in the same doc
   ("Acceptable, but worth monitoring"). Not a bug; excluded.

The NICE_TO_HAVE tier's one security-flagged item — `|safe` usage in
`workspace_detail.html`/`task_detail.html`, `docs/PRODUCTIZATION_AUDIT.
md` P0.17's own CAN_WAIT-but-unconfirmed XSS entry — is more
security-relevant than its NICE_TO_HAVE tier suggests now that B0/B1
closed every AuthN/AuthZ/tenant-isolation gap (this becomes relatively
more prominent as a residual attack surface once request forgery isn't
the easier path in). **Selected as a verification item** — audited
directly against real template/Python source (see Findings below), not
assumed safe from the doc's own hedge language.

A third candidate, a dependency/supply-chain CVE audit (`docs/
PRODUCTIZATION_AUDIT.md` P0.17: "not performed in this pass"), is
**also selected** — bounded, evidence-producing, no external credential
needed (network access to PyPI's advisory data is available in this
environment, confirmed before starting).

Two other P0.17-flagged items were re-examined and NOT selected because
direct inspection found the underlying mechanism already sound, not
because they were skipped:
- **Path traversal** (`app/services/git_workspace.py`): `validate_repo`/
  `validate_branch`/`validate_worktree` already enforce real root-
  containment + a real `git rev-parse --show-toplevel` round-trip +
  a branch-name regex rejecting `..` — re-confirmed by direct read,
  not re-implemented.
- **Prompt injection via repo content**: still correctly CAN_WAIT per
  P0.17's own reasoning (bounded blast radius, tool-less `--tools ""`
  structured-output invocation) — nothing in B0/B1 changed that.

## Scope

**B2.1 — `releases._next_version()` race, real fix.** Two concurrent
`create_release()` calls for the same repository, both with no explicit
`version` (the auto-derived `COUNT(*)+1` fallback path — only used when
neither a `VERSION` file nor `PROJECT.yaml`'s `project.version` is set),
can compute the identical version string; the real `UNIQUE(repository_
id, version)` constraint (already in the schema) prevents a duplicate
row but the losing caller currently gets a raw, unhandled `sqlite3`
exception (a 500) instead of a clean outcome. Fixed with the exact same
bounded-retry-on-collision shape as `ExecutionWaveService.
run_execution_wave()`/`PlannerService`'s own two prior fixes: re-read
the next candidate fresh each attempt, catch the insert failure, retry
up to 5 times, a graceful `ReleaseError`/failure result on exhaustion.
An **explicit** caller-supplied `version` that collides (including the
same TOCTOU race between two callers passing the identical explicit
string) is NOT silently retried with a different value — it raises the
same clear "version already exists" `ReleaseError` the existing
pre-check already gives, now also on the race window, never masked as
a generic retry-exhausted message.

The `work_products` row `create_release()` builds (`RELEASE_MANIFEST`,
titled with the version string) is created ONCE, after the version is
finally confirmed — not inside the retry loop — so a collision-retry
never leaves an orphaned `WorkProduct` behind and the title never
references a version string that ultimately lost the race.

**B2.2 — XSS verification, `|safe` usage.** Direct-code-audit finding
(not a guess): both `resume_form`/`block_form` (`app/templates/
task_detail.html`, `app/templates/workspace_detail.html`) are built
entirely inside a Jinja `{% set %}...{% endset %}` block, which Jinja
autoescapes internally exactly like any other template output — the
`|safe` filter only suppresses re-escaping the already-safe result at
the point it's echoed, it does not bypass escaping of the interior
`{{ }}` interpolations. The only interpolated values are `w.agent`
(guarded by `w.agent in ['codex','claude']` before any output, so
constrained to two fixed literals) and `w.repository_id`/`w.id`
(INTEGER FK columns, never attacker-supplied text). **Confirmed: no
real XSS vector here.** A regression test (`tests/test_b2_release_
concurrency.py`) renders the real template with an adversarial
(non-whitelisted) `agent` value and asserts the fixed-choice guard
still holds, so a future change that widens the `if` check without
reconsidering escaping would fail loudly. `TECHNICAL_DEBT.md`'s
hedge language ("not confirmed free of user-controlled substrings")
is corrected to state the confirmed-safe finding plainly.

**B2.3 — Dependency/supply-chain audit.** `pip-audit` run against the
real installed `.venv` (not a hypothetical), findings reported and
acted on: safe/compatible upgrades applied for any real advisory found;
anything requiring a breaking major-version bump is documented as a
residual, not silently patched around. Not a runtime dependency itself
— an audit tool, run once, not added to `pyproject.toml`.

## Non-goals / explicitly deferred (B3+)

- Workspace-identity permanence (architectural, has a documented
  workaround already).
- UI complexity / Simple-vs-Advanced split (P0.12 proposal, unrelated
  to security/correctness).
- `review_runs` "most recent row" `review_kind` filter tightening —
  cosmetic per its own TECHNICAL_DEBT.md entry, gates that matter
  already filter correctly.
- `SECURITY_PASS` gate docstring staleness — cosmetic.
- Dashboard's unfiltered sandbox aggregate counts (B1's own noted
  residual) — low severity, still deferred.
- B0.7's simplified per-org GitHub PAT consumer vs. ADR-001's full
  GitHub App/JWT design — unchanged, still needs a real external App
  this environment cannot fabricate.

## Design principles (carried over, unchanged)

Reuse the EXACT existing bounded-retry pattern (no new concurrency
primitive); `AUTH_MODE=none` untouched (B2 touches no auth/tenant
code path at all — `ReleaseService`/template rendering are identical
in both modes); no schema migration needed (the `UNIQUE` constraint
already exists, added at B0.2-era migration 27).

## Acceptance criteria

1. A deterministic concurrency test proves two simultaneous auto-
   versioned `create_release()` calls for the same repository both
   succeed with distinct, correct version numbers — no raw exception,
   no lost release.
2. A deterministic test proves an explicit-version collision still
   raises the same clear `ReleaseError`, not a generic retry-exhausted
   message, including the race-window case (not just the pre-check).
3. No orphaned `WorkProduct` row after a retried collision (a real
   assertion, not just absence-of-crash).
4. A real-template-render test proves the `resume_form`/`block_form`
   guard holds for a non-whitelisted `agent` value.
5. `pip-audit` evidence recorded in the final report — findings acted
   on or explicitly documented as residual, never silently ignored.
6. Full existing regression suite (fast non-real subset) stays green;
   `AUTH_MODE=none` behavior unaffected (B2 touches no auth code).

## Stop condition

Same as B0/B1: do not begin B3 automatically.
