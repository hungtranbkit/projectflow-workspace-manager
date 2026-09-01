from __future__ import annotations
import subprocess, threading
from datetime import datetime, timezone
from pathlib import Path
from app.services.project_contract import load_contract

def now(): return datetime.now(timezone.utc).isoformat()

class TestRunner:
    def __init__(self, db, git, runner=None):
        """`runner` is a SandboxedCommandRunner (B0.6) -- optional so
        every pre-existing direct construction of TestRunner(db, git)
        (tests, scripts) keeps working unmodified, falling back to a
        real, always-direct-host runner (mandatory=False) that
        reproduces today's exact subprocess.run(shell=True) behavior."""
        self.db, self.git = db, git
        if runner is None:
            from app.services.sandboxed_exec import SandboxedCommandRunner
            from app.services.sandbox_runtime import SandboxRuntimeService
            runner = SandboxedCommandRunner(SandboxRuntimeService(), mandatory=False)
        self.runner = runner
    def reconcile_on_startup(self) -> None:
        """P0-2 (docs/CORE_USABILITY_QUALIFICATION.md): a REAL,
        reproduced bug, the same shape as AgentSessionManager/
        CleanupWorker/DeploymentService's own reconcile_on_startup()
        fixes. `/api/integrations/{iid}/test` (app/main.py) refuses a
        new test run while `test_runs.status IN ('QUEUED','RUNNING')`
        for that integration -- the real background thread (`start()`
        below) doing that work dies with the OLD process on a server
        restart mid-run, leaving the row QUEUED/RUNNING forever: the
        no-op guard then blocks every future click for that Integration
        permanently, not just the interrupted one. A server restart
        honestly lost that in-process work -- mark it FAIL with a clear
        reason so a fresh test run is possible again."""
        for stuck in self.db.all("SELECT id, status FROM test_runs WHERE status IN ('QUEUED','RUNNING')"):
            self.db.execute(
                "UPDATE test_runs SET status='FAIL',finished_at=?,stderr_tail=? WHERE id=?",
                (now(), f"Test run was {stuck['status']} when the server restarted; the in-process work was lost. Retry.", stuck["id"]))

    def start(self, kind: str, entity_id: int, path: Path):
        stages = load_contract(path)
        ids = [self.db.execute("INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit) VALUES(?,?,?,?,?,?)", (kind, entity_id, cmd, stage, "QUEUED", self.git.head(path))) for stage, cmd, _, _ in stages]
        self.db.event(kind, entity_id, "TEST_STARTED", ", ".join(x[0] for x in stages))
        threading.Thread(target=self._run, args=(kind, entity_id, path, stages, ids), daemon=True).start()
        return ids
    def _run(self, kind, entity_id, path, stages, ids):
        overall = True
        for (stage, command, working, timeout), run_id in zip(stages, ids):
            if not overall:
                self.db.execute("UPDATE test_runs SET status='SKIPPED',finished_at=? WHERE id=?", (now(), run_id)); continue
            self.db.execute("UPDATE test_runs SET status='RUNNING',started_at=? WHERE id=?", (now(), run_id))
            try:
                proc = self.runner.run(command, path, working, timeout)
                status = "PASS" if proc.returncode == 0 else "FAIL"; overall = overall and proc.returncode == 0
                self.db.execute("UPDATE test_runs SET status=?,finished_at=?,exit_code=?,stdout_tail=?,stderr_tail=? WHERE id=?", (status, now(), proc.returncode, proc.stdout[-50000:], proc.stderr[-50000:], run_id))
            except subprocess.TimeoutExpired as exc:
                overall = False; self.db.execute("UPDATE test_runs SET status='TIMEOUT',finished_at=?,exit_code=124,stdout_tail=?,stderr_tail=? WHERE id=?", (now(), (exc.stdout or "")[-50000:], (exc.stderr or "")[-50000:], run_id))
        action = "TEST_PASS" if overall else "TEST_FAIL"; self.db.event(kind, entity_id, action)
        if kind == "integration":
            status = "TESTING" if overall else "FAILED"
            self.db.execute("UPDATE integration_workspaces SET status=?,ready_for_main=0,verified_commit=NULL,verified_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, entity_id))

