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
        """Run once: perform any cleanup that is due, mark any sandbox
        stuck in a transient state (PROVISIONING/STOPPING/CLEANING) from a
        prior process's unfinished work as needing attention rather than
        silently trusting stale in-progress status, and re-verify every
        currently-RUNNING sandbox is still actually healthy.

        P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a real Docker
        container can die on its own between polls (OOM-killed, host
        resource exhaustion, `docker stop` from outside ProjectFlow
        entirely) -- before this, `sandboxes.status` only ever left
        RUNNING once a human happened to open the sandbox and click
        Check Health, so the dashboard/Task detail page could claim
        RUNNING indefinitely after the real container was long gone.
        Reuses SandboxManager.health_check() exactly as the manual
        button already does (same HTTP health-spec probe, same
        RUNTIME_DEPENDENCY check) -- just on this worker's own poll
        interval instead of waiting for a click. One sandbox's health
        check failing to even run (contract missing, runtime error) must
        never abort reconciling the rest."""
        for sb in self.due_sandboxes():
            try:
                self.manager.cleanup(sb["id"])
            except Exception as exc:
                self.db.event("sandbox", sb["id"], "CLEANUP_WORKER_ERROR", str(exc))
        # P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a REAL, reproduced
        # bug -- every sandbox action route (start/stop/restart/rebuild/
        # reset-data/cleanup, app/main.py's SANDBOX_BUSY_STATUSES check)
        # refuses to run while status is PROVISIONING/STARTING/
        # RESETTING/CLEANING, and the detail page auto-reloads forever
        # while busy (sandbox_detail.html's own `data-running` JS). A
        # background operation thread doing that real work dies with
        # the OLD process on a server restart -- previously this only
        # logged an event and left the row genuinely BUSY forever, which
        # made the sandbox permanently unrecoverable through the UI (no
        # button ever becomes clickable again). Since reconcile() runs
        # once immediately on start() (this class's own docstring),
        # anything found busy here was abandoned by a process that no
        # longer exists -- mark it UNHEALTHY (a real, already-recognized
        # status outside SANDBOX_BUSY_STATUSES) with a clear reason, so
        # Rebuild/Cleanup become clickable again instead of a dead end.
        # Exactly app/main.py's own SANDBOX_BUSY_STATUSES -- the single
        # source of truth for "no action route will touch this row".
        for stuck in self.db.all("SELECT id, status FROM sandboxes WHERE status IN ('PROVISIONING','STARTING','RESETTING','CLEANING')"):
            self.db.execute(
                "UPDATE sandboxes SET status='UNHEALTHY',health_status='UNHEALTHY',error_code='INTERRUPTED_BY_RESTART',"
                "error_message=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (f"Operation ({stuck['status']}) was interrupted by a server restart. Use Rebuild or Cleanup to recover.", stuck["id"]))
            self.db.event("sandbox", stuck["id"], "RECONCILE_STALE_OPERATION_STATE", f"was {stuck['status']}, left running after a prior process restart -- marked UNHEALTHY, recoverable again")
        for running in self.db.all("SELECT id FROM sandboxes WHERE status='RUNNING'"):
            try:
                self.manager.health_check(running["id"])
            except Exception as exc:
                self.db.event("sandbox", running["id"], "RECONCILE_HEALTH_CHECK_ERROR", str(exc))

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
