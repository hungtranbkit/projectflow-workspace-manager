# B4 — GitHub PR/CI Webhook Event Ingestion (ADR-001's own "phase 2")

**Status: IMPLEMENTED (ingestion + read-only surface only — see Non-goals).**
The phase after B3 (GitHub App Installation Architecture, PASS). No
`B4` spec existed in the repo before this document.

## How this scope was chosen

Fresh re-audit of `docs/TECHNICAL_DEBT.md` post-B3: BLOCKER is empty;
IMPORTANT has two non-actionable items (workspace-identity permanence —
architectural, has a documented workaround, declined as B4 scope for
the fourth time running for the same reason, not a new judgment;
real-provider's bounded retry — already accepted-by-design);
NICE_TO_HAVE is cosmetic or a product decision already deferred twice
(the `pytest` major-version bump — re-examined below, not re-deferred
by default this time).

**Re-examining the `pytest` bump rather than assuming it stays
deferred:** unlike a runtime dependency (B2's `cryptography` bump,
where usage-surface analysis plus the real test suite was sufficient
proof), `pytest` is the framework the entire regression suite runs
under — a bad upgrade could break collection/fixtures across 100+ test
files in ways only visible by actually running everything, so this
needed real evidence, not just reasoning about API surface. Attempted
directly: `pip install "pytest>=9,<10"` into this venv (landed
9.1.1), then both `--collect-only` and the full fast non-real
regression suite (1080+ tests). Result: **clean** — collection
succeeded, the full run completed with zero failures, `pip-audit`
confirms PYSEC-2026-1845 is now resolved (0 remaining advisories on
this dependency). B2's/B3's own caution was the right default without
evidence, but the evidence itself says this bump is safe — **adopted**
(`pyproject.toml`'s test extra now pins `pytest>=9,<10`), the same
"verify, then adopt" pattern already used for B2's `cryptography` bump,
not left deferred on the strength of an assumption that turned out not
to hold.

**GitHub webhook/status-polling integration** is the one item this
session's own prior work (B3) explicitly named as deferred and
"worthwhile": ADR-001's own words — "Using `pull_request`/`check_run`/
`status` webhooks to **replace** today's polling (`gh pr view`) is a
real, worthwhile enhancement — explicitly deferred to a phase-2 pass."
B3 already built phase 1 (the HMAC-verified `/webhooks/github` route,
currently handling only `installation` events). Extending it to also
ingest these three event types is the natural, already-scoped
continuation, reuses B3's own verification machinery unchanged, and is
fully testable locally with hand-built payloads matching GitHub's real,
documented webhook JSON shape — no live App needed for the parsing/
storage logic itself (only live delivery is unavailable, the same
limitation B3 already had and disclosed).

## Why NOT the full "replace polling" migration (bounded scope)

ADR-001 says "replace," but this phase deliberately does NOT touch any
of `GitHubMergeService.pr_status()`'s ~5 existing call sites (`app/
main.py:2666,2670,4861,4895,4912`) or any merge/gate-eligibility
decision path (`review_fix_orchestrator`, `WorkflowService._gate_
review_pass`, the real merge-approval checks). Reasoning: this session
has never observed real GitHub webhook delivery behavior (ordering,
retries, at-least-once semantics, a missed/delayed delivery) — building
a full replacement of live-checked, merge-blocking decision paths on
data this session cannot verify arrives reliably would be exactly the
"do not fake live external evidence" instruction violated by
implication. B4 ingests and stores webhook data as a **read-only,
clearly-labeled supplementary signal**; the authoritative decision
paths are untouched. Wiring it in as the primary source is explicitly
B5+ work, gated on real-world delivery-reliability evidence a real App
registration would provide.

## Scope

**B4.0 — `pytest` dependency bump (verified, adopted).**
`pyproject.toml`'s test extra moves `pytest>=8,<9` -> `pytest>=9,<10`
(landed 9.1.1), closing PYSEC-2026-1845/GHSA-6w46-j5rx-g56g — real
evidence (clean collection, clean full regression run, `pip-audit`
confirms 0 remaining advisories), not a docs-only note.

**B4.1 — Repository GitHub identity (a real, local-only derivation).**
`repositories.github_owner_repo` (migration 36, nullable TEXT, e.g.
`"octocat/hello-world"`) — parsed from `git remote get-url origin`
(the exact same local, no-network check `GitHubMergeService.
available()` already makes), never a live API call. Computed lazily
and cached the first time a webhook needs to resolve a repository (or
via a new `GitHubMergeService.github_owner_repo(repo_path)` helper),
matching this codebase's own "derive Migration path" don't-store-what-
can-be-computed discipline as SecurityApplicabilityService (DERIVED_
TRUTH), not a second source of truth for the same fact.

**B4.2 — `merge_records` webhook snapshot columns.** **Correction made
during this phase's own implementation, not assumed correct up front:**
the first migration draft added `merge_records.pr_number`/`head_sha` as
new columns — implementation immediately failed (`duplicate column
name: pr_number`) because E10's own migration 10 already added
`pr_number`, `head_sha`, `ci_status`, `mergeability`, `pr_state`,
`pr_url`, `merge_state_status` to `merge_records` long before B4,
kept fresh by the existing live-poll path this whole time. A stray
`repositories.github_owner_repo` column left behind by that failed
first attempt (SQLite's `executescript` applies each `ALTER TABLE`
statement as it goes, not as one atomic unit — the script aborted
partway through) was cleaned up by hand (`ALTER TABLE ... DROP
COLUMN`) before the corrected migration ran. Migration 36, corrected,
adds only: `repositories.github_owner_repo` and three genuinely new
`merge_records` columns — `webhook_ci_status`, `webhook_mergeability`,
`webhook_updated_at` (all nullable, populated only by a real, verified
webhook event). B4 reads the EXISTING `pr_number`/`head_sha` columns to
match an incoming event to the right row; it never writes them — that
stays the live-poll path's own job, exactly as before B4 existed. The
webhook snapshot columns are never conflated with `ci_status`/
`mergeability` (which the live-poll path exclusively owns) or with
`merge_status`/`merged_commit`/`merged_at`.

**B4.3 — Webhook ingestion.** `/webhooks/github` (B3's own HMAC-
verified route) gains handling for `pull_request` (any action carrying
a `pull_request` object) and `check_run`/`status` events, dispatched on
the real `X-GitHub-Event` header (not merely guessed from payload shape
— a real, separate correctness fix made alongside this phase's own
work, since every App-delivered payload carries an `installation`
object regardless of event type, so B3's own original `action==
"deleted" and "installation" in payload` check was not actually
sufficient to identify an `installation` event specifically, even
though no real payload has yet exercised that gap). Resolves
`repository.full_name` -> `repositories.github_owner_repo` (computing
it on first sight if not yet cached) -> the matching `merge_records`
row via its own existing `pr_number` or `head_sha`, and updates the
webhook snapshot columns only. Naturally idempotent (a plain `UPDATE`
to "current known state," never an append/increment) — safe under
GitHub's own at-least-once redelivery guarantee without a separate
dedup table. Every other event type/action is accepted and ignored.

**B4.4 — Read-only surface.** The integration detail page shows the
webhook snapshot, if any, clearly labeled ("GitHub webhook last
reported...", with its own timestamp) alongside, never replacing, the
existing live-poll-derived status already shown there.

## Non-goals (explicit)

- Replacing any of the 5 existing live `pr_status()` read call sites,
  or any merge/gate-eligibility decision — deferred to B5+, gated on
  real webhook-delivery-reliability evidence.
- Workspace-identity permanence — architectural, has a documented
  workaround, declined again for the same reason as B1/B2/B3.
- Anything requiring a real, externally-registered GitHub App (live
  webhook delivery, real installation) — same disclosed limitation as
  B3.

## Design principles (carried over, unchanged)

`AUTH_MODE=none` untouched (the webhook route's own existing B3 gate —
`if not settings.github_webhook_secret: raise HTTPException(404)` —
already makes this a no-op there; B4 adds no new gate). Additive-only
migration (36). Reuses B3's HMAC verification unchanged, no new trust
boundary. `github_owner_repo` is DERIVED_TRUTH (computed from git, not
a second hand-entered value) matching this codebase's own existing
DERIVED_TRUTH precedent (`docs/SOURCE_OF_TRUTH.md`'s own vocabulary).

## Acceptance criteria

1. `github_owner_repo` is correctly parsed from real local git remote
   URLs in both common GitHub URL forms (SSH and HTTPS), proven against
   a real git repo fixture, not a hand-typed string.
2. A real HMAC-signed `pull_request` webhook payload updates the
   correct `merge_records` row's webhook snapshot columns; a real
   `check_run`/`status` payload does too; an unresolvable repository
   (unknown `full_name`) or PR (`pr_number` with no matching
   `merge_records` row) is a safe no-op, never a crash.
2b. Redelivery (the identical payload posted twice) is idempotent —
    the second delivery produces the same end state, not a duplicate
    row or an error.
3. The existing 5 live `pr_status()` call sites and existing B3
   webhook tests (`installation` events) are provably unaffected —
   full regression, not just "I didn't touch that code."
4. The read-only surface renders the webhook snapshot when present,
   renders nothing extra when absent (an org/repo with no webhook
   traffic yet looks exactly as it did before B4).
5. Full existing regression suite (fast non-real subset) stays green.

## Stop condition

Same as B0-B3: do not begin B5 automatically.
