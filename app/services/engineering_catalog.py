from __future__ import annotations

"""Role & Capability Catalog (Phase E2): a reusable policy/metadata
foundation answering "what engineering roles exist, what capabilities
does each require, what can each agent provider actually do, and is a
given provider<->role assignment valid" -- for later phases (planning,
staffing, orchestration) to build on.

This module is deliberately NOT:

- Application RBAC/security authorization. A provider having a
  capability never means it may act on it without the existing
  human-triggered HTTP action / operator approval this app already
  requires -- Create PR, Merge, Deploy are all still human button
  clicks after this phase, exactly as before. See CAPABILITY vs
  AUTHORIZATION vs APPROVAL in docs/ENGINEERING_ROLES.md.
- A replacement for AGENT_LAUNCHERS (app/launchers.py -- the real
  processes ProjectFlow can actually spawn) or settings.agents (which
  agent NAMES are allowed on a Builder Workspace at all). Both stay
  exactly as they are; this catalog answers a different question (what
  can a provider *do*, in engineering-responsibility terms) layered on
  top of them, never duplicating either.
- Automatic role assignment, staffing, or orchestration (deferred to a
  later phase).

ENGINEERING ROLE vs TASK ROLE (do not confuse): `agent_workspaces.role`
already exists in this codebase and means something else entirely -- a
free-text component/team label an operator types in ("Backend",
"Firmware") used only for sandbox-source naming and prompt context
(see render_agent_prompt's "## ROLE" section). It is NOT touched or
reused by this module; an Engineering Role (BUILDER/REVIEWER/...) is a
completely different, catalog-defined concept."""

SUPPORT_LEVELS = ("SUPPORTED", "PARTIAL", "UNSUPPORTED")
REQUIREMENT_LEVELS = ("REQUIRED", "OPTIONAL")

# ---------------------------------------------------------------- Roles
# category: ANALYSIS | DELIVERY | QUALITY | OPERATIONS
ROLES = {
    "REQUIREMENTS_ANALYST": {
        "name": "Requirements Analyst", "category": "ANALYSIS",
        "description": "Turns a Change's original intent into a structured requirement analysis WorkProduct, before any spec or design work starts.",
    },
    "SPEC_ANALYST": {
        "name": "Spec Analyst", "category": "ANALYSIS",
        "description": "Reads and writes FeatureSpecs against the Spec Layer (specs/) -- requirements, acceptance criteria, and invariants a Task can be gated and verified against (SpecRegistry/SpecGate).",
    },
    "PLANNER": {
        "name": "Planner", "category": "ANALYSIS",
        "description": "Decomposes a Change into concrete Tasks (Change -> Tasks) and records the plan as a WorkProduct. Not autonomous decomposition -- catalog/capability support only in this phase.",
    },
    "BUILDER": {
        "name": "Builder", "category": "DELIVERY",
        "description": "Produces source changes in an isolated Builder Workspace (its own branch/worktree) and submits them for review with real evidence (verification_reports).",
    },
    "REVIEWER": {
        "name": "Reviewer", "category": "QUALITY",
        "description": "Inspects the exact source commit/diff/evidence a Builder submitted and returns PASS, FIX_REQUIRED, or BLOCKED (review_runs).",
    },
    "SECURITY_REVIEWER": {
        "name": "Security Reviewer", "category": "QUALITY",
        "description": "Reviews a change for security impact. ProjectFlow has no dedicated security-review workflow yet -- today this is capability-equivalent to REVIEWER, kept as a distinct catalog role so future policy (e.g. mandatory on a SECURITY_CHANGE Change) can require it without inventing a new mechanism.",
    },
    "QA_VERIFIER": {
        "name": "QA / Verification", "category": "QUALITY",
        "description": "Verifies behavior in an exact runtime/source environment (a Sandbox pinned to a real commit) and records PASS/FAIL evidence (qa_runs/manual_verifications). Most QA in ProjectFlow today is a human operator, not an agent process.",
    },
    "INTEGRATOR": {
        "name": "Integrator", "category": "OPERATIONS",
        "description": "Combines approved source branches into an Integration Workspace, resolves integration conflicts, and qualifies the integrated source (task_integrations/integration_workspaces).",
    },
    "RELEASE_MANAGER": {
        "name": "Release Manager", "category": "OPERATIONS",
        "description": "Builds, promotes, and deploys qualified merged source under deployment policy (DeploymentService). MERGE_PR/DEPLOY_PRODUCTION stay human-gated regardless of this role.",
    },
    # Phase E6 (Architecture & Technical/UI Design Lifecycle): three new
    # roles, justified by real E6 execution only -- no artificial org
    # chart (E6.1's own instruction). ARCHITECTURE_REVIEW/DESIGN_REVIEW
    # deliberately reuse the existing REVIEWER role below rather than
    # adding a fourth: reviewing a proposed architecture/design is the
    # same "inspect independently, return PASS/FIX_REQUIRED-shaped
    # verdict" responsibility REVIEWER already models, just against a
    # different artifact than source code.
    "SOFTWARE_ARCHITECT": {
        "name": "Software Architect", "category": "ANALYSIS",
        "description": "Assesses a Change's structural impact against the current architecture -- affected components, boundaries, dependencies, data ownership, security/compatibility impact -- and records it as an ArchitectureAnalysis WorkProduct, proposing ADRs for decisions worth remembering. Never modifies production code.",
    },
    "TECHNICAL_DESIGNER": {
        "name": "Technical Designer", "category": "ANALYSIS",
        "description": "Turns an approved spec plus architecture analysis/ADRs into a structured TechnicalDesign WorkProduct (interfaces, API/data contracts, failure modes, migration/rollback, requirement trace) a future implementation Task can be planned and built against. Never modifies production code.",
    },
    "UI_UX_DESIGNER": {
        "name": "UI/UX Designer", "category": "ANALYSIS",
        "description": "Defines user-facing behavior and structure -- flows, screens, states, accessibility -- as a UI_UX_DESIGN WorkProduct, only when a Change actually changes user-facing interaction. Behavior/structure only; E6 does not generate visual mockups/screenshots.",
    },
    # Phase E7 (Test Design, Requirement Coverage & Executable Acceptance
    # Mapping): TEST_REVIEWER deliberately not added -- REVIEWER already
    # cleanly serves independent test review (same "inspect independently,
    # return a PASS/NEEDS_REFINEMENT/HUMAN_DECISION_REQUIRED/REJECT-shaped
    # verdict" responsibility it already holds for architecture/design
    # review in E6).
    "TEST_DESIGNER": {
        "name": "Test Designer", "category": "QUALITY",
        "description": "Turns an approved spec (requirements/acceptance criteria/invariants) plus architecture/design into structured TestCaseSpecs that PROVE expected behavior -- never derives expected outcome from current implementation. Records TEST_PLAN/TEST_CASE_SET WorkProducts. Never writes source or test files itself -- that stays a Builder/Test-Implementation Task (E8).",
    },
}

# ------------------------------------------------------------ Capabilities
# category loosely follows the app's own subsystems. sensitivity marks
# the handful of capabilities that stay policy-controlled/human-gated
# no matter which role/provider holds them (E2 section 9).
CAPABILITIES = {
    "READ_REPOSITORY": {"name": "Read repository", "category": "REPOSITORY", "sensitivity": "NORMAL",
        "description": "Read a registered repository's source/worktree."},
    "READ_SPEC": {"name": "Read spec", "category": "SPEC", "sensitivity": "NORMAL",
        "description": "Read FeatureSpecs/requirements/acceptance criteria via SpecRegistry."},
    "WRITE_SPEC": {"name": "Write spec", "category": "SPEC", "sensitivity": "NORMAL",
        "description": "Edit specs/**/*.yaml. SpecRegistry itself never writes -- this is always a human/editor action today."},
    "PLAN_TASK": {"name": "Plan Task", "category": "PLANNING", "sensitivity": "NORMAL",
        "description": "Decompose a Change into Tasks and record the plan."},
    "EDIT_SOURCE": {"name": "Edit source", "category": "BUILD", "sensitivity": "NORMAL",
        "description": "Modify files inside a Builder Workspace's own worktree."},
    "CREATE_COMMIT": {"name": "Create commit", "category": "BUILD", "sensitivity": "NORMAL",
        "description": "Commit changes in a Builder Workspace worktree."},
    "RUN_TESTS": {"name": "Run tests", "category": "BUILD", "sensitivity": "NORMAL",
        "description": "Run the project's automated tests (TestRunner/test_runs)."},
    "READ_TEST_RESULTS": {"name": "Read test results", "category": "BUILD", "sensitivity": "NORMAL",
        "description": "Read recorded test_runs rows."},
    "REVIEW_SOURCE": {"name": "Review source", "category": "REVIEW", "sensitivity": "NORMAL",
        "description": "Inspect a Builder's submitted source at its pinned commit."},
    "REVIEW_DIFF": {"name": "Review diff", "category": "REVIEW", "sensitivity": "NORMAL",
        "description": "Inspect the actual diff/change content, not just the end state."},
    "SUBMIT_REVIEW": {"name": "Submit review result", "category": "REVIEW", "sensitivity": "NORMAL",
        "description": "Record PASS/FIX_REQUIRED/BLOCKED against a review_runs row."},
    "RUN_RUNTIME_VERIFICATION": {"name": "Run runtime verification", "category": "VERIFICATION", "sensitivity": "NORMAL",
        "description": "Exercise the application in a real Sandbox pinned to an exact commit."},
    "RECORD_VERIFICATION": {"name": "Record verification result", "category": "VERIFICATION", "sensitivity": "NORMAL",
        "description": "Record a PASS/FAIL verification result (qa_runs/manual_verifications)."},
    "RESOLVE_CONFLICTS": {"name": "Resolve integration conflicts", "category": "INTEGRATION", "sensitivity": "NORMAL",
        "description": "Merge the latest target branch into an Integration Workspace and resolve conflicts."},
    "CREATE_INTEGRATION": {"name": "Create integration", "category": "INTEGRATION", "sensitivity": "NORMAL",
        "description": "Start a Task Integration once every required Builder Workspace has Review PASS."},
    "PUSH_BRANCH": {"name": "Push branch", "category": "INTEGRATION", "sensitivity": "NORMAL",
        "description": "Push an Integration branch's current HEAD to the GitHub remote."},
    "CREATE_PR": {"name": "Create Pull Request", "category": "RELEASE", "sensitivity": "NORMAL",
        "description": "Open a real GitHub Pull Request from a verified source branch (GitHubMergeService.create_pr)."},
    "READ_CI": {"name": "Read CI status", "category": "RELEASE", "sensitivity": "NORMAL",
        "description": "Read PR/CI/mergeability status (GitHubMergeService.pr_status)."},
    "MERGE_PR": {"name": "Merge Pull Request", "category": "RELEASE", "sensitivity": "SENSITIVE",
        "description": "Merge a Pull Request into main via the GitHub API. Policy-controlled/human-gated; no provider is SUPPORTED for this by default regardless of role."},
    "BUILD_ARTIFACT": {"name": "Build artifact", "category": "RELEASE", "sensitivity": "NORMAL",
        "description": "Build a deployable artifact/image (DeploymentService)."},
    "DEPLOY_DEV": {"name": "Deploy to DEV", "category": "RELEASE", "sensitivity": "NORMAL",
        "description": "Deploy merged source to the DEV environment (POST /api/tasks/{tid}/deployments)."},
    "DEPLOY_TEST": {"name": "Deploy to TEST", "category": "RELEASE", "sensitivity": "NORMAL",
        "description": "Deploy to a TEST environment. PROJECT.yaml already reserves a deployment.test block; no route exposes it yet."},
    "DEPLOY_PRODUCTION": {"name": "Deploy to production", "category": "RELEASE", "sensitivity": "SENSITIVE",
        "description": "Deploy to production. PROJECT.yaml already reserves a deployment.production block; no route exposes it yet. Policy-controlled/human-gated."},
    "ROLLBACK_DEPLOYMENT": {"name": "Rollback deployment", "category": "RELEASE", "sensitivity": "SENSITIVE",
        "description": "Roll back a deployment to its last VERIFIED artifact."},
    "USE_INTERACTIVE_TERMINAL": {"name": "Use interactive terminal", "category": "TERMINAL", "sensitivity": "NORMAL",
        "description": "Drive a real PTY session in INTERACTIVE mode (AgentSessionManager)."},
    "USE_BROWSER": {"name": "Use browser terminal", "category": "TERMINAL", "sensitivity": "NORMAL",
        "description": "Open a live agent session in the browser (Open in Web / Live Agents)."},
    "READ_EVIDENCE": {"name": "Read evidence", "category": "EVIDENCE", "sensitivity": "NORMAL",
        "description": "Read recorded evidence for a Task (EvidenceStore)."},
    "WRITE_WORK_PRODUCT": {"name": "Write work product", "category": "EVIDENCE", "sensitivity": "NORMAL",
        "description": "Create/record a WorkProduct (WorkProductService)."},
    # Phase E6: reasoning-only capabilities -- every one of these is
    # structured LLM reasoning over bounded context (E6.21: "prefer
    # tool-less structured invocation"), never a deployment or
    # release-sensitive operation (E6.1's own instruction: "Do NOT
    # associate any design role with deployment or sensitive release
    # capabilities").
    "REPOSITORY_ANALYSIS": {"name": "Repository analysis", "category": "ARCHITECTURE", "sensitivity": "NORMAL",
        "description": "Bounded, explicitly read-only structural inspection (e.g. a top-level module/file inventory) -- never an unrestricted Builder shell; see ArchitectureContextBuilder."},
    "ARCHITECTURE_REASONING": {"name": "Architecture reasoning", "category": "ARCHITECTURE", "sensitivity": "NORMAL",
        "description": "Reason about component boundaries, dependencies, and structural impact of a proposed Change."},
    "DEPENDENCY_ANALYSIS": {"name": "Dependency analysis", "category": "ARCHITECTURE", "sensitivity": "NORMAL",
        "description": "Identify upstream/downstream dependencies and integration points a proposed change would affect."},
    "TECHNICAL_DESIGN": {"name": "Technical design", "category": "DESIGN", "sensitivity": "NORMAL",
        "description": "Produce a structured technical design (interfaces, contracts, failure modes) from an approved spec/architecture."},
    "API_DESIGN": {"name": "API design", "category": "DESIGN", "sensitivity": "NORMAL",
        "description": "Design API/interface contracts for a proposed change."},
    "DATA_DESIGN": {"name": "Data design", "category": "DESIGN", "sensitivity": "NORMAL",
        "description": "Design data model/persistence changes, including migration and rollback shape."},
    "UI_UX_DESIGN": {"name": "UI/UX design", "category": "DESIGN", "sensitivity": "NORMAL",
        "description": "Define user-facing flows, screens, states, and acceptance mapping. Behavior/structure only, never visual mockup generation."},
    "FAILURE_MODE_ANALYSIS": {"name": "Failure mode analysis", "category": "DESIGN", "sensitivity": "NORMAL",
        "description": "Identify failure modes and error-handling requirements for a proposed architecture/design."},
    "MIGRATION_DESIGN": {"name": "Migration design", "category": "DESIGN", "sensitivity": "NORMAL",
        "description": "Design a migration/rollback plan for a persistence or contract change. Never itself runs a migration -- BUILD-stage execution stays a Builder/RELEASE_MANAGER responsibility."},
    "ARCHITECTURE_REVIEW": {"name": "Architecture/design review", "category": "ARCHITECTURE", "sensitivity": "NORMAL",
        "description": "Independently review a proposed ArchitectureAnalysis or TechnicalDesign/UI_UX_DESIGN and return a PASS/NEEDS_REFINEMENT/HUMAN_DECISION_REQUIRED/REJECT-shaped verdict. Reused for both architecture review and design review -- same responsibility shape as REVIEWER's existing SUBMIT_REVIEW, against a different artifact."},
    # Phase E7: reasoning-only, never deployment/release-sensitive.
    "TEST_DESIGN": {"name": "Test design", "category": "QUALITY", "sensitivity": "NORMAL",
        "description": "Produce structured TestCaseSpecs proving approved requirements/acceptance criteria -- never derives expected behavior from current implementation."},
    "ACCEPTANCE_TEST_DESIGN": {"name": "Acceptance test design", "category": "QUALITY", "sensitivity": "NORMAL",
        "description": "Design TestCaseSpecs that explicitly prove one or more AcceptanceCriteria."},
    "INVARIANT_TEST_DESIGN": {"name": "Invariant test design", "category": "QUALITY", "sensitivity": "NORMAL",
        "description": "Design a proof strategy (integration test, report-query test, runtime invariant verification, ...) for a FeatureSpec invariant."},
    "REGRESSION_TEST_DESIGN": {"name": "Regression test design", "category": "QUALITY", "sensitivity": "NORMAL",
        "description": "Design TestCaseSpecs guarding previously-fixed or previously-approved behavior against future regression."},
    "FAILURE_CASE_DESIGN": {"name": "Failure case design", "category": "QUALITY", "sensitivity": "NORMAL",
        "description": "Design negative/failure/boundary TestCaseSpecs -- expected behavior under invalid input, denied permission, or a failed dependency."},
    "COVERAGE_ANALYSIS": {"name": "Coverage analysis", "category": "QUALITY", "sensitivity": "NORMAL",
        "description": "Analyze which requirements/acceptance criteria/invariants a set of TestCaseSpecs actually proves. The deterministic recomputation (RequirementCoverageService) never trusts this as authoritative -- it always re-derives real coverage from IDs itself."},
    "TEST_REVIEW": {"name": "Test review", "category": "QUALITY", "sensitivity": "NORMAL",
        "description": "Independently review a Test Plan/TestCaseSpecs and return a PASS/NEEDS_REFINEMENT/HUMAN_DECISION_REQUIRED/REJECT-shaped verdict -- same responsibility shape as ARCHITECTURE_REVIEW, against test design instead."},
}

# --------------------------------------------------------- Role -> Capability
# Never includes MERGE_PR/DEPLOY_PRODUCTION as REQUIRED anywhere (E2
# section 7): those stay optional, policy-controlled capabilities even
# for RELEASE_MANAGER.
ROLE_CAPABILITIES = {
    "REQUIREMENTS_ANALYST": {"REQUIRED": ["READ_REPOSITORY", "WRITE_WORK_PRODUCT"], "OPTIONAL": ["READ_SPEC"]},
    "SPEC_ANALYST": {"REQUIRED": ["READ_REPOSITORY", "READ_SPEC", "WRITE_SPEC", "WRITE_WORK_PRODUCT"], "OPTIONAL": []},
    "PLANNER": {"REQUIRED": ["READ_REPOSITORY", "READ_SPEC", "PLAN_TASK", "WRITE_WORK_PRODUCT"], "OPTIONAL": []},
    "BUILDER": {"REQUIRED": ["READ_REPOSITORY", "EDIT_SOURCE", "CREATE_COMMIT", "RUN_TESTS"],
                "OPTIONAL": ["READ_SPEC", "USE_INTERACTIVE_TERMINAL", "USE_BROWSER", "WRITE_WORK_PRODUCT"]},
    "REVIEWER": {"REQUIRED": ["READ_REPOSITORY", "REVIEW_SOURCE", "REVIEW_DIFF", "READ_TEST_RESULTS", "SUBMIT_REVIEW"],
                 "OPTIONAL": ["READ_SPEC", "ARCHITECTURE_REVIEW", "TEST_REVIEW"]},
    "SECURITY_REVIEWER": {"REQUIRED": ["READ_REPOSITORY", "REVIEW_SOURCE", "REVIEW_DIFF", "SUBMIT_REVIEW"],
                           "OPTIONAL": ["READ_SPEC"]},
    "QA_VERIFIER": {"REQUIRED": ["READ_EVIDENCE", "RUN_RUNTIME_VERIFICATION", "RECORD_VERIFICATION"],
                     "OPTIONAL": ["USE_BROWSER"]},
    "INTEGRATOR": {"REQUIRED": ["READ_REPOSITORY", "RESOLVE_CONFLICTS", "CREATE_INTEGRATION", "RUN_TESTS"],
                   "OPTIONAL": ["PUSH_BRANCH", "CREATE_PR", "READ_CI"]},
    "RELEASE_MANAGER": {"REQUIRED": ["BUILD_ARTIFACT", "DEPLOY_DEV", "READ_EVIDENCE"],
                         "OPTIONAL": ["DEPLOY_TEST", "DEPLOY_PRODUCTION", "ROLLBACK_DEPLOYMENT", "MERGE_PR"]},
    "SOFTWARE_ARCHITECT": {"REQUIRED": ["READ_SPEC", "ARCHITECTURE_REASONING", "DEPENDENCY_ANALYSIS", "WRITE_WORK_PRODUCT"],
                            "OPTIONAL": ["READ_REPOSITORY", "REPOSITORY_ANALYSIS", "FAILURE_MODE_ANALYSIS", "MIGRATION_DESIGN"]},
    "TECHNICAL_DESIGNER": {"REQUIRED": ["READ_SPEC", "TECHNICAL_DESIGN", "API_DESIGN", "DATA_DESIGN", "WRITE_WORK_PRODUCT"],
                            "OPTIONAL": ["READ_REPOSITORY", "FAILURE_MODE_ANALYSIS", "MIGRATION_DESIGN"]},
    "UI_UX_DESIGNER": {"REQUIRED": ["READ_SPEC", "UI_UX_DESIGN", "WRITE_WORK_PRODUCT"], "OPTIONAL": ["READ_REPOSITORY"]},
    "TEST_DESIGNER": {"REQUIRED": ["READ_SPEC", "TEST_DESIGN", "COVERAGE_ANALYSIS", "WRITE_WORK_PRODUCT"],
                       "OPTIONAL": ["READ_REPOSITORY", "ACCEPTANCE_TEST_DESIGN", "INVARIANT_TEST_DESIGN",
                                    "REGRESSION_TEST_DESIGN", "FAILURE_CASE_DESIGN"]},
}

# --------------------------------------------------------------- Providers
# Discovered from the real codebase (app/launchers.py AGENT_LAUNCHERS,
# app/config.py Settings.agents default) -- not assumed. codex/claude
# have real, working AGENT_LAUNCHERS entries (a genuine PTY process
# ProjectFlow can spawn); gemini/aider/other are settings.agents-allowed
# NAMES a Builder Workspace may be labeled with, but have no adapter at
# all -- there is no code path that can actually start a session for
# them (AgentSessionManager.start() already raises SessionError
# AGENT_UNSUPPORTED for them today, independent of this catalog).
KNOWN_PROVIDERS = ("codex", "claude", "gemini", "aider", "other")
LAUNCHABLE_PROVIDERS = ("codex", "claude")


def is_launchable_provider(provider: str) -> bool:
    """True only for a provider AGENT_LAUNCHERS can actually spawn a
    real PTY process for."""
    return (provider or "").strip().lower() in LAUNCHABLE_PROVIDERS


def is_known_provider(provider: str) -> bool:
    """True for any name this catalog has an opinion about at all
    (codex/claude/gemini/aider/other), launchable or not. Used to
    decide whether a free-text reviewer_agent/tester_agent value even
    represents an AGENT actor worth checking against the catalog, vs a
    HUMAN/SYSTEM one (E2 section 15) -- an arbitrary human name (e.g.
    "alice") is never in this set, so it is never checked; a named-but-
    unsupported provider (e.g. "gemini") IS checked, so its rejection
    is still visible even though it was never launchable in the first
    place. Catalog-level support only, no new actor_kind column."""
    return (provider or "").strip().lower() in KNOWN_PROVIDERS


def _launchable_caps() -> dict[str, tuple[str, str]]:
    return {
        "READ_REPOSITORY": ("SUPPORTED", ""),
        "EDIT_SOURCE": ("SUPPORTED", ""),
        "CREATE_COMMIT": ("SUPPORTED", ""),
        "RUN_TESTS": ("SUPPORTED", ""),
        "READ_TEST_RESULTS": ("SUPPORTED", ""),
        "USE_INTERACTIVE_TERMINAL": ("SUPPORTED", ""),
        "USE_BROWSER": ("SUPPORTED", ""),
        "READ_SPEC": ("SUPPORTED", ""),
        "WRITE_SPEC": ("PARTIAL", "SpecRegistry never writes; an agent could edit the YAML files directly in its worktree like any other source file, but no dedicated workflow exists yet."),
        "PLAN_TASK": ("PARTIAL", "No planner/decomposition workflow exists yet (deferred) -- an agent can be asked to propose a plan as free-text output, not create Tasks itself."),
        "REVIEW_SOURCE": ("SUPPORTED", ""),
        "REVIEW_DIFF": ("SUPPORTED", ""),
        "SUBMIT_REVIEW": ("SUPPORTED", "review_runs.reviewer_agent is a free-text label recorded by a human submitting the result; ProjectFlow never launches this provider as its own reviewer process."),
        "RUN_RUNTIME_VERIFICATION": ("PARTIAL", "ProjectFlow has no automated QA agent launch path -- QA/Runtime Verification is recorded as a human/manual result (manual_verifications.operator); a provider name here is a label only."),
        "RECORD_VERIFICATION": ("PARTIAL", "Same as RUN_RUNTIME_VERIFICATION -- recording is a human action today."),
        "RESOLVE_CONFLICTS": ("PARTIAL", "integration_workspaces has no stored provider/launch path -- a human resolves conflicts, optionally running this provider's CLI manually outside ProjectFlow's controlled session."),
        "CREATE_INTEGRATION": ("PARTIAL", "Create Integration is a human-clicked route, not agent-launched."),
        "PUSH_BRANCH": ("PARTIAL", "Push Integration Branch is a human-clicked route; no agent launches it directly."),
        "CREATE_PR": ("PARTIAL", "Create PR is a human-clicked route calling GitHubMergeService directly; no agent process performs the call."),
        "READ_CI": ("PARTIAL", "CI/mergeability status is read by the route/template, not by an agent."),
        "MERGE_PR": ("UNSUPPORTED", "Policy-controlled/human-gated -- see docs/ENGINEERING_ROLES.md."),
        "BUILD_ARTIFACT": ("PARTIAL", "DeploymentService runs the build; not agent-launched."),
        "DEPLOY_DEV": ("PARTIAL", "Deploy to DEV is a human-clicked route; not agent-launched."),
        "DEPLOY_TEST": ("UNSUPPORTED", "No route exposes a TEST environment deploy yet."),
        "DEPLOY_PRODUCTION": ("UNSUPPORTED", "Policy-controlled/human-gated; no route exposes production deploy."),
        "ROLLBACK_DEPLOYMENT": ("UNSUPPORTED", "Policy-controlled/human-gated."),
        "READ_EVIDENCE": ("SUPPORTED", ""),
        "WRITE_WORK_PRODUCT": ("SUPPORTED", ""),
        # Phase E6: all reasoning-only (bounded structured context, no
        # tool access -- PlannerAgentInvoker.invoke() runs with
        # --tools "" exactly like the Planner/Spec roles do), so every
        # one is SUPPORTED for a real launchable provider. None of them
        # touches deployment/release.
        "REPOSITORY_ANALYSIS": ("SUPPORTED", "Bounded, read-only top-level structural metadata only (ArchitectureContextBuilder) -- never a live shell/tool call."),
        "ARCHITECTURE_REASONING": ("SUPPORTED", ""),
        "DEPENDENCY_ANALYSIS": ("SUPPORTED", ""),
        "TECHNICAL_DESIGN": ("SUPPORTED", ""),
        "API_DESIGN": ("SUPPORTED", ""),
        "DATA_DESIGN": ("SUPPORTED", ""),
        "UI_UX_DESIGN": ("SUPPORTED", ""),
        "FAILURE_MODE_ANALYSIS": ("SUPPORTED", ""),
        "MIGRATION_DESIGN": ("SUPPORTED", "Design only -- never executes a migration."),
        "ARCHITECTURE_REVIEW": ("SUPPORTED", ""),
        # Phase E7: reasoning-only, same as E6's block above.
        "TEST_DESIGN": ("SUPPORTED", ""),
        "ACCEPTANCE_TEST_DESIGN": ("SUPPORTED", ""),
        "INVARIANT_TEST_DESIGN": ("SUPPORTED", ""),
        "REGRESSION_TEST_DESIGN": ("SUPPORTED", ""),
        "FAILURE_CASE_DESIGN": ("SUPPORTED", ""),
        "COVERAGE_ANALYSIS": ("SUPPORTED", ""),
        "TEST_REVIEW": ("SUPPORTED", ""),
    }


def _no_adapter_caps() -> dict[str, tuple[str, str]]:
    note = "No adapter/launcher wired in ProjectFlow for this provider name -- settings.agents allows it as a label, but AGENT_LAUNCHERS has no entry, so no session can actually be started."
    return {key: ("UNSUPPORTED", note) for key in CAPABILITIES}


PROVIDER_CAPABILITIES: dict[str, dict[str, tuple[str, str]]] = {
    "codex": _launchable_caps(),
    "claude": _launchable_caps(),
    "gemini": _no_adapter_caps(),
    "aider": _no_adapter_caps(),
    "other": _no_adapter_caps(),
}


class CatalogError(ValueError):
    pass


class RoleCapabilityService:
    def __init__(self, db, providers=KNOWN_PROVIDERS):
        self.db = db
        self.providers = tuple(providers)

    # ---- seeding (idempotent, restart-safe -- E2 section 21/23) -----
    def seed(self) -> None:
        for key, role in ROLES.items():
            self.db.execute(
                "INSERT INTO engineering_roles(key,name,description,category,system_defined) VALUES(?,?,?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET name=excluded.name,description=excluded.description,category=excluded.category,updated_at=CURRENT_TIMESTAMP",
                (key, role["name"], role["description"], role["category"]),
            )
        for key, cap in CAPABILITIES.items():
            self.db.execute(
                "INSERT INTO capabilities(key,name,description,category,sensitivity,system_defined) VALUES(?,?,?,?,?,1) "
                "ON CONFLICT(key) DO UPDATE SET name=excluded.name,description=excluded.description,category=excluded.category,sensitivity=excluded.sensitivity,updated_at=CURRENT_TIMESTAMP",
                (key, cap["name"], cap["description"], cap["category"], cap["sensitivity"]),
            )
        role_ids = {r["key"]: r["id"] for r in self.db.all("SELECT id,key FROM engineering_roles")}
        cap_ids = {c["key"]: c["id"] for c in self.db.all("SELECT id,key FROM capabilities")}
        for role_key, groups in ROLE_CAPABILITIES.items():
            for requirement, cap_keys in groups.items():
                for cap_key in cap_keys:
                    self.db.execute(
                        "INSERT INTO role_capabilities(role_id,capability_id,requirement) VALUES(?,?,?) "
                        "ON CONFLICT(role_id,capability_id) DO UPDATE SET requirement=excluded.requirement",
                        (role_ids[role_key], cap_ids[cap_key], requirement),
                    )
        for provider, caps in PROVIDER_CAPABILITIES.items():
            for cap_key, (support_level, notes) in caps.items():
                self.db.execute(
                    "INSERT INTO agent_capabilities(provider,capability_id,support_level,source,notes) VALUES(?,?,?,'BUILTIN',?) "
                    "ON CONFLICT(provider,capability_id) DO UPDATE SET support_level=excluded.support_level,notes=excluded.notes,source='BUILTIN',updated_at=CURRENT_TIMESTAMP",
                    (provider, cap_ids[cap_key], support_level, notes),
                )

    # ---- read/query ---------------------------------------------------
    def get_role(self, role_key: str) -> dict | None:
        return self.db.one("SELECT * FROM engineering_roles WHERE key=?", ((role_key or "").strip().upper(),))

    def list_roles(self) -> list[dict]:
        return self.db.all("SELECT * FROM engineering_roles ORDER BY category,key")

    def list_capabilities(self) -> list[dict]:
        return self.db.all("SELECT * FROM capabilities ORDER BY category,key")

    def capabilities_for_role(self, role_key: str) -> list[dict]:
        return self.db.all(
            "SELECT c.*, rc.requirement FROM role_capabilities rc "
            "JOIN capabilities c ON c.id=rc.capability_id JOIN engineering_roles r ON r.id=rc.role_id "
            "WHERE r.key=? ORDER BY rc.requirement DESC, c.key",
            ((role_key or "").strip().upper(),),
        )

    def capabilities_for_provider(self, provider: str) -> list[dict]:
        return self.db.all(
            "SELECT c.*, ac.support_level, ac.notes FROM agent_capabilities ac "
            "JOIN capabilities c ON c.id=ac.capability_id WHERE ac.provider=? ORDER BY c.key",
            ((provider or "").strip().lower(),),
        )

    def provider_supports_role(self, provider: str, role_key: str, project_policy: dict | None = None) -> bool:
        return self.validate_assignment(provider, role_key, project_policy)["valid"]

    def validate_assignment(self, provider: str, role_key: str, project_policy: dict | None = None) -> dict:
        """Never silently accepts an impossible assignment (E2 section
        10): checks every REQUIRED capability the role needs against
        what the provider's catalog row says it supports, plus an
        optional repository-level PROJECT.yaml `engineering:` policy
        narrowing (E2 section 18) -- a policy can only RESTRICT which
        providers may hold a role, never grant a capability the global
        catalog doesn't already say the provider supports."""
        provider_n = (provider or "").strip().lower()
        role_key_n = (role_key or "").strip().upper()
        role = self.get_role(role_key_n)
        if not role:
            return {"valid": False, "provider": provider_n, "role": role_key_n,
                    "missing_required_capabilities": [], "partial_capabilities": [],
                    "policy_blocked": False, "warnings": [f"Unknown role: {role_key_n}"]}
        required = self.capabilities_for_role(role_key_n)
        provider_caps = {c["key"]: c for c in self.capabilities_for_provider(provider_n)}
        missing: list[str] = []
        partial: list[str] = []
        warnings: list[str] = []
        if provider_n not in self.providers:
            warnings.append(f"'{provider_n}' is not a recognized provider in this ProjectFlow instance's configured agents.")
        for cap in required:
            if cap["requirement"] != "REQUIRED":
                continue
            level = provider_caps[cap["key"]]["support_level"] if cap["key"] in provider_caps else "UNSUPPORTED"
            if level == "UNSUPPORTED":
                missing.append(cap["key"])
            elif level == "PARTIAL":
                partial.append(cap["key"])
        policy_blocked = False
        if project_policy:
            role_cfg = (project_policy.get("roles") or {}).get(role_key_n) or {}
            allowed = role_cfg.get("allowed_providers")
            if allowed and provider_n not in [str(a).strip().lower() for a in allowed]:
                policy_blocked = True
                warnings.append(f"Repository policy restricts {role_key_n} to {allowed}; '{provider_n}' is not listed.")
        return {"valid": (not missing) and (not policy_blocked), "provider": provider_n, "role": role_key_n,
                "missing_required_capabilities": missing, "partial_capabilities": partial,
                "policy_blocked": policy_blocked, "warnings": warnings}

    def recommended_roles_for_change(self, change_type: str, risk_level: str) -> list[str]:
        """Advisory/catalog metadata only (E2 section 17) -- never
        creates a Task, Change, or agent. risk_level sets the base
        ladder (matching the spec's own worked examples); change_type
        can add a role on top, never remove one."""
        risk = (risk_level or "NORMAL").strip().upper()
        ctype = (change_type or "").strip().upper()
        base = {
            "LOW": ["BUILDER", "REVIEWER"],
            "NORMAL": ["BUILDER", "REVIEWER", "INTEGRATOR"],
            "HIGH": ["BUILDER", "REVIEWER", "QA_VERIFIER", "INTEGRATOR", "RELEASE_MANAGER"],
        }.get(risk, ["BUILDER", "REVIEWER", "INTEGRATOR"])
        roles = list(base)
        if ctype == "FEATURE" and "SPEC_ANALYST" not in roles:
            roles.insert(0, "SPEC_ANALYST")
        if ctype == "ARCHITECTURE_CHANGE":
            for r in ("PLANNER", "SPEC_ANALYST", "SOFTWARE_ARCHITECT", "TECHNICAL_DESIGNER"):
                if r not in roles:
                    roles.insert(0, r)
        if ctype == "SECURITY_CHANGE" and "SECURITY_REVIEWER" not in roles:
            roles.append("SECURITY_REVIEWER")
        return roles
