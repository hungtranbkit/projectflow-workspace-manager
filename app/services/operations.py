from __future__ import annotations
import threading

ACTIVE_STATUSES = ("QUEUED", "RUNNING")
TERMINAL_STATUSES = ("SUCCEEDED", "FAILED")


class OperationInProgress(Exception):
    """Raised by begin()/run_async() when an operation of the same
    (entity_type, entity_id, operation_type) is already QUEUED or RUNNING.
    Callers catch this and simply reflect the existing operation back
    (redirect, no error banner) instead of launching a second real
    git/GitHub/Docker call -- this is the actual duplicate-click guard."""
    def __init__(self, operation: dict):
        self.operation = operation
        super().__init__(f"{operation['operation_type']} already {operation['status']} for {operation['entity_type']}#{operation['entity_id']}")


class OperationService:
    """Generic IDLE -> QUEUED/RUNNING -> SUCCEEDED/FAILED ledger for
    action buttons that have no existing job/run table of their own.
    Never a second source of truth for entities that already track their
    own status (test_runs, sandbox_operations, agent_sessions) -- see
    db.py V12 comment."""

    def __init__(self, db):
        self.db = db

    def active(self, entity_type: str, entity_id: int, operation_type: str) -> dict | None:
        return self.db.one(
            "SELECT * FROM operations WHERE entity_type=? AND entity_id=? AND operation_type=? AND status IN ('QUEUED','RUNNING') ORDER BY id DESC LIMIT 1",
            (entity_type, entity_id, operation_type),
        )

    def latest(self, entity_type: str, entity_id: int, operation_type: str | None = None) -> dict | None:
        if operation_type:
            return self.db.one(
                "SELECT * FROM operations WHERE entity_type=? AND entity_id=? AND operation_type=? ORDER BY id DESC LIMIT 1",
                (entity_type, entity_id, operation_type),
            )
        return self.db.one(
            "SELECT * FROM operations WHERE entity_type=? AND entity_id=? ORDER BY id DESC LIMIT 1",
            (entity_type, entity_id),
        )

    def recent(self, entity_type: str, entity_id: int, limit: int = 10) -> list[dict]:
        return self.db.all(
            "SELECT * FROM operations WHERE entity_type=? AND entity_id=? ORDER BY id DESC LIMIT ?",
            (entity_type, entity_id, limit),
        )

    def begin(self, entity_type: str, entity_id: int, operation_type: str) -> int:
        """Insert a new RUNNING row -- raises OperationInProgress instead
        if one is already active. Must be called BEFORE the real work
        starts (never after), so a concurrent second click always sees it."""
        existing = self.active(entity_type, entity_id, operation_type)
        if existing:
            raise OperationInProgress(existing)
        return self.db.execute(
            "INSERT INTO operations(operation_type,entity_type,entity_id,status,started_at) VALUES(?,?,?,'RUNNING',CURRENT_TIMESTAMP)",
            (operation_type, entity_type, entity_id),
        )

    def succeed(self, op_id: int, result_summary: str = "") -> None:
        self.db.execute(
            "UPDATE operations SET status='SUCCEEDED',completed_at=CURRENT_TIMESTAMP,result_summary=? WHERE id=?",
            ((result_summary or "")[:2000], op_id),
        )

    def fail(self, op_id: int, error) -> None:
        self.db.execute(
            "UPDATE operations SET status='FAILED',completed_at=CURRENT_TIMESTAMP,error=? WHERE id=?",
            (str(error)[:2000], op_id),
        )

    def run_async(self, entity_type: str, entity_id: int, operation_type: str, fn) -> int:
        """Start fn() in a background thread as one tracked Operation.
        fn() may return a short human-readable result_summary string (or
        None); any exception it raises is captured as the FAILED error.
        Raises OperationInProgress synchronously (before spawning
        anything) if one is already active."""
        op_id = self.begin(entity_type, entity_id, operation_type)

        def _run():
            try:
                summary = fn()
                self.succeed(op_id, summary or "")
            except Exception as exc:
                self.fail(op_id, exc)

        threading.Thread(target=_run, daemon=True).start()
        return op_id
