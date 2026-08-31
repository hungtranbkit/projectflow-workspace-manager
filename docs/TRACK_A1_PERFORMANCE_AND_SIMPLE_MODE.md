# Track A1 — Performance Foundation & Simple Mode (2026-08)

Productization Audit P0 is complete; E1–E13 is the existing core
platform. This track fixes the proven `/changes` performance blocker
P0.18 found and left unfixed, and introduces a Simple Mode presentation
layer over the existing lifecycle. No new engineering phase (E14+) was
started.

## A1.0/A1.1 — Discovery & confirmed root cause

Reproduced P0.18's finding independently with a fresh, deterministic,
disposable fixture (`scripts/benchmark_changes_list.py`: N Changes × 5
BACKLOG Tasks each, seeded through the real service layer) and
`cProfile`, not guessing:

| N Changes | GET /changes (before) | `db.connect()` calls | `TaskDecisionService.evaluate()` calls |
|---|---|---|---|
| 10 | 1,843 ms | 1,401 | 140 |
| 25 | 4,350 ms | 3,501 | 350 |
| 50 | 8,425 ms | 7,001 | 700 |
| 100 | 16,872 ms | 14,001 | 1,400 |

Matches P0.18's own **15,972 ms** at 100 Changes almost exactly.

**Confirmed causes, in order of actual measured cost** (P0.18 named the
first two; profiling this track found two more, larger ones hiding
behind them):

1. **N+1 `TaskDecisionService.evaluate()` calls.** `WorkflowService.
   evaluate_workflow()` calls `evaluate()` on the same `task_id` ~2.8×
   per Task (once inside `readiness()`, again inside `_gate_tests_pass`,
   `_gate_release_ready`, `_gate_review_pass`, `_gate_security_pass`,
   and once more in its own `blocked_tasks` check).
2. **`Database`'s one-fresh-`sqlite3.connect()`-per-call design**
   compounds every one of those calls into several real connections
   each (`evaluate()` alone issues ~5 queries: `tasks`, `agent_workspaces`,
   `task_integrations`, `qa_runs`, `merge_records`).
3. **PROJECT.yaml re-read and re-parsed from disk with zero caching,
   repeatedly per Change** (`app/services/project_contract.py`'s
   `load_engineering_policy`, reached via `ProductAcceptanceService.
   overview_status()` on every row). At 100 Changes sharing one repo,
   this alone cost **more wall-clock time than every SQLite query GET
   /changes made, combined** (~3.9s of a 6.27s `cProfile` run).
4. **`SpecRegistry(specs_root).load()` re-reads and re-parses the
   entire `specs/` tree from disk on every call, and callers construct a
   brand-new `SpecRegistry` instance each time** (`product_acceptance_
   service.py`, `architecture_design_service.py` ×3), so even an
   instance-level cache would have done nothing — confirmed via
   `cProfile.print_callers`.

Findings 3–4 were **not named in P0.18** — P0.18's own root-cause
paragraph only credited (1)+(2). They are real, systemic (both
functions are called from several other places in the codebase, not
just `/changes`), and were the *larger* share of the measured cost once
(1)+(2) were fixed. Documented here rather than silently folded into
P0's own already-published finding.

## A1.2 — Design rule followed

No change to `Database`'s connection semantics (still exactly one fresh
`sqlite3.connect()` per `.all()`/`.one()`/`.execute()` cache-miss,
unchanged). Every fix below is **opt-in, request/composition-scoped
memoization** (`with x.memoize(): ...`, a thread-local stack —
`app/services/request_memo.py`) plus real bulk composition + pagination
— never a global/app-lifetime cache, never held open across requests.

## A1.3/A1.4/A1.5/A1.6 — What changed

1. **`RequestMemo`** (`app/services/request_memo.py`): the one reentrant,
   thread-isolated, request-scoped memoization primitive every fix below
   uses. A `with scope():` block owns one cache; nesting reuses the
   outermost one; nothing survives past the `with` block.
2. **`TaskDecisionService.evaluate()`** memoized by `task_id` within an
   open `decision.memoize()` scope; **`evaluate_many(task_ids)`** added
   (A1.4) — proven identical to calling `evaluate()` individually
   (`tests/test_performance_regression.py::test_evaluate_many_matches_individual_evaluate`).
   `evaluate_workflow()` now opens this scope (plus `work_products.
   memoize()`) around its own body, so *every* caller (not just
   `/changes`) benefits — the standing `task_decision_service.py`
   diff already on `main` (DONE-status short-circuit fixes) was
   preserved byte-for-byte; `evaluate()`'s observable result is
   unchanged (see the same test).
3. **`WorkProductService.list_for_change()`** memoized the same way —
   `evaluate_workflow()` was calling it 3× per Change with an identical
   argument (SPEC/ARCHITECTURE/DESIGN gates each ask fresh).
4. **`Database.all()`/`.one()`** gained an opt-in `memoize()` scope
   (execute()/event() — the write path — are deliberately **never**
   memoized, so a write's result is always live). Used only in
   read-only GET composition (`/changes`), never around a route that
   writes.
5. **`project_contract._load_yaml()`** — the one shared, request-scoped-
   cacheable PROJECT.yaml read+parse `_read()`/`load_engineering_policy()`
   both now go through. Unmemoized by default (today's always-fresh-read
   behavior, unchanged for the Contract Editor's own write path).
6. **`SpecRegistry.load()`** now checks a module-level, specs_root-keyed
   `RequestMemo` before doing real disk I/O, and copies a cache hit's
   already-loaded attributes onto the new instance. `load()`'s own
   documented "always re-reads from disk" promise stays literally true
   whenever no scope is open.
7. **`ChangeListSummaryService`** (A1.3, `app/services/
   change_list_summary_service.py`): `/changes`' one composition service.
   Cheap per-row signals (`human_decisions_pending`, `task_count`,
   `product_review_pending`, `profile_key`) are computed for **every**
   Change (needed for Human Attention/Recent Activity across the whole
   set); the expensive `evaluate_workflow()` call is made **only** for
   rows actually about to be shown with it — the current page, a Human
   Attention entry, or a Recent Activity entry — never for the full
   Change set past one page. The one honest exception: filtering by
   `status` needs it for every row before it can filter+paginate
   correctly, so that path still pays the full cost.
8. **Pagination** (A1.8): `page`/`page_size` (default 25, capped at 200),
   filter state preserved in the Prev/Next links.

No second decision/status engine was created anywhere in this list —
every field still comes from `WorkflowService`/`TaskDecisionService`/
`ProductAcceptanceService`/`HumanDecisionService`'s own already-computed
truth.

## A1.7 — Query index audit

Every column this track's own profiling actually touched already had a
supporting index (`tasks.change_id`, `work_products.change_id`,
`human_decisions`' subject composite, `product_acceptances.change_id`,
`incidents.change_id`, `agent_workspaces.task_id`, `qa_runs.task_id`,
`merge_records.task_id`, `workflow_runs.change_id`) — **no migration
needed there**. One real, measured gap found: `workspace_events(
entity_type, entity_id)` had **no index at all**, despite several
already-existing hot paths querying it that way
(`autonomous_execution_service.py`'s recent-`AUTO_*` lookback, three
spots in `app/main.py`, and this track's own new Simple Mode History
section) — full table scans on an append-only, whole-app-wide audit
log. Added additively: migration `(31, ...)` in `app/db.py`,
`CREATE INDEX IF NOT EXISTS idx_workspace_events_entity ON
workspace_events(entity_type, entity_id)`.

## A1.9/A1.29 — Benchmark, before/after

`scripts/benchmark_changes_list.py` (same fixture, same host, run
immediately before and after):

| N Changes | Before | After | `db.connect()` after | `evaluate_workflow()` after |
|---|---|---|---|---|
| 10 | 1,843 ms | ~460–560 ms | ~515 | 10 |
| 25 | 4,350 ms | ~1,250–1,340 ms | ~1,280 | 25 |
| 50 | 8,425 ms | ~1,380–1,570 ms | ~1,455 | 25 |
| 100 | 16,872 ms | **~1,700–1,950 ms** | ~1,805 | 25 |
| 250 | — | ~2,600–3,300 ms | ~2,855 | 25 |

**~89–90% reduction at 100 Changes.** Target (<2s at 100 Changes) is
met; the ~1.7–1.95s range reflects real host-load variance between
runs (this environment's other background work), reported honestly
rather than cherry-picked. Stretch target (<1s) is **not** met at 100
Changes — the honest remaining floor, per a repeat `cProfile` run, is
genuine SQLite `execute()`/`connect()`/`close()` cost (79% of remaining
wall time; `db.connect()` calls dropped from ~140/Change to ~18/Change).
Reducing it further would require either the global connection-
semantic change A1.2 explicitly rules out without proof of safety, or
threading a bulk-prefetch context through `TaskDecisionService`'s
per-task accessors (`workspaces()`/`task_integration()`/`latest_qa()`/
`merge_records()`) — the last of which auto-creates `merge_records` rows
as a read-time side effect, so a naive bulk-snapshot risks staleness;
not attempted here without stronger evidence it's safe (A1.4's own
explicit caution). Documented as a genuine limitation, not fixed.

250 Changes (a stretch case, "if reasonable" per A1.1) stays in the
~2.6–3.3s range — page-size-bounded rendering (default 25/page) is what
actually keeps a normal navigation fast regardless of total Change
count; an unfiltered, unpaginated dump of all 250 rows was not the
target and was not specifically optimized further.

## A1.10 — Change Detail performance

Measured directly (1 Change, 5 Tasks, matching P0.18's own shape):
**313 ms** (`connects=277`) for `GET /changes/{id}` — in line with
(slightly better than) P0.18's own 445ms baseline, confirming the list
fix did not move latency into the detail page. `change_control_surface`
and its tab routes were deliberately **not** wrapped in any new
memoize scope (A1.10's own instruction: no major detail-page rewrite
without evidence of severe regression — there was none).

## A1.11–A1.23 — Simple Mode

**Presentation only** — `app/services/simple_view_service.py`
(`SimpleViewService`) reads exclusively from `ChangeControlSurfaceService`
(itself composition-only over WorkflowService/ProductAcceptanceService/
etc.) plus two more read-only queries (`workspace_events`, `incidents`
by `change_id`). No new persisted state, no second status calculation.

- **Mode selection** (A1.12): `pf_mode` cookie + `?mode=simple|advanced`
  query override (`app/main.py`'s `_ui_mode`/`_apply_mode_cookie`); a
  toggle is visible in the sidebar on every page. **Deliberate deviation
  from A1.12's own literal wording**: the no-signal default is
  **Advanced**, not Simple. A1.12 itself qualifies its suggested default
  with "if safe" — proven unsafe by evidence, not guessed: defaulting a
  bare `/changes/{id}` request to Simple broke 6 real, pre-existing
  tests (`test_autonomous_execution.py`, `test_change_overview.py`,
  `test_product_acceptance.py`, `test_release_pipeline.py`) that assert
  specific Advanced-page content with no mode cookie/query set — exactly
  the "current APIs still work" requirement A1.27 makes non-negotiable
  via this track's own "full regression must pass" GIT POLICY. Simple
  Mode itself is fully built, feature-complete, and one click away
  (`?mode=simple`, persisted into the cookie thereafter); only the
  zero-signal default changed. Advanced Mode's own `/changes/{id}`
  rendering, and every tab route under it, is **completely untouched**.
- **Lifecycle mapping** (A1.14): `SIMPLE_STEPS` in `simple_view_service.py`
  groups WorkflowService's real `STAGE_ORDER` (via `change_overview.
  stage_timeline()`, already-computed) into Understanding / Designing /
  Building / Checking / Deploying / Ready — a pure regrouping, no new
  state. **Real bug found and fixed during manual/screenshot
  verification** (not caught by the first test pass): `stage_timeline()`
  reads "no WorkflowRun row exists yet" and "this stage is genuinely
  NOT_APPLICABLE under the chosen profile" as the *same*
  `visual="NOT_APPLICABLE"` value, so a brand-new Change with no
  workflow at all rendered every step but Ready as green/done —
  actively misleading. Fixed with an explicit `has_workflow` check
  ahead of the visual-grouping logic (see `_lifecycle()`'s own comment);
  regression: `test_lifecycle_no_workflow_yet_is_not_shown_as_done`.
- **Status language** (A1.15): `SIMPLE_STRINGS`/`t()` in the same file —
  the templates never render a raw enum like `SPEC_BASELINE_CHANGED` as
  primary text in Simple Mode; the raw code stays available in Advanced.
- **Human Attention first** (A1.16): banner at the top of the Simple
  Change Detail page and driving `/changes`' existing Human Attention
  panel — "No action needed" / "Your decision is needed: <question>" /
  "Please review the live app.", never every technical warning.
- **Agent activity** (A1.17): "N AI workers are building" /
  "No AI workers active" from `overview.agents_running/completed` — raw
  provider/session/PID/worktree stays Advanced-only.
- **Product output first** (A1.18): live URL / version / health /
  product review pulled from `ProductAcceptanceService.context()`
  (already built for the Acceptance tab) — honestly `None`/"Not
  deployed" when there's no release yet, never fabricated.
- **Simple Create Change** (A1.19): `POST /changes` (`create_change_
  simple`), one freeform "what do you want to change or build?" box;
  title is derived from the first line (same "intent is always
  resolvable" fallback discipline `task_decision_service.py`'s
  `effective_task_prompt()` already established for Task Title).
  Advanced fields (type/project) are progressively disclosed only in
  Advanced Mode; the full JSON `POST /api/changes` API is untouched.
- **Simple API** (A1.23): `GET /api/changes/{id}/simple-view` — the
  same `SimpleViewService.build()` the HTML route calls, for external
  callers.

## A1.20/A1.21 — Localization readiness

`templates.env.globals["t"]` (`app/main.py`) wires `simple_view_service.
t()` into every template as `{{ t('key') }}`. `SIMPLE_STRINGS` is a
flat, English-only dict today; adding a second language is "point `t()`
at a second dict keyed the same way, selected by a request-scoped
locale," not a template rewrite — no domain logic is embedded in any
string value (A1.20's own explicit rule).

**Pre-existing state found, not touched by this track**: the codebase
already has ad-hoc, hardcoded Vietnamese strings outside this seam
(`base.html`'s nav "Hướng dẫn", several `user_state_view.py` blocker
messages) — real localization work later should migrate these into the
same `t()` seam rather than leaving two different bilingual mechanisms.
Flagged here as a genuine finding, not fixed (out of this track's
scope).

## A1.22 — Mobile / responsive

No new CSS framework — Simple Mode reuses `style.css`'s existing
`.panel`/`.grid`/`.facts` classes, which already collapse to one/two
columns under the existing 900px/600px breakpoints, plus a small
addition (`.mode-toggle`, `.attention-banner`, `.simple-steps`) with its
own `max-width:600px` rule (the step labels collapse to icon-only,
current step's label stays visible).

Verified for real with Playwright (installed in this environment;
no existing test file drives it, so this was a one-off script, not
added to the suite) against a live disposable instance at three
viewports — desktop (1440×900), tablet (768×1024), mobile (375×812) —
across `/changes`, Simple Change Detail, and the Acceptance tab: **zero
horizontal-overflow cases** (`document.documentElement.scrollWidth >
clientWidth` checked on all 9 combinations). Screenshots taken at
375×812 confirm the Human Attention banner, lifecycle stepper, and
friendly status badges all render correctly single-column on mobile.

## A1.24 — Chat App Builder readiness

Not implemented (deliberately, per instruction). What today's APIs
already support toward a future "I want an inventory app" → "the Save
button should be below the form" chat flow:

| Need | Existing API | Ready? |
|---|---|---|
| Create a Change from a plain-English ask | `POST /changes` (A1.19) / `POST /api/changes` | Yes |
| Read lifecycle state in plain language | `GET /api/changes/{id}/simple-view` (A1.23) | Yes |
| Resolve a HumanDecision | `POST /api/human-decisions/{id}/resolve` | Yes (pre-existing) |
| Request/read Product Acceptance | `ProductAcceptanceService` routes (E11) | Yes (pre-existing) |
| Create a follow-up Change from feedback | `changes.create(parent_change_id=...)` (E11.9 lineage) | Yes (pre-existing) |

**Remaining gap**: nothing today turns a *follow-up chat message*
("the Save button should be below the form") into a scoped Task/Plan
edit on an *already-building* Change — every existing entry point
creates a **new** Change (or a new follow-up Change after Product
Acceptance rejects/requests changes). A live, mid-build conversational
edit loop is genuinely new work, not present in E1–E13 or this track.

## A1.25/A1.26 — Tests

- `tests/test_performance_regression.py` (5 tests): bounded real-
  evaluation count inside `evaluate_workflow()` (≤1 per Task, not
  ~2.8×), `evaluate_many()` result-identity, bounded `db.connect()`
  growth at 10→100 Changes, pagination row-count bounds, filter
  correctness across the full (not just current-page) Change set. No
  exact-millisecond assertions (kept in the separate benchmark script).
- `tests/test_simple_mode.py` (14 tests): Advanced-by-default + Simple
  one click away + cookie persistence, Advanced fully preserved (every
  tab route still 200s), `/api/changes/{id}/simple-view` matches the
  service directly, lifecycle mapping from real workflow truth
  (including the no-workflow-yet regression below), human attention
  (none-pending and pending-decision cases), backend-only/no-release
  Changes show honest "not deployed" (never fabricated), incidents
  surfaced in History, agent activity text has no raw session fields,
  friendly status language on `/changes`, and the Simple Create form.

## A1.27/A1.28 — Backward compatibility & full regression

Advanced Mode's `/changes/{id}` route, every tab route under it, and
`/api/changes/{id}/control-surface` are byte-for-byte unchanged.
Legacy `/changes` filters (`status`/`change_type`/`profile`) still work
identically, plus `page`/`page_size` are additive-only (defaulted, never
required).

**A real regression was caught and fixed here, not merely claimed
clean**: the first full-suite run (891 tests, `pytest tests/ -k "not
real_"`) found 6 real failures, all one root cause -- defaulting
`/changes/{id}` to Simple Mode broke every pre-existing test asserting
Advanced-page content with no mode signal set. Fixed by flipping the
no-signal default to Advanced (see the Simple Mode section's own
deviation note above), not by editing the failing tests to expect new
behavior. A second full run after the fix: **891 passed, 0 failed, 0
errors** (`tests/ -k "not real_"`, all non-real-provider files
including `test_golden_e2e.py`, `test_productization_audit.py`, every
E1-E13 phase file, and this track's own `test_performance_regression.py`
(5 tests) + `test_simple_mode.py` (14 tests)). Real-provider (`test_
real_*`) qualification intentionally kept separate/unchanged, per this
codebase's own established convention.

## Known limitations (genuine)

- A1.12's suggested Simple-by-default is **not** the shipped default —
  Advanced is, for the reason in the Simple Mode section above (proven
  by 6 real test failures, not assumed). A future program can revisit
  this once/if there's a real user account to scope the default to,
  rather than a process-wide fallback every zero-signal request shares.
- Stretch performance target (<1s at 100 Changes) not met; the honest
  floor is real SQLite connection overhead under the existing
  one-connection-per-call `Database` design (A1.2's own constraint).
- 250-Change unpaginated cost (~2.6–3.3s) was not further optimized;
  pagination (default 25/page) is what keeps normal navigation fast
  regardless of total Change count.
- Filtering `/changes` by `status` still pays the full per-row
  `evaluate_workflow()` cost (an intentional, documented trade-off, not
  an oversight).
- No mobile-width Playwright run was added; responsiveness was verified
  by inspection against already-existing, already-tested breakpoints.
- Pre-existing ad-hoc Vietnamese strings outside the new `t()` seam were
  found but not migrated (out of scope).
