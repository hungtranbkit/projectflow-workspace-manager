# ProjectFlow Workspace Manager context

## 2026-11-28 — PR #106 review corrections (published; CI pending)

- Reconciled independent review comment 7240692767. The legacy structured
  brief renderer previously omitted managed repository/workspace rules, and
  prompt create/edit/effective-prompt paths stripped nonblank owner text.
- app/main.py now appends repository identity, branch/worktree, sandbox,
  workspace isolation and role/instructions, plus the repository AGENTS.md
  excerpt and instruction to read AGENTS.md/PROJECT.yaml to the legacy path
  before the trusted AUTONOMY_FIRST adapter. Resume uses the same renderer.
- app/main.py create/edit persistence and
  app/services/task_decision_service.py effective prompt now preserve raw
  nonblank prompt content; whitespace-only values retain blank/title-fallback
  behavior. Tests exercise create/edit/render/delivery and legacy launch/resume
  with a strict repo rule.
- Verification after these source changes: focused prompt UX, prompt delivery,
  and exited-session recovery suite exited 0 (35 tests; pytest double-quiet
  suppresses the count summary); PROJECT.yaml preflight PASS; git diff --check
  exit 0. No live dispatch or deployment.
- Fix commit b2feff0efe010abb0454174c610a30653132145b was made and pushed on
  the existing PR #106 branch at prior head bb3955b916d92960991f0b23dc0fbbce0efb7aa9,
  stacked on PR #105. Current exact head is
  b523fcde641f69b81c6bcfda834e9ec99510e803. PR #106 still has no GitHub
  workflow/status result despite the workflow's unfiltered pull_request
  trigger; the cause is unknown. Local tests do not substitute for required
  PR checks. No formal review submission is visible after the head update;
  request independent review and normal exact-head CI before merge.

## 2026-11-28 — AUTONOMY_FIRST v1 prompt propagation (#104 follow-up to #105)

- This isolated follow-up is based on docs-only PR #105 head
  `3a363bc7ceb6af95fd6a843719df0ebf8b47039a`; it does not modify ProjectFlow
  `main` or the PR #105 branch. The canonical ProjectFlow checkout has unrelated
  dirty `PROJECT.yaml` and downloaded package files; leave them untouched.
- Published as ProjectFlow PR #106 at implementation commit
  `13d586b1674fff5562e644771e8503ffa13f80ea`, based on PR #105's branch.
  It consumes Terminal MCP policy PR #144 (source commit
  `2b6b977d568e02f8c72aece9835d02d06a3755b4`); merge order must keep the
  canonical source ahead of or coordinated with this adapter.
- Current supervised task ID: `dt_80d21c9312414e22`. Prior
  `dt_6bf695d228eb4c6b` remains CANCELLED/PERSISTED with zero dispatches and
  SESSION_DELETED; no old work was relaunched.
- Terminal MCP owns the canonical shared policy in
  `terminal_mcp/orchestration_policy.py`. ProjectFlow's
  `app/services/autonomy_policy.py` is a compact, version-pinned trusted prompt
  adapter. `render_agent_prompt()` adds it after user task content and managed
  repo rules for both implementation prompts and legacy structured briefs;
  resume/delivery use the same renderer. User prompt text stays verbatim.
- The policy is instructional only: it does not add dispatch/recovery authority,
  alter TaskDecisionService, statuses, DB schema, review/QA/CI gates, merge rules,
  or production release authority. Terminal MCP receipts remain the source for
  dispatch/execution distinctions; a canceled persisted task with zero dispatches
  is not revived or duplicated.
- Prior task receipt `dt_6bf695d228eb4c6b` was verified `CANCELLED`, `PERSISTED`,
  `dispatches=0`, `SESSION_DELETED`; it is context for the regression rule only,
  not active work or a dispatch request.
- Focused verification command
  `/home/dell/workspace/projectflow-workspace-manager/.venv/bin/python -m pytest -q
  tests/test_prompt_ux.py tests/test_prompt_delivery.py tests/test_control_plane.py
  tests/test_exited_no_report_recovery.py tests/test_autonomous_execution.py
  tests/test_spec_supervisor_gate.py` completed **92 passed, exit 0** (counted from
  pytest's 72 + 20 progress dots; pyproject's double-quiet setting omits the
  normal summary). Log `/tmp/pf-autonomy-first-focused-20261128.log`, SHA-256
  `07779fe6f36df09e3bc5d892e995ab5c663426ad9071391ed4de68b7553f3a20`. The
  `PROJECT.yaml` preflight script and `git diff --check` both exit 0. Verification
  is recorded in issue #104 and PR #105/follow-up PR. No
  deployment, service restart, live dispatch, or production change was performed.
  Independent review and CI remain required before merge.

## Known limitations / next action

- The ProjectFlow adapter is a compact source-pinned copy because this repository
  cannot import Terminal MCP code at runtime. Keep it aligned with canonical
  AUTONOMY_FIRST v1 and review both source hashes when either changes.
- No automatic approval, session recreation, task requeue, or safe alternate
  executor implementation was added. Those operations remain governed by the
  existing Terminal MCP receipt, authorization, binding, and idempotency APIs.
- Run the documented targeted tests, publish both isolated branches, then obtain
  independent review and passing required CI. Do not merge/deploy as part of this
  task.
