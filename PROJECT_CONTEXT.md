# ProjectFlow Workspace Manager context

## 2026-11-28 — AUTONOMY_FIRST v1 prompt propagation (#104 follow-up to #105)

- This isolated follow-up is based on docs-only PR #105 head
  `3a363bc7ceb6af95fd6a843719df0ebf8b47039a`; it does not modify ProjectFlow
  `main` or the PR #105 branch. The canonical ProjectFlow checkout has unrelated
  dirty `PROJECT.yaml` and downloaded package files; leave them untouched.
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
