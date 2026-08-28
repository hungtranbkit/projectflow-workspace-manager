from __future__ import annotations
import subprocess, threading
from datetime import datetime, timezone
from pathlib import Path
from app.services.project_contract import load_contract

def now(): return datetime.now(timezone.utc).isoformat()

class TestRunner:
    def __init__(self, db, git): self.db, self.git = db, git
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
                proc = subprocess.run(command, cwd=(path / working).resolve(), shell=True, text=True, capture_output=True, timeout=timeout)
                status = "PASS" if proc.returncode == 0 else "FAIL"; overall = overall and proc.returncode == 0
                self.db.execute("UPDATE test_runs SET status=?,finished_at=?,exit_code=?,stdout_tail=?,stderr_tail=? WHERE id=?", (status, now(), proc.returncode, proc.stdout[-50000:], proc.stderr[-50000:], run_id))
            except subprocess.TimeoutExpired as exc:
                overall = False; self.db.execute("UPDATE test_runs SET status='TIMEOUT',finished_at=?,exit_code=124,stdout_tail=?,stderr_tail=? WHERE id=?", (now(), (exc.stdout or "")[-50000:], (exc.stderr or "")[-50000:], run_id))
        action = "TEST_PASS" if overall else "TEST_FAIL"; self.db.event(kind, entity_id, action)
        if kind == "integration":
            status = "TESTING" if overall else "FAILED"
            self.db.execute("UPDATE integration_workspaces SET status=?,ready_for_main=0,verified_commit=NULL,verified_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, entity_id))

