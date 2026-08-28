from __future__ import annotations
from datetime import datetime, timezone

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
RISK_GATES = {"LOW": ("REVIEW",), "NORMAL": ("REVIEW", "INTEGRATION"), "HIGH": ("REVIEW", "QA", "INTEGRATION")}

NEXT_ACTIONS = (
    "REVIEW_BACKLOG", "SELECT_FOR_DEVELOPMENT", "COMPLETE_BRIEF", "CREATE_BUILDER_WORKSPACE",
    "OPEN_BUILDER", "RUN_BUILDER_TEST", "SUBMIT_FOR_REVIEW", "START_REVIEW", "RETURN_TO_BUILDER",
    "START_QA", "CREATE_INTEGRATION", "OPEN_INTEGRATOR", "RUN_INTEGRATION_TEST", "RESOLVE_CONFLICT",
    "REBUILD_SANDBOX", "PREPARE_PR", "WAIT_FOR_MERGES", "CLOSE_TASK", "NONE",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Every next_action target is a GET page/anchor for the human to act on,
# except these three, which are the action itself (a POST endpoint a
# button submits to directly) -- kept as one explicit set so templates
# never have to guess which verb an action code needs.
POST_ACTIONS = {"SELECT_FOR_DEVELOPMENT", "CREATE_INTEGRATION", "CLOSE_TASK"}


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
        """A Task's intent is 'complete' either way: the new single
        Implementation Prompt (the primary path -- one non-empty prompt is
        enough, nothing else required), or the legacy structured brief
        (GOAL + ACCEPTANCE_CRITERIA, for Tasks created before the prompt-
        first UX existed)."""
        if (t.get("implementation_prompt") or "").strip():
            return True
        return bool((t.get("brief_goal") or "").strip() and (t.get("brief_acceptance_criteria") or "").strip())

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
        return {
            "id": w["id"], "agent": w["agent"], "role": w["role"], "repo_name": w["repo_name"],
            "repository_id": w["repository_id"], "worktree_path": w["worktree_path"], "branch": w["branch"],
            "status": w["status"], "head": head, "ready": ready, "report": report,
            "review": review, "review_status": review_status,
            "fix_required": review_status == "FIX_REQUIRED",
        }

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
        qa = self.latest_qa(task_id)
        merges = self.merge_records(task_id, workspaces)
        required_merges = [m for m in merges if m["required"]]

        if t["status"] == "CANCELLED":
            return self._result(t, "CANCELLED", "COMPLETE", _action("NONE", None, "Task cancelled."), [], "NO", False, False,
                                 builders, qa, ti, ti_repos, merges, risk)
        if t["status"] == "BACKLOG":
            na = _action("SELECT_FOR_DEVELOPMENT", "Select for Development", "Task is in Backlog.", f"/api/tasks/{task_id}/select")
            return self._result(t, "BACKLOG", "PLANNING", na, [], "NO", False, False, builders, qa, ti, ti_repos, merges, risk)

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
        return self._result(t, status, stage, next_action, blocking, test_readiness, integration_eligibility["eligible"],
                             ready_for_main, builders, qa, ti, ti_repos, merges, risk, integration_eligibility)

    def _qa_current(self, qa, t):
        return qa.get("brief_version") in (None, t["brief_version"])

    def _integration_ok(self, ti, ti_repos):
        """Healthy AND verified -- 'TESTING' means tests are only in
        flight, not proof of anything yet, so only an explicit
        READY_FOR_MAIN on every participating repo's Integration
        Workspace counts (the same gate /api/integrations/{iid}/
        ready-for-main itself enforces before setting it)."""
        if not ti or ti["status"] == "CONFLICT": return False
        if not ti_repos: return False
        return all(r["status"] == "READY_FOR_MAIN" for r in ti_repos)

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
            if not self.brief_complete(t):
                return _action("COMPLETE_BRIEF", "Write Task Prompt", "Describe the task in the Implementation Prompt.", f"/tasks/{tid}#prompt")
            return _action("CREATE_BUILDER_WORKSPACE", "Create Builder Workspace", "No Builder Workspace yet.", f"/tasks/{tid}#new-workspace")
        for b in builders:
            if b["fix_required"]:
                return _action("RETURN_TO_BUILDER", f"Fix required: {b['agent']}", (b["review"] or {}).get("findings") or "Reviewer requested changes.", f"/workspaces/{b['id']}")
            if not b["ready"]:
                return _action("OPEN_BUILDER", f"Continue Builder: {b['agent']}", "Builder has not submitted for review yet.", f"/workspaces/{b['id']}")
            if b["review_status"] in ("NONE",):
                return _action("SUBMIT_FOR_REVIEW", f"Start Review: {b['agent']}", "Builder ready, no review started.", f"/workspaces/{b['id']}")
            if b["review_status"] == "STALE":
                return _action("START_REVIEW", f"Re-review (source changed): {b['agent']}", "Source or Brief changed since last review.", f"/workspaces/{b['id']}")
            if b["review_status"] in ("PENDING", "RUNNING"):
                return _action("START_REVIEW", f"Complete Review: {b['agent']}", "Review in progress.", f"/workspaces/{b['id']}")
        if self.requires_qa(risk):
            if not qa: return _action("START_QA", "Start QA", "All reviews PASS -- QA required for HIGH risk.", f"/tasks/{tid}#qa")
            if qa["status"] in ("PENDING", "RUNNING"): return _action("START_QA", "Complete QA", "QA in progress.", f"/tasks/{tid}#qa")
            if qa["status"] in ("FAIL", "BLOCKED"): return _action("RETURN_TO_BUILDER", "QA failed", qa.get("notes") or "QA reported a failure.", f"/tasks/{tid}#qa")
            if not self._qa_current(qa, t): return _action("START_QA", "Re-run QA (Brief changed)", "Brief version changed since QA PASS.", f"/tasks/{tid}#qa")
        if self.requires_integration(risk):
            if not ti: return _action("CREATE_INTEGRATION", "Create Integration", "All required gates PASS -- ready to integrate.", f"/api/tasks/{tid}/integrations")
            if ti["status"] == "CONFLICT": return _action("RESOLVE_CONFLICT", "Resolve Conflict", "Integration has a merge conflict.", f"/tasks/{tid}#integration")
            if not self._integration_ok(ti, ti_repos): return _action("RUN_INTEGRATION_TEST", "Run Integration Tests", "Integration exists, tests not current/passing.", f"/tasks/{tid}#integration")
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

    def _result(self, t, status, stage, next_action, blocking, test_readiness, integration_eligible, ready_for_main,
                builders, qa, ti, ti_repos, merges, risk, integration_eligibility=None):
        return {
            "task": t, "status": status, "stage": stage, "risk_profile": risk, "next_action": next_action,
            "blocking_reasons": blocking, "test_readiness": test_readiness, "ready_for_main": ready_for_main,
            "builders": builders, "qa": qa, "task_integration": ti, "integration_repos": ti_repos,
            "merge_records": merges,
            "integration_eligibility": integration_eligibility or {"required": self.requires_integration(risk), "eligible": integration_eligible, "exists": bool(ti)},
        }
