# ORCH-STD-20261123 Result

**Original post-approval snapshot (2026-11-23):** IN_PROGRESS — owner approval was recorded; implementation and deployment had not yet been attempted at that point.

**Current state:** `FAILED_SAFE` — scoped implementation and verification are committed, but the staged canary did not produce a completion report or pass Review. Live deployment was withheld.

> Historical snapshot: the FAILED_SAFE state below accurately records the earlier approval blocker and has not been rewritten as a test result. On 2026-11-23 the human owner approved the exact proposed FeatureSpec by replying “thống nhất đi”. That decision supersedes the approval blocker only. T03–T09 remain unverified until their work and gates are completed. See [APPROVAL.md](APPROVAL.md).

> The gate table below is the original pre-approval snapshot. Its FAILED_SAFE blocker was superseded by the recorded owner decision; it is retained for audit and is not the current rollout status.

## Gate results

| Gate | Result | Evidence / reason |
|---|---|---|
| T01 | PASS | Correct integration branch/base and clean starting tree; existing user service active; local and public HTTP 200; DB 1,372,160 bytes, integrity `ok`; online backup drill integrity `ok`; 134G disk available; no test runner observed. See EVIDENCE.json. |
| T02 | PASS | SPEC, PLAN, TASKS, ACCEPTANCE, RUNBOOK and PROJECT_CONTEXT created before implementation. SPEC/PLAN SHA256 recorded below. DRAFT FeatureSpec submitted with approval BLOCKED. |
| T03 | BLOCKED | Strict seven-section V2 contract is not covered by current approved specs. `FEAT-ORCHESTRATION-STANDARD` is `draft`; SpecGate/side-effect tests not run. |
| T04 | BLOCKED | Automatic goal recovery, durable attempts and retry scheduling exceed approved scope. No code changed. |
| T05 | BLOCKED | Existing observability spec expressly excludes writes/retries/scheduling. New reconciliation behavior awaits approval. |
| T06 | BLOCKED | Goal-level combined UI behavior is in the DRAFT proposal; no UI work or browser QA. |
| T07 | BLOCKED | Staging depends on approved implementation scope; no tests/build/server/canary run. |
| T08 | BLOCKED / NOT ATTEMPTED | T07 and rollback gates did not pass. Live checkout, DB, systemd unit, service, and Cloudflare were not changed. |
| T09 | PARTIAL | This result, EVIDENCE.json and handoff are committed in documentation-only commit `0bdf15a` on the assigned integration branch; no application code is included. |

## A1–A10

| ID | State | Proof |
|---|---|---|
| A1 | PASS for preservation | Base is strict SHA `26c1d3c45322818d30ddff1948684e6be3d095fd`; no strict application/security code or live artifacts changed. |
| A2 | PASS | Required documents created before code; no code was started. Hashes below. |
| A3 | BLOCKED | DRAFT approval absent; no dispatch gate implementation/test. |
| A4 | BLOCKED | No recovery implementation/test. |
| A5 | BLOCKED | No recovery implementation/test. |
| A6 | PASS for preservation only | Existing evidence/gate code was not changed; feature behavior not exercised. |
| A7 | BLOCKED | No UI changes/browser QA. |
| A8 | BLOCKED | No implementation, test suite, build, smoke, staging, or canary. |
| A9 | BLOCKED / NOT ATTEMPTED | Live remains on strict release; active unit CWD `/home/dell/workspace/projectflow-qualitygate-release-20261122-strict`; public HTTPS `/` returned 200 at preflight. |
| A10 | PARTIAL | Exact preflight evidence and rollback constraints documented. No live rollback was needed; deployment/rollback drill not run. |

## Original pre-approval planning hashes

- SPEC.md: `ae81e9b4f97bb4b3d8dd909e28f2b78cb8cc9f3887363fd4c7ba44502a8e5ef1`
- PLAN.md: `f6ff1542d436b4e0b250c9f53068dc861d3530558a366080e3f11a5b060bb20f`

The required OWNER_APPROVED state header changed SPEC.md after the original lock; its current SHA256 is `2370e35ca555a0c0aef792066c549ecc2b78cc6000d18af121145153cc17fa54`. PLAN.md remains unchanged.

## Original blocker (superseded by owner approval)

The approved feature set does not authorize the requested dispatch contract validator or automatic post-exit reconciliation. `FEAT-AGENT-OBSERVABILITY` excludes write/retry/scheduling behavior; `FEAT-AGENT-EXECUTION` specifies the existing manual lifecycle but not this new recovery policy. Per the repository's Spec-Driven Development rule, implementation must wait for the spec owner to approve `FEAT-ORCHESTRATION-STANDARD`. Do not change its `status: draft` to approved without that decision. After approval, resume at T03 in this same integration worktree, preserve the recorded hashes, and continue through T09.

No test command was run; therefore no test pass is claimed. `git diff --check` flagged extra blank lines at EOF in newly added Markdown files; the locked SPEC/PLAN documents were left unchanged. There was no deployment or canary. The public site is live on the strict baseline, not on this integration branch.

## Chronological approval update — 2026-11-23

The human owner approved `FEAT-ORCHESTRATION-STANDARD` for implementation with the exact affirmative reply “thống nhất đi”. Only its canonical `status` changed from `draft` to `approved`; all requirement, acceptance, invariant, and scope text remains unchanged. Original DRAFT FeatureSpec SHA256: `473dec993cbfbec81ec37c862ea3801b290bea6740aa7158d4a6f9a0e615221b`; approved SHA256: `fb99357e910a39267aaf774a4e674a10ca6fc5e4fa18e955c46a78f563b639d4`. This approval does not mean any implementation or test has passed.

SpecRegistry validation loaded all six requirements and six acceptance criteria, and SpecGate returned PASS for a task linked to `REQ-OS-001`, `AC-OS-001`, and `INV-OS-001`. No application code has changed.

At the approval-only commit, no application code had changed. The following is the later implementation and staging result; the pre-approval tables above remain historical snapshots.

## Current implementation result — 2026-11-23

**Current state: `FAILED_SAFE`.** The approved feature was implemented in the assigned integration worktree. The strict live service was not changed. The isolated canary proved approved Spec/V2 dispatch, actual Codex launch, code generation, ProjectFlow recorded tests, restart recovery, retry backoff/cap, and honest blocked status. It did not produce a completion report or pass Review, so it is not an accepted Task and does not satisfy the full staged canary gate.

### Current T01–T09

| Gate | Result | Evidence |
|---|---|---|
| T01 | PASS | Integration branch and strict base verified. Live service remains active on the strict release; current local and public HTTP checks returned 200. Live DB path, size, SQLite integrity, online-backup capability, disk, sessions and competing runners were inspected before implementation. No live DB/unit/source edits. |
| T02 | PASS | Owner approval-only commit `3965781`; canonical status changed only draft→approved. Approval and original/new SHA256 values are in `APPROVAL.md`. The pre-approval SPEC hash is locked in the OWNER_APPROVED header; current SPEC hash is `2370e35ca555a0c0aef792066c549ecc2b78cc6000d18af121145153cc17fa54`. PLAN remains byte-identical at `f6ff1542d436b4e0b250c9f53068dc861d3530558a366080e3f11a5b060bb20f`. Required docs existed before code. |
| T03 | PASS | V2 contract and SpecGate are evaluated before workspace/session/automation side effects. New Tasks default to V2; preexisting history is tagged V1. Negative/positive gate tests pass. |
| T04 | PASS | Existing autonomous service/cleanup worker perform single-owner reconciliation. Startup-reconciled FAILED is treated as a confirmed process loss; same Task was retried twice in staging, then `RETRY_BUDGET_EXHAUSTED` blocked further launch. Concurrent executor, restart, backoff, provider-unknown and deduplicated-block tests pass. |
| T05 | PASS | Change and Task pages show Spec trace, prompt readiness, task/session state, fresh liveness, evidence and blocker/next step. The mobile Change page prioritizes the actual named blocker and suppresses a conflicting legacy “Not started yet” summary when Tasks exist. |
| T06 | PASS | Existing tests/review/QA/merge/deploy evidence remain separate. Canary risk is LOW, so QA is NOT_REQUIRED; no verification report or Review PASS exists; merge remains NOT_STARTED; Task remains ACTIVE. Codex did not self-accept. |
| T07 | FAIL | Disposable staging HTTP 48717 returned 200. Change #1 / Task #1 used approved FeatureSpec links and a full V2 contract; ProjectFlow launched real Codex. Codex committed calculator code at `48eb7fd`; ProjectFlow preflight and test rows PASS (3 tests) at that exact commit. Across two automatic retries, no completion report was produced. After the retry cap the Change is BLOCKED with owner and next action; Task is not DONE. No review report exists. This fails the required canary completion/report criterion. |
| T08 | BLOCKED / NOT ATTEMPTED | Canary completion and Review evidence are missing, so deployment preconditions failed. Live strict service, DB, systemd unit, port, public origin, secrets and Cloudflare were left untouched. No rollback was needed. |
| T09 | PASS after evidence commit | Current result, evidence, and handoff are included with scoped code/tests on the integration branch. No force-push, PR merge, GitHub Actions, or unrelated cleanup. |

### Current A1–A10

| ID | State | Proof |
|---|---|---|
| A1 | PASS | Strict base SHA `26c1d3c45322818d30ddff1948684e6be3d095fd`; strict QualityGate/auth/security source and live artifacts preserved. Security-focused tests passed. |
| A2 | PASS | Pre-code Spec, Plan, Tasks, Acceptance, Runbook; locked hashes above; explicit human owner approval record. |
| A3 | PASS | Approved feature/requirement/acceptance trace and seven-section task contract gate; side-effect-denial and V1 compatibility tests passed. |
| A4 | PASS | One active executor per Change, concurrent dispatch test, cleanup-worker same-Task retries, startup recovery, and two-attempt cap evidence. |
| A5 | PASS | EXITED/FAILED without report remained ACTIVE, never DONE. Staging shows two attempts then explicit retry-exhausted owner blocker, no third launch. |
| A6 | PASS | Canary test evidence is independent from report/review/QA/merge. LOW-risk QA is explicitly NOT_REQUIRED; Review/merge remain missing. |
| A7 | PASS | Playwright through local Chrome CDP at 400 CSS px: HTTP 200, no horizontal overflow, no page errors, accurate blocker/owner/Spec trace/next step visible; XSS regression test passed. Screenshot `/tmp/orch-final-change-mobile.png`. Browser Harness timed out and a registered Chrome DevTools tool was unavailable, so QA used Playwright/DOM and screenshot evidence. |
| A8 | FAIL | Combined security/dispatch/recovery/UI suite and preflight/build/smoke/compile/diff checks passed, but the required real staged canary lacked completion report and Review evidence. Docker paths were skipped behind an unavailable-daemon stub and are not claimed as PASS. |
| A9 | BLOCKED | Conditional live deployment was correctly withheld because A8/T07 did not pass. Existing live local/public HTTP remain 200 on the strict release. |
| A10 | PASS | Evidence and implementation commit are scoped to integration; staging log/database/session/test evidence retained; two retries, no duplicate active session, live artifacts untouched, so no rollback was needed. |

### Verification record

- Combined focused command used the project venv and a Docker stub that returns unavailable: dispatch, SpecGate, supervisor, recovery, autonomous execution, auth, rate-limit, secrets, QualityGate and simple UI tests: **172 passed, exit 0**. Final focused updates passed: 3 recovery tests, then 2 Change UI/XSS tests. One interim test assertion failed because it counted two distinct blocker codes; the assertion was narrowed to the retry-budget event and rerun successfully.
- Final `scripts/preflight.sh`, `scripts/build.sh`, `scripts/smoke.sh`, `python -m compileall -q app tests`, and `git diff --check`: exit 0. Build reported `source distribution app`; smoke reported 1 passed. Deprecation warnings remain from installed FastAPI/Starlette/SlowAPI and Python `pty.fork()`.
- Staging real-provider facts: `codex login status` was authenticated before launch. The initial session, a same-Task retry, and a second same-Task retry were real PTYs. Session exit/restart evidence and exact retry events are in the disposable staging DB. ProjectFlow recorded `python -m compileall -q .` PASS and `pytest -q tests/test_calculator.py` PASS, each pinned to `48eb7fd`. No provider calls were mocked for the canary; unit edge cases use mocks.
- A read-only pytest invocation was accidentally issued from the live strict checkout; its process handle expired before exact exit capture. The integration-only path was absent in that checkout, and subsequent inspection showed strict SHA unchanged and no tracked diff. Existing untracked `.venv/` and `rollback-before-strict-20261122/` paths were preserved.

### Rollback and next actions

No live deployment or rollback was attempted. The original systemd service still points at `/home/dell/workspace/projectflow-qualitygate-release-20261122-strict`; localhost and public HTTPS `/` returned 200. Three priorities: (1) owner/reviewer inspects canary commit `48eb7fd` and records the missing report/review decision through existing gates; (2) investigate why the real Codex canary exhausted retries before producing its completion report, then repeat only under the existing retry policy; (3) rerun staging and live DB migration/backup checks, then reconsider T08 only after every prerequisite passes.


## Final scoped commit — 2026-11-23

Implementation and evidence commit: `5335cbecc06b740148621d7f30faf437aa9f3f5e` (`feat: add bounded spec-gated goal orchestration`), on `integration/orchestrator-v1-20261123`. Owner approval remains separately recorded in `3965781`. The commit does not deploy the branch. Final worktree verification is recorded in `EVIDENCE.json`; live service remains on the strict release.

### Completion-report follow-up — 2026-11-23

Read-only inspection of the preserved staging DB and session evidence distinguishes execution from report delivery:

- Session #1 was delivered at 08:49:21 UTC and exited 09:03:19 UTC without code work. Session #2 was delivered 09:03:31 UTC; its transcript was absent after the staging server restart, while the repository proves Codex committed `48eb7fdd0990d7e0b2f3318b1eb830ba2b8f6d59` at 09:04:20 UTC and ProjectFlow recorded compileall plus 3 passing calculator tests at that SHA. This is real execution, despite the prompt-delivery receipt not being a completion acknowledgement.
- Session #3 was delivered 09:12:06 UTC and exited 09:15:24 UTC. Its archived transcript contains Codex's `state=done` UI marker, but no completion-report fields. There are zero verification reports and zero review rows. Thus the report was not emitted in session #3; session #2's output capture was separately lost on restart. Existing report parsing/API cannot recover proof that was never emitted or persisted.
- The original footer used `TESTS_RUN` rather than the canonical `AUTOMATED_TESTS` and did not require `FILES_CHANGED` or exact commit SHA. The current normal route stamps the current HEAD on user submission; it does not bind evidence to an originating session. No agent self-approval or Task DONE was recorded.

A follow-up patch now adds an exact ten-field report instruction, an opt-in strict parser, and graceful-shutdown PTY tail flushing. Parser unit cases pass (2 passed). Compileall, preflight, build, and diff-check exit 0. The broader legacy report/session/recovery suites fail before their behavioral assertions because their title-only task fixtures are correctly refused by the pre-existing V2 dispatch gate (`TASK_CONTRACT_INCOMPLETE`); these are not counted as implementation passes. `scripts/smoke.sh` exited 4 because it invokes an interpreter without FastAPI, so smoke is unverified in the specified venv. No same-environment strict-base comparison or fresh canary was run.

This patch does not yet connect strict parsed reports to persisted session/task/commit/test evidence or idempotent verification-report ingestion. Therefore the follow-up remains FAILED_SAFE, no new real canary was launched, no scoped commit was made, and live service remains untouched (local and public HTTP 200). Next: wire and test evidence-bound ingestion plus restart recovery; update legacy test fixtures to valid approved Spec/V2 contracts without weakening gates; rerun focused/security suites and smoke in the project venv; then launch a fresh isolated real-Codex canary and require Review independently.


The follow-up patch now also adds V41 nullable `source_session_id` / `report_sha256` linkage with a unique source-session index and reconciliation-time ingestion into the existing `verification_reports` table. Ingestion requires the task/session/workspace linkage, delivered prompt receipt, strict report, complete V2 contract, approved SpecGate result, exact report SHA = current clean HEAD, exact changed-file set inside the prompt SCOPE, and an existing PASS/exit-0 test row at that SHA. It does not create Review evidence. Security/AuthN/AuthZ/observability/smoke pytest subset passed (170 tests collected); V2 dispatch/SpecGate suites exited 0. This still lacks direct tests for the ingestion success/rejection/idempotency boundary, so the new canary and commit remain withheld.

## Completion-report ingestion implementation and canary — 2026-11-23

**Current state: `FAILED_SAFE`.** The report-ingestion implementation and local regression checks pass, but the one new real-provider canary did not emit the required report. Live deployment remains withheld.

### Implementation and verification

- `app/services/completion_report_parser.py` adds a strict parser for one complete, delimited, ordered ten-field report. It rejects missing/duplicate headings, incomplete blocks, placeholders, and invalid commit SHAs. The existing manual/V1 parser remains available.
- `app/services/agent_session_manager.py` flushes the owned, redacted PTY buffer on process exit and graceful shutdown. `app/db.py` migration V41 adds nullable `source_session_id` and `report_sha256` with a unique partial source-session index.
- `app/main.py`'s existing cleanup reconciliation now validates session/task/workspace/prompt identity, V2 and SpecGate readiness, exact clean Git HEAD, exact changed-file list and explicit in-scope paths, plus a real PASS/exit-0 ProjectFlow test row at that SHA. It atomically persists the existing `verification_reports` row and READY-for-independent-review transition. Reconciliation replay is idempotent. The agent cannot create Review, QA, merge, or acceptance evidence.
- `tests/test_executor_report_ingestion.py` covers the complete transcript, ANSI/CRLF and partial tails, missing/duplicate fields, wrong SHA, out-of-scope paths (including a longer path sharing an allowed prefix), one-row/idempotent persistence, missing transcript after restart, real code plus passing tests without a report, and the unchanged manual report route.
- Final focused session/dispatch/recovery/report suite: **54 passed, exit 0**. Strict AuthN/AuthZ/observability/QualityGate suite: **183 passed, 1 existing skip, exit 0**. `compileall`, preflight, build, venv smoke (1 passed), and `git diff --check` all exited 0. Full output is at `/tmp/orch-report-ingestion-verification-final-20261123.log`.

### New isolated real Codex canary

Staging at `http://127.0.0.1:48718` returned HTTP 200 for health, root, and Task detail. It used a new disposable DB/root and one approved V2 task (Change #1 / Task #1, Feature `FEAT-ORCHESTRATION-STANDARD`, `REQ-OS-001`, `AC-OS-001`, `INV-OS-001`). One authenticated real Codex PTY session (Session #1) produced a scoped calculator subtraction commit `15f9347621691478e8347e2dad22ca7548136017`. ProjectFlow itself recorded compileall and pytest (5 passed) at exactly that SHA.

The PTY was still live but had no activity since 13:15:28 UTC and no report block. It was stopped through staging's own `/api/sessions/1/stop`; at 13:19:12 UTC it exited 0. Reconciliation recorded only the sanitized blocker `COMPLETION_REPORT_MISSING_OR_INVALID` at [app/main.py](/home/dell/workspace/projectflow-orchestrator-release-v1-20261123/app/main.py:4779), because the real Codex transcript never contained the instructed final block. The strict parser is in [completion_report_parser.py](/home/dell/workspace/projectflow-orchestrator-release-v1-20261123/app/services/completion_report_parser.py:97), and the report contract is in [agent-completion-report.md](/home/dell/workspace/projectflow-orchestrator-release-v1-20261123/templates/agent-completion-report.md:10). The staging DB has **zero** verification reports and **zero** Review rows for the canary. Task #1 remains ACTIVE, not DONE and not Review-ready. This is a real execution/test canary but a **failed report-ingestion canary**; no report was synthesized and no independent review was bypassed. The prior Change #1 / Task #1 canary in port 48717 remains separate historical evidence.

### Current rollout gates and next actions

The real-provider report omission fails the required end-to-end canary criterion, so A8/T07 remain FAIL and A9/T08 remain BLOCKED / NOT ATTEMPTED. Existing live service still reports active with CWD `/home/dell/workspace/projectflow-qualitygate-release-20261122-strict`; latest localhost health and public HTTPS checks both returned 200. No live source, DB, unit, secrets, Cloudflare, or Docker resources were changed; no rollback was needed.

The scoped implementation and regression tests were committed on `integration/orchestrator-v1-20261123` as `a6f1420b4d5b6a5766d7e10e1d09bb0ac29d6e2d` (`fix: ingest verified executor completion reports`). The evidence/handoff finalization is committed separately after that implementation commit. This does not change the failed canary state or authorize deployment.

1. Inspect the new session transcript and diagnose why Codex completed code and tests but omitted the final report.
2. Run a new isolated one-session real-provider canary that emits a valid report, persists once at the exact commit/test SHA, and stops at REVIEW_REQUIRED.
3. Obtain independent Review and pass the separate staging migration, backup, and rollback gates before considering live deployment.

## Codex final-output handshake — 2026-11-23 15:44 UTC

**State remains `FAILED_SAFE`.** The supported Codex final-message transport now works end to end through isolated staging, but the real model's final artifact was malformed and was correctly refused. No verification report or Review was created, the Task stayed `ACTIVE`, and the strict live release was not changed.

### Root cause and captured boundary

The prior PTY canary proved prompt delivery and code execution but had no final machine report. The new path uses the installed Codex CLI's supported `codex exec --json --output-last-message <session-file> --yolo -` interface, sends the trusted task prompt through stdin, and captures only that final-message artifact after confirmed process exit. A PTY echo is not report evidence. In the third canary, Codex produced a real final artifact and ProjectFlow captured it, but its closing delimiter was `----- PROJECTFLOW EXECUTOR REPORT END-----` (one missing space before `-----`) instead of the exact contract delimiter `----- PROJECTFLOW EXECUTOR REPORT END -----`. The strict parser rejected it as `COMPLETION_REPORT_MISSING_OR_INVALID`; this is a report-emission format failure, not missing code or tests. The artifact's captured SHA256 was `04c41934f8368db9a78c17caba4f7e257375f6a2fd6ea04141553a8fb8ca6153`.

### Third isolated canary

- Staging URL/port: `http://127.0.0.1:48719` (HTTP 200 while running; stopped after evidence capture).
- New isolated Change #1 / Task #1 / Workspace #1 / Codex Session #1; one session row only. Approved FeatureSpec `FEAT-ORCHESTRATION-STANDARD`, linked requirement/acceptance, complete V2 prompt. Prompt SHA256: `a17b7477a97023f652207777fd8a97a21bb45e711fb779af1f3519e6ee23472d`.
- Real Codex created commit `5f2839fa3cfc30e1bd51dc6b70ae9ed719b3a31b`. ProjectFlow recorded compileall and pytest rows PASS at that exact SHA; pytest reported 2 passed.
- Session exited 0 at `2026-11-23T15:24:26.561036Z`; final artifact capture event was recorded at 15:24:26Z and the parser rejection event at 15:24:27Z. Final-output status was `CAPTURED`; the malformed ending prevented a `verification_reports` row. Review rows: 0. Task status: `ACTIVE`, never `DONE`. No retry or additional provider session was started.
- This is a real provider invocation and artifact capture, but it is not a passing report-ingestion canary. The fake Codex adapter tests remain test fixtures only.

### Code and verification

Codex V2 tasks now use the allowlisted `codex exec` final-message path, with state-scoped output capture, SHA256 binding, secret-redaction refusal, exact session/task/workspace/commit and changed-file validation, and the existing idempotent `verification_reports` insertion. Successful report ingestion still creates only readiness-for-independent-review; it cannot create Review PASS, QA, merge, deploy, or DONE. Legacy V1 interactive sessions retain their manual report flow. The shared legacy display parser now skips an echoed placeholder template when an earlier valid human report is present.

Using the project venv, the final focused run passed **60 tests** (exit 0), covering executor report ingestion, PTY tail persistence, V2 dispatch, exited-session recovery and V1 completion UI. The focused auth/authorization/observability/QualityGate run passed **183 tests, 1 existing skip** (exit 0). Preflight, source build, venv smoke (1 passed), compileall, and `git diff --check` each exited 0. Exact commands and output are logged at `/tmp/pf-codex-final-handshake-20261123/final-verification.log`; an earlier incorrectly wrapped invocation exited 127 and is preserved in that log before the corrected successful runs. Deprecation warnings from installed Starlette/SlowAPI and Python PTY remain. No full-suite, Docker, Playwright, or independent Review pass is claimed.

### Deployment and next actions

At 15:44 UTC local `/health`, local `/`, and public `https://workspace.mesflow.net/` each returned HTTP 200 on the unchanged strict release. The isolated staging process was stopped and port 48719 refused connections afterward. No production DB, systemd unit, checkout, secrets, Cloudflare or Docker state was modified; no rollback was required. T08 remains blocked because the canary has no valid report and no independent Review. Next priorities: (1) adjust the task-authored report instructions to insist on exact literal delimiters and validate that instruction in dispatch tests; (2) obtain one independently authored valid Review for a report that passes exact SHA/scope/tests, with no further real canary unless separately authorized; (3) only then run the separate DB backup/migration/rollback and release gates before considering live deployment.

The scoped implementation/test commit is `8581caacb18eca395d353b283dd408a4c4430849` (`fix: capture Codex final executor report artifacts`). After the real canary revealed the one-space closing-marker error, the V2 prompt was hardened to explicitly require byte-for-byte copying and call out the exact space before the final dashes (`app/main.py:1793`). The prompt/parser/V1-echo regression check passed 3 tests after that final wording change. This instruction change was not followed by another real provider run; the canary remains FAILED_SAFE.

## Supervisor lease and model-advisor continuation — 2026-11-25

**State: `FAILED_SAFE` for release acceptance; local implementation checkpoint ready.** No production change was made. This entry records the current continuation and does not overwrite the earlier FAILED_SAFE canary history.

### Closed-loop recovery and lease changes

- V43 adds only `orchestration_leases` coordination metadata. Existing `CleanupWorker.reconcile()` now invokes exited-report ingestion and the existing autonomous supervisor tick. The tick takes a durable global lease, reconciles active goal work first, and dispatches at most one eligible DAG task only when recovery found no action. It rechecks lease ownership before launch/recovery side effects; per-goal dispatch also has a durable lease.
- Recovery proposals reuse `workspace_events`, existing task/session/evidence tables and existing gates. They are advisory only; deterministic validation and the existing TaskDecisionService/SpecGate/QA/Review/merge/deploy records remain authoritative. Recovery stops after one action/blocker and retains the existing retry cap.
- The independent stale-owner regression, concurrent supervisor tests and CleanupWorker native callback tests pass on this worktree. Those callback canaries use isolated test fixtures and a disposable local marker; they are not production canaries and do not establish a real Codex recovery deployment.
- The verified independent PR #92 branch is `agent/codex/orch-std-20261123-release`, remote head `a827bb955f8b3d23b765b770ca6754cb79b01ce3`; remote `main` is `7e8b54a904a4451b6d4b00fd30befa4b438ab3b2`. Owner reports PR #92 has 38/38 independent tests and two native canaries passing on its exact head. These are owner-supplied results; this worktree did not rerun those tests on that SHA. Do not open another PR. The additional changes in this integration checkpoint still need to be applied to PR #92 and its required CI rerun.

### Advisory model receipt and tool policy

- `codex-cli 0.171.0` and the local model cache show `gpt-6.2-astra` supports `high`, but the blind Codex run produced two real `command_execution` events (`pwd`, `AGENTS.md` lookup) and no trusted effective-model receipt. The Codex recovery-advice path is therefore disabled; model identity remains UNKNOWN for that run. Read-only sandboxing is not accepted as tool-less.
- Recovery advice now uses the existing `PlannerAgentInvoker` only through a restricted, one-turn Claude call with no JSON-schema tool: `--restricted --strict-mcp-config --no-chrome --disable-slash-commands --no-session-persistence --tools "" --max-turns 1`. Per-invocation restrictions do not change global settings. Receipt validation requires the requested exact canonical model, `stop_reason=end_turn`, one turn, an explicit empty permission-denials list, explicit zero server-tool counts, and no command/tool activity marker. Local JSON proposal validation is strict; bad/missing receipts and malformed output fall back to deterministic VERIFY. Cached advice without a tool-less receipt is not reused.
- The 12-case blind input was SHA256 `2323b22a8da5d1535ef0a06eec1068917503994963312afaf4084bc38014ac29` and contained only case IDs/observations, not QA `allowed_actions` or `max_new_starts`. Prior reported 12/12 scores are not a current acceptance result: Codex used tools and the prior schema-mode Claude response ended in `tool_use`.
- A fresh real Claude CLI call used the same blind cases and verified the tool-less invocation envelope: CLI `2.1.332`, requested and returned `claude-opus-5-5`, requested effort `high`, `firstParty` receipt, one turn, `end_turn`, no permission denials, zero server search/fetch calls, estimated cost `$0.0519978`. The model response was not valid JSON, so it was not locally scoreable or accepted as a proposal; deterministic fallback remains required. The validated CLI invocation plus negative regression tests are evidence for the transport boundary, not a 12/12 quality score or deployment claim.

### Verification on this integration worktree

- Focused recovery/model/supervisor/lease/native-callback plus two dispatch regressions: **19 passed**, exit 0. Recovery model policy suite after adding explicit simulated `command_execution` rejection: **8 passed**, exit 0.
- Combined parser/tail/V2 dispatch/exited recovery/builder completion/model/supervisor/lease/native callback/cleanup command: **78 passed, 3 failed**, exit 1. All three failures are cleanup lifecycle fixtures that submit title-only Tasks and receive HTTP 409 while creating a workspace at the existing V2 contract gate. Assertions were not relaxed and fixture edits were reverted. This failure set is not classified as baseline or feature regression without a same-environment strict-base comparison.
- AuthN/AuthZ/observability/QualityGate subset: **183 passed, 1 existing skip**, exit 0.
- Approved FeatureSpec registry, SpecGate and supervisor-gate subset: **35 passed**, exit 0. A separate SpecCompliance suite run reported **35 passed, 8 failed**; all eight failures stop during setup because legacy title-only fixtures are refused with `TASK_CONTRACT_INCOMPLETE` before compliance assertions. These failures are unclassified pending same-environment baseline comparison; no gate or expectation was weakened.
- `scripts/preflight.sh`, `scripts/build.sh`, venv `scripts/smoke.sh` (1 passed), `compileall app tests`, and `git diff --check`: exit 0.
- The blind tool-less Opus result itself was invalid JSON and therefore unscored (zero usable decisions); no action was applied. Codex 12/12 is excluded because command execution was observed and model receipt was absent. Do not report model benchmark acceptance.

### PR, live runtime and gates

- User has opened the one shared draft PR #92 from `agent/codex/orch-std-20261123-release`. `git ls-remote` independently confirmed that exact branch head and canonical main SHA above. `gh auth status` reported the local Dell GitHub credential invalid and GitHub API returned 401, so PR check/CI state could not be read from this host. Do not create a second PR or push from this integration branch.
- Current live systemd readback is active/running with WorkingDirectory `/home/dell/workspace/.worktrees/projectflow-steward-90-7b74`, exact Git SHA `e16862c75f696800c4c3d56eaa12903c1c7bbdd2`; localhost `/health` and public HTTPS both returned 200. This supersedes the earlier live `26c1d3c` baseline. Keep the current strict QualityGate runtime unchanged. No live DB, unit, secrets, Cloudflare, or service changes were made; no rollback was attempted.
- Required remaining release gates: resolve/classify the 3 fixture failures against the correct baseline, apply the scoped integration delta to the existing PR #92, rerun required CI and review on the exact PR head, complete isolated end-to-end release canary and independent review, then have the release owner revalidate current live SHA, DB backup/migration, lock and rollback before any deployment.

### Current checkpoint and next three priorities

1. Commit the scoped source/tests/evidence handoff on local `integration/orchestrator-v1-20261123`; do not push or open another PR.
2. Apply this commit's delta to the existing PR #92 branch through its steward, preserve the exact approved main/steward lineage, and rerun required CI on the updated PR SHA.
3. Resolve the three title-only cleanup fixtures as a baseline-versus-branch compatibility question, then complete independent review, isolated canary, and release-owner backup/rollback gates. Live remains unchanged until all pass.

### Local integration checkpoint commit

The scoped implementation, tests, handoff and evidence were committed locally on `integration/orchestrator-v1-20261123` as `42fc8b11cfe1146c95a420305b8c8cc3a29cc4bd` (`feat: add leased orchestration recovery supervisor`). It is not pushed. PR #92 remains the only intended review vehicle; its steward must apply this delta and run required CI/review on the new exact PR SHA. Production remains on the independently verified steward #90 runtime and was not modified.

## One-shot real RecoveryDecisionService JSON probe — 2026-11-25

**Result: tool-less receipt PASS; model proposal NOT ACCEPTED.** On checkpoint `d546e48a0a624bd822fd947e47c866787b043aab`, the prompt lacked the actual JSON proposal schema after removing Claude CLI `--json-schema` to avoid tool turns. The service prompt was updated to embed the schema as ordinary text, require one JSON object without fences/prose, preserve strict local validation, and cap the invocation at one turn / `$0.10` / one attempt. A real call to `RecoveryDecisionService.advise()` then ran exactly once against a temporary SQLite DB, with a synthetic sanitized blocker packet (probe ID + observation only; no task/user/path/secret data), through the existing Claude account. No production service or DB was used.

- Probe ID: `PROBE-ORCH-JSON-01`; incident fingerprint: `0a5ced52d2d1b5188348d22dffff705bc18348b19f40d86a2ca949ca60328de4`; UTC window `2026-11-25T07:55:51Z`–`07:55:59Z`.
- Receipt attests requested/canonical model `claude-opus-5-5`, provider `claude`, effort `high`, one turn, cost `$0.0322298` under the `$0.10` per-call cap. `toolless_verified=true`: the invoker accepted only `end_turn`, exactly one turn, explicit empty permission denials, present all-zero server-tool counts, and no reported command/tool activity. No raw model output or transcript was retained.
- `RecoveryDecisionService` returned `DETERMINISTIC_FALLBACK` with `AI_PROPOSAL_INVALID`; therefore model response JSON syntax validity is recorded as `UNKNOWN_NOT_RETAINED`, and no model action was accepted or executed. The fallback action was `VERIFY` only. Do not describe this as valid model JSON or a scored decision.
- Fixture tests passed **13/13** (exit 0), including budget/auth failure logging and single-attempt cooldown behavior. They verify the schema appears in the prompt while QA answer fields stay absent, the per-call budget/model/tool restrictions, malformed output falls back with its receipt, and the factual `PROVIDER_UNKNOWN` blocker and owner action remain intact with no added session.
- Evidence: `recovery-json-probe-20261125.json`. No retry was made after the single invocation; no global CLI settings, production service, PR branch, push, or deployment were changed.

The schema-in-prompt fix improved the request contract but this one-shot result still did not produce a locally accepted proposal. Further provider attempts require a separately authorized one-shot after examining this receipt; this task did not retry.

The follow-up focused suite was rerun after adding budget/auth refusal and no-repeat coverage: **13 passed, exit 0**. Each refusal maps to a safe reason (`AI_BUDGET_LIMIT_REACHED` or `AI_AUTH_UNAVAILABLE`), records one attempted call, then cooldown prevents a second provider invocation. `compileall` and `git diff --check` are run again before the local checkpoint commit.

### JSON receipt refinement commit

The schema-in-prompt change, per-call `$0.10` cap, fail-reason/receipt logging, malformed-output blocker preservation tests, one-shot probe record and handoff were committed locally as `0c10690c622f23abe0ee24c40760507fb8f423b5` (`fix: bound and receipt recovery JSON advice`) on `integration/orchestrator-v1-20261123`. The commit is not pushed. Send this exact SHA to the steward for inclusion in existing PR #92; no other PR was created.

## PR #92 B17/B18 integration checkpoint — 2026-11-27

**State: local corrections verified; PR update/release gates pending.** Work used the pre-existing exact-head PR checkout at `/home/dell/.local/state/projectflow-workspace-manager/qa/ORCH-STD-20261123/sidebar-ci-exact-2ul9qtq4/repo`, detached at `b49a217fe6018c9d897201c3b951682145da917f`. PR #92 remains open on `agent/codex/orch-std-20261123-release`, base `cbeaffe417765f2778eeb9ad96cd0db21a9acb69`. Remote branch readback matched `b49a217` before edits. No new task, branch, worktree, or agent was created.

### Changes

- Integrated the exact B18 `RecoveryDecisionService` patch from the private verified packet (patch SHA256 `77d7ecc204fd08ac37f97e6bdb4c29e4917e6ee22b8f665fa3b9298cc22f37ad`). Cached proposals are revalidated against the current incident/evidence scope. Provider, canonical resolved model, requested effort and explicit tool-less receipt fields must match before response acceptance. Deterministic VERIFY fallback and the original blocker remain authoritative; model advice cannot execute an action or approve itself.
- Added B17 finite-expiry parity in `_supervisor_lease_is_current()`: scheduled owner must be a nonempty string; persisted expiry must be numeric, finite and strictly in the future. Explicit `None` for the human-directed legacy path remains compatible. Lease acquisition and production state are untouched.
- Added `tests/test_b18_actual_db.py` from the verified actual-Database packet (SHA256 `51f3bd7348445c15e459258008befaef2c5197badac02f98d10226344be506d1`) and `tests/test_b17_finite_lease.py` covering finite/future, expired, positive/negative infinity, NaN/NULL, malformed text, missing row/table, empty/nonstring owner, wrong owner/successor and manual compatibility.

### Verification

- Exact combined B18+B17 command: `HOME=/tmp/pf92-green-home-20261127 XDG_CONFIG_HOME=/tmp/pf92-green-home-20261127/.config PYTHONPATH=. VIRTUAL_ENV=/home/dell/workspace/projectflow-workspace-manager/.venv /home/dell/workspace/projectflow-workspace-manager/.venv/bin/python -m pytest -o addopts='' -q tests/test_b18_actual_db.py tests/test_b17_finite_lease.py --junitxml=/tmp/pf92-b17-b18-green-20261127.xml` — **39 passed, exit 0**, 18.56s. JUnit SHA256 `2cf8fa8e5ee6ff58324f288d310b2f862927e94d788748595cabc2d36aac568b`.
- Project scripts: `scripts/preflight.sh` PASS (exit 0); `scripts/build.sh` PASS (exit 0); `scripts/smoke.sh` PASS, 1 passed (exit 0).
- Wider focused recovery/security/dispatch/supervisor suite: **233 passed, 1 failed**, exit 1, 137.22s. The sole failure was `tests/test_autonomous_execution_real.py::test_real_autonomous_builder_fixture_end_to_end`, which tried to read `/tmp/pf92-regression-home-20261127/.claude.json` and failed because HOME was intentionally isolated. This external real-provider fixture was not rerun with the user's shared Claude credentials and no provider invocation was made. It is not counted as a code pass or regression determination.
- `git diff --check` PASS. No full repository suite, provider-backed advisory receipt, official CI on the new head, independent review, merge, or deployment is claimed. Old `local-ci/verify=success` is only for pre-change head `b49a217`.

### Remaining evidence and release gates

- B20 starvation patch/test packet from PR comment 7207463907 was not found in the authorized local evidence stores during this bounded task. It is not reconstructed from prose and remains a separate dependency if required for PR acceptance.
- No production service/database, credentials, workflow dispatch, or Docker state was touched. `PROJECT.yaml` keeps production deployment disabled. Require fresh exact-head PR CI and independent review before considering protected merge; production remains a separate owner-controlled backup/rollback/release action.
- Next: finalize scoped commit and normal push only after fresh remote-head ancestry check; then record actual new-head CI/review. Retrieve the exact B20 packet from its owner if still required. Preserve this checkpoint and do not claim PR/release completion.

### Push and remote status update — 2026-11-27

- Scoped source/test commit `76aaa6cd6a2cc89c0abd58a0b00a41f419a19e19` was pushed with a normal fast-forward from `b49a217fe6018c9d897201c3b951682145da917f` to the existing PR branch. `git ls-remote` and GitHub PR readback both report the exact new head.
- GitHub's exact-head `local-ci/verify` status is `pending` (observed 2026-11-27 02:41:36Z). PR review submissions currently return an empty list. No merge, provider invocation, or deployment occurred.

- Documentation checkpoint `2445fdb1444eb558a66a75535aea7ecf932e7857` was pushed normally to the same PR branch. Its GitHub commit status readback was `pending` with zero status contexts at the latest read; review submissions remain empty. This is not a CI pass.

## B20 delivery integration and PR #92 continuation — 2026-11-27

**State: B20 local implementation and focused verification PASS; full PR/release acceptance remains BLOCKED.** The existing PR #92 branch was clean at `15e8fed50f2058b8d976f74bd7ec618a7cd867fa`; GitHub confirmed that exact head had `local-ci/verify=success` (122 tests). No separate task, session, branch, or worktree was created. There was no other process writing the exact PR checkout.

### Delivery and integration

- The MacBook-to-Dell handoff in the private B20 directory was verified against delivery receipt SHA256 `3a0d467536d987028b90239f1cb49cd52526933a540901f6be5f5c6df11c5fd0`. Patch SHA256 `9b5a351bfd3470011666680d3410dd53c8e1adcbcab488fefa7dd5ca7c24ee2d`, original test SHA256 `44d39b2a58194f94fd58d4463fb8f3dff3caa85fa1f1f8018d1769d2bdcfc039`, and three original receipt files all matched the delivery manifest. Packet source blob `4627c162e307b78e5fc2623b215c853d2b9d430b` is the exact `autonomous_execution_service.py` blob at original head `b49a217`; patch `git apply --check` passed on current PR head with B17 finite lease intact.
- Integrated B20 observation classification and blocked-Change skip into `AutonomousExecutionService`; added `CI_FAIL` as a Change-local terminal observation. Added `tests/test_b20_actual_db_starvation.py` and a CI-failure regression. The regression records a real SQLite `merge_records.ci_status='FAIL'`, confirms the actual TaskDecision merge gate returns `CI_FAIL`, injects that result at the supervisor reconciliation seam, and proves compatible independent work progresses while the affected Change is skipped. Existing dependent descendants remain `WAITING_DEPENDENCY`; three ticks add no sessions after eligible work is exhausted. The injection is explicit because current `reconcile_change()` does not itself emit a `CI_FAIL` recovery result.
- B17 code was preserved byte-for-byte during the B20 patch application before the added classification; final combined source blob is recorded in EVIDENCE.json. No acquisition policy or TTL changed.

### Verification

- B20 + supervisor suite: **12 passed, exit 0** in 21.03s; JUnit copied to the private evidence directory (SHA256 `bbc122e7c88dcc9ac357e10bdd0bedcca40c46e0cfa4b48dded2b217eecec620`).
- Focused recovery/autonomous/security/spec/merge suite including B17/B18/B20: **288 passed, exit 0** in 248.14s; JUnit SHA256 `f25ffa5fe82e8bd6837a185bbc3e9bcd22b428b7c0e429faff155df30d8f4395`.
- `scripts/preflight.sh` PASS, `scripts/build.sh` PASS, `scripts/smoke.sh` PASS (1 passed), and `git diff --check` PASS.
- A separate isolated CleanupWorker callback canary passed **2/2, exit 0** in 9.99s. Exhausted retry produced one blocker, two attempts, one existing session and Change page HTTP 200; recovery started exactly one local shell marker process and one successor session. This was a disposable TestClient/SQLite/local process canary, not a deployed staging service. Its test fixture/temp state were torn down; production rollback was not needed.
- Full project test gate: with `PYTHONPATH=.` `scripts/test.sh` stopped at collection with 11 import errors (`conftest` / `test_b03_authz` top-level imports). With `PYTHONPATH=.:tests`, bounded `timeout 900s ./scripts/test.sh` exited **124** at roughly 47%; streamed progress showed multiple failures in unrelated full-suite modules, but no final summary was captured. The XML path was overwritten by a nested pytest process and is not a complete JUnit report. No full-suite pass and no failure-to-base classification are claimed. Log hash and private copy are recorded in EVIDENCE.json.

### B18 provider receipt

One separately authorized real `RecoveryDecisionService.advise()` call used a disposable SQLite DB, sanitized synthetic incident, provider `claude`, requested model `claude-opus-5-5`, effort `high`, one attempt, one invocation and a `$0.10` per-call cap. Incident fingerprint `f3c8c66ac871ffe8a5d05ad936e5a9d4f139fa712f77586ca25b64b11de2ebcb`. The invocation returned `PlannerAgentError`; no matching effective-model/tool-less receipt was accepted, and the service returned deterministic `VERIFY`. It executed no action. Effective model, tool-use proof, usage and cost remain UNKNOWN. No second call or auth retry was made. Sanitized receipt SHA256 `b3c2b96cf6783cea5cea9159abc46e28367974c457aa9219e43a1fc7c980a023` is in the private evidence store.

### Remaining gates

PR #92 remains open and unchanged on the remote until this scoped commit is pushed. After a normal fast-forward, require new-head `local-ci/verify`, independent review and exact remote readback. The separate B18 accepted provider receipt is still absent; the full project test gate is incomplete. No merge/deploy or production DB/service change is authorized (`PROJECT.yaml` keeps production deployment disabled). No independent reviewer was found/assigned in the PR review list; do not self-approve.
