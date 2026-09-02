from __future__ import annotations
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.services.project_contract import load_contract, ContractError

"""Integration (Phase E10.1-E10.6). ProjectFlow, never the Builder or
Reviewer, owns integration -- this is the ONE place a Task's reviewed
worktree branch is actually merged into its repository's canonical
target branch. Reuses E9's own integration_readiness() as the sole
gate (never re-derives review/security/scope/staleness truth), and
E8.5's own real primitives (create_baseline_probe/merge/conflict_files/
remove_baseline_probe) for both the merge itself and post-integration
verification -- never a second Git abstraction."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_runner(argv, cwd, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)


class IntegrationError(ValueError):
    pass


class IntegrationService:
    def __init__(self, db, work_products, worktree_manager, review_fix_orchestrator, git, runner=_default_runner):
        self.db = db
        self.work_products = work_products
        self.worktree_manager = worktree_manager
        self.review_fix_orchestrator = review_fix_orchestrator
        self.git = git
        self.runner = runner

    def reconcile_on_startup(self) -> None:
        """P0 BLOCKER (docs/CORE_USABILITY_QUALIFICATION.md, final
        stability pass): a REAL, reproduced defect found during a
        repo-wide audit for stuck-forever busy state, and the most
        severe one this program found -- `repository_integration_locks.
        repository_id` is a real PRIMARY KEY, this class's own atomic
        single-writer lock for 'an integration is in progress for this
        repository right now' (_lock()/_unlock(), see integrate_task()'s
        own try/finally). A Python `finally` block only protects against
        an in-process exception -- it does NOT run if the process itself
        is killed/crashes/restarts mid-integration, so a hard kill
        between `_lock()` succeeding and the matching `_unlock()` left
        this exact repository PERMANENTLY unable to integrate ANYTHING
        ever again (confirmed real: a stale row makes every future
        `_lock()` call for that repository return False forever, and
        integrate_task() then always returns 'LOCKED' -- there is no UI
        action anywhere that clears this table). Every row here is
        always safe to clear at startup, not just ones matching some
        status set -- same reasoning as ExecutionWaveService.reconcile_
        on_startup()'s own task_reservations fix: a lock surviving to
        the next process start is BY DEFINITION stale, nothing
        legitimate holds one across a restart."""
        self.db.execute("DELETE FROM repository_integration_locks")

    # ---- E10.1: consume E9's own readiness, never re-derive it ------------
    def preflight_integration(self, task_id: int) -> dict:
        return self.review_fix_orchestrator.integration_readiness(task_id)

    def _lock(self, repository_id: int, by: str) -> bool:
        try:
            self.db.execute("INSERT INTO repository_integration_locks(repository_id,locked_by) VALUES(?,?)", (repository_id, by))
            return True
        except Exception:
            return False

    def _unlock(self, repository_id: int) -> None:
        self.db.execute("DELETE FROM repository_integration_locks WHERE repository_id=?", (repository_id,))

    # ---- E10.6: real verification of the INTEGRATED target, never just
    # trusting the Task worktree's own earlier PASS ------------------------
    def verify_integrated_state(self, repo_path: str, commit: str) -> dict:
        """Runs the repository's own PROJECT.yaml-declared required CI
        stages (the exact same load_contract() DeploymentService/the
        real merge-gate probe flow already use) against a disposable
        detached probe at the newly-integrated commit -- Task worktree
        PASS never substitutes for this."""
        probe = self.git.create_baseline_probe(repo_path, commit)
        try:
            try:
                contract = load_contract(Path(repo_path))
            except ContractError:
                contract = []
            results = []
            passed = True
            for stage, command, working_dir, timeout in contract:
                cwd = (probe / working_dir).resolve()
                try:
                    r = self.runner(["bash", "-lc", command], cwd, timeout)
                    ok = r.returncode == 0
                    stdout, stderr = (r.stdout or "")[-2000:], (r.stderr or "")[-2000:]
                except subprocess.TimeoutExpired:
                    ok, stdout, stderr = False, "", "timed out"
                results.append({"stage": stage, "ok": ok, "stdout": stdout, "stderr": stderr})
                passed = passed and ok
            return {"passed": passed, "results": results}
        finally:
            self.git.remove_baseline_probe(repo_path, probe)

    # ---- E10.2/E10.3: the real merge ---------------------------------------
    def integrate_task(self, task_id: int) -> dict:
        readiness = self.preflight_integration(task_id)
        if not readiness["ready"]:
            return {"outcome": "BLOCKED", "task_id": task_id, "reasons": readiness["blockers"]}
        governing_id = readiness["governing_task_id"]
        ws = self.worktree_manager.get_task_worktree(governing_id)
        if not ws:
            return {"outcome": "NO_MANAGED_WORKTREE", "task_id": task_id}
        repo_row = self.db.one("SELECT * FROM repositories WHERE id=?", (ws["repository_id"],))

        # E10.4: repository-scoped lock -- established now even though
        # E13 concurrency isn't enabled yet.
        if not self._lock(ws["repository_id"], f"task:{governing_id}"):
            return {"outcome": "LOCKED", "task_id": task_id,
                     "message": "Another integration is already in progress for this repository"}
        try:
            # Re-check CLEAN right before merging (avoid a TOCTOU window
            # between the preflight above and the real merge below).
            integ = self.worktree_manager.check_integration(governing_id)
            if integ["result"] == "BASE_STALE":
                # E10.3 default conservative policy: a stale-but-clean
                # base still requires an explicit re-verify decision,
                # never a silent auto-rebase.
                return {"outcome": "BASE_STALE_REQUIRES_REVERIFY", "task_id": task_id}
            if integ["result"] != "CLEAN":
                return {"outcome": integ["result"], "task_id": task_id, "conflicting_files": integ.get("conflicting_files", [])}

            current_base_head = self.git.head(repo_row["repo_path"], ws["base_branch"])
            probe_path = self.git.create_baseline_probe(repo_row["repo_path"], current_base_head)
            try:
                result = self.git.merge(probe_path, ws["branch"])
                if result.returncode != 0:
                    conflicts = self.git.conflict_files(probe_path)
                    self.git.git(probe_path, "merge", "--abort", check=False)
                    return {"outcome": "CONFLICT", "task_id": task_id, "conflicting_files": conflicts}
                integrated_commit = self.git.head(probe_path)
            finally:
                self.git.remove_baseline_probe(repo_row["repo_path"], probe_path)

            # E10.3: move the real branch ref only -- never touches the
            # canonical checkout's own working tree/index, so an
            # unrelated uncommitted change sitting in that checkout
            # (E8.5.6's own discovery: this is always survivable) is
            # completely unaffected.
            self.git.git(repo_row["repo_path"], "update-ref", f"refs/heads/{ws['base_branch']}", integrated_commit)

            verify = self.verify_integrated_state(repo_row["repo_path"], integrated_commit)

            reviewed_head = self.git.head(ws["worktree_path"])
            self.db.execute(
                "INSERT INTO merge_records(task_id,repository_id,required,integration_branch,merge_status,merged_commit,merged_at) "
                "VALUES(?,?,1,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(task_id,repository_id) DO UPDATE SET merge_status=excluded.merge_status,merged_commit=excluded.merged_commit,merged_at=excluded.merged_at,updated_at=CURRENT_TIMESTAMP",
                (governing_id, ws["repository_id"], ws["branch"], "MERGED" if verify["passed"] else "MERGE_VERIFY_FAILED", integrated_commit))

            change_id = self.db.one("SELECT change_id FROM tasks WHERE id=?", (governing_id,))["change_id"]
            wp_id = self.work_products.create(
                kind="INTEGRATED_CHANGE", title=f"Integrated: task {governing_id} -> {ws['base_branch']} ({integrated_commit[:8]})",
                change_id=change_id, task_id=governing_id, status="APPROVED" if verify["passed"] else "REJECTED",
                content_ref=integrated_commit,
                content_metadata={"repository_id": ws["repository_id"], "worktree_path": ws["worktree_path"],
                                    "task_branch": ws["branch"], "base_commit": ws["base_commit"],
                                    "reviewed_head": reviewed_head, "integrated_commit": integrated_commit,
                                    "target_branch": ws["base_branch"], "strategy": "no-ff-probe-then-update-ref",
                                    "merge_result": "CLEAN", "integration_verification": verify})
            self.db.event("task", governing_id, "TASK_INTEGRATED" if verify["passed"] else "INTEGRATION_VERIFY_FAILED",
                           f"commit={integrated_commit} work_product={wp_id}")
            if not verify["passed"]:
                return {"outcome": "INTEGRATION_VERIFY_FAILED", "task_id": task_id, "integrated_commit": integrated_commit,
                         "work_product_id": wp_id, "verification": verify}
            return {"outcome": "INTEGRATED", "task_id": task_id, "integrated_commit": integrated_commit,
                     "work_product_id": wp_id, "target_branch": ws["base_branch"], "repository_id": ws["repository_id"]}
        finally:
            self._unlock(ws["repository_id"])
