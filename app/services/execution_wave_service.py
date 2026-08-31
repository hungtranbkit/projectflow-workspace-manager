from __future__ import annotations
import json

"""Phase E13: Parallel Multi-Agent Execution & Integration Waves --
ExecutionWave (E13.6/E13.7): a runtime scheduling interpretation of the
EXISTING Task DAG (plans/plan_items/task_dependencies, AutonomousExecutionService.
list_auto_ready_tasks()) -- never a second Plan, never a second
dependency/readiness engine.

Backward compatibility (E13.8/E13.9), the single most important
property of this whole phase: engineering.parallel_execution.enabled
defaults to False. When absent or False, plan_execution_wave() selects
AT MOST ONE Task -- byte-for-byte the same outcome AutonomousExecutionService.
run_change() already produces -- so every existing Project with no new
config keeps behaving exactly as before. Parallel selection (more than
one Task per wave) only ever happens when a project explicitly opts in.

Capacity (E13.10/E13.11): effective capacity = min(project capacity
[the EXISTING autonomous_execution.max_concurrent_builders, reused as
the one source of concurrency truth -- E13.8's own instruction],
provider capacity [engineering.providers.<name>.max_concurrent_sessions,
counted system-wide across every Change/project -- this also realizes
"global capacity" (E13.10/E13.48): a Builder running under another
Change already consumes real provider capacity, so it is never invisible
to a new wave's own selection]). No distributed/cluster scheduling
(E13.10's own explicit instruction).

Reservation (E13.14/15): task_reservations.task_id is its OWN PRIMARY
KEY -- a second concurrent scheduler attempting to reserve the same
Task fails on SQLite's own constraint, atomically, with no separate
lock table needed. Reservations are short-lived: released the moment a
launch succeeds (the real AgentSession becomes the ongoing truth from
then on) or fails (so a failed launch never permanently blocks a Task).

Integration stays fully serialized (E13.31/32/33) by composition: each
sibling is integrated one at a time through IntegrationService.
integrate_task() UNCHANGED -- because check_integration()/
integration_readiness() are always LIVE, non-cached computations
against the CURRENT canonical head, calling integrate_task() again for
a later sibling already re-derives against whatever the earlier
sibling's integration just changed. No new merge/conflict engine."""

from app.services.task_decision_service import LIVE_SESSION_STATUSES
from app.services.autonomous_execution_service import AUTO_ELIGIBLE_TASK_TYPES

WAVE_STATUSES = ("PLANNED", "RUNNING", "PARTIAL", "COMPLETE", "BLOCKED", "FAILED")
_DEFAULT_PARALLEL_POLICY = {"enabled": False, "repository_serial": False, "parallel_test_safe": None}


class ExecutionWaveError(ValueError):
    pass


class ExecutionWaveService:
    def __init__(self, db, changes, autonomous_execution_service, parallel_safety_service,
                 integration_service, git, project_policy_resolver=None):
        self.db = db
        self.changes = changes
        self.autonomous_execution_service = autonomous_execution_service
        self.parallel_safety_service = parallel_safety_service
        self.integration_service = integration_service
        self.git = git
        self.project_policy_resolver = project_policy_resolver

    # ---- E13.8/E13.9: policy ----------------------------------------------
    def get_parallel_policy(self, change: dict | None) -> dict:
        policy = dict(_DEFAULT_PARALLEL_POLICY)
        if not change or not self.project_policy_resolver:
            return policy
        project_policy = self.project_policy_resolver(change) or {}
        block = project_policy.get("parallel_execution") or {}
        for k in _DEFAULT_PARALLEL_POLICY:
            if k in block:
                policy[k] = block[k]
        return policy

    def _provider_capacity_policy(self, change: dict | None) -> dict[str, int]:
        if not change or not self.project_policy_resolver:
            return {}
        project_policy = self.project_policy_resolver(change) or {}
        providers = project_policy.get("providers") or {}
        out = {}
        for name, cfg in providers.items():
            if isinstance(cfg, dict) and isinstance(cfg.get("max_concurrent_sessions"), int):
                out[name] = cfg["max_concurrent_sessions"]
        return out

    # ---- E13.11/E13.48: capacity reads -- system-wide (never scoped to
    # only the candidate Change), so a Builder running under another
    # Change/project is never invisible to a new wave's own selection ----
    def _change_live_count(self, change_id: int) -> int:
        row = self.db.one(
            "SELECT COUNT(*) c FROM agent_sessions WHERE task_id IN (SELECT id FROM tasks WHERE change_id=?) AND status IN (%s)" %
            ",".join("?" * len(LIVE_SESSION_STATUSES)), (change_id, *LIVE_SESSION_STATUSES))
        return row["c"] if row else 0

    def _provider_live_count(self, provider: str) -> int:
        row = self.db.one(
            "SELECT COUNT(*) c FROM agent_sessions WHERE agent=? AND status IN (%s)" %
            ",".join("?" * len(LIVE_SESSION_STATUSES)), (provider, *LIVE_SESSION_STATUSES))
        return row["c"] if row else 0

    def _repository_live_count(self, repository_id: int) -> int:
        row = self.db.one(
            "SELECT COUNT(*) c FROM agent_sessions s JOIN agent_workspaces w ON w.id=s.workspace_id "
            "WHERE w.repository_id=? AND s.status IN (%s)" %
            ",".join("?" * len(LIVE_SESSION_STATUSES)), (repository_id, *LIVE_SESSION_STATUSES))
        return row["c"] if row else 0

    # ---- E13.35: plan_execution_wave -- PURE, no DB writes, safe to
    # call repeatedly/at any time (including with parallel disabled, or
    # from a disposable/inspection-only caller -- E13.56) --------------
    def plan_execution_wave(self, change_id: int) -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise ExecutionWaveError("Change not found")
        autonomous_policy = self.autonomous_execution_service.get_policy(change)
        parallel_policy = self.get_parallel_policy(change)
        result = {"change_id": change_id, "parallel_enabled": bool(parallel_policy["enabled"]), "selected": [], "deferred": []}
        if not autonomous_policy["enabled"]:
            result["deferred"].append({"task_id": None, "reason": "DISABLED"})
            return result

        all_tasks = [t for t in self.changes.list_tasks_for_change(change_id)
                     if (t.get("task_type") or "").strip().upper() in AUTO_ELIGIBLE_TASK_TYPES]
        readiness_by_id = {t["id"]: self.autonomous_execution_service.evaluate_task(t["id"]) for t in all_tasks}
        ready = self.autonomous_execution_service.list_auto_ready_tasks(change_id)  # dependency-depth ordered
        ready_by_id = {r["task_id"]: r for r in ready}

        for t in all_tasks:
            r = readiness_by_id[t["id"]]
            if r["readiness"] != "AUTO_READY":
                result["deferred"].append({"task_id": t["id"], "reason": r["readiness"], "detail": r.get("reason")})

        if not ready:
            return result

        # E13.8: parallel disabled -> exactly the legacy single-Task cap,
        # byte-identical selection outcome to run_change()'s own choice
        # (first Task in the SAME dependency-depth order).
        effective_project_cap = autonomous_policy["max_concurrent_builders"] if parallel_policy["enabled"] else 1
        already_running = self._change_live_count(change_id)
        project_capacity_left = max(0, effective_project_cap - already_running)
        if project_capacity_left <= 0:
            for r in ready:
                result["deferred"].append({"task_id": r["task_id"], "reason": "PROJECT_CAPACITY_REACHED"})
            return result

        # E13.12/E13.21: repository_serial (or an already-serial repo
        # with an active session outside this wave) removes every
        # candidate in that repository outright, before pairwise safety.
        candidate_ids = []
        for r in ready:
            repo_id = r["repository_id"]
            if parallel_policy.get("repository_serial") and self._repository_live_count(repo_id) > 0:
                result["deferred"].append({"task_id": r["task_id"], "reason": "REPOSITORY_SERIAL_EXECUTION"})
                continue
            candidate_ids.append(r["task_id"])

        safety = self.parallel_safety_service.evaluate_candidate_set(candidate_ids)
        for d in safety["deferred"]:
            result["deferred"].append({"task_id": d["task_id"], "reason": d["reason"], "detail": d["detail"]})

        provider_caps = self._provider_capacity_policy(change)
        provider_running_cache: dict[str, int] = {}
        selected_out = []
        used_per_provider: dict[str, int] = {}
        for tid in safety["selected"]:
            if len(selected_out) >= project_capacity_left:
                result["deferred"].append({"task_id": tid, "reason": "PROJECT_CAPACITY_REACHED"})
                continue
            provider = ready_by_id[tid]["provider"]
            cap = provider_caps.get(provider)
            if cap is not None:
                if provider not in provider_running_cache:
                    provider_running_cache[provider] = self._provider_live_count(provider)
                total_used = provider_running_cache[provider] + used_per_provider.get(provider, 0)
                if total_used >= cap:
                    result["deferred"].append({"task_id": tid, "reason": "PROVIDER_CAPACITY_REACHED", "provider": provider})
                    continue
            used_per_provider[provider] = used_per_provider.get(provider, 0) + 1
            selected_out.append({"task_id": tid, "provider": provider, "repository_id": ready_by_id[tid]["repository_id"],
                                   "safety_result": "PARALLEL_SAFE"})
        result["selected"] = selected_out
        return result

    # ---- E13.36: run_execution_wave -- reserve + launch, returns
    # immediately (E13.17: never waits for a Builder to finish) --------
    def run_execution_wave(self, change_id: int) -> dict:
        plan = self.plan_execution_wave(change_id)
        if not plan["selected"]:
            return {"outcome": "NO_TASKS_SELECTED", "change_id": change_id, "deferred": plan["deferred"]}

        change = self.changes.get(change_id)
        repo_row = self.db.one("SELECT * FROM repositories WHERE id=?", (plan["selected"][0]["repository_id"],))
        wave_base_commit = None
        try:
            if repo_row:
                wave_base_commit = self.git.head(repo_row["repo_path"])
        except Exception:
            wave_base_commit = None

        # E13.15/49: wave_number itself is a read-then-write, so two
        # concurrent run_execution_wave() calls for the SAME Change can
        # both compute the same next_num -- retried on the resulting
        # UNIQUE(change_id,wave_number) collision (task_reservations'
        # own PK is still the real double-LAUNCH guard below; this only
        # protects the audit row's own numbering from colliding).
        wave_id = None
        for _attempt in range(5):
            next_num = self.db.one(
                "SELECT COALESCE(MAX(wave_number),0)+1 n FROM execution_waves WHERE change_id=?", (change_id,))["n"]
            try:
                wave_id = self.db.execute(
                    "INSERT INTO execution_waves(change_id,wave_number,status,base_commit,started_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)",
                    (change_id, next_num, "RUNNING", wave_base_commit))
                break
            except Exception:
                continue
        if wave_id is None:
            return {"outcome": "NO_TASKS_SELECTED", "change_id": change_id, "deferred": plan["deferred"],
                     "message": "Could not allocate a wave number (concurrent scheduler contention)"}
        self.db.event("change", change_id, "EXECUTION_WAVE_PLANNED", f"wave={wave_id} selected={len(plan['selected'])}")
        self.db.event("change", change_id, "EXECUTION_WAVE_STARTED", f"wave={wave_id}")
        for d in plan["deferred"]:
            if d["reason"] == "PARALLEL_UNKNOWN":
                self.db.event("task", d["task_id"], "PARALLEL_UNKNOWN_SERIALIZED", json.dumps(d.get("detail") or {}))
            elif d["reason"] == "PARALLEL_CONFLICT":
                self.db.event("task", d["task_id"], "PARALLEL_CONFLICT_DETECTED", json.dumps(d.get("detail") or {}))

        launched = []
        for item in plan["selected"]:
            tid = item["task_id"]
            t = self.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
            task_base_commit = None
            try:
                if repo_row:
                    task_base_commit = self.git.head(repo_row["repo_path"])
            except Exception:
                pass
            wt_id = self.db.execute(
                "INSERT INTO execution_wave_tasks(wave_id,task_id,repository_id,safety_result,provider,"
                "reservation_state,task_base_commit) VALUES(?,?,?,?,?,?,?)",
                (wave_id, tid, item["repository_id"], item["safety_result"], item["provider"], "RESERVED", task_base_commit))
            # E13.14/15: atomic claim -- a concurrent tick() reserving the
            # SAME Task fails here on SQLite's own PRIMARY KEY constraint.
            try:
                self.db.execute("INSERT INTO task_reservations(task_id,wave_id,state) VALUES(?,?,'RESERVED')", (tid, wave_id))
            except Exception:
                self.db.execute("UPDATE execution_wave_tasks SET reservation_state='RELEASED' WHERE id=?", (wt_id,))
                self.db.event("task", tid, "TASK_RESERVATION_RELEASED", "already reserved by another scheduler pass")
                continue
            self.db.event("task", tid, "TASK_RESERVED", f"wave={wave_id}")
            try:
                launch = self.autonomous_execution_service.launch_reserved(tid, item["repository_id"], item["provider"])
            except Exception as exc:
                launch = {"outcome": "EXECUTION_FAILED", "task_id": tid, "message": str(exc)}
            # Reservation only needs to survive the short scheduling
            # window -- the real AgentSession (on success) or the launch
            # failure itself (on failure) is the ongoing truth from here.
            self.db.execute("DELETE FROM task_reservations WHERE task_id=?", (tid,))
            if launch.get("outcome") == "LAUNCHED":
                self.db.execute(
                    "UPDATE execution_wave_tasks SET reservation_state='LAUNCHED',session_id=?,workspace_id=?,"
                    "result_metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (launch.get("session_id"), launch.get("workspace_id"), json.dumps(launch), wt_id))
                self.db.event("task", tid, "PARALLEL_BUILDER_LAUNCHED", f"wave={wave_id} session={launch.get('session_id')}")
                launched.append({**launch, "wave_task_id": wt_id})
            else:
                self.db.execute(
                    "UPDATE execution_wave_tasks SET reservation_state='FAILED',result_metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(launch), wt_id))
                self.db.event("task", tid, "TASK_RESERVATION_RELEASED", f"launch failed: {launch.get('message','')}"[:500])

        status = "RUNNING" if launched else "FAILED"
        if launched and len(launched) < len(plan["selected"]):
            status = "PARTIAL"
        self.db.execute("UPDATE execution_waves SET status=? WHERE id=?", (status, wave_id))
        return {"outcome": "LAUNCHED" if launched else "NO_TASKS_LAUNCHED", "change_id": change_id, "wave_id": wave_id,
                "launched": launched, "deferred": plan["deferred"]}

    # ---- reads --------------------------------------------------------
    def get_wave(self, wave_id: int) -> dict | None:
        wave = self.db.one("SELECT * FROM execution_waves WHERE id=?", (wave_id,))
        if not wave:
            return None
        tasks = self.db.all(
            "SELECT ewt.*, t.title AS task_title, t.status AS task_status FROM execution_wave_tasks ewt "
            "JOIN tasks t ON t.id=ewt.task_id WHERE ewt.wave_id=? ORDER BY ewt.id", (wave_id,))
        return {**wave, "tasks": tasks}

    def current_wave_for_change(self, change_id: int) -> dict | None:
        row = self.db.one("SELECT id FROM execution_waves WHERE change_id=? ORDER BY wave_number DESC LIMIT 1", (change_id,))
        return self.get_wave(row["id"]) if row else None

    def list_waves_for_change(self, change_id: int) -> list[dict]:
        rows = self.db.all("SELECT id FROM execution_waves WHERE change_id=? ORDER BY wave_number DESC", (change_id,))
        return [self.get_wave(r["id"]) for r in rows]

    # ---- E13.27/28/46: actual scope, post-run -----------------------------
    def actual_scope_for_task(self, task_id: int) -> list[str]:
        """Real changed-file set (git diff against the Task's own
        immutable base_commit) -- the SAME primitive AutonomousExecutionService.
        check_scope_violation() already computes, reused here for a
        sibling-to-sibling comparison instead of a scope_hints check."""
        ws = self.db.one("SELECT * FROM agent_workspaces WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))
        if not ws:
            return []
        try:
            return self.git.changed_files(ws["worktree_path"], ws["base_commit"])
        except Exception:
            return []

    def compare_actual_scope(self, task_a_id: int, task_b_id: int) -> dict:
        a, b = set(self.actual_scope_for_task(task_a_id)), set(self.actual_scope_for_task(task_b_id))
        overlap = sorted(a & b)
        if not overlap:
            return {"result": "ACTUAL_SCOPE_DISJOINT", "overlap_files": []}
        from app.services.parallel_safety_service import _MIGRATION_PATTERNS, _MANIFEST_FILES, _GLOBAL_CONFIG_FILES, _matches_any, _basename_in
        risky = any(_matches_any(f, _MIGRATION_PATTERNS) or _basename_in(f, _MANIFEST_FILES) or _basename_in(f, _GLOBAL_CONFIG_FILES)
                    for f in overlap)
        return {"result": "ACTUAL_SCOPE_CONFLICT_RISK" if risky else "ACTUAL_SCOPE_OVERLAP", "overlap_files": overlap}

    def recheck_actual_scope(self, wave_id: int) -> list[dict]:
        """E13.27/28: predicted-SAFE never trusted blindly -- compares
        every sibling pair's REAL changed files after Builders finish.
        A predicted-SAFE pair with real overlap is recorded as
        PARALLEL_PREDICTION_MISS (audit/future-learning evidence only --
        never auto-merged, never changes the wave's own outcome)."""
        wave = self.get_wave(wave_id)
        if not wave:
            raise ExecutionWaveError("Execution wave not found")
        launched = [t for t in wave["tasks"] if t["reservation_state"] == "LAUNCHED"]
        findings = []
        for i in range(len(launched)):
            for j in range(i + 1, len(launched)):
                a, b = launched[i], launched[j]
                cmp = self.compare_actual_scope(a["task_id"], b["task_id"])
                finding = {"task_a": a["task_id"], "task_b": b["task_id"], **cmp}
                if cmp["result"] != "ACTUAL_SCOPE_DISJOINT":
                    self.db.event("change", wave["change_id"], "ACTUAL_SCOPE_OVERLAP_DETECTED", json.dumps(finding))
                    if a["safety_result"] == "PARALLEL_SAFE" and b["safety_result"] == "PARALLEL_SAFE":
                        self.db.event("change", wave["change_id"], "PARALLEL_PREDICTION_MISS", json.dumps(finding))
                        finding["prediction_miss"] = True
                findings.append(finding)
        return findings

    # ---- E13.31/32/33: serialized integration, rechecked per sibling -----
    def integrate_wave(self, wave_id: int) -> dict:
        """One sibling at a time, in launch order -- IntegrationService.
        integrate_task() is called UNCHANGED for each; because its own
        preflight/check_integration are always live against the CURRENT
        canonical head, a later sibling's call already re-derives
        against whatever the earlier sibling's own integration just
        changed (E13.32's own 'recheck' requirement -- no new mechanism
        needed for this, only for detecting it happened, below)."""
        wave = self.get_wave(wave_id)
        if not wave:
            raise ExecutionWaveError("Execution wave not found")
        self.recheck_actual_scope(wave_id)
        launched = [t for t in wave["tasks"] if t["reservation_state"] == "LAUNCHED"]
        results = []
        integrated_count = 0
        for idx, t in enumerate(launched):
            # A predicted-CLEAN Task can only genuinely conflict "after a
            # sibling" once at least one earlier sibling has actually
            # integrated -- distinguishes an ordinary first-sibling
            # CONFLICT from one specifically caused by this wave's own
            # earlier merge (E13.33).
            outcome = self.integration_service.integrate_task(t["task_id"])
            if outcome["outcome"] == "INTEGRATED":
                integrated_count += 1
            elif idx > 0 and integrated_count > 0 and outcome["outcome"] in ("CONFLICT", "BLOCKED"):
                outcome = {**outcome, "outcome": "INTEGRATION_CONFLICT_AFTER_SIBLING"}
                self.db.event("task", t["task_id"], "INTEGRATION_CONFLICT_AFTER_SIBLING", json.dumps(outcome)[:1000])
            results.append({"task_id": t["task_id"], **outcome})
            self.db.execute("UPDATE execution_wave_tasks SET result_metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (json.dumps({**json.loads(t["result_metadata"] or "{}"), "integration": outcome}), t["id"]))

        if integrated_count == len(launched) and launched:
            status = "COMPLETE"
        elif integrated_count > 0:
            status = "PARTIAL"
        else:
            status = "BLOCKED"
        self.db.execute("UPDATE execution_waves SET status=?,completed_at=CURRENT_TIMESTAMP WHERE id=?", (status, wave_id))
        self.db.event("change", wave["change_id"], "WAVE_COMPLETE" if status == "COMPLETE" else "WAVE_PARTIAL",
                       f"wave={wave_id} integrated={integrated_count}/{len(launched)}")
        return {"wave_id": wave_id, "status": status, "results": results}
