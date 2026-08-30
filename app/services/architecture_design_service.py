from __future__ import annotations
import hashlib
import json
from pathlib import Path

"""Architecture & Technical/UI Design Lifecycle (Phase E6): Approved
Spec -> Architecture Analysis -> Architecture Review -> Technical Design
-> UI/UX Design (when applicable) -> Design Review -> DESIGN_READY.

CRITICAL SEPARATION, enforced by construction across six classes,
mirroring E4/E5's own AUTHOR/ARTIFACT/REVIEWER/APPROVAL separation
exactly (E6's own instruction: keep ARCHITECTURE ANALYSIS, ARCHITECTURE
ARTIFACT, TECHNICAL DESIGN, UI/UX DESIGN, DESIGN REVIEW, TASK
IMPLEMENTATION separate):

  ARCHITECTURE ANALYSIS  -- ArchitectureAnalysisService. One bounded,
    stateless, tool-less PlannerAgentInvoker.invoke() call (the exact
    same shared mechanism E4/E5 use) that returns STRUCTURED data,
    stored verbatim as an ARCHITECTURE_ANALYSIS WorkProduct plus zero or
    more proposed ADR WorkProducts. Never asked to modify source.

  ARCHITECTURE REVIEW    -- ArchitectureReviewService. A COMPLETELY
    SEPARATE invoke() call (a fresh, brand-new subprocess) that reviews
    the analysis independently and returns PASS/NEEDS_REFINEMENT/
    HUMAN_DECISION_REQUIRED/REJECT. Only a PASS ever moves the analysis
    (and any PROPOSED ADRs it produced) to APPROVED.

  TECHNICAL DESIGN / UI/UX DESIGN -- TechnicalDesignService/
    UiUxDesignService. Each its own separate invocation, consuming the
    APPROVED (or, if architecture was never required for this Change,
    absent) architecture context plus the approved spec -- never
    authorized to touch specs/ or source.

  DESIGN REVIEW           -- DesignReviewService. Another separate
    invocation reviewing TechnicalDesign + UI/UX Design together across
    eight explicit dimensions, with a DESIGN_SPEC_CONFLICT outcome
    distinct from REJECT for the specific case "this design cannot
    satisfy the approved spec as written."

  TASK IMPLEMENTATION     -- untouched. E6 never generates an
    implementation Task itself (E6.18) -- once DESIGN_READY, the
    Planner (E4) is (re)invoked exactly as before; PlannerContextBuilder
    is extended (see app/services/planner_service.py) to see these
    WorkProducts, never the other way around.

Every agent invocation here is tool-less (--tools "", the same
PlannerAgentInvoker every E4/E5 role already uses) -- E6.21's own
instruction that an architecture/design agent must never write source
files. ArchitectureContextBuilder's repository inventory is assembled
by ProjectFlow's own Python code (a bounded, non-recursive, read-only
directory listing) and handed to the model as inert text; the model
itself is never given live tool/filesystem access to expand on it.

No new database table exists for this phase (E6.15's own instruction:
"prefer WorkProduct relationships plus a thin DesignService"). Every
artifact is a typed WorkProduct (ARCHITECTURE_ANALYSIS/ADR/
TECHNICAL_DESIGN/UI_UX_DESIGN/ARCHITECTURE_REVIEW/DESIGN_REVIEW --
already-declared kinds, see work_product_service.py); status DRAFT ->
APPROVED/REJECTED is the one state transition a review verdict ever
causes (WorkProductService.set_status); human decisions reuse
HumanDecisionService with subject_type='work_product' (E6.14, no second
decision system); "which architecture analysis governed this design"
is a TraceService link, not a new foreign key."""

from app.services.human_decisions import HumanDecisionService

ARCHITECTURE_CLASSIFICATIONS = (
    "NO_ARCHITECTURE_CHANGE", "LOCAL_ARCHITECTURE_CHANGE",
    "ARCHITECTURE_REFINEMENT", "ARCHITECTURE_BREAKING_CHANGE",
)
REVIEW_VERDICTS = ("PASS", "NEEDS_REFINEMENT", "HUMAN_DECISION_REQUIRED", "REJECT")
DESIGN_REVIEW_VERDICTS = REVIEW_VERDICTS + ("DESIGN_SPEC_CONFLICT",)
CONFLICT_CLASSIFICATIONS = ("REFINABLE", "HUMAN_SPEC_CHANGE_REQUIRED")

# Directories a bounded, non-recursive repository inventory always skips
# -- generated/vendored/VCS noise, never real product structure.
_INVENTORY_SKIP = {".git", ".worktrees", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".pytest_cache"}


class ArchitectureDesignError(ValueError):
    pass


# ===================================================================
# Structured output schemas -- each role its own schema (matches
# PlannerAgentInvoker.invoke()'s contract; see E4/E5).
# ===================================================================
_HUMAN_DECISION_BLOCK = {
    "type": "array",
    "items": {
        "type": "object", "required": ["question", "reason"],
        "properties": {"question": {"type": "string"}, "reason": {"type": "string"},
                        "decision_type": {"type": "string", "enum": [
                            "ARCHITECTURE_BREAKING_CHANGE", "PRODUCT_TRADEOFF", "SECURITY_BOUNDARY",
                            "DATA_OWNERSHIP", "USER_WORKFLOW", "OTHER"]}},
    },
}

ADR_BLOCK = {
    "type": "array",
    "items": {
        "type": "object", "required": ["title", "decision"],
        "properties": {
            "title": {"type": "string"},
            "status": {"type": "string", "enum": ["PROPOSED", "ACCEPTED"]},
            "context": {"type": "string"},
            "decision": {"type": "string"},
            "alternatives": {"type": "array", "items": {"type": "string"}},
            "consequences": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "migration_impact": {"type": "string"},
            "rollback_implications": {"type": "string"},
            "related_requirements": {"type": "array", "items": {"type": "string"}},
        },
    },
}

ARCHITECTURE_ANALYSIS_JSON_SCHEMA = {
    "type": "object",
    "required": ["affected_components", "classification"],
    "properties": {
        "affected_components": {"type": "array", "items": {"type": "string"}},
        "existing_boundaries": {"type": "array", "items": {"type": "string"}},
        "proposed_boundary_changes": {"type": "array", "items": {"type": "string"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "integrations": {"type": "array", "items": {"type": "string"}},
        "data_ownership_impacts": {"type": "array", "items": {"type": "string"}},
        "api_impacts": {"type": "array", "items": {"type": "string"}},
        "persistence_impacts": {"type": "array", "items": {"type": "string"}},
        "runtime_impacts": {"type": "array", "items": {"type": "string"}},
        "deployment_impacts": {"type": "array", "items": {"type": "string"}},
        "security_impacts": {"type": "array", "items": {"type": "string"}},
        "compatibility_impacts": {"type": "array", "items": {"type": "string"}},
        "migration_required": {"type": "boolean"},
        "rollback_considerations": {"type": "array", "items": {"type": "string"}},
        "observability_impacts": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "architecture_decisions_needed": {"type": "array", "items": {"type": "string"}},
        "classification": {"type": "string", "enum": list(ARCHITECTURE_CLASSIFICATIONS)},
        "classification_rationale": {"type": "string"},
        "adrs": ADR_BLOCK,
        "human_decisions": _HUMAN_DECISION_BLOCK,
    },
}

ARCHITECTURE_REVIEW_JSON_SCHEMA = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string", "enum": list(REVIEW_VERDICTS)},
        "findings": {
            "type": "array",
            "items": {
                "type": "object", "required": ["category", "description"],
                "properties": {
                    "category": {"type": "string", "enum": [
                        "requirement_coverage", "architectural_consistency", "unnecessary_complexity",
                        "boundary_correctness", "dependency_direction", "data_ownership", "security",
                        "compatibility", "migration_feasibility", "rollback_feasibility",
                        "operational_impact", "observability", "scope_creep"]},
                    "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                    "description": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
            },
        },
        "human_decisions": _HUMAN_DECISION_BLOCK,
    },
}

TECHNICAL_DESIGN_JSON_SCHEMA = {
    "type": "object",
    "required": ["design_summary", "components_to_change"],
    "properties": {
        "design_summary": {"type": "string"},
        "components_to_change": {"type": "array", "items": {"type": "string"}},
        "responsibilities": {"type": "array", "items": {"type": "string"}},
        "interfaces": {"type": "array", "items": {"type": "string"}},
        "api_contracts": {"type": "array", "items": {"type": "string"}},
        "data_model_changes": {"type": "array", "items": {"type": "string"}},
        "state_transitions": {"type": "array", "items": {"type": "string"}},
        "validation_rules": {"type": "array", "items": {"type": "string"}},
        "failure_modes": {"type": "array", "items": {"type": "string"}},
        "error_handling": {"type": "array", "items": {"type": "string"}},
        "concurrency_idempotency": {"type": "array", "items": {"type": "string"}},
        "migration_plan": {"type": "string"},
        "backward_compatibility": {"type": "string"},
        "rollback_strategy": {"type": "string"},
        "observability": {"type": "array", "items": {"type": "string"}},
        "configuration_changes": {"type": "array", "items": {"type": "string"}},
        "performance_considerations": {"type": "array", "items": {"type": "string"}},
        "security_considerations": {"type": "array", "items": {"type": "string"}},
        "implementation_constraints": {"type": "array", "items": {"type": "string"}},
        "test_implications": {"type": "array", "items": {"type": "string"}},
        # ProjectFlow independently RE-DERIVES uncovered_requirements
        # against the real SpecRegistry (E6.9) -- covered_requirements
        # is the model's own claim, never trusted as the final word.
        "covered_requirements": {"type": "array", "items": {"type": "string"}},
    },
}

UI_UX_DESIGN_JSON_SCHEMA = {
    "type": "object",
    "required": ["user_goals", "user_flows"],
    "properties": {
        "user_goals": {"type": "array", "items": {"type": "string"}},
        "user_flows": {"type": "array", "items": {"type": "string"}},
        "screens": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "purpose": {"type": "string"}}}},
        "navigation": {"type": "array", "items": {"type": "string"}},
        "interaction_rules": {"type": "array", "items": {"type": "string"}},
        "loading_states": {"type": "array", "items": {"type": "string"}},
        "empty_states": {"type": "array", "items": {"type": "string"}},
        "error_states": {"type": "array", "items": {"type": "string"}},
        "permission_states": {"type": "array", "items": {"type": "string"}},
        "offline_states": {"type": "array", "items": {"type": "string"}},
        "validation_feedback": {"type": "array", "items": {"type": "string"}},
        "accessibility_considerations": {"type": "array", "items": {"type": "string"}},
        "responsive_considerations": {"type": "array", "items": {"type": "string"}},
        "edge_cases": {"type": "array", "items": {"type": "string"}},
        "acceptance_mapping": {"type": "array", "items": {"type": "object", "properties": {
            "acceptance_id": {"type": "string"}, "covered_by": {"type": "string"}}}},
    },
}

DESIGN_REVIEW_JSON_SCHEMA = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string", "enum": list(DESIGN_REVIEW_VERDICTS)},
        "conflict_classification": {"type": "string", "enum": list(CONFLICT_CLASSIFICATIONS)},
        "findings": {
            "type": "array",
            "items": {
                "type": "object", "required": ["dimension", "description"],
                "properties": {
                    "dimension": {"type": "string", "enum": [
                        "SPEC_ALIGNMENT", "ARCHITECTURE_ALIGNMENT", "TECHNICAL_FEASIBILITY",
                        "FAILURE_HANDLING", "TESTABILITY", "UX_CONSISTENCY", "SCOPE", "SECURITY_IMPLICATIONS"]},
                    "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                    "description": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
            },
        },
        "human_decisions": _HUMAN_DECISION_BLOCK,
    },
}

SOFTWARE_ARCHITECT_PREAMBLE = """You are the SOFTWARE ARCHITECT for ProjectFlow, an autonomous software engineering control plane.

Core principles: Human owns intent; Spec owns expected behavior -- you own system boundaries and constraints only. You propose; you never modify source code, and nothing you say is authoritative until an independent Architecture Review passes it.

Assess the structural impact of the Change against the current architecture context below. Be concrete: name real affected components, not generic categories.

Classify the proposed change as exactly one of:
- NO_ARCHITECTURE_CHANGE: isolated implementation inside existing component boundaries.
- LOCAL_ARCHITECTURE_CHANGE: adds an internal service/module without changing ownership/contracts.
- ARCHITECTURE_REFINEMENT: improves boundaries/internal architecture while preserving external contracts.
- ARCHITECTURE_BREAKING_CHANGE: alters service boundaries materially, alters data ownership, changes a security boundary, changes persistence semantics, introduces a destructive migration, materially alters an external API contract, or requires a product-level tradeoff.

Only propose an ADR for a decision genuinely worth remembering (a real architectural tradeoff), never for a trivial implementation detail. If continuing requires choosing between materially different architecture-level outcomes (not an ordinary implementation choice), add it to human_decisions instead of guessing."""

ARCHITECTURE_REVIEWER_PREAMBLE = """You are an INDEPENDENT ARCHITECTURE REVIEWER for ProjectFlow. You are a fresh reviewer with no memory of how this analysis was produced -- judge only what is in front of you.

Review the proposed ArchitectureAnalysis (and any proposed ADRs) below against the approved spec and existing architecture context. Check explicitly for: requirement coverage, architectural consistency, unnecessary complexity, boundary correctness, dependency direction, data ownership, security implications, compatibility, migration feasibility, rollback feasibility, operational impact, observability, and scope creep.

Verdict:
- PASS only if there is nothing worth fixing.
- NEEDS_REFINEMENT for concrete, fixable issues -- describe exactly what to fix.
- HUMAN_DECISION_REQUIRED only if a finding requires a real product-level tradeoff, a security-boundary change, or a data-ownership change a human must decide -- not for ordinary architecture-quality issues.
- REJECT only if the analysis is fundamentally unusable as written."""

TECHNICAL_DESIGNER_PREAMBLE = """You are the TECHNICAL DESIGNER for ProjectFlow, an autonomous software engineering control plane.

Turn the approved spec, architecture analysis, and any ADRs below into a structured technical design a future implementation Task can be planned and built against. You own the proposed technical solution only -- never modify source, never weaken or omit a spec requirement.

List every requirement id (from the approved spec below) your design actually covers in covered_requirements -- be honest; ProjectFlow will independently verify this against the real spec, so an inflated list only produces a false PASS that a later review will catch. Model concrete failure modes, error handling, and (where the change touches persistence or an external contract) a real migration_plan and rollback_strategy -- never leave these as generic placeholders when applicable."""

UI_UX_DESIGNER_PREAMBLE = """You are the UI/UX DESIGNER for ProjectFlow, an autonomous software engineering control plane.

Define user-facing behavior and structure for the approved spec below -- flows, screens, states, accessibility -- never a visual mockup or image (that is explicitly out of scope for this role). Cover loading/empty/error/permission states concretely, not just the happy path. Map each user-facing acceptance criterion to the screen/flow/state that satisfies it in acceptance_mapping."""

DESIGN_REVIEWER_PREAMBLE = """You are an INDEPENDENT DESIGN REVIEWER for ProjectFlow. You are a fresh reviewer with no memory of how this design was produced -- judge only what is in front of you.

Review the Technical Design (and UI/UX Design, if present) below against the original human intent, approved spec, architecture, and ADRs. Check separately across these dimensions: SPEC_ALIGNMENT, ARCHITECTURE_ALIGNMENT, TECHNICAL_FEASIBILITY, FAILURE_HANDLING, TESTABILITY, UX_CONSISTENCY, SCOPE, SECURITY_IMPLICATIONS.

Verdict:
- PASS only if there is nothing worth fixing.
- NEEDS_REFINEMENT for concrete, fixable design issues.
- HUMAN_DECISION_REQUIRED only for a real product-level tradeoff a human must decide.
- REJECT if the design is fundamentally unusable as written.
- DESIGN_SPEC_CONFLICT if the design, as the ONLY way you can see to satisfy it, cannot actually satisfy the approved spec -- then also set conflict_classification: REFINABLE if a different architecture/design choice could still satisfy the spec as written (never propose weakening the spec itself), or HUMAN_SPEC_CHANGE_REQUIRED if satisfying the spec as written is not achievable at all and the spec itself would need to change (that change must go through the separate Spec Lifecycle, never here).

Never suggest weakening or deleting a spec requirement merely to make a design pass review."""


def _bounded_repository_inventory(root, max_entries: int = 60) -> list[dict]:
    """E6.4: 'do not dump the full repository.' A single, non-recursive
    top-level listing ProjectFlow's own Python code assembles -- the
    model never gets live tool/filesystem access to expand on this
    itself (PlannerAgentInvoker always runs with --tools "")."""
    try:
        root = Path(root)
        if not root.is_dir():
            return []
        entries = []
        for p in sorted(root.iterdir(), key=lambda x: x.name):
            if p.name.startswith(".") and p.name not in (".github",):
                continue
            if p.name in _INVENTORY_SKIP:
                continue
            entries.append({"name": p.name, "type": "dir" if p.is_dir() else "file"})
            if len(entries) >= max_entries:
                break
        return entries
    except OSError:
        return []


def design_state_digest(work_products, change_id: int) -> str | None:
    """E6.17: a digest of the CURRENT (non-SUPERSEDED) architecture/
    design WorkProduct state governing a Change -- used by PlannerService
    to detect PLAN_DESIGN_STALE the same way E5.18 detects spec
    baseline drift. None if no design-relevant WorkProduct exists yet
    (a Plan made before any architecture/design work has nothing to be
    stale against)."""
    kinds = ("ARCHITECTURE_ANALYSIS", "ADR", "ARCHITECTURE_REVIEW", "TECHNICAL_DESIGN", "UI_UX_DESIGN", "DESIGN_REVIEW")
    rows = [wp for wp in work_products.list_for_change(change_id) if wp["kind"] in kinds and wp["status"] != "SUPERSEDED"]
    if not rows:
        return None
    fingerprint = sorted((wp["kind"], wp["id"], wp["status"], wp["updated_at"]) for wp in rows)
    return hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()


class _DesignAgentRole:
    """Shared plumbing for every E6 agent role -- same shape as E5's own
    _AgentRole (app/services/spec_lifecycle_service.py), duplicated
    rather than imported so this module stays self-contained (E5's
    class is a private, phase-local helper, not a shared abstraction)."""

    def __init__(self, db, invoker, roles_catalog, role_key):
        self.db = db
        self.invoker = invoker
        self.roles_catalog = roles_catalog
        self.role_key = role_key

    def _check_assignment(self, provider):
        assignment = self.roles_catalog.validate_assignment(provider, self.role_key)
        if not assignment["valid"]:
            raise ArchitectureDesignError(f"Provider '{provider}' cannot act as {self.role_key}: missing {assignment['missing_required_capabilities']}")


# ===================================================================
# Bounded architecture/design context (E6.4)
# ===================================================================
class ArchitectureContextBuilder:
    def __init__(self, db, changes, work_products, trace, roles_catalog, workflow_catalog, workflow_service, specs_root, repo_root):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.trace = trace
        self.roles_catalog = roles_catalog
        self.workflow_catalog = workflow_catalog
        self.workflow_service = workflow_service
        self.specs_root = specs_root
        self.repo_root = repo_root

    def governing_feature_ids(self, change_id: int) -> list[str]:
        return [l["target_id"] for l in self.trace.for_source("change", change_id) if l["target_type"] == "spec_feature"]

    def build(self, change_id: int, project_policy: dict | None = None) -> dict:
        from app.services.spec_registry import SpecRegistry, SpecError
        change = self.changes.get(change_id)
        governing_ids = self.governing_feature_ids(change_id)
        approved_specs = []
        # governing_requirement_ids/governing_specs are deliberately
        # NEVER the "all approved features" fallback below -- they feed
        # deterministic checks (UI/UX applicability, TechnicalDesign
        # requirement coverage) that must never treat an UNRELATED
        # feature elsewhere in the spec catalog as evidence about THIS
        # Change. Empty (not "everything") when no spec_feature trace
        # link exists yet -- the honest answer, not a guess.
        all_requirement_ids: list[str] = []
        governing_specs: list[dict] = []
        try:
            registry = SpecRegistry(self.specs_root).load()
            governing_features = [registry.feature(fid) for fid in governing_ids if registry.feature(fid)]
            # Context-only fallback (LLM prompt material, never a
            # deterministic-check input): with nothing explicitly linked
            # yet, still give the analyst/designer SOME related spec
            # context to reason about, same as E5's SpecAuthorService.
            pool = governing_features or [f for f in registry.features.values() if f.get("status") == "approved"]
            for f in pool:
                if not f:
                    continue
                approved_specs.append({
                    "id": f["id"], "title": f.get("title"), "summary": f.get("summary"), "version": f.get("version"),
                    "requirements": f.get("requirements") or [], "acceptance_criteria": f.get("acceptance_criteria") or [],
                    "invariants": f.get("invariants") or [],
                })
            for f in governing_features:
                if not f:
                    continue
                governing_specs.append({
                    "id": f["id"], "title": f.get("title"), "summary": f.get("summary"),
                    "requirements": f.get("requirements") or [], "acceptance_criteria": f.get("acceptance_criteria") or [],
                })
                all_requirement_ids.extend(r["id"] for r in (f.get("requirements") or []))
        except SpecError:
            pass

        run = self.workflow_service.get_workflow(change_id) if self.workflow_service else None
        profile_key = run["profile_key"] if run else None
        design_kinds = ("ARCHITECTURE_ANALYSIS", "ADR", "ARCHITECTURE_REVIEW", "TECHNICAL_DESIGN", "UI_UX_DESIGN", "DESIGN_REVIEW")
        existing_design_work = [
            {"kind": wp["kind"], "title": wp["title"], "status": wp["status"], "id": wp["id"]}
            for wp in self.work_products.list_for_change(change_id) if wp["kind"] in design_kinds and wp["status"] != "SUPERSEDED"
        ]

        project = None
        if change and change.get("project_id"):
            r = self.db.one("SELECT id,repo_name,repo_path FROM repositories WHERE id=?", (change["project_id"],))
            if r:
                project = {"repository_id": r["id"], "repo_name": r["repo_name"]}

        return {
            "change": {"id": change_id, "title": change.get("title") if change else None,
                       "original_intent": change.get("description") if change else None,
                       "change_type": change.get("change_type") if change else None,
                       "risk_level": change.get("risk_level") if change else None},
            "approved_specs": approved_specs,
            "governing_specs": governing_specs,
            "all_requirement_ids": sorted(set(all_requirement_ids)),
            "project": project,
            "project_policy": project_policy or {},
            "workflow_profile": profile_key,
            "existing_design_work": existing_design_work,
            "available_design_roles": [r["key"] for r in self.roles_catalog.list_roles()
                                        if r["key"] in ("SOFTWARE_ARCHITECT", "TECHNICAL_DESIGNER", "UI_UX_DESIGNER", "REVIEWER")],
            # E6.4: bounded, read-only, non-recursive -- never a repo dump.
            # Documented limitation: this is top-level structure only; no
            # safe recursive/tool-driven repository-analysis path exists
            # yet (PlannerAgentInvoker is deliberately tool-less).
            "repository_top_level": _bounded_repository_inventory(self.repo_root),
        }


# ===================================================================
# Architecture Analysis (E6.3/E6.5/E6.6)
# ===================================================================
class ArchitectureAnalysisService(_DesignAgentRole):
    def __init__(self, db, changes, work_products, trace, invoker, roles_catalog, context_builder: ArchitectureContextBuilder, repo_root):
        super().__init__(db, invoker, roles_catalog, "SOFTWARE_ARCHITECT")
        self.changes = changes
        self.work_products = work_products
        self.trace = trace
        self.context_builder = context_builder
        self.repo_root = repo_root

    def get(self, wp_id: int) -> dict | None:
        wp = self.work_products.get(wp_id)
        return wp if wp and wp["kind"] == "ARCHITECTURE_ANALYSIS" else None

    def current_for_change(self, change_id: int) -> dict | None:
        rows = [wp for wp in self.work_products.list_for_change(change_id)
                if wp["kind"] == "ARCHITECTURE_ANALYSIS" and wp["status"] != "SUPERSEDED"]
        return rows[-1] if rows else None

    def _invoke_and_store(self, change_id, provider, prompt, digest, supersedes_id) -> dict:
        try:
            self._check_assignment(provider)
        except ArchitectureDesignError as exc:
            self.db.event("change", change_id, "ARCHITECTURE_ANALYSIS_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "work_product": None, "message": str(exc)}
        try:
            raw_text = self.invoker.invoke(provider, prompt, ARCHITECTURE_ANALYSIS_JSON_SCHEMA, self.repo_root)
            parsed = json.loads(raw_text)
        except Exception as exc:
            self.db.event("change", change_id, "ARCHITECTURE_ANALYSIS_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED", "work_product": None, "message": str(exc)}
        if not isinstance(parsed, dict) or "affected_components" not in parsed or "classification" not in parsed:
            self.db.event("change", change_id, "ARCHITECTURE_ANALYSIS_OUTPUT_INVALID", "missing required fields")
            return {"outcome": "OUTPUT_INVALID", "work_product": None, "message": "Missing affected_components/classification"}
        classification = (parsed.get("classification") or "").strip().upper()
        if classification not in ARCHITECTURE_CLASSIFICATIONS:
            self.db.event("change", change_id, "ARCHITECTURE_ANALYSIS_OUTPUT_INVALID", f"unknown classification '{classification}'")
            return {"outcome": "OUTPUT_INVALID", "work_product": None, "message": f"Unknown classification '{classification}'"}
        parsed["classification"] = classification

        change = self.changes.get(change_id)
        wp_id = self.work_products.create(
            kind="ARCHITECTURE_ANALYSIS", title=f"Architecture Analysis for {change['title'] if change else change_id}",
            project_id=change.get("project_id") if change else None, change_id=change_id, status="DRAFT",
            content_metadata=parsed, content_digest=digest, supersedes_id=supersedes_id)

        adr_ids = []
        for adr in parsed.get("adrs") or []:
            adr_wp_id = self.work_products.create(
                kind="ADR", title=adr.get("title") or f"ADR for {change['title'] if change else change_id}",
                project_id=change.get("project_id") if change else None, change_id=change_id, status="PROPOSED",
                content_metadata=adr)
            self.trace.link("work_product", wp_id, "work_product", adr_wp_id, relation="PRODUCED")
            adr_ids.append(adr_wp_id)

        hd_ids = []
        for hd in parsed.get("human_decisions") or []:
            hd_ids.append(HumanDecisionService(self.db).create(
                "work_product", wp_id, hd.get("question") or "", hd.get("reason") or "",
                hd.get("decision_type") or "OTHER"))

        task_id = self.db.execute(
            "INSERT INTO tasks(slug,title,description,status,change_id,task_type) VALUES(?,?,?,?,?,?)",
            (f"archanalysis-{change_id}-{wp_id}", f"Architecture Analysis: {change['title'] if change else change_id}", "",
             "ACTIVE", change_id, "ARCHITECTURE_ANALYSIS"))
        self.work_products.link_task(task_id, wp_id, "OUTPUT")
        self.db.event("change", change_id, "ARCHITECTURE_ANALYSIS_CREATED",
                       f"work_product={wp_id} classification={classification} adrs={len(adr_ids)}")
        return {"outcome": "READY", "work_product": self.work_products.get(wp_id), "classification": classification,
                "adr_ids": adr_ids, "human_decision_ids": hd_ids, "task_id": task_id}

    def analyze(self, change_id: int, provider: str = "claude") -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise ArchitectureDesignError("Change not found")
        context = self.context_builder.build(change_id)
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        prompt = SOFTWARE_ARCHITECT_PREAMBLE + "\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True)
        return self._invoke_and_store(change_id, provider, prompt, digest, None)

    def refine(self, analysis_wp_id: int, review_findings: dict, provider: str = "claude") -> dict:
        prior = self.get(analysis_wp_id)
        if not prior:
            raise ArchitectureDesignError("Architecture analysis not found")
        context = self.context_builder.build(prior["change_id"])
        context["prior_analysis"] = json.loads(prior["content_metadata"] or "{}")
        context["review_findings"] = review_findings
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        prompt = (SOFTWARE_ARCHITECT_PREAMBLE +
                  "\n\nYou are REFINING a prior analysis based on independent review findings below. "
                  "Address every finding. Never weaken/omit an already-identified impact merely to make "
                  "review easier.\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True))
        result = self._invoke_and_store(prior["change_id"], provider, prompt, digest, analysis_wp_id)
        if result["outcome"] == "READY":
            self.work_products.set_status(analysis_wp_id, "SUPERSEDED")
        return result


# ===================================================================
# Independent Architecture Review (E6.7) -- a fresh invocation
# ===================================================================
class ArchitectureReviewService(_DesignAgentRole):
    def __init__(self, db, changes, work_products, trace, invoker, roles_catalog, specs_root, repo_root, human_decisions: HumanDecisionService):
        super().__init__(db, invoker, roles_catalog, "REVIEWER")
        self.changes = changes
        self.work_products = work_products
        self.trace = trace
        self.specs_root = specs_root
        self.repo_root = repo_root
        self.human_decision_service = human_decisions

    def review(self, analysis_wp_id: int, provider: str = "claude") -> dict:
        from app.services.spec_registry import SpecRegistry, SpecError
        analysis = self.work_products.get(analysis_wp_id)
        if not analysis or analysis["kind"] != "ARCHITECTURE_ANALYSIS":
            raise ArchitectureDesignError("analysis_wp_id does not reference an ARCHITECTURE_ANALYSIS WorkProduct")
        try:
            self._check_assignment(provider)
        except ArchitectureDesignError as exc:
            self.db.event("change", analysis["change_id"], "ARCHITECTURE_REVIEW_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "verdict": None, "message": str(exc)}

        change = self.changes.get(analysis["change_id"])
        adr_rows = [self.work_products.get(int(l["target_id"])) for l in self.trace.for_source("work_product", analysis_wp_id)
                    if l["target_type"] == "work_product" and l["relation"] == "PRODUCED"]
        approved_specs = []
        try:
            registry = SpecRegistry(self.specs_root).load()
            approved_specs = [{"id": f["id"], "title": f.get("title"), "summary": f.get("summary")}
                               for f in registry.features.values() if f.get("status") == "approved"]
        except SpecError:
            pass
        # Deliberately only the analysis's final structured output --
        # never a "rationale" field (this WorkProduct has none anyway,
        # by construction: ArchitectureAnalysisService never stores one).
        context = {"original_intent": change.get("description") or change.get("title") if change else None,
                   "architecture_analysis": json.loads(analysis["content_metadata"] or "{}"),
                   "proposed_adrs": [json.loads(a["content_metadata"] or "{}") for a in adr_rows if a],
                   "approved_specs": approved_specs}
        prompt = ARCHITECTURE_REVIEWER_PREAMBLE + "\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True)
        try:
            raw_text = self.invoker.invoke(provider, prompt, ARCHITECTURE_REVIEW_JSON_SCHEMA, self.repo_root)
            parsed = json.loads(raw_text)
        except Exception as exc:
            self.db.event("change", analysis["change_id"], "ARCHITECTURE_REVIEW_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED", "verdict": None, "message": str(exc)}
        if not isinstance(parsed, dict) or "verdict" not in parsed or parsed["verdict"] not in REVIEW_VERDICTS:
            self.db.event("change", analysis["change_id"], "ARCHITECTURE_REVIEW_OUTPUT_INVALID", "missing/unknown verdict")
            return {"outcome": "OUTPUT_INVALID", "verdict": None, "message": "Missing or unknown verdict"}

        review_wp_id = self.work_products.create(
            kind="ARCHITECTURE_REVIEW", title=f"Architecture Review: {analysis['title']}",
            project_id=analysis["project_id"], change_id=analysis["change_id"], status="DRAFT",
            content_metadata=parsed)
        task_id = self.db.execute(
            "INSERT INTO tasks(slug,title,description,status,change_id,task_type) VALUES(?,?,?,?,?,?)",
            (f"archreview-{analysis_wp_id}-{review_wp_id}", f"Architecture Review: {analysis['title']}", "",
             "ACTIVE", analysis["change_id"], "ARCHITECTURE_REVIEW"))
        self.work_products.link_task(task_id, review_wp_id, "OUTPUT")

        verdict = parsed["verdict"]
        hd_ids = []
        if verdict == "PASS":
            self.work_products.set_status(analysis_wp_id, "APPROVED")
            for a in adr_rows:
                if a and a["status"] == "PROPOSED":
                    self.work_products.set_status(a["id"], "APPROVED")
        elif verdict == "REJECT":
            self.work_products.set_status(analysis_wp_id, "REJECTED")
        elif verdict == "HUMAN_DECISION_REQUIRED":
            for hd in parsed.get("human_decisions") or []:
                hd_ids.append(self.human_decision_service.create(
                    "work_product", analysis_wp_id, hd.get("question") or "", hd.get("reason") or "",
                    hd.get("decision_type") or "OTHER"))
        self.db.event("change", analysis["change_id"], "ARCHITECTURE_REVIEWED", f"analysis={analysis_wp_id} verdict={verdict}")
        return {"outcome": "REVIEWED", "verdict": verdict, "findings": parsed.get("findings") or [],
                "human_decision_ids": hd_ids, "work_product": self.work_products.get(review_wp_id)}


# ===================================================================
# Technical Design (E6.8/E6.9)
# ===================================================================
class TechnicalDesignService(_DesignAgentRole):
    def __init__(self, db, changes, work_products, trace, invoker, roles_catalog, context_builder: ArchitectureContextBuilder, specs_root, repo_root):
        super().__init__(db, invoker, roles_catalog, "TECHNICAL_DESIGNER")
        self.changes = changes
        self.work_products = work_products
        self.trace = trace
        self.context_builder = context_builder
        self.specs_root = specs_root
        self.repo_root = repo_root

    def get(self, wp_id: int) -> dict | None:
        wp = self.work_products.get(wp_id)
        return wp if wp and wp["kind"] == "TECHNICAL_DESIGN" else None

    def current_for_change(self, change_id: int) -> dict | None:
        rows = [wp for wp in self.work_products.list_for_change(change_id)
                if wp["kind"] == "TECHNICAL_DESIGN" and wp["status"] != "SUPERSEDED"]
        return rows[-1] if rows else None

    def _required_requirement_ids(self, change_id: int) -> list[str]:
        return self.context_builder.build(change_id)["all_requirement_ids"]

    def _coverage(self, change_id: int, covered: list[str]) -> tuple[list[str], list[str]]:
        required = self._required_requirement_ids(change_id)
        covered_set = {c for c in (covered or []) if c in required}
        uncovered = [r for r in required if r not in covered_set]
        return sorted(covered_set), uncovered

    def _invoke_and_store(self, change_id, provider, prompt, digest, supersedes_id) -> dict:
        try:
            self._check_assignment(provider)
        except ArchitectureDesignError as exc:
            self.db.event("change", change_id, "TECHNICAL_DESIGN_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "work_product": None, "message": str(exc)}
        try:
            raw_text = self.invoker.invoke(provider, prompt, TECHNICAL_DESIGN_JSON_SCHEMA, self.repo_root)
            parsed = json.loads(raw_text)
        except Exception as exc:
            self.db.event("change", change_id, "TECHNICAL_DESIGN_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED", "work_product": None, "message": str(exc)}
        if not isinstance(parsed, dict) or "design_summary" not in parsed or "components_to_change" not in parsed:
            self.db.event("change", change_id, "TECHNICAL_DESIGN_OUTPUT_INVALID", "missing required fields")
            return {"outcome": "OUTPUT_INVALID", "work_product": None, "message": "Missing design_summary/components_to_change"}

        covered, uncovered = self._coverage(change_id, parsed.get("covered_requirements"))
        parsed["covered_requirements"] = covered
        parsed["uncovered_requirements"] = uncovered

        change = self.changes.get(change_id)
        wp_id = self.work_products.create(
            kind="TECHNICAL_DESIGN", title=f"Technical Design for {change['title'] if change else change_id}",
            project_id=change.get("project_id") if change else None, change_id=change_id, status="DRAFT",
            content_metadata=parsed, content_digest=digest, supersedes_id=supersedes_id)
        task_id = self.db.execute(
            "INSERT INTO tasks(slug,title,description,status,change_id,task_type) VALUES(?,?,?,?,?,?)",
            (f"techdesign-{change_id}-{wp_id}", f"Technical Design: {change['title'] if change else change_id}", "",
             "ACTIVE", change_id, "TECHNICAL_DESIGN"))
        self.work_products.link_task(task_id, wp_id, "OUTPUT")
        self.db.event("change", change_id, "TECHNICAL_DESIGN_CREATED",
                       f"work_product={wp_id} covered={len(covered)} uncovered={len(uncovered)}")
        return {"outcome": "READY", "work_product": self.work_products.get(wp_id),
                "covered_requirements": covered, "uncovered_requirements": uncovered, "task_id": task_id}

    def design(self, change_id: int, architecture_analysis_wp_id: int | None = None, provider: str = "claude") -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise ArchitectureDesignError("Change not found")
        context = self.context_builder.build(change_id)
        if architecture_analysis_wp_id is not None:
            wp = self.work_products.get(architecture_analysis_wp_id)
            if wp and wp["kind"] == "ARCHITECTURE_ANALYSIS":
                context["architecture_analysis"] = json.loads(wp["content_metadata"] or "{}")
                adr_rows = [self.work_products.get(int(l["target_id"])) for l in self.trace.for_source("work_product", architecture_analysis_wp_id)
                            if l["target_type"] == "work_product" and l["relation"] == "PRODUCED"]
                context["adrs"] = [json.loads(a["content_metadata"] or "{}") for a in adr_rows if a]
        else:
            context["architecture_analysis_limitation"] = "No architecture analysis is available for this Change -- designing directly from the approved spec."
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        prompt = TECHNICAL_DESIGNER_PREAMBLE + "\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True)
        return self._invoke_and_store(change_id, provider, prompt, digest, None)

    def refine(self, design_wp_id: int, review_findings: dict, provider: str = "claude") -> dict:
        prior = self.get(design_wp_id)
        if not prior:
            raise ArchitectureDesignError("Technical design not found")
        context = self.context_builder.build(prior["change_id"])
        context["prior_design"] = json.loads(prior["content_metadata"] or "{}")
        context["review_findings"] = review_findings
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        prompt = (TECHNICAL_DESIGNER_PREAMBLE +
                  "\n\nYou are REFINING a prior design based on independent review findings below. Address every "
                  "finding. Never weaken/omit spec coverage merely to make review easier.\n\nContext:\n" +
                  json.dumps(context, indent=2, sort_keys=True))
        result = self._invoke_and_store(prior["change_id"], provider, prompt, digest, design_wp_id)
        if result["outcome"] == "READY":
            self.work_products.set_status(design_wp_id, "SUPERSEDED")
        return result


# ===================================================================
# UI/UX applicability detection (E6.10) -- deterministic, no LLM call
# ===================================================================
class UiUxApplicabilityService:
    # Word-boundary matched (never a bare substring `in` check) -- short
    # tokens like "ui"/"ux" would otherwise false-positive inside
    # ordinary words (require, build, quick, guide, equip, ...).
    _UI_KEYWORDS = ("ui", "ux", "user interface", "screen", "page", "button", "form", "dashboard",
                     "view", "navigat\\w*", "display", "render", "frontend", "front-end", "browser",
                     "dialog", "modal", "click", "tap", "mobile", "responsive", "accessib\\w*", "widget")
    _UI_KEYWORD_PATTERN = None  # built lazily, class-level cache
    _USER_ACTOR_HINTS = ("user", "operator", "customer", "admin", "visitor", "end user", "end-user")
    _SYSTEM_ACTOR_HINTS = ("system", "job", "worker", "cron", "batch", "internal service", "background")

    def __init__(self, work_products, changes):
        self.work_products = work_products
        self.changes = changes

    @classmethod
    def _keyword_pattern(cls):
        if cls._UI_KEYWORD_PATTERN is None:
            import re
            cls._UI_KEYWORD_PATTERN = re.compile(r"\b(?:" + "|".join(cls._UI_KEYWORDS) + r")\b", re.IGNORECASE)
        return cls._UI_KEYWORD_PATTERN

    def _keyword_hit(self, text: str) -> str | None:
        m = self._keyword_pattern().search(text or "")
        return m.group(0).lower() if m else None

    def detect(self, change_id: int, requirement_analysis: dict | None = None,
               approved_specs: list[dict] | None = None, project_policy: dict | None = None) -> dict:
        evidence: list[str] = []
        policy_override = ((project_policy or {}).get("design") or {}).get("ui_ux_when_user_facing")
        if policy_override is not None:
            return {"applicable": bool(policy_override), "reason": "POLICY_OVERRIDE", "evidence": ["engineering.design.ui_ux_when_user_facing"]}

        existing_ui = [wp for wp in self.work_products.list_for_change(change_id) if wp["kind"] == "UI_UX_DESIGN" and wp["status"] != "SUPERSEDED"]
        if existing_ui:
            return {"applicable": True, "reason": "EXISTING_UI_UX_WORK_PRODUCT", "evidence": [f"work_product={existing_ui[-1]['id']}"]}

        applicable = False
        actors = (requirement_analysis or {}).get("actors") or []
        for actor in actors:
            low = str(actor).lower()
            if any(h in low for h in self._USER_ACTOR_HINTS) and not any(h in low for h in self._SYSTEM_ACTOR_HINTS):
                applicable = True
                evidence.append(f"actor:{actor}")

        texts: list[str] = list((requirement_analysis or {}).get("functional_requirements") or [])
        for spec in approved_specs or []:
            texts.extend(r.get("text", "") for r in (spec.get("requirements") or []))
            texts.extend(a.get("text", "") for a in (spec.get("acceptance_criteria") or []))
        for t in texts:
            hit = self._keyword_hit(str(t))
            if hit:
                applicable = True
                evidence.append(f"keyword:{hit}")

        change = self.changes.get(change_id) if self.changes else None
        if change:
            hit = self._keyword_hit(change.get("description") or "") or self._keyword_hit(change.get("title") or "")
            if hit:
                applicable = True
                evidence.append(f"change_intent_keyword:{hit}")

        reason = "STRUCTURED_EVIDENCE_MATCH" if applicable else "NO_USER_FACING_EVIDENCE"
        return {"applicable": applicable, "reason": reason, "evidence": evidence}


# ===================================================================
# UI/UX Design (E6.11)
# ===================================================================
class UiUxDesignService(_DesignAgentRole):
    def __init__(self, db, changes, work_products, invoker, roles_catalog, context_builder: ArchitectureContextBuilder, specs_root, repo_root):
        super().__init__(db, invoker, roles_catalog, "UI_UX_DESIGNER")
        self.changes = changes
        self.work_products = work_products
        self.context_builder = context_builder
        self.specs_root = specs_root
        self.repo_root = repo_root

    def get(self, wp_id: int) -> dict | None:
        wp = self.work_products.get(wp_id)
        return wp if wp and wp["kind"] == "UI_UX_DESIGN" else None

    def current_for_change(self, change_id: int) -> dict | None:
        rows = [wp for wp in self.work_products.list_for_change(change_id)
                if wp["kind"] == "UI_UX_DESIGN" and wp["status"] != "SUPERSEDED"]
        return rows[-1] if rows else None

    def _invoke_and_store(self, change_id, provider, prompt, digest, supersedes_id) -> dict:
        try:
            self._check_assignment(provider)
        except ArchitectureDesignError as exc:
            self.db.event("change", change_id, "UI_UX_DESIGN_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "work_product": None, "message": str(exc)}
        try:
            raw_text = self.invoker.invoke(provider, prompt, UI_UX_DESIGN_JSON_SCHEMA, self.repo_root)
            parsed = json.loads(raw_text)
        except Exception as exc:
            self.db.event("change", change_id, "UI_UX_DESIGN_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED", "work_product": None, "message": str(exc)}
        if not isinstance(parsed, dict) or "user_goals" not in parsed or "user_flows" not in parsed:
            self.db.event("change", change_id, "UI_UX_DESIGN_OUTPUT_INVALID", "missing required fields")
            return {"outcome": "OUTPUT_INVALID", "work_product": None, "message": "Missing user_goals/user_flows"}

        change = self.changes.get(change_id)
        wp_id = self.work_products.create(
            kind="UI_UX_DESIGN", title=f"UI/UX Design for {change['title'] if change else change_id}",
            project_id=change.get("project_id") if change else None, change_id=change_id, status="DRAFT",
            content_metadata=parsed, content_digest=digest, supersedes_id=supersedes_id)
        task_id = self.db.execute(
            "INSERT INTO tasks(slug,title,description,status,change_id,task_type) VALUES(?,?,?,?,?,?)",
            (f"uiuxdesign-{change_id}-{wp_id}", f"UI/UX Design: {change['title'] if change else change_id}", "",
             "ACTIVE", change_id, "UI_UX_DESIGN"))
        self.work_products.link_task(task_id, wp_id, "OUTPUT")
        self.db.event("change", change_id, "UI_UX_DESIGN_CREATED", f"work_product={wp_id}")
        return {"outcome": "READY", "work_product": self.work_products.get(wp_id), "task_id": task_id}

    def design(self, change_id: int, provider: str = "claude") -> dict:
        change = self.changes.get(change_id)
        if not change:
            raise ArchitectureDesignError("Change not found")
        context = self.context_builder.build(change_id)
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        prompt = UI_UX_DESIGNER_PREAMBLE + "\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True)
        return self._invoke_and_store(change_id, provider, prompt, digest, None)

    def refine(self, design_wp_id: int, review_findings: dict, provider: str = "claude") -> dict:
        prior = self.get(design_wp_id)
        if not prior:
            raise ArchitectureDesignError("UI/UX design not found")
        context = self.context_builder.build(prior["change_id"])
        context["prior_design"] = json.loads(prior["content_metadata"] or "{}")
        context["review_findings"] = review_findings
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        prompt = (UI_UX_DESIGNER_PREAMBLE +
                  "\n\nYou are REFINING a prior design based on independent review findings below. Address every "
                  "finding.\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True))
        result = self._invoke_and_store(prior["change_id"], provider, prompt, digest, design_wp_id)
        if result["outcome"] == "READY":
            self.work_products.set_status(design_wp_id, "SUPERSEDED")
        return result


# ===================================================================
# Independent Design Review (E6.12/E6.13) -- reviews both together
# ===================================================================
class DesignReviewService(_DesignAgentRole):
    def __init__(self, db, changes, work_products, invoker, roles_catalog, specs_root, repo_root, human_decisions: HumanDecisionService):
        super().__init__(db, invoker, roles_catalog, "REVIEWER")
        self.changes = changes
        self.work_products = work_products
        self.specs_root = specs_root
        self.repo_root = repo_root
        self.human_decision_service = human_decisions

    def review(self, technical_design_wp_id: int, ui_ux_design_wp_id: int | None, provider: str = "claude") -> dict:
        from app.services.spec_registry import SpecRegistry, SpecError
        design = self.work_products.get(technical_design_wp_id)
        if not design or design["kind"] != "TECHNICAL_DESIGN":
            raise ArchitectureDesignError("technical_design_wp_id does not reference a TECHNICAL_DESIGN WorkProduct")
        try:
            self._check_assignment(provider)
        except ArchitectureDesignError as exc:
            self.db.event("change", design["change_id"], "DESIGN_REVIEW_ASSIGNMENT_INVALID", str(exc))
            return {"outcome": "ASSIGNMENT_INVALID", "verdict": None, "message": str(exc)}

        change = self.changes.get(design["change_id"])
        ui_wp = self.work_products.get(ui_ux_design_wp_id) if ui_ux_design_wp_id else None
        approved_specs = []
        try:
            registry = SpecRegistry(self.specs_root).load()
            approved_specs = [{"id": f["id"], "title": f.get("title"), "requirements": f.get("requirements"),
                                "acceptance_criteria": f.get("acceptance_criteria")}
                               for f in registry.features.values() if f.get("status") == "approved"]
        except SpecError:
            pass
        arch_wp = self.db.one(
            "SELECT content_metadata FROM work_products WHERE kind='ARCHITECTURE_ANALYSIS' AND change_id=? AND status='APPROVED' ORDER BY id DESC LIMIT 1",
            (design["change_id"],))
        context = {"original_intent": change.get("description") or change.get("title") if change else None,
                   "approved_specs": approved_specs,
                   "architecture_analysis": json.loads(arch_wp["content_metadata"]) if arch_wp else None,
                   "technical_design": json.loads(design["content_metadata"] or "{}"),
                   "ui_ux_design": json.loads(ui_wp["content_metadata"] or "{}") if ui_wp else None}
        prompt = DESIGN_REVIEWER_PREAMBLE + "\n\nContext:\n" + json.dumps(context, indent=2, sort_keys=True)
        try:
            raw_text = self.invoker.invoke(provider, prompt, DESIGN_REVIEW_JSON_SCHEMA, self.repo_root)
            parsed = json.loads(raw_text)
        except Exception as exc:
            self.db.event("change", design["change_id"], "DESIGN_REVIEW_FAILED", str(exc)[:500])
            return {"outcome": "EXECUTION_FAILED", "verdict": None, "message": str(exc)}
        if not isinstance(parsed, dict) or parsed.get("verdict") not in DESIGN_REVIEW_VERDICTS:
            self.db.event("change", design["change_id"], "DESIGN_REVIEW_OUTPUT_INVALID", "missing/unknown verdict")
            return {"outcome": "OUTPUT_INVALID", "verdict": None, "message": "Missing or unknown verdict"}
        verdict = parsed["verdict"]
        conflict_classification = None
        if verdict == "DESIGN_SPEC_CONFLICT":
            # Safe direction on ambiguity: an unclassified conflict is
            # treated as requiring a human, never silently auto-refined
            # (E6.13: never guess this).
            conflict_classification = parsed.get("conflict_classification") or "HUMAN_SPEC_CHANGE_REQUIRED"

        review_wp_id = self.work_products.create(
            kind="DESIGN_REVIEW", title=f"Design Review: {design['title']}",
            project_id=design["project_id"], change_id=design["change_id"], status="DRAFT",
            content_metadata={**parsed, "conflict_classification": conflict_classification})
        task_id = self.db.execute(
            "INSERT INTO tasks(slug,title,description,status,change_id,task_type) VALUES(?,?,?,?,?,?)",
            (f"designreview-{technical_design_wp_id}-{review_wp_id}", f"Design Review: {design['title']}", "",
             "ACTIVE", design["change_id"], "DESIGN_REVIEW"))
        self.work_products.link_task(task_id, review_wp_id, "OUTPUT")

        hd_ids = []
        if verdict == "PASS":
            self.work_products.set_status(technical_design_wp_id, "APPROVED")
            if ui_wp:
                self.work_products.set_status(ui_ux_design_wp_id, "APPROVED")
        elif verdict == "REJECT":
            self.work_products.set_status(technical_design_wp_id, "REJECTED")
            if ui_wp:
                self.work_products.set_status(ui_ux_design_wp_id, "REJECTED")
        elif verdict == "HUMAN_DECISION_REQUIRED":
            for hd in parsed.get("human_decisions") or []:
                hd_ids.append(self.human_decision_service.create(
                    "work_product", technical_design_wp_id, hd.get("question") or "", hd.get("reason") or "",
                    hd.get("decision_type") or "OTHER"))
        elif verdict == "DESIGN_SPEC_CONFLICT" and conflict_classification == "HUMAN_SPEC_CHANGE_REQUIRED":
            hd_ids.append(self.human_decision_service.create(
                "work_product", technical_design_wp_id,
                "The design cannot satisfy the approved spec as written -- does the spec itself need to change?",
                "Independent Design Review reported DESIGN_SPEC_CONFLICT/HUMAN_SPEC_CHANGE_REQUIRED. "
                "Resolving this may require the separate Spec Lifecycle (Phase E5) -- this design can never "
                "silently rewrite the canonical spec.", "HUMAN_SPEC_CHANGE_REQUIRED"))

        self.db.event("change", design["change_id"], "DESIGN_REVIEWED",
                       f"design={technical_design_wp_id} verdict={verdict} conflict={conflict_classification}")
        return {"outcome": "REVIEWED", "verdict": verdict, "conflict_classification": conflict_classification,
                "findings": parsed.get("findings") or [], "human_decision_ids": hd_ids,
                "work_product": self.work_products.get(review_wp_id)}


# ===================================================================
# ArchitectureDesignLifecycleService -- bounded refinement loops (E6.13)
# + read helpers (E6.15) + WorkflowService gate evidence (E6.16)
# ===================================================================
class ArchitectureDesignLifecycleService:
    MAX_ROUNDS = 3

    def __init__(self, db, changes, work_products, trace, context_builder: ArchitectureContextBuilder,
                 architecture_analysis: ArchitectureAnalysisService, architecture_review: ArchitectureReviewService,
                 ui_ux_applicability: UiUxApplicabilityService, technical_design: TechnicalDesignService,
                 ui_ux_design: UiUxDesignService, design_review: DesignReviewService,
                 human_decisions: HumanDecisionService, requirement_analysis_lookup=None,
                 workflow_service=None, project_policy_resolver=None):
        self.db = db
        self.changes = changes
        self.work_products = work_products
        self.trace = trace
        self.context_builder = context_builder
        self.architecture_analysis = architecture_analysis
        self.architecture_review = architecture_review
        self.ui_ux_applicability = ui_ux_applicability
        self.technical_design = technical_design
        self.ui_ux_design = ui_ux_design
        self.design_review = design_review
        self.human_decision_service = human_decisions
        # Optional Callable[[change_id], dict|None] -- E5's
        # RequirementAnalysisService's stored WorkProduct content, reused
        # (never re-derived) as UI/UX-applicability evidence when
        # available. None is fine (a Change with no requirement analysis
        # yet still gets a real answer from spec text alone).
        self.requirement_analysis_lookup = requirement_analysis_lookup
        # Optional -- used only so the WorkflowService gate hook
        # (design_ready(change_id), a single-arg call, see
        # app/services/workflow_engine.py) can resolve the Change's own
        # profile_key/PROJECT.yaml policy itself rather than requiring
        # every caller to pass them explicitly. Both None is fine (every
        # explicit run_design/status/detect_ui_ux call below still
        # accepts an explicit override for tests).
        self.workflow_service = workflow_service
        self.project_policy_resolver = project_policy_resolver

    def _resolve_profile_key(self, change_id: int) -> str | None:
        if not self.workflow_service:
            return None
        run = self.workflow_service.get_workflow(change_id)
        return run["profile_key"] if run else None

    def _resolve_project_policy(self, change_id: int) -> dict | None:
        if not self.project_policy_resolver:
            return None
        change = self.changes.get(change_id)
        if not change:
            return None
        return self.project_policy_resolver(change)

    # ---- read -----------------------------------------------------
    def current_architecture_analysis(self, change_id: int) -> dict | None:
        return self.architecture_analysis.current_for_change(change_id)

    def current_technical_design(self, change_id: int) -> dict | None:
        return self.technical_design.current_for_change(change_id)

    def current_ui_ux_design(self, change_id: int) -> dict | None:
        return self.ui_ux_design.current_for_change(change_id)

    def adrs_for_analysis(self, analysis_wp_id: int) -> list[dict]:
        return [self.work_products.get(int(l["target_id"])) for l in self.trace.for_source("work_product", analysis_wp_id)
                if l["target_type"] == "work_product" and l["relation"] == "PRODUCED"]

    def design_findings(self, change_id: int) -> list[dict]:
        rows = [wp for wp in self.work_products.list_for_change(change_id) if wp["kind"] in ("ARCHITECTURE_REVIEW", "DESIGN_REVIEW")]
        out = []
        for wp in rows:
            meta = json.loads(wp["content_metadata"] or "{}")
            for f in meta.get("findings") or []:
                out.append({"review_kind": wp["kind"], "review_id": wp["id"], **f})
        return out

    def status(self, change_id: int, project_policy: dict | None = None) -> dict:
        policy = project_policy if project_policy is not None else self._resolve_project_policy(change_id)
        analysis = self.current_architecture_analysis(change_id)
        design = self.current_technical_design(change_id)
        ui = self.current_ui_ux_design(change_id)
        applicability = self.detect_ui_ux(change_id, project_policy=policy)
        return {
            "change_id": change_id,
            "architecture_analysis": analysis, "architecture_ready": self.architecture_ready(change_id),
            "technical_design": design, "ui_ux_design": ui, "ui_ux_applicability": applicability,
            "design_ready": self.design_ready(change_id, project_policy=policy),
        }

    def detect_ui_ux(self, change_id: int, project_policy: dict | None = None) -> dict:
        policy = project_policy if project_policy is not None else self._resolve_project_policy(change_id)
        ra = self.requirement_analysis_lookup(change_id) if self.requirement_analysis_lookup else None
        context = self.context_builder.build(change_id, project_policy=policy)
        return self.ui_ux_applicability.detect(change_id, requirement_analysis=ra,
                                                 approved_specs=context.get("governing_specs"), project_policy=policy)

    # ---- WorkflowService gate evidence (E6.16) ---------------------
    def architecture_ready(self, change_id: int) -> bool:
        analysis = self.current_architecture_analysis(change_id)
        return bool(analysis and analysis["status"] == "APPROVED")

    def design_ready(self, change_id: int, project_policy: dict | None = None, profile_key: str | None = None) -> bool:
        policy = project_policy if project_policy is not None else self._resolve_project_policy(change_id)
        profile_key = profile_key if profile_key is not None else self._resolve_profile_key(change_id)
        design = self.current_technical_design(change_id)
        if not design or design["status"] != "APPROVED":
            return False
        applicability = self.detect_ui_ux(change_id, project_policy=policy)
        if applicability["applicable"]:
            ui = self.current_ui_ux_design(change_id)
            if not ui or ui["status"] != "APPROVED":
                return False
        if self.human_decision_service.pending_for_change(change_id):
            return False
        meta = json.loads(design["content_metadata"] or "{}")
        if meta.get("uncovered_requirements") and profile_key == "CONTROLLED":
            # E6.9: CONTROLLED must never silently leave a required,
            # externally-observable requirement uncovered. ProjectFlow
            # re-derives this from the real SpecRegistry at design-time
            # (TechnicalDesignService._coverage) -- this is a live
            # re-check, not a trust of the stored review verdict. (Known
            # limitation: there is no "documentation-only requirement"
            # tag in the FeatureSpec schema yet, so this check is
            # currently all-or-nothing for CONTROLLED -- see KNOWN
            # LIMITATIONS in the phase report.)
            return False
        return True

    # ---- bounded architecture loop (E6.5/E6.6/E6.7) ------------------
    def run_architecture(self, change_id: int, provider: str = "claude", max_rounds: int | None = None) -> dict:
        max_rounds = max_rounds if max_rounds is not None else self.MAX_ROUNDS
        result = self.architecture_analysis.analyze(change_id, provider=provider)
        if result["outcome"] != "READY":
            return {"outcome": result["outcome"], "stage": "ARCHITECTURE_ANALYSIS", "message": result.get("message"),
                    "work_product": None, "rounds": 0}
        analysis_id = result["work_product"]["id"]

        for round_no in range(max_rounds):
            review = self.architecture_review.review(analysis_id, provider=provider)
            if review["outcome"] != "REVIEWED":
                return {"outcome": review["outcome"], "stage": "ARCHITECTURE_REVIEW", "message": review.get("message"),
                        "work_product": self.work_products.get(analysis_id), "rounds": round_no}
            verdict = review["verdict"]
            if verdict == "PASS":
                return {"outcome": "ARCHITECTURE_READY", "work_product": self.work_products.get(analysis_id),
                        "verdict": verdict, "classification": result["classification"], "rounds": round_no + 1}
            if verdict in ("HUMAN_DECISION_REQUIRED", "REJECT"):
                return {"outcome": "HUMAN_DECISION_REQUIRED" if verdict == "HUMAN_DECISION_REQUIRED" else "REJECTED",
                        "work_product": self.work_products.get(analysis_id), "verdict": verdict, "rounds": round_no + 1}
            if round_no + 1 >= max_rounds:
                return {"outcome": "NEEDS_REFINEMENT", "work_product": self.work_products.get(analysis_id),
                        "verdict": verdict, "rounds": round_no + 1, "message": f"Refinement round limit ({max_rounds}) reached"}
            refined = self.architecture_analysis.refine(analysis_id, review, provider=provider)
            if refined["outcome"] != "READY":
                return {"outcome": refined["outcome"], "stage": "ARCHITECTURE_REFINE", "message": refined.get("message"),
                        "work_product": self.work_products.get(analysis_id), "rounds": round_no + 1}
            analysis_id = refined["work_product"]["id"]

        return {"outcome": "NEEDS_REFINEMENT", "work_product": self.work_products.get(analysis_id), "rounds": max_rounds}

    # ---- bounded design loop (E6.10/E6.11/E6.12/E6.13) ---------------
    def run_design(self, change_id: int, provider: str = "claude", max_rounds: int | None = None,
                    project_policy: dict | None = None) -> dict:
        max_rounds = max_rounds if max_rounds is not None else self.MAX_ROUNDS
        analysis = self.current_architecture_analysis(change_id)
        architecture_analysis_wp_id = analysis["id"] if analysis and analysis["status"] == "APPROVED" else None

        design_result = self.technical_design.design(change_id, architecture_analysis_wp_id, provider=provider)
        if design_result["outcome"] != "READY":
            return {"outcome": design_result["outcome"], "stage": "TECHNICAL_DESIGN", "message": design_result.get("message"),
                    "technical_design": None, "ui_ux_design": None, "rounds": 0}
        design_id = design_result["work_product"]["id"]

        applicability = self.detect_ui_ux(change_id, project_policy=project_policy)
        ui_id = None
        if applicability["applicable"]:
            ui_result = self.ui_ux_design.design(change_id, provider=provider)
            if ui_result["outcome"] != "READY":
                return {"outcome": ui_result["outcome"], "stage": "UI_UX_DESIGN", "message": ui_result.get("message"),
                        "technical_design": self.work_products.get(design_id), "ui_ux_design": None,
                        "ui_ux_applicability": applicability, "rounds": 0}
            ui_id = ui_result["work_product"]["id"]

        for round_no in range(max_rounds):
            review = self.design_review.review(design_id, ui_id, provider=provider)
            if review["outcome"] != "REVIEWED":
                return {"outcome": review["outcome"], "stage": "DESIGN_REVIEW", "message": review.get("message"),
                        "technical_design": self.work_products.get(design_id),
                        "ui_ux_design": self.work_products.get(ui_id) if ui_id else None,
                        "ui_ux_applicability": applicability, "rounds": round_no}
            verdict = review["verdict"]
            if verdict == "PASS":
                return {"outcome": "DESIGN_READY", "technical_design": self.work_products.get(design_id),
                        "ui_ux_design": self.work_products.get(ui_id) if ui_id else None,
                        "ui_ux_applicability": applicability, "verdict": verdict, "rounds": round_no + 1}
            if verdict in ("HUMAN_DECISION_REQUIRED", "REJECT"):
                return {"outcome": "HUMAN_DECISION_REQUIRED" if verdict == "HUMAN_DECISION_REQUIRED" else "REJECTED",
                        "technical_design": self.work_products.get(design_id),
                        "ui_ux_design": self.work_products.get(ui_id) if ui_id else None,
                        "ui_ux_applicability": applicability, "verdict": verdict, "rounds": round_no + 1}
            if verdict == "DESIGN_SPEC_CONFLICT":
                if review["conflict_classification"] == "REFINABLE" and round_no + 1 < max_rounds:
                    pass  # fall through to refine below, same as NEEDS_REFINEMENT
                else:
                    return {"outcome": "DESIGN_SPEC_CONFLICT", "conflict_classification": review["conflict_classification"],
                            "technical_design": self.work_products.get(design_id),
                            "ui_ux_design": self.work_products.get(ui_id) if ui_id else None,
                            "ui_ux_applicability": applicability, "verdict": verdict, "rounds": round_no + 1}
            if round_no + 1 >= max_rounds:
                return {"outcome": "NEEDS_REFINEMENT", "technical_design": self.work_products.get(design_id),
                        "ui_ux_design": self.work_products.get(ui_id) if ui_id else None,
                        "ui_ux_applicability": applicability, "verdict": verdict, "rounds": round_no + 1,
                        "message": f"Refinement round limit ({max_rounds}) reached"}
            refined = self.technical_design.refine(design_id, review, provider=provider)
            if refined["outcome"] != "READY":
                return {"outcome": refined["outcome"], "stage": "TECHNICAL_DESIGN_REFINE", "message": refined.get("message"),
                        "technical_design": self.work_products.get(design_id),
                        "ui_ux_design": self.work_products.get(ui_id) if ui_id else None, "rounds": round_no + 1}
            design_id = refined["work_product"]["id"]
            if ui_id:
                ui_refined = self.ui_ux_design.refine(ui_id, review, provider=provider)
                if ui_refined["outcome"] == "READY":
                    ui_id = ui_refined["work_product"]["id"]

        return {"outcome": "NEEDS_REFINEMENT", "technical_design": self.work_products.get(design_id),
                "ui_ux_design": self.work_products.get(ui_id) if ui_id else None, "rounds": max_rounds}
