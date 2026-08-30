from __future__ import annotations
import hashlib
import json
import shutil
import subprocess

"""Dynamic Planner (Phase E4): the first AI planning layer, added
strictly ABOVE the existing Change/WorkProduct/Role/Workflow model.

CRITICAL ARCHITECTURAL RULE, enforced by construction across four
separate classes in this module:

  PLANNER REASONING     -- PlannerAgentInvoker. One bounded, stateless,
    non-interactive subprocess call (`claude -p ... --json-schema ...`)
    that returns text. It has NO filesystem/tool access (--tools "")
    and is NOT tied to any Builder Workspace/worktree -- it is not a
    coding session, so it never touches _start_builder_session/
    AgentSessionManager/Supervisor at all.

  PLAN ARTIFACT          -- PlanValidator + the plans/plan_items/
    plan_human_decisions tables. The LLM's raw text becomes a plain
    Python dict (json.loads), then a deterministically-validated,
    durably-stored ProjectFlow object. Nothing past this point is ever
    "trust the LLM" -- PlanValidator re-checks every domain rule
    (known TaskType, known Role, cycles, workflow coverage, ...) itself.

  TASK MATERIALIZATION   -- PlannerService.materialize_plan(). Plan ->
    real tasks/task_dependencies rows, inside one DB transaction. Never
    starts an AgentSession. Never calls _start_builder_session.

  TASK EXECUTION          -- untouched. A materialized Task is an
    ordinary BACKLOG Task exactly like one created by hand through
    /api/tasks -- Supervisor/TaskDecisionService/SpecGate see no
    difference and require no changes to run it.

The Planner is not the source of truth (product principle 3/5/6): it
proposes; SpecGate/SpecComplianceVerifier/TaskDecisionService/the
Workflow Engine's own GateRequirements remain the only things that ever
decide whether real work is actually done."""

from app.services.workflow_engine import GATES_BY_STAGE, TASK_TYPES


# ===================================================================
# PLANNER REASONING -- one bounded, stateless, tool-less LLM call
# ===================================================================
class PlannerAgentError(RuntimeError):
    pass


def _default_runner(argv, cwd, timeout):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)


PLAN_JSON_SCHEMA = {
    "type": "object",
    "required": ["summary", "tasks"],
    "properties": {
        "summary": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "human_decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question", "reason"],
                "properties": {
                    "question": {"type": "string"},
                    "reason": {"type": "string"},
                    "spec_change_signal": {"type": "string", "enum": ["AUTO_SPEC_REFINEMENT", "HUMAN_SPEC_CHANGE_REQUIRED", "NONE"]},
                },
            },
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key", "title", "task_type"],
                "properties": {
                    "key": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "task_type": {"type": "string"},
                    "preferred_role": {"type": ["string", "null"]},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "required_inputs": {"type": "array", "items": {"type": "string"}},
                    "expected_outputs": {"type": "array", "items": {"type": "string"}},
                    "requirements": {"type": "array", "items": {"type": "string"}},
                    "scope": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                    "optional": {"type": "boolean"},
                },
            },
        },
    },
}

PLANNER_SYSTEM_PREAMBLE = """You are the PLANNER for ProjectFlow, an autonomous software engineering control plane.

Core principles you must follow exactly:
1. Human owns intent -- the Change below states it; never reinterpret it.
2. Spec owns expected behavior -- respect the linked specs verbatim.
3. You own work decomposition only -- propose Tasks, never execute them.
4. Agents own execution; Verification owns technical truth; you own neither.
5. You may change HOW the work is organized. You must NEVER silently change WHAT was asked.

Decompose the Change described below into a structured Plan of Tasks for the given WorkflowProfile.

Rules (violating these makes your output unusable, not just imperfect):
- Every task's "task_type" must be exactly one of the keys listed under engineering.task_types below -- never invent a new one.
- "preferred_role", if set, must be exactly one of the keys listed under engineering.roles below, or null.
- "depends_on" values must reference another task's own "key" in this same plan -- never a task_type name, never an id.
- Cover every stage listed under workflow.required_stages with at least one task whose task_type maps to it, unless existing_progress already covers that stage (say so in "assumptions" if so) -- never propose skipping a required stage or gate.
- If continuing requires a decision that would change WHAT is being built (business behavior, security boundary, data meaning, a materially different user-facing outcome, or a material change to an already-approved spec) -- add it to "human_decisions" instead of guessing. Do not invent product/business decisions yourself.
- Ordinary implementation choices (library choice with equivalent behavior, internal structure, refactor strategy, test fixtures, file layout, algorithm choice with unchanged behavior) are yours to make -- never escalate those.
"""


class PlannerAgentInvoker:
    """The ONLY place a subprocess for planning is ever spawned. `runner`
    is injectable (same DI pattern as DeploymentService/GitHubMergeService)
    so every test except the one dedicated real-planner test never
    shells out or spends real API budget."""

    def __init__(self, runner=_default_runner, timeout=180, which=shutil.which):
        self.runner = runner
        self.timeout = timeout
        self.which = which

    def invoke(self, provider: str, prompt: str, cwd) -> str:
        """Returns the raw JSON text the model produced (already
        json-schema-validated by the CLI itself where supported) --
        never parsed here. Raises PlannerAgentError for anything that
        means "no usable output came back at all" (not found, timeout,
        non-zero exit, malformed envelope) -- PLANNER_EXECUTION_FAILED,
        never silently treated as an empty plan."""
        provider = (provider or "").strip().lower()
        if provider == "claude":
            executable = self.which("claude")
            if not executable:
                raise PlannerAgentError("claude CLI not found in PATH")
            argv = [executable, "-p", prompt, "--output-format", "json",
                    "--json-schema", json.dumps(PLAN_JSON_SCHEMA), "--tools", "", "--max-turns", "1"]
        else:
            # codex's non-interactive structured-output flags are not
            # wired for planning in this phase -- an honest
            # PLANNER_ASSIGNMENT_INVALID/PLANNER_EXECUTION_FAILED beats
            # a fragile best-effort text scrape.
            raise PlannerAgentError(f"Planner provider '{provider}' has no non-interactive structured-output path wired yet")
        try:
            result = self.runner(argv, str(cwd), self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise PlannerAgentError(f"Planner invocation timed out after {self.timeout}s") from exc
        except Exception as exc:
            raise PlannerAgentError(f"Planner invocation failed: {exc}") from exc
        if result.returncode != 0:
            raise PlannerAgentError(f"Planner exited {result.returncode}: {(result.stderr or '')[:2000]}")
        try:
            envelope = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise PlannerAgentError(f"Planner produced a non-JSON envelope: {exc}") from exc
        if envelope.get("is_error") or envelope.get("subtype") != "success":
            raise PlannerAgentError(f"Planner run reported an error: {envelope.get('result') or envelope}")
        text = envelope.get("result")
        if not isinstance(text, str):
            raise PlannerAgentError("Planner envelope had no text result")
        return text


# ===================================================================
# PLANNER CONTEXT -- bounded, deterministic, no repo dump
# ===================================================================
class PlannerContextBuilder:
    """Collects only relevant project state (E4.4) -- never the whole
    repository. Every field here is read from an EXISTING service; this
    class computes nothing new about spec/review/QA/deploy truth."""

    def __init__(self, db, changes, work_products, decision, workflow_catalog, workflow_service, roles_catalog, specs_root):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.decision = decision
        self.workflow_catalog = workflow_catalog
        self.workflow_service = workflow_service
        self.roles_catalog = roles_catalog
        self.specs_root = specs_root

    def build(self, change_id: int, profile_key: str) -> dict:
        from app.services.spec_registry import SpecRegistry, SpecError

        change = self.changes.get(change_id)
        stage_reqs = self.workflow_catalog.profile_stages(profile_key)
        run = self.workflow_service.get_workflow(change_id)
        current_state = self.workflow_service.evaluate_workflow(change_id) if run else None

        spec_block = {"baseline_sha256": None, "approved_features": []}
        try:
            registry = SpecRegistry(self.specs_root).load()
            spec_block["baseline_sha256"] = registry.baseline_digest()
            spec_block["approved_features"] = [
                {"id": f["id"], "title": f.get("title"), "version": f.get("version"),
                 "requirement_ids": [r["id"] for r in (f.get("requirements") or [])],
                 "acceptance_ids": [a["id"] for a in (f.get("acceptance_criteria") or [])]}
                for f in registry.features.values() if f.get("status") == "approved"
            ]
        except SpecError:
            pass  # a broken spec tree is never fatal to planning -- just an empty spec context

        project = None
        if change and change.get("project_id"):
            r = self.db.one("SELECT id,repo_name,repo_path FROM repositories WHERE id=?", (change["project_id"],))
            if r:
                project = {"repository_id": r["id"], "repo_name": r["repo_name"]}

        existing_tasks = []
        for t in self.changes.list_tasks_for_change(change_id):
            d = self.decision.evaluate(t["id"])
            existing_tasks.append({"id": t["id"], "title": t["title"], "task_type": t.get("task_type"),
                                    "status": d["status"], "stage": d["stage"]})
        existing_work_products = [
            {"kind": wp["kind"], "title": wp["title"], "status": wp["status"]}
            for wp in self.work_products.list_for_change(change_id)
        ]

        return {
            "change": {
                "id": change_id, "title": change.get("title") if change else None,
                "original_intent": change.get("description") if change else None,
                "change_type": change.get("change_type") if change else None,
                "risk_level": change.get("risk_level") if change else None,
                "lifecycle_state": change.get("lifecycle_state") if change else None,
            },
            "workflow": {
                "profile_key": profile_key,
                "required_stages": [s["stage_key"] for s in stage_reqs if s["requirement"] in ("REQUIRED", "REQUIRED_IF")],
                "optional_stages": [s["stage_key"] for s in stage_reqs if s["requirement"] == "OPTIONAL"],
                "current_state": {"status": current_state["status"], "current_stage": current_state["current_stage"],
                                   "unmet_gates": current_state["unmet_gates"]} if current_state else None,
            },
            "spec": spec_block,
            "project": project,
            "engineering": {
                "task_types": {k: {"stage": v["stage"], "preferred_role": v["preferred_role"]} for k, v in TASK_TYPES.items()},
                "roles": [r["key"] for r in self.roles_catalog.list_roles()],
            },
            "existing_progress": {"tasks": existing_tasks, "work_products": existing_work_products},
        }

    @staticmethod
    def digest(context: dict) -> str:
        return hashlib.sha256(json.dumps(context, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def render_prompt(context: dict) -> str:
        return PLANNER_SYSTEM_PREAMBLE + "\nContext:\n" + json.dumps(context, indent=2, sort_keys=True)


# ===================================================================
# PLAN ARTIFACT -- deterministic validation, never "trust the LLM"
# ===================================================================
class PlanValidator:
    def __init__(self, workflow_catalog, roles_catalog):
        self.workflow_catalog = workflow_catalog
        self.roles_catalog = roles_catalog

    def _cycle_free(self, tasks_by_key: dict) -> list[str]:
        errors = []
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {k: WHITE for k in tasks_by_key}

        def visit(key, path):
            if color.get(key) == BLACK:
                return
            if color.get(key) == GRAY:
                errors.append(f"Dependency cycle detected involving: {' -> '.join(path + [key])}")
                return
            color[key] = GRAY
            for dep in tasks_by_key.get(key, {}).get("depends_on", []) or []:
                if dep in tasks_by_key:
                    visit(dep, path + [key])
            color[key] = BLACK

        for k in tasks_by_key:
            if color[k] == WHITE:
                visit(k, [])
        return errors

    def validate(self, parsed: dict, profile_key: str) -> dict:
        """Never PASS on missing/impossible structure (E4.6). Returns
        {"errors": [...], "warnings": [...], "human_decisions": [...],
        "stage_coverage": {stage: bool}}. Caller (PlannerService)
        derives the final outcome from this."""
        errors: list[str] = []
        warnings: list[str] = []
        tasks = parsed.get("tasks") or []
        human_decisions = parsed.get("human_decisions") or []

        keys_seen: set[str] = set()
        tasks_by_key: dict[str, dict] = {}
        for t in tasks:
            key = t.get("key")
            if not key:
                errors.append("A task is missing its 'key'")
                continue
            if key in keys_seen:
                errors.append(f"Duplicate task key: {key}")
                continue
            keys_seen.add(key)
            tasks_by_key[key] = t

        for t in tasks:
            key = t.get("key")
            task_type = (t.get("task_type") or "").strip().upper()
            if not self.workflow_catalog.get_task_type(task_type):
                errors.append(f"Task '{key}': unknown task_type '{task_type}'")
                continue
            role = t.get("preferred_role")
            if role:
                role_row = self.roles_catalog.get_role(role)
                if not role_row:
                    errors.append(f"Task '{key}': unknown preferred_role '{role}'")
                else:
                    tt = self.workflow_catalog.get_task_type(task_type)
                    compatible = json.loads(tt["compatible_role_keys"] or "[]") if tt else []
                    if role.upper() not in compatible and role.upper() != (tt.get("preferred_role_key") if tt else None):
                        warnings.append(f"Task '{key}': preferred_role '{role}' is not associated with task_type '{task_type}' in the catalog -- preserved as a recommendation, not substituted.")
            for dep in t.get("depends_on") or []:
                if dep == key:
                    errors.append(f"Task '{key}': cannot depend on itself")
                elif dep not in keys_seen and dep not in [x.get("key") for x in tasks]:
                    errors.append(f"Task '{key}': depends_on unknown key '{dep}'")

        errors.extend(self._cycle_free(tasks_by_key))

        # Workflow coverage (E4.7): every REQUIRED/REQUIRED_IF stage must
        # be reachable by the proposed Plan. "Reachable" is deliberately
        # not "has its own dedicated task_type" -- no TaskType in the E3
        # catalog exists purely to "run review" or "run tests" as a
        # standalone Task, because ProjectFlow's REAL existing lifecycle
        # already carries every Builder Workspace through Review and (per
        # risk profile) Runtime Verification automatically once it is
        # created (Submit for Review -> Start Review -> PASS; QA per
        # RISK_GATES) -- a BUILD-stage task structurally implies REVIEW
        # and VERIFY coverage the same way ("One task may contribute to
        # multiple gates only where existing truth models support it").
        # HUMAN_ACCEPTANCE is exempt entirely: it is satisfied later by a
        # real human decision (a HUMAN_DECISION WorkProduct), never by
        # planned work itself.
        IMPLICIT_COVERAGE = {"REVIEW": "BUILD", "VERIFY": "BUILD"}
        COVERAGE_EXEMPT_STAGES = {"HUMAN_ACCEPTANCE"}
        stage_reqs = self.workflow_catalog.profile_stages(profile_key)
        stage_coverage: dict[str, bool] = {}
        covered_stages = set()
        for t in tasks:
            tt = self.workflow_catalog.get_task_type((t.get("task_type") or "").strip().upper())
            if tt:
                covered_stages.add(tt["stage_key"])
        for sr in stage_reqs:
            if sr["requirement"] not in ("REQUIRED", "REQUIRED_IF"):
                continue
            stage = sr["stage_key"]
            if stage in COVERAGE_EXEMPT_STAGES:
                stage_coverage[stage] = True
                continue
            ok = stage in covered_stages or IMPLICIT_COVERAGE.get(stage) in covered_stages
            stage_coverage[stage] = ok
            if not ok and sr["requirement"] == "REQUIRED":
                errors.append(f"Workflow coverage: no task covers required stage '{stage}' (CONTROLLED/AGENTIC_STANDARD gates for this stage can never be silently skipped)")

        return {"errors": errors, "warnings": warnings, "stage_coverage": stage_coverage,
                "human_decisions": human_decisions, "task_count": len(tasks)}


class PlannerError(ValueError):
    pass


# ===================================================================
# PLANNER SERVICE -- the facade: generate, validate, materialize, replan
# ===================================================================
class PlannerService:
    def __init__(self, db, changes, work_products, decision, roles_catalog, workflow_catalog, workflow_service,
                 context_builder: PlannerContextBuilder, invoker: PlannerAgentInvoker, validator: PlanValidator,
                 specs_root, repo_root):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.decision = decision
        self.roles_catalog = roles_catalog
        self.workflow_catalog = workflow_catalog
        self.workflow_service = workflow_service
        self.context_builder = context_builder
        self.invoker = invoker
        self.validator = validator
        self.specs_root = specs_root
        self.repo_root = repo_root

    # ---- read -------------------------------------------------------
    def get_plan(self, plan_id: int) -> dict | None:
        return self.db.one("SELECT * FROM plans WHERE id=?", (plan_id,))

    def list_plans(self, change_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM plans WHERE change_id=? ORDER BY revision", (change_id,))

    def plan_items(self, plan_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM plan_items WHERE plan_id=? ORDER BY id", (plan_id,))

    def human_decisions(self, plan_id: int) -> list[dict]:
        return self.db.all("SELECT * FROM plan_human_decisions WHERE plan_id=? ORDER BY id", (plan_id,))

    def human_decisions_pending(self, change_id: int) -> bool:
        """Additive hook WorkflowService (E3) can optionally call (wired
        in app/main.py, defaults to None everywhere else so E3's own
        tests/behavior are completely unaffected) -- E4.12: 'Change/
        Workflow must expose WAITING_HUMAN' while a Plan has an
        unresolved WHAT-level decision."""
        row = self.db.one(
            "SELECT d.id FROM plan_human_decisions d JOIN plans p ON p.id=d.plan_id "
            "WHERE p.change_id=? AND d.resolved=0 LIMIT 1", (change_id,))
        return bool(row)

    def resolve_human_decision(self, decision_id: int, resolution_note: str) -> dict:
        row = self.db.one("SELECT * FROM plan_human_decisions WHERE id=?", (decision_id,))
        if not row:
            raise PlannerError("Human decision not found")
        self.db.execute(
            "UPDATE plan_human_decisions SET resolved=1,resolution_note=?,resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            (resolution_note.strip(), decision_id))
        plan = self.get_plan(row["plan_id"])
        self.db.event("change", plan["change_id"], "PLAN_HUMAN_DECISION_RESOLVED", f"plan={plan['id']} decision={decision_id}")
        return self.db.one("SELECT * FROM plan_human_decisions WHERE id=?", (decision_id,))

    # ---- generation (E4.10) ------------------------------------------
    def plan_change(self, change_id: int, provider: str = "claude", materialize: bool = False) -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise PlannerError("Change not found")
        run = self.workflow_service.get_workflow(change_id)
        if not run:
            raise PlannerError("This Change has no WorkflowRun yet -- create one first (POST /api/changes/{id}/workflow)")
        profile_key = run["profile_key"]

        assignment = self.roles_catalog.validate_assignment(provider, "PLANNER")
        if not assignment["valid"]:
            self.db.event("change", change_id, "PLANNER_ASSIGNMENT_REJECTED", f"provider={provider} missing={assignment['missing_required_capabilities']}")
            return {"outcome": "PLANNER_ASSIGNMENT_INVALID", "plan": None,
                    "message": f"Provider '{provider}' cannot act as PLANNER: missing {assignment['missing_required_capabilities']}"}

        context = self.context_builder.build(change_id, profile_key)
        digest = PlannerContextBuilder.digest(context)
        prompt = PlannerContextBuilder.render_prompt(context)

        try:
            raw_text = self.invoker.invoke(provider, prompt, self.repo_root)
        except PlannerAgentError as exc:
            self.db.event("change", change_id, "PLANNER_EXECUTION_FAILED", str(exc)[:500])
            return {"outcome": "PLANNER_EXECUTION_FAILED", "plan": None, "message": str(exc)}

        try:
            parsed = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            self.db.event("change", change_id, "PLANNER_OUTPUT_INVALID", f"json parse failed: {exc}")
            return {"outcome": "PLANNER_OUTPUT_INVALID", "plan": None, "message": f"Planner output was not valid JSON: {exc}"}
        if not isinstance(parsed, dict) or "tasks" not in parsed or "summary" not in parsed:
            self.db.event("change", change_id, "PLANNER_OUTPUT_INVALID", "missing summary/tasks")
            return {"outcome": "PLANNER_OUTPUT_INVALID", "plan": None, "message": "Planner output is missing required 'summary'/'tasks' fields"}

        revision = (self.db.one("SELECT MAX(revision) AS r FROM plans WHERE change_id=?", (change_id,))["r"] or 0) + 1
        plan_id = self.db.execute(
            "INSERT INTO plans(change_id,workflow_run_id,revision,status,planner_provider,planner_role,input_context_digest,summary,assumptions,raw_output) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (change_id, run["id"], revision, "DRAFT", provider, "PLANNER", digest,
             str(parsed.get("summary") or ""), json.dumps(parsed.get("assumptions") or []), json.dumps(parsed)))
        # OR IGNORE (plan_items has UNIQUE(plan_id,item_key)): a
        # malformed LLM output with a duplicate/missing key must never
        # crash the write path with an unhandled IntegrityError -- the
        # complete, un-deduplicated raw parsed structure is already
        # captured verbatim in plans.raw_output above for audit, and
        # PlanValidator (run right after, against that same `parsed`
        # dict, not against these rows) is what actually reports
        # "Duplicate task key"/"missing its key" as a real PLAN_INVALID
        # error -- this loop only needs to survive storing it.
        for i, t in enumerate(parsed.get("tasks") or []):
            self.db.execute(
                "INSERT OR IGNORE INTO plan_items(plan_id,item_key,title,description,task_type,preferred_role,depends_on_keys,required_inputs,expected_outputs,requirement_ids,scope_hints,rationale,optional) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, t.get("key") or f"_missing_key_{i}", t.get("title") or "", t.get("description") or "",
                 (t.get("task_type") or "").strip().upper() or None, (t.get("preferred_role") or None),
                 json.dumps(t.get("depends_on") or []), json.dumps(t.get("required_inputs") or []),
                 json.dumps(t.get("expected_outputs") or []), json.dumps(t.get("requirements") or []),
                 json.dumps(t.get("scope") or []), t.get("rationale") or "", 1 if t.get("optional") else 0))
        for hd in parsed.get("human_decisions") or []:
            self.db.execute(
                "INSERT INTO plan_human_decisions(plan_id,question,reason,spec_change_signal) VALUES(?,?,?,?)",
                (plan_id, hd.get("question") or "", hd.get("reason") or "", hd.get("spec_change_signal") or "NONE"))
        self.db.event("change", change_id, "PLAN_CREATED", f"plan={plan_id} revision={revision} provider={provider}")

        result = self._validate_and_finalize(plan_id, profile_key)
        if materialize and result["outcome"] == "PLAN_READY":
            result["materialization"] = self.materialize_plan(plan_id)
        return result

    def _validate_and_finalize(self, plan_id: int, profile_key: str) -> dict:
        plan = self.get_plan(plan_id)
        parsed = json.loads(plan["raw_output"])
        validation = self.validator.validate(parsed, profile_key)
        unresolved_human = [hd for hd in validation["human_decisions"] if hd]
        outcome = "PLAN_READY"
        if validation["errors"]:
            outcome = "PLAN_INVALID"
            status = "REJECTED"
        elif unresolved_human:
            outcome = "HUMAN_DECISION_REQUIRED"
            status = "VALIDATED"
        else:
            status = "VALIDATED"
        self.db.execute("UPDATE plans SET status=?,validation_result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (status, json.dumps(validation), plan_id))
        if status == "VALIDATED":
            change = self.changes.get(plan["change_id"])
            self.work_products.create(
                kind="IMPLEMENTATION_PLAN", title=f"Plan v{plan['revision']} for {change['title'] if change else plan['change_id']}",
                project_id=change.get("project_id") if change else None, change_id=plan["change_id"],
                content_ref=f"plan:{plan_id}", status="DRAFT",
                content_metadata={"plan_id": plan_id, "revision": plan["revision"], "input_context_digest": plan["input_context_digest"]})
        self.db.event("change", plan["change_id"], "PLAN_VALIDATED" if status == "VALIDATED" else "PLAN_REJECTED", f"plan={plan_id} outcome={outcome}")
        return {"outcome": outcome, "plan": self.get_plan(plan_id), "validation": validation}

    def validate_plan(self, plan_id: int) -> dict:
        """Re-run validation (e.g. after a human decision was resolved)."""
        plan = self.get_plan(plan_id)
        if not plan:
            raise PlannerError("Plan not found")
        return self._validate_and_finalize(plan_id, self.db.one("SELECT profile_key FROM workflow_runs WHERE id=?", (plan["workflow_run_id"],))["profile_key"])

    # ---- materialization (E4.8) --------------------------------------
    def materialize_plan(self, plan_id: int) -> dict:
        plan = self.get_plan(plan_id)
        if not plan:
            raise PlannerError("Plan not found")
        if plan["status"] != "VALIDATED":
            raise PlannerError(f"Plan must be VALIDATED to materialize (current status: {plan['status']})")
        if any(not hd["resolved"] for hd in self.human_decisions(plan_id)):
            raise PlannerError("This Plan has unresolved human decisions -- resolve them before materializing (E4.12: never materialize past an unresolved WHAT-level decision)")
        items = self.plan_items(plan_id)
        with self.db.connect() as conn:
            key_to_task_id: dict[str, int] = {}
            for it in items:
                cur = conn.execute(
                    "INSERT INTO tasks(slug,title,description,status,change_id,task_type) VALUES(?,?,?,?,?,?)",
                    (f"plan-{plan_id}-{it['item_key']}".lower().replace(" ", "-"), it["title"], it["description"],
                     "BACKLOG", plan["change_id"], it["task_type"]))
                key_to_task_id[it["item_key"]] = cur.lastrowid
                conn.execute("UPDATE plan_items SET materialized_task_id=? WHERE id=?", (cur.lastrowid, it["id"]))
            for it in items:
                for dep_key in json.loads(it["depends_on_keys"] or "[]"):
                    if dep_key in key_to_task_id:
                        conn.execute(
                            "INSERT OR IGNORE INTO task_dependencies(task_id,depends_on_task_id) VALUES(?,?)",
                            (key_to_task_id[it["item_key"]], key_to_task_id[dep_key]))
            conn.execute("UPDATE plans SET status='MATERIALIZED',materialized_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (plan_id,))
        self.db.event("change", plan["change_id"], "PLAN_MATERIALIZED", f"plan={plan_id} tasks={len(key_to_task_id)}")
        return {"plan_id": plan_id, "task_ids": key_to_task_id}

    # ---- replanning (E4.11) ------------------------------------------
    def replan_change(self, change_id: int, provider: str = "claude") -> dict:
        latest = self.db.one("SELECT * FROM plans WHERE change_id=? ORDER BY revision DESC LIMIT 1", (change_id,))
        result = self.plan_change(change_id, provider=provider)
        if result["plan"] and latest:
            self.db.execute("UPDATE plans SET supersedes_plan_id=? WHERE id=?", (latest["id"], result["plan"]["id"]))
            # A MATERIALIZED plan's own status is historical fact and is
            # never rewritten (E4.11: "old Plan remains immutable/
            # history-visible") -- only a still-open (DRAFT/VALIDATED)
            # predecessor is marked SUPERSEDED.
            if latest["status"] in ("DRAFT", "VALIDATED"):
                self.db.execute("UPDATE plans SET status='SUPERSEDED',updated_at=CURRENT_TIMESTAMP WHERE id=?", (latest["id"],))
            self.db.event("change", change_id, "PLAN_SUPERSEDED", f"{latest['id']} -> {result['plan']['id']}")
            result["plan"] = self.get_plan(result["plan"]["id"])
        return result
