from __future__ import annotations

"""Workflow / Process Engine (Phase E3): the deterministic process
substrate a future Planner (E4+) will use. Three responsibilities kept
strictly separate (E3's key architectural rule):

  WORKFLOW DEFINITION  -- "what process should apply?"
    WorkflowProfile / WorkflowStage / GateRequirement / TaskType: a
    declarative, code-seeded catalog (this module), the same
    idempotent-upsert pattern app/services/engineering_catalog.py
    already established for Role/Capability -- never a giant
    if/else tree, never a condition-language engine.

  WORKFLOW INSTANCE     -- "where is this Change in that process?"
    WorkflowRun: one durable row per Change (change_id UNIQUE) that
    only ever records identity (profile_key/version, created_at) --
    NEVER a mutable status/stage column a route (or an Agent) could
    arbitrarily write. Status/stage/gates are always DERIVED fresh by
    WorkflowStateEvaluator on every read, the same "no persisted
    status a route can forget to update" discipline
    TaskDecisionService already uses for Task status/stage.

  TASK EXECUTION        -- "execute this specific piece of work."
    Untouched: TaskStore (tasks table + app/main.py helpers),
    Supervisor (_start_builder_session), TaskDecisionService. This
    module only ever READS their output; it is not another
    Supervisor and never starts an Agent session itself.

GateRequirement evaluation reuses existing truth exclusively -- see
WorkflowStateEvaluator's per-gate _gate_* methods, each citing its real
source (SpecComplianceVerifier, TaskDecisionService, WorkProductService,
the deployments table). Never a parallel/independent calculation, and
never PASS on missing evidence."""

import json

REQUIREMENT_LEVELS = ("REQUIRED", "OPTIONAL", "REQUIRED_IF")
WORKFLOW_STATUSES = ("PENDING", "ACTIVE", "BLOCKED", "WAITING_HUMAN", "COMPLETE", "FAILED")
READINESS_STATES = ("READY", "WAITING_DEPENDENCY", "BLOCKED", "COMPLETE")

# ---------------------------------------------------------------- Stages
# Canonical order -- also the order current_stage is resolved in.
STAGE_ORDER = (
    "ANALYSIS", "SPEC", "ARCHITECTURE", "DESIGN", "PLANNING", "BUILD",
    "REVIEW", "VERIFY", "RELEASE", "DEPLOY", "HUMAN_ACCEPTANCE",
)
STAGES = {
    "ANALYSIS": "Understand the requested change before committing to a spec or design.",
    "SPEC": "Define expected behavior as an approved FeatureSpec (Spec Layer).",
    "ARCHITECTURE": "Assess/record structural impact (ADR/architecture analysis).",
    "DESIGN": "Produce a technical or UI/UX design to build against.",
    "PLANNING": "Decompose the Change into concrete Tasks.",
    "BUILD": "Agents produce source changes in Builder Workspaces.",
    "REVIEW": "Code review, spec-compliance review, and (where applicable) security review.",
    "VERIFY": "Automated tests and Runtime Verification against real evidence.",
    "RELEASE": "Build/qualify a deployable artifact.",
    "DEPLOY": "Deploy the qualified artifact and confirm it is live/healthy.",
    "HUMAN_ACCEPTANCE": "An explicit human sign-off that the Change is accepted.",
}

# ------------------------------------------------------- Gate requirements
# Each gate belongs to exactly one stage and is evaluated by exactly one
# WorkflowStateEvaluator._gate_* method -- see that class for the real
# existing-truth source each one reuses.
GATES = {
    "SPEC_APPROVED": {"name": "Spec approved", "stage": "SPEC",
        "description": "An approved FeatureSpec WorkProduct exists for this Change."},
    "ARCHITECTURE_READY": {"name": "Architecture ready", "stage": "ARCHITECTURE",
        "description": "An approved architecture-analysis/ADR WorkProduct exists for this Change."},
    "DESIGN_READY": {"name": "Design ready", "stage": "DESIGN",
        "description": "An approved technical/UI-UX design WorkProduct exists for this Change."},
    "REVIEW_PASS": {"name": "Review pass", "stage": "REVIEW",
        "description": "Every Builder Workspace on every Task in this Change has a current Review PASS."},
    "SPEC_COMPLIANCE_PASS": {"name": "Spec compliance pass", "stage": "REVIEW",
        "description": "Every spec-linked Task in this Change has a PASS SpecComplianceResult."},
    "SECURITY_PASS": {"name": "Security review pass", "stage": "REVIEW",
        "description": "Same review evidence as REVIEW_PASS -- ProjectFlow has no distinct security-review data source yet (see docs/ENGINEERING_ROLES.md's SECURITY_REVIEWER note); kept as its own gate key for future differentiation."},
    "TESTS_PASS": {"name": "Automated tests pass", "stage": "VERIFY",
        "description": "Every Task's AUTOMATED_TESTS checklist item is done, at the exact current HEAD."},
    # Phase E7: attached to the EXISTING VERIFY stage rather than a new
    # top-level stage (E7.17's own explicit fallback -- adding a stage
    # would shift order_index for every already-seeded stage in
    # production; a gate needs no stage of its own, the same way
    # REVIEW_PASS/SPEC_COMPLIANCE_PASS/SECURITY_PASS already share
    # REVIEW). Vacuously satisfied when nothing governs the Change yet
    # (no FeatureSpec linkage -- see _gate_test_design_ready), so VIBE
    # (typically un-spec-linked) is never blocked by it.
    "TEST_DESIGN_READY": {"name": "Test design ready", "stage": "VERIFY",
        "description": "Every governing FeatureSpec's requirements have at least one proving TestCaseSpec, confirmed by an independent Test Review PASS."},
    "RELEASE_READY": {"name": "Release ready", "stage": "RELEASE",
        "description": "Every Task in this Change is READY_FOR_MAIN or DONE."},
    "DEPLOY_VERIFIED": {"name": "Deploy verified", "stage": "DEPLOY",
        "description": "Every repository this Change touches has a VERIFIED DEV deployment."},
    "HUMAN_ACCEPTANCE": {"name": "Human acceptance", "stage": "HUMAN_ACCEPTANCE",
        "description": "An approved HUMAN_DECISION WorkProduct exists for this Change."},
}
GATES_BY_STAGE: dict[str, list[str]] = {}
for _gk, _g in GATES.items():
    GATES_BY_STAGE.setdefault(_g["stage"], []).append(_gk)

# ------------------------------------------------------------ Profiles
# Keep v1 deterministic and explicit (E3.3): only one named REQUIRED_IF
# condition exists (DEPLOYMENT_REQUESTED), resolved by
# WorkflowStateEvaluator._condition_met -- never a general expression
# language.
PROFILES = {
    "VIBE": {"name": "Vibe", "version": 1,
        "description": "Minimal process for low-risk/internal/prototype changes. No Spec/Architecture required by default."},
    "AGENTIC_STANDARD": {"name": "Agentic Standard", "version": 1,
        "description": "The default autonomous engineering workflow: Spec, Build, Review, Verify required; Architecture/Design/Release/Deploy/Human Acceptance optional."},
    "CONTROLLED": {"name": "Controlled", "version": 1,
        "description": "Stronger specification/design/review/verification/release requirements, plus mandatory human acceptance."},
}
DEFAULT_PROFILE = "AGENTIC_STANDARD"

# profile_key -> {stage_key: (requirement, condition_key|None)}
PROFILE_STAGES = {
    "VIBE": {
        "BUILD": ("REQUIRED", None),
        "VERIFY": ("REQUIRED", None),
        "REVIEW": ("OPTIONAL", None),
        "DEPLOY": ("REQUIRED_IF", "DEPLOYMENT_REQUESTED"),
    },
    "AGENTIC_STANDARD": {
        "ANALYSIS": ("OPTIONAL", None),
        "SPEC": ("REQUIRED", None),
        "ARCHITECTURE": ("OPTIONAL", None),
        "DESIGN": ("OPTIONAL", None),
        "PLANNING": ("OPTIONAL", None),
        "BUILD": ("REQUIRED", None),
        "REVIEW": ("REQUIRED", None),
        "VERIFY": ("REQUIRED", None),
        "RELEASE": ("OPTIONAL", None),
        "DEPLOY": ("REQUIRED_IF", "DEPLOYMENT_REQUESTED"),
        "HUMAN_ACCEPTANCE": ("OPTIONAL", None),
    },
    "CONTROLLED": {
        "ANALYSIS": ("OPTIONAL", None),
        "SPEC": ("REQUIRED", None),
        "ARCHITECTURE": ("OPTIONAL", None),
        "DESIGN": ("REQUIRED", None),
        "PLANNING": ("OPTIONAL", None),
        "BUILD": ("REQUIRED", None),
        "REVIEW": ("REQUIRED", None),
        "VERIFY": ("REQUIRED", None),
        "RELEASE": ("REQUIRED", None),
        "DEPLOY": ("REQUIRED_IF", "DEPLOYMENT_REQUESTED"),
        "HUMAN_ACCEPTANCE": ("REQUIRED", None),
    },
}

# --------------------------------------------------------- Task types
# TaskType is WHAT kind of work, never Role (WHO/what capability). Each
# maps to the stage it naturally belongs to and a preferred Engineering
# Role from the E2 catalog -- None where the catalog intentionally has
# no matching specialized role (never forced).
TASK_TYPES = {
    "REQUIREMENT_ANALYSIS": {"name": "Requirement Analysis", "stage": "ANALYSIS", "preferred_role": "REQUIREMENTS_ANALYST", "compatible_roles": ["REQUIREMENTS_ANALYST"]},
    "SPEC_AUTHORING": {"name": "Spec Authoring", "stage": "SPEC", "preferred_role": "SPEC_ANALYST", "compatible_roles": ["SPEC_ANALYST"]},
    "SPEC_REVIEW": {"name": "Spec Review", "stage": "SPEC", "preferred_role": "REVIEWER", "compatible_roles": ["REVIEWER", "SPEC_ANALYST"]},
    # Phase E6: these four were seeded ahead of time in E3 with a
    # placeholder preferred_role (PLANNER/None) because no specialized
    # role existed yet -- E6.1 adds SOFTWARE_ARCHITECT/TECHNICAL_DESIGNER/
    # UI_UX_DESIGNER, so filling these in now is completing E3's own
    # documented placeholder, not redefining the catalog.
    "ARCHITECTURE_ANALYSIS": {"name": "Architecture Analysis", "stage": "ARCHITECTURE", "preferred_role": "SOFTWARE_ARCHITECT", "compatible_roles": ["SOFTWARE_ARCHITECT"]},
    "ARCHITECTURE_REVIEW": {"name": "Architecture Review", "stage": "ARCHITECTURE", "preferred_role": "REVIEWER", "compatible_roles": ["REVIEWER"]},
    "TECHNICAL_DESIGN": {"name": "Technical Design", "stage": "DESIGN", "preferred_role": "TECHNICAL_DESIGNER", "compatible_roles": ["TECHNICAL_DESIGNER", "SOFTWARE_ARCHITECT"]},
    "UI_UX_DESIGN": {"name": "UI/UX Design", "stage": "DESIGN", "preferred_role": "UI_UX_DESIGNER", "compatible_roles": ["UI_UX_DESIGNER"]},
    # Phase E7: completes E3's own placeholder the same way E6 completed
    # ARCHITECTURE_ANALYSIS/TECHNICAL_DESIGN/UI_UX_DESIGN's -- QA_VERIFIER
    # stays a compatible role (it still owns runtime verification), but
    # TEST_DESIGNER is now the specialized preferred one.
    "TEST_DESIGN": {"name": "Test Design", "stage": "PLANNING", "preferred_role": "TEST_DESIGNER", "compatible_roles": ["TEST_DESIGNER", "QA_VERIFIER"]},
    "IMPLEMENTATION": {"name": "Implementation", "stage": "BUILD", "preferred_role": "BUILDER", "compatible_roles": ["BUILDER"]},
    "TEST_IMPLEMENTATION": {"name": "Test Implementation", "stage": "BUILD", "preferred_role": "BUILDER", "compatible_roles": ["BUILDER"]},
    "CODE_REVIEW": {"name": "Code Review", "stage": "REVIEW", "preferred_role": "REVIEWER", "compatible_roles": ["REVIEWER"]},
    "SPEC_COMPLIANCE_REVIEW": {"name": "Spec Compliance Review", "stage": "REVIEW", "preferred_role": "REVIEWER", "compatible_roles": ["REVIEWER", "SPEC_ANALYST"]},
    "SECURITY_REVIEW": {"name": "Security Review", "stage": "REVIEW", "preferred_role": "SECURITY_REVIEWER", "compatible_roles": ["SECURITY_REVIEWER", "REVIEWER"]},
    "FIX": {"name": "Fix", "stage": "BUILD", "preferred_role": "BUILDER", "compatible_roles": ["BUILDER"]},
    "BUG_TRIAGE": {"name": "Bug Triage", "stage": "ANALYSIS", "preferred_role": "REQUIREMENTS_ANALYST", "compatible_roles": ["REQUIREMENTS_ANALYST"]},
    "BUILD": {"name": "Build", "stage": "RELEASE", "preferred_role": "RELEASE_MANAGER", "compatible_roles": ["RELEASE_MANAGER"]},
    "RELEASE": {"name": "Release", "stage": "RELEASE", "preferred_role": "RELEASE_MANAGER", "compatible_roles": ["RELEASE_MANAGER"]},
    "DEPLOY": {"name": "Deploy", "stage": "DEPLOY", "preferred_role": "RELEASE_MANAGER", "compatible_roles": ["RELEASE_MANAGER"]},
    "POST_DEPLOY_VERIFY": {"name": "Post-Deploy Verify", "stage": "DEPLOY", "preferred_role": "QA_VERIFIER", "compatible_roles": ["QA_VERIFIER"]},
    "HUMAN_ACCEPTANCE": {"name": "Human Acceptance", "stage": "HUMAN_ACCEPTANCE", "preferred_role": None, "compatible_roles": []},
    "DOCUMENTATION": {"name": "Documentation", "stage": "BUILD", "preferred_role": "BUILDER", "compatible_roles": ["BUILDER", "SPEC_ANALYST"]},
}


class WorkflowError(ValueError):
    pass


# ===================================================================
# WORKFLOW DEFINITION -- code-seeded catalog, idempotent upsert
# ===================================================================
class WorkflowCatalogService:
    def __init__(self, db):
        self.db = db

    def seed(self) -> None:
        for i, key in enumerate(STAGE_ORDER):
            self.db.execute(
                "INSERT INTO workflow_stages(key,name,description,order_index) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET name=excluded.name,description=excluded.description,order_index=excluded.order_index,updated_at=CURRENT_TIMESTAMP",
                (key, key.replace("_", " ").title(), STAGES[key], i),
            )
        stage_ids = {r["key"]: r["id"] for r in self.db.all("SELECT id,key FROM workflow_stages")}
        for key, gate in GATES.items():
            self.db.execute(
                "INSERT INTO gate_requirements(key,name,description,stage_id) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET name=excluded.name,description=excluded.description,stage_id=excluded.stage_id,updated_at=CURRENT_TIMESTAMP",
                (key, gate["name"], gate["description"], stage_ids[gate["stage"]]),
            )
        for key, profile in PROFILES.items():
            self.db.execute(
                "INSERT INTO workflow_profiles(key,name,description,version) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET name=excluded.name,description=excluded.description,version=excluded.version,updated_at=CURRENT_TIMESTAMP",
                (key, profile["name"], profile["description"], profile["version"]),
            )
        profile_ids = {r["key"]: r["id"] for r in self.db.all("SELECT id,key FROM workflow_profiles")}
        for profile_key, stages in PROFILE_STAGES.items():
            for stage_key, (requirement, condition_key) in stages.items():
                self.db.execute(
                    "INSERT INTO workflow_profile_stages(profile_id,stage_id,requirement,condition_key) VALUES(?,?,?,?) "
                    "ON CONFLICT(profile_id,stage_id) DO UPDATE SET requirement=excluded.requirement,condition_key=excluded.condition_key",
                    (profile_ids[profile_key], stage_ids[stage_key], requirement, condition_key),
                )
        for key, tt in TASK_TYPES.items():
            self.db.execute(
                "INSERT INTO task_types(key,name,description,stage_key,preferred_role_key,compatible_role_keys) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET name=excluded.name,stage_key=excluded.stage_key,preferred_role_key=excluded.preferred_role_key,compatible_role_keys=excluded.compatible_role_keys,updated_at=CURRENT_TIMESTAMP",
                (key, tt["name"], tt.get("description", ""), tt["stage"], tt["preferred_role"], json.dumps(tt["compatible_roles"])),
            )

    # ---- read ----------------------------------------------------
    def list_profiles(self) -> list[dict]:
        return self.db.all("SELECT * FROM workflow_profiles ORDER BY key")

    def get_profile(self, key: str) -> dict | None:
        return self.db.one("SELECT * FROM workflow_profiles WHERE key=?", ((key or "").strip().upper(),))

    def profile_stages(self, profile_key: str) -> list[dict]:
        return self.db.all(
            "SELECT s.key AS stage_key, s.name, s.order_index, ps.requirement, ps.condition_key "
            "FROM workflow_profile_stages ps JOIN workflow_stages s ON s.id=ps.stage_id "
            "JOIN workflow_profiles p ON p.id=ps.profile_id WHERE p.key=? ORDER BY s.order_index",
            ((profile_key or "").strip().upper(),),
        )

    def list_stages(self) -> list[dict]:
        return self.db.all("SELECT * FROM workflow_stages ORDER BY order_index")

    def list_gates(self) -> list[dict]:
        return self.db.all(
            "SELECT g.*, s.key AS stage_key FROM gate_requirements g JOIN workflow_stages s ON s.id=g.stage_id ORDER BY s.order_index,g.key"
        )

    def list_task_types(self) -> list[dict]:
        return self.db.all("SELECT * FROM task_types ORDER BY key")

    def get_task_type(self, key: str) -> dict | None:
        return self.db.one("SELECT * FROM task_types WHERE key=?", ((key or "").strip().upper(),))


# ===================================================================
# TASK DEPENDENCY GRAPH (E3.6)
# ===================================================================
class TaskDependencyService:
    def __init__(self, db):
        self.db = db

    def _task(self, task_id: int) -> dict:
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise WorkflowError(f"Task {task_id} not found")
        return row

    def _reaches(self, start_task_id: int, target_task_id: int) -> bool:
        """True if target_task_id is reachable from start_task_id by
        following existing depends_on edges -- used to detect that
        adding a new edge would close a cycle, before it is ever
        written."""
        seen: set[int] = set()
        stack = [start_task_id]
        while stack:
            node = stack.pop()
            if node == target_task_id:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(r["depends_on_task_id"] for r in self.db.all(
                "SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?", (node,)))
        return False

    def add_dependency(self, task_id: int, depends_on_task_id: int) -> int:
        if task_id == depends_on_task_id:
            raise WorkflowError("A Task cannot depend on itself")
        t = self._task(task_id)
        dep = self._task(depends_on_task_id)
        # Same Project / compatible Change scope (E3.6): only enforced
        # when BOTH tasks actually have a Change -- a Task with no
        # Change at all (the common legacy case) is never blocked by
        # this check, matching E1/E2's own additive backward-compat rule.
        if t.get("change_id") and dep.get("change_id") and t["change_id"] != dep["change_id"]:
            raise WorkflowError("Tasks belong to different Changes -- cross-Change dependencies are not supported yet")
        if self.db.one("SELECT id FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?", (task_id, depends_on_task_id)):
            raise WorkflowError("This dependency already exists")
        if self._reaches(depends_on_task_id, task_id):
            raise WorkflowError(f"Adding this dependency would create a cycle (Task {depends_on_task_id} already (transitively) depends on Task {task_id})")
        did = self.db.execute(
            "INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES(?,?)", (task_id, depends_on_task_id))
        self.db.event("task", task_id, "DEPENDENCY_ADDED", f"depends_on={depends_on_task_id}")
        return did

    def dependencies_for(self, task_id: int) -> list[dict]:
        return self.db.all(
            "SELECT td.*, t.title AS depends_on_title FROM task_dependencies td "
            "JOIN tasks t ON t.id=td.depends_on_task_id WHERE td.task_id=? ORDER BY td.id", (task_id,))

    def dependents_of(self, task_id: int) -> list[dict]:
        return self.db.all(
            "SELECT td.*, t.title AS task_title FROM task_dependencies td "
            "JOIN tasks t ON t.id=td.task_id WHERE td.depends_on_task_id=? ORDER BY td.id", (task_id,))

    def readiness(self, task_id: int, decision) -> dict:
        """Deterministic readiness (E3.6) -- reuses
        TaskDecisionService.evaluate() as the ONLY source of a Task's
        own completion truth, both for this task and every predecessor;
        never a second, competing Task state machine."""
        own = decision.evaluate(task_id)
        if own["status"] == "DONE":
            return {"task_id": task_id, "readiness": "COMPLETE", "unmet_dependencies": []}
        if own["status"] == "BLOCKED":
            return {"task_id": task_id, "readiness": "BLOCKED", "unmet_dependencies": []}
        deps = self.dependencies_for(task_id)
        unmet = [d["depends_on_task_id"] for d in deps if decision.evaluate(d["depends_on_task_id"])["status"] != "DONE"]
        return {"task_id": task_id, "readiness": "WAITING_DEPENDENCY" if unmet else "READY", "unmet_dependencies": unmet}


# ===================================================================
# WORKFLOW INSTANCE + DERIVED STATE (E3.5 / E3.8)
# ===================================================================
class WorkflowService:
    def __init__(self, db, catalog: WorkflowCatalogService, changes, work_products, decision, spec_compliance, dependencies: TaskDependencyService,
                 human_decisions_pending=None):
        self.db = db
        self.catalog = catalog
        self.changes = changes
        self.work_products = work_products
        self.decision = decision
        self.spec_compliance = spec_compliance
        self.dependencies = dependencies
        # Phase E4 (Dynamic Planner) additive hook: an optional
        # Callable[[change_id], bool] a caller can wire in after both
        # services exist (see app/main.py) so evaluate_workflow() can
        # surface WAITING_HUMAN while a Plan has an unresolved WHAT-level
        # decision, without WorkflowService importing anything from the
        # Planner module -- None here (the default, and every E3 test's
        # own construction) means zero behavior change.
        self.human_decisions_pending = human_decisions_pending
        # Phase E6 additive hook, same pattern as human_decisions_pending
        # above: an optional object exposing .architecture_ready(change_id)
        # and .design_ready(change_id) (wired in app/main.py once
        # ArchitectureDesignLifecycleService exists) so ARCHITECTURE_READY/
        # DESIGN_READY use real independent-review evidence instead of
        # bare WorkProduct presence. None here (default, and every E3/E4/
        # E5 test's own construction) preserves the exact E3 fallback
        # behavior below -- zero behavior change for anything that
        # doesn't wire this in.
        self.architecture_design_gate = None
        # Phase E7 additive hook, same pattern as architecture_design_gate
        # above: an optional object exposing .test_design_ready(change_id)
        # (wired in app/main.py once TestDesignLifecycleService exists).
        # None by default -- every E3/E4/E5/E6 test's own construction
        # leaves this unset, and _gate_test_design_ready below is
        # vacuously satisfied whenever nothing governs the Change
        # (no Change-level spec_feature trace link) regardless, so no
        # existing behavior changes either way.
        self.test_design_gate = None
        # Phase E9 additive hook, same pattern as architecture_design_gate/
        # test_design_gate above: an optional object exposing
        # .review_pass(task_id) and .security_pass(task_id) -- each
        # returns True/False when it has real E9 CodeReview/SecurityReview
        # evidence for that Task's CURRENT head commit, or None to mean
        # "no E9 evidence yet, fall back to the legacy per-workspace
        # check" (wired in app/main.py once ReviewFixOrchestratorService
        # exists). None here (default, every pre-E9 test's own
        # construction) preserves the exact legacy REVIEW_PASS/
        # SECURITY_PASS behavior below untouched.
        self.review_gate = None

    # ---- creation (E3.10) -----------------------------------------
    def create_workflow_for_change(self, change_id: int, profile_key: str | None = None, project_policy: dict | None = None) -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise WorkflowError("Change not found")
        if self.get_workflow(change_id):
            raise WorkflowError("This Change already has a workflow -- create a new Change instead of replacing an existing process instance")
        wf_policy = (project_policy or {}).get("workflow") or {}
        allowed = wf_policy.get("allowed_profiles")
        profile_key = (profile_key or wf_policy.get("default_profile") or DEFAULT_PROFILE).strip().upper()
        profile = self.catalog.get_profile(profile_key)
        if not profile:
            raise WorkflowError(f"Unknown workflow profile: {profile_key} (must be one of {sorted(PROFILES)})")
        if allowed and profile_key not in [str(a).strip().upper() for a in allowed]:
            raise WorkflowError(f"Repository policy restricts workflows to {allowed}; '{profile_key}' is not listed")
        wid = self.db.execute(
            "INSERT INTO workflow_runs(change_id,profile_key,profile_version) VALUES(?,?,?)",
            (change_id, profile_key, profile["version"]),
        )
        self.db.event("change", change_id, "WORKFLOW_CREATED", f"profile={profile_key} v{profile['version']}")
        return self.get_workflow(change_id)

    def get_workflow(self, change_id: int) -> dict | None:
        return self.db.one("SELECT * FROM workflow_runs WHERE change_id=?", (change_id,))

    # ---- gate evaluation (E3.7) -- each method reuses ONE existing
    # truth source; never a parallel/independent calculation, never
    # PASS on missing evidence. -------------------------------------
    def _approved_work_product_exists(self, change_id: int, kinds: tuple[str, ...]) -> bool:
        return any(wp["kind"] in kinds and wp["status"] == "APPROVED" for wp in self.work_products.list_for_change(change_id))

    def _gate_spec_approved(self, change_id: int, tasks: list[dict]) -> bool:
        return self._approved_work_product_exists(change_id, ("FEATURE_SPEC",))

    def _gate_architecture_ready(self, change_id: int, tasks: list[dict]) -> bool:
        """E6.16: closes the E3 placeholder. When
        ArchitectureDesignLifecycleService is wired in (self.
        architecture_design_gate, additive hook set in app/main.py),
        this is real independent-review evidence -- an ArchitectureAnalysis
        APPROVED only after a separate ArchitectureReviewService
        invocation returned PASS, never bare WorkProduct presence. Falls
        back to the original E3 presence-only check when unwired (every
        E3 test's own WorkflowService construction) -- zero behavior
        change there."""
        if self.architecture_design_gate is not None:
            return bool(self.architecture_design_gate.architecture_ready(change_id))
        return self._approved_work_product_exists(change_id, ("ARCHITECTURE_ANALYSIS", "ADR"))

    def _gate_design_ready(self, change_id: int, tasks: list[dict]) -> bool:
        """E6.16: same upgrade as _gate_architecture_ready above -- real
        evidence (TechnicalDesign valid/current, independent design
        review PASS, UI/UX PASS if applicable, no unresolved design
        human decisions, requirement references valid, no unresolved
        DESIGN_SPEC_CONFLICT) instead of bare WorkProduct presence."""
        if self.architecture_design_gate is not None:
            return bool(self.architecture_design_gate.design_ready(change_id))
        return self._approved_work_product_exists(change_id, ("TECHNICAL_DESIGN", "UI_UX_DESIGN"))

    def _gate_human_acceptance(self, change_id: int, tasks: list[dict]) -> bool:
        return self._approved_work_product_exists(change_id, ("HUMAN_DECISION",))

    def _gate_test_design_ready(self, change_id: int, tasks: list[dict]) -> bool:
        """E7.17: attached to VERIFY (see the GATES entry's own comment
        for why no new stage was added). Unwired (test_design_gate is
        None, every pre-E7 test's own construction) -- vacuously True,
        zero behavior change. Wired: delegates to
        TestDesignLifecycleService.test_design_ready(), which is itself
        vacuously True whenever nothing governs the Change at the
        Change-level spec trace-link (the same 'governing feature'
        definition E6's ArchitectureContextBuilder established) -- a
        real, documented scope limit: Task-level spec_feature_id
        linkage alone does not yet trigger this gate."""
        if self.test_design_gate is None:
            return True
        return bool(self.test_design_gate.test_design_ready(change_id))

    def _gate_review_pass(self, change_id: int, tasks: list[dict]) -> bool:
        """E9.25: a Task with real E9 CodeReview evidence for its
        CURRENT head commit uses that (review_gate.review_pass()
        returns True/False); a Task with none yet (review_gate is
        unwired, or simply has no E9 review row) falls back to the
        exact legacy per-workspace check -- TaskDecisionService.
        evaluate()'s own builder review_status, never re-derived here.
        This is a per-Task compatibility rule, not a service-level
        on/off switch, so old/manual Tasks keep working unchanged even
        after review_gate is wired."""
        if not tasks:
            return False
        for t in tasks:
            if self.review_gate is not None:
                result = self.review_gate.review_pass(t["id"])
                if result is not None:
                    if not result:
                        return False
                    continue
            d = self.decision.evaluate(t["id"])
            if not d["builders"] or any(b["review_status"] != "PASS" for b in d["builders"]):
                return False
        return True

    def _gate_security_pass(self, change_id: int, tasks: list[dict]) -> bool:
        """E9.24: closes the E3 limitation this method's own previous
        docstring named -- SECURITY_PASS no longer aliases REVIEW_PASS
        when real SecurityReview evidence exists. review_gate.
        security_pass() returns True (PASS/PASS_WITH_FINDINGS or
        genuinely NOT_APPLICABLE), False (FIX_REQUIRED/blocked), or
        None ("no E9 evidence for this Task's head yet, or review_gate
        unwired") -- None falls back to the exact legacy REVIEW_PASS-
        reuse behavior, so nothing pre-E9 changes."""
        if not tasks:
            return False
        for t in tasks:
            if self.review_gate is not None:
                result = self.review_gate.security_pass(t["id"])
                if result is not None:
                    if not result:
                        return False
                    continue
            d = self.decision.evaluate(t["id"])
            if not d["builders"] or any(b["review_status"] != "PASS" for b in d["builders"]):
                return False
        return True

    def _gate_spec_compliance_pass(self, change_id: int, tasks: list[dict]) -> bool:
        """Reuses SpecComplianceVerifier directly -- never re-implements
        spec compliance logic here. Only meaningful for Tasks actually
        spec-linked; a Change with zero spec-linked Tasks has no
        evidence to point to, so this is UNMET (never a default PASS)."""
        linked = [t for t in tasks if t.get("spec_feature_id")]
        if not linked:
            return False
        return all(self.spec_compliance.verify(t["id"])["verdict"] == "PASS" for t in linked)

    def _gate_tests_pass(self, change_id: int, tasks: list[dict]) -> bool:
        """Reuses TaskDecisionService's own AUTOMATED_TESTS checklist
        item (test_runs-backed, pinned to exact HEAD) -- never a second
        test-result calculation."""
        if not tasks:
            return False
        for t in tasks:
            checklist = self.decision.evaluate(t["id"])["checklist"]
            item = next((c for c in checklist if c["key"] == "AUTOMATED_TESTS"), None)
            if not item or item["state"] != "done":
                return False
        return True

    def _gate_release_ready(self, change_id: int, tasks: list[dict]) -> bool:
        if not tasks:
            return False
        for t in tasks:
            d = self.decision.evaluate(t["id"])
            if not (d["ready_for_main"] or d["status"] == "DONE"):
                return False
        return True

    def _gate_deploy_verified(self, change_id: int, tasks: list[dict]) -> bool:
        """Reuses the existing `deployments` table (DeploymentService's
        own evidence) -- never a second deployment-status calculation."""
        repo_ids: set[int] = set()
        for t in tasks:
            repo_ids.update(r["repository_id"] for r in self.db.all(
                "SELECT repository_id FROM merge_records WHERE task_id=? AND required=1", (t["id"],)))
        if not repo_ids:
            return False
        for rid in repo_ids:
            latest = self.db.one(
                "SELECT * FROM deployments WHERE repository_id=? AND environment='DEV' "
                "AND task_id IN (SELECT id FROM tasks WHERE change_id=?) ORDER BY id DESC LIMIT 1",
                (rid, change_id),
            )
            if not latest or latest["status"] != "VERIFIED":
                return False
        return True

    _GATE_EVALUATORS = {
        "SPEC_APPROVED": "_gate_spec_approved",
        "ARCHITECTURE_READY": "_gate_architecture_ready",
        "DESIGN_READY": "_gate_design_ready",
        "REVIEW_PASS": "_gate_review_pass",
        "SPEC_COMPLIANCE_PASS": "_gate_spec_compliance_pass",
        "SECURITY_PASS": "_gate_security_pass",
        "TESTS_PASS": "_gate_tests_pass",
        "TEST_DESIGN_READY": "_gate_test_design_ready",
        "RELEASE_READY": "_gate_release_ready",
        "DEPLOY_VERIFIED": "_gate_deploy_verified",
        "HUMAN_ACCEPTANCE": "_gate_human_acceptance",
    }

    def _evaluate_gate(self, gate_key: str, change_id: int, tasks: list[dict]) -> bool:
        method = getattr(self, self._GATE_EVALUATORS[gate_key])
        return bool(method(change_id, tasks))

    def _condition_met(self, condition_key: str, change_id: int, tasks: list[dict]) -> bool:
        """The ONE named v1 condition (E3.3/E3.9) -- never a general
        expression language. DEPLOYMENT_REQUESTED is true the moment any
        deployment has actually been requested for a Task in this
        Change (a real deployments row exists), regardless of its
        current status."""
        if condition_key == "DEPLOYMENT_REQUESTED":
            return any(self.db.one("SELECT id FROM deployments WHERE task_id=?", (t["id"],)) for t in tasks)
        return False

    # ---- derived state (E3.8) --------------------------------------
    def evaluate_workflow(self, change_id: int) -> dict:
        run = self.get_workflow(change_id)
        if not run:
            return {"change_id": change_id, "workflow": None, "profile_key": None, "status": "PENDING",
                    "current_stage": None, "stages": [], "unmet_gates": [], "ready_tasks": [], "blocked_tasks": [],
                    "waiting_tasks": [], "complete_tasks": []}
        tasks = self.changes.list_tasks_for_change(change_id)
        stage_reqs = self.catalog.profile_stages(run["profile_key"])

        # unmet_gates deliberately covers EVERY currently-required-and-
        # incomplete stage's gates, not only current_stage's -- a caller
        # asking "what's left before this Change can finish" wants the
        # whole remaining gap, not just the next single step.
        stages_out = []
        unmet_gates: list[str] = []
        required_incomplete: list[str] = []
        for sr in stage_reqs:
            requirement = sr["requirement"]
            if requirement == "REQUIRED_IF":
                requirement = "REQUIRED" if self._condition_met(sr["condition_key"], change_id, tasks) else "NOT_APPLICABLE"
            if requirement == "NOT_APPLICABLE":
                stages_out.append({"stage": sr["stage_key"], "requirement": "NOT_APPLICABLE", "gates": {}, "complete": True})
                continue
            gate_keys = GATES_BY_STAGE.get(sr["stage_key"], [])
            gate_results = {gk: self._evaluate_gate(gk, change_id, tasks) for gk in gate_keys}
            # ANALYSIS/PLANNING/BUILD have no dedicated GateRequirement
            # (no clean single existing-truth source beyond "Tasks
            # exist" itself -- REVIEW/VERIFY's own gates already require
            # real Task evidence downstream, so nothing here is a
            # fabricated signal): a gate-less required stage is complete
            # once at least one Task has actually been attached.
            complete = all(gate_results.values()) if gate_results else bool(tasks)
            stages_out.append({"stage": sr["stage_key"], "requirement": requirement, "gates": gate_results, "complete": complete})
            if requirement == "REQUIRED" and not complete:
                required_incomplete.append(sr["stage_key"])
                unmet_gates.extend(gk for gk, met in gate_results.items() if not met)

        current_stage = required_incomplete[0] if required_incomplete else None

        readiness = [self.dependencies.readiness(t["id"], self.decision) for t in tasks]
        ready_tasks = [r["task_id"] for r in readiness if r["readiness"] == "READY"]
        waiting_tasks = [r["task_id"] for r in readiness if r["readiness"] == "WAITING_DEPENDENCY"]
        blocked_tasks = [r["task_id"] for r in readiness if r["readiness"] == "BLOCKED"]
        complete_tasks = [r["task_id"] for r in readiness if r["readiness"] == "COMPLETE"]

        # Deploy-failure detection (bounded, grounded): the most recent
        # deployment for a repo this Change touches is FAILED/
        # ROLLBACK_FAILED with no later VERIFIED one for that repo.
        failed_deploy = False
        if "DEPLOY" in [sr["stage_key"] for sr in stage_reqs]:
            repo_ids = {r["repository_id"] for t in tasks for r in self.db.all(
                "SELECT repository_id FROM merge_records WHERE task_id=? AND required=1", (t["id"],))}
            for rid in repo_ids:
                latest = self.db.one(
                    "SELECT status FROM deployments WHERE repository_id=? AND environment='DEV' "
                    "AND task_id IN (SELECT id FROM tasks WHERE change_id=?) ORDER BY id DESC LIMIT 1",
                    (rid, change_id))
                if latest and latest["status"] in ("FAILED", "ROLLBACK_FAILED"):
                    failed_deploy = True

        # E4.12: an unresolved WHAT-level Plan decision outranks every
        # other signal -- no further execution/verification truth
        # matters until a human resolves what is actually being built.
        if self.human_decisions_pending and self.human_decisions_pending(change_id):
            status = "WAITING_HUMAN"
        elif blocked_tasks or any(self.decision.evaluate(t["id"])["blocking_reasons"] for t in tasks):
            status = "BLOCKED"
        elif failed_deploy:
            status = "FAILED"
        elif not required_incomplete:
            status = "COMPLETE"
        elif current_stage == "HUMAN_ACCEPTANCE":
            status = "WAITING_HUMAN"
        elif not tasks:
            status = "PENDING"
        else:
            status = "ACTIVE"

        return {
            "change_id": change_id, "workflow": run, "profile_key": run["profile_key"], "status": status,
            "current_stage": current_stage, "stages": stages_out, "unmet_gates": unmet_gates,
            "ready_tasks": ready_tasks, "waiting_tasks": waiting_tasks, "blocked_tasks": blocked_tasks,
            "complete_tasks": complete_tasks,
        }

    def list_ready_tasks(self, change_id: int) -> list[int]:
        return self.evaluate_workflow(change_id)["ready_tasks"]

    def list_unmet_gates(self, change_id: int) -> list[str]:
        return self.evaluate_workflow(change_id)["unmet_gates"]
