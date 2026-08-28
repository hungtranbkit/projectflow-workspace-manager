from __future__ import annotations
import threading
import time
from datetime import datetime, timezone


def now_iso() -> str: return datetime.now(timezone.utc).isoformat()


class CleanupWorker:
    """Lightweight periodic scheduler -- no Celery/Redis. Persisted
    timestamps (sandboxes.cleanup_eligible_at) are the source of truth, so a
    server restart never loses eligibility: on start(), reconcile() runs
    once immediately (picks up anything that became due while the process
    was down or that was left mid-operation), then a daemon thread polls on
    an interval."""

    def __init__(self, db, sandbox_manager, poll_seconds: int = 60):
        self.db = db
        self.manager = sandbox_manager
        self.poll_seconds = poll_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def due_sandboxes(self) -> list[dict]:
        return self.db.all(
            "SELECT * FROM sandboxes WHERE status='CLEANUP_ELIGIBLE' AND cleanup_eligible_at IS NOT NULL AND cleanup_eligible_at<=?",
            (now_iso(),),
        )

    def reconcile(self) -> None:
        """Run once: perform any cleanup that is due, and mark any sandbox
        stuck in a transient state (PROVISIONING/STOPPING/CLEANING) from a
        prior process's unfinished work as needing attention rather than
        silently trusting stale in-progress status."""
        for sb in self.due_sandboxes():
            try:
                self.manager.cleanup(sb["id"])
            except Exception as exc:
                self.db.event("sandbox", sb["id"], "CLEANUP_WORKER_ERROR", str(exc))
        for stuck in self.db.all("SELECT id FROM sandboxes WHERE status IN ('PROVISIONING','STOPPING','CLEANING')"):
            self.db.event("sandbox", stuck["id"], "RECONCILE_STALE_OPERATION_STATE", "left running after a prior process restart")

    def start(self) -> None:
        self.reconcile()
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.reconcile()

    def stop(self) -> None:
        self._stop.set()
