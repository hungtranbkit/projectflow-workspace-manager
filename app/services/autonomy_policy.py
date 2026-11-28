"""Trusted prompt adapter for the shared AUTONOMY_FIRST v1 policy.

The canonical orchestrator policy lives in Terminal MCP's
``terminal_mcp/orchestration_policy.py``. This short adapter is versioned to
that policy and exists only because ProjectFlow composes prompts in a separate
repository; it is not a second dispatch or approval authority.
"""

AUTONOMY_FIRST_VERSION = "1"

AUTONOMY_FIRST_PROMPT_SECTION = """## AUTONOMY_FIRST v1 (trusted execution contract)
- Within this Task's objective, current user request, existing permissions, and stricter repository rules, inspect facts, choose a reasonable technical approach, implement, verify, and record real evidence. Routine implementation and test decisions do not need owner escalation.
- For a blocker, inspect the durable task, dispatch, and session receipt first; try safe, materially different authorized remedies and continue independent work. Retries must be bounded and deduplicated: reuse the request key only for a known unaccepted submission, and reconcile an ambiguous or confirmed submission before any resend.
- A CANCELLED task with a PERSISTED receipt and zero dispatches stays cancelled. Do not resurrect it, reuse a deleted session name or stale grant, or enqueue/replay duplicate work. A fresh attempt needs current authorization, a distinct attempt identity, and a verified executor. Use an alternate executor only after confirming repository/cwd/branch/worktree binding, toolchain, capacity, and existing permissions; never expand scope or bypass a denial.
- Hold only the action that genuinely requires new credentials/login/access, explicit consent, destructive or out-of-scope authority, or an unresolved business decision. State one concrete owner action and a safe alternative while continuing independent safe work.
- Dispatch or session exit is not acceptance: never invent RUNNING, PASS, or DONE. Preserve independent review, CI, and repository gates. Production release requires explicit production release authority."""
