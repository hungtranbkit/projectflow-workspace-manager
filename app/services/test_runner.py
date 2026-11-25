from __future__ import annotations
import subprocess, threading
from datetime import datetime, timezone
from pathlib import Path
from app.services.project_contract import ContractError, load_contract
from app.services.visual_qa_contract import task_requires_visual_qa

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
        try:
            source_state = self.git.source_state(path)
        except Exception as exc:
            raise ContractError(f"Could not capture test source identity: {type(exc).__name__}.") from exc
        stages = self.stages_for(kind, entity_id, path)
        try:
            queued_state = self.git.source_state(path)
        except Exception as exc:
            raise ContractError(f"Could not verify stable test source identity: {type(exc).__name__}.") from exc
        if queued_state != source_state:
            raise ContractError("Repository source changed while test stages were queued; retry against a stable worktree.")
        # A dirty or untracked source tree is still testable for developer
        # feedback, but its result cannot be evidence for HEAD's commit.
        tested_commit = source_state["head"] if source_state["clean"] else None
        ids = [self.db.execute("INSERT INTO test_runs(workspace_type,workspace_id,command,stage,status,tested_commit) VALUES(?,?,?,?,?,?)", (kind, entity_id, cmd, stage, "QUEUED", tested_commit)) for stage, cmd, _, _ in stages]
        self.db.event(kind, entity_id, "TEST_STARTED", ", ".join(x[0] for x in stages))
        threading.Thread(target=self._run, args=(kind, entity_id, path, stages, ids, source_state), daemon=True).start()
        return ids

    def stages_for(self, kind: str, entity_id: int, path: Path):
        task = self._task_for(kind, entity_id)
        return load_contract(path, visual_qa_required=task_requires_visual_qa(task))

    def _task_for(self, kind: str, entity_id: int):
        if kind == "agent":
            return self.db.one(
                "SELECT t.implementation_prompt,t.brief_requirements,t.brief_test_plan "
                "FROM agent_workspaces w JOIN tasks t ON t.id=w.task_id WHERE w.id=?",
                (entity_id,),
            )
        if kind == "integration":
            return self.db.one(
                "SELECT t.implementation_prompt,t.brief_requirements,t.brief_test_plan "
                "FROM integration_workspaces iw "
                "JOIN task_integrations ti ON ti.id=iw.task_integration_id "
                "JOIN tasks t ON t.id=ti.task_id WHERE iw.id=?",
                (entity_id,),
            )
        return None
    @staticmethod
    def _same_source(left, right):
        stable = (left["head"], left["tracked_diff_sha256"]) == (right["head"], right["tracked_diff_sha256"])
        # For an initially clean tree, newly appearing untracked files are
        # uncommitted source candidates and cannot inherit the queued SHA.
        # A tree already dirty at queue time has no commit binding; its
        # disposable untracked outputs remain usable for feedback.
        return stable and (not left["clean"] or left["untracked_paths"] == right["untracked_paths"])

    @staticmethod
    def _source_drift_detail(stage, when):
        return f"SOURCE_CHANGED_{when}_TEST: repository HEAD, tracked files, or newly untracked source paths changed; this stage is not valid evidence for the queued source."

    def _run(self, kind, entity_id, path, stages, ids, source_state):
        overall = True
        for (stage, command, working, timeout), run_id in zip(stages, ids):
            if not overall:
                self.db.execute("UPDATE test_runs SET status='SKIPPED',finished_at=? WHERE id=?", (now(), run_id)); continue
            try:
                current_source = self.git.source_state(path)
            except Exception as exc:
                current_source = None
                source_error = f"Source state could not be verified: {type(exc).__name__}."
            else:
                source_error = self._source_drift_detail(stage, "BEFORE") if not self._same_source(source_state, current_source) else ""
            if source_error:
                overall = False
                self.db.execute("UPDATE test_runs SET status='FAIL',finished_at=?,stderr_tail=? WHERE id=?",
                                (now(), source_error[-50000:], run_id))
                continue
            self.db.execute("UPDATE test_runs SET status='RUNNING',started_at=? WHERE id=?", (now(), run_id))
            try:
                proc = self.runner.run(command, path, working, timeout)
                try:
                    finished_source = self.git.source_state(path)
                    drift = not self._same_source(source_state, finished_source)
                except Exception:
                    drift = True
                status = "PASS" if proc.returncode == 0 and not drift else "FAIL"
                overall = overall and status == "PASS"
                stderr = proc.stderr or ""
                if drift:
                    stderr = (stderr + "\n" + self._source_drift_detail(stage, "DURING")).strip()
                if source_state["head"] and not source_state.get("clean"):
                    stderr = (stderr + "\nSOURCE_NOT_COMMIT_BOUND: worktree was dirty when tests were queued; commit and rerun for current-SHA evidence.").strip()
                self.db.execute("UPDATE test_runs SET status=?,finished_at=?,exit_code=?,stdout_tail=?,stderr_tail=? WHERE id=?", (status, now(), proc.returncode, proc.stdout[-50000:], stderr[-50000:], run_id))
            except subprocess.TimeoutExpired as exc:
                overall = False
                stderr = (exc.stderr or "")
                if not source_state.get("clean"):
                    stderr = (stderr + "\nSOURCE_NOT_COMMIT_BOUND: worktree was dirty when tests were queued; commit and rerun for current-SHA evidence.").strip()
                self.db.execute("UPDATE test_runs SET status='TIMEOUT',finished_at=?,exit_code=124,stdout_tail=?,stderr_tail=? WHERE id=?", (now(), (exc.stdout or "")[-50000:], stderr[-50000:], run_id))
        action = "TEST_PASS" if overall else "TEST_FAIL"; self.db.event(kind, entity_id, action)
        if kind == "integration":
            status = "TESTING" if overall else "FAILED"
            self.db.execute("UPDATE integration_workspaces SET status=?,ready_for_main=0,verified_commit=NULL,verified_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, entity_id))

