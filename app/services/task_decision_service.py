from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

from app.services.failure_classifier import fingerprint as fingerprint_of, parse_failures, parse_summary
from app.services.project_contract import ContractError, load_contract
from app.services.sandbox_contract import SandboxContractError, load_sandbox_contract

# Task Lifecycle & Gate Model Refactor -- the single authoritative source
# for Task status/stage/next-action/gate-eligibility. No route or template
# may compute any of these independently; every one of them reads from
# TaskDecisionService.evaluate().
#
# tasks.status is PERSISTED as only one of these three:
STATUSES = ("BACKLOG", "ACTIVE", "CANCELLED")
# ...the full user-facing status vocabulary also includes BLOCKED,
# READY_FOR_MAIN and DONE, but those are NEVER written to the column --
# they are computed live from real child evidence on every read, the same
# fix already applied twice before in this codebase (task_integrations.
# ready_for_main, and the V5 Kanban column) to stop a route silently
# leaving a stale flag behind.
DISPLAY_STATUSES = ("BACKLOG", "ACTIVE", "BLOCKED", "READY_FOR_MAIN", "DONE", "CANCELLED")
STAGES = ("PLANNING", "DEVELOPMENT", "REVIEW", "QA", "INTEGRATION", "MERGING", "COMPLETE")

RISK_PROFILES = ("LOW", "NORMAL", "HIGH")
# Workflow-decision-UX policy change: Runtime Verification ("QA" internally
# -- qa_runs/start-qa/submit-qa keep their existing names for backward
# compatibility; only the user-facing label changed, see user_state_view.py)
# is now required for NORMAL risk too, not only HIGH -- a NORMAL Task no
# longer jumps straight from Review PASS to Integration with nobody having
# actually run the app. LOW is the only profile that still skips straight
# to Integration/Ready for Main on Review PASS alone. This is the single
# place that decision changes; RISK_GATES is imported by app/main.py rather
# than re-declared there (a second, drifted copy of this table was a real
# bug fixed by this same change).
RISK_GATES = {"LOW": ("REVIEW",), "NORMAL": ("REVIEW", "QA", "INTEGRATION"), "HIGH": ("REVIEW", "QA", "INTEGRATION")}

NEXT_ACTIONS = (
    "REVIEW_BACKLOG", "SELECT_FOR_DEVELOPMENT", "CREATE_BUILDER_WORKSPACE",
    "START_BUILDER", "VIEW_BUILDER", "REVIEW_BUILDER_RESULT", "RUN_BUILDER_TEST",
    "SUBMIT_FOR_REVIEW", "START_REVIEW", "RETURN_TO_BUILDER",
    "CREATE_SANDBOX", "SANDBOX_PROVISIONING", "START_QA", "CREATE_INTEGRATION", "OPEN_INTEGRATOR", "RUN_INTEGRATION_TEST", "RESOLVE_CONFLICT",
    "REVIEW_BASELINE_FAILURE", "FIX_INTEGRATION_FAILURE", "PUSH_INTEGRATION", "WAIT_FOR_CI", "CONFIRM_INTEGRATION_READY",
    "REBUILD_SANDBOX", "PREPARE_PR", "WAIT_FOR_MERGES", "CLOSE_TASK", "NONE",
)

# Sandbox statuses that mean "still coming up", never a second button
# alongside Create Sandbox (section 6/13 of the workflow-decision-UX spec:
# a successful action must advance the UI, never leave a duplicate primary
# action behind).
SANDBOX_PROVISIONING_STATUSES = ("CREATED", "PROVISIONING", "STARTING", "RESETTING")
SANDBOX_READY_STATUSES = ("RUNNING", "CLEANUP_ELIGIBLE")

# Human-friendly translation for the raw merge-blocker codes
# (MERGE_BLOCKER_CODES below) -- section 10 of the workflow-decision-UX
# spec. Never shown instead of the code (Advanced still has the raw code),
# always shown instead of it in the normal Workflow Summary card.
BLOCKER_MESSAGES = {
    "NOT_READY": ("This Task is not Ready for Main yet.", "Finish the current step above."),
    "CI_FAIL": ("GitHub CI failed on the pushed branch.", "Open the PR, fix the failure, and push again."),
    "CI_PENDING": ("GitHub CI is still running.", "No action needed -- ProjectFlow will refresh this status."),
    "SOURCE_STALE": ("Source changed after verification.", "Run verification again on the new commit."),
    "REVIEW_STALE": ("Source changed after review.", "Review the latest commit again."),
    "QA_STALE": ("Source or Brief changed after Runtime Verification.", "Run Runtime Verification again."),
    "INTEGRATION_STALE": ("Integration source changed.", "Run integration tests again."),
    "NO_SANDBOX": ("A runtime environment is required before verification.", "Create a Sandbox."),
    "BASELINE_WAIVER_REQUIRED": ("An integration test failure needs a decision.", "Review the baseline failure and waive or fix it."),
    "PR_CLOSED": ("The Pull Request was closed without merging.", "Open a new Pull Request."),
    "TARGET_BRANCH_CHANGED": ("The target branch moved since this PR was opened.", "Merge latest changes into the branch."),
    "UNKNOWN_MERGEABILITY": ("GitHub has not confirmed this PR can be merged yet.", "Refresh in a moment."),
    "CONFLICT": ("The integration branch conflicts with the latest main branch.", "Resolve the conflict in the Integrator."),
    "NO_PR": ("No Pull Request has been opened yet.", "Create a Pull Request."),
}


def humanize_blocker(code: str) -> dict:
    """Never show a raw blocker code as the primary explanation (section
    10) -- falls back to a title-cased version of the code itself for
    anything not in the table above, so a future code never renders as a
    literal SCREAMING_SNAKE_CASE string with no explanation at all."""
    msg, remediation = BLOCKER_MESSAGES.get(code, (code.replace("_", " ").capitalize() + ".", None))
    return {"code": code, "message": msg, "remediation": remediation}


# The normalized, evidence-based checklist (section 2/19-21) -- distinct
# from `current_step`/wizard_steps (a UI navigation concept) and from
# `stage` (the persisted-adjacent lifecycle stage): this is only ever
# ✓ done / ● current / ○ future / ! blocked / — skipped, one row per real
# piece of evidence, never a raw implementation state name.
CHECKLIST_STEPS = ("TASK", "BUILDER", "AUTOMATED_TESTS", "REVIEW", "RUNTIME_VERIFICATION", "INTEGRATION", "MERGE")
CHECKLIST_LABELS = {
    "TASK": "Task configured", "BUILDER": "Builder completed", "AUTOMATED_TESTS": "Automated tests PASS",
    "REVIEW": "Review PASS", "RUNTIME_VERIFICATION": "Runtime verification", "INTEGRATION": "Integration",
    "MERGE": "Merge to main",
}

# Section 4: plain-language "what's still missing" per next_action code,
# shown only when there is no real blocker (a real blocker always wins --
# see _missing_requirements). Deliberately lists the WHOLE remaining
# requirement set for the current step, not only whichever one is stopping
# it right this second (section 12's own worked example: Create Sandbox
# still shows "Manual verification has not been completed" alongside it).
MISSING_REQUIREMENTS = {
    "CREATE_BUILDER_WORKSPACE": ["A Builder Workspace has not been created yet"],
    "START_BUILDER": ["The agent has not been started yet"],
    "REVIEW_BUILDER_RESULT": ["The agent session ended without a completion report"],
    "SUBMIT_FOR_REVIEW": ["The Builder has not submitted for review yet"],
    "START_REVIEW": ["Review has not been completed"],
    "CREATE_SANDBOX": ["A Sandbox has not been created", "Manual verification has not been completed"],
    "SANDBOX_PROVISIONING": ["Manual verification has not been completed"],
    "START_QA": ["Manual verification has not been completed"],
    "RUN_INTEGRATION_TEST": ["Integration tests have not passed yet"],
    "PUSH_INTEGRATION": ["The integration branch has not been pushed yet"],
    "PREPARE_PR": ["No Pull Request has been opened yet"],
}

# Session states that count as "the agent is actually running" for
# next_action purposes -- matches agent_sessions.status values a live PTY
# can be in (see AgentSessionManager).
LIVE_SESSION_STATUSES = ("STARTING", "RUNNING", "WAITING_FOR_INPUT")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def effective_task_prompt(t) -> str:
    """Task Title fallback (Builder execution UX): the Implementation
    Prompt is the task intent when non-empty; otherwise the Task title
    itself is sufficient intent -- Start Builder is never blocked on an
    empty Implementation Prompt, since a title is mandatory at Task
    creation and therefore always resolvable."""
    p = (t.get("implementation_prompt") or "").strip()
    return p if p else (t.get("title") or "").strip()


def prompt_source(t) -> str:
    """Which of the two the effective prompt actually came from -- shown
    in the UI so Title-fallback is never silently indistinguishable from
    a deliberately-written Implementation Prompt."""
    return "IMPLEMENTATION_PROMPT" if (t.get("implementation_prompt") or "").strip() else "TITLE"


# Every next_action target is a GET page/anchor for the human to act on,
# except these three, which are the action itself (a POST endpoint a
# button submits to directly) -- kept as one explicit set so templates
# never have to guess which verb an action code needs.
POST_ACTIONS = {"SELECT_FOR_DEVELOPMENT", "CREATE_INTEGRATION", "CLOSE_TASK", "CREATE_SANDBOX"}


def _action(action, label, reason, target=None):
    return {"action": action, "label": label, "reason": reason, "target": target,
            "method": "POST" if action in POST_ACTIONS else "GET"}


class TaskDecisionService:
    """Given a Task and its children (Builder Workspaces, ReviewRuns,
    QARuns, Integration, MergeRecords), decide status/stage/next-action/
    blocking-reasons/test-readiness/integration-eligibility/ready-for-main
    in one place. Reads real tables directly (never a second cached
    snapshot); a git call is only ever made to compare a pinned commit
    against the source branch's current HEAD (staleness), same discipline
    `sandboxes.is_stale()` already established."""

    def __init__(self, db, git):
        self.db = db
        self.git = git

    # ---- policy -------------------------------------------------------
    def risk_profile_for(self, task_row) -> str:
        rp = (task_row.get("risk_profile") or "NORMAL").upper()
        return rp if rp in RISK_PROFILES else "NORMAL"

    def requires_qa(self, risk: str) -> bool:
        return "QA" in RISK_GATES.get(risk, RISK_GATES["NORMAL"])

    def requires_integration(self, risk: str) -> bool:
        return "INTEGRATION" in RISK_GATES.get(risk, RISK_GATES["NORMAL"])

    # ---- child queries --------------------------------------------------
    def workspaces(self, task_id):
        return self.db.all(
            "SELECT w.*,r.repo_name,r.repo_path FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id "
            "WHERE w.task_id=? ORDER BY w.created_at", (task_id,))

    def latest_report(self, workspace_id):
        return self.db.one("SELECT * FROM verification_reports WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (workspace_id,))

    def latest_review(self, workspace_id):
        return self.db.one("SELECT * FROM review_runs WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (workspace_id,))

    def review_history(self, workspace_id):
        return self.db.all("SELECT * FROM review_runs WHERE workspace_id=? ORDER BY id DESC", (workspace_id,))

    def latest_qa(self, task_id):
        return self.db.one("SELECT * FROM qa_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))

    def qa_history(self, task_id):
        return self.db.all("SELECT * FROM qa_runs WHERE task_id=? ORDER BY id DESC", (task_id,))

    def task_integration(self, task_id):
        return self.db.one("SELECT * FROM task_integrations WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))

    def integration_repos(self, task_integration_id):
        return self.db.all(
            "SELECT i.*,r.repo_name FROM integration_workspaces i JOIN repositories r ON r.id=i.repository_id "
            "WHERE i.task_integration_id=? ORDER BY i.id", (task_integration_id,))

    def merge_records(self, task_id, workspaces=None):
        """Auto-upserts one required MergeRecord per distinct repository
        a Task's Builder Workspaces actually touch -- the set of required
        repos is derived from real Builder Workspaces, never hand-typed."""
        ws = self.workspaces(task_id) if workspaces is None else workspaces
        for repo_id in {w["repository_id"] for w in ws}:
            self.db.execute(
                "INSERT OR IGNORE INTO merge_records(task_id,repository_id,required,merge_status) VALUES(?,?,1,'NOT_STARTED')",
                (task_id, repo_id))
        return self.db.all("SELECT m.*,r.repo_name FROM merge_records m JOIN repositories r ON r.id=m.repository_id WHERE m.task_id=? ORDER BY m.id", (task_id,))

    def brief_complete(self, t) -> bool:
        """A Task's intent is always resolvable now: Implementation Prompt
        if written, else the Task title (mandatory at creation) -- kept as
        a method (rather than inlined `True`) only because the Overview
        gate checklist and older callers still ask this question; it no
        longer gates CREATE_BUILDER_WORKSPACE (see _next_action)."""
        return bool(effective_task_prompt(t))

    def latest_session(self, workspace_id):
        return self.db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (workspace_id,))

    def current_commit(self, worktree_path):
        try:
            return self.git.head(worktree_path)
        except Exception:
            return None

    # ---- per-builder derived view --------------------------------------
    def builder_view(self, w, brief_version):
        """One Builder Workspace's exact-commit-pinned state. `ready`
        means Submitted for Review (git clean + report existed at submit
        time); `review_status` is recomputed STALE the moment the pinned
        `reviewed_commit` no longer matches the branch's live HEAD, or the
        review was done against an older Brief version -- never trusted
        as still valid just because a PASS row exists."""
        report = self.latest_report(w["id"])
        review = self.latest_review(w["id"])
        head = self.current_commit(w["worktree_path"])
        ready = w["status"] == "READY"
        review_status = "NONE"
        if review:
            stale = (review["reviewed_commit"] and head and review["reviewed_commit"] != head) or \
                    (review.get("brief_version") not in (None, brief_version))
            review_status = "STALE" if stale and review["status"] in ("PASS", "RUNNING", "PENDING") else review["status"]
        session = self.latest_session(w["id"])
        # NOT_STARTED is never a stored agent_sessions.status -- it is the
        # honest absence of any session row for this workspace (Workspace
        # READY must never be misread as Agent RUNNING).
        agent_status = session["status"] if session else "NOT_STARTED"
        return {
            "id": w["id"], "agent": w["agent"], "role": w["role"], "repo_name": w["repo_name"],
            "repository_id": w["repository_id"], "worktree_path": w["worktree_path"], "branch": w["branch"],
            "base_commit": w["base_commit"], "task_id": w["task_id"],
            "status": w["status"], "head": head, "ready": ready, "report": report,
            "review": review, "review_status": review_status,
            "fix_required": review_status == "FIX_REQUIRED",
            "session": session, "agent_status": agent_status,
            "builder_instructions": w.get("builder_instructions") or "",
            "sandbox_state": self.builder_sandbox_state(w),
        }

    # ---- Runtime Verification sandbox gating (section 12-15) ------------
    def builder_sandbox_state(self, w):
        """Whether THIS Builder Workspace's own sandbox -- reused as the
        Runtime Verification environment for Tasks with one Builder, the
        common case -- is required, absent, provisioning, or ready. Mirrors
        the pre-existing `workspace_readiness()` rule in app/main.py
        exactly (a repo that declares a sandbox: contract requires one
        unless the workspace explicitly opted out with profile NONE) so
        Task Detail and Workspace Detail can never disagree about it
        (section 18) -- this is now the one place that rule lives; nothing
        else recomputes it independently."""
        try:
            configured = load_sandbox_contract(Path(w["repo_path"])) is not None
        except SandboxContractError:
            configured = True  # a misconfigured contract is not "absent"
        required = configured and w.get("sandbox_profile") != "NONE"
        sb = self.db.one("SELECT * FROM sandboxes WHERE owner_type='AGENT_WORKSPACE' AND owner_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
        if not required:
            return {"required": False, "sandbox": sb, "phase": "NOT_REQUIRED"}
        if not sb:
            return {"required": True, "sandbox": None, "phase": "NEEDS_SANDBOX"}
        if sb["status"] in SANDBOX_READY_STATUSES:
            return {"required": True, "sandbox": sb, "phase": "READY"}
        if sb["status"] == "FAILED":
            return {"required": True, "sandbox": sb, "phase": "FAILED"}
        return {"required": True, "sandbox": sb, "phase": "PROVISIONING"}

    def qa_sandbox_state(self, builders):
        """The Task-wide version of the same question, for the Runtime
        Verification step: the primary (first) Builder's sandbox state
        stands in for the whole Task in the common single-Builder case;
        a multi-Builder Task is satisfied once at least one participating
        Builder has a ready sandbox (Runtime Verification only needs one
        running environment to open and check, not one per repo)."""
        if not builders:
            return {"required": False, "sandbox": None, "phase": "NOT_REQUIRED"}
        states = [b["sandbox_state"] for b in builders]
        if not any(s["required"] for s in states):
            return {"required": False, "sandbox": None, "phase": "NOT_REQUIRED"}
        if any(s["phase"] == "READY" for s in states):
            return next(s for s in states if s["phase"] == "READY")
        if any(s["phase"] == "PROVISIONING" for s in states):
            return next(s for s in states if s["phase"] == "PROVISIONING")
        if any(s["phase"] == "FAILED" for s in states):
            return next(s for s in states if s["phase"] == "FAILED")
        return next(s for s in states if s["required"])

    # ---- the decision ---------------------------------------------------
    def evaluate(self, task_id: int) -> dict:
        t = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not t:
            raise ValueError(f"Task {task_id} not found")
        risk = self.risk_profile_for(t)
        workspaces = self.workspaces(task_id)
        builders = [self.builder_view(w, t["brief_version"]) for w in workspaces]
        ti = self.task_integration(task_id)
        ti_repos = self.integration_repos(ti["id"]) if ti else []
        for r in ti_repos:
            r["gate_status"] = self.integration_gate_status(r, task_id)
        qa = self.latest_qa(task_id)
        merges = self.merge_records(task_id, workspaces)
        required_merges = [m for m in merges if m["required"]]

        if t["status"] == "CANCELLED":
            return self._result(t, "CANCELLED", "COMPLETE", _action("NONE", None, "Task cancelled."), [], "NO", False, False,
                                 builders, qa, ti, ti_repos, merges, risk, current_step=None)
        if t["status"] == "BACKLOG":
            na = _action("SELECT_FOR_DEVELOPMENT", "Select for Development", "Task is in Backlog.", f"/api/tasks/{task_id}/select")
            return self._result(t, "BACKLOG", "PLANNING", na, [], "NO", False, False, builders, qa, ti, ti_repos, merges, risk, current_step="TASK")

        # ACTIVE from here -- everything else is computed.
        blocking = []
        for b in builders:
            if b["fix_required"]:
                blocking.append(f"{b['agent']} · {b['role'] or b['repo_name']}: reviewer requested changes")
            if b["review_status"] == "BLOCKED":
                blocking.append(f"{b['agent']} · {b['role'] or b['repo_name']}: review BLOCKED")
        if qa and qa["status"] in ("FAIL", "BLOCKED"):
            blocking.append(f"QA {qa['status']}")
        for m in required_merges:
            if m["merge_status"] == "FAILED": blocking.append(f"{m['repo_name']} merge FAILED")
        if ti and ti["status"] == "CONFLICT": blocking.append("Integration has unresolved conflicts")

        all_builders_ready = bool(builders) and all(b["ready"] for b in builders)
        all_reviews_current_pass = bool(builders) and all(b["review_status"] == "PASS" for b in builders)
        qa_ok = (not self.requires_qa(risk)) or (qa is not None and qa["status"] == "PASS" and self._qa_current(qa, t))
        integration_ok = (not self.requires_integration(risk)) or self._integration_ok(ti, ti_repos)
        gates_ok = all_reviews_current_pass and qa_ok and integration_ok and not blocking
        all_merged = bool(required_merges) and all(m["merge_status"] == "MERGED" for m in required_merges)

        stage = self._stage(builders, all_builders_ready, all_reviews_current_pass, qa, qa_ok, risk, ti, integration_ok, gates_ok, all_merged)
        ready_for_main = gates_ok and not all_merged
        if all_merged and gates_ok:
            status = "DONE"
        elif blocking:
            status = "BLOCKED"
        elif ready_for_main:
            status = "READY_FOR_MAIN"
        else:
            status = "ACTIVE"

        test_readiness = self._test_readiness(builders, blocking)
        next_action = self._next_action(t, builders, qa, ti, ti_repos, required_merges, risk, ready_for_main, blocking, status)
        integration_eligibility = {
            "required": self.requires_integration(risk),
            "eligible": all_builders_ready and all_reviews_current_pass and not blocking,
            "exists": bool(ti),
        }
        current_step = self._current_step(status, builders, qa, ti, ti_repos, risk, t, all_merged)
        return self._result(t, status, stage, next_action, blocking, test_readiness, integration_eligibility["eligible"],
                             ready_for_main, builders, qa, ti, ti_repos, merges, risk, integration_eligibility, current_step)

    def _qa_current(self, qa, t):
        return qa.get("brief_version") in (None, t["brief_version"])

    def _current_step(self, status, builders, qa, ti, ti_repos, risk, t, all_merged):
        """The one deterministic 'current step' of the wizard
        (TASK/SETUP/AGENT_RUNNING/REVIEW/TEST_QA/INTEGRATION/
        READY_FOR_MAIN/DONE) -- computed from the exact same signals
        evaluate() already derived, never a second, template-side
        lifecycle calculation. Deliberately separate from `status`
        (BACKLOG/ACTIVE/BLOCKED/READY_FOR_MAIN/DONE/CANCELLED stays the
        simplified, persisted-adjacent status; the step is a UI
        concept only, section 23)."""
        if status == "CANCELLED":
            return None  # not part of the 8-step wizard
        if status == "BACKLOG":
            return "TASK"
        if not builders:
            return "SETUP"
        # A reviewer's FIX_REQUIRED sends the whole Task back to the
        # Builder conceptually (section 11) even though the Builder
        # Workspace's own `ready` flag never resets -- the wizard step
        # must reflect that, not just the raw submitted/not-submitted bit.
        if any(b["fix_required"] for b in builders):
            return "AGENT_RUNNING"
        if not all(b["ready"] for b in builders):
            if any(b["agent_status"] in LIVE_SESSION_STATUSES for b in builders):
                return "AGENT_RUNNING"
            if any(b["agent_status"] in ("EXITED", "FAILED") for b in builders):
                return "AGENT_RUNNING"  # exited without a completion report -- still this step, showing Resume/Open Live/Mark Blocked
            return "SETUP"
        if not all(b["review_status"] == "PASS" for b in builders):
            return "REVIEW"
        if self.requires_qa(risk) and not (qa and qa["status"] == "PASS" and self._qa_current(qa, t)):
            return "TEST_QA"
        if self.requires_integration(risk) and not self._integration_ok(ti, ti_repos):
            return "INTEGRATION"
        if all_merged:
            return "DONE"
        return "READY_FOR_MAIN"

    # ---- baseline-waiver failure classification (section 11) -----------
    def integration_gate_status(self, ti_repo, task_id):
        """Classifies one per-repo Integration Workspace's required-gate
        result at its current HEAD: PASS / FAIL / NOT_RUN /
        PASS_WITH_APPROVED_BASELINE_WAIVER, plus each individual failure
        classified as NEW_FAILURE / BASELINE_FAILURE / WAIVED / UNKNOWN.
        A failure only ever becomes BASELINE_FAILURE against a REAL,
        stored BaselineFailureEvidence row whose fingerprint matches this
        exact failure -- never inferred from 'this looks unrelated'
        (section 11-13). A waiver only ever applies to the exact
        (gate, test_identifier, fingerprint) it was approved for -- if
        the failure's fingerprint has since changed, the old waiver
        simply does not match anymore (section 17: 'if failure changes,
        waiver invalid')."""
        iid = ti_repo["id"]
        try:
            head = self.git.head(ti_repo["worktree_path"])
        except Exception:
            return {"tests_pass": False, "tests_status": "UNKNOWN", "failures": [], "tests_required": 0, "head": None}
        try:
            required = len(load_contract(Path(ti_repo["worktree_path"])))
        except ContractError:
            required = 0
        rows = self.db.all(
            "SELECT * FROM test_runs WHERE workspace_type='integration' AND workspace_id=? AND tested_commit=? ORDER BY id DESC",
            (iid, head))
        latest = {}
        for r in rows:
            latest.setdefault(r["stage"], r)  # first seen per stage, DESC id -> most recent
        if required == 0 or len(latest) < required:
            return {"tests_pass": False, "tests_status": "NOT_RUN", "failures": [], "tests_required": required,
                     "tests_passed": sum(1 for v in latest.values() if v["status"] == "PASS"), "head": head, "summary": {}}
        failures = []
        summary = {"passed": 0, "failed": 0, "skipped": 0}
        for stage, row in latest.items():
            if row["stage"] == "test":
                for k, v in parse_summary(row["stdout_tail"] or "").items():
                    summary[k] = max(summary[k], v)
            if row["status"] == "PASS":
                continue
            parsed = parse_failures(row["stdout_tail"] or "") or [{"test_identifier": stage, "reason": row["status"]}]
            for f in parsed:
                fp = fingerprint_of(f["test_identifier"], f["reason"])
                waiver = self.db.one(
                    "SELECT * FROM gate_waivers WHERE task_id=? AND integration_id=? AND gate=? AND test_identifier=? AND failure_fingerprint=? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
                    (task_id, iid, stage, f["test_identifier"], fp))
                evidence = self.db.one(
                    "SELECT * FROM baseline_failure_evidence WHERE repository_id=? AND base_commit=? AND gate=? AND test_identifier=? ORDER BY id DESC LIMIT 1",
                    (ti_repo["repository_id"], ti_repo["base_commit"], stage, f["test_identifier"]))
                if waiver:
                    cls = "WAIVED"
                elif evidence and evidence["failure_fingerprint"] == fp:
                    cls = "BASELINE_FAILURE"
                elif evidence:
                    cls = "NEW_FAILURE"  # evidence exists for this test id but the failure itself changed
                else:
                    cls = "UNKNOWN"
                failures.append({
                    "stage": stage, "test_identifier": f["test_identifier"], "reason": f["reason"],
                    "fingerprint": fp, "classification": cls,
                    "evidence_id": evidence["id"] if evidence else None,
                    "waiver_id": waiver["id"] if waiver else None,
                })
        unresolved = [f for f in failures if f["classification"] != "WAIVED"]
        if not failures:
            tests_status = "PASS"
        elif not unresolved:
            tests_status = "PASS_WITH_APPROVED_BASELINE_WAIVER"
        else:
            tests_status = "FAIL"
        return {
            "tests_pass": tests_status in ("PASS", "PASS_WITH_APPROVED_BASELINE_WAIVER"),
            "tests_status": tests_status, "failures": failures, "tests_required": required,
            "tests_passed": sum(1 for v in latest.values() if v["status"] == "PASS"),
            "head": head, "summary": summary,
        }

    # ---- real merge gate (section 4/9) ----------------------------------
    MERGE_BLOCKER_CODES = (
        "NOT_READY", "CI_FAIL", "CI_PENDING", "SOURCE_STALE", "REVIEW_STALE", "QA_STALE",
        "INTEGRATION_STALE", "BASELINE_WAIVER_REQUIRED", "PR_CLOSED", "TARGET_BRANCH_CHANGED",
        "UNKNOWN_MERGEABILITY", "CONFLICT", "NO_PR",
    )

    def effective_source_for_repo(self, d, repository_id):
        """The exact branch + commit that reached READY_FOR_MAIN for one
        repository -- its Task Integration branch when Integration is
        required (NORMAL/HIGH), else its own Builder Workspace branch
        (LOW risk skips Integration entirely, READY_FOR_MAIN comes
        straight from Review PASS). Never a guess -- the same
        branch/commit _integration_ok/integration_gate_status already
        treat as authoritative for that repo."""
        if self.requires_integration(d["risk_profile"]):
            repo_ti = next((r for r in d["integration_repos"] if r["repository_id"] == repository_id), None)
            if not repo_ti:
                return None, None
            gs = repo_ti.get("gate_status") or {}
            return repo_ti["branch"], gs.get("head")
        b = next((x for x in d["builders"] if x["repository_id"] == repository_id), None)
        return (b["branch"], b["head"]) if b else (None, None)

    # ---- one dominant primary action, per Integration (sections 17-20) --
    def integration_next_action(self, ti_repo, task_id, merge_record=None):
        """The ONE next real action for a single Integration Workspace --
        the exact same ladder _next_action uses inside the Task wizard,
        extracted so the Integration page itself can show one dominant
        primary action derived from this SAME authoritative decision,
        never a second, independently-drifting ordering (section 19).
        `target` is always None here -- callers (Task wizard vs.
        Integration page) fill in whatever anchor/URL makes sense for
        where they're rendering this from."""
        if ti_repo["status"] == "CONFLICT":
            return _action("RESOLVE_CONFLICT", "Resolve Conflict", "Integration has a merge conflict.", None)
        if ti_repo["status"] == "READY_FOR_MAIN":
            # Callers inside the Task wizard's own multi-repo ladder never
            # reach here with an already-ready repo (the blocker search
            # only ever picks a NOT-ready one) -- this branch exists for
            # the Integration page's own direct call, so a freshly
            # confirmed Ready for Main never re-offers itself as if still
            # pending (section 17: a completed action must not linger).
            return _action("NONE", "Ready for Main", "Already confirmed Ready for Main -- waiting on the Task's Merge step.", None)
        gs = ti_repo.get("gate_status") or {}
        failures = gs.get("failures") or []
        unresolved = [f for f in failures if f["classification"] != "WAIVED"]
        if unresolved and all(f["classification"] == "BASELINE_FAILURE" for f in unresolved):
            return _action("REVIEW_BASELINE_FAILURE", "Review Baseline Failure",
                            f"{len(unresolved)} failure(s) match verified baseline evidence -- waive or fix the baseline.", None)
        if unresolved:
            return _action("FIX_INTEGRATION_FAILURE", "Fix Integration Failure",
                            f"{len(unresolved)} new/unclassified failure(s) block Ready for Main.", None)
        if not gs or gs.get("tests_status") in (None, "NOT_RUN"):
            return _action("RUN_INTEGRATION_TEST", "Run Integration Tests", "Integration exists, tests not current/passing.", None)
        if ti_repo.get("push_status") != "PUSHED" or gs.get("head") != ti_repo.get("last_pushed_head"):
            return _action("PUSH_INTEGRATION", "Push Integration Branch",
                            "Tests pass on this HEAD, but it has not been pushed to GitHub yet.", None)
        if merge_record and merge_record.get("ci_status") == "PENDING":
            return _action("WAIT_FOR_CI", "Waiting for CI", "Pushed -- waiting for GitHub CI on the updated PR.", None)
        return _action("CONFIRM_INTEGRATION_READY", "Mark Ready for Main", "Tests pass and the branch is pushed -- confirm Ready for Main.", None)

    def merge_gate_status(self, d, repository_id, merge_record):
        """Every reason [Merge] must stay disabled for one repository,
        computed fresh from the Task's own live decision plus the
        MergeRecord's last-synced GitHub snapshot -- never a second,
        template-side calculation, and never trusting stale UI state
        (section 4/5/9: the real merge action re-fetches all of this
        again immediately before actually calling the GitHub merge API).
        Merge-reconciliation section 2/4: once a repo's MergeRecord is
        already MERGED, mergeability/CI/conflict/staleness are pre-merge
        eligibility concepts that no longer apply -- a merged repo never
        goes back to evaluating them, never shows a stale PR_OPEN/
        UNKNOWN_MERGEABILITY/SOURCE_STALE blocker, and is never eligible
        for a second real merge call."""
        if merge_record.get("merge_status") == "MERGED":
            branch, commit = self.effective_source_for_repo(d, repository_id)
            return {"eligible": False, "blockers": [], "source_branch": branch, "current_commit": commit, "merged": True}
        branch, commit = self.effective_source_for_repo(d, repository_id)
        blockers: list[str] = []
        if not d["ready_for_main"]:
            blockers.append("NOT_READY")
        b = next((x for x in d["builders"] if x["repository_id"] == repository_id), None)
        if b and b["review_status"] == "STALE":
            blockers.append("REVIEW_STALE")
        qa = d.get("qa")
        if self.requires_qa(d["risk_profile"]) and qa and not self._qa_current(qa, d["task"]):
            blockers.append("QA_STALE")
        if self.requires_integration(d["risk_profile"]):
            repo_ti = next((r for r in d["integration_repos"] if r["repository_id"] == repository_id), None)
            gs = (repo_ti or {}).get("gate_status") or {}
            if gs.get("tests_status") == "FAIL":
                unresolved = [f for f in (gs.get("failures") or []) if f["classification"] != "WAIVED"]
                if unresolved and all(f["classification"] == "BASELINE_FAILURE" for f in unresolved):
                    blockers.append("BASELINE_WAIVER_REQUIRED")
                elif unresolved:
                    blockers.append("INTEGRATION_STALE")
            elif repo_ti and repo_ti["status"] != "READY_FOR_MAIN":
                blockers.append("INTEGRATION_STALE")
        if not merge_record.get("pr_number"):
            blockers.append("NO_PR")
        else:
            if merge_record.get("pr_state") == "CLOSED":
                blockers.append("PR_CLOSED")
            if merge_record.get("ci_status") == "FAIL":
                blockers.append("CI_FAIL")
            elif merge_record.get("ci_status") == "PENDING":
                blockers.append("CI_PENDING")
            if merge_record.get("mergeability") == "CONFLICTING":
                blockers.append("CONFLICT")
            elif merge_record.get("mergeability") == "UNKNOWN":
                blockers.append("UNKNOWN_MERGEABILITY")
            if merge_record.get("merge_state_status") == "BEHIND":
                blockers.append("TARGET_BRANCH_CHANGED")
            verified = merge_record.get("verified_commit")
            if verified and ((commit and verified != commit) or (merge_record.get("head_sha") and merge_record["head_sha"] != verified)):
                blockers.append("SOURCE_STALE")
        return {"eligible": not blockers, "blockers": blockers, "source_branch": branch, "current_commit": commit}

    def _integration_ok(self, ti, ti_repos):
        """Healthy AND verified -- 'TESTING' means tests are only in
        flight, not proof of anything yet, so only an explicit
        READY_FOR_MAIN on every participating repo's Integration
        Workspace counts (the same gate /api/integrations/{iid}/
        ready-for-main itself enforces before setting it).

        State-consistency audit finding: a persisted status=='READY_FOR_
        MAIN' row does NOT by itself prove verified_commit still equals
        the branch's real current HEAD -- that reconciliation
        (`invalidate()`) was previously only ever triggered as a side
        effect of a human loading /integrations/{iid} directly. Any
        OTHER route reading this decision (task_detail, /decision API,
        merge routes) would keep seeing a stale READY_FOR_MAIN --
        'Integration healthy' checked ✓ -- even after new, never-tested
        commits landed on the source branch. gate_status['head'] is
        always computed fresh (real git read, every call), so comparing
        it against the persisted verified_commit here makes the
        decision layer itself self-healing: the invariant 'Integration
        READY_FOR_MAIN implies current HEAD == verified HEAD' now holds
        no matter which route asks, never only the one that happens to
        also run the DB-write side effect."""
        if not ti or ti["status"] == "CONFLICT": return False
        if not ti_repos: return False
        for r in ti_repos:
            if r["status"] != "READY_FOR_MAIN":
                return False
            gs = r.get("gate_status") or {}
            if r.get("verified_commit") and gs.get("head") and r["verified_commit"] != gs["head"]:
                return False  # HEAD moved since Ready for Main was confirmed; DB flag not yet reconciled
        return True

    # ---- public wrappers for gate-checklist rendering (section 38) -----
    # Same checks evaluate() uses internally, exposed so a template's gate
    # checklist reads the real gate instead of re-deriving its own copy.
    def qa_current(self, qa, t):
        return bool(qa) and self._qa_current(qa, t)

    def integration_healthy(self, ti, ti_repos):
        return self._integration_ok(ti, ti_repos)

    def _stage(self, builders, all_ready, all_reviews_pass, qa, qa_ok, risk, ti, integration_ok, gates_ok, all_merged):
        if not builders: return "PLANNING"
        if not all_ready: return "DEVELOPMENT"
        if not all_reviews_pass: return "REVIEW"
        if self.requires_qa(risk) and not qa_ok: return "QA"
        if self.requires_integration(risk) and not integration_ok: return "INTEGRATION"
        if all_merged: return "COMPLETE"
        return "MERGING"

    def _test_readiness(self, builders, blocking):
        if not builders or blocking: return "NO"
        ready_count = sum(1 for b in builders if b["ready"] and b["status"] != "STALE")
        if ready_count == 0: return "NO"
        if ready_count == len(builders): return "YES"
        return "PARTIAL"

    def _next_action(self, t, builders, qa, ti, ti_repos, required_merges, risk, ready_for_main, blocking, status):
        tid = t["id"]
        if not builders:
            # Task Title fallback: intent is always resolvable (title is
            # mandatory at creation), so there is nothing left to gate on
            # here -- go straight to creating the first Builder Workspace.
            return _action("CREATE_BUILDER_WORKSPACE", "Create Builder Workspace", "No Builder Workspace yet.", f"/tasks/{tid}#new-workspace")
        for b in builders:
            if b["fix_required"]:
                return _action("RETURN_TO_BUILDER", f"Fix required: {b['agent']}", (b["review"] or {}).get("findings") or "Reviewer requested changes.", f"/workspaces/{b['id']}")
            if not b["ready"]:
                if b["agent_status"] in LIVE_SESSION_STATUSES:
                    # The actual PTY-backed terminal, never the Workspace
                    # management page (session is guaranteed present here
                    # since agent_status can only be live when a session
                    # row exists -- see _builder_view above).
                    return _action("VIEW_BUILDER", f"View: {b['agent']}", "Agent is running.", f"/workspaces/{b['id']}/sessions/{b['session']['id']}")
                if b["agent_status"] in ("EXITED", "FAILED"):
                    return _action("REVIEW_BUILDER_RESULT", f"Review result: {b['agent']}", "Agent session ended without a completion report yet.", f"/workspaces/{b['id']}")
                return _action("START_BUILDER", f"Start {b['agent'].capitalize()}: {b['role'] or b['repo_name']}", "Builder Workspace ready, agent not started yet.", f"/workspaces/{b['id']}")
            if b["review_status"] in ("NONE",):
                return _action("SUBMIT_FOR_REVIEW", f"Start Review: {b['agent']}", "Builder ready, no review started.", f"/workspaces/{b['id']}")
            if b["review_status"] == "STALE":
                return _action("START_REVIEW", f"Re-review (source changed): {b['agent']}", "Source or Brief changed since last review.", f"/workspaces/{b['id']}")
            if b["review_status"] in ("PENDING", "RUNNING"):
                return _action("START_REVIEW", f"Complete Review: {b['agent']}", "Review in progress.", f"/workspaces/{b['id']}")
        if self.requires_qa(risk):
            needs_fresh_run = (not qa) or (qa["status"] == "PASS" and not self._qa_current(qa, t))
            if needs_fresh_run:
                # Runtime Verification needs a real running environment
                # before there is anything to open and check (section
                # 12-15) -- Create Sandbox / wait for it to finish
                # provisioning are real prerequisite steps of STARTING a
                # fresh run, not folded silently into "Start QA". An
                # in-flight or already-failed run (below) already has
                # whatever sandbox it had at start time -- no second gate.
                sbx = self.qa_sandbox_state(builders)
                if sbx["required"] and sbx["phase"] == "NEEDS_SANDBOX":
                    primary = next(b for b in builders if b["sandbox_state"]["phase"] == "NEEDS_SANDBOX")
                    return _action("CREATE_SANDBOX", "Create Sandbox", "Runtime verification needs a sandbox to open and check.", f"/api/tasks/{tid}/workspaces/{primary['id']}/create-sandbox")
                if sbx["required"] and sbx["phase"] == "PROVISIONING":
                    return _action("SANDBOX_PROVISIONING", None, "Sandbox is being created...", None)
                if sbx["required"] and sbx["phase"] == "FAILED":
                    sandbox_id = sbx["sandbox"]["id"] if sbx["sandbox"] else None
                    return _action("REBUILD_SANDBOX", "Sandbox failed -- Rebuild", "Sandbox creation failed.", f"/sandboxes/{sandbox_id}" if sandbox_id else f"/tasks/{tid}#qa")
                label = "Start Runtime Verification" if not qa else "Re-run Runtime Verification (Brief changed)"
                reason = "All reviews PASS -- runtime verification required." if not qa else "Brief version changed since the last PASS."
                return _action("START_QA", label, reason, f"/tasks/{tid}#qa")
            if qa["status"] in ("PENDING", "RUNNING"): return _action("START_QA", "Complete Runtime Verification", "Runtime verification in progress.", f"/tasks/{tid}#qa")
            if qa["status"] in ("FAIL", "BLOCKED"):
                # Section 8/14: the button does the next real thing --
                # straight to the Builder Workspace to resume, the same
                # target a Review FIX_REQUIRED already uses above, not
                # just an anchor back onto this same page.
                primary = builders[0] if builders else None
                target = f"/workspaces/{primary['id']}" if primary else f"/tasks/{tid}#qa"
                return _action("RETURN_TO_BUILDER", "Runtime verification failed", qa.get("notes") or "Runtime verification reported a failure.", target)
        if self.requires_integration(risk):
            if not ti: return _action("CREATE_INTEGRATION", "Create Integration", "All required gates PASS -- ready to integrate.", f"/api/tasks/{tid}/integrations")
            if ti["status"] == "CONFLICT": return _action("RESOLVE_CONFLICT", "Resolve Conflict", "Integration has a merge conflict.", f"/tasks/{tid}#integration")
            if not self._integration_ok(ti, ti_repos):
                # Section 19/20: the exact same per-repo ladder the
                # Integration page itself uses (integration_next_action)
                # -- one authoritative ordering, never two.
                blocker = next((r for r in ti_repos if r["status"] != "READY_FOR_MAIN"), None)
                if not blocker:
                    return _action("RUN_INTEGRATION_TEST", "Run Integration Tests", "Integration exists, tests not current/passing.", f"/tasks/{tid}#integration")
                mr = next((m for m in required_merges if m["repository_id"] == blocker["repository_id"]), None)
                action = dict(self.integration_next_action(blocker, tid, mr))
                if action["action"] != "NONE": action["target"] = f"/tasks/{tid}#integration"
                return action
        if status == "BLOCKED":
            return _action("NONE", "Blocked", blocking[0] if blocking else "Blocked.", None)
        if status == "DONE":
            return _action("CLOSE_TASK", "Close Task", "All required repos merged.", f"/api/tasks/{tid}/close")
        if ready_for_main:
            pending = [m for m in required_merges if m["merge_status"] != "MERGED"]
            not_started = [m for m in pending if m["merge_status"] == "NOT_STARTED"]
            if not_started: return _action("PREPARE_PR", f"Prepare PR: {not_started[0]['repo_name']}", "Ready for main -- push and open a Pull Request.", f"/tasks/{tid}#merges")
            return _action("WAIT_FOR_MERGES", "Waiting for merges", f"{len(pending)} repo(s) still not merged.", f"/tasks/{tid}#merges")
        return _action("NONE", None, "Waiting on the gate above.", None)

    # ---- Workflow Summary: checklist / missing requirements / previous
    # step (sections 2/4/11/19-21 of the workflow-decision-UX spec) -------
    def builder_tests_status(self, b):
        """Evidence-based, never inferred from work_status alone (section
        20): PASS only if every automated test stage run FOR THE BUILDER'S
        EXACT CURRENT HEAD passed -- a test run against an older commit
        proves nothing about the current one."""
        head = b.get("head")
        if not head:
            return "NOT_RUN"
        rows = self.db.all("SELECT * FROM test_runs WHERE workspace_type='agent' AND workspace_id=? AND tested_commit=? ORDER BY id DESC", (b["id"], head))
        if not rows:
            return "NOT_RUN"
        latest = {}
        for r in rows:
            latest.setdefault(r["stage"], r)
        if any(r["status"] == "FAIL" for r in latest.values()):
            return "FAIL"
        if all(r["status"] == "PASS" for r in latest.values()):
            return "PASS"
        return "NOT_RUN"

    def _checklist(self, status, builders, risk, current_step, all_builders_ready, all_reviews_current_pass, qa_ok, integration_ok, all_merged):
        if status == "CANCELLED":
            return []

        def item(key, state, note=None):
            return {"key": key, "label": CHECKLIST_LABELS[key], "state": state, "note": note}

        if status == "BACKLOG":
            return [item("TASK", "current")] + [item(k, "future") for k in CHECKLIST_STEPS[1:]]
        items = [item("TASK", "done")]
        items.append(item("BUILDER", "done" if all_builders_ready else ("current" if current_step in ("SETUP", "AGENT_RUNNING") else "future")))
        test_states = [self.builder_tests_status(b) for b in builders] if builders else []
        if builders and all(s == "PASS" for s in test_states):
            items.append(item("AUTOMATED_TESTS", "done"))
        elif any(s == "FAIL" for s in test_states):
            items.append(item("AUTOMATED_TESTS", "blocked", "Automated tests are failing."))
        else:
            items.append(item("AUTOMATED_TESTS", "current" if current_step == "AGENT_RUNNING" and all_builders_ready else "future"))
        review_blocked = any(b["fix_required"] for b in builders)
        items.append(item("REVIEW", "done" if all_reviews_current_pass else ("blocked" if review_blocked else ("current" if current_step == "REVIEW" else "future"))))
        if not self.requires_qa(risk):
            items.append(item("RUNTIME_VERIFICATION", "skipped", "Not required for this Workflow."))
        else:
            items.append(item("RUNTIME_VERIFICATION", "done" if qa_ok else ("current" if current_step == "TEST_QA" else "future")))
        if not self.requires_integration(risk):
            items.append(item("INTEGRATION", "skipped", "Not required for this Workflow."))
        else:
            items.append(item("INTEGRATION", "done" if integration_ok else ("current" if current_step == "INTEGRATION" else "future")))
        items.append(item("MERGE", "done" if all_merged else ("current" if current_step in ("READY_FOR_MAIN", "DONE") else "future")))
        return items

    def _missing_requirements(self, next_action, blocking):
        """Plain-language bullets for what is stopping the current step
        (section 4) -- a real blocker always wins (it is the actual
        reason), otherwise the fixed per-action checklist of what the
        step still needs (section 12-15: e.g. Create Sandbox still shows
        'Manual verification has not been completed' as a second, not-yet-
        reached requirement, not just the one blocking right now)."""
        if blocking:
            return list(blocking)
        return list(MISSING_REQUIREMENTS.get(next_action["action"], []))

    def _previous_step_summary(self, builders, current_step):
        """Section 11: a short completion summary of the step just
        finished, never the full Agent Report -- only ever real, recorded
        fields (commit/what_changed/tests/review), no invented text. Keyed
        off the first Builder Workspace -- the common single-Builder case
        this spec's own worked example is -- a multi-Builder Task can
        still see every Builder's own detail in Advanced."""
        if not builders or current_step in (None, "TASK", "SETUP", "AGENT_RUNNING"):
            return None
        b = builders[0]
        if not b.get("ready"):
            return None
        report = b.get("report") or {}
        return {
            "agent": b["agent"], "commit": (b.get("head") or "")[:8] or None,
            "what_changed": report.get("what_changed") or None,
            "tests_status": self.builder_tests_status(b),
            "review_status": b["review_status"] if current_step != "REVIEW" else None,
        }

    def _result(self, t, status, stage, next_action, blocking, test_readiness, integration_eligible, ready_for_main,
                builders, qa, ti, ti_repos, merges, risk, integration_eligibility=None, current_step=None):
        all_builders_ready = bool(builders) and all(b["ready"] for b in builders)
        all_reviews_current_pass = bool(builders) and all(b["review_status"] == "PASS" for b in builders)
        qa_ok = (not self.requires_qa(risk)) or (qa is not None and qa["status"] == "PASS" and self._qa_current(qa, t))
        integration_ok = (not self.requires_integration(risk)) or self._integration_ok(ti, ti_repos)
        required_merges = [m for m in merges if m["required"]]
        all_merged = bool(required_merges) and all(m["merge_status"] == "MERGED" for m in required_merges)
        return {
            "task": t, "status": status, "stage": stage, "risk_profile": risk, "next_action": next_action,
            "blocking_reasons": blocking, "test_readiness": test_readiness, "ready_for_main": ready_for_main,
            "builders": builders, "qa": qa, "task_integration": ti, "integration_repos": ti_repos,
            "merge_records": merges, "current_step": current_step,
            "integration_eligibility": integration_eligibility or {"required": self.requires_integration(risk), "eligible": integration_eligible, "exists": bool(ti)},
            "effective_task_prompt": effective_task_prompt(t), "prompt_source": prompt_source(t),
            "checklist": self._checklist(status, builders, risk, current_step, all_builders_ready, all_reviews_current_pass, qa_ok, integration_ok, all_merged),
            "missing_requirements": self._missing_requirements(next_action, blocking),
            "previous_step_summary": self._previous_step_summary(builders, current_step),
        }
