from __future__ import annotations
import hashlib
from pathlib import Path

"""Worktree Isolation Foundation (Phase E8.5).

DISCOVERY (see the phase's own FINAL REPORT for the full map): this
codebase has never had a "Builder edits the canonical checkout"
execution model to migrate away from. GitWorkspaceService.create_agent()
-- called by the ONE existing add_task_workspace() function in
app/main.py, for a manual "Start Builder" click and an E8 autonomous
launch alike -- has always created a real, isolated `git worktree add`
per Builder Workspace, from an immutable base_commit, on a
deterministic collision-checked branch (agent/<agent>/<task-slug>).
agent_workspaces already IS the durable Task<->worktree identity E8.5.2
asks for (repository_id, task_id, branch, worktree_path, base_branch,
base_commit, last_commit, status, created_at, updated_at, closed_at).

WorktreeManager is therefore a THIN service over that EXISTING table +
GitWorkspaceService -- never a second workspace concept, never a
second Git abstraction, never a second launch mechanism. It adds
exactly what genuinely doesn't exist yet: a richer, computed lifecycle
status (never a stored duplicate of agent_workspaces.status), base-
staleness detection, a real non-mutating integration/conflict check
(reusing the exact create_baseline_probe/merge/conflict_files/
remove_baseline_probe primitives E1's own integration flow already
built), explicit abandon/remove operations, and canonical-checkout-
untouched verification."""

LIFECYCLE_STATES = (
    "CREATING", "READY", "IN_USE", "CHANGES_PRESENT", "REVIEW_PENDING",
    "INTEGRATED", "ABANDONED", "MISSING", "REMOVED",
)


class WorktreeManagerError(ValueError):
    pass


class WorktreeManager:
    def __init__(self, db, git, add_task_workspace):
        self.db = db
        self.git = git
        # E8.5.9/E8.5.1: the ONLY callable this service ever uses to
        # actually create a workspace/worktree -- the exact same closure
        # app/main.py's manual routes and AutonomousExecutionService both
        # already use. No second launch/creation mechanism.
        self._add_task_workspace = add_task_workspace

    # ---- reads -----------------------------------------------------------
    def _ws_row(self, task_id: int) -> dict | None:
        return self.db.one(
            "SELECT w.*, r.repo_path, r.repo_name FROM agent_workspaces w "
            "JOIN repositories r ON r.id = w.repository_id "
            "WHERE w.task_id = ? ORDER BY w.id DESC LIMIT 1", (task_id,))

    def _live_session(self, workspace_id: int) -> dict | None:
        from app.services.task_decision_service import LIVE_SESSION_STATUSES
        return self.db.one(
            "SELECT * FROM agent_sessions WHERE workspace_id=? AND status IN (%s) ORDER BY id DESC LIMIT 1" %
            ",".join("?" * len(LIVE_SESSION_STATUSES)), (workspace_id, *LIVE_SESSION_STATUSES))

    def _merges_all_in(self, task_id: int) -> bool:
        rows = self.db.all("SELECT merge_status,required FROM merge_records WHERE task_id=?", (task_id,))
        required = [r for r in rows if r["required"]]
        return bool(required) and all(r["merge_status"] == "MERGED" for r in required)

    def lifecycle_status(self, ws: dict, task_id: int) -> str:
        """Derived, computed ONLY -- never a second stored status column.
        Reads agent_workspaces.status/closed_at/abandoned_at (existing +
        E8.5's own two additive columns), live agent_sessions (already
        reconciled honestly on restart by AgentSessionManager.
        reconcile_on_startup(), so IN_USE naturally clears itself after a
        crash -- E8.5.22), and merge_records (existing E1 integration
        evidence) for INTEGRATED."""
        if ws.get("abandoned_at"):
            return "ABANDONED"
        if not Path(ws["worktree_path"]).is_dir():
            return "MISSING"
        if ws.get("closed_at"):
            return "INTEGRATED" if self._merges_all_in(task_id) else "REMOVED"
        # REVIEW_PENDING (an explicit Submit-for-Review already happened)
        # outranks IN_USE -- a real interactive Builder CLI never exits
        # on its own turn's end (confirmed live: a genuine Claude Code
        # session stays RUNNING/idle after committing and being marked
        # READY), so "a PTY happens to still be attached" is a weaker,
        # less specific signal than "this workspace was already
        # submitted for review."
        if ws.get("status") == "READY":
            return "REVIEW_PENDING"
        if self._live_session(ws["id"]):
            return "IN_USE"
        try:
            changed = bool(self.git.changed_files(ws["worktree_path"], ws["base_commit"]))
        except Exception:
            changed = False
        if changed:
            return "CHANGES_PRESENT"
        return "READY"

    def get_task_worktree(self, task_id: int) -> dict | None:
        ws = self._ws_row(task_id)
        if not ws:
            return None
        staleness = self.check_staleness(task_id, ws=ws)
        return {**ws, "lifecycle_status": self.lifecycle_status(ws, task_id),
                "worktree_isolation": True, "staleness": staleness}

    def inspect_task_worktree(self, task_id: int) -> dict:
        ws = self._ws_row(task_id)
        if not ws:
            raise WorktreeManagerError("This Task has no managed worktree")
        details = {"head": None, "status": [], "modified": [], "untracked": [], "commits": []}
        if Path(ws["worktree_path"]).is_dir():
            try:
                details = self.git.details(ws["worktree_path"])
            except Exception:
                pass
        return {**self.get_task_worktree(task_id), "details": details,
                "integration_check": self.check_integration(task_id, ws=ws)}

    def list_repository_worktrees(self, repository_id: int) -> list[dict]:
        rows = self.db.all(
            "SELECT w.*, r.repo_path, r.repo_name FROM agent_workspaces w "
            "JOIN repositories r ON r.id = w.repository_id WHERE w.repository_id=? ORDER BY w.id DESC",
            (repository_id,))
        return [{**w, "lifecycle_status": self.lifecycle_status(w, w["task_id"])} for w in rows if w.get("task_id")]

    # ---- staleness (E8.5.18) ---------------------------------------------
    def check_staleness(self, task_id: int, ws: dict | None = None) -> dict:
        ws = ws or self._ws_row(task_id)
        if not ws:
            return {"stale": False, "reason": None}
        try:
            current_base_head = self.git.head(ws["repo_path"], ws["base_branch"])
        except Exception:
            return {"stale": False, "reason": None, "base_commit": ws["base_commit"], "current_base_head": None}
        stale = current_base_head != ws["base_commit"]
        return {"stale": stale, "reason": "WORKTREE_BASE_STALE" if stale else None,
                "base_commit": ws["base_commit"], "current_base_head": current_base_head}

    # ---- integration/conflict check (E8.5.19) -----------------------------
    def check_integration(self, task_id: int, ws: dict | None = None) -> dict:
        """Non-mutating dry-run merge of the Task's own branch into the
        CURRENT canonical base branch tip -- reuses create_baseline_probe/
        merge/conflict_files/remove_baseline_probe verbatim (the exact
        primitives E1's own integration/waiver-check flow already built),
        never a second merge-simulation mechanism. Never actually merges
        anything real; the probe worktree is always disposable and
        force-removed afterward."""
        ws = ws or self._ws_row(task_id)
        if not ws:
            return {"result": "UNKNOWN", "reason": "no managed worktree"}
        staleness = self.check_staleness(task_id, ws=ws)
        try:
            current_base_head = self.git.head(ws["repo_path"], ws["base_branch"])
            probe_path = self.git.create_baseline_probe(ws["repo_path"], current_base_head)
        except Exception as exc:
            return {"result": "UNKNOWN", "reason": str(exc)}
        try:
            result = self.git.merge(probe_path, ws["branch"])
            conflicts = self.git.conflict_files(probe_path) if result.returncode != 0 else []
            self.git.git(probe_path, "merge", "--abort", check=False)
            outcome = "CONFLICT" if conflicts else "CLEAN"
            if outcome == "CLEAN" and staleness["stale"]:
                # A clean simulated merge is still worth flagging when the
                # base has moved (E8.5.18: informational, never auto-
                # rebased) -- BASE_STALE takes precedence in the report so
                # a caller knows the Task worktree isn't sitting on the
                # current tip even though nothing would conflict.
                outcome = "BASE_STALE"
            return {"result": outcome, "conflicting_files": conflicts, "base_stale": staleness["stale"]}
        finally:
            try:
                self.git.remove_baseline_probe(ws["repo_path"], probe_path)
            except Exception:
                pass

    # ---- canonical-checkout-untouched verification (E8.5.5) --------------
    @staticmethod
    def _status_hash(status_text: str) -> str:
        return hashlib.sha256(status_text.encode("utf-8")).hexdigest()

    def snapshot_canonical_status(self, workspace_id: int, repo_path: str) -> None:
        """Called at Builder launch time (see AutonomousExecutionService/
        _start_builder_session integration) -- a hash, never the raw
        status text, of the CANONICAL repository's own git status right
        before a session starts. Compared again in verify_canonical_
        untouched() so an unexpected change to the canonical checkout
        during that window is detected from real evidence."""
        try:
            snapshot = self._status_hash(self.git.status(repo_path))
        except Exception:
            return
        self.db.execute("UPDATE agent_workspaces SET canonical_status_snapshot=? WHERE id=?", (snapshot, workspace_id))

    def verify_canonical_untouched(self, workspace_id: int) -> dict:
        ws = self.db.one("SELECT w.*, r.repo_path FROM agent_workspaces w JOIN repositories r ON r.id=w.repository_id WHERE w.id=?", (workspace_id,))
        if not ws or not ws.get("canonical_status_snapshot"):
            return {"checked": False, "modified": False}
        try:
            current = self._status_hash(self.git.status(ws["repo_path"]))
        except Exception:
            return {"checked": False, "modified": False}
        modified = current != ws["canonical_status_snapshot"]
        if modified:
            self.db.event("task", ws["task_id"], "CANONICAL_REPO_MODIFIED",
                           f"workspace={workspace_id} repository_id={ws['repository_id']}")
        return {"checked": True, "modified": modified}

    # ---- explicit lifecycle operations (E8.5.20/E8.5.21) ------------------
    def create_task_worktree(self, task_id: int, repository_id: int, agent: str, role: str = "",
                              base_branch: str | None = None, sandbox_profile: str = "") -> dict:
        """Diagnostic/manual-testing entry point (E8.5.26) -- delegates to
        the EXACT existing add_task_workspace() closure, same as
        AutonomousExecutionService._launch() does. Never a second
        creation path."""
        repo = self.db.one("SELECT * FROM repositories WHERE id=?", (repository_id,))
        if not repo:
            raise WorktreeManagerError("Unknown repository_id")
        result = self._add_task_workspace(task_id, repository_id, agent, role,
                                           base_branch or repo["default_branch"], sandbox_profile)
        if not result.get("ok"):
            raise WorktreeManagerError(result.get("error") or "Could not create Task worktree")
        self.db.event("task", task_id, "TASK_WORKTREE_READY", f"workspace={result['workspace_id']}")
        return result

    def abandon_task_worktree(self, task_id: int, note: str = "") -> dict:
        """E8.5.21: preserves ALL metadata (row, branch, worktree on disk)
        -- never deletes anything itself. A human/operator can still
        inspect an abandoned worktree; only remove_task_worktree() (an
        explicit, separate call) ever touches the filesystem."""
        ws = self._ws_row(task_id)
        if not ws:
            raise WorktreeManagerError("This Task has no managed worktree")
        if ws.get("abandoned_at"):
            return {"ok": True, "already_abandoned": True, "workspace_id": ws["id"]}
        self.db.execute("UPDATE agent_workspaces SET abandoned_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP,notes=? WHERE id=?",
                         ((ws["notes"] + ("\n" if ws["notes"] else "") + note).strip() if note else ws["notes"], ws["id"]))
        self.db.event("task", task_id, "TASK_WORKTREE_ABANDONED", note or "")
        return {"ok": True, "workspace_id": ws["id"]}

    def remove_task_worktree(self, task_id: int, force: bool = False) -> dict:
        """E8.5.20: removal allowed only once INTEGRATED, ABANDONED, or
        an explicit operator force -- never merely because a Builder
        process exited. Only ever removes a path this Task's own
        registered agent_workspaces row names -- GitWorkspaceService.
        validate_worktree()'s own containment check is the second,
        independent guard against removing anything else."""
        ws = self._ws_row(task_id)
        if not ws:
            raise WorktreeManagerError("This Task has no managed worktree")
        status = self.lifecycle_status(ws, task_id)
        if status not in ("INTEGRATED", "ABANDONED") and not force:
            raise WorktreeManagerError(
                f"Worktree is {status}, not INTEGRATED or ABANDONED -- pass force=true for an explicit operator override")
        if Path(ws["worktree_path"]).is_dir():
            try:
                if status == "ABANDONED" or force:
                    self.git.git(ws["repo_path"], "worktree", "remove", "--force", ws["worktree_path"], check=False)
                else:
                    self.git.close(ws["repo_path"], ws["worktree_path"])
            except Exception as exc:
                raise WorktreeManagerError(f"Could not remove worktree: {exc}") from exc
        self.db.execute(
            "UPDATE agent_workspaces SET status='CLOSED',closed_at=COALESCE(closed_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (ws["id"],))
        self.db.event("task", task_id, "TASK_WORKTREE_REMOVED", f"workspace={ws['id']} force={force}")
        return {"ok": True, "workspace_id": ws["id"]}
