from __future__ import annotations
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.services.project_contract import ContractError, load_contract
from app.services.failure_classifier import fingerprint, parse_failures


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GateWaiverError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class GateWaiverService:
    """Reproduces a required-gate failure against a Task's own clean base
    commit in a disposable, detached worktree -- the ONLY way
    BaselineFailureEvidence is ever written (section 12: never inferred
    from 'this looks unrelated'). Runs in a background thread (the real
    gate command can take minutes) and always cleans up the probe
    worktree afterward, success or failure."""

    def __init__(self, db, git):
        self.db = db
        self.git = git

    def start_reproduction(self, *, repository_id: int, repo_path: str, base_commit: str, gate: str, test_identifier: str) -> int:
        """Queues a REPRODUCE_BASELINE run (a test_runs row, workspace_type
        'baseline') and starts the real reproduction in a background
        thread. Returns the run id immediately so a caller/route can
        redirect without blocking on a multi-minute test command."""
        try:
            stages = load_contract(Path(repo_path))
        except ContractError as exc:
            raise GateWaiverError("CONTRACT_MISSING", str(exc)) from exc
        matching = next((s for s in stages if s[0] == gate), None)
        if not matching:
            raise GateWaiverError("GATE_NOT_DECLARED", f"'{gate}' is not a required stage in PROJECT.yaml")
        stage, command, working, timeout = matching
        run_id = self.db.execute(
            "INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit,started_at) VALUES(?,?,?,?,?,?,?)",
            ("baseline", repository_id, command, stage, "RUNNING", base_commit, now()),
        )
        thread = threading.Thread(
            target=self._run, args=(run_id, repository_id, repo_path, base_commit, stage, command, working, timeout, test_identifier), daemon=True,
        )
        thread.start()
        return run_id

    def _run(self, run_id, repository_id, repo_path, base_commit, stage, command, working, timeout, test_identifier):
        probe_path = None
        try:
            probe_path = self.git.create_baseline_probe(repo_path, base_commit)
            proc = subprocess.run(command, cwd=(probe_path / working).resolve(), shell=True, text=True, capture_output=True, timeout=timeout)
            status = "PASS" if proc.returncode == 0 else "FAIL"
            self.db.execute(
                "UPDATE test_runs SET status=?,finished_at=?,exit_code=?,stdout_tail=?,stderr_tail=? WHERE id=?",
                (status, now(), proc.returncode, proc.stdout[-50000:], proc.stderr[-50000:], run_id),
            )
            failures = parse_failures(proc.stdout)
            match = next((f for f in failures if f["test_identifier"] == test_identifier), None)
            if match:
                fp = fingerprint(match["test_identifier"], match["reason"])
                self.db.execute(
                    "INSERT INTO baseline_failure_evidence(repository_id,base_commit,gate,test_identifier,failure_fingerprint,baseline_run_id,evidence) VALUES(?,?,?,?,?,?,?)",
                    (repository_id, base_commit, stage, test_identifier, fp, run_id, proc.stdout[-4000:]),
                )
            self.db.event("baseline_probe", run_id, "REPRODUCED" if match else "NOT_REPRODUCED", test_identifier)
        except subprocess.TimeoutExpired as exc:
            self.db.execute(
                "UPDATE test_runs SET status='TIMEOUT',finished_at=?,exit_code=124,stdout_tail=?,stderr_tail=? WHERE id=?",
                (now(), (exc.stdout or "")[-50000:], (exc.stderr or "")[-50000:], run_id),
            )
        except Exception as exc:
            self.db.execute(
                "UPDATE test_runs SET status='FAIL',finished_at=?,exit_code=-1,stderr_tail=? WHERE id=?",
                (now(), str(exc)[-2000:], run_id),
            )
        finally:
            if probe_path is not None:
                try:
                    self.git.remove_baseline_probe(repo_path, probe_path)
                except Exception:
                    pass

    def approve_waiver(self, *, task_id: int, integration_id: int, gate: str, test_identifier: str,
                        failure_fingerprint_value: str, baseline_commit: str, baseline_run_id: int | None,
                        integration_run_id: int | None, reason: str, approved_by: str) -> int:
        """The only way a GateWaiver row is created -- never a blanket
        override. Callers (the HTTP route) are responsible for having
        already verified the fingerprint match against real evidence;
        this just records the audited fact."""
        return self.db.execute(
            "INSERT INTO gate_waivers(task_id,integration_id,gate,test_identifier,failure_fingerprint,baseline_commit,baseline_run_id,integration_run_id,reason,approved_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, integration_id, gate, test_identifier, failure_fingerprint_value, baseline_commit, baseline_run_id, integration_run_id, reason.strip(), approved_by),
        )
