from __future__ import annotations

"""Engineering Domain Foundation (Phase E1.1/E1.2): Change is the first-
class human/product intent that may produce many engineering Tasks --
the layer ABOVE existing Task execution the E1 spec asks for. This
service is deliberately thin (create/get/list/attach), the same
"table + small query/command service" pattern TaskDecisionService's own
callers already use in app/main.py -- never a second, competing "Task
store" abstraction.

Project identity (E1.6): `project_id` references `repositories(id)` --
ProjectFlow has no separate `projects` table, and `repositories`
already is the project boundary (one row per registered repo, each
with its own PROJECT.yaml). Reused as-is."""

CHANGE_TYPES = (
    "FEATURE", "BUG", "IMPROVEMENT", "REFACTOR", "ARCHITECTURE_CHANGE",
    "SECURITY_CHANGE", "HOTFIX", "OPERATIONS",
)
# Same risk vocabulary Task.risk_profile already uses (RISK_PROFILES in
# task_decision_service.py) -- one risk vocabulary in this codebase, not
# a second, independently-drifting one for Change.
RISK_LEVELS = ("LOW", "NORMAL", "HIGH")
LIFECYCLE_STATES = (
    "NEW", "ANALYZING", "SPECIFYING", "DESIGNING", "PLANNING", "BUILDING",
    "REVIEWING", "VERIFYING", "RELEASING", "DEPLOYING", "HUMAN_ACCEPTANCE",
    "DONE", "BLOCKED", "CANCELLED",
)
_TERMINAL_STATES = ("DONE", "CANCELLED")


class ChangeError(ValueError):
    pass


class ChangeService:
    def __init__(self, db):
        self.db = db

    def create(self, *, title: str, description: str = "", change_type: str = "FEATURE",
               risk_level: str = "NORMAL", project_id: int | None = None) -> int:
        title = (title or "").strip()
        if not title:
            raise ChangeError("Change title is required")
        change_type = (change_type or "FEATURE").strip().upper()
        if change_type not in CHANGE_TYPES:
            raise ChangeError(f"Unknown change_type: {change_type} (must be one of {CHANGE_TYPES})")
        risk_level = (risk_level or "NORMAL").strip().upper()
        if risk_level not in RISK_LEVELS:
            raise ChangeError(f"Unknown risk_level: {risk_level} (must be one of {RISK_LEVELS})")
        if project_id is not None and not self.db.one("SELECT id FROM repositories WHERE id=?", (project_id,)):
            raise ChangeError(f"Unknown project_id: {project_id}")
        cid = self.db.execute(
            "INSERT INTO changes(project_id,title,description,change_type,risk_level,lifecycle_state) VALUES(?,?,?,?,?,'NEW')",
            (project_id, title, (description or "").strip(), change_type, risk_level),
        )
        self.db.event("change", cid, "CHANGE_CREATED", f"type={change_type} risk={risk_level}")
        return cid

    def get(self, change_id: int) -> dict | None:
        return self.db.one("SELECT * FROM changes WHERE id=?", (change_id,))

    def list(self, *, project_id: int | None = None) -> list[dict]:
        if project_id is not None:
            return self.db.all("SELECT * FROM changes WHERE project_id=? ORDER BY id DESC", (project_id,))
        return self.db.all("SELECT * FROM changes ORDER BY id DESC")

    def set_lifecycle_state(self, change_id: int, state: str) -> None:
        """E1: no automatic derivation yet (deliberately deferred to a
        later phase, unlike Task.status which TaskDecisionService already
        computes live) -- this is a plain, explicit, human-driven
        transition. No route in this phase lets an Agent call this with
        DONE; see app/main.py's Change API surface."""
        state = (state or "").strip().upper()
        if state not in LIFECYCLE_STATES:
            raise ChangeError(f"Unknown lifecycle_state: {state} (must be one of {LIFECYCLE_STATES})")
        row = self.get(change_id)
        if not row:
            raise ChangeError("Change not found")
        closed_at_sql = ",closed_at=CURRENT_TIMESTAMP" if state in _TERMINAL_STATES else ""
        self.db.execute(
            f"UPDATE changes SET lifecycle_state=?,updated_at=CURRENT_TIMESTAMP{closed_at_sql} WHERE id=?",
            (state, change_id),
        )
        self.db.event("change", change_id, "LIFECYCLE_STATE_CHANGED", f"{row['lifecycle_state']} -> {state}")

    def list_tasks_for_change(self, change_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM tasks WHERE change_id=? ORDER BY id", (change_id,))

    def attach_task_to_change(self, change_id: int, task_id: int) -> None:
        """E1.2: existing Tasks keep working with change_id NULL --
        attaching is always an explicit, optional action, never implied
        or required by anything else in this phase."""
        if not self.get(change_id):
            raise ChangeError("Change not found")
        if not self.db.one("SELECT id FROM tasks WHERE id=?", (task_id,)):
            raise ChangeError("Task not found")
        self.db.execute("UPDATE tasks SET change_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (change_id, task_id))
        self.db.event("task", task_id, "ATTACHED_TO_CHANGE", str(change_id))
