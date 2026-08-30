from __future__ import annotations
import json

from app.services.review_service import task_chain_ids

"""Bounded Review/Fix loop (E9.13/E9.14/E9.18). ProjectFlow, not the
LLM, owns the state machine: tick() is a pure, explicit, single-step
function -- Implementation CODE_CHANGE -> CodeReview -> (SecurityReview
if applicable) -> FIX_REQUIRED creates a real FIX Task -> Fix Builder
-> re-verification -> re-review, bounded at MAX_ROUNDS, never an
infinite Builder<->Reviewer loop.

FIX WORKTREE STRATEGY (E9.14, decided and documented here rather than
improvised): agent_workspaces.worktree_path/branch are both DB-UNIQUE,
so a second row can never point at the SAME physical worktree/branch a
FIX Task would need to reuse. The only safe way to give a FIX Task the
"same retained Task worktree" E9.14 asks for is an explicit OWNERSHIP
TRANSFER -- the existing agent_workspaces row's task_id is repointed
from the original Task to the new FIX Task (never a second row, never
a second `git worktree add`). This is done only when the original
Task's workspace is at the REVIEW boundary (status='READY', no live
session) -- the same "no concurrent Builder in that worktree" safety
E9.14 requires, satisfied by construction (only one task_id can own a
workspace at a time). A note is stamped on the transferred row so the
original Task's own history stays auditable even though
WorktreeManager.get_task_worktree(original_task_id) will 404
afterward -- the change set now belongs to the Fix Task, which is the
whole point of "review findings apply to that exact branch"."""

MAX_ROUNDS = 3
CRITICAL_STOP_CATEGORY = "SECURITY"


class ReviewFixError(ValueError):
    pass


class ReviewFixOrchestratorService:
    def __init__(self, db, changes, work_products, findings_store, code_review_service, security_review_service,
                 security_applicability, worktree_manager, workflow_service, human_decisions, decision, git,
                 start_builder_session, project_policy_resolver=None):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.findings_store = findings_store
        self.code_review_service = code_review_service
        self.security_review_service = security_review_service
        self.security_applicability = security_applicability
        self.worktree_manager = worktree_manager
        self.workflow_service = workflow_service
        self.human_decisions = human_decisions
        self.decision = decision
        self.git = git
        self._start_builder_session = start_builder_session
        self.project_policy_resolver = project_policy_resolver

    # ---- reads ------------------------------------------------------
    def _task(self, task_id: int) -> dict | None:
        return self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def _latest_review(self, task_id: int, kind: str) -> dict | None:
        return self.db.one("SELECT * FROM review_runs WHERE task_id=? AND review_kind=? ORDER BY id DESC LIMIT 1", (task_id, kind))

    def _governing_task_id(self, task_id: int) -> int:
        """A FIX Task chain -- follow forward to whichever Task
        currently owns the worktree (the most recent Fix Task, if any,
        else the Task itself)."""
        current = task_id
        while True:
            child = self.db.one("SELECT id FROM tasks WHERE fix_of_task_id=? ORDER BY id DESC LIMIT 1", (current,))
            if not child:
                return current
            current = child["id"]

    def status(self, task_id: int) -> dict:
        governing = self._governing_task_id(task_id)
        code = self._latest_review(governing, "CODE")
        security = self._latest_review(governing, "SECURITY")
        round_number = max((code or {}).get("round_number", 0) or 0, (security or {}).get("round_number", 0) or 0)
        fix_task = self.db.one("SELECT * FROM tasks WHERE fix_of_task_id=? ORDER BY id DESC LIMIT 1", (governing,)) \
            if governing != task_id else self.db.one("SELECT * FROM tasks WHERE fix_of_task_id=? ORDER BY id DESC LIMIT 1", (task_id,))
        blockers = self.findings_store.open_blocking(task_chain_ids(self.db, governing))
        return {"task_id": task_id, "governing_task_id": governing, "round": round_number, "max_rounds": MAX_ROUNDS,
                "code_review": code, "security_review": security, "current_fix_task": fix_task,
                "blockers": [{"id": f["id"], "category": f["category"], "severity": f["severity"], "title": f["title"]} for f in blockers]}

    def integration_readiness(self, task_id: int) -> dict:
        governing = self._governing_task_id(task_id)
        code = self._latest_review(governing, "CODE")
        blockers = []
        if not code or code["verdict"] not in ("PASS", "PASS_WITH_FINDINGS"):
            blockers.append("CODE_REVIEW_NOT_PASS")
        ws = self.worktree_manager.get_task_worktree(governing)
        if not ws:
            blockers.append("NO_MANAGED_WORKTREE")
        else:
            head = self.git.head(ws["worktree_path"])
            if code and code["reviewed_commit"] != head:
                blockers.append("REVIEW_STALE")
            if ws["staleness"]["stale"]:
                blockers.append("WORKTREE_BASE_STALE")
            integ = self.worktree_manager.check_integration(governing)
            if integ["result"] != "CLEAN":
                blockers.append(f"INTEGRATION_{integ['result']}")
        t = self._task(governing)
        applicability = self._security_applicability(t, ws) if ws else {"required": False}
        security = self._latest_review(governing, "SECURITY")
        if applicability.get("required"):
            if not security or security["verdict"] not in ("PASS", "PASS_WITH_FINDINGS"):
                blockers.append("SECURITY_REVIEW_NOT_PASS")
        if self.findings_store.open_blocking(task_chain_ids(self.db, governing)):
            blockers.append("UNRESOLVED_BLOCKING_FINDING")
        if self.human_decisions.pending_for_change(t["change_id"]) if t and t.get("change_id") else False:
            blockers.append("WAITING_HUMAN")
        return {"task_id": task_id, "governing_task_id": governing,
                "ready": not blockers, "outcome": "INTEGRATION_READY" if not blockers else "BLOCKED", "blockers": blockers}

    def _security_applicability(self, t, ws):
        run = self.workflow_service.get_workflow(t["change_id"]) if t.get("change_id") else None
        profile = run["profile_key"] if run else None
        policy = None
        if self.project_policy_resolver:
            try:
                policy = self.project_policy_resolver(self.changes.get(t["change_id"]))
            except Exception:
                policy = None
        return self.security_applicability.applicable(t["id"], ws, profile, policy)

    # ---- WorkflowService gates (E9.23/E9.24/E9.25) --------------------
    def review_pass(self, task_id: int) -> bool | None:
        """True/False when this Task has real CodeReview evidence for
        its CURRENT head commit; None ("no E9 evidence yet") tells
        WorkflowService to fall back to the legacy per-workspace check
        -- see _gate_review_pass's own docstring."""
        governing = self._governing_task_id(task_id)
        review = self._latest_review(governing, "CODE")
        if not review:
            return None
        ws = self.worktree_manager.get_task_worktree(governing)
        if not ws:
            return None
        try:
            head = self.git.head(ws["worktree_path"])
        except Exception:
            return None
        if review["reviewed_commit"] != head:
            return False  # REVIEW_STALE (E9.28) -- a newer commit exists, this PASS no longer applies
        if review["verdict"] not in ("PASS", "PASS_WITH_FINDINGS"):
            return False
        return not self.findings_store.open_blocking(task_chain_ids(self.db, governing))

    def security_pass(self, task_id: int) -> bool | None:
        """E9.24: real evidence, never a REVIEW_PASS alias. True when
        genuinely NOT_APPLICABLE or a current PASS/PASS_WITH_FINDINGS
        SecurityReview exists with no open CRITICAL finding; False when
        applicable but missing/stale/failing; None only when
        applicability itself cannot be determined (no worktree yet)."""
        governing = self._governing_task_id(task_id)
        t = self._task(governing)
        if not t:
            return None
        ws = self.worktree_manager.get_task_worktree(governing)
        if not ws:
            return None
        applicability = self._security_applicability(t, ws)
        if not applicability.get("required"):
            return True
        review = self._latest_review(governing, "SECURITY")
        if not review:
            return False
        try:
            head = self.git.head(ws["worktree_path"])
        except Exception:
            return None
        if review["reviewed_commit"] != head:
            return False
        if review["verdict"] not in ("PASS", "PASS_WITH_FINDINGS"):
            return False
        return not self._has_critical_security_finding(governing, review["id"])

    # ---- the bounded step (E9.18) ------------------------------------
    def tick(self, task_id: int, provider: str = "claude") -> dict:
        governing_id = self._governing_task_id(task_id)
        t = self._task(governing_id)
        if not t:
            raise ReviewFixError("Task not found")
        if t.get("change_id") and self.human_decisions.pending_for_change(t["change_id"]):
            return {"outcome": "WAITING_HUMAN", "task_id": task_id, "governing_task_id": governing_id}

        ws = self.worktree_manager.get_task_worktree(governing_id)
        if not ws:
            return {"outcome": "NO_MANAGED_WORKTREE", "task_id": task_id}
        head = self.git.head(ws["worktree_path"])

        code = self._latest_review(governing_id, "CODE")
        if not code or code["reviewed_commit"] != head:
            result = self.code_review_service.review_task(governing_id, provider)
            # result's own "outcome" (REVIEWED/NO_CHANGES/EXECUTION_FAILED/
            # ...) is a lower-level detail than this tick()'s own step
            # outcome -- spread first, then override "outcome"/task_id/
            # governing_task_id so this dict's OWN keys always win, never
            # silently clobbered by whatever key order **result happens
            # to contain.
            return {**result, "outcome": "CODE_REVIEW_RUN", "task_id": task_id, "governing_task_id": governing_id}

        governing_review = code
        if code["verdict"] in ("PASS", "PASS_WITH_FINDINGS"):
            applicability = self._security_applicability(t, ws)
            if applicability.get("required"):
                security = self._latest_review(governing_id, "SECURITY")
                if not security or security["reviewed_commit"] != head:
                    result = self.security_review_service.review_task(governing_id, provider)
                    if self._has_critical_security_finding(governing_id, result.get("review_id")):
                        self._security_critical_stop(t, result.get("work_product_id"))
                        return {**result, "outcome": "SECURITY_CRITICAL_BLOCK", "task_id": task_id, "governing_task_id": governing_id}
                    return {**result, "outcome": "SECURITY_REVIEW_RUN", "task_id": task_id, "governing_task_id": governing_id}
                governing_review = security
            else:
                return {"outcome": "REVIEW_FIX_DONE", "task_id": task_id, "governing_task_id": governing_id,
                        "integration_readiness": self.integration_readiness(task_id)}

        verdict = governing_review["verdict"]
        if verdict in ("PASS", "PASS_WITH_FINDINGS"):
            return {"outcome": "REVIEW_FIX_DONE", "task_id": task_id, "governing_task_id": governing_id,
                    "integration_readiness": self.integration_readiness(task_id)}
        if verdict == "HUMAN_DECISION_REQUIRED":
            return {"outcome": "WAITING_HUMAN", "task_id": task_id, "governing_task_id": governing_id}
        if verdict == "REJECT":
            return {"outcome": "REJECTED", "task_id": task_id, "governing_task_id": governing_id}

        # FIX_REQUIRED
        round_number = governing_review["round_number"] or 0
        if round_number + 1 > MAX_ROUNDS:
            self.db.event("task", governing_id, "REVIEW_FIX_LIMIT_REACHED", f"review={governing_review['id']}")
            return {"outcome": "REVIEW_FIX_LIMIT_REACHED", "task_id": task_id, "governing_task_id": governing_id}

        existing_fix = self.db.one("SELECT * FROM tasks WHERE fix_of_task_id=? AND fix_review_id=?", (governing_id, governing_review["id"]))
        if existing_fix:
            fix_task = existing_fix
        else:
            fix_task = self._create_fix_task(t, ws, governing_review)

        live = self.db.one(
            "SELECT id FROM agent_sessions WHERE workspace_id=? AND status IN ('STARTING','RUNNING','WAITING_FOR_INPUT')",
            (ws["id"],))
        if live:
            return {"outcome": "FIX_BUILDER_ALREADY_RUNNING", "task_id": task_id, "governing_task_id": fix_task["id"], "fix_task_id": fix_task["id"]}
        if fix_task["status"] == "BACKLOG":
            self.db.execute("UPDATE tasks SET status='ACTIVE',updated_at=CURRENT_TIMESTAMP WHERE id=?", (fix_task["id"],))
            self.db.event("task", fix_task["id"], "TASK_SELECTED", "auto (E9 fix loop)")
        ws_row = self.db.one("SELECT w.*, r.repo_path, r.repo_name FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.id=?", (ws["id"],))
        try:
            sid = self._start_builder_session(ws_row)
        except Exception as exc:
            self.db.event("task", fix_task["id"], "AUTO_EXECUTION_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED", "task_id": task_id, "governing_task_id": fix_task["id"], "message": str(exc)}
        self.db.event("task", fix_task["id"], "AUTO_BUILDER_LAUNCHED", f"session={sid} workspace={ws['id']} provider=fix")
        return {"outcome": "FIX_BUILDER_LAUNCHED", "task_id": task_id, "governing_task_id": fix_task["id"],
                "fix_task_id": fix_task["id"], "session_id": sid, "workspace_id": ws["id"]}

    def _has_critical_security_finding(self, task_id: int, review_id: int | None) -> bool:
        if not review_id:
            return False
        rows = self.db.all("SELECT * FROM findings WHERE task_id=? AND review_id=? AND status='OPEN'", (task_id, review_id))
        return any(f["severity"] == "CRITICAL" for f in rows)

    def _security_critical_stop(self, t: dict, wp_id: int | None) -> None:
        """E9.22: stop autonomous progression immediately, never
        auto-integrate. Escalated as a real HumanDecision (same
        subject_type='work_product' convention E6.14/E9.21 already
        established) so WAITING_HUMAN surfaces on the Change too."""
        self.db.event("task", t["id"], "SECURITY_CRITICAL_BLOCK", f"work_product={wp_id}")
        if wp_id:
            self.human_decisions.create("work_product", wp_id,
                                         "A CRITICAL security finding was reported -- confirm the intended secure behavior before any autonomous fix or integration proceeds.",
                                         "SECURITY_CRITICAL_BLOCK", "NONE")

    # ---- Fix Task creation (E9.13/E9.14/E9.15) -----------------------
    def _create_fix_task(self, original_task: dict, ws: dict, review: dict) -> dict:
        if ws["status"] != "READY":
            raise ReviewFixError("Original Task's workspace is not at the REVIEW boundary (status=READY) -- cannot safely hand off its worktree")
        if self.db.one("SELECT id FROM agent_sessions WHERE workspace_id=? AND status IN ('STARTING','RUNNING','WAITING_FOR_INPUT')", (ws["id"],)):
            raise ReviewFixError("A live session still owns this worktree -- cannot hand off while a Builder is running")

        findings = self.db.all("SELECT * FROM findings WHERE task_id=? AND review_id=? AND status='OPEN'", (original_task["id"], review["id"]))
        blocking = [f for f in findings if f["severity"] in ("HIGH", "CRITICAL")]
        lines = ["## FIX FINDINGS (resolve these; nothing else)"]
        for f in blocking:
            lines.append(f"- [{f['category']}/{f['severity']}] {f['title']} ({f['file_path'] or 'n/a'}): {f['description']}")
        lines += ["", "## RULES",
                  "- Fix the findings above only -- do not change the approved Spec, weaken tests, or suppress failures.",
                  "- Do not expand scope beyond the original Task's own scope_hints.",
                  "- Preserve already-correct behavior; do not regress anything the review did not flag."]
        slug_base = (original_task["slug"] + "-fix")[:80]
        n = 1
        while self.db.one("SELECT id FROM tasks WHERE slug=?", (f"{slug_base}-{n}",)):
            n += 1
        fix_task_id = self.db.execute(
            "INSERT INTO tasks(slug,title,description,status,change_id,task_type,implementation_prompt,fix_of_task_id,fix_review_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (f"{slug_base}-{n}", f"Fix: {original_task['title']} (review #{review['id']})", "", "BACKLOG",
             original_task.get("change_id"), "FIX", "\n".join(lines), original_task["id"], review["id"]))
        # Ownership transfer -- see this module's own docstring for why
        # this is the only safe way to give the FIX Task the SAME
        # retained worktree/branch (worktree_path/branch are DB-UNIQUE).
        note = f"fix_transferred_from_task={original_task['id']} at review={review['id']}"
        self.db.execute("UPDATE agent_workspaces SET task_id=?,status='CREATED',notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (fix_task_id, ((ws.get("notes") or "") + "\n" + note).strip(), ws["id"]))
        self.db.event("task", fix_task_id, "TASK_WORKTREE_IN_USE", f"transferred from task={original_task['id']} workspace={ws['id']}")
        self.db.event("task", original_task["id"], "FIX_TASK_CREATED", f"fix_task={fix_task_id} review={review['id']}")
        return self.db.one("SELECT * FROM tasks WHERE id=?", (fix_task_id,))
