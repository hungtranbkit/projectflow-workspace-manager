from __future__ import annotations
import json
from pathlib import Path

"""Autonomous Implementation Orchestration (Phase E8): Validated Plan ->
Materialized Task DAG -> Ready Task Detection -> Builder Assignment ->
Builder Agent Session -> Code/Test Execution -> WorkProduct/Evidence
Capture -> Task Completion -> Next Ready Task.

CRITICAL ARCHITECTURAL RULE (E8's own instruction): ORCHESTRATION /
TASK EXECUTION / TASK DECISION / REVIEW / VERIFICATION stay separate.
This module is ORCHESTRATION ONLY:

  - TASK EXECUTION remains exactly what it already was --
    AgentSessionManager + _start_builder_session (app/main.py) is the
    ONE place a real Agent session is ever started, for a manual click
    or an autonomous tick alike. This module never spawns a process,
    never touches a worktree, never runs git itself -- it calls the
    SAME closures app/main.py already wires for the manual "Start
    Builder" button (add_task_workspace/start_builder_session, injected
    at construction time), so there is exactly one Supervisor, not two.

  - TASK DECISION remains exactly TaskDecisionService/
    TaskDependencyService.readiness() -- evaluate_task() below only
    ADDS autonomous-specific preconditions (policy, staleness, role
    validation, capacity, repeated-failure, dirty worktree) on top of
    that existing truth; it never re-derives Task status/stage itself.

  - REVIEW/VERIFICATION are untouched. A Builder finishing successfully
    only ever means BUILDER_FINISHED, never TASK_COMPLETE -- the exact
    same REVIEW_REQUIRED/verification-required boundary TaskDecisionService
    already enforces for a manually-started Builder applies identically
    here (E8.13). E9 will add autonomous review; E8 deliberately stops
    at this boundary.

No new database table exists for this phase. Orchestration audit events
reuse the existing `workspace_events` table (db.event()) -- the SAME
mechanism every other phase's Start/Review/Deploy action already writes
to -- rather than a new event-log table (E8.21)."""

AUTO_ELIGIBLE_TASK_TYPES = ("IMPLEMENTATION", "TEST_IMPLEMENTATION", "FIX")

READINESS_STATES = (
    "AUTO_READY", "WAITING_DEPENDENCY", "WAITING_SPEC", "WAITING_DESIGN", "WAITING_TEST_DESIGN",
    "WAITING_HUMAN", "STALE_PLAN", "ROLE_ASSIGNMENT_INVALID", "SCOPE_BLOCKED", "ALREADY_RUNNING",
    "REPEATED_FAILURE_STOP", "NOT_AUTONOMOUS_TASK", "COMPLETE",
    # Named explicitly by E8.19 (not in E8.4's own list, but given its
    # own real state name there). E8.5.6 relaxed evaluate_task() to no
    # longer EMIT this for a dirty CANONICAL checkout (worktree isolation
    # means it never actually endangers anything -- see evaluate_task()'s
    # own comment) -- kept in this tuple for the readiness-state
    # vocabulary's own documentation/history and in case a future,
    # genuinely-unsafe worktree-creation failure wants a state of its own
    # again; not currently returned by any code path.
    "DIRTY_WORKTREE_REQUIRES_ATTENTION",
)

TICK_OUTCOMES = (
    "LAUNCHED", "NO_READY_TASK", "WAITING_HUMAN", "WAITING_DEPENDENCY", "WAITING_REVIEW",
    "BLOCKED", "EXECUTION_FAILED", "REPEATED_FAILURE_STOP", "COMPLETE",
    # Two additional, honestly-necessary outcomes E8.15's own list
    # doesn't name but E8.1/E8.5 require distinguishing: a Change whose
    # policy has autonomous_execution disabled, and one already at its
    # configured max_concurrent_builders.
    "DISABLED", "AT_CAPACITY",
)

# Consecutive AUTO_EXECUTION_FAILED audit events for the same Task
# (with no AUTO_BUILDER_LAUNCHED success in between) before autonomous
# execution stops retrying it -- never an infinite retry loop (E8.15).
REPEATED_FAILURE_THRESHOLD = 3

_DEFAULT_POLICY = {"enabled": False, "max_concurrent_builders": 1, "auto_start_ready_tasks": True}


class AutonomousExecutionError(ValueError):
    pass


def _item(readiness: str, reason: str, **extra) -> dict:
    return {"readiness": readiness, "reason": reason, **extra}


class TaskExecutionContextBuilder:
    """E8.8/E8.9: the bounded engineering-context section of a Builder
    Prompt -- rendered as plain text lines, appended into the EXISTING
    render_agent_prompt() pipeline (app/main.py) right after its own
    _spec_context_section(t), never a second prompt mechanism. Bounded
    to what THIS Task's own governing PlanItem/requirement ids actually
    touch -- never the whole project's architecture/design/test-design
    state."""

    def __init__(self, db, work_products, trace, specs_root, test_case_specs, executable_mapping):
        self.db = db
        self.work_products = work_products
        self.trace = trace
        self.specs_root = specs_root
        self.test_case_specs = test_case_specs
        self.executable_mapping = executable_mapping

    def _plan_item_for_task(self, task_id: int) -> dict | None:
        return self.db.one("SELECT * FROM plan_items WHERE materialized_task_id=?", (task_id,))

    def _governing_feature_ids(self, change_id: int) -> list[str]:
        return [l["target_id"] for l in self.trace.for_source("change", change_id) if l["target_type"] == "spec_feature"]

    def render_lines(self, task_id: int, change_id: int | None) -> list[str]:
        if not change_id:
            return []
        plan_item = self._plan_item_for_task(task_id)
        req_ids = set(json.loads(plan_item["requirement_ids"] or "[]")) if plan_item else set()
        lines: list[str] = []

        # ---- Architecture constraints (E8.8) --------------------------
        arch_rows = [wp for wp in self.work_products.list_for_change(change_id)
                     if wp["kind"] == "ARCHITECTURE_ANALYSIS" and wp["status"] == "APPROVED"]
        if arch_rows:
            content = json.loads(arch_rows[-1]["content_metadata"] or "{}")
            lines += ["## ARCHITECTURE CONSTRAINTS", f"Classification: {content.get('classification', 'UNKNOWN')}"]
            if content.get("existing_boundaries"):
                lines += ["Existing boundaries to respect:", *[f"- {x}" for x in content["existing_boundaries"]]]
            if content.get("proposed_boundary_changes"):
                lines += ["Boundary changes this Change makes:", *[f"- {x}" for x in content["proposed_boundary_changes"]]]
            adr_wps = [self.work_products.get(int(l["target_id"])) for l in self.trace.for_source("work_product", arch_rows[-1]["id"])
                       if l["target_type"] == "work_product" and l["relation"] == "PRODUCED"]
            for adr in adr_wps:
                if adr and adr["status"] == "APPROVED":
                    ac = json.loads(adr["content_metadata"] or "{}")
                    lines.append(f"ADR: {ac.get('title', '')} -- {ac.get('decision', '')}")
            lines.append("")

        # ---- Technical Design slice (E8.8) -----------------------------
        design_rows = [wp for wp in self.work_products.list_for_change(change_id)
                       if wp["kind"] == "TECHNICAL_DESIGN" and wp["status"] == "APPROVED"]
        if design_rows:
            d = json.loads(design_rows[-1]["content_metadata"] or "{}")
            lines += ["## TECHNICAL DESIGN", d.get("design_summary", "")]
            for label, key in (("Components to change", "components_to_change"), ("Interfaces", "interfaces"),
                                ("API contracts", "api_contracts"), ("Data model changes", "data_model_changes"),
                                ("Failure modes to handle", "failure_modes"), ("Error handling", "error_handling"),
                                ("Migration plan", "migration_plan"), ("Backward compatibility", "backward_compatibility")):
                val = d.get(key)
                if not val:
                    continue
                if isinstance(val, list):
                    lines += [f"{label}:", *[f"- {x}" for x in val]]
                else:
                    lines.append(f"{label}: {val}")
            lines.append("")

        # ---- UI/UX Design slice, only if applicable (E8.8) -------------
        ui_rows = [wp for wp in self.work_products.list_for_change(change_id)
                   if wp["kind"] == "UI_UX_DESIGN" and wp["status"] == "APPROVED"]
        if ui_rows:
            u = json.loads(ui_rows[-1]["content_metadata"] or "{}")
            lines.append("## UI/UX DESIGN")
            if u.get("user_flows"):
                lines += ["User flows:", *[f"- {x}" for x in u["user_flows"]]]
            if u.get("interaction_rules"):
                lines += ["Interaction rules:", *[f"- {x}" for x in u["interaction_rules"]]]
            if u.get("acceptance_mapping"):
                lines += ["Acceptance mapping:", *[f"- {m.get('acceptance_id')}: {m.get('covered_by')}" for m in u["acceptance_mapping"]]]
            lines.append("")

        # ---- Test contract (E8.9): TestCaseSpecs + executable status ---
        cases = self.test_case_specs.list_for_change(change_id)
        relevant = [c for c in cases if not req_ids or (set(json.loads(c["requirement_ids"] or "[]")) & req_ids)]
        if relevant:
            lines.append("## TEST CONTRACT (governing TestCaseSpecs -- see E7 Test Design)")
            for c in relevant:
                mapping = self.executable_mapping.get(c["id"])
                status = mapping["implementation_status"] if mapping else "UNIMPLEMENTED"
                where = f" ({mapping['repository_path']}::{mapping['test_symbol']})" if mapping and mapping.get("repository_path") else ""
                lines.append(f"- [{c['item_key']}] {c['title']} ({c['test_level']}/{c['test_type']}) -- {status}{where}: {c['expected_results']}")
            lines += ["",
                      "TEST CONTRACT RULES (mandatory):",
                      "- Implement/pass the TestCaseSpecs above that are UNIMPLEMENTED and relevant to this Task's scope.",
                      "- Do not edit the approved Spec, acceptance criteria, or invariants to make a test pass.",
                      "- Do not weaken, delete, or silently remap a TestCaseSpec's requirement/acceptance/invariant ids.",
                      "- Do not mark a test result PASS yourself -- report real test output; ProjectFlow records the evidence.",
                      "- If a required test genuinely cannot pass without changing WHAT was approved, report SPEC_DRIFT/a human decision instead of removing or weakening it.",
                      ""]

        # ---- Scope + predecessor outputs (E8.8/E8.18) -------------------
        if plan_item:
            scope = json.loads(plan_item["scope_hints"] or "[]")
            if scope:
                lines += ["## SCOPE", "Stay within these paths unless explicitly told otherwise:", *[f"- {x}" for x in scope], ""]
            deps = json.loads(plan_item["depends_on_keys"] or "[]")
            if deps:
                dep_rows = self.db.all(
                    "SELECT * FROM plan_items WHERE plan_id=? AND item_key IN (%s)" % ",".join("?" * len(deps)),
                    (plan_item["plan_id"], *deps))
                predecessor_outputs = []
                for dep in dep_rows:
                    if dep["materialized_task_id"]:
                        for wp in self.work_products.list_for_task(dep["materialized_task_id"]):
                            predecessor_outputs.append(f"- {dep['item_key']} ({dep['title']}) produced {wp['kind']}: {wp['title']}")
                if predecessor_outputs:
                    lines += ["## PREDECESSOR WORK", *predecessor_outputs, ""]

        return lines


class AutonomousExecutionService:
    def __init__(self, db, changes, work_products, decision, task_dependencies, workflow_service,
                 human_decisions, spec_gate, roles_catalog, planner_service, git,
                 add_task_workspace, start_builder_session, project_policy_resolver, settings,
                 test_case_specs=None, worktree_manager=None):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.decision = decision
        self.task_dependencies = task_dependencies
        self.workflow_service = workflow_service
        self.human_decisions = human_decisions
        self.spec_gate = spec_gate
        self.roles_catalog = roles_catalog
        self.planner_service = planner_service
        self.git = git
        # E8.12: optional -- only needed to resolve governing TestCaseSpec
        # ids for record_code_change_work_product() below. None is a
        # valid, fully backward-compatible construction (the WorkProduct
        # is still captured, just without test_case_spec_ids).
        self.test_case_specs = test_case_specs
        # E8.5: optional -- only needed for the base-staleness note
        # (informational, see E8.5.6/E8.5.18 below) and the canonical-
        # status snapshot taken right before a Builder launches (E8.5.5).
        # None is a fully backward-compatible construction (every E8
        # unit test built before E8.5 still passes unchanged).
        self.worktree_manager = worktree_manager
        # E8.10: the ONLY two callables this service ever invokes to
        # actually touch a repository/start a process -- both are the
        # EXACT closures app/main.py already uses for the manual "Start
        # Builder" button (add_task_workspace/_start_builder_session).
        # No second Supervisor, no second shell-execution mechanism.
        self._add_task_workspace = add_task_workspace
        self._start_builder_session = start_builder_session
        self._project_policy_resolver = project_policy_resolver
        self.settings = settings

    # ---- policy (E8.1) --------------------------------------------------
    def get_policy(self, change: dict) -> dict:
        policy = _DEFAULT_POLICY.copy()
        if not self._project_policy_resolver:
            return policy
        project_policy = self._project_policy_resolver(change) or {}
        block = project_policy.get("autonomous_execution") or {}
        policy.update({k: v for k, v in block.items() if k in _DEFAULT_POLICY})
        return policy

    # ---- helpers ----------------------------------------------------------
    def _task(self, task_id: int) -> dict:
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise AutonomousExecutionError("Task not found")
        return row

    def _repo_for_change(self, change: dict) -> dict | None:
        if not change.get("project_id"):
            return None
        return self.db.one("SELECT * FROM repositories WHERE id=?", (change["project_id"],))

    def _live_session_for_task(self, task_id: int) -> dict | None:
        from app.services.task_decision_service import LIVE_SESSION_STATUSES
        return self.db.one(
            "SELECT * FROM agent_sessions WHERE task_id=? AND status IN (%s) ORDER BY id DESC LIMIT 1" %
            ",".join("?" * len(LIVE_SESSION_STATUSES)), (task_id, *LIVE_SESSION_STATUSES))

    def _live_builder_count(self, change_id: int) -> int:
        from app.services.task_decision_service import LIVE_SESSION_STATUSES
        row = self.db.one(
            "SELECT COUNT(*) c FROM agent_sessions WHERE task_id IN (SELECT id FROM tasks WHERE change_id=?) AND status IN (%s)" %
            ",".join("?" * len(LIVE_SESSION_STATUSES)), (change_id, *LIVE_SESSION_STATUSES))
        return row["c"] if row else 0

    def _repeated_failures(self, task_id: int) -> int:
        """Consecutive AUTO_EXECUTION_FAILED audit events for this Task,
        counting back from the most recent event until an
        AUTO_BUILDER_LAUNCHED (a real success) is hit -- reuses the
        existing workspace_events audit trail, never a new failure-count
        column/table (E8.15/E8.21)."""
        rows = self.db.all(
            "SELECT action FROM workspace_events WHERE entity_type='task' AND entity_id=? "
            "AND action IN ('AUTO_EXECUTION_FAILED','AUTO_BUILDER_LAUNCHED') ORDER BY id DESC LIMIT ?",
            (task_id, REPEATED_FAILURE_THRESHOLD))
        count = 0
        for r in rows:
            if r["action"] == "AUTO_EXECUTION_FAILED":
                count += 1
            else:
                break
        return count

    # ---- E8.3/E8.4: deterministic readiness waterfall ---------------------
    def evaluate_task(self, task_id: int) -> dict:
        t = self._task(task_id)
        task_type = (t.get("task_type") or "").strip().upper()
        if task_type not in AUTO_ELIGIBLE_TASK_TYPES:
            return _item("NOT_AUTONOMOUS_TASK", f"task_type '{task_type or 'NONE'}' is not autonomous-eligible", task_id=task_id)
        if not t.get("change_id"):
            return _item("NOT_AUTONOMOUS_TASK", "Task does not belong to a Change", task_id=task_id)
        change_id = t["change_id"]
        change = self.changes.get(change_id)
        if not change:
            return _item("NOT_AUTONOMOUS_TASK", "Change not found", task_id=task_id)

        # P0.7/P0.10 audit finding: tasks.status is a real DB column with
        # only three written values (BACKLOG/ACTIVE/CANCELLED -- see
        # task_decision_service.STATUSES); a CANCELLED Task has no unmet
        # dependencies of its own, so TaskDependencyService.readiness()
        # (which only maps status=='DONE' to readiness='COMPLETE') falls
        # through to readiness='READY' for it, and nothing later in this
        # waterfall re-checks raw task status -- without this explicit
        # check a CANCELLED Task could still be selected and (re)launched
        # by list_auto_ready_tasks()/the wave scheduler, silently reusing
        # its own stale, already-abandoned workspace.
        if t.get("status") == "CANCELLED":
            return _item("NOT_AUTONOMOUS_TASK", "Task is CANCELLED", task_id=task_id)

        readiness = self.task_dependencies.readiness(task_id, self.decision)
        if readiness["readiness"] == "COMPLETE":
            return _item("COMPLETE", "Task is already DONE", task_id=task_id)
        if readiness["readiness"] in ("WAITING_DEPENDENCY", "BLOCKED"):
            return _item("WAITING_DEPENDENCY", "One or more dependency Tasks are not DONE yet",
                          task_id=task_id, unmet_dependencies=readiness.get("unmet_dependencies", []))

        if self.human_decisions.pending_for_change(change_id):
            return _item("WAITING_HUMAN", "This Change has an unresolved human decision", task_id=task_id)

        gate = self.spec_gate.evaluate(t)
        if gate["outcome"] not in ("PASS", "NOT_APPLICABLE"):
            return _item("WAITING_SPEC", f"SpecGate {gate['outcome']}: {gate['reason']}", task_id=task_id)

        run = self.workflow_service.get_workflow(change_id)
        if run:
            state = self.workflow_service.evaluate_workflow(change_id)
            if "DESIGN_READY" in state["unmet_gates"]:
                return _item("WAITING_DESIGN", "DESIGN_READY gate is not met for this Change", task_id=task_id)
            if "TEST_DESIGN_READY" in state["unmet_gates"]:
                return _item("WAITING_TEST_DESIGN", "TEST_DESIGN_READY gate is not met for this Change", task_id=task_id)

        plans = self.planner_service.list_plans(change_id)
        if plans:
            plan_id = plans[-1]["id"]
            for check, reason in ((self.planner_service.check_staleness, "SPEC"),
                                   (self.planner_service.check_design_staleness, "DESIGN"),
                                   (self.planner_service.check_test_design_staleness, "TEST_DESIGN")):
                result = check(plan_id)
                if result["stale"]:
                    return _item("STALE_PLAN", f"REPLAN_REQUIRED: {result['reason']} ({reason})", task_id=task_id, plan_id=plan_id)

        provider = self._default_provider()
        if not provider:
            return _item("ROLE_ASSIGNMENT_INVALID", "No launchable provider is configured for this installation", task_id=task_id)
        repo_row = self._repo_for_change(change)
        policy = None
        if repo_row:
            try:
                from app.services.project_contract import load_engineering_policy
                policy = load_engineering_policy(Path(repo_row["repo_path"]))
            except Exception:
                policy = None
        assignment = self.roles_catalog.validate_assignment(provider, "BUILDER", policy)
        if not assignment["valid"]:
            return _item("ROLE_ASSIGNMENT_INVALID",
                         f"Provider '{provider}' cannot act as BUILDER: missing {assignment['missing_required_capabilities']}",
                         task_id=task_id)

        if self._live_session_for_task(task_id):
            return _item("ALREADY_RUNNING", "A live Agent session already exists for this Task", task_id=task_id)

        if self._repeated_failures(task_id) >= REPEATED_FAILURE_THRESHOLD:
            return _item("REPEATED_FAILURE_STOP",
                         f"{REPEATED_FAILURE_THRESHOLD} consecutive autonomous launch failures -- stopped, needs human attention",
                         task_id=task_id)

        if not repo_row:
            return _item("SCOPE_BLOCKED", "This Change has no registered repository to launch a Builder into", task_id=task_id)

        # E8.5.6: relaxed from a hard block. Discovery for E8.5 confirmed
        # a Builder NEVER operates on the canonical checkout at all --
        # add_task_workspace()/GitWorkspaceService.create_agent() always
        # creates a real, isolated `git worktree add` from the immutable
        # base_commit, for a manual launch and an autonomous one alike,
        # since before E1. `git worktree add` does not read or touch the
        # canonical working tree's uncommitted files, so a dirty
        # CANONICAL checkout -- e.g. this very repo's own known unrelated
        # WIP diff -- never actually endangers anything and is no longer
        # an automatic block. What it DOES mean, honestly: autonomous
        # execution never guesses at including a user's own uncommitted
        # canonical changes in the Task's base -- surfaced as an
        # informational note, never silently ignored.
        extra: dict = {}
        try:
            if self.git.status(repo_row["repo_path"]).strip():
                extra["worktree_isolation_note"] = "BASE_REVISION_EXCLUDES_UNCOMMITTED_CHANGES"
        except Exception:
            pass  # fail-open on a transient git error, same discipline the rest of this module uses

        # E8.5.18: base staleness is informational only here -- never
        # blocking, never auto-rebased. Only meaningful once a worktree
        # already exists for this Task (a brand-new AUTO_READY Task has
        # nothing to compare yet).
        if self.worktree_manager is not None:
            staleness = self.worktree_manager.check_staleness(task_id)
            if staleness.get("stale"):
                extra["worktree_base_stale"] = True

        return _item("AUTO_READY", "Eligible to auto-start", task_id=task_id, repository_id=repo_row["id"], provider=provider, **extra)

    def _default_provider(self) -> str | None:
        from app.services.engineering_catalog import LAUNCHABLE_PROVIDERS
        for p in self.settings.agents:
            if p in LAUNCHABLE_PROVIDERS:
                return p
        return None

    # ---- E8.5/E8.7: ordering ------------------------------------------
    def list_auto_ready_tasks(self, change_id: int) -> list[dict]:
        tasks = [t for t in self.changes.list_tasks_for_change(change_id)
                 if (t.get("task_type") or "").strip().upper() in AUTO_ELIGIBLE_TASK_TYPES]
        ready = []
        for t in tasks:
            result = self.evaluate_task(t["id"])
            if result["readiness"] == "AUTO_READY":
                plan_item = self.db.one("SELECT * FROM plan_items WHERE materialized_task_id=?", (t["id"],))
                depth = len(json.loads(plan_item["depends_on_keys"] or "[]")) if plan_item else 0
                order_key = plan_item["id"] if plan_item else t["id"]
                ready.append({**result, "title": t["title"], "_depth": depth, "_order": order_key})
        # Deterministic priority (E8.7): dependency depth, then Plan item
        # ordering / task id -- never an LLM choosing among an
        # already-deterministic DAG.
        ready.sort(key=lambda r: (r["_depth"], r["_order"]))
        for r in ready:
            r.pop("_depth", None)
            r.pop("_order", None)
        return ready

    # ---- E8.6: single scheduling tick -----------------------------------
    def run_change(self, change_id: int) -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise AutonomousExecutionError("Change not found")
        policy = self.get_policy(change)
        if not policy["enabled"]:
            return {"outcome": "DISABLED", "change_id": change_id}
        if not policy["auto_start_ready_tasks"]:
            return {"outcome": "NO_READY_TASK", "change_id": change_id, "message": "auto_start_ready_tasks is disabled by policy"}
        running = self._live_builder_count(change_id)
        if running >= policy["max_concurrent_builders"]:
            return {"outcome": "AT_CAPACITY", "change_id": change_id, "running": running,
                    "max_concurrent_builders": policy["max_concurrent_builders"]}

        ready = self.list_auto_ready_tasks(change_id)
        if not ready:
            summary = self._blocker_summary(change_id)
            self.db.event("change", change_id, "AUTO_TASK_NOT_READY", json.dumps(summary))
            return {"outcome": summary["dominant_outcome"], "change_id": change_id, "blockers": summary["counts"]}

        task_id = ready[0]["task_id"]
        self.db.event("task", task_id, "AUTO_TASK_SELECTED", f"change={change_id}")
        return {**self._launch(task_id, ready[0]["repository_id"], ready[0]["provider"]), "change_id": change_id}

    def run_next(self, change_id: int) -> dict:
        """Alias kept for the exact method name E8.5 names -- identical
        behavior to run_change(); the distinction is purely naming
        clarity at call sites, never two different code paths."""
        return self.run_change(change_id)

    def tick(self, change_id: int | None = None) -> dict:
        """The scheduler entry point (E8.6). Never a background daemon --
        only ever invoked by an explicit API call or test (E8.6/E8.27).
        At most ONE Builder is actually launched per call, globally,
        even when change_id is omitted and multiple Changes are eligible
        -- every other eligible Change is still evaluated (so the
        caller/UI gets a full picture) but never launched into the same
        tick once capacity is spent."""
        if change_id is not None:
            result = self.run_change(change_id)
            return {"results": [result], "launched": result["outcome"] == "LAUNCHED"}
        changes = sorted(
            [c for c in self.changes.list() if self.workflow_service.get_workflow(c["id"])],
            key=lambda c: c["id"])
        results = []
        launched = False
        for change in changes:
            if launched:
                results.append({"change_id": change["id"], "outcome": "SKIPPED_CAPACITY_REACHED"})
                continue
            result = self.run_change(change["id"])
            results.append(result)
            if result["outcome"] == "LAUNCHED":
                launched = True
        return {"results": results, "launched": launched}

    def _blocker_summary(self, change_id: int) -> dict:
        tasks = [t for t in self.changes.list_tasks_for_change(change_id)
                 if (t.get("task_type") or "").strip().upper() in AUTO_ELIGIBLE_TASK_TYPES]
        if not tasks:
            return {"dominant_outcome": "NO_READY_TASK", "counts": {}}
        outcomes = [self.evaluate_task(t["id"])["readiness"] for t in tasks]
        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o] = counts.get(o, 0) + 1
        if all(o == "COMPLETE" for o in outcomes):
            dominant = "COMPLETE"
        elif "WAITING_HUMAN" in outcomes:
            dominant = "WAITING_HUMAN"
        elif "REPEATED_FAILURE_STOP" in outcomes:
            dominant = "REPEATED_FAILURE_STOP"
        elif any(o == "WAITING_DEPENDENCY" for o in outcomes) and all(o in ("WAITING_DEPENDENCY", "COMPLETE") for o in outcomes):
            dominant = "WAITING_DEPENDENCY"
        elif any(o not in ("AUTO_READY", "COMPLETE", "NOT_AUTONOMOUS_TASK") for o in outcomes):
            dominant = "BLOCKED"
        else:
            dominant = "NO_READY_TASK"
        return {"dominant_outcome": dominant, "counts": counts}

    # ---- E8.23: operator-triggered single-Task run ("Run next ready task") --
    def launch_task_if_ready(self, task_id: int) -> dict:
        """Evaluates THIS specific Task's own readiness and launches it
        only if AUTO_READY -- unlike tick()/run_change() (which pick
        whichever Task is first in DAG order for a whole Change), this
        is for an operator explicitly testing/running one named Task."""
        readiness = self.evaluate_task(task_id)
        if readiness["readiness"] != "AUTO_READY":
            return {"outcome": readiness["readiness"], "task_id": task_id, "message": readiness["reason"]}
        return self._launch(task_id, readiness["repository_id"], readiness["provider"])

    # ---- E13.16: the ONE launch path ExecutionWaveService also uses ------
    def launch_reserved(self, task_id: int, repository_id: int, provider: str) -> dict:
        """Same _launch() every other entry point (manual Start Builder,
        run_change()'s single-task tick, launch_task_if_ready()) already
        uses -- ExecutionWaveService calls this once per Task it has
        already selected+reserved for a wave, never a second Supervisor/
        launch mechanism."""
        return self._launch(task_id, repository_id, provider)

    # ---- E8.10/E8.10-adjacent: launch (Supervisor stays the only path) --
    def _launch(self, task_id: int, repository_id: int, provider: str) -> dict:
        t = self._task(task_id)
        change = self.changes.get(t["change_id"])
        # add_task_workspace() refuses BACKLOG ("Task must be Selected for
        # Development..."), the same rule the manual [Select for
        # Development] button enforces. A materialized Task always starts
        # BACKLOG (planner_service.py), and a human normally clicks that
        # button before assigning a Builder -- autonomous Builder
        # Assignment (E8's own primary goal) performs the identical
        # BACKLOG->ACTIVE transition itself here rather than asking a
        # human to do it first, since evaluate_task() has already
        # confirmed this Task is genuinely AUTO_READY.
        if t["status"] == "BACKLOG":
            self.db.execute("UPDATE tasks SET status='ACTIVE',updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
            self.db.event("task", task_id, "TASK_SELECTED", "auto (E8)")
            t = self._task(task_id)
        repo_row = self.db.one("SELECT * FROM repositories WHERE id=?", (repository_id,))
        existing = self.db.all("SELECT * FROM agent_workspaces WHERE task_id=? AND repository_id=? AND agent=? AND status!='CLOSED'",
                                (task_id, repository_id, provider))
        if existing:
            ws = existing[-1]
        else:
            preferred_role = None
            tt = self.db.one("SELECT preferred_role_key FROM task_types WHERE key=?", (t.get("task_type"),))
            if tt:
                preferred_role = tt["preferred_role_key"]
            result = self._add_task_workspace(task_id, repository_id, provider, preferred_role or "",
                                               repo_row["default_branch"], "")
            if not result["ok"]:
                self.db.event("task", task_id, "AUTO_EXECUTION_BLOCKED", result["error"])
                return {"outcome": "BLOCKED", "task_id": task_id, "message": result["error"]}
            ws = self.db.one("SELECT * FROM agent_workspaces WHERE id=?", (result["workspace_id"],))
        # E8.5.5: snapshot the CANONICAL repository's own status right
        # before the Builder actually starts -- compared again when it
        # submits work (record_code_change_work_product below) so an
        # unexpected change to the canonical checkout during that window
        # is real, checked evidence, never an assumption.
        if self.worktree_manager is not None and repo_row:
            self.worktree_manager.snapshot_canonical_status(ws["id"], repo_row["repo_path"])
        try:
            sid = self._start_builder_session(ws)
        except Exception as exc:
            self.db.event("task", task_id, "AUTO_EXECUTION_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED", "task_id": task_id, "message": str(exc)}
        self.db.event("task", task_id, "AUTO_BUILDER_LAUNCHED", f"session={sid} workspace={ws['id']} provider={provider}")
        return {"outcome": "LAUNCHED", "task_id": task_id, "session_id": sid, "workspace_id": ws["id"]}

    # ---- Scope Guard (E8.18) ----------------------------------------------
    def check_scope_violation(self, w: dict, t: dict | None) -> dict:
        """Compare a Builder Workspace's REAL changed files (git diff
        against its own pinned base_commit -- never the agent's
        self-reported files_changed text, which isn't authoritative)
        against its governing PlanItem's scope_hints. No scope_hints
        declared -> always {'violation': False} (E8.18: scope is only
        enforced when a PlanItem actually declares it). Detection only --
        never reverts/blocks anything itself; 'do not auto-accept' means
        the result must be recorded and visible, not that this method
        takes the submission away from the existing review boundary."""
        if not t:
            return {"violation": False, "reason": "no task"}
        plan_item = self.db.one("SELECT * FROM plan_items WHERE materialized_task_id=?", (t["id"],))
        scope = json.loads(plan_item["scope_hints"] or "[]") if plan_item else []
        if not scope:
            return {"violation": False, "reason": "no scope_hints declared"}
        try:
            changed = self.git.changed_files(w["worktree_path"], w["base_commit"])
        except Exception as exc:
            return {"violation": False, "reason": f"could not determine changed files: {exc}"}

        def _in_scope(path):
            return any(path == s or path.startswith(s.rstrip("/") + "/") for s in scope)

        out_of_scope = [f for f in changed if not _in_scope(f)]
        if out_of_scope:
            return {"violation": True, "reason": "SCOPE_VIOLATION", "out_of_scope_files": out_of_scope,
                    "changed_files": changed, "declared_scope": scope}
        return {"violation": False, "reason": "within declared scope", "changed_files": changed, "declared_scope": scope}

    # ---- WorkProduct/evidence capture (E8.12) ----------------------------
    def record_code_change_work_product(self, w: dict, t: dict | None, head: str, files_changed: str = "") -> int | None:
        """Fires at the SAME moment a Builder Workspace genuinely becomes
        READY_FOR_REVIEW -- app/main.py's _insert_verification_report(),
        shared by the manual Submit-for-Review path and the autonomous
        one alike, since a Task/Change reaching this point means the same
        thing regardless of which path produced it. Captures a durable
        CODE_CHANGE WorkProduct (never a second copy of the diff itself:
        content_ref is the pinned commit sha, the real content stays in
        the repository's own git history) linking Change/Task/
        AgentSession/provider/role/modified files/source revision/
        requirement ids/TestCaseSpec ids/design+spec baseline. Returns
        None (no-op) for a Task with no Change -- CODE_CHANGE capture is
        an engineering-lifecycle concept, not a legacy-Task one."""
        if not t or not t.get("change_id"):
            return None
        change_id = t["change_id"]
        plan_item = self.db.one("SELECT * FROM plan_items WHERE materialized_task_id=?", (t["id"],))
        req_ids = sorted(set(json.loads(plan_item["requirement_ids"] or "[]"))) if plan_item else []
        testcase_ids: list[str] = []
        if self.test_case_specs is not None:
            cases = self.test_case_specs.list_for_change(change_id)
            testcase_ids = [c["item_key"] for c in cases
                             if not req_ids or (set(json.loads(c["requirement_ids"] or "[]")) & set(req_ids))]
        spec_rows = [wp for wp in self.work_products.list_for_change(change_id)
                     if wp["kind"] == "FEATURE_SPEC" and wp["status"] == "APPROVED"]
        design_rows = [wp for wp in self.work_products.list_for_change(change_id)
                       if wp["kind"] == "TECHNICAL_DESIGN" and wp["status"] == "APPROVED"]
        session = self.db.one("SELECT * FROM agent_sessions WHERE workspace_id=? ORDER BY id DESC LIMIT 1", (w["id"],))
        scope_check = self.check_scope_violation(w, t)
        # E8.5.15: worktree/branch/base/head trace -- w already carries
        # every one of these (agent_workspaces IS the managed-worktree
        # identity, see worktree_manager.py's own module docstring); no
        # giant diff blob stored, git history is the authoritative copy.
        try:
            commits = self.git.git(w["worktree_path"], "log", f"{w['base_commit']}..{head}",
                                    "--pretty=format:%h %s", check=False).stdout.splitlines()
        except Exception:
            commits = []
        canonical_check = {"checked": False, "modified": False}
        if self.worktree_manager is not None:
            canonical_check = self.worktree_manager.verify_canonical_untouched(w["id"])
        metadata = {
            "workspace_id": w["id"],
            "repository_id": w.get("repository_id"),
            "worktree_path": w.get("worktree_path"),
            "branch_name": w.get("branch"),
            "base_commit": w.get("base_commit"),
            "head_commit": head,
            "commits": commits,
            "agent_session_id": session["id"] if session else None,
            "provider": w.get("agent"), "role": w.get("role"),
            "modified_files": [f.strip() for f in (files_changed or "").split(",") if f.strip()],
            "source_revision": head,
            "requirement_ids": req_ids,
            "test_case_spec_ids": testcase_ids,
            "design_baseline_work_product_id": design_rows[-1]["id"] if design_rows else None,
            "spec_baseline_work_product_id": spec_rows[-1]["id"] if spec_rows else None,
            "scope_check": scope_check,
            "canonical_repo_check": canonical_check,
        }
        wpid = self.work_products.create(
            kind="CODE_CHANGE", title=f"Code change: {t.get('title') or t.get('slug')} ({head[:8]})",
            change_id=change_id, task_id=t["id"], status="PROPOSED",
            content_ref=head, content_metadata=metadata)
        self.work_products.link_task(t["id"], wpid, "OUTPUT")
        if scope_check.get("violation"):
            self.db.event("task", t["id"], "AUTO_SCOPE_VIOLATION_DETECTED",
                           f"work_product={wpid} out_of_scope={','.join(scope_check.get('out_of_scope_files', []))}")
        return wpid

    # ---- change-level read helper for UI (E8.23) -------------------------
    def status(self, change_id: int) -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise AutonomousExecutionError("Change not found")
        policy = self.get_policy(change)
        ready = self.list_auto_ready_tasks(change_id) if policy["enabled"] else []
        running = self._live_builder_count(change_id)
        last_event = self.db.one(
            "SELECT * FROM workspace_events WHERE entity_type IN ('task','change') AND action LIKE 'AUTO_%' "
            "AND (entity_id IN (SELECT id FROM tasks WHERE change_id=?) OR (entity_type='change' AND entity_id=?)) "
            "ORDER BY id DESC LIMIT 1", (change_id, change_id))
        # E8.5.25: "Managed worktrees / Review pending" counts for the
        # Change Overview card -- purely a read/count over the SAME
        # worktree_manager.lifecycle_status() every other E8.5 surface
        # already uses, never a second computation.
        managed_worktrees = 0
        review_pending = 0
        if self.worktree_manager is not None:
            for t in self.changes.list_tasks_for_change(change_id):
                ws = self.worktree_manager.get_task_worktree(t["id"])
                if ws:
                    managed_worktrees += 1
                    if ws["lifecycle_status"] == "REVIEW_PENDING":
                        review_pending += 1
        return {"change_id": change_id, "policy": policy, "ready_tasks": ready, "running_builders": running,
                "last_orchestration_event": last_event,
                "managed_worktrees": managed_worktrees, "review_pending_worktrees": review_pending}
